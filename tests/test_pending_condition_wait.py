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
    PAUSED_RETRY_HOLD_MS,
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


async def _fire_retry(orch, issue, monkeypatch) -> dict:
    """Fire the scheduled retry through the real timer→dispatch path.

    Captures the `attempt`/`attempt_kind` `_process_retry` hands to
    `_dispatch`, so re-installing a running entry with the captured attempt
    reproduces production retry propagation (no harness reset of
    `entry.retry_attempt`). A paused ticket is resumed through the real
    `resume_worker` first — its immediately-fired timer and the direct
    `_on_retry_timer` call below both target the same captured dispatch.
    """
    captured: dict = {}

    async def _fetch(_cfg):
        return [issue]

    def _dispatch(captured_issue, _cfg, *, attempt, attempt_kind=None):
        captured["attempt"] = attempt
        captured["attempt_kind"] = attempt_kind

    monkeypatch.setattr(orch, "_fetch_candidates", _fetch)
    monkeypatch.setattr(orch, "_dispatch", _dispatch)
    if orch.is_paused(issue.id):
        assert orch.resume_worker(issue.id) is True
    await orch._on_retry_timer(issue.id)
    return captured


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
        assert retry.attempt == 0  # pass-through: no history, no inflation
        # Backoff starts at the first scheduled-wake delay.
        assert retry.due_at_ms - orch._loop.time() * 1000.0 == pytest.approx(
            CI_WAIT_RETRY_DELAYS_MS[0], abs=5_000
        )
        flags = registry.get_issue_flags(issue.id)
        assert flags is None  # nothing written, nothing cleared
        assert issue.id not in orch._persisted_retry_attempts
        _drain_retry(orch, issue)

    asyncio.run(_run())


def test_backoff_progression_and_cap_exemption(tmp_path, monkeypatch):
    """Eight wakes through the real timer→dispatch path.

    The attempt never moves off 0, the delay progression comes from the
    wakeup counter alone, and `agent.max_retries` (3 in the harness) is
    never approached — no wakeup inflates the failure budget.
    """
    cfg = _pending_cfg()
    orch = _orch()
    issue = _issue("MT-CI-2", state="In Progress")
    registry = RunRegistry(tmp_path / ".symphony" / "state.db")
    delays: list[float] = []

    async def _run() -> None:
        orch._loop = asyncio.get_running_loop()
        orch._run_registry = registry
        monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
        _install_running_entry(orch, issue)

        for _ in range(8):  # far beyond max_retries=3
            await _exit_pending(orch, issue)
            retry = orch._retry[issue.id]
            assert retry.kind == "ci_wait"
            assert retry.attempt == 0
            delays.append(retry.due_at_ms - orch._loop.time() * 1000.0)
            captured = await _fire_retry(orch, issue, monkeypatch)
            assert captured == {"attempt": 0, "attempt_kind": "ci_wait"}
            entry = _install_running_entry(orch, issue)  # exit popped it
            entry.retry_attempt = captured["attempt"]

        assert orch.is_paused(issue.id) is False
        assert issue.id not in orch._pending_escalations
        assert registry.get_issue_flags(issue.id) is None  # budget untouched
        assert orch._ci_wait_wakeups[issue.id] == 8
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
        # The ordinary operator pause, not a max-retries exhaustion: the
        # re-park is installed at the pause cadence with the preserved
        # attempt (no history → 0, never incremented).
        retry = orch._retry[issue.id]
        assert retry.kind == "retry"
        assert retry.attempt == 0
        assert retry.due_at_ms - orch._loop.time() * 1000.0 == pytest.approx(
            PAUSED_RETRY_HOLD_MS, abs=1_000
        )
        assert issue.id not in orch._pending_escalations
        flags = registry.get_issue_flags(issue.id)
        assert flags is not None and flags.paused is True
        assert flags.retry_attempt in (None, 0)
        assert issue.id not in orch._ci_wait_started_at
        _drain_retry(orch, issue)

    asyncio.run(_run())


def test_pending_wait_then_exit_47_accounts_the_first_failure(tmp_path, monkeypatch):
    """A pending wake is invisible to failure accounting.

    Four pending wakes, then a real exit 47 (unknown evidence): that failure
    is still failure #1 — not #1+wakeups — and it takes the ordinary
    legacy pause, not a premature cap exhaustion.
    """
    cfg = _pending_cfg()
    orch = _orch()
    issue = _issue("MT-CI-9", state="In Progress")
    registry = RunRegistry(tmp_path / ".symphony" / "state.db")

    async def _run() -> None:
        orch._loop = asyncio.get_running_loop()
        orch._run_registry = registry
        monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
        _install_running_entry(orch, issue)

        for _ in range(4):
            await _exit_pending(orch, issue)
            captured = await _fire_retry(orch, issue, monkeypatch)
            assert captured == {"attempt": 0, "attempt_kind": "ci_wait"}
            entry = _install_running_entry(orch, issue)
            entry.retry_attempt = captured["attempt"]

        await orch._on_worker_exit(
            issue.id, "before_run_error", _pending_error(47), exit_code=47
        )
        assert orch.is_paused(issue.id) is True  # ordinary legacy pause
        retry = orch._retry[issue.id]
        assert retry.kind == "retry"
        assert retry.attempt == 1  # first failure, undistorted by the waits
        assert registry.get_issue_flags(issue.id).retry_attempt == 1
        assert issue.id not in orch._pending_escalations
        _drain_retry(orch, issue)

    asyncio.run(_run())


