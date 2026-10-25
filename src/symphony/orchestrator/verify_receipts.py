"""Opt-in runtime Verify provenance and a repository-owned terminal condition.

Private receipts are operational evidence, not a workflow coordinator. Board
and workspace assertions never establish provenance. Same-UID deliberate edits
to private runtime storage are outside this boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..logging import get_logger
from ..utils import git_inspect
from ..issue import Issue, normalize_state
from ..workflow import ServiceConfig
from .entries import RunningEntry

log = get_logger()


class ConditionUnavailable(RuntimeError):
    """Do not accept or clean up; retry without rewriting the board."""


def contract(cfg: ServiceConfig) -> dict[str, Any] | None:
    raw = cfg.raw.get("verify_closure")
    if "verify_closure" not in cfg.raw:
        return None
    if not isinstance(raw, dict):
        raise ValueError("verify_closure must be a mapping")
    required = {
        "verify_state",
        "verifier_kind",
        "artifact",
        "terminal_states",
        "failure_state",
        "command",
    }
    if set(raw) != required:
        raise ValueError(
            "verify_closure keys must be exactly " + ", ".join(sorted(required))
        )
    for key in ("verify_state", "verifier_kind", "artifact", "failure_state"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise ValueError(f"verify_closure.{key} must be a nonempty string")
    for key in ("terminal_states", "command"):
        if (
            not isinstance(raw[key], list)
            or not raw[key]
            or any(not isinstance(v, str) or not v.strip() for v in raw[key])
        ):
            raise ValueError(f"verify_closure.{key} must be a nonempty string list")
    active = {normalize_state(s) for s in cfg.tracker.active_states}
    terminal = {normalize_state(s) for s in cfg.tracker.terminal_states}
    if normalize_state(raw["verify_state"]) not in active:
        raise ValueError("Verify receipt stage must be active")
    if not {normalize_state(s) for s in raw["terminal_states"]} <= terminal:
        raise ValueError("closure destinations must be terminal states")
    if normalize_state(
        raw["failure_state"]
    ) not in active | terminal or normalize_state(raw["failure_state"]) in {
        normalize_state(s) for s in raw["terminal_states"]
    }:
        raise ValueError("invalid closure failure state")
    path = Path(raw["artifact"])
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("closure artifact must be workspace-relative")
    # Dedicated backend routing forces an independent session at the Verify
    # boundary. Ticket pins cannot grant Verify authority to an author backend.
    stage = normalize_state(raw["verify_state"])
    kinds = cfg.agent.stage_kinds
    if (
        kinds.get(stage) != raw["verifier_kind"]
        or any(
            kind == raw["verifier_kind"]
            for state, kind in kinds.items()
            if normalize_state(state) != stage
        )
        or (
            cfg.raw.get("agent", {}).get("kind", cfg.agent.kind) == raw["verifier_kind"]
            and any(
                normalize_state(state) != stage and normalize_state(state) not in kinds
                for state in cfg.tracker.active_states
            )
        )
    ):
        raise ValueError("Verify requires a dedicated configured stage backend")
    return raw


def fingerprint(policy: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def receipt_path(cfg: ServiceConfig, issue: Issue) -> Path:
    key = hashlib.sha256(issue.id.encode()).hexdigest()
    return cfg.workflow_path.parent / ".symphony" / "verify-receipts" / f"{key}.json"


def artifact_path(workspace: Path, policy: dict[str, Any]) -> Path:
    path = workspace / policy["artifact"]
    if not path.resolve().is_relative_to(workspace.resolve()):
        raise ValueError("closure artifact escapes workspace")
    return path


def _event(event: str, entry: RunningEntry, **fields: Any) -> None:
    """One receipt lifecycle line: identities and SHAs, never artifact bytes."""
    log.info(
        event,
        issue_id=entry.issue.id,
        identifier=entry.issue.identifier,
        run_id=entry.run_id,
        **fields,
    )


def mark_contract_repair(
    cfg: ServiceConfig, entry: RunningEntry, producing_state: str
) -> None:
    """A stage contract rewound a completed Verify turn back into Verify.

    The next Verify turn repairs board evidence for the same run; `begin`
    may carry this run's receipt across it (see `_carry_problem`)."""
    try:
        policy = contract(cfg)
    except ValueError:
        return  # `begin` refuses the misconfiguration itself
    if policy is not None and normalize_state(producing_state) == normalize_state(
        policy["verify_state"]
    ):
        entry.verify_contract_repair = True


