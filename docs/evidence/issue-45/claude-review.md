# Independent Claude challenge

Exact candidate: `55bd3c973d1d0ab8782244ef19ba65f87185227f`.
Model: Claude Opus (CLI configured default). Read-only session
`655153e4-7ea1-4508-9fb5-c270c41c4a2d`, 17 reported turns, $1.1096792.

# Verdict: CHANGES_REQUIRED for candidate `55bd3c9` (compared against base `cf25d3d`)

I found one High and one Medium defect, both in the runtime code this commit adds. Both are confirmed by an offline probe. The provenance, binding and enforcement core holds up. Neither blocking defect is a host environment failure or base lint.

## Blocking findings

**F1 (High): the per-tick sweep and startup cleanup move healthy cards and pre-adoption Done cards to Blocked.**
- **Where:** `src/symphony/orchestrator/core.py:9035-9047` (sweep), `core.py:10050` (startup cleanup), `core.py:9011-9033`, and `src/symphony/orchestrator/verify_receipts.py:260-261`.
- **Problem 1:** The sweep fetches every card in a configured terminal state on every poll. It runs the checker on each one, whether or not that card was ever dispatched under this contract.
- **Problem 2:** `evaluate` merges "checker unavailable, malformed or timed out" with an explicit `passed:false` rejection. `_enforce_verify_closure` then writes `failure_state` for either one.
- **Concrete failure:** On first adoption, every historical RPGFlow Done card whose PR has P0–P2 findings and no receipt moves to Blocked and gets a note. Separately, a single GitHub or reader outage, or a 30-second timeout, moves every Done and Human Review card to Blocked, including cards that passed earlier.
- **Probe:** a pre-adoption `Done` card with no receipt ends up `Blocked` with "Verify Closure Rejected" (`/tmp/45-review/probe.log`, `HISTORICAL Blocked`). No existing test uses more than one card or a historical card.
- **Bounded fix:**
  1. Make `evaluate` return three outcomes: pass, reject, unavailable.
  2. On "unavailable", write nothing to the board. Instead: skip cleanup, don't set `terminal_seen_at`, keep the exit check pending, record a tracker error, and retry next poll.
  3. Only an explicit `passed:false` writes `failure_state`.
  4. Limit the sweep and startup cleanup to issues the runtime has a run-registry record for. Or, at minimum, document a required pre-adoption step and test it.

**F2 (Medium): a sweep exception aborts the whole tick, before dispatch validation and dispatch.**
- **Where:** `core.py:4129` and `core.py:9043-9047`. There is no try/except around each card or around the terminal fetch.
- **Concrete failure:** One failing tracker write, a failing terminal fetch, or a malformed hot-reloaded `verify_closure` stops all dispatch on every poll. In the malformed-config case, `contract()` raises in the sweep before the graceful `validate_for_dispatch` check at about line 4156 is reached.
- **Probe:** `SWEEP_RAISES` confirmed that the exception propagates.
- **Bounded fix:** wrap each card in try/except that calls `_record_tracker_error` and continues. Catch fetch errors too. If `contract()` raises `ValueError`, skip the sweep and let `validate_for_dispatch` fail closed.

## Non-blocking findings

**F3 (Medium, conditional): a Verify that writes a terminal state mid-turn can be wrongly rejected.**
- **Where:** `verify_receipts.py:113` and the reconcile check at `core.py:9407`.
- `begin` deletes the receipt before the turn. If Verify writes a terminal state during its turn, the reconcile tick can see that state before `complete()` creates the new receipt. The runtime then rejects it, moves the card to Blocked, and cancels a legitimate Verify. The probe confirmed this (`MIDTURN False`).
- RPGFlow uses Verify → Document, so the impact there is limited. The generic contract does allow it.
- **Fix:** while a configured Verify turn for the running entry is still in flight, have reconcile defer (return without setting `terminal_seen_at`) and leave the check to `_evaluate_turn_result` after the turn. Or forbid terminal success destinations for Verify in `contract()`.

