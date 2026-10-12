# #45 bounded implementation completion packet

Disposition: **READY FOR RPGFLOW #191 COMPOSED ACCEPTANCE**.

This completes the upstream runtime seam, not live adoption or parent #191.
Implementation code reviewed and verified at
`651787363c7e15c90020be4584595d2d0d34456a`; the final PR head adds this evidence
only. The PR remains open for owner review.

## 1. Base and installed identity

Installed Symphony v0.26.0: `cf25d3d0ea9b9a93019ffba4114813a8b3643668`, clean
`adopt/issue-23-v3` at `/home/carlos/dev/oh-my-symphony`, with launcher resolving
to that checkout's `.venv/bin/symphony`. Fork main is
`2f004f1cd7aef8a2ecd051ebe8fe12d276c0ea4a`, predating its seven ownership/quota
fixes. The implementation branch/worktree is `fix/45-verify-attestation` at
`/tmp/symphony45`. Its PR targets `baseline/45-installed-v026`, published at the
installed SHA, so only this new mechanism appears in the diff. Owner integration
must preserve/promote that prerequisite lineage; main was not modified.

The authorized additive approach still holds on that inspected base. No core
redesign, persistent coordinator or live Symphony dispatch was needed. #45 and
the latest #191 owner authorization were read before editing; #192 evidence was
reused without restarting broad research.

## 2. Files and purpose

- `orchestrator/verify_receipts.py`: opt-in contract validation, private receipt
  snapshot and strict trusted predicate wire protocol.
- `orchestrator/attempt.py`: fresh Verify input/candidate capture, successful
  completion attestation, normal terminal condition and in-flight bookkeeping.
- `orchestrator/core.py`: shared rejection/hold handling; reconcile, exit retry,
  startup cleanup and per-card periodic sweep checks.
- `orchestrator/worker_exit.py`: validate before finalization; hold unresolved
  checks in existing ownership retry machinery. Workflows without the feature
  keep their original scheduling/cleanup behavior.
- `orchestrator/entries.py`: runtime-only author-session/in-flight flags.
- `workflow/preflight.py`: malformed contract surfaces as dispatch validation.
- `tests/test_verify_receipts.py`, `tests/fixtures/verify_condition.py`: actual
  positive/negative runtime oracles with a frozen repository condition.
- `docs/verify-closure.md`, README link and this evidence directory: exact
  integration, adoption scope and proof, including faithful read-only PR190 data.

Source paths above are under `src/symphony/` unless marked otherwise.

## 3. Receipt representation, ownership and persistence

Before a configured independent Verify turn, invalidate the previous private
receipt and remove the closure input; observe Git HEAD with no uncommitted tracked
changes. A successful returned turn plus runtime completion event permits capture
of fresh JSON whose candidate SHA equals the observed SHA. Board self-identification,
Git author names, generic APPROVE and Document files do not attest provenance.
Pinned author or resumed conversations cannot supply independent authority.

A latest private JSON receipt binds issue identity, runtime run/local turn,
authorized stage/backend, observed candidate and completion SHAs, contract hash,
artifact reference and exact bytes/digest. Atomic replace follows file fsync.
Storage: host `.symphony/verify-receipts/<sha256(issue.id)>.json`, outside worker
workspaces. Later Document overwrites cannot upgrade this snapshot. Malformed,
non-UTF-8, oversize or wrong-candidate artifacts produce no receipt. No signing,
secrets, database or coordinator was introduced.

## 4. Predicate and runtime enforcement paths

The trusted repository argv command gets issue identity and the runtime receipt
(or null) on stdin. Only exit 0 plus strict `passed:true, missing:[]` accepts.
Explicit negative evidence parks in the configured failure state, followed by an
inspectable missing set. Unavailable/nonzero/timed-out/malformed-output predicates
hold acceptance/cleanup and retry without rewriting board state. Invalid private
receipt bindings definitively reject. The repository owns authoritative finding
identity/severity/A, explicit closure/B, current candidate/C and actual lineage.
No second full external review is triggered.

Applied before normal turn-end acceptance; worker exit/finalization; pending exit
validation retries; reconcile `terminal_seen_at`/cleanup; startup workspace cleanup;
and every tick's terminal sweep. Sweep failures are isolated per card. A live
Verify turn still collecting evidence is held during reconcile, then validated
at turn end or exit. Configured positive closure still reaches Human Review.

## 5. Restart behavior

Durable receipts survive process restart/workspace cleanup. No approval cache is
trusted: startup and each poll re-evaluate configured terminal cards even without
a running entry. Missing receipt remains missing after restart. A run-enrollment
filter was deliberately avoided because an unseen forged terminal could bypass
it. Operator-owned pre-adoption scope inventory is required; unconfigured
historical obligations may be explicitly excluded by trusted policy, never by
worker prose. In-scope false historical terminals, including PR190, still reject.

## 6. Deterministic proof

[Focused runtime log](qa/focused.log): **62 passed**, including 18 unchanged
ownership tests. No expected failures count as acceptance.

