---
name: tdd
description: Implement approved behavior through red-green slices. Use for clear feature or bug-fix coding, not prototypes or diagnosis-only work.
---

# TDD

Confirm edit authorization. Red→green; refactor only when green.

vertical slice:
1. Before test, name production change to fail.
2. Write test that fails at observable seam; source-text is not behavior evidence.
3. Regression red: pre-fix saved base/disposable worktree; no destructive reset.
4. Minimal change makes it pass; run affected integration checks.
5. Bind green to declared success evidence, not task completion; explicit gaps.
6. Commit a runnable slice.

Stateful/workflow-heavy: inventory states/transitions; exhaust a fast deterministic matrix as a release invariant.
Coverage denominator includes never-executed lines; observed-only is not coverage evidence. Mock provider/transport; keep state transitions real.

Exempt with recorded proportional check: pure documentation, deterministic generated output, disposable prototypes, mechanical moves; never weaken gates.
