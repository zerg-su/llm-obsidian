---
name: debug
description: Diagnose reproducible bugs to root cause. Use for crashes, regressions, wrong behavior, or flakiness; fix only when authorized.
---

# Debug

Deterministic repro command: red evidence.
Otherwise require red-capable loop before hypotheses; absent: report evidence gap, stop speculative fix/completion claim.

1. Rank/test falsifiable hypotheses; prove repro-backed root cause before product mutation.
2. diagnosis-only: report evidence; stop without product edits.
3. Authorized fix: narrow seam/regression; rerun minimal/original feedback loops.
4. Failed fix attempt = product change + failed original repro rerun.
5. Three failed product fixes: unconditional architecture stop; no fourth.
6. Link defect repair to declared outcome evidence; absence bars completion.
7. Remove temporary instrumentation; report residual uncertainty.

Escalate before public-interface/migration/security-boundary/dependency/permission/external-state changes.
Never hide failures, pad timeouts, claim untested root cause.
