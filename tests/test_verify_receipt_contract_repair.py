"""A stage-contract repair of a completed Verify keeps that Verify's receipt.

Shape of the HACP GH-140 incident (human-ai-control-plane#162): an
independent Verify turn approves candidate B, commits QA evidence (B'),
writes a closure bound to B and completes, so its receipt is minted. The
stage contract then rejects the ticket's evidence formatting and rewinds
Verify -> Verify. The repair turn starts on B', commits more QA evidence
and writes no new closure. Before this change the repair turn's `begin`
deleted the receipt and `complete` minted none, so the closure condition
saw `receipt: null` although an independent Verify really ran.

The carried receipt is the verified turn's receipt byte for byte (its own
turn, candidate and completion SHA); the trusted condition still decides
whether the tail after it is acceptable. Anything else fails closed.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from symphony.orchestrator import verify_receipts as receipts
from symphony.orchestrator.contracts import ContractResult
from tests import test_stage_ownership as lane
from tests.test_verify_receipts import case, closure  # noqa: F401  (fixture)


def _commit(workspace: Path, name: str) -> str:
    path = workspace / "docs" / "GH-T" / "qa" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name)
    git = ["git", "-C", str(workspace)]
    subprocess.run([*git, "add", "-A", "docs"], check=True)
    subprocess.run(
        [*git, "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-qm", name],
        check=True,
    )
    return subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()


def _verified_turn(case, turn: int = 3) -> bytes:
    """Verify approves B, commits QA evidence B' and completes."""
    o, cfg, entry, workspace, sha, *_ = case
    candidate = receipts.begin(cfg, entry, workspace, "Verify")
    assert candidate == sha
    (workspace / "closure.json").write_text(json.dumps(closure(sha)))
    _commit(workspace, "evidence-1.md")
    entry.last_completed_turn_event = turn
    receipts.complete(cfg, entry, workspace, candidate, turn)
    entry.verify_turn_pending = False
    return receipts.receipt_path(cfg, entry.issue).read_bytes()


def _repair_turn(
    case, turn: int = 4, *, closure_bytes=None, complete=True, commit=True
):
    o, cfg, entry, workspace, sha, *_ = case
    candidate = receipts.begin(cfg, entry, workspace, "Verify")
    if commit:
        _commit(workspace, f"evidence-{turn}.md")
    if closure_bytes is not None:
        (workspace / "closure.json").write_bytes(closure_bytes)
    if complete:
        entry.last_completed_turn_event = turn
        receipts.complete(cfg, entry, workspace, candidate, turn)
        entry.verify_turn_pending = False
    return candidate


def _receipt(case):
    o, cfg, entry, *_ = case
    path = receipts.receipt_path(cfg, entry.issue)
    return json.loads(path.read_text()) if path.exists() else None


def _rewind(case, producing_state: str = "verify") -> None:
    o, cfg, entry, *_ = case
    receipts.mark_contract_repair(cfg, entry, producing_state)


def test_contract_repair_without_new_closure_carries_the_verified_receipt(case):
    original = _verified_turn(case)
    _rewind(case)
    _repair_turn(case)
    o, cfg, entry, workspace, sha, *_ = case
    assert receipts.receipt_path(cfg, entry.issue).read_bytes() == original
    receipt = _receipt(case)
    assert receipt["candidate_sha"] == sha and receipt["turn"] == 3


def test_unchanged_closure_rewrite_still_carries(case):
    original = _verified_turn(case)
    _rewind(case)
    o, cfg, entry, workspace, sha, *_ = case
    _repair_turn(case, closure_bytes=json.loads(original)["artifact_content"].encode())
    assert receipts.receipt_path(cfg, entry.issue).read_bytes() == original


def test_fresh_closure_in_the_repair_turn_mints_a_new_receipt(case):
    _verified_turn(case)
    _rewind(case)
    o, cfg, entry, workspace, sha, *_ = case
    head = subprocess.check_output(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True
    ).strip()
    _repair_turn(case, closure_bytes=json.dumps(closure(head)).encode())
    receipt = _receipt(case)
    assert receipt["candidate_sha"] == head and receipt["turn"] == 4


