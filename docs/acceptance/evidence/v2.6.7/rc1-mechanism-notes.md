# RC1 mechanism evidence notes (non-lifecycle)

These records are bounded mechanism/test-flake evidence per the 2.6.7
Outcome Contract. They consume no product cycle and do not enter the
lifecycle defect ledger or its three-class release stop count.

## M267-001 — `test_runtime_task_summary.py` timing sensitivity under load

- Observed twice in this dispatch during full `make test` runs executing in
  parallel with other worker-spawning suites; never reproduced in isolation
  (3/3 green single runs immediately after each full-suite failure).
- First observation predates every 2.6.7 code change: the baseline
  `make test-harness` at the approved plan HEAD (`35c22ed1`) failed once in
  the same file (`fix-and-resubmit consumes an identity-bound response and
  reaches review`) and passed on rerun.
- Second observation (`summary-only refresh reuses the exact-HEAD
  verification identity and effect`) occurred at `run make test` after the
  2.6.7 slices; same file, different case, green 3/3 in isolation.
- Classification: test flake (real worker subprocess + poll-timing fixture
  under machine load), outside the behavioral subject; tests are excluded
  from `lifecycle_subject_sha256`.
- Disposition: recorded. If it recurs deterministically or in isolation,
  open a typed lifecycle defect with a reproducer.
