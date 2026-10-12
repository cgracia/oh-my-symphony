# Runtime-attested Verify closure condition

Opt-in runtime seam for control-plane #45 / RPGFlow #191. Existing workflows
without this configuration retain their behavior. This is evidence provenance
and terminal eligibility, not subjective review correctness or review ingestion.

## Operator-owned configuration

Add a top-level block to the host WORKFLOW.md (not the worker workspace):

```yaml
verify_closure:
  verify_state: Verify
  verifier_kind: claude
  artifact: .verify-closure.json
  terminal_states: [Human Review, Done]
  failure_state: Blocked
  command: [/absolute/path/python, /trusted/host/check_verify_closure.py]
```

All six keys are required; malformed configuration fails closed. Configure a
dedicated Verify backend via `agent.stage_kinds`, distinct from author stages.
The artifact path is workspace-relative, with no traversal. The command is
trusted argv (no shell), runs from the host workflow directory with a 30-second
timeout, and must not load executable code/policy from a worker workspace.
Include every success destination that must require closure. In RPGFlow this
includes Human Review and Done. Do not auto-archive or normalize an unchecked
success destination into an unchecked success state.

## Receipt ownership and persistence

Before each actual configured Verify turn, the runtime deletes any prior private
receipt and the workspace closure input. It observes Git HEAD and requires no
uncommitted tracked changes at that point. A pinned author session cannot attest
itself; recovered conversations are conservatively ineligible and must receive
a fresh Verify run. Only a successfully returned turn with its runtime completion
event can mint a receipt. Missing or invalid candidate binding produces no receipt.

Verify writes a JSON object with `candidate_sha` equal to that observed HEAD.
All other closure schema fields belong to the repository's checker. Symphony
stores the exact bytes and SHA-256, candidate HEAD and completion HEAD, actual
backend/authorized stage, runtime run ID/local turn, issue identity, and a hash
of this configured contract. These facts do not come from worker JSON labels.

The single latest receipt lives at
`<host workflow>/.symphony/verify-receipts/<sha256(issue.id)>.json`. Atomic replace
follows file fsync. Receipts survive restart and workspace cleanup; they are
private operational evidence, never imported from a board note or workspace
receipt. Do not symlink this directory into worker workspaces. Deliberate same-UID
edits to private runtime files, operator configuration or the trusted checker are
outside this slice's threat model. Keep this directory out of version control.

## Checker wire contract

One JSON request is written to stdin:

```json
{"schema":1,"issue_id":"…","identifier":"GH-47","terminal_state":"Human Review","receipt":null}
```

`receipt` is null if no receipt exists, otherwise the runtime record described
above, including `artifact_content` (original JSON text), `artifact_sha256`,
`candidate_sha`, `completed_sha`, `run_id`, `turn`, `stage`, `verifier_kind`,
`contract_sha256`, `artifact`, `issue_id`, `identifier`, and `schema: 1`.
The runtime validates receipt bindings/digest before invoking the checker.

Success requires exit 0 and exactly
`{"passed": true, "missing": []}` on stdout. Rejection uses
`{"passed": false, "missing": ["exact finding URL or diagnostic", "…"]}`.
Other output, transport errors, timeout or nonzero exit produce an unavailable
condition: acceptance and cleanup are held, a runtime error is recorded, and
validation retries without rewriting the board. Explicit `passed:false` means
a definitive rejection. Repository transport/reader outages must exit nonzero;
malformed or missing closure evidence is a definitive rejection. No arbitrary
worker-supplied path is used to obtain the closure during terminal validation.

The repository checker **must** independently obtain the authoritative required
material finding set and the current candidate, then compare exact identity,
native severity, external reviewed SHA A, disposition, repair SHA B and rationale.
Unknown/duplicate finding records, missing findings, malformed review-reader
status and incompatible lineage must reject. `out_of_scope_requires_decision`
is a blocked decision boundary, never implicit closure. A null receipt may pass
only when the authoritative required material set is empty (e.g. P3 advisory).
A -> bounded B -> independent Verify B is supported; a QA-only descendant C may
retain B coverage if the checker proves ancestry and permits the actual changed
paths. No second external review is triggered by Symphony.

`tests/fixtures/verify_condition.py` illustrates this wire contract with frozen
host evidence. Its ancestry lists are test doubles, not a live Git/GitHub reader.
Do not install that fixture checker as the RPGFlow policy implementation.

## Enforcement and restart

The same predicate runs before normal terminal turn acceptance, before worker
exit can finalize/clean up, in exit validation retries, and before reconciliation
sets `terminal_seen_at` or removes the workspace. A per-tick sweep also checks
terminal cards without a running entry, including the immediate startup tick.
There is no persisted coordinator, approval cache or new workflow stage.

An in-flight independent Verify turn is held during reconciliation until its
receipt is complete; its own turn-end/exit path then decides acceptance.
Rejection first writes the configured failure state, then appends an inspectable
`Verify Closure Rejected` note with the missing set. Failed writes propagate to
existing running/exit retry handling; a failed sweep card records an error and retries
on the next poll while other cards continue. A terminal-fetch failure skips the
sweep; malformed configuration is left to dispatch preflight. Startup cleanup also applies the predicate before removing any configured
terminal workspace. No terminal cleanup occurs on a failed check. A worker can
briefly write a terminal state to the board; this observer-based seam rejects it
at the next runtime observation. It does not make arbitrary external board reads
linearizable. Consumers must treat the board packet as unaccepted until the
runtime condition validates it. Live adoption remains a separate owner action.

## Required pre-adoption scope decision

Before enabling the condition, inventory existing cards in the configured
terminal destinations against the trusted checker. Decide explicitly which
historical issue identities are outside the configured review obligation; put
that scope in operator-owned policy, never mutable ticket/workspace prose. Any
in-scope historical card with required material findings and no valid receipt
will be rejected on startup, intentionally. Do not exempt cards merely because
a worker once claimed Done. PR190's false terminal stays in scope. The multi-card
scope oracle covers an explicitly unconfigured historical card, one failing
tracker write, and one in-scope false terminal. Filtering solely on a current
running entry or new-contract run record would permit unobserved/restart bypass,
so this seam observes all configured destinations and delegates obligation
scope to the trusted repository condition.

## RPGFlow composed acceptance remaining

Provide the narrow authoritative checker using the #188 review reader and actual
Git/PR lineage, Verify schema/prompt output, and frozen trusted configuration.
Run the matrix against this runtime in an isolated RPGFlow lane: PR190 negative,
all-findings-closed positive, P3/decision cases and ownership/PR-head/no-auto-merge
regressions. The upstream tests prove runtime provenance/enforcement composition
with a frozen repository predicate, not correctness of a future live checker.
For the RPGFlow slice, retain `auto_merge_on_done: false` and disable automatic
Blocked recovery while closure is a decision/verification obligation.
PR190/#47 remain untouched; do not adopt this runtime merely because tests pass.
