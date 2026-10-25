"""Budget-exhaustion grant probes (human-ai-control-plane issue #9, question B).

Shape of the HACP GH-129 incident: a worker exhausts `agent.max_turns`, the
ticket parks in Blocked with a persisted `budget_exhausted` flag, and the
owner later wants the independent Verify stage to run.

- `guard` tests pin today's safety behaviour and must pass before and after.
- `char` tests characterize the unpatched runtime.
- `SEAM` tests fail on the deployed runtime (no supported lever) and pin the
  candidate's contract: an operator grant of N explicit extra turns,
  recorded with the flag lift in one registry transaction, charged per turn
  before the turn starts, idempotent by `grant_id`, capped per ticket by
  `agent.budget_grant_max_turns`, fail-closed when the registry is unsure.
  Authentication of the operator lives in the web route (`test_webapi.py`).

All probes use the real file board tracker and the real SQLite run registry.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import fields, replace
from datetime import timedelta
from pathlib import Path

import pytest

from symphony.orchestrator import Orchestrator, _config_for_issue_agent, worker_exit
from symphony.orchestrator.core import _EligibilityDisposition
from symphony.orchestrator.entries import RunningEntry, _IssueDebug
from symphony.orchestrator.run_registry import RunRegistry
from symphony.trackers.file import FileBoardTracker, parse_ticket_file

from tests.test_orchestrator_dispatch import _make_config, _orch

ACTIVE = ("Todo", "In Progress", "Verify", "Document")
TERMINAL = ("Human Review", "Done", "Blocked", "Archive")
# HACP lane: cross-model verification, implementer != verifier.
STAGE_KINDS = {"in progress": "codex", "verify": "claude", "document": "codex"}
ID = "GH-T"
ACTOR = "operator:0123456789ab"
REASON = "PR green; run independent Verify"


def _cfg(tmp_path: Path, **agent_overrides):
    base = _make_config(
        tracker_kind="file",
        active_states=ACTIVE,
        terminal_states=TERMINAL,
        workflow_path=tmp_path / "WORKFLOW.md",
        workspace_root=tmp_path / "ws",
    )
    board_root = tmp_path / "kanban"
    board_root.mkdir(exist_ok=True)
    settings = {
        "stage_kinds": dict(STAGE_KINDS),
        "max_turns": 10,
        "max_total_turns": 30,
        "budget_exhausted_state": "Blocked",
        "budget_grant_max_turns": 10,
        **agent_overrides,
    }
    # Unpatched runtimes have no grant knob; guards must still run there.
    known = {f.name for f in fields(base.agent)}
    agent = replace(base.agent, **{k: v for k, v in settings.items() if k in known})
    return replace(
        base, tracker=replace(base.tracker, board_root=board_root), agent=agent
    )


def _db(tmp_path: Path) -> Path:
    return tmp_path / ".symphony" / "state.db"


def _registry(tmp_path: Path) -> RunRegistry:
    return RunRegistry(_db(tmp_path), lease_ttl=timedelta(minutes=5))


def _boot(tmp_path: Path, cfg) -> Orchestrator:
    """A service start: fresh process state, flags rehydrated from SQLite."""
    orch = _orch()
    orch._workflow_state.current = lambda: cfg  # type: ignore[method-assign]
    registry = _registry(tmp_path)
    orch._run_registry = registry
    orch._finish_run_registry_open(registry, cfg, registry._path)
    return orch


def _tracker(cfg) -> FileBoardTracker:
    return FileBoardTracker(cfg.tracker)


def _fetch(orch: Orchestrator, cfg, identifier: str = ID):
    issue = orch._tracker_call_fetch_issue_full_by_id(cfg, identifier)
    assert issue is not None
    return issue


def _move(orch, cfg, state: str, identifier: str = ID) -> None:
    _tracker(cfg).update_state(_fetch(orch, cfg, identifier), state)


def _flag(orch, issue_id: str) -> bool:
    flags = orch._run_registry.get_issue_flags(issue_id)
    return bool(flags and flags.budget_exhausted)


def _eligibility(orch, cfg, identifier: str = ID):
    return orch._eligibility_decision(
        _fetch(orch, cfg, identifier), cfg, owning_retry=False
    )


def _body(orch, cfg) -> str:
    return _fetch(orch, cfg).description or ""


def _exhaust_max_turns(tmp_path: Path, orch: Orchestrator, cfg, *, turns: int = 10):
    """Run the real worker-exit `max_turns` path on the ticket's worker."""
    issue = _fetch(orch, cfg)
    entry = RunningEntry(
        issue=issue,
        started_at=issue.created_at,
        retry_attempt=None,
        worker_task=None,  # type: ignore[arg-type]
        workspace_path=tmp_path / "ws" / ID,
        agent_kind=_config_for_issue_agent(cfg, issue).agent.kind,
        state_at_turn_start=issue.state,
    )
    entry.turn_count = turns
    debug = orch._issue_debug.setdefault(issue.id, _IssueDebug())
    debug.completed_turn_count += turns
    asyncio.run(worker_exit._stop_on_max_turns(orch, cfg, issue.id, entry, debug))
    return issue.id


