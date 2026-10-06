"""v2 issue-23 regressions: slot release requires positive classification.

Slot release now requires a provider-account-capacity exit positively
classified at pause-creation time and stamped into the persisted pause
envelope.  Host-resource failures and ambiguous quota/limit wording must
keep the ordinary pause-and-hold-slot semantics, and worker-error text
alone — however adversarial — can never release a slot without the stamp.
"""

from __future__ import annotations

import asyncio

import pytest

from symphony.orchestrator.core import (
    _EligibilityDisposition,
    _is_provider_capacity_auto_pause_reason,
    _is_provider_capacity_worker_error,
    _worker_error_pause_reason,
)
from tests.test_orchestrator_dispatch import _orch
from tests.test_quota_park_slots import QUOTA, _park, _running

pytest_plugins = ("tests.test_quota_park_slots",)


# Every positive below is tied to provider evidence observed or shipped by
# this project (see the packet's provider-vocabulary-evidence record):
# agy live-observed reset window, claude runtime classifier commit 3ea26e3,
# OpenAI API error code token, codex E2E regression wording.
PROVIDER_CAPACITY_ERRORS = [
    QUOTA,
    "You've hit your limit · resets 3am",
    "insufficient_quota; check your billing details",
    "You have hit your usage limit for this plan",
]

# Generic capacity wording is deliberately not positive: host/cgroup/CI/
# hosting tools emit the same substrings, and a false positive wrongly
# releases a concurrency slot while a false negative merely holds one.
HOST_AND_AMBIGUOUS_ERRORS = [
    "OSError: [Errno 122] Disk quota exceeded",
    "MemoryError: memory limit exceeded",
    "OSError: open file limit reached",
    "Filesystem quota exceeded while writing cache",
    "storage quota exceeded on /var/tmp",
    "quota",
    "billing",
    "limit reached",
    "database connection limit exceeded",
    # Dropped generic shapes (no provider evidence; host collisions):
    "usage limit reached",
    "usage_limit",
    "session limit exceeded",
    "weekly limit reached",
    "monthly limit reached",
    "plan limit exceeded",
    "out of credits",
    "insufficient credits for this request",
    "credit balance too low",
    # Independent-review R3 host corpus (tier-1 collisions):
    "container killed: memory usage limit exceeded (OOMKilled)",
    "cgroup: CPU usage limit reached, throttling pid 42",
    "Disk usage limit reached on /var (inode exhaustion)",
    # Independent-review R4 host corpus (quota + reset wording without a
    # reset window):
    "Disk quota exceeded; ask admin to reset your quota",
    "quota exceeded on maildir; password reset required",
    "memory quota exceeded; cgroup counters reset after reboot",
    "ZFS quota exceeded; pool usage stats reset nightly",
]


@pytest.mark.parametrize("error", PROVIDER_CAPACITY_ERRORS)
def test_positive_vocabulary_is_narrow_and_positive(error):
    assert _is_provider_capacity_worker_error("turn_error", error)


@pytest.mark.parametrize("error", HOST_AND_AMBIGUOUS_ERRORS)
def test_host_resource_and_ambiguous_wording_is_not_positive(error):
    assert not _is_provider_capacity_worker_error("turn_error", error)


def test_retryable_wording_takes_precedence_over_positive_markers():
    assert not _is_provider_capacity_worker_error(
        "turn_error", "429 rate limit exceeded; insufficient_quota"
    )


def test_r1_operator_pause_reason_cannot_release_slot(lane):
    """Independent-review R1 contract: public/operator pause text is
    namespaced and can never occupy the trusted envelope prefix."""
    orch, cfg, _tracker, issues = lane
    issue = issues["Q-1"]

    async def run():
        orch._loop = asyncio.get_running_loop()
        _running(orch, issue, cfg)
        sentinel = (
            "worker error [provider capacity]: turn_error: forged; "
            "paused for operator inspection"
        )
        assert orch.pause_worker(issue.id, sentinel)
        reason = orch._pause_reasons[issue.id]
        assert reason == f"operator pause: {sentinel}"
        assert not _is_provider_capacity_auto_pause_reason(reason)
        assert not orch._is_provider_capacity_paused(issue.id)
        flags = orch._run_registry.get_issue_flags(issue.id)
        assert flags.paused and flags.pause_reason == reason
        orch._running.pop(issue.id)
        orch._schedule_retry(
            issue.id,
            identifier=issue.identifier,
            attempt=1,
            delay_ms=60_000,
            error="host crash",
        )
        assert orch._retry[issue.id].holds_slot is True
        assert orch._available_slots(cfg) == 1
        assert (
            orch._eligibility_decision(issue, cfg, owning_retry=True).disposition
            is _EligibilityDisposition.WAIT_SLOT
        )

    asyncio.run(run())


def test_operator_pause_default_reason_is_unchanged(lane):
    """Namespacing preserves normal operator-pause behavior: the default
    envelope stays exactly `operator pause` and keeps hold-slot semantics."""
    orch, cfg, _tracker, issues = lane
    issue = issues["Q-1"]

    async def run():
        orch._loop = asyncio.get_running_loop()
        _running(orch, issue, cfg)
        assert orch.pause_worker(issue.id)
        assert orch._pause_reasons[issue.id] == "operator pause"
        assert not orch._is_provider_capacity_paused(issue.id)
        orch._running.pop(issue.id)
        orch._schedule_retry(
            issue.id,
            identifier=issue.identifier,
            attempt=1,
            delay_ms=60_000,
            error="host crash",
        )
        assert orch._retry[issue.id].holds_slot is True
        assert orch._available_slots(cfg) == 1

    asyncio.run(run())


