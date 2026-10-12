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

from ..utils import git_inspect
from ..issue import Issue, normalize_state
from ..workflow import ServiceConfig
from .entries import RunningEntry


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
        or cfg.agent.kind == raw["verifier_kind"]
        and not kinds
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


def begin(
    cfg: ServiceConfig, entry: RunningEntry, workspace: Path, stage: str
) -> str | None:
    policy = contract(cfg)
    if policy is None:
        return None
    if normalize_state(stage) != normalize_state(policy["verify_state"]):
        if entry.agent_kind == policy["verifier_kind"]:
            entry.verify_session_authored = True
        return None
    # Invalidate before touching worker input: failed/crashed Verify cannot
    # inherit approval from an earlier turn. No receipt is minted on error.
    receipt_path(cfg, entry.issue).unlink(missing_ok=True)
    if (
        entry.verify_session_authored
        or entry.recovery_session_resumed
        or entry.agent_kind != policy["verifier_kind"]
        or normalize_state(entry.authorized_state)
        != normalize_state(policy["verify_state"])
    ):
        raise ValueError("worker lacks configured independent Verify authority")
    clean = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--"], cwd=workspace,
        timeout=10, check=False,
    )
    if clean.returncode != 0:
        raise ValueError("Verify candidate has uncommitted tracked changes")
    artifact_path(workspace, policy).unlink(missing_ok=True)
    sha = git_inspect.resolve_commit(workspace, "HEAD")
    if not sha or not entry.run_id:
        raise ValueError("Verify receipt requires a runtime run and Git candidate")
    return sha


def complete(
    cfg: ServiceConfig,
    entry: RunningEntry,
    workspace: Path,
    candidate: str | None,
    turn: int,
) -> None:
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
    # Missing closure is ordinary failed evidence; the terminal predicate
    # reports it. Never turn a generic approval into an empty closure.
    if not path.is_file() or path.is_symlink():
        return
    content = path.read_bytes()
    if len(content) > 1_000_000:
        raise ValueError("closure artifact exceeds 1 MB")
    closure = json.loads(content)
    if not isinstance(closure, dict) or closure.get("candidate_sha") != candidate:
        return
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
        artifact_content=content.decode("utf-8"),
    )
    target = receipt_path(cfg, entry.issue)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".receipt-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(receipt, handle)
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
        return False, [f"Verify closure predicate unavailable or malformed: {exc}"]