def _carry_problem(
    policy: dict[str, Any],
    entry: RunningEntry,
    prior: tuple[bytes, dict[str, Any]] | None,
    head: str,
) -> str | None:
    """Why this run's prior receipt may not survive a contract repair turn.

    It must be a well-formed receipt minted by this same run under the
    current contract, and the repair turn must start on exactly the tree
    the verified turn ended on. The trusted condition still judges the
    candidate-to-head tail the repair adds."""
    if prior is None:
        return "no prior receipt"
    receipt = prior[1]
    try:
        checks = (
            receipt.get("schema") == 1,
            receipt["issue_id"] == entry.issue.id,
            receipt["identifier"] == entry.issue.identifier,
            receipt["run_id"] == entry.run_id and bool(entry.run_id),
            normalize_state(receipt["stage"])
            == normalize_state(policy["verify_state"]),
            receipt["verifier_kind"] == policy["verifier_kind"],
            receipt["contract_sha256"] == fingerprint(policy),
            receipt["artifact"] == policy["artifact"],
            type(receipt["turn"]) is int and receipt["turn"] > 0,
            hashlib.sha256(receipt["artifact_content"].encode()).hexdigest()
            == receipt["artifact_sha256"],
            json.loads(receipt["artifact_content"])["candidate_sha"]
            == receipt["candidate_sha"],
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return "prior receipt malformed"
    if not all(checks):
        return "prior receipt not bound to this run and contract"
    if receipt["completed_sha"] != head:
        return "workspace changed since the verified turn completed"
    return None


def _read_receipt(path: Path) -> tuple[bytes, dict[str, Any]] | None:
    try:
        raw = path.read_bytes()
        receipt = json.loads(raw)
    except (OSError, ValueError):
        return None
    return (raw, receipt) if isinstance(receipt, dict) else None


def begin(
    cfg: ServiceConfig, entry: RunningEntry, workspace: Path, stage: str
) -> str | None:
    policy = contract(cfg)
    if policy is None:
        return None
    repair, entry.verify_contract_repair = entry.verify_contract_repair, False
    entry.verify_receipt_carry = None
    if normalize_state(stage) != normalize_state(policy["verify_state"]):
        if entry.agent_kind == policy["verifier_kind"]:
            entry.verify_session_authored = True
        return None
    # Invalidate before touching worker input: failed/crashed Verify cannot
    # inherit approval from an earlier turn. No receipt is minted on error.
    # A contract repair turn holds the prior receipt in memory only, and
    # `complete` restores it only after that turn completes successfully.
    path = receipt_path(cfg, entry.issue)
    prior = _read_receipt(path) if repair else None
    if path.exists() or path.is_symlink():
        path.unlink(missing_ok=True)
        _event("verify_receipt_invalidated", entry, reason="verify turn started")
    if (
        entry.verify_session_authored
        or entry.recovery_session_resumed
        or entry.agent_kind != policy["verifier_kind"]
        or normalize_state(entry.authorized_state)
        != normalize_state(policy["verify_state"])
    ):
        raise ValueError("worker lacks configured independent Verify authority")
    clean = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--"],
        cwd=workspace,
        timeout=10,
        check=False,
    )
    if clean.returncode != 0:
        raise ValueError("Verify candidate has uncommitted tracked changes")
    artifact_path(workspace, policy).unlink(missing_ok=True)
    sha = git_inspect.resolve_commit(workspace, "HEAD")
    if not sha or not entry.run_id:
        raise ValueError("Verify receipt requires a runtime run and Git candidate")
    entry.verify_turn_pending = True
    if repair:
        problem = _carry_problem(policy, entry, prior, sha)
        if problem is None:
            entry.verify_receipt_carry = prior
        _event(
            "verify_receipt_carry_offered" if problem is None
            else "verify_receipt_carry_refused",
            entry,
            candidate_sha=sha,
            reason=problem or "contract repair of this run's verified turn",
        )
    return sha