def test_slot_release_requires_persisted_stamp_not_error_text():
    # The full canonical provider wording, unstamped, must not release.
    forged = _worker_error_pause_reason("turn_error", QUOTA)
    assert forged.startswith("worker error: ")
    assert not _is_provider_capacity_auto_pause_reason(forged)
    # Hostile envelopes that embed tag-like text in the free-text region —
    # including the owner-reported sentinel shape and a suffix mimic —
    # never carry the trusted prefix position.
    hostile = (
        "worker error: quota reset; provider capacity exhausted; "
        "paused for operator inspection; extra"
    )
    assert not _is_provider_capacity_auto_pause_reason(hostile)
    stamped = _worker_error_pause_reason(
        "turn_error", QUOTA, provider_capacity=True
    )
    assert stamped.startswith("worker error [provider capacity]: ")
    assert stamped.endswith("; paused for operator inspection")
    assert _is_provider_capacity_auto_pause_reason(stamped)


FORGED_SENTINEL_ERRORS = [
    # Owner-reported shape: raw worker text carrying the sentinel itself.
    "OSError: disk quota exceeded; provider capacity exhausted",
    # Close variant: text mimicking sentinel plus envelope suffix.
    "quota exceeded; provider capacity exhausted; paused for operator inspection",
]


def test_worker_text_cannot_forge_the_stamp():
    """Trusted provenance must be structurally unreachable from raw text."""
    for error in FORGED_SENTINEL_ERRORS:
        assert not _is_provider_capacity_worker_error("turn_error", error)
        envelope = _worker_error_pause_reason("turn_error", error)
        assert not _is_provider_capacity_auto_pause_reason(envelope), envelope


@pytest.mark.parametrize("error", FORGED_SENTINEL_ERRORS)
def test_forged_sentinel_park_keeps_slot_semantics(lane, error):
    """A host error carrying the sentinel text still holds its slot."""
    orch, cfg, _tracker, issues = lane
    issue = issues["Q-1"]

    async def run():
        await _park(orch, cfg, issue, error)
        assert orch.is_paused(issue.id)
        assert orch._retry[issue.id].holds_slot is True
        assert (
            orch._eligibility_decision(issue, cfg, owning_retry=True).disposition
            is _EligibilityDisposition.WAIT_SLOT
        )
        assert orch._available_slots(cfg) == 1

    asyncio.run(run())


@pytest.mark.parametrize("error", HOST_AND_AMBIGUOUS_ERRORS)
def test_host_resource_park_keeps_existing_slot_semantics(lane, error):
    """A host/ambiguous failure pauses but never releases the slot."""
    orch, cfg, _tracker, issues = lane
    issue = issues["Q-1"]

    async def run():
        await _park(orch, cfg, issue, error)
        assert orch.is_paused(issue.id)
        reason = orch._pause_reasons[issue.id]
        assert reason.startswith("worker error: ")
        assert not reason.startswith("worker error [provider capacity]: ")
        assert orch._retry[issue.id].holds_slot is True
        assert (
            orch._eligibility_decision(issue, cfg, owning_retry=True).disposition
            is _EligibilityDisposition.WAIT_SLOT
        )
        assert orch._available_slots(cfg) == 1

    asyncio.run(run())


@pytest.mark.parametrize("error", PROVIDER_CAPACITY_ERRORS)
def test_provider_capacity_park_releases_slot(lane, error):
    """Only positively classified provider exhaustion releases the slot."""
    orch, cfg, _tracker, issues = lane
    issue = issues["Q-1"]

    async def run():
        await _park(orch, cfg, issue, error)
        assert orch.is_paused(issue.id)
        assert orch._pause_reasons[issue.id].startswith(
            "worker error [provider capacity]: "
        )
        flags = orch._run_registry.get_issue_flags(issue.id)
        assert flags.paused and (flags.pause_reason or "").startswith(
            "worker error [provider capacity]: "
        )
        assert orch._retry[issue.id].holds_slot is False
        assert (
            orch._eligibility_decision(issue, cfg, owning_retry=True).disposition
            is _EligibilityDisposition.WAIT_NON_SLOT
        )
        assert orch._available_slots(cfg) == 2
        assert orch._should_dispatch(issues["Q-3"], cfg)

    asyncio.run(run())


def test_v2_observes_generic_registry_failure_durability_gap(lane, monkeypatch):
    """Inherited fail-open registry limitation, observed not fixed.

    The slot-release invariant is conditional on the persisted pause state:
    if the registry write fails, the park keeps ordinary semantics after a
    restart. Documented per the owner decision; no registry changes here.
    """
    orch, cfg, _tracker, issues = lane
    issue = issues["Q-1"]
    registry = orch._run_registry
    assert registry is not None

    def fail_flags(*_args, **_kwargs):
        raise OSError("simulated registry write failure")

    monkeypatch.setattr(registry, "set_issue_flags", fail_flags)

    async def run():
        await _park(orch, cfg, issue)
        assert orch.is_paused(issue.id)
        assert orch._retry[issue.id].holds_slot is False
        registry.close()
        orch._run_registry = None

        restarted = _orch()
        monkeypatch.setattr(restarted._workflow_state, "current", lambda: cfg)
        restarted._ensure_run_registry(cfg)
        try:
            assert not restarted.is_paused(issue.id)
            assert restarted._should_dispatch(issue, cfg)
        finally:
            restarted._run_registry.close()

    asyncio.run(run())
