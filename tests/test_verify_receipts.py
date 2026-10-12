"""Passing negative and positive runtime oracles; no live provider calls."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

from symphony.orchestrator import verify_receipts as receipts
from tests import test_stage_ownership as lane

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = json.loads((ROOT / "docs/evidence/issue-45/pr190.json").read_text())


@pytest.fixture
def case(tmp_path, monkeypatch):
    host = tmp_path / "host"
    workspace = tmp_path / "worker"
    host.mkdir()
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@local",
            "commit",
            "--allow-empty",
            "-qm",
            "candidate",
        ],
        check=True,
    )
    sha = subprocess.check_output(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True
    ).strip()
    policy = dict(
        findings=EVIDENCE["findings"],
        candidate_sha=sha,
        ancestry=[[EVIDENCE["external_reviewed_sha"], sha]],
        qa_descendants=[],
    )
    policy_file = host / "required.json"
    policy_file.write_text(json.dumps(policy))
    cfg = replace(
        lane._make_config(stage_kinds=lane._STAGE_KINDS),
        workflow_path=host / "WORKFLOW.md",
        workspace_root=tmp_path / "ws",
        raw={
            "verify_closure": dict(
                verify_state="Verify",
                verifier_kind="claude",
                artifact="closure.json",
                terminal_states=["Human Review", "Done"],
                failure_state="Blocked",
                command=[
                    sys.executable,
                    str(ROOT / "tests/fixtures/verify_condition.py"),
                    str(policy_file),
                ],
            )
        },
    )
    o = lane._orch(workspace)
    issue = lane._make_issue("Verify")
    entry = lane._seed_running_entry(
        o, issue, workspace, agent_kind="claude", authorized_state="Verify"
    )
    entry.run_id = "runtime-run-1"
    board = {"issue": issue}
    notes = []

    async def refresh(*args):
        return board["issue"]

    def update(_cfg, _issue, target):
        board["issue"] = replace(board["issue"], state=target)

    def note(_cfg, _issue, heading, body):
        notes.append((heading, body))

    monkeypatch.setattr(o, "_refresh_issue_state", refresh)
    monkeypatch.setattr(o, "_refresh_issue_full", refresh)
    monkeypatch.setattr(o, "_tracker_call_update_state", update)
    monkeypatch.setattr(o, "_tracker_call_append_note", note)
    monkeypatch.setattr(o._workflow_state, "current", lambda: cfg)
    monkeypatch.setattr(
        o, "_tracker_call_terminal_issues", lambda _cfg: [board["issue"]]
    )
    return o, cfg, entry, workspace, sha, board, notes, policy_file


def closure(sha):
    return dict(
        candidate_sha=sha,
        closures=[
            dict(
                finding_url=f["url"],
                reviewed_sha=f["reviewed_sha"],
                severity=f["native_severity"],
                candidate_sha=sha,
                disposition="closed",
                rationale="Independently checked the bounded repair.",
            )
            for f in EVIDENCE["findings"]
        ],
    )


def attest(case, artifact=None):
    o, cfg, entry, workspace, sha, board, notes, policy_file = case
    candidate = receipts.begin(cfg, entry, workspace, "Verify")
    (workspace / "closure.json").write_text(
        json.dumps(closure(sha) if artifact is None else artifact)
    )
    entry.last_completed_turn_event = 1
    receipts.complete(cfg, entry, workspace, candidate, 1)


@pytest.mark.parametrize(
    "defect",
    [
        "none",
        "document-only",
        "generic",
        "wrong-id",
        "wrong-severity",
        "stale",
        "partial",
        "decision",
        "duplicate",
        "pr190",
    ],
)
def test_material_rejection(case, defect):
    o, cfg, entry, workspace, sha, board, notes, policy_file = case
    data = closure(sha)
    if defect == "generic":
        data = dict(candidate_sha=sha, verdict="APPROVE")
    elif defect == "wrong-id":
        data["closures"][0]["finding_url"] = "wrong"
    elif defect == "wrong-severity":
        data["closures"][0]["severity"] = "P3"
    elif defect == "stale":
        policy = json.loads(policy_file.read_text())
        policy["candidate_sha"] = "incompatible"
        policy_file.write_text(json.dumps(policy))
    elif defect == "partial":
        data["closures"].pop()
    elif defect == "decision":
        data["closures"][0]["disposition"] = "out_of_scope_requires_decision"
    elif defect == "duplicate":
        data["closures"].append(data["closures"][0])
    elif defect == "pr190":
        # Actual Verify prose, actual false Document URL table and packet.
        data = dict(candidate_sha=sha, review=EVIDENCE["verify_artifact"])
        board["issue"] = replace(
            board["issue"],
            description=EVIDENCE["document_closure"]
            + "\n"
            + EVIDENCE["human_review_packet"],
        )
    if defect not in {"none", "document-only"}:
        attest(case, data)
    else:
        (workspace / "closure.json").write_text(json.dumps(data))
    board["issue"] = replace(board["issue"], state="Human Review")
    result, rejected = asyncio.run(o._enforce_verify_closure(cfg, board["issue"]))
    assert rejected and result.state == board["issue"].state == "Blocked"
    assert notes[-1][0] == "Verify Closure Rejected"
    if defect == "partial":
        assert EVIDENCE["findings"][-1]["url"] in notes[-1][1]


def test_valid_receipt_ignores_document_overwrite_and_restart(case, monkeypatch):
    o, cfg, entry, workspace, sha, board, notes, policy_file = case
    attest(case)
    (workspace / "closure.json").write_text("fabricated Document content")
    board["issue"] = replace(board["issue"], state="Human Review")
    o._running.clear()
    # A new process-equivalent orchestrator consumes only durable evidence.
    restarted = lane._orch(workspace)
    monkeypatch.setattr(
        restarted, "_tracker_call_terminal_issues", lambda _cfg: [board["issue"]]
    )
    asyncio.run(restarted._sweep_verify_closure(cfg))
    assert board["issue"].state == "Human Review"
    assert receipts.evaluate(cfg, board["issue"]) == (True, [])


@pytest.mark.parametrize("mode", ["reconcile", "exit", "exit-retry", "restart"])
def test_all_paths_fail_closed(case, monkeypatch, mode):
    o, cfg, entry, workspace, sha, board, notes, policy_file = case
    board["issue"] = replace(board["issue"], state="Human Review")
    entry.authorized_state = "Document"
    entry.agent_kind = "codex"
    entry.issue = replace(entry.issue, state="Document")
    if mode == "reconcile":
        asyncio.run(
            o._reconcile_one(
                board["issue"],
                entry,
                cfg,
                active={"verify", "document"},
                terminal={"human review", "blocked"},
                now=datetime.now(timezone.utc),
                recent_grace_s=0,
            )
        )
        assert entry.terminal_seen_at is None
    elif mode.startswith("exit"):
        if mode == "exit-retry":
            update = o._tracker_call_update_state
            monkeypatch.setattr(
                o,
                "_tracker_call_update_state",
                lambda *a: (_ for _ in ()).throw(OSError("board write failed")),
            )
        asyncio.run(o._on_worker_exit(entry.issue.id, "error", "crash after move"))
        if mode == "exit-retry":
            assert entry.issue.id in o._exit_ownership_pending
            assert board["issue"].state == "Human Review"
            monkeypatch.setattr(o, "_tracker_call_update_state", update)
            asyncio.run(o._drain_exit_ownership_pending(cfg))
            assert not o._exit_ownership_pending
    else:
        o._running.clear()
        asyncio.run(o._sweep_verify_closure(cfg))
    assert board["issue"].state == "Blocked"


def test_p3_only_needs_no_material_receipt(case):
    o, cfg, entry, workspace, sha, board, notes, policy_file = case
    policy = json.loads(policy_file.read_text())
    for f in policy["findings"]:
        f["native_severity"] = "P3"
    policy_file.write_text(json.dumps(policy))
    assert receipts.evaluate(cfg, replace(entry.issue, state="Human Review")) == (
        True,
        [],
    )


@pytest.mark.parametrize(
    "fault",
    ["document", "failed-turn", "malformed", "digest", "contract-change", "wrong-sha"],
)
def test_receipt_provenance_fail_closed(case, fault):
    o, cfg, entry, workspace, sha, board, notes, policy_file = case
    if fault == "document":
        entry.authorized_state = "Document"
        entry.agent_kind = "codex"
        assert receipts.begin(cfg, entry, workspace, "Document") is None
    elif fault == "failed-turn":
        candidate = receipts.begin(cfg, entry, workspace, "Verify")
        (workspace / "closure.json").write_text(json.dumps(closure(sha)))
        with pytest.raises(ValueError, match="completion event"):
            receipts.complete(cfg, entry, workspace, candidate, 1)
    elif fault == "wrong-sha":
        attest(case, closure("wrong"))
    else:
        attest(case)
        path = receipts.receipt_path(cfg, entry.issue)
        if fault == "malformed":
            path.write_text("{")
        elif fault == "digest":
            data = json.loads(path.read_text())
            data["artifact_content"] += " "
            path.write_text(json.dumps(data))
        elif fault == "contract-change":
            cfg.raw["verify_closure"]["artifact"] = "different.json"
    assert not receipts.evaluate(cfg, replace(entry.issue, state="Human Review"))[0]


@pytest.mark.parametrize("valid", [False, True])
def test_actual_turn_loop_attests_verify_then_gates_document(case, monkeypatch, valid):
    o, cfg, entry, workspace, sha, board, notes, policy_file = case
    entry.issue = replace(entry.issue, state="In Progress")
    entry.agent_kind = "codex"
    entry.authorized_state = "In Progress"
    board["issue"] = entry.issue
    operated = []

    class Backend(lane._RecordingBackend):
        def __init__(self, init):
            super().__init__(len(operated))
            self.kind = init.cfg.agent.kind

        async def run_turn(self, **kwargs):
            stage = board["issue"].state
            operated.append((stage, self.kind))
            if stage == "Verify":
                data = (
                    closure(sha)
                    if valid
                    else dict(candidate_sha=sha, verdict="APPROVE")
                )
                (workspace / "closure.json").write_text(json.dumps(data))
            if stage == "Document":
                # Later author writes cannot supply or overwrite private approval.
                (workspace / "closure.json").write_text(json.dumps(closure(sha)))
            board["issue"] = replace(
                board["issue"],
                state={
                    "In Progress": "Verify",
                    "Verify": "Document",
                    "Document": "Human Review",
                }[stage],
            )
            await o._on_codex_event(entry.issue.id, {"event": "turn_completed"})

    monkeypatch.setattr(lane.core_mod, "build_backend", Backend)
    asyncio.run(o._run_agent_attempt(entry.issue, attempt=None, cfg=cfg))
    assert operated == [
        ("In Progress", "codex"),
        ("Verify", "claude"),
        ("Document", "codex"),
    ]
    assert board["issue"].state == ("Human Review" if valid else "Blocked")
    assert receipts.receipt_path(cfg, entry.issue).exists()


def test_second_verify_invalidates_old_receipt_and_seeded_artifact(case):
    o, cfg, entry, workspace, sha, board, notes, policy_file = case
    attest(case)
    assert receipts.receipt_path(cfg, entry.issue).exists()
    receipts.begin(cfg, entry, workspace, "Verify")
    assert not receipts.receipt_path(cfg, entry.issue).exists()
    assert not (workspace / "closure.json").exists()


def test_pinned_author_session_cannot_attest_verify(case):
    o, cfg, entry, workspace, sha, board, notes, policy_file = case
    receipts.begin(cfg, entry, workspace, "Document")
    assert entry.verify_session_authored
    with pytest.raises(ValueError, match="independent"):
        receipts.begin(cfg, entry, workspace, "Verify")


@pytest.mark.parametrize(
    "answer",
    [
        "null",
        "{}",
        '{"passed":true,"missing":["still-open"]}',
        '{"passed":"true","missing":[]}',
    ],
)
def test_bad_predicate_output_blocks(case, answer):
    o, cfg, entry, workspace, sha, board, notes, policy_file = case
    cfg.raw["verify_closure"]["command"] = [sys.executable, "-c", f"print({answer!r})"]
    assert not receipts.evaluate(cfg, replace(entry.issue, state="Human Review"))[0]


def test_valid_bounded_repair_then_qa_descendant(case):
    o, cfg, entry, workspace, sha, board, notes, policy_file = case
    attest(case)
    policy = json.loads(policy_file.read_text())
    policy["candidate_sha"] = "qa-descendant-C"
    policy["qa_descendants"] = [[sha, "qa-descendant-C"]]
    policy_file.write_text(json.dumps(policy))
    assert receipts.evaluate(cfg, replace(entry.issue, state="Human Review")) == (
        True,
        [],
    )


def test_startup_cleanup_cannot_remove_unattested_terminal(case, monkeypatch):
    o, cfg, entry, workspace, sha, board, notes, policy_file = case
    board['issue'] = replace(board['issue'], state='Done')
    removed = []
    async def remove(path):
        removed.append(path)
    monkeypatch.setattr(o._workspace_manager, 'remove', remove, raising=False)
    asyncio.run(o._startup_terminal_cleanup(cfg))
    assert board['issue'].state == 'Blocked'
    assert removed == []