@pytest.mark.parametrize(
    "content",
    [b"{", json.dumps({"candidate_sha": "0" * 40}).encode()],
    ids=["malformed", "other-candidate"],
)
def test_contradicting_closure_drops_the_carry(case, content):
    _verified_turn(case)
    _rewind(case)
    _repair_turn(case, closure_bytes=content)
    assert _receipt(case) is None


def test_ordinary_second_verify_still_invalidates(case):
    _verified_turn(case)
    _repair_turn(case)  # no contract rewind
    assert _receipt(case) is None


def test_rewind_from_another_stage_does_not_mark(case):
    _verified_turn(case)
    _rewind(case, producing_state="in progress")
    _repair_turn(case)
    assert _receipt(case) is None


def test_repair_turn_that_never_completes_leaves_no_receipt(case):
    _verified_turn(case)
    _rewind(case)
    _repair_turn(case, complete=False)  # crashed / failed turn
    assert _receipt(case) is None
    o, cfg, entry, workspace, sha, *_ = case
    # The retry is an ordinary Verify; the stale carry is gone.
    _repair_turn(case, turn=5)
    assert _receipt(case) is None


def test_repair_turn_without_completion_event_raises_and_leaves_none(case):
    _verified_turn(case)
    _rewind(case)
    o, cfg, entry, workspace, sha, *_ = case
    candidate = receipts.begin(cfg, entry, workspace, "Verify")
    with pytest.raises(ValueError, match="completion event"):
        receipts.complete(cfg, entry, workspace, candidate, 4)
    assert _receipt(case) is None


def test_mark_is_one_shot(case):
    _verified_turn(case)
    _rewind(case)
    _repair_turn(case)
    assert _receipt(case) is not None
    _repair_turn(case, turn=5)  # a later Verify is ordinary again
    assert _receipt(case) is None


def test_repeated_repair_without_new_commits_keeps_carrying(case):
    original = _verified_turn(case)
    _rewind(case)
    _repair_turn(case, commit=False)
    _rewind(case)
    _repair_turn(case, turn=5)
    o, cfg, entry, *_ = case
    assert receipts.receipt_path(cfg, entry.issue).read_bytes() == original


def test_repeated_repair_after_a_repair_commit_fails_closed(case):
    """The carry binds the verified turn's completion tree; a second repair
    starting after the first repair's commit is not that tree."""
    _verified_turn(case)
    _rewind(case)
    _repair_turn(case)
    assert _receipt(case) is not None
    _rewind(case)
    _repair_turn(case, turn=5)
    assert _receipt(case) is None


def test_workspace_change_before_the_repair_turn_refuses(case):
    _verified_turn(case)
    _rewind(case)
    o, cfg, entry, workspace, sha, *_ = case
    _commit(workspace, "between-turns.md")
    _repair_turn(case)
    assert _receipt(case) is None


@pytest.mark.parametrize(
    "fault", ["run", "issue", "artifact-digest", "contract", "missing", "garbage"]
)
def test_carry_refuses_receipts_not_bound_to_this_run(case, fault):
    _verified_turn(case)
    _rewind(case)
    o, cfg, entry, workspace, sha, *_ = case
    path = receipts.receipt_path(cfg, entry.issue)
    data = json.loads(path.read_text())
    if fault == "run":
        entry.run_id = "runtime-run-2"  # a recovered attempt is a new run
    elif fault == "issue":
        data["issue_id"] = "other"
    elif fault == "artifact-digest":
        data["artifact_content"] = json.dumps(dict(candidate_sha=sha, closures=[]))
    elif fault == "contract":
        cfg.raw["verify_closure"]["failure_state"] = "In Progress"
    if fault == "missing":
        path.unlink()
    elif fault == "garbage":
        path.write_text("not json")
    elif fault != "run":
        path.write_text(json.dumps(data))
    _repair_turn(case)
    assert _receipt(case) is None


def test_restart_between_turns_loses_the_mark(case):
    _verified_turn(case)
    _rewind(case)
    o, cfg, entry, workspace, sha, *_ = case
    fresh = replace(entry, verify_contract_repair=False, verify_receipt_carry=None)
    candidate = receipts.begin(cfg, fresh, workspace, "Verify")
    fresh.last_completed_turn_event = 4
    receipts.complete(cfg, fresh, workspace, candidate, 4)
    assert _receipt(case) is None


