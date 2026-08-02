---
name: tdd
description: Implement approved behavior through red-green slices. Use for clear feature or bug-fix coding, not prototypes or diagnosis-only work.
---

# TDD

Confirm edit authorization.

vertical slice:
1. Before test, name production change that should fail.
2. Add test that fails at observable seam; source-text is not behavior evidence.
3. Regression red: pre-fix disposable worktree/saved base; never destructive reset.
4. Smallest change that makes it pass; run affected integration checks.
5. Bind green to declared success evidence, not task completion; gaps explicit.
6. Commit a runnable slice.

Stateful/workflow-heavy: inventory states/transitions; exhaust a fast deterministic matrix as release invariant.
Coverage denominator includes never-executed lines; observed-only is not coverage evidence. Mock provider/transport; keep state transitions real.

Exempt pure documentation, deterministic generated output, disposable prototypes, mechanical moves: record proportional check; never weaken gates
