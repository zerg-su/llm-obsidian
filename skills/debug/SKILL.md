---
name: debug
description: Find reproducible bug root causes; fix only when authorized.
---

# Debug

Deterministic repro command = red evidence.
Without it: red-capable loop before hypotheses; record evidence gap; stop speculative fix/completion.

1. Reproduce and record failure; minimize without changing its meaning.
2. Falsify hypotheses; prove repro-backed root cause before product mutation.
3. Diagnosis-only: report evidence; no product edits.
4. Authorized fix: narrowest seam; add regression evidence; rerun minimized/original loops.
5. Failed fix attempt = product change + failed original repro rerun.
6. Three failed product fixes: unconditional architecture stop; no fourth.
7. Trace defect repair to declared outcome evidence; absence bars completion.
8. Remove instrumentation; state uncertainty.

Escalate interface, migration, security, dependency, permission, or external effects.
Never hide failures, pad timeouts, claim untested root cause.