def test_real_failure_history_survives_a_pending_wait(tmp_path, monkeypatch):
    """failure → wait → failure accounts as 1 → (invisible) → 2.

    All propagation is real: each retry fires through `_on_retry_timer`
    into `_dispatch`, and each entry's `retry_attempt` comes from that
    captured dispatch — the harness never resets it.
    """
    cfg = _pending_cfg()
    orch = _orch()
    issue = _issue("MT-CI-10", state="In Progress")
    registry = RunRegistry(tmp_path / ".symphony" / "state.db")

    async def _run() -> None:
        orch._loop = asyncio.get_running_loop()
        orch._run_registry = registry
        monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)

        # Real failure #1 (unknown evidence): ordinary pause + retry #1.
        entry = _install_running_entry(orch, issue)
        entry.retry_attempt = None
        await orch._on_worker_exit(
            issue.id, "before_run_error", _pending_error(47), exit_code=47
        )
        assert orch._retry[issue.id].attempt == 1
        assert registry.get_issue_flags(issue.id).retry_attempt == 1
        captured = await _fire_retry(orch, issue, monkeypatch)  # resumes + fires
        assert captured == {"attempt": 1, "attempt_kind": "retry"}

        # A pending wait in between: history must pass through untouched.
        entry = _install_running_entry(orch, issue)
        entry.retry_attempt = captured["attempt"]
        await _exit_pending(orch, issue)
        retry = orch._retry[issue.id]
        assert retry.kind == "ci_wait"
        assert retry.attempt == 1  # preserved, not incremented to 2
        assert orch._persisted_retry_attempts.get(issue.id) == 1
        assert registry.get_issue_flags(issue.id).retry_attempt == 1
        captured = await _fire_retry(orch, issue, monkeypatch)
        assert captured == {"attempt": 1, "attempt_kind": "ci_wait"}

        # Real failure #2: attempt 2 — history + 1, not history + wakeups + 1.
        entry = _install_running_entry(orch, issue)
        entry.retry_attempt = captured["attempt"]
        await orch._on_worker_exit(
            issue.id, "before_run_error", _pending_error(47), exit_code=47
        )
        assert orch._retry[issue.id].attempt == 2
        assert registry.get_issue_flags(issue.id).retry_attempt == 2
        assert issue.id not in orch._pending_escalations  # cap (3) not tripped
        _drain_retry(orch, issue)

    asyncio.run(_run())


def test_escalation_at_the_retry_cap_pauses_instead_of_exhausting(
    tmp_path, monkeypatch
):
    """The 2h escalation is an operator pause, never a cap exhaustion.

    With the budget genuinely at 3/3, an over-ceiling pending wait still
    pauses legibly with the attempt preserved — and the ordinary
    max-retries exhaustion still fires only for the FOURTH real failure.
    """
    cfg = _pending_cfg()
    orch = _orch()
    issue = _issue("MT-CI-11", state="In Progress")
    registry = RunRegistry(tmp_path / ".symphony" / "state.db")

    async def _run() -> None:
        orch._loop = asyncio.get_running_loop()
        orch._run_registry = registry
        monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
        escalated: list[dict] = []

        async def _record_escalation(**kwargs):
            escalated.append(kwargs)

        monkeypatch.setattr(orch, "_escalate_max_retries", _record_escalation)

        for expected in (1, 2, 3):
            entry = _install_running_entry(orch, issue)
            entry.retry_attempt = None if expected == 1 else expected - 1
            await orch._on_worker_exit(
                issue.id, "before_run_error", _pending_error(47), exit_code=47
            )
            assert orch._retry[issue.id].attempt == expected
            captured = await _fire_retry(orch, issue, monkeypatch)
            assert captured == {"attempt": expected, "attempt_kind": "retry"}

        # Budget at the cap (3/3): a pending wait that outlives the ceiling.
        entry = _install_running_entry(orch, issue)
        entry.retry_attempt = 3
        orch._ci_wait_started_at[issue.id] = time.monotonic() - CI_WAIT_MAX_SECONDS - 1
        await _exit_pending(orch, issue)

        assert orch.is_paused(issue.id) is True
        retry = orch._retry[issue.id]  # installed → not the exhaustion path
        assert retry.kind == "retry"
        assert retry.attempt == 3  # preserved, not incremented to 4
        assert issue.id not in orch._pending_escalations
        assert escalated == []
        flags = registry.get_issue_flags(issue.id)
        assert flags.paused is True
        assert flags.retry_attempt == 3
        _drain_retry(orch, issue)

        # Ordinary exhaustion still works: the FOURTH real failure escalates.
        entry = _install_running_entry(orch, issue)
        entry.retry_attempt = 3
        await orch._on_worker_exit(
            issue.id, "before_run_error", _pending_error(47), exit_code=47
        )
        await asyncio.sleep(0)  # let the supervised escalation task run
        assert issue.id not in orch._retry  # cap check declined the schedule
        assert len(escalated) == 1
        assert escalated[0]["attempt"] == 4

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
    """Durability without new persistence: the durable state carries no pause
    and no erased history.

    One real failure, then a pending wait: the persisted flags still hold
    the genuine `retry_attempt`, so a restarted orchestrator rehydrates the
    failure history (dispatch restores the attempt from it) — the wait
    neither resets the budget nor leaves anything sticky behind.
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
        # Real failure #1, then resume-and-fire through the real path.
        entry = _install_running_entry(orch, issue)
        entry.retry_attempt = None
        await orch._on_worker_exit(
            issue.id, "before_run_error", _pending_error(47), exit_code=47
        )
        assert registry.get_issue_flags(issue.id).retry_attempt == 1
        captured = await _fire_retry(orch, issue, monkeypatch)
        assert captured == {"attempt": 1, "attempt_kind": "retry"}

        entry = _install_running_entry(orch, issue)
        entry.retry_attempt = captured["attempt"]
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
        # Genuine history survived the wait and the restart.
        assert restarted._persisted_retry_attempts.get(issue.id) == 1
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