**F4 (Low): malformed closure input causes a retry loop instead of "no receipt".**
- **Where:** `verify_receipts.py:161` and `:177`.
- Malformed JSON or non-UTF-8 content raises (the probe confirmed `JSONDecodeError`). That becomes a `verify_receipt_error` exit and a Verify retry. The doc says "invalid … produces no receipt".
- **Fix:** catch `ValueError` and return without a receipt.

**F5 (Low): the dedicated-backend check has a gap.**
- **Where:** `verify_receipts.py:78-79`.
- The check `agent.kind == verifier_kind and not kinds` accepts a partial `stage_kinds` whose default kind is the verifier kind. Author stages that aren't listed then run the verifier backend, which contradicts "dedicated".
- Independence still holds because each phase or entry starts a new session and `verify_session_authored` is sticky.
- **Fix:** reject this config when any active state other than Verify lacks a `stage_kinds` entry.

## What held up

- **Pinned and resumed sessions:**
  - `_rebuild_backend_for_phase` starts a new session at each phase change and resets `last_completed_turn_event` (`core.py:7168-7193`).
  - `verify_session_authored` and `recovery_session_resumed` stay set for the life of the entry and block attestation.
  - A quota-fallback pin to a non-verifier backend fails closed.
- **Candidate binding:** HEAD is observed on a clean tree and the closure must match `candidate_sha` exactly. The artifact is cleared before the turn, and symlinks and path traversal are refused.
- **Turn end and provenance:** the receipt requires the runtime's own completion event and checks the backend and authorized stage at completion. A Document overwrite after the receipt is ignored; the test plus the restart path cover this.
- **Every terminal path calls the check:** turn result, reconcile (after the ownership clamp, before `terminal_seen_at`), worker exit, pending-exit drain, startup cleanup, and the sweep. The opt-in early return at `worker_exit.py:195` blocks finalization while a check is pending.
- **Checker I/O:** output is parsed strictly. The null-receipt P3 path goes to the checker as specified.
- **Receipt freshness after later author stages:** this is deliberately left to the repository checker, because no current HEAD is sent in the request. That matches the stated threat model, but the live checker must read the real PR head.
- **Oracle validity:** the PR190 fixture contains the actual Verify prose (REJECT rounds and the final APPROVE at `8dda2c0`) and the actual false Document table and packet. It is rejected because there are no structured closure records. The runtime never reads the Document description.
  - This proves the negative case for "a generic approval isn't closure". It does not and cannot prove that a false *structured* closure gets caught; that is subjective correctness and out of scope.

## Limits of evidence

- I ran `tests/test_verify_receipts.py` and `tests/test_stage_ownership.py`: 50 passed (`/tmp/45-review-tests.log`). I also ran my 4-case probe (`/tmp/45-review/test_probe.py`, `probe.log`).
- I did not rerun the full suite or the RPGFlow composed tests, and I didn't read `composed.log` or `rpgflow-compat.log`.
- I didn't check whether reconcile isolates exceptions per entry around the new call at `core.py:9404`.
- I didn't inspect the Jira and Linear trackers, or `_auto_normalize_legacy_human_review_done`. That function runs right after the sweep and matters if Done is not a configured closure destination.
- I made no source, board or config edits, and no live dispatch.

Plan mode was on, but this session had no tool to write the plan file or exit plan mode, so this summary is the whole deliverable.

## Bounded dispositions

F1: Transport/unavailable is now a distinct exception that holds acceptance and cleanup without board writes. Explicit negative evidence still parks. Historical scope remains owned by the trusted repository predicate; mandatory pre-adoption inventory and owner-owned scope decisions are documented and covered by a multi-card oracle. A run-registry-only filter was not applied because it would allow an unseen forged terminal after restart. PR190 remains in scope.

F2: Per-card errors are isolated and recorded; terminal-fetch errors skip the sweep; invalid configuration is reported by dispatch preflight.

F3: Runtime-owned Verify-in-flight flag defers reconciliation acceptance until receipt capture. Exit and turn end still validate, including failed turns.

F4: Malformed/non-UTF-8/oversize artifact input produces no receipt.

F5: Partial dedicated-backend configuration with a verifier default is rejected.

Self-audit: fixture predicate now rejects extra unknown records; added oracle.
