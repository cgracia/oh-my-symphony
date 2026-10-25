"""The issue budget panel reads the run registry on its owning thread.

`RunRegistry` opens its SQLite connection without `check_same_thread=False`,
so every read must run on the thread that opened it (the event loop's). The
reopen counter in `issue_budget_snapshot` used to read through
`asyncio.to_thread`: SQLite refused that every time, the counter read
unknown and `health()` degraded with `run_registry_error` until the next
good registry call.

Real file board tracker and real SQLite run registry; no fakes on the read.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from symphony.orchestrator import Orchestrator
from symphony.orchestrator.run_registry import RunRegistry
from symphony.trackers.file import FileBoardTracker

from tests.test_orchestrator_dispatch import _make_config, _orch

ID = "GH-T"


def _cfg(tmp_path: Path):
    base = _make_config(
        tracker_kind="file",
        active_states=("Todo", "In Progress", "Verify"),
        terminal_states=("Human Review", "Done", "Blocked"),
        workflow_path=tmp_path / "WORKFLOW.md",
        workspace_root=tmp_path / "ws",
    )
    board_root = tmp_path / "kanban"
    board_root.mkdir()
    return replace(base, tracker=replace(base.tracker, board_root=board_root))


def _boot(tmp_path: Path, cfg) -> Orchestrator:
    orch = _orch()
    orch._workflow_state.current = lambda: cfg  # type: ignore[method-assign]
    registry = RunRegistry(
        tmp_path / ".symphony" / "state.db", lease_ttl=timedelta(minutes=5)
    )
    orch._run_registry = registry
    orch._finish_run_registry_open(registry, cfg, registry._path)
    return orch


def test_budget_snapshot_reads_registry_on_its_owning_thread(tmp_path):
    cfg = _cfg(tmp_path)
    FileBoardTracker(cfg.tracker).create(
        identifier=ID, title="t", state="Blocked", description="b"
    )
    orch = _boot(tmp_path, cfg)
    issue = orch._tracker_call_fetch_issue_full_by_id(cfg, ID)
    assert issue is not None
    registry = orch._run_registry
    registry.recent_runs(issue.id, limit=1)  # opens the connection here
    owner = threading.get_ident()
    seen: list[int] = []
    real = registry.recent_runs

    def _spy(*args, **kwargs):
        seen.append(threading.get_ident())
        return real(*args, **kwargs)

    registry.recent_runs = _spy  # type: ignore[method-assign]

    snapshot = asyncio.run(orch.issue_budget_snapshot(issue))

    reopens = next(i for i in snapshot["items"] if i["name"] == "reopens")
    assert seen == [owner], "registry read must stay on the owning thread"
    assert orch._last_registry_error is None, orch._last_registry_error
    assert "run_registry_error" not in orch.health().get("degraded_reasons", [])
    assert reopens["used"] == 0
