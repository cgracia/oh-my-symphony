# Independent Claude delta challenge

Exact candidate: `651787363c7e15c90020be4584595d2d0d34456a`.
Model: `claude-opus-5-5`. Same read-only session `655153e4-7ea1-4508-9fb5-c270c41c4a2d`, 7 reported turns. CLI reported cost $1.5458587999999998 (resumed-session accounting; not summed with initial report).

# Delta verdict: APPROVE for candidate `651787363c7e15c90020be4584595d2d0d34456a`

All five findings (F1–F5) are closed, and the repairs introduce no blocking regressions. I found two low-severity leftovers, both in code changed by the repairs; neither blocks.

## Findings closed

**F1 (High): closed.**
- `evaluate` now raises `ConditionUnavailable` when the checker fails to run, times out, exits nonzero or returns malformed output (`verify_receipts.py:276-279`).
- My probe showed no board write on any of these failures:
  - **Sweep, checker exits 3:** the card stayed `Done` with 0 notes.
  - **Checker returns `{}`:** the card stayed `Human Review` with 0 notes.
- Every caller holds and retries:
  - **Sweep:** records a per-card error.
  - **Reconcile:** the existing per-entry try at `core.py:9234-9244` catches it, so `terminal_seen_at` is never set.
  - **Turn result:** exits with `phase_transition_error`. Worker exit then records the error and leaves the exit check pending, and the drain retries it.
  - **Startup cleanup:** catches it and skips the workspace.
- An explicit `passed:false` still moves the card to the failure state.
- **Scope, assessed against "configured material findings":** I accept the approach.
  - Which issues carry obligations is decided by the trusted checker's operator-owned policy. The fixture's `outside_configured_scope` list is keyed on the `issue_id` the runtime supplies.
  - Every configured terminal card is still observed, and nothing in a worker's board or workspace can exempt a card.
  - Filtering on run-registry records would have let a terminal card forged after a restart go unobserved, so the repair is right to reject that option.
  - The remaining risk is operational: an owner who skips the documented pre-adoption inventory gets in-scope historical cards parked. The contract treats that as intended.

**F2 (Medium): closed.** `core.py:9035-9058` now isolates each card, records its error, and returns if the terminal fetch fails. A `ValueError` from bad config is left to dispatch preflight.

**F3: closed.**
- `begin` sets `verify_turn_pending` after the candidate is observed.
- The flag is cleared in the `finally` after `complete` and again in `_cleanup_backend`.
- Reconcile holds while the flag is set (`core.py:9418`). This check comes after the ownership clamp, so clamp behaviour is unchanged.
- A Verify turn that fails mid-way still goes through worker-exit enforcement and gets a definitive rejection, because no receipt was created.

**F4: closed.** Oversize, malformed JSON and non-UTF-8 artifacts now produce no receipt.

**F5: closed.** The verifier as default backend combined with a partial `stage_kinds` is now rejected. `stage_kinds` keys are normalized (`config.py:296`), so the membership test is valid.

**Fixture self-audit:** the fixture checker now rejects unknown closure identities, which strengthens it as a test oracle.

## Remaining non-blocking findings

- **R1 (Low): bad `agent` config can fail every tick.**
  - **Where:** `verify_receipts.py`, F5 clause: `cfg.raw.get("agent", {}).get(...)`.
  - **Problem:** if the raw `agent` value is null or not a mapping, this raises `AttributeError` (my probe: `AGENT_NULL AttributeError`). Neither the sweep's `except ValueError` nor preflight's `ValueError` → `ConfigValidationError` conversion catches it. Every tick would then fail instead of logging a clean `dispatch_validation_failed`.
  - **Impact:** it still fails closed; nothing is accepted.
  - **Fix:** use `raw_agent = cfg.raw.get("agent") or {}` and raise `ValueError` if it isn't a dict.
  - **Limit:** I did not confirm whether the real config loader even accepts `agent: null`.
- **R2 (Low): a hanging checker can stretch every tick.**
  - **Where:** the sweep loop.
  - **Problem:** before the F1 fix, a checker outage moved each card to Blocked once. Now the outage is held and retried, so a checker that hangs costs up to 30 s per terminal card on every poll. That delays dispatch and the per-tick lease heartbeat. The contract tells checkers to exit nonzero on an outage, which is fast, so only a hanging checker hits this.
  - **Fix:** stop the sweep for that tick after the first `ConditionUnavailable`.

## Evidence limits

- On the exact HEAD with a clean tree, `tests/test_verify_receipts.py` plus `tests/test_stage_ownership.py` gave **62 passed** (`/tmp/45-review/delta-tests.log`).
- My 3-case delta probe passed (`/tmp/45-review/delta-probe.log`).
- I did not run the full suite, the composed RPGFlow cases or lint, and I didn't read the new tests beyond their names.
- I didn't trace whether `_cleanup_backend` runs on every attempt exit path before worker exit. I inferred that from the attempt structure.
- Workspace cleanup and normal-exit handling after a held unavailable check are deferred until the drain validates the card. I judged that acceptable, but no test covers it.
- I made no source, board or config edits and no live activity. Probe files were written only to `/tmp/45-review/`.

No runtime source changes follow this approved candidate. Final head adds completion evidence only. R1/R2 remain disclosed, non-blocking residuals; no recursive review was requested.
