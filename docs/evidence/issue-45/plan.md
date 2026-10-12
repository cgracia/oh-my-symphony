# Bounded Verify attestation implementation

Contract: control-plane #45 and RPGFlow #191 owner authorization. Observed
failure: Document forged independent finding closure. No gameplay authority
changes. Base: installed v0.26.0 cf25d3d (fork main 2f004f1 lacks its ownership
fixes). Isolated branch/worktree; no live dispatch or installation.

Compose a private JSON receipt with existing turn lifecycle and ownership
checks. Clear the closure input and invalidate the prior receipt before an
actual configured Verify turn, observe Git HEAD, then snapshot fresh structured
closure only after successful completion. Store run/turn/stage/backend, observed
SHA, contract fingerprint and content digest outside the worker workspace.

A trusted host-configured argv predicate receives only the runtime snapshot
and issue identity. The repository owns required finding ingestion and lineage
policy. Exit status/output must strictly pass. Reject terminal transitions via
existing state update/error/retry paths, before cleanup; repeat against all
configured terminal cards each tick so restart needs no coordinator. Missing
receipt is passed to the predicate so authoritative advisory-only sets may
pass. Other workflows remain unchanged.

Prove real negatives and a valid positive, crash/retry, reconcile, restart and
existing ownership/release behavior. Preserve PR190 fixture. Freeze a candidate
and obtain one independent Claude challenge, bounded repairs/delta if needed.
Document exact repository integration and remaining live adoption steps.
