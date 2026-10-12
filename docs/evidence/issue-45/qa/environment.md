# Test environment and interim failures

Initial full run on host: 77 failed, 2627 passed, 15 skipped, 24 errors. Hard-coded /usr/bin Git paths fail on Nix; unrelated /tmp/.git contaminates root scanning; new opt-in early return caused existing release/finalizer failures. Full trace retained.
First namespace initialization: /dev/null denied on read-only root; no tests collected. Full trace retained.
POSIX namespace run: 24 failed, 2705 passed, 15 skipped. Initial implementation opt-out regressions fixed; remaining host /etc/bash_logout modifies return codes/output, plus one chat transcript assertion. Full trace retained.
Final namespace: standard /usr/bin and /bin command paths; tmpfs /tmp with only isolated repo and clone bind-mounted; native /dev and /proc; empty logout file bound over the resolved Nix logout script inside namespace only. Host root/profile/runtime unchanged. This is a test environment, not a runtime security feature.

The clean-shell initial candidate run passed2727 tests with15 skips and85.57%coverage, with2 environment failures: a broken user-local Python3.12 selected by the unchanged workspace setup script. Hiding that binary only inside the test namespace made the workspace file pass70tests. On the repaired candidate, the full final run is2740passed/16skipped/85.59%coverage. The extra skip follows hiding local CLI binaries during this offline run.

The focused transport tests initially changed the command *after* attestation, so the contract hash correctly rejected the receipt rather than simulating transport failure. Fixed by freezing the command before attestation. An oversized parameter ID also inflated PYTEST_CURRENT_TEST beyond subprocess argument limits; explicit short IDs fixed the test harness. All raw failing logs remain under `/tmp/45-results/`, `/tmp/symphony45-full.log` and `/tmp/symphony45-focused-fixed*.log`; they are not counted as passing evidence.

Changed-source/test lint passes. Whole-suite lint has three pre-existing unused-name findings in `tests/test_stage_ownership.py` from installed base cf25d3d; unchanged by this implementation. Type checking and translation check pass.
