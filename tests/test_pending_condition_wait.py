"""HACP #301 — declared pending-condition waits for ``before_run`` hooks.

A workflow that sets ``hooks.pending_retry_exit`` classifies exactly that
structural ``before_run`` exit code as "condition pending": no pause, no
``agent.max_retries`` consumption, a bounded non-slot-holding scheduled
wait, and escalation back to the ordinary operator pause after the
wall-clock ceiling. Every other exit code — and every other reason — keeps
the legacy worker-error path.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

# This suite has no conftest inserting tests/ on sys.path (pytest 9 default
# import mode), so the shared dispatch-test harness is imported explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from symphony.errors import ConfigValidationError, SymphonyError
from symphony.orchestrator.attempt import _hook_exit_code
from symphony.orchestrator.constants import (
    CI_WAIT_MAX_SECONDS,
    CI_WAIT_RETRY_DELAYS_MS,
)
from symphony.orchestrator.core import Orchestrator
from symphony.orchestrator.entries import _IssueDebug
from symphony.orchestrator.run_registry import RunRegistry
from symphony.workflow import build_service_config, load_workflow
from symphony.workflow.config import HooksConfig
from symphony.workflow.state import WorkflowState

from test_orchestrator_dispatch import (
    _install_running_entry,
    _issue,
    _make_config,
    _orch,
)


def _hooks(pending: int) -> HooksConfig:
    return HooksConfig(None, None, None, None, 60_000, pending_retry_exit=pending)


def _pending_cfg(pending: int = 75):
    return _make_config(
        workflow_path=Path("/tmp/WORKFLOW.md"),
        workspace_root=Path("/tmp/ws"),
        hooks=_hooks(pending),
    )


def _pending_error(code: int = 75) -> str:
    # The exact shape `WorkspaceManager._run_hook` renders via SymphonyError.
    return (
        f"symphony_error: hook before_run exited {code}; stderr: FAIL: pending "
        f"(hook='before_run' returncode={code})"
    )


async def _exit_pending(orch, issue, code: int = 75, cfg=None) -> None:
    del cfg  # workflow state already stubbed by the caller
    await orch._on_worker_exit(
        issue.id, "before_run_error", _pending_error(code), exit_code=code
    )


def _drain_retry(orch, issue) -> None:
    retry = orch._retry.pop(issue.id, None)
    if retry is not None:
        retry.timer_handle.cancel()


# ----------------------------------------------------------------------
# config declaration
# ----------------------------------------------------------------------


def test_pending_retry_exit_validation(tmp_path):
    def _cfg(yaml_fragment: str):
        path = tmp_path / "WORKFLOW.md"
        path.write_text(
            "---\ntracker: { kind: file, board_root: ./kanban }\n"
            + yaml_fragment
            + "\n---\nbody\n",
            encoding="utf-8",
        )
        return build_service_config(load_workflow(path))

    assert _cfg("").hooks.pending_retry_exit == 0
    assert _cfg("hooks:\n  pending_retry_exit: 75").hooks.pending_retry_exit == 75
    for bad in (1, 63, 126, "later"):
        with pytest.raises(ConfigValidationError):
            _cfg(f"hooks:\n  pending_retry_exit: {bad}")


def test_hook_exit_code_reads_structural_context_only():
    forged_text = (
        "hook before_run exited 47; stderr: boom (hook='before_run' returncode=75)"
    )
    # The structural context wins over any returncode rendered into the text.
    assert _hook_exit_code(
        SymphonyError(forged_text, hook="before_run", returncode=47)
    ) == 47
    # No hook-runner context → nothing classified, even with forged text.
    assert _hook_exit_code(RuntimeError(forged_text)) is None
    # Non-positive / absent codes never classify.
    assert _hook_exit_code(SymphonyError("x", returncode=0)) is None
    assert _hook_exit_code(SymphonyError("x")) is None


# ----------------------------------------------------------------------
# classification + scheduling
# ----------------------------------------------------------------------


def test_matching_exit_schedules_wait_without_pause(tmp_path, monkeypatch):
    cfg = _pending_cfg()
    orch = _orch()
    issue = _issue("MT-CI-1", state="Document")
    registry = RunRegistry(tmp_path / ".symphony" / "state.db")

    async def _run() -> None:
        orch._loop = asyncio.get_running_loop()
        orch._run_registry = registry
        monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
        _install_running_entry(orch, issue)

        await _exit_pending(orch, issue)

        assert orch.is_paused(issue.id) is False
        assert issue.id not in orch._pause_reasons
        retry = orch._retry[issue.id]
        assert retry.kind == "ci_wait"
        assert retry.holds_slot is False
        assert retry.attempt == 1
        # Backoff starts at the first scheduled-wake delay.
        assert retry.due_at_ms - orch._loop.time() * 1000.0 == pytest.approx(
            CI_WAIT_RETRY_DELAYS_MS[0], abs=5_000
        )
        flags = registry.get_issue_flags(issue.id)
        assert flags is None  # no retry_attempt persisted: budget untouched
        _drain_retry(orch, issue)

    asyncio.run(_run())


def test_backoff_progression_and_cap_exemption(tmp_path, monkeypatch):
    cfg = _make_config(
        workflow_path=Path("/tmp/WORKFLOW.md"),
        workspace_root=Path("/tmp/ws"),
        hooks=_hooks(75),
    )
    orch = _orch()
    issue = _issue("MT-CI-2", state="Document")
    registry = RunRegistry(tmp_path / ".symphony" / "state.db")
    delays: list[float] = []

    async def _run() -> None:
        orch._loop = asyncio.get_running_loop()
        orch._run_registry = registry
        monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
        # The harness AgentConfig leaves max_retries at DEFAULT_MAX_RETRIES=3,
        # so eight pending wakes exercise the cap exemption directly.
        _install_running_entry(orch, issue)

        for _ in range(8):  # far beyond max_retries=3
            entry = orch._running.get(issue.id) or _install_running_entry(orch, issue)
            entry.retry_attempt = None
            await _exit_pending(orch, issue)
            retry = orch._retry[issue.id]
            delays.append(retry.due_at_ms - orch._loop.time() * 1000.0)
            assert retry.kind == "ci_wait"
            _drain_retry(orch, issue)

        assert orch.is_paused(issue.id) is False
        assert issue.id not in orch._pending_escalations
        assert registry.get_issue_flags(issue.id) is None
        assert delays[0] == pytest.approx(CI_WAIT_RETRY_DELAYS_MS[0], abs=5_000)
        assert delays[1] == pytest.approx(CI_WAIT_RETRY_DELAYS_MS[1], abs=5_000)
        for later in delays[2:]:
            assert later == pytest.approx(CI_WAIT_RETRY_DELAYS_MS[2], abs=5_000)

    asyncio.run(_run())


def test_wall_clock_ceiling_escalates_to_operator_pause(tmp_path, monkeypatch):
    cfg = _pending_cfg()
    orch = _orch()
    issue = _issue("MT-CI-3", state="Document")
    registry = RunRegistry(tmp_path / ".symphony" / "state.db")

    async def _run() -> None:
        orch._loop = asyncio.get_running_loop()
        orch._run_registry = registry
        monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
        _install_running_entry(orch, issue)
        # A wait that has already exceeded the ceiling.
        orch._ci_wait_started_at[issue.id] = time.monotonic() - CI_WAIT_MAX_SECONDS - 1

        await _exit_pending(orch, issue)

        assert orch.is_paused(issue.id) is True
        reason = orch._pause_reasons[issue.id]
        assert "paused for operator inspection" in reason
        assert "pending condition (exit 75)" in reason
        flags = registry.get_issue_flags(issue.id)
        assert flags is not None and flags.paused is True
        # The escalation itself consumes the ordinary retry budget.
        assert orch._retry[issue.id].kind == "retry"
        assert issue.id not in orch._ci_wait_started_at
        _drain_retry(orch, issue)

    asyncio.run(_run())


@pytest.mark.parametrize("code", [47, None])
def test_undeclared_exit_codes_keep_the_worker_error_pause(tmp_path, monkeypatch, code):
    cfg = _pending_cfg()  # declares 75
    orch = _orch()
    issue = _issue("MT-CI-4", state="Document")
    registry = RunRegistry(tmp_path / ".symphony" / "state.db")

    async def _run() -> None:
        orch._loop = asyncio.get_running_loop()
        orch._run_registry = registry
        monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
        _install_running_entry(orch, issue)

        await orch._on_worker_exit(
            issue.id,
            "before_run_error",
            _pending_error(code if code else 47),
            exit_code=code,
        )

        assert orch.is_paused(issue.id) is True  # legacy behavior unchanged
        assert orch._retry[issue.id].kind == "retry"
        _drain_retry(orch, issue)

    asyncio.run(_run())


def test_declaration_off_keeps_legacy_behavior(tmp_path, monkeypatch):
    cfg = _pending_cfg(pending=0)  # off: nothing may classify
    orch = _orch()
    issue = _issue("MT-CI-5", state="Document")
    registry = RunRegistry(tmp_path / ".symphony" / "state.db")

    async def _run() -> None:
        orch._loop = asyncio.get_running_loop()
        orch._run_registry = registry
        monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
        _install_running_entry(orch, issue)

        await _exit_pending(orch, issue)  # exit 75, but undeclared

        assert orch.is_paused(issue.id) is True
        assert orch._retry[issue.id].kind == "retry"
        _drain_retry(orch, issue)

    asyncio.run(_run())


def test_non_pending_exit_ends_the_open_wait(tmp_path, monkeypatch):
    cfg = _pending_cfg()
    orch = _orch()
    issue = _issue("MT-CI-6", state="Document")
    registry = RunRegistry(tmp_path / ".symphony" / "state.db")

    async def _run() -> None:
        orch._loop = asyncio.get_running_loop()
        orch._run_registry = registry
        monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
        _install_running_entry(orch, issue)

        await _exit_pending(orch, issue)
        assert issue.id in orch._ci_wait_started_at
        _drain_retry(orch, issue)

        # A later, different failure (say a turn error) ends the wait: the
        # next pending condition starts a fresh wall-clock bound.
        _install_running_entry(orch, issue)
        await orch._on_worker_exit(issue.id, "turn_error", "boom", exit_code=None)
        assert issue.id not in orch._ci_wait_started_at
        assert issue.id not in orch._ci_wait_wakeups
        _drain_retry(orch, issue)

    asyncio.run(_run())


def test_restart_shape_is_an_ordinary_candidate(tmp_path, monkeypatch):
    """Durability without new persistence: the durable state carries no pause.

    After a pending wait is scheduled, the persisted issue flags record
    neither a pause nor a retry attempt, so a restarted orchestrator
    rehydrates nothing sticky: the ticket re-enters the normal candidate
    loop, whose next dispatch re-runs `before_run` (and its exact-head
    observation) from scratch.
    """
    cfg = _pending_cfg()
    orch = _orch()
    issue = _issue("MT-CI-7", state="In Progress")  # an active state in the harness
    db = tmp_path / ".symphony" / "state.db"
    registry = RunRegistry(db)

    async def _run() -> None:
        orch._loop = asyncio.get_running_loop()
        orch._run_registry = registry
        monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
        _install_running_entry(orch, issue)
        await _exit_pending(orch, issue)
        _drain_retry(orch, issue)

    asyncio.run(_run())
    registry.close()

    restarted = Orchestrator(WorkflowState(Path("/tmp/no.md")))
    registry2 = RunRegistry(db)

    async def _rehydrate() -> None:
        restarted._loop = asyncio.get_running_loop()
        restarted._run_registry = registry2
        monkeypatch.setattr(restarted._workflow_state, "current", lambda: cfg)
        restarted._rehydrate_issue_flags(
            registry2.list_issue_flags(), cfg=cfg, registry=registry2
        )
        assert restarted.is_paused(issue.id) is False
        assert issue.id not in restarted._pause_reasons
        decision = restarted._eligibility_decision(issue, cfg, owning_retry=False)
        assert decision.disposition.value != "reject"

    asyncio.run(_rehydrate())
    registry2.close()


def test_worker_body_propagates_structural_hook_exit_code(tmp_path, monkeypatch):
    """F2 §4 item 8 (HACP #301): the hook's exit code reaches the classifier.

    Drives the real ``run_agent_attempt`` body with a workspace manager whose
    ``before_run`` raises the hook runner's exact ``SymphonyError``. The
    structural ``returncode`` must arrive at the exit handler and classify
    as a pending wait — no pause, ``ci_wait`` scheduled — without any text
    parsing of the error. The backend is never built, so a pending wake
    costs zero model turns.
    """
    cfg = _pending_cfg()
    orch = _orch()
    issue = _issue("MT-CI-8", state="Document")
    registry = RunRegistry(tmp_path / ".symphony" / "state.db")
    built: list[str] = []

    class _Workspace:
        path = Path("/tmp/ws-fake")

    class _PendingWorkspaceManager:
        def path_for(self, identifier):
            return Path("/tmp/ws-fake") / identifier

        async def create_or_reuse(self, identifier):
            return _Workspace()

        async def before_run(self, path):
            raise SymphonyError(
                "hook before_run exited 75; stderr: document-ci: pending",
                hook="before_run",
                returncode=75,
            )

        async def after_run_best_effort(self, path):
            return None

    def _fail_build(*_args, **_kwargs):
        built.append("backend")

        raise AssertionError("backend must not be built on a pending wake")

    async def _run() -> None:
        orch._loop = asyncio.get_running_loop()
        orch._run_registry = registry
        monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
        monkeypatch.setattr(orch, "_workspace_manager", _PendingWorkspaceManager())
        monkeypatch.setattr(orch, "_build_agent_backend", _fail_build)
        _install_running_entry(orch, issue)

        await orch._run_agent_attempt(issue, None, cfg)

        assert built == []
        assert orch.is_paused(issue.id) is False
        assert orch._retry[issue.id].kind == "ci_wait"
        _drain_retry(orch, issue)

    asyncio.run(_run())