def complete(
    cfg: ServiceConfig,
    entry: RunningEntry,
    workspace: Path,
    candidate: str | None,
    turn: int,
) -> None:
    carry, entry.verify_receipt_carry = entry.verify_receipt_carry, None
    if candidate is None:
        return
    policy = contract(cfg)
    if policy is None or entry.last_completed_turn_event != turn:
        raise ValueError(
            "Verify receipt requires an observed successful completion event"
        )
    if entry.agent_kind != policy["verifier_kind"] or normalize_state(
        entry.authorized_state
    ) != normalize_state(policy["verify_state"]):
        raise ValueError("Verify provenance changed during turn")
    path = artifact_path(workspace, policy)
    target = receipt_path(cfg, entry.issue)
    # Missing closure is ordinary failed evidence; the terminal predicate
    # reports it. Never turn a generic approval into an empty closure.
    content = (
        path.read_bytes() if path.is_file() and not path.is_symlink() else None
    )
    if carry is not None and carry[1]["turn"] < turn and (
        content is None or content == carry[1]["artifact_content"].encode()
    ):
        # The repair turn left no new closure (or rewrote the verified one
        # unchanged): the verified turn's receipt stands, byte for byte.
        _write(target, carry[0])
        _event(
            "verify_receipt_carried",
            entry,
            turn=turn,
            receipt_turn=carry[1]["turn"],
            candidate_sha=carry[1]["candidate_sha"],
        )
        return
    problem = _closure_problem(content, candidate)
    if problem is not None:
        _event(
            "verify_receipt_not_minted",
            entry,
            turn=turn,
            candidate_sha=candidate,
            reason=problem,
            carry_dropped=carry is not None,
        )
        return
    assert content is not None
    content_text = content.decode("utf-8")
    receipt = dict(
        schema=1,
        issue_id=entry.issue.id,
        identifier=entry.issue.identifier,
        run_id=entry.run_id,
        turn=turn,
        stage=entry.authorized_state,
        verifier_kind=entry.agent_kind,
        candidate_sha=candidate,
        completed_sha=git_inspect.resolve_commit(workspace, "HEAD"),
        contract_sha256=fingerprint(policy),
        artifact=policy["artifact"],
        artifact_sha256=hashlib.sha256(content).hexdigest(),
        artifact_content=content_text,
    )
    _write(target, json.dumps(receipt).encode())
    _event("verify_receipt_minted", entry, turn=turn, candidate_sha=candidate)


def _closure_problem(content: bytes | None, candidate: str) -> str | None:
    if content is None:
        return "closure artifact missing"
    if len(content) > 1_000_000:
        return "closure artifact too large"
    try:
        closure = json.loads(content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "closure artifact is not JSON"
    if not isinstance(closure, dict) or closure.get("candidate_sha") != candidate:
        return "closure artifact does not bind the turn's candidate"
    return None


def _write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".receipt-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def evaluate(cfg: ServiceConfig, issue: Issue) -> tuple[bool, list[str]]:
    policy = contract(cfg)
    if policy is None or normalize_state(issue.state) not in {
        normalize_state(s) for s in policy["terminal_states"]
    }:
        return True, []
    receipt = None
    path = receipt_path(cfg, issue)
    if path.exists():
        try:
            receipt = json.loads(path.read_text())
            if not isinstance(receipt, dict) or receipt.get("schema") != 1:
                raise ValueError("receipt schema")
            checks = (
                receipt["issue_id"] == issue.id,
                receipt["identifier"] == issue.identifier,
                receipt["contract_sha256"] == fingerprint(policy),
                normalize_state(receipt["stage"])
                == normalize_state(policy["verify_state"]),
                receipt["verifier_kind"] == policy["verifier_kind"],
                bool(receipt["run_id"]),
                type(receipt["turn"]) is int and receipt["turn"] > 0,
                receipt["artifact"] == policy["artifact"],
                hashlib.sha256(receipt["artifact_content"].encode()).hexdigest()
                == receipt["artifact_sha256"],
                json.loads(receipt["artifact_content"])["candidate_sha"]
                == receipt["candidate_sha"],
            )
            if not all(checks):
                raise ValueError("receipt binding")
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return False, [f"invalid private Verify receipt: {exc}"]
    request = dict(
        schema=1,
        issue_id=issue.id,
        identifier=issue.identifier,
        terminal_state=issue.state,
        receipt=receipt,
    )
    try:
        result = subprocess.run(
            policy["command"],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            cwd=cfg.workflow_path.parent,
            timeout=30,
            check=False,
        )
        answer = json.loads(result.stdout)
        if (
            result.returncode != 0
            or not isinstance(answer, dict)
            or set(answer) != {"passed", "missing"}
        ):
            raise ValueError("predicate exit/output")
        missing = answer["missing"]
        if (
            type(answer["passed"]) is not bool
            or not isinstance(missing, list)
            or any(not isinstance(s, str) or not s for s in missing)
        ):
            raise ValueError("predicate result schema")
        if answer["passed"] and missing:
            raise ValueError("passing predicate has missing findings")
        return answer["passed"], missing or (
            [] if answer["passed"] else ["repository condition rejected closure"]
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise ConditionUnavailable(
            f"Verify closure predicate unavailable or malformed: {exc}"
        ) from exc
