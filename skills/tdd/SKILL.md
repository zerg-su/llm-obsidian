---
name: tdd
description: Implement approved behavior through red-green slices. Use for clear feature or bug-fix coding, not prototypes or diagnosis-only work.
---

# TDD

Treat an approved plan or ContextPacket as seam confirmation. Otherwise confirm
the observable behavior and implementation authorization before editing code.

For each vertical slice:

1. Add the smallest test that fails for the intended public behavior.
2. Run it and verify the failure is the expected product gap.
3. Implement the smallest production change that makes it pass.
4. Run the focused test, then affected integration checks.
5. Refactor only while green; preserve the public assertion.
6. Commit a runnable slice before starting the next behavior.

Use test doubles only at external adapters; do not replace domain behavior with
the mock. Exempt pure documentation, deterministic generated output, disposable
prototypes, and mechanical moves when an executable assertion would add no
signal. Never weaken an existing gate to manufacture green.