| Oracle | Passing result |
|---|---|
| Faithful PR190 actual generic Verify + false Document table/packet | Rejected |
| No Verify receipt / Document-only closure | Rejected |
| Generic APPROVE without exact IDs | Rejected |
| Wrong ID, severity or extra unknown/duplicate records | Rejected |
| Stale/incompatible candidate lineage or wrong observed SHA | Rejected |
| Partial closure | Rejected; exact absent URL inspectable |
| `out_of_scope_requires_decision` | Blocked decision boundary, never closed |
| P3-only authoritative advisory set | Non-blocking |
| Every material finding independently attested on valid lineage | Human Review reached through actual routed turn loop |
| B plus permitted QA-only descendant C | Eligible; no second external review |
| Worker-exit/crash and failed-write retry | Fail-closed; pending validation recovered |
| Reconcile and startup/per-tick with no running entry | Reject before acceptance/cleanup |
| Predicate outage/malformed output | Hold with no rewrite or cleanup; retry |
| One failed card among multiple terminals | Other cards validated; owner scope retained |
| Later Document overwrite / restart private receipt | Cannot replace attested bytes; valid snapshot passes |

[Composed board log](qa/composed.log): **10 passed** using the candidate runtime,
RPGFlow's real FileBoardTracker/disposable lane harness and frozen PR190 evidence.
Negative cases end in Blocked; valid and P3 cases reach Human Review. The predicate
uses frozen host finding/ancestry data; this is not a live GitHub-reader/real-Git
lineage proof. Reproducible local harness: `/tmp/composed45/test_composed_runtime.py`.
It imports the unchanged diagnostic worktree test harness; no RPGFlow files were
edited. Real policy integration remains below.

## 7. Existing behavior and full verification

[Final full coverage run](qa/full.log): **2740 passed, 16 skipped, 85.59%coverage**,
exit 0, on reviewed code 6517873. This includes ownership, rewind, stage contracts,
terminal handling, retry and release-finalizer behavior. No live/provider calls.
[Existing RPGFlow compatibility](qa/rpgflow-compat.log): **64 passed**, explicitly
loading candidate runtime; reader, stage ownership, rewind, PR-head and
`auto_merge_on_done:false` behavior remain green.
[Type check](qa/type.log): zero errors/warnings. Changed-source/test Ruff and
translation check pass. Full lint retains 3 pre-existing unused-name findings in
the installed baseline's ownership tests; no new lint finding. The Nix test
namespace and interim failures/corrections are disclosed in [environment notes](qa/environment.md).
The namespace is solely a test environment; installed runtime/host profiles were
not changed. No new process isolation is part of the implementation.

## 8. Exact RPGFlow integration still required

Use [the wire contract](../../verify-closure.md) with a trusted host checker and
Verify prompt/schema output. The checker must use #188's authoritative review
reader, reject missing/malformed/stale reader evidence, retain exact native
finding URLs/severity/reviewed SHA/text, compare explicit independently captured
closures and actual Git/PR candidate lineage. Prove A->B ancestry and permit B->C
only for the approved QA-only diff; broader changes require an appropriate
independent review decision. Do not install the fixture predicate.

Freeze operator-owned obligation scope after inventorying existing terminal
cards, configure a dedicated Verify backend, private storage and both Human
Review/Done destinations. Preserve `auto_merge_on_done:false`; do not use automatic
Blocked recovery as a fake disposition of unresolved material findings.
Run full parent #191 acceptance with that real checker and prompt/schema in an
isolated lane, including PR190 negative, all-closed positive and compatibility.
Owner review/adoption is a separate action. PR190 is suitable as the frozen
post-adoption negative regression; this phase does not authorize its repair or
#47 continuation.

## 9. Independent review

[Initial Claude challenge](claude-review.md) on exact `55bd3c9`:
CHANGES_REQUIRED (F1-F5). One bounded repair followed.
[Delta challenge](claude-delta-review.md) on exact
`651787363c7e15c90020be4584595d2d0d34456a`: **APPROVE**, all five findings closed;
62 focused tests and independent delta probes passed. Model `claude-opus-5-5`.
One initial 16-tool-turn budget and one 8-turn delta budget; no recursive review,
manual Codex request or live lane. Final head adds evidence only, with runtime
source exactly equal to that approved candidate.

## 10. Residual assumptions and maintenance

Trusted host configuration/checker/private receipt storage are the authority
boundary; deliberate same-UID edits there remain out of scope. Verify is still
a semantic oracle: runtime proves provenance/identity/lineage existence, not the
truth of a false structured review. Missing configured obligations must never
be inferred from worker labels or prose. Board writes can be briefly visible
before the next observer check; consumers must use accepted runtime conditions,
not a worker's OWNER_REVIEW_READY heading alone.

Receipts retain one latest snapshot per issue. The checker must read current
required findings and PR head each validation; fast nonzero exit handles outages.
A hanging command can cost 30 seconds per terminal card per poll (Claude R2, low).
Malformed raw `agent:null` may surface an AttributeError instead of a clean
config message (R1, low); it still fails closed. Both remain disclosed rather
than starting another review/architecture cycle. The schema/configuration is a
small maintained integration contract. Historical-scope inventory and fresh
Verify runs for recovered conversations are operator responsibilities.

## 11. Disposition

**READY FOR RPGFLOW #191 COMPOSED ACCEPTANCE**.

Implementation PR open for owner review; no merge, installation/adoption, live
dispatch, PR190 modification, #47 continuation, coordinator or notification work.
