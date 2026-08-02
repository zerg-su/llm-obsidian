---
name: debug
description: Use for reproducible bug root causes; fix only when authorized.
---

# Debug

Deterministic repro command = red evidence.
Otherwise: red-capable loop before hypotheses; record evidence gap; stop speculative fix.

1. Reproduce/record failure; minimize without changing meaning.
2. Rank falsifiable hypotheses by evidence/cost; change one variable per probe.
   Prove repro-backed root cause before product mutation.
3. diagnosis-only: stop without product edits.
4. Authorized fix: add regression evidence at the seam that exercises the real
   bug pattern; rerun minimized/original loops. If no correct seam exists,
   record that missing seam as an architecture finding/evidence gap instead of
   substituting a shallow test, and feed it to the architecture-stop path.
5. Failed fix attempt = product change + failed original repro rerun.
6. Three failed fixes: unconditional architecture stop; no fourth.
7. Defect repair needs declared outcome evidence; absence bars completion.
8. Remove temporary instrumentation; rerun original feedback loops; report residual uncertainty.

Escalate interfaces, migrations, security, dependencies, permissions, external effects.
Never hide failure, pad timeouts, or claim untested root cause.
