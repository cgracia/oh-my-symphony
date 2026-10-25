"""Plumbing e2e for the operator budget grant (human-ai-control-plane #9, B).

Real orchestrator ticks, real Markdown board, real SQLite registry; only the
agent backends are fakes that advance the card one lane per turn and record
which agent kind each lane was built for. Proves, after a grant on a
budget-exhausted card parked in Blocked:

- normal dispatch resumes in Verify under the configured verifier family
  (claude), and Document returns to the implementer family (codex);
- every executed turn is charged to the registry ledger, and the lane parks
  again (Blocked, flag persisted) the moment the granted turns run out.

This is not a live model run and produces no real Verify receipt.
"""

from __future__ import annotations

import asyncio
from dataclasses import fields, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import symphony.orchestrator.core as core_mod
from symphony.orchestrator.run_registry import RunRegistry, registry_path_for_workflow
from symphony.trackers.file import FileBoardTracker

from tests.test_agent_lifecycle_e2e import (
    _CONTRACT_CLEAN_BODY,
    _FileBoardLifecycleBackend,
    _make_file_board_lifecycle_config,
    _orch,
)

ID = "LIFE-1"


class _HacpLaneBackend(_FileBoardLifecycleBackend):
    """The HACP lane ends at Human Review; the owner decides the merge."""

    transitions = {
        "In Progress": "Verify",
        "Verify": "Document",
        "Document": "Human Review",
    }
STAGE_KINDS = {"in progress": "codex", "verify": "claude", "document": "codex"}


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    board_root = tmp_path / "kanban"
    workspace_path = tmp_path / "workspace" / ID
    cfg = _make_file_board_lifecycle_config(
        tmp_path, board_root=board_root, workspace_path=workspace_path
    )
    cfg = replace(
        cfg,
        agent=replace(
            cfg.agent,
            stage_kinds=dict(STAGE_KINDS),
            max_turns=10,
            max_total_turns=30,
            budget_exhausted_state="Blocked",
            # As in the HACP lane: no auto-recovery, commit or merge.
            auto_recover_blocked=False,
            auto_commit_on_done=False,
            auto_merge_on_done=False,
        ),
    )
    if "budget_grant_max_turns" in {f.name for f in fields(cfg.agent)}:
        cfg = replace(cfg, agent=replace(cfg.agent, budget_grant_max_turns=10))
    tracker = FileBoardTracker(cfg.tracker)
    tracker.create(
        identifier=ID,
        title="budget grant lifecycle fixture",
        state="Blocked",
        description=_CONTRACT_CLEAN_BODY
        + "\n## Budget Exceeded\n\nmax_turns budget exceeded (max_turns=10/attempt)"
        " while state stayed Verify.\n",
    )
    issue = tracker.fetch_issue_full_by_id(ID)
    assert issue is not None
    tracker.close()
    # The state a restarted daemon finds: the exhaustion flag persisted.
    registry = RunRegistry(
        registry_path_for_workflow(cfg.workflow_path), lease_ttl=timedelta(minutes=5)
    )
    registry.set_issue_flags(issue.id, budget_exhausted=True)
    registry.close()

    o = _orch(workspace_path)
    monkeypatch.setattr(o._workflow_state, "reload", lambda: (cfg, None))
    monkeypatch.setattr(o._workflow_state, "current", lambda: cfg)
    built: list[tuple[str, _FileBoardLifecycleBackend]] = []

    def _factory(init: Any) -> _FileBoardLifecycleBackend:
        backend = _HacpLaneBackend(
            cfg=init.cfg, cwd=init.cwd, init_id=len(built)
        )
        built.append((init.cfg.agent.kind, backend))
        return backend

    monkeypatch.setattr(core_mod, "build_backend", _factory)
    return o, cfg, issue.id, built


def _state(cfg) -> str:
    tracker = FileBoardTracker(cfg.tracker)
    try:
        issue = tracker.fetch_issue_full_by_id(ID)
        assert issue is not None
        return issue.state
    finally:
        tracker.close()


async def _drain(o) -> None:
    for _ in range(8):
        await o._on_tick()
        tasks = [e.worker_task for e in o._running.values() if e.worker_task]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        await asyncio.sleep(0)
        if not o._running and not o._retry:
            await o._on_tick()
            if not o._running:
                return


def _ran(built) -> list[tuple[str, str]]:
    return [(kind, s) for kind, b in built for s in b.operated_states]


def test_E1_SEAM_grant_resumes_verify_on_the_verifier_family(tmp_path, monkeypatch):
    o, cfg, issue_id, built = _setup(tmp_path, monkeypatch)

    async def _run() -> None:
        await o._on_tick()  # boot: registry open, flag rehydrated
        assert issue_id in o._turn_budget_exhausted and not o._running
        changed, message, details = await o.grant_budget_for_issue(
            ID, grant_id="e2e-grant-0001", actor="operator:e2e",
            reason="run independent Verify", extra_turns=3, target_state="Verify",
        )
        assert changed, message
        await _drain(o)

    asyncio.run(_run())
    assert _ran(built) == [("claude", "Verify"), ("codex", "Document")]
    # Document moved the card to Human Review; the harness body carries merge
    # evidence, so the pre-existing legacy normalization may then file it as
    # Done. Either way it left the active lanes normally, not via a budget hold.
    assert _state(cfg) in {"Human Review", "Done"}
    assert o._run_registry.budget_grant_totals(issue_id) == (3, 2)
    assert issue_id not in o._turn_budget_exhausted


def test_E2_SEAM_lane_parks_again_when_granted_turns_run_out(tmp_path, monkeypatch):
    o, cfg, issue_id, built = _setup(tmp_path, monkeypatch)

    async def _run() -> None:
        await o._on_tick()
        changed, message, _ = await o.grant_budget_for_issue(
            ID, grant_id="e2e-grant-0002", actor="operator:e2e",
            reason="one Verify turn only", extra_turns=1, target_state="Verify",
        )
        assert changed, message
        await _drain(o)

    asyncio.run(_run())
    assert _ran(built) == [("claude", "Verify")]  # Document never ran a turn
    assert _state(cfg) == "Blocked"
    assert o._run_registry.budget_grant_totals(issue_id) == (1, 1)
    flags = o._run_registry.get_issue_flags(issue_id)
    assert flags is not None and flags.budget_exhausted
    assert issue_id in o._turn_budget_exhausted
