"""Stage ownership: `stage_kinds` lanes cannot be consumed by another backend.

A worker may complete its own lane and request the next state; the
orchestrator re-resolves the backend for the new lane (F-01). But an
advance observed at a turn boundary — or on a reconcile poll — that
CROSSES a lane whose `stage_kinds` owner differs from the moving worker
means the worker consumed a stage it was never authorized to execute.
The GH-165 false terminal (one implementer session self-driving
`In Progress -> Verify -> Document -> Human Review` inside a single
turn, verifier never scheduled) is the motivating case.

Semantics under test (v0.26.x):

1. Turn-end clamp: an unauthorized multi-lane advance is rewritten to
   the first crossed lane the worker does not own; the normal phase
   path then re-routes to that lane's configured backend.
2. Reconcile clamp (active and terminal): the same guard when a poll
   observes the rogue advance — the worker is cancelled and the ticket
   is clamped back; the terminal state is never accepted while the
   unauthorized advance stands.
3. A pinned ticket (explicit `agent_kind`) stays human-authoritative.
4. Boards without `stage_kinds` keep today's semantics.
5. Single forward steps and same-kind crossings remain legal.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import symphony.orchestrator.core as core_mod
from symphony.issue import Issue
from symphony.orchestrator import Orchestrator, RunningEntry
from symphony.orchestrator.entries import _IssueDebug
from symphony.orchestrator.helpers import (
    _stage_ownership_crossed_lanes,
    _stage_ownership_rewind_state,
)
from symphony.workflow import (
    AgentConfig,
    ClaudeConfig,
    CodexConfig,
    GeminiConfig,
    HooksConfig,
    PiConfig,
    PrimeAgentConfig,
    PromptConfig,
    ServerConfig,
    ServiceConfig,
    TrackerConfig,
    TuiConfig,
    WorkflowState,
)


# ---------------------------------------------------------------------------
# Config / fixtures (mirrors test_orchestrator_phase_transition.py shape)
# ---------------------------------------------------------------------------

ACTIVE_STATES = ("Todo", "In Progress", "Verify", "Document")
TERMINAL_STATES = ("Human Review", "Done", "Blocked", "Cancelled")

_STAGE_KINDS = {
    "in progress": "codex",
    "verify": "claude",
    "document": "codex",
}

# Satisfies every shipped stage-contract evaluator so ownership — not
# contract content — is the dimension under test.
_CONTRACT_CLEAN_BODY = (
    "## Plan\n"
    "- build it\n"
    "\n"
    "## Acceptance Tests\n"
    "- pytest -q\n"
    "\n"
    "## Done Signals\n"
    "- behavior visible\n"
    "\n"
    "## Implementation\n"
    "- changed source\n"
    "\n"
    "## Self-Critique\n"
    "- checked edge paths\n"
    "\n"
    "## Security Audit\n"
    "| check | verdict | evidence |\n"
    "| --- | --- | --- |\n"
    "| secrets | pass | qa/security.md |\n"
    "\n"
    "## Review\n"
    "clean\n"
    "\n"
    "## QA Evidence\n"
    "- pytest -q rc=0\n"
    "\n"
    "## AC Scorecard\n"
    "| signal | source | result | evidence |\n"
    "| --- | --- | --- | --- |\n"
    "| ac-1 | pytest | pass | MT-1/qa/version.log |\n"
    "\n"
    "## Merge Status\n"
    "merged\n"
    "\n"
    "## Wiki Updates\n"
    "- docs/llm-wiki/mt-1.md\n"
    "\n"
    "## Human Review\n"
    "ready\n"
)


def _make_config(
    *,
    max_turns: int = 6,
    max_attempts: int = 3,
    stage_kinds: dict[str, str] | None = None,
) -> ServiceConfig:
    return ServiceConfig(
        workflow_path=Path("/tmp/WORKFLOW.md"),
        poll_interval_ms=30_000,
        workspace_root=Path("/tmp/ws"),
        tracker=TrackerConfig(
            kind="file",
            endpoint="https://api.linear.app/graphql",
            api_key="tok",
            project_slug="proj",
            active_states=ACTIVE_STATES,
            terminal_states=TERMINAL_STATES,
        ),
        hooks=HooksConfig(None, None, None, None, 60_000),
        agent=AgentConfig(
            kind="codex",
            max_concurrent_agents=1,
            max_turns=max_turns,
            max_retry_backoff_ms=300_000,
            max_concurrent_agents_by_state={},
            max_attempts=max_attempts,
            stage_kinds=dict(stage_kinds) if stage_kinds is not None else {},
        ),
        codex=CodexConfig(
            command="codex app-server",
            approval_policy=None,
            thread_sandbox=None,
            turn_sandbox_policy=None,
            turn_timeout_ms=3_600_000,
            read_timeout_ms=5_000,
            stall_timeout_ms=300_000,
        ),
        claude=ClaudeConfig(
            command="claude -p",
            turn_timeout_ms=3_600_000,
            read_timeout_ms=5_000,
            stall_timeout_ms=300_000,
            resume_across_turns=True,
        ),
        gemini=GeminiConfig(
            command='gemini -p ""',
            turn_timeout_ms=3_600_000,
            read_timeout_ms=5_000,
            stall_timeout_ms=300_000,
        ),
        pi=PiConfig(
            command='pi --mode json -p ""',
            turn_timeout_ms=3_600_000,
            read_timeout_ms=5_000,
            stall_timeout_ms=300_000,
            resume_across_turns=True,
        ),
        prime_agent=PrimeAgentConfig(
            command="prime-agent -p --mode json",
            turn_timeout_ms=3_600_000,
            read_timeout_ms=5_000,
            stall_timeout_ms=300_000,
            resume_across_turns=True,
        ),
        server=ServerConfig(port=None),
        tui=TuiConfig(language="en", visible_lanes=5),
        prompts=PromptConfig(),
        prompt_template="issue={{ issue.identifier }} state={{ issue.state }} rewind={{ is_rewind }}",
    )


def _make_issue(state: str = "In Progress", *, agent_kind: str | None = None) -> Issue:
    return Issue(
        id="iss-1",
        identifier="MT-1",
        title="stage ownership fixture",
        description=_CONTRACT_CLEAN_BODY,
        priority=2,
        state=state,
        blocked_by=(),
        agent_kind=agent_kind,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _orch(tmp_path: Path) -> Orchestrator:
    state = WorkflowState(Path("/tmp/no.md"))
    o = Orchestrator(state)
    o._workspace_manager = _FakeWorkspaceManager(tmp_path)  # type: ignore[assignment]
    return o


class _FakeWorkspace:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.workspace_key = "fake"
        self.created_now = True


class _FakeWorkspaceManager:
    def __init__(self, path: Path) -> None:
        self._path = path
        self.after_run_paths: list[Path] = []

    def path_for(self, identifier: str) -> Path:
        del identifier
        return self._path

    async def create_or_reuse(self, identifier: str) -> _FakeWorkspace:
        del identifier
        return _FakeWorkspace(self._path)

    async def before_run(self, path: Path) -> None:
        del path

    async def after_run_best_effort(self, path: Path) -> None:
        self.after_run_paths.append(path)


@staticmethod
def _noop_static(*args: Any, **kwargs: Any) -> None:  # pragma: no cover
    del args, kwargs


def _seed_running_entry(
    o: Orchestrator,
    issue: Issue,
    tmp_path: Path,
    *,
    agent_kind: str = "codex",
    authorized_state: str = "In Progress",
) -> RunningEntry:
    # Contract evaluators require the producing lane's artefact directory
    # to exist in the (fake) workspace.
    work_dir = tmp_path / "docs" / issue.identifier / "work"
    qa_dir = tmp_path / "docs" / issue.identifier / "qa"
    work_dir.mkdir(parents=True, exist_ok=True)
    qa_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "notes.md").write_text("ok")
    (qa_dir / "version.log").write_text("ok")
    (qa_dir / "security.md").write_text("ok")
    entry = RunningEntry(
        issue=issue,
        started_at=datetime.now(timezone.utc),
        retry_attempt=None,
        worker_task=None,  # type: ignore[arg-type]
        workspace_path=tmp_path,
        agent_kind=agent_kind,
        authorized_state=authorized_state,
        release_authority_resolved=True,
    )
    o._running[issue.id] = entry
    o._issue_debug.setdefault(issue.id, _IssueDebug())
    return entry


def _install_fake_backend(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    instances: list[Any] = []

    def _factory(init: Any) -> Any:
        backend = _RecordingBackend(init_id=len(instances))
        backend.calls.append(("factory", {"agent_kind": init.cfg.agent.kind}))
        instances.append(backend)
        return backend

    monkeypatch.setattr(core_mod, "build_backend", _factory)
    return instances


class _RecordingBackend:
    def __init__(self, init_id: int) -> None:
        self.init_id = init_id
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def start(self) -> None:
        self.calls.append(("start", {}))

    async def initialize(self) -> None:
        self.calls.append(("initialize", {}))

    async def start_session(self, *, initial_prompt: str, issue_title: str) -> None:
        self.calls.append(("start_session", {"initial_prompt": initial_prompt}))

    async def run_turn(self, *, prompt: str, is_continuation: bool) -> None:
        self.calls.append(("run_turn", {"prompt": prompt, "is_continuation": is_continuation}))

    async def stop(self) -> None:
        self.calls.append(("stop", {}))


def _install_state_sequence(
    monkeypatch: pytest.MonkeyPatch, states: list[str]
) -> dict[str, int]:
    calls = {"i": 0}

    async def _fake_refresh(self, cfg, issue_id):  # noqa: ANN001
        del self, cfg, issue_id
        idx = calls["i"]
        calls["i"] += 1
        # After the sequence is exhausted, hold the last observed state so
        # the attempt ends through the terminal/STOP path rather than a
        # synthetic extra transition.
        next_state = states[idx] if idx < len(states) else states[-1]
        return Issue(
            id="iss-1",
            identifier="MT-1",
            title="stage ownership fixture",
            description=_CONTRACT_CLEAN_BODY,
            priority=2,
            state=next_state,
            blocked_by=(),
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(Orchestrator, "_refresh_issue_state", _fake_refresh)
    return calls


# ---------------------------------------------------------------------------
# Pure helper semantics
# ---------------------------------------------------------------------------


def test_crossed_lanes_between_adjacent_and_multi_lane_states() -> None:
    cfg = _make_config()
    assert (
        _stage_ownership_crossed_lanes(
            cfg, from_state="In Progress", to_state="Verify"
        )
        == []
    )
    assert (
        _stage_ownership_crossed_lanes(
            cfg, from_state="In Progress", to_state="Document"
        )
        == ["Verify"]
    )
    # A terminal destination counts as beyond the last active lane.
    assert (
        _stage_ownership_crossed_lanes(
            cfg, from_state="In Progress", to_state="Human Review"
        )
        == ["Verify", "Document"]
    )
    # Backward moves cross nothing.
    assert (
        _stage_ownership_crossed_lanes(cfg, from_state="Document", to_state="In Progress")
        == []
    )
    # Unknown origin crosses nothing (drift is not an advance).
    assert (
        _stage_ownership_crossed_lanes(cfg, from_state="Nowhere", to_state="Document")
        == []
    )


def test_rewind_state_requires_crossed_lane_owned_by_other_backend() -> None:
    cfg = _make_config(stage_kinds=_STAGE_KINDS)
    issue = _make_issue("In Progress")

    # Verify is owned by claude; a codex worker cannot consume it.
    assert (
        _stage_ownership_rewind_state(
            cfg, issue, from_state="In Progress", to_state="Human Review", worker_kind="codex"
        )
        == "Verify"
    )
    assert (
        _stage_ownership_rewind_state(
            cfg, issue, from_state="In Progress", to_state="Document", worker_kind="codex"
        )
        == "Verify"
    )
    # Adjacent single step: a legal "request the next lane" — None.
    assert (
        _stage_ownership_rewind_state(
            cfg, issue, from_state="In Progress", to_state="Verify", worker_kind="codex"
        )
        is None
    )
    # claude owns Verify but not Document.
    assert (
        _stage_ownership_rewind_state(
            cfg, _make_issue("Verify"), from_state="Verify", to_state="Human Review", worker_kind="claude"
        )
        == "Document"
    )
    # The documenter (codex) owns the step into the terminal lane.
    assert (
        _stage_ownership_rewind_state(
            cfg, _make_issue("Document"), from_state="Document", to_state="Human Review", worker_kind="codex"
        )
        is None
    )


def test_rewind_state_respects_pins_and_missing_stage_kinds() -> None:
    cfg = _make_config(stage_kinds=_STAGE_KINDS)
    pinned = _make_issue("In Progress", agent_kind="codex")
    # An explicit ticket pin is human authority: no clamp.
    assert (
        _stage_ownership_rewind_state(
            cfg, pinned, from_state="In Progress", to_state="Human Review", worker_kind="codex"
        )
        is None
    )
    unkinded = _make_config(stage_kinds=None)
    assert unkinded.agent.stage_kinds == {}
    assert (
        _stage_ownership_rewind_state(
            unkinded,
            _make_issue("In Progress"),
            from_state="In Progress",
            to_state="Human Review",
            worker_kind="codex",
        )
        is None
    )
    # An unknown mover kind falls back to the workflow default kind; the
    # clamp then compares the crossed lanes against that default.
    cfg2 = _make_config(stage_kinds=_STAGE_KINDS)
    assert cfg2.agent.kind == "codex"
    assert (
        _stage_ownership_rewind_state(
            cfg2,
            _make_issue("In Progress"),
            from_state="In Progress",
            to_state="Human Review",
            worker_kind="",
        )
        == "Verify"
    )


def test_same_kind_multi_lane_crossing_is_not_clamped() -> None:
    cfg = _make_config(stage_kinds={"in progress": "codex", "verify": "codex", "document": "codex"})
    assert (
        _stage_ownership_rewind_state(
            cfg,
            _make_issue("In Progress"),
            from_state="In Progress",
            to_state="Document",
            worker_kind="codex",
        )
        is None
    )


# ---------------------------------------------------------------------------
# Turn-end clamp inside the worker loop
# ---------------------------------------------------------------------------


def test_turn_end_multi_lane_self_drive_clamps_to_first_unowned_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The GH-165 shape: one worker self-drives to a terminal lane in one turn."""
    cfg = _make_config(stage_kinds=_STAGE_KINDS)
    issue = _make_issue(state="In Progress")
    o = _orch(tmp_path)
    _seed_running_entry(o, issue, tmp_path)
    instances = _install_fake_backend(monkeypatch)
    _install_state_sequence(monkeypatch, ["Human Review", "Verify", "Human Review"])

    updates: list[tuple[str, str]] = []
    notes: list[str] = []
    monkeypatch.setattr(
        Orchestrator,
        "_tracker_call_update_state",
        staticmethod(lambda _cfg, issue_arg, target: updates.append((issue_arg.state, target))),
    )
    monkeypatch.setattr(
        Orchestrator,
        "_tracker_call_append_note",
        staticmethod(lambda _cfg, issue_arg, heading, body: notes.append(heading)),
    )

    asyncio.run(o._run_agent_attempt(issue, attempt=None, cfg=cfg))

    # The clamp rewrote the observed Human Review advance back to Verify;
    # the claude verifier's later jump straight to the terminal lane was
    # likewise clamped to Document (claude does not own Document).
    assert updates == [("Human Review", "Verify"), ("Human Review", "Document")]
    assert notes == ["Stage Ownership", "Stage Ownership"]
    # Routing walked the configured owners: codex (In Progress), claude
    # (Verify after the first clamp), codex (Document after the second).
    kinds = [
        c[1]["agent_kind"] for inst in instances for c in inst.calls if c[0] == "factory"
    ]
    assert kinds == ["codex", "claude", "codex"]


