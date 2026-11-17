"""Per-ticket stage routes (`agent_stage_kinds` frontmatter, HACP #123/#160).

A board routed by `agent.stage_kinds` can let one ticket use another
backend for a named stage (e.g. a confined implementer for In Progress)
without changing the lane map for every other ticket, and without the
`agent_kind` pin's side effects (every stage, ownership enforcement off).

Semantics under test:

1. Precedence: `agent_kind` pin > ticket stage route > lane `stage_kinds`
   > workflow `kind`, and the route applies to its named stage only.
2. The route may not touch the Verify stage, may not name the verifier
   backend, may not combine with a pin, needs a stage-routed board, an
   active state and a supported kind. A malformed route is an error.
3. Eligibility rejects a refused route; dispatch config refuses it too.
4. Stage ownership uses the ticket's route for the lanes it names.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from symphony.errors import ConfigValidationError
from symphony.orchestrator import Orchestrator
from symphony.orchestrator import core as core_mod
from symphony.orchestrator.helpers import (
    _config_for_issue_agent,
    _stage_ownership_rewind_state,
    ticket_stage_kinds_problem,
)
from symphony.trackers.file import issue_from_file
from symphony.workflow import WorkflowState

from tests.test_stage_ownership import _STAGE_KINDS, _make_config, _make_issue

_CLOSURE = {"verify_closure": {"verify_state": "Verify", "verifier_kind": "claude"}}


def _cfg(**kw):
    return replace(_make_config(stage_kinds=_STAGE_KINDS, **kw), raw=dict(_CLOSURE))


def _routed(state: str = "In Progress", route=(("in progress", "opencode"),), **kw):
    return replace(_make_issue(state, **kw), stage_kinds=tuple(route))


def test_precedence_and_scope_of_a_ticket_route() -> None:
    agent = _cfg().agent
    assert agent.kind_for_state("In Progress", None, "opencode") == "opencode"
    assert agent.kind_for_state("In Progress", "pi", "opencode") == "pi"
    assert agent.kind_for_state("Verify", None, None) == "claude"
    issue = _routed()
    assert issue.stage_kind_for("In Progress") == "opencode"
    assert issue.stage_kind_for("Verify") is None
    assert issue.stage_kind_for("Document") is None


def test_dispatch_config_routes_only_the_named_stage() -> None:
    cfg = _cfg()
    assert _config_for_issue_agent(cfg, _routed("In Progress")).agent.kind == "opencode"
    assert _config_for_issue_agent(cfg, _routed("Verify")).agent.kind == "claude"
    assert _config_for_issue_agent(cfg, _routed("Document")).agent.kind == "codex"
    assert _config_for_issue_agent(cfg, _make_issue("In Progress")).agent.kind == "codex"


@pytest.mark.parametrize(
    "route, why",
    [
        ((("verify", "codex"),), "may not route the Verify stage"),
        ((("in progress", "claude"),), "verifier backend"),
        ((("document", "claude"),), "verifier backend"),
        ((("in progress", "nope"),), "unsupported kind"),
        ((("human review", "opencode"),), "not an active state"),
    ],
)
def test_refused_routes(route, why) -> None:
    problem = ticket_stage_kinds_problem(_cfg(), _routed(route=route))
    assert problem is not None and why in problem
    with pytest.raises(ConfigValidationError):
        _config_for_issue_agent(_cfg(), _routed(route=route))


def test_route_needs_a_stage_routed_board_and_no_pin() -> None:
    plain = replace(_make_config(stage_kinds=None), raw=dict(_CLOSURE))
    assert "stage_kinds" in ticket_stage_kinds_problem(plain, _routed())
    assert "pin" in ticket_stage_kinds_problem(_cfg(), _routed(agent_kind="codex"))
    assert ticket_stage_kinds_problem(_cfg(), _routed()) is None
    assert ticket_stage_kinds_problem(_cfg(), _make_issue()) is None


def test_eligibility_rejects_a_refused_route() -> None:
    orch = Orchestrator(WorkflowState(Path("/tmp/no.md")))
    cfg = _cfg()
    bad = _routed(route=(("verify", "codex"),))
    decision = orch._eligibility_decision(bad, cfg, owning_retry=False)
    assert decision.disposition is core_mod._EligibilityDisposition.REJECT
    assert decision.code == "invalid_stage_route"
    good = orch._eligibility_decision(_routed(), cfg, owning_retry=False)
    assert good.disposition is core_mod._EligibilityDisposition.READY


def test_ownership_uses_the_ticket_route() -> None:
    cfg = _cfg()
    # The routed implementer may make its single forward step...
    assert _stage_ownership_rewind_state(
        cfg, _routed(), from_state="In Progress", to_state="Verify", worker_kind="opencode"
    ) is None
    # ...but not consume Verify on the way to Document.
    assert _stage_ownership_rewind_state(
        cfg, _routed(), from_state="In Progress", to_state="Document", worker_kind="opencode"
    ) == "Verify"
    # A route on Document makes the routed kind its owner.
    doc = _routed(route=(("document", "opencode"),))
    assert _stage_ownership_rewind_state(
        cfg, doc, from_state="Verify", to_state="Human Review", worker_kind="opencode"
    ) is None


def _ticket(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / "GH-1.md"
    path.write_text(
        "---\nid: GH-1\nidentifier: GH-1\ntitle: t\nstate: In Progress\n" + extra + "---\n\nbody\n",
        encoding="utf-8",
    )
    return path


def test_file_tracker_parses_and_flags_routes(tmp_path: Path) -> None:
    issue = issue_from_file(_ticket(tmp_path, 'agent_stage_kinds: {"In Progress": OpenCode}\n'))
    assert issue is not None
    assert issue.stage_kinds == (("in progress", "opencode"),)
    assert issue.stage_kinds_error is None
    absent = issue_from_file(_ticket(tmp_path, ""))
    assert absent is not None and absent.stage_kinds == () and absent.stage_kinds_error is None
    for bad in ("agent_stage_kinds: opencode\n", "agent_stage_kinds: {}\n",
                'agent_stage_kinds: {"In Progress": 3}\n'):
        flagged = issue_from_file(_ticket(tmp_path, bad))
        assert flagged is not None and flagged.stage_kinds_error, bad
        assert ticket_stage_kinds_problem(_cfg(), flagged) == flagged.stage_kinds_error


def test_board_card_exposes_the_parsed_route(tmp_path: Path) -> None:
    from symphony.webapi import _issue_card

    routed = issue_from_file(_ticket(tmp_path, 'agent_stage_kinds: {"In Progress": opencode}\n'))
    card = _issue_card(routed)
    assert (card["agent_stage_kinds"], card["agent_stage_kinds_error"]) == ({"in progress": "opencode"}, "")
    plain = _issue_card(issue_from_file(_ticket(tmp_path, "")))
    assert (plain["agent_stage_kinds"], plain["agent_stage_kinds_error"]) == ({}, "")
    bad = _issue_card(issue_from_file(_ticket(tmp_path, "agent_stage_kinds: opencode\n")))
    assert bad["agent_stage_kinds"] == {} and bad["agent_stage_kinds_error"]