def test_carried_receipt_still_answers_to_the_condition(case):
    """The carry changes which receipt exists, not what the condition accepts."""
    _verified_turn(case)
    _rewind(case)
    _repair_turn(case)
    o, cfg, entry, workspace, sha, board, notes, policy_file = case
    head = subprocess.check_output(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True
    ).strip()
    review = replace(entry.issue, state="Human Review")
    policy = json.loads(policy_file.read_text())
    policy["candidate_sha"] = head  # the PR head after the repair
    policy_file.write_text(json.dumps(policy))
    assert not receipts.evaluate(cfg, review)[0]  # tail not proven QA-only
    policy["qa_descendants"] = [[sha, head]]
    policy_file.write_text(json.dumps(policy))
    assert receipts.evaluate(cfg, review) == (True, [])


def _failed_verify_contract(**kwargs) -> ContractResult:
    return ContractResult(
        passed=False,
        missing=["AC Scorecard row 1 evidence"],
        note_heading="Contract Failure",
        note_body="Verify evidence must cite a durable QA artifact.",
    )


@pytest.mark.parametrize(
    "with_fix,qa_tail",
    [(True, True), (True, False), (False, True)],
    ids=["carry", "carry-unproven-tail", "no-carry"],
)
def test_actual_turn_loop_gh140_contract_repair_keeps_receipt(
    case, monkeypatch, with_fix, qa_tail
):
    """In Progress -> Verify (approve, QA commit) -> contract rewind ->
    Verify repair (QA commit, no closure) -> Document -> Human Review."""
    o, cfg, entry, workspace, sha, board, notes, policy_file = case
    cfg = replace(cfg, agent=replace(cfg.agent, stage_contracts="on"))
    monkeypatch.setattr(o._workflow_state, "current", lambda: cfg)
    entry.issue = replace(entry.issue, state="In Progress")
    entry.agent_kind = "codex"
    entry.authorized_state = "In Progress"
    board["issue"] = entry.issue
    operated = []
    verify_turns = []
    real_contract = lane.core_mod.evaluate_contract

    def contract(**kwargs):
        if kwargs["producing_state"] == "verify" and len(verify_turns) == 1:
            return _failed_verify_contract(**kwargs)
        result = real_contract(**kwargs)
        return replace(result, passed=True, missing=[])

    monkeypatch.setattr(lane.core_mod, "evaluate_contract", contract)
    if not with_fix:
        monkeypatch.setattr(receipts, "mark_contract_repair", lambda *a: None)

    class Backend(lane._RecordingBackend):
        def __init__(self, init):
            super().__init__(len(operated))
            self.kind = init.cfg.agent.kind

        async def run_turn(self, **kwargs):
            stage = board["issue"].state
            operated.append((stage, self.kind))
            if stage == "Verify":
                verify_turns.append(stage)
                if len(verify_turns) == 1:
                    (workspace / "closure.json").write_text(json.dumps(closure(sha)))
                _commit(workspace, f"evidence-{len(verify_turns)}.md")
            if stage == "Document":
                # The trusted condition now judges the real PR head: the
                # candidate-to-head tail is authorized only when QA-only.
                head = subprocess.check_output(
                    ["git", "-C", str(workspace), "rev-parse", "HEAD"], text=True
                ).strip()
                policy = json.loads(policy_file.read_text())
                policy["candidate_sha"] = head
                policy["qa_descendants"] = [[sha, head]] if qa_tail else []
                policy_file.write_text(json.dumps(policy))
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
        ("Verify", "claude"),
        ("Document", "codex"),
    ]
    receipt = _receipt(case)
    if with_fix:
        assert receipt is not None and receipt["candidate_sha"] == sha
        assert board["issue"].state == ("Human Review" if qa_tail else "Blocked")
    else:
        # The GH-140 outcome: the independent Verify happened, its proof did not
        # survive the repair turn, and closure fails for want of a receipt.
        assert receipt is None
        assert board["issue"].state == "Blocked"
