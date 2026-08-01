---
name: debug
description: Use for reproducible bug root causes; fix only when authorized.
---

# Debug

Deterministic repro command = red evidence.
Otherwise: red-capable loop before hypotheses; record evidence gap; stop speculative fix.

1. Reproduce/record failure; minimize without changing meaning.
2. Falsify; prove repro-backed root cause before product mutation.
3. diagnosis-only: stop without product edits.
4. Authorized fix: add regression evidence; rerun minimized/original loops.
5. Failed fix attempt = product change + failed original repro rerun.
6. Three failed fixes: unconditional architecture stop; no fourth.
7. Defect repair needs declared outcome evidence; absence bars completion.
8. Remove temporary instrumentation; rerun original feedback loops; report residual uncertainty.

Escalate interfaces, migrations, security, dependencies, permissions, external effects.
Never hide failure, pad timeouts, or claim untested root cause.