def _incident(tmp_path: Path, lane: str = "In Progress", **agent_overrides):
    """GH-129: exhaustion in `lane` in one process, then a service restart."""
    cfg = _cfg(tmp_path, **agent_overrides)
    _tracker(cfg).create(
        identifier=ID, title="risk-B change", state=lane, description="body"
    )
    first = _boot(tmp_path, cfg)
    issue_id = _exhaust_max_turns(tmp_path, first, cfg)
    first._run_registry.close()
    return _boot(tmp_path, cfg), cfg, issue_id


def _grant(orch, *, grant_id="gh-t-grant-0001", turns=3, target="Verify", **kw):
    return asyncio.run(
        orch.grant_budget_for_issue(
            ID,
            grant_id=grant_id,
            actor=kw.pop("actor", ACTOR),
            reason=kw.pop("reason", REASON),
            extra_turns=turns,
            target_state=target,
        )
    )


def _ledger(orch, issue_id):
    return orch._run_registry.list_budget_grants(issue_id)


# ---------------------------------------------------------------------------
# guards / char: today's behaviour
# ---------------------------------------------------------------------------


def test_R1_guard_max_turns_exit_persists_flag_and_parks(tmp_path):
    cfg = _cfg(tmp_path)
    _tracker(cfg).create(identifier=ID, title="t", state="In Progress", description="b")
    orch = _boot(tmp_path, cfg)
    issue_id = _exhaust_max_turns(tmp_path, orch, cfg)
    assert _flag(orch, issue_id)
    assert _fetch(orch, cfg).state == "Blocked"
    assert "## Budget Exceeded" in _body(orch, cfg)


def test_R2_guard_flag_survives_restart(tmp_path):
    orch, _cfg_, issue_id = _incident(tmp_path)
    assert issue_id in orch._turn_budget_exhausted


def test_R3_guard_board_move_to_verify_stays_rejected(tmp_path):
    """The GH-129 operator action: Blocked -> Verify by board/API PATCH."""
    orch, cfg, _ = _incident(tmp_path)
    _move(orch, cfg, "Verify")
    decision = _eligibility(orch, cfg)
    assert decision.disposition is _EligibilityDisposition.REJECT
    assert decision.code == "budget_exhausted"


def test_R4_guard_pause_resume_control_does_not_reach_budget_hold(tmp_path):
    orch, cfg, issue_id = _incident(tmp_path)
    _move(orch, cfg, "Verify")
    assert orch.find_resumable_issue_id(ID) is None  # API resume -> 404
    assert orch.resume_worker(issue_id) is False
    assert _flag(orch, issue_id)


def test_R5_guard_retry_bookkeeping_and_second_restart_keep_flag(tmp_path):
    orch, cfg, issue_id = _incident(tmp_path)
    orch._clear_issue_flags(issue_id, retry_attempt=True, paused=True)
    orch._run_registry.close()
    again = _boot(tmp_path, cfg)
    assert issue_id in again._turn_budget_exhausted and _flag(again, issue_id)


def test_R6_char_hand_cleared_flag_grant_size_depends_on_restart(tmp_path):
    """Clearing only the flag (the forbidden registry edit) grants 0 turns in
    the same process and a whole fresh `max_total_turns` after a restart: the
    cumulative turn counter lives in memory."""
    cfg = _cfg(tmp_path)
    _tracker(cfg).create(identifier=ID, title="t", state="Verify", description="b")
    orch = _boot(tmp_path, cfg)
    issue_id = _exhaust_max_turns(tmp_path, orch, cfg, turns=30)
    orch._run_registry.clear_issue_flags(issue_id, budget_exhausted=True)
    same_process_left = 30 - orch._issue_debug[issue_id].completed_turn_count
    orch._run_registry.close()
    restarted = _boot(tmp_path, cfg)
    debug = restarted._issue_debug.get(issue_id, _IssueDebug())
    assert (same_process_left, 30 - debug.completed_turn_count) == (0, 30)


def test_R7_char_board_move_into_active_lane_erases_exhaustion_note(tmp_path):
    """Tracker G5: a move into an active lane strips `## Budget Exceeded`."""
    orch, cfg, _ = _incident(tmp_path)
    assert "## Budget Exceeded" in _body(orch, cfg)
    _move(orch, cfg, "Verify")
    assert "## Budget Exceeded" not in _body(orch, cfg)


# ---------------------------------------------------------------------------
# SEAM: grant contract
# ---------------------------------------------------------------------------


def test_G1_SEAM_one_call_grant_records_lifts_and_moves(tmp_path):
    orch, cfg, issue_id = _incident(tmp_path)
    changed, message, details = _grant(orch)
    assert changed, message
    assert details["status"] == "granted" and details["granted_total"] == 3
    [row] = _ledger(orch, issue_id)
    assert (row.grant_id, row.actor, row.extra_turns, row.turns_used) == (
        "gh-t-grant-0001", ACTOR, 3, 0,
    )
    assert (row.from_state, row.target_state) == ("Blocked", "Verify")
    assert not _flag(orch, issue_id) and issue_id not in orch._turn_budget_exhausted
    assert _fetch(orch, cfg).state == "Verify"
    assert _eligibility(orch, cfg).code != "budget_exhausted"
    body = _body(orch, cfg)
    assert "## Budget Grant" in body and "gh-t-grant-0001" in body
    assert "max_turns budget exceeded (max_turns=10/attempt)" in body  # lifted hold


def test_G2_SEAM_grant_preserves_reviewer_independence(tmp_path):
    orch, cfg, _ = _incident(tmp_path)
    assert _grant(orch)[0]
    assert _config_for_issue_agent(cfg, _fetch(orch, cfg)).agent.kind == "claude"
    front, _ = parse_ticket_file(cfg.tracker.board_root / f"{ID}.md")
    assert "agent" not in front and "agent_kind" not in front


@pytest.mark.parametrize(
    ("overrides", "turns", "fragment"),
    [
        ({"budget_grant_max_turns": 0}, 3, "disabled"),
        ({}, 0, "extra_turns"),
        ({}, 11, "extra_turns"),  # above the lifetime cap
        ({"max_turns": 2}, 3, "extra_turns"),  # above one attempt
    ],
    ids=["disabled", "zero", "over-cap", "over-attempt"],
)
def test_G3_SEAM_turns_are_explicit_and_bounded(tmp_path, overrides, turns, fragment):
    orch, _cfg_, issue_id = _incident(tmp_path, **overrides)
    changed, message, _ = _grant(orch, turns=turns)
    assert not changed and fragment in message
    assert _flag(orch, issue_id) and _ledger(orch, issue_id) == []


def test_G4_SEAM_granted_turns_are_charged_durably_across_restart(tmp_path):
    """N extra turns means N: charged before each turn, surviving restart,
    independent of the in-memory counter that a restart resets (R6)."""
    orch, cfg, issue_id = _incident(tmp_path)
    assert _grant(orch, turns=3)[0]
    assert orch._charge_budget_grant_turn(issue_id)
    assert orch._charge_budget_grant_turn(issue_id)
    orch._run_registry.close()
    again = _boot(tmp_path, cfg)
    assert again._budget_grant_active(issue_id)
    assert not again._budget_grant_spent(issue_id)
    assert again._charge_budget_grant_turn(issue_id)
    assert not again._charge_budget_grant_turn(issue_id)  # 4th turn refused
    assert again._budget_grant_spent(issue_id)
    assert again._run_registry.budget_grant_totals(issue_id) == (3, 3)


def test_G5_SEAM_lifetime_cap_spans_grants(tmp_path):
    orch, cfg, issue_id = _incident(tmp_path, budget_grant_max_turns=5)
    assert _grant(orch, turns=3)[0]
    _exhaust_max_turns(tmp_path, orch, cfg)  # the granted work runs out again
    changed, message, details = _grant(orch, grant_id="gh-t-grant-0002", turns=3)
    assert not changed and "budget_grant_max_turns" in message
    assert _flag(orch, issue_id) and _fetch(orch, cfg).state == "Blocked"
    assert _grant(orch, grant_id="gh-t-grant-0003", turns=2)[0]
    assert orch._run_registry.budget_grant_totals(issue_id) == (5, 0)


def test_G6_SEAM_replay_is_idempotent_and_conflicts_are_refused(tmp_path):
    orch, cfg, issue_id = _incident(tmp_path)
    assert _grant(orch)[0]
    changed, _message, details = _grant(orch)
    assert changed and details["status"] == "replayed"
    assert details["granted_total"] == 3 and len(_ledger(orch, issue_id)) == 1
    assert _body(orch, cfg).count("## Budget Grant") == 1
    changed, message, _ = _grant(orch, turns=5)  # same id, different grant
    assert not changed and "already used" in message
    assert len(_ledger(orch, issue_id)) == 1


@pytest.mark.parametrize(
    "kw",
    [
        {"actor": "operator:ba5eba11ba5e"},
        {"reason": "a different reason"},
        {"target": "In Progress"},
    ],
    ids=["actor", "reason", "target"],
)
def test_G6b_SEAM_a_stored_grant_is_immutable(tmp_path, kw):
    """Same id with any other field is a conflict, never a silent replay."""
    orch, cfg, issue_id = _incident(tmp_path)
    assert _grant(orch)[0]
    changed, message, _ = _grant(orch, **kw)
    assert not changed and "already used" in message
    (grant,) = _ledger(orch, issue_id)
    assert (grant.actor, grant.reason, grant.target_state) == (ACTOR, REASON, "Verify")


def test_G7_SEAM_ticket_notes_are_mirrors_not_the_ledger(tmp_path):
    """Deleting the mirror widens nothing; a replay restores it."""
    orch, cfg, issue_id = _incident(tmp_path, budget_grant_max_turns=3)
    assert _grant(orch, turns=3)[0]
    path = cfg.tracker.board_root / f"{ID}.md"
    text = path.read_text()
    path.write_text(text[: text.index("## Budget Grant")])
    assert "## Budget Grant" not in _body(orch, cfg)
    _exhaust_max_turns(tmp_path, orch, cfg)
    changed, message, _ = _grant(orch, grant_id="gh-t-grant-0002", turns=1)
    assert not changed and "budget_grant_max_turns" in message
    changed, _message, details = _grant(orch)  # replay the original id
    assert changed and details["status"] == "replayed"
    assert details["mirror"] == "written" and "gh-t-grant-0001" in _body(orch, cfg)
    assert orch._run_registry.budget_grant_totals(issue_id) == (3, 0)


def test_G8_SEAM_board_text_cannot_mint_a_grant(tmp_path):
    """Workers can write ticket text; a forged grant note lifts nothing."""
    orch, cfg, issue_id = _incident(tmp_path)
    _tracker(cfg).append_note(
        _fetch(orch, cfg), "Budget Grant", "Grant `forged-grant-01` by operator"
    )
    _move(orch, cfg, "Verify")
    orch._run_registry.close()
    again = _boot(tmp_path, cfg)
    assert issue_id in again._turn_budget_exhausted
    assert not again._budget_grant_active(issue_id)
    assert _eligibility(again, cfg).code == "budget_exhausted"


def test_G9_SEAM_crash_after_commit_converges_on_replay(tmp_path):
    """Process dies right after the registry transaction: no memory update,
    no mirror, no move. Restart + replay converge to exactly one grant."""
    orch, cfg, issue_id = _incident(tmp_path)
    issue = _fetch(orch, cfg)
    result = orch._run_registry.grant_budget(
        grant_id="gh-t-grant-0001", issue_id=issue_id, identifier=ID,
        actor=ACTOR, reason=REASON, extra_turns=3, lifetime_cap=10,
        from_state=issue.state, target_state="Verify",
    )
    assert result.status == "granted"
    orch._run_registry.close()  # crash
    again = _boot(tmp_path, cfg)
    assert issue_id not in again._turn_budget_exhausted
    assert again._budget_grant_active(issue_id)
    assert _fetch(again, cfg).state == "Blocked"  # terminal: nothing dispatches
    changed, _message, details = _grant(again)
    assert changed and details["status"] == "replayed"
    assert _fetch(again, cfg).state == "Verify"
    assert len(_ledger(again, issue_id)) == 1
    assert again._run_registry.budget_grant_totals(issue_id) == (3, 0)


def test_G10_SEAM_crash_inside_transaction_rolls_back(tmp_path, monkeypatch):
    orch, cfg, issue_id = _incident(tmp_path)

    def _boom(*_a, **_k):
        raise RuntimeError("injected crash inside the grant transaction")

    monkeypatch.setattr(orch._run_registry, "_write_issue_flags", _boom)
    changed, message, _ = _grant(orch)
    assert not changed and "not recorded" in message
    monkeypatch.undo()
    assert _flag(orch, issue_id) and _ledger(orch, issue_id) == []
    assert issue_id in orch._turn_budget_exhausted
    assert "## Budget Grant" not in _body(orch, cfg)
    assert _fetch(orch, cfg).state == "Blocked"


@pytest.mark.parametrize("same_id", [True, False], ids=["same-id", "distinct-ids"])
def test_G11_SEAM_concurrent_grants_from_two_processes(tmp_path, same_id):
    """Two service connections race on one database: one hold, one grant."""
    _orch_, _cfg_, issue_id = _incident(tmp_path)
    _orch_._run_registry.close()
    barrier = threading.Barrier(2)
    results: list[str] = []

    def _race(n: int) -> None:
        registry = _registry(tmp_path)
        try:
            barrier.wait()
            outcome = registry.grant_budget(
                grant_id="race-grant-0001" if same_id else f"race-grant-000{n}",
                issue_id=issue_id, identifier=ID, actor=ACTOR, reason=REASON,
                extra_turns=3, lifetime_cap=10, from_state="Blocked",
                target_state="Verify",
            )
            results.append(outcome.status)
        finally:
            registry.close()

    threads = [threading.Thread(target=_race, args=(n,)) for n in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    expected = ["granted", "replayed"] if same_id else ["granted", "not_held"]
    assert sorted(results) == sorted(expected)
    registry = _registry(tmp_path)
    assert registry.budget_grant_totals(issue_id) == (3, 0)
    assert len(registry.list_budget_grants(issue_id)) == 1


def test_G12_SEAM_registry_unavailable_fails_closed(tmp_path, monkeypatch):
    orch, _cfg_, issue_id = _incident(tmp_path)
    registry = orch._run_registry
    orch._run_registry = None
    changed, message, _ = _grant(orch)
    assert not changed and "registry unavailable" in message
    assert not orch._charge_budget_grant_turn(issue_id)
    assert orch._budget_grant_spent(issue_id)
    orch._run_registry = registry
    assert _grant(orch)[0]

    def _boom(*_a, **_k):
        raise RuntimeError("injected registry outage")

    monkeypatch.setattr(registry, "consume_budget_grant_turn", _boom)
    monkeypatch.setattr(registry, "budget_grant_totals", _boom)
    assert not orch._charge_budget_grant_turn(issue_id)
    assert orch._budget_grant_spent(issue_id)


def test_G13_SEAM_grant_refuses_running_worker_and_unheld_ticket(tmp_path):
    orch, cfg, issue_id = _incident(tmp_path)
    orch.find_running_issue_id = lambda _identifier: "x"  # type: ignore[method-assign]
    changed, message, _ = _grant(orch)
    assert not changed and "running" in message
    del orch.find_running_issue_id
    assert _grant(orch)[0]
    changed, message, _ = _grant(orch, grant_id="gh-t-grant-0002")
    assert not changed and "not held" in message
    assert len(_ledger(orch, issue_id)) == 1


@pytest.mark.parametrize("target", [None, "Done", "Human Review", "Nowhere"])
def test_G14_SEAM_grant_requires_an_active_target_lane(tmp_path, target):
    orch, cfg, issue_id = _incident(tmp_path)
    changed, message, _ = _grant(orch, target=target)
    assert not changed and "active lane" in message
    assert _flag(orch, issue_id) and _ledger(orch, issue_id) == []
    assert _fetch(orch, cfg).state == "Blocked"


@pytest.mark.parametrize(
    ("kw", "fragment"),
    [
        ({"reason": "  "}, "reason"),
        ({"grant_id": "short"}, "grant_id"),
        ({"grant_id": "bad id with spaces"}, "grant_id"),
        ({"actor": ""}, "operator identity"),
    ],
)
def test_G15_SEAM_grant_requires_reason_id_and_actor(tmp_path, kw, fragment):
    orch, _cfg_, issue_id = _incident(tmp_path)
    changed, message, _ = _grant(orch, **kw)
    assert not changed and fragment in message
    assert _flag(orch, issue_id) and _ledger(orch, issue_id) == []


# ---------------------------------------------------------------------------
# secondary C: issue detail reads the registry off its owning thread
# ---------------------------------------------------------------------------


def test_C1_SEAM_budget_snapshot_reads_the_real_registry(tmp_path):
    """GET /api/v1/issues/<id> budget panel (the 2026-10-25 `run_registry_error`
    `budget_reopen_history`): the SQLite connection is owned by the loop
    thread, so a read inside `asyncio.to_thread` always fails, the reopen
    counter reads unknown and health degrades until the next good op."""
    cfg = _cfg(tmp_path)
    _tracker(cfg).create(identifier=ID, title="t", state="Blocked", description="b")
    orch = _boot(tmp_path, cfg)
    owner = threading.get_ident()
    orch._run_registry.recent_runs(_fetch(orch, cfg).id, limit=1)  # loop-thread open
    seen: list[int] = []
    real = orch._run_registry.recent_runs

    def _spy(*a, **k):
        seen.append(threading.get_ident())
        return real(*a, **k)

    orch._run_registry.recent_runs = _spy  # type: ignore[method-assign]

    async def _snapshot():
        return await orch.issue_budget_snapshot(_fetch(orch, cfg))

    snapshot = asyncio.run(_snapshot())
    reopens = next(i for i in snapshot["items"] if i["name"] == "reopens")
    assert seen == [owner], "registry read must stay on the owning thread"
    assert orch._last_registry_error is None, orch._last_registry_error
    assert "run_registry_error" not in orch.health().get("degraded_reasons", [])
    assert reopens["used"] == 0


def test_G16_SEAM_ledger_stays_outside_the_numbered_schema_chain(tmp_path):
    """Upstream already owns schema version 9; the carried ledger must not
    claim a version, or a later upstream upgrade skips upstream's v9."""
    from symphony.orchestrator.migrations import LATEST_SCHEMA_VERSION

    registry = _registry(tmp_path)
    conn = registry._connect()
    versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations")]
    assert LATEST_SCHEMA_VERSION == 8 and max(versions) == 8
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'budget_grants'"
    ).fetchone()
    registry.close()
    _registry(tmp_path).close()  # reopening is idempotent


def test_G17_SEAM_unreadable_grant_ledger_blocks_dispatch_until_loaded(
    tmp_path, monkeypatch
):
    """A boot that cannot read which tickets hold grants must not let a
    granted ticket fall back to ordinary `max_total_turns` accounting."""
    orch, cfg, issue_id = _incident(tmp_path)
    assert _grant(orch)[0]
    orch._run_registry.close()

    def _boom(*_a, **_k):
        raise RuntimeError("injected ledger read error")

    monkeypatch.setattr(RunRegistry, "budget_grant_issue_ids", _boom)
    again = _boot(tmp_path, cfg)
    assert not again._budget_grant_active(issue_id)
    assert _eligibility(again, cfg).code == "budget_grants_unread"
    assert _eligibility(again, cfg).code == "budget_grants_unread"  # retried
    monkeypatch.undo()
    assert _eligibility(again, cfg) is None or (
        _eligibility(again, cfg).code != "budget_grants_unread"
    )
    assert again._budget_grant_active(issue_id)


@pytest.mark.parametrize(
    ("lane", "target", "allowed"),
    [
        ("Verify", "Document", False),
        ("Verify", "Verify", True),
        ("Verify", "In Progress", True),
        ("In Progress", "Verify", True),
        ("In Progress", "Document", False),
    ],
)
def test_G18_SEAM_a_grant_never_skips_verify(tmp_path, lane, target, allowed):
    orch, cfg, issue_id = _incident(tmp_path, lane=lane)
    changed, message, _ = _grant(orch, target=target)
    assert changed is allowed, message
    if not allowed:
        assert "may not skip Verify" in message
        assert _flag(orch, issue_id) and _ledger(orch, issue_id) == []
        assert _fetch(orch, cfg).state == "Blocked"


def test_G18b_SEAM_board_edits_cannot_move_the_hold_lane_forward(tmp_path):
    """Workers can edit the card; the registry keeps where the hold stopped.
    Without any record the hold counts from the first active lane."""
    orch, cfg, issue_id = _incident(tmp_path, lane="Verify")
    _move(orch, cfg, "Document")  # still held: the flag blocks dispatch
    changed, message, _ = _grant(orch, target="Document")
    assert not changed and "may not skip Verify" in message
    orch._run_registry._connect().execute("DELETE FROM budget_holds")
    _move(orch, cfg, "Blocked")
    changed, message, _ = _grant(orch, target="Document")
    assert not changed and "may not skip Verify" in message
    assert _grant(orch, target="Verify")[0]


def test_G19_SEAM_a_late_replay_never_rewinds_the_lifecycle(tmp_path):
    """Replaying a grant that already ran changes nothing: not a later hold,
    not a ticket that has since reached Human Review."""
    orch, cfg, issue_id = _incident(tmp_path, budget_grant_max_turns=3)
    assert _grant(orch, turns=3)[0]
    assert orch._charge_budget_grant_turn(issue_id)
    _move(orch, cfg, "Human Review")
    changed, message, details = _grant(orch, turns=3)
    assert changed and details["status"] == "replayed" and "already in use" in message
    assert _fetch(orch, cfg).state == "Human Review"
    # Spent and held again: a replay must not lift the new hold.
    _move(orch, cfg, "Verify")
    _exhaust_max_turns(tmp_path, orch, cfg)
    changed, _message, _ = _grant(orch, turns=3)
    assert changed and _flag(orch, issue_id)
    assert issue_id in orch._turn_budget_exhausted
    assert _fetch(orch, cfg).state == "Blocked"


def test_G19b_SEAM_an_unstarted_replay_moves_only_a_card_still_parked(tmp_path):
    orch, cfg, issue_id = _incident(tmp_path)
    issue = _fetch(orch, cfg)
    assert orch._run_registry.grant_budget(
        grant_id="gh-t-grant-0001", issue_id=issue_id, identifier=ID,
        actor=ACTOR, reason=REASON, extra_turns=3, lifetime_cap=10,
        from_state=issue.state, target_state="Verify",
    ).status == "granted"
    _move(orch, cfg, "Done")  # owner closed it before any granted turn
    _changed, _message, details = _grant(orch)
    assert details["status"] == "replayed"
    assert _fetch(orch, cfg).state == "Done"
