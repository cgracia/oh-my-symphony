"""Quota parks are durable claims, never execution-slot reservations.

Real file-board and SQLite pause lifecycle, scripted worker exits, no providers.
The concurrency ceiling is two throughout; resume uses the normal retry path.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import pytest

from symphony.orchestrator.core import (
    _EligibilityDisposition,
    _worker_error_pause_reason,
)
from symphony.trackers.file import FileBoardTracker
from symphony.workspace import WorkspaceManager
from tests.test_orchestrator_dispatch import _install_running_entry, _make_config, _orch


QUOTA = "Individual quota reached; Resets in 3h4m58s"


@pytest.fixture
def lane(tmp_path, monkeypatch):
    cfg = _make_config(
        max_concurrent=2,
        tracker_kind="file",
        workflow_path=tmp_path / "WORKFLOW.md",
        workspace_root=tmp_path / "ws",
        active_states=("In Progress", "Verify", "Document"),
        terminal_states=("Human Review", "Done", "Blocked", "Cancelled"),
    )
    cfg = replace(
        cfg,
        tracker=replace(cfg.tracker, board_root=tmp_path / "kanban"),
        agent=replace(
            cfg.agent,
            stage_kinds={"in progress": "agy", "verify": "claude", "document": "agy"},
            fallback_kinds=(),
            auto_merge_on_done=False,
        ),
    )
    tracker = FileBoardTracker(cfg.tracker)
    for name in ("Q-1", "Q-2", "Q-3"):
        tracker.create(identifier=name, title=name, state="In Progress")
    issues = {i.identifier: i for i in tracker.fetch_candidate_issues()}
    orch = _orch()
    orch._workspace_manager = WorkspaceManager(cfg.workspace_root, cfg.hooks)
    monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
    monkeypatch.setattr(orch._workflow_state, "reload", lambda: (cfg, None))
    orch._ensure_run_registry(cfg)
    yield orch, cfg, tracker, issues
    for retry in orch._retry.values():
        retry.timer_handle.cancel()
    if orch._run_registry is not None:
        orch._run_registry.close()


def _running(orch, issue, cfg):
    entry = _install_running_entry(orch, issue)
    entry.workspace_path = cfg.workspace_root / issue.identifier
    entry.workspace_path.mkdir(parents=True, exist_ok=True)
    (entry.workspace_path / "preserved.txt").write_text("uncommitted work")
    entry.agent_kind = cfg.agent.kind_for_state(issue.state, issue.agent_kind)
    orch._claimed.add(issue.id)
    return entry


async def _park(orch, cfg, issue, error=QUOTA):
    orch._loop = asyncio.get_running_loop()
    _running(orch, issue, cfg)
    await orch._on_worker_exit(issue.id, reason="turn_error", error=error)
    orch._retry[issue.id].timer_handle.cancel()


async def _finish_resume(orch):
    tasks = [
        t
        for t in orch._background_tasks
        if t.get_name().startswith("symphony-retry-now-")
    ]
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


async def _idle_worker(issue, attempt, cfg):
    await asyncio.Event().wait()


def test_quota_exit_frees_slot_for_independent_issue(lane):
    orch, cfg, tracker, issues = lane

    async def run():
        _running(orch, issues["Q-2"], cfg)
        await _park(orch, cfg, issues["Q-1"])
        parked = issues["Q-1"]
        flags = orch._run_registry.get_issue_flags(parked.id)
        assert flags.paused and QUOTA in flags.pause_reason
        assert parked.id not in orch._running
        assert parked.id in orch._in_flight_ids()  # duplicate prevention survives
        assert orch._retry[parked.id].holds_slot is False
        assert orch._available_slots(cfg) == 1
        assert orch._should_dispatch(issues["Q-3"], cfg)
        _running(orch, issues["Q-3"], cfg)
        assert orch._available_slots(cfg) == 0
        assert tracker.fetch_issue_full_by_id(parked.id).state == parked.state
        assert (
            cfg.workspace_root / "Q-1" / "preserved.txt"
        ).read_text() == "uncommitted work"

    asyncio.run(run())


def test_two_quota_parks_and_repeated_reconcile_remain_non_slot(lane):
    orch, cfg, _tracker, issues = lane

    async def run():
        _tracker.update_state(issues["Q-2"], "Verify")
        issues["Q-2"] = _tracker.fetch_issue_full_by_id(issues["Q-2"].id)
        await _park(orch, cfg, issues["Q-1"])
        await _park(orch, cfg, issues["Q-2"], "You've hit your limit · resets 3am")
        for _ in range(4):
            await orch._reconcile_running(cfg)
            for issue in (issues["Q-1"], issues["Q-2"]):
                orch._retry[issue.id].timer_handle.cancel()
                await orch._on_retry_timer(issue.id)
                retry = orch._retry[issue.id]
                assert (retry.holds_slot, retry.attempt, retry.kind) == (
                    False,
                    1,
                    "retry",
                )
                decision = orch._eligibility_decision(issue, cfg, owning_retry=True)
                assert decision.disposition is _EligibilityDisposition.WAIT_NON_SLOT
                assert decision.code == "paused"
                assert orch.is_paused(issue.id)
            assert orch._available_slots(cfg) == 2
            assert not orch._running

    asyncio.run(run())


def test_restart_rehydrates_quota_park_without_slot_or_auto_resume(lane, monkeypatch):
    orch, cfg, _tracker, issues = lane
    issue = issues["Q-1"]

    async def run():
        await _park(orch, cfg, issue)
        retry_attempt = orch._retry[issue.id].attempt
        orch._run_registry.close()
        orch._run_registry = None
        restarted = _orch()
        monkeypatch.setattr(restarted._workflow_state, "current", lambda: cfg)
        restarted._loop = asyncio.get_running_loop()
        restarted._workspace_manager = WorkspaceManager(cfg.workspace_root, cfg.hooks)
        monkeypatch.setattr(restarted, "_run_agent_attempt", _idle_worker)
        restarted._ensure_run_registry(cfg)
        try:
            assert restarted.is_paused(issue.id)
            assert restarted._persisted_retry_attempts[issue.id] == retry_attempt
            assert not restarted._should_dispatch(issue, cfg)
            assert (
                restarted._eligibility_decision(
                    issue, cfg, owning_retry=True
                ).disposition
                is _EligibilityDisposition.WAIT_NON_SLOT
            )
            # Exercise recreation of a retry after startup as well as the idle park.
            restarted._schedule_retry(
                issue.id,
                identifier=issue.identifier,
                attempt=retry_attempt,
                delay_ms=60_000,
                error=QUOTA,
            )
            assert restarted._retry[issue.id].holds_slot is False
            assert restarted._available_slots(cfg) == 2
            assert restarted.resume_worker(issue.id)
            await _finish_resume(restarted)
            # Real dispatch establishes admission synchronously; don't run a provider.
            assert issue.id in restarted._running
            task = restarted._running[issue.id].worker_task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            for retry in restarted._retry.values():
                retry.timer_handle.cancel()
            restarted._run_registry.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("stage", "backend"), [("In Progress", "agy"), ("Verify", "claude")]
)
def test_resume_with_full_capacity_waits_then_uses_normal_dispatch(
    lane, monkeypatch, stage, backend
):
    orch, cfg, _tracker, issues = lane
    _tracker.update_state(issues["Q-1"], stage)
    issue = _tracker.fetch_issue_full_by_id(issues["Q-1"].id)
    dispatches = []

    # Keep real _dispatch (workspace selection, backend resolution, ownership,
    # admission and lease); replace only the worker body that would call a CLI.
    monkeypatch.setattr(orch, "_run_agent_attempt", _idle_worker)

    async def run():
        await _park(orch, cfg, issue)
        for name in ("Q-2", "Q-3"):
            _running(orch, issues[name], cfg)
        assert orch.resume_worker(issue.id)
        assert not orch.resume_worker(issue.id)
        await _finish_resume(orch)
        # No turn can start while both independent workers are active.
        assert issue.id not in orch._running
        for _ in range(3):
            orch._retry[issue.id].timer_handle.cancel()
            await orch._on_retry_timer(issue.id)
            assert orch._retry[issue.id].holds_slot is False
            assert len(orch._running) == 2
            assert orch._retry[issue.id].attempt == 1
        orch._running.pop(issues["Q-3"].id)
        orch._retry[issue.id].timer_handle.cancel()
        await orch._on_retry_timer(issue.id)
        resumed = orch._running[issue.id]
        dispatches.append(resumed)
        assert len(orch._running) == 2
        assert orch._available_slots(cfg) == 0
        assert resumed.workspace_path == cfg.workspace_root / issue.identifier
        assert resumed.issue.state == stage
        assert resumed.agent_kind == backend
        assert resumed.retry_attempt == 1
        assert (
            resumed.workspace_path / "preserved.txt"
        ).read_text() == "uncommitted work"
        flags = orch._run_registry.get_issue_flags(issue.id)
        assert not flags.paused and flags.pause_reason is None
        resumed.worker_task.cancel()
        await asyncio.gather(resumed.worker_task, return_exceptions=True)

    asyncio.run(run())
    assert len(dispatches) == 1


@pytest.mark.parametrize(
    "reason",
    [
        "operator pause",
        "turn_timeout",
        "cancelled",
        "product ambiguity",
        "budget exhaustion",
        "quota",  # quota wording alone is NOT an auto-pause
        _worker_error_pause_reason("turn_error", "generic backend crash"),
        _worker_error_pause_reason("turn_error", "429 rate limit exceeded"),
    ],
)
def test_non_quota_pause_and_stale_retry_quota_keep_existing_slot_semantics(
    lane, reason
):
    orch, cfg, _tracker, issues = lane
    issue = issues["Q-1"]

    async def run():
        orch._loop = asyncio.get_running_loop()
        _running(orch, issue, cfg)
        assert orch.pause_worker(issue.id, reason)
        # An active operator-paused turn retains its slot until it exits.
        assert orch._available_slots(cfg) == 1
        orch._running.pop(issue.id)
        orch._schedule_retry(
            issue.id,
            identifier=issue.identifier,
            attempt=1,
            delay_ms=60_000,
            error=QUOTA,  # stale quota must not decide slot class
        )
        orch._retry[issue.id].timer_handle.cancel()
        await orch._on_retry_timer(issue.id)
        assert orch._retry[issue.id].holds_slot is True
        assert orch._available_slots(cfg) == 1
        assert (
            orch._eligibility_decision(issue, cfg, owning_retry=True).disposition
            is _EligibilityDisposition.WAIT_SLOT
        )

    asyncio.run(run())


def test_stale_quota_pause_text_without_pause_flag_keeps_backoff_slot(lane):
    orch, cfg, _tracker, issues = lane
    issue = issues["Q-1"]

    async def run():
        orch._loop = asyncio.get_running_loop()
        orch._pause_reasons[issue.id] = _worker_error_pause_reason("turn_error", QUOTA)
        orch._schedule_retry(
            issue.id,
            identifier=issue.identifier,
            attempt=1,
            delay_ms=60_000,
            error=QUOTA,
        )
        assert orch._retry[issue.id].holds_slot is True
        assert orch._available_slots(cfg) == 1

    asyncio.run(run())


@pytest.mark.parametrize("state", ["Human Review", "Done", "Cancelled"])
def test_terminal_states_stay_outside_execution(lane, state):
    orch, cfg, tracker, issues = lane
    issue = issues["Q-1"]
    tracker.update_state(issue, state)
    terminal = tracker.fetch_issue_full_by_id(issue.id)
    assert not orch._should_dispatch(terminal, cfg)
    assert orch._available_slots(cfg) == 2


def test_pause_resume_toggling_cannot_create_extra_worker(lane, monkeypatch):
    orch, cfg, _tracker, issues = lane
    issue = issues["Q-1"]

    monkeypatch.setattr(orch, "_run_agent_attempt", _idle_worker)

    async def run():
        await _park(orch, cfg, issue)
        for name in ("Q-2", "Q-3"):
            _running(orch, issues[name], cfg)
        for _ in range(5):
            assert orch.resume_worker(issue.id)
            await _finish_resume(orch)
            assert len(orch._running) == 2 and issue.id not in orch._running
            # Only a real exited worker can quota-park; an idle caller cannot
            # request another pause or create another worker via the public API.
            assert not orch.pause_worker(issue.id, QUOTA)
            assert not orch.resume_worker(issue.id)
            # Simulate another persisted quota park through the supported flags
            # API, without raw registry edits, to stress duplicate resume callbacks.
            orch._paused_issue_ids.add(issue.id)
            reason = _worker_error_pause_reason("turn_error", QUOTA)
            orch._pause_reasons[issue.id] = reason
            orch._set_issue_flags(issue.id, paused=True, pause_reason=reason)
        assert len(orch._retry) == 1
        assert orch._available_slots(cfg) == 0

    asyncio.run(run())
