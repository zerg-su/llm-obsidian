---
name: tdd
description: Implement approved behavior through red-green slices. Use for clear feature or bug-fix coding, not prototypes or diagnosis-only work.
---

# TDD

Confirm observable seam and edit authorization from approved plan/ContextPacket.

For each vertical slice:

1. Before testing, name the production change that should make it fail.
2. Add smallest test that fails at observable seam; a source-text proxy is not behavior evidence.
3. For regression, prove red on preserved pre-fix state in disposable worktree/saved base; never destructive reset.
4. Implement smallest production change that makes it pass.
5. Run focused test and affected integration checks; refactor only while green.
6. Bind green to declared success evidence, not task completion; leave gaps explicit.
7. Commit a runnable slice.

Use test doubles only at external adapters. Exempt pure documentation, deterministic generated output,
disposable prototypes, or mechanical moves only with a recorded proportional check; never weaken gates.