def test_turn_end_single_step_advance_is_never_clamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_config(stage_kinds=_STAGE_KINDS)
    issue = _make_issue(state="In Progress")
    o = _orch(tmp_path)
    _seed_running_entry(o, issue, tmp_path)
    instances = _install_fake_backend(monkeypatch)
    _install_state_sequence(monkeypatch, ["Verify", "Document", "Human Review"])

    updates: list[tuple[str, str]] = []
    monkeypatch.setattr(
        Orchestrator,
        "_tracker_call_update_state",
        staticmethod(lambda _cfg, issue_arg, target: updates.append((issue_arg.state, target))),
    )

    asyncio.run(o._run_agent_attempt(issue, attempt=None, cfg=cfg))

    # No clamp fired: every observed step was a legal adjacent request.
    assert updates == []
    # Routing still walked the configured owners: codex -> claude -> codex.
    kinds = [
        c[1]["agent_kind"] for inst in instances for c in inst.calls if c[0] == "factory"
    ]
    assert kinds == ["codex", "claude", "codex"]


def test_turn_end_clamp_aborts_when_tracker_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed clamp must fail the attempt, never accept the advance."""
    cfg = _make_config(stage_kinds=_STAGE_KINDS)
    issue = _make_issue(state="In Progress")
    o = _orch(tmp_path)
    _seed_running_entry(o, issue, tmp_path)
    instances = _install_fake_backend(monkeypatch)
    _install_state_sequence(monkeypatch, ["Human Review", "Done"])

    updates: list[tuple[str, str]] = []

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("board unavailable")

    monkeypatch.setattr(Orchestrator, "_tracker_call_append_note", staticmethod(_boom))
    monkeypatch.setattr(
        Orchestrator,
        "_tracker_call_update_state",
        staticmethod(lambda _cfg, issue_arg, target: updates.append((issue_arg.state, target))),
    )

    asyncio.run(o._run_agent_attempt(issue, attempt=None, cfg=cfg))

    # The clamp never rewrote the board and the attempt aborted instead of
    # routing a new backend for the unauthorized advance: fail closed.
    assert updates == []
    rerouted_kinds = [
        c[1]["agent_kind"] for inst in instances for c in inst.calls if c[0] == "factory"
    ]
    assert rerouted_kinds == ["codex"]


# ---------------------------------------------------------------------------
# Reconcile clamp (poll observes the rogue advance)
# ---------------------------------------------------------------------------


def _reconcile_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    board_state: str,
    entry_state: str,
    authorized_state: str,
    agent_kind: str = "codex",
) -> tuple[Orchestrator, RunningEntry, list[tuple[str, str]], list[str]]:
    cfg = _make_config(stage_kinds=_STAGE_KINDS)
    o = _orch(tmp_path)
    entry = _seed_running_entry(
        o, _make_issue(entry_state), tmp_path, agent_kind=agent_kind, authorized_state=authorized_state
    )

    updates: list[tuple[str, str]] = []
    notes: list[str] = []
    monkeypatch.setattr(
        Orchestrator,
        "_tracker_call_update_state",
        staticmethod(lambda _cfg, issue_arg, target: updates.append((issue_arg.state, target))),
    )
    monkeypatch.setattr(
        Orchestrator,
        "_tracker_call_append_note",
        staticmethod(lambda _cfg, issue_arg, heading, body: notes.append(heading)),
    )

    async def _fake_refresh_full(self, cfg, issue_id):  # noqa: ANN001
        del self, cfg, issue_id
        return _make_issue(board_state)

    monkeypatch.setattr(Orchestrator, "_refresh_issue_full", _fake_refresh_full)
    monkeypatch.setattr(
        Orchestrator,
        "_tracker_call_states_by_ids",
        staticmethod(lambda _cfg, ids: [_make_issue(board_state)]),
    )
    return o, entry, updates, notes


async def _reconcile_with_idle_worker(
    o: Orchestrator,
    entry: RunningEntry,
    cfg: ServiceConfig,
    issue: Issue,
) -> bool:
    """Run one `_reconcile_one` pass with a live idle worker task attached.

    Returns True when reconcile itself requested the worker's cancellation
    (measured before this helper's own teardown cancel).
    """

    async def _worker() -> None:
        await asyncio.sleep(3600)

    entry.worker_task = asyncio.ensure_future(_worker())
    await o._reconcile_one(
        issue,
        entry,
        cfg,
        active={s.lower() for s in cfg.tracker.active_states},
        terminal={s.lower() for s in cfg.tracker.terminal_states},
        now=datetime.now(timezone.utc),
        recent_grace_s=60.0,
    )
    assert entry.worker_task is not None
    reconcile_cancelled = entry.worker_task.cancelling() > 0
    if not entry.worker_task.cancelled():
        entry.worker_task.cancel()
    await asyncio.gather(entry.worker_task, return_exceptions=True)
    return reconcile_cancelled


def test_reconcile_terminal_self_drive_clamps_and_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    o, entry, updates, notes = _reconcile_setup(
        tmp_path,
        monkeypatch,
        board_state="Human Review",
        entry_state="In Progress",
        authorized_state="In Progress",
    )
    cfg = _make_config(stage_kinds=_STAGE_KINDS)
    issue = _make_issue("Human Review")

    entry.terminal_seen_at = None
    entry.exit_started_at = None

    reconcile_cancelled = asyncio.run(
        _reconcile_with_idle_worker(o, entry, cfg, issue)
    )

    assert ("Human Review", "Verify") in updates
    assert notes == ["Stage Ownership"]
    assert reconcile_cancelled
    assert entry.issue.state == "Verify"
    # The terminal state was never accepted.
    assert entry.terminal_seen_at is None


def test_reconcile_active_self_drive_clamps_and_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    o, entry, updates, notes = _reconcile_setup(
        tmp_path,
        monkeypatch,
        board_state="Document",
        entry_state="In Progress",
        authorized_state="In Progress",
    )
    cfg = _make_config(stage_kinds=_STAGE_KINDS)
    issue = _make_issue("Document")

    reconcile_cancelled = asyncio.run(
        _reconcile_with_idle_worker(o, entry, cfg, issue)
    )

    assert ("Document", "Verify") in updates
    assert reconcile_cancelled
    assert entry.issue.state == "Verify"


def test_reconcile_clamp_uses_authorized_state_not_laundered_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-turn poll must not launder the rogue advance into the baseline.

    `entry.issue.state` was already snapshot to the advanced lane by an
    earlier poll; the clamp must still compare against the authorized
    boundary and fire.
    """
    o, entry, updates, notes = _reconcile_setup(
        tmp_path,
        monkeypatch,
        board_state="Human Review",
        entry_state="Document",  # laundered snapshot
        authorized_state="In Progress",
    )
    cfg = _make_config(stage_kinds=_STAGE_KINDS)
    issue = _make_issue("Human Review")

    asyncio.run(_reconcile_with_idle_worker(o, entry, cfg, issue))
    assert ("Human Review", "Verify") in updates


def test_reconcile_legal_adjacent_advance_never_clamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    o, entry, updates, notes = _reconcile_setup(
        tmp_path,
        monkeypatch,
        board_state="Verify",
        entry_state="In Progress",
        authorized_state="In Progress",
    )
    cfg = _make_config(stage_kinds=_STAGE_KINDS)
    issue = _make_issue("Verify")

    reconcile_cancelled = asyncio.run(
        _reconcile_with_idle_worker(o, entry, cfg, issue)
    )
    assert updates == []
    assert not reconcile_cancelled
    # The active branch snapshot still runs for legal advances.
    assert entry.issue.state == "Verify"