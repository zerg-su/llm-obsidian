---
name: debug
description: Diagnose reproducible bugs to root cause. Use for crashes, regressions, wrong behavior, or flakiness; fix only when authorized.
---

# Debug

Run the shortest trustworthy feedback loop:

1. Reproduce the reported behavior and record the exact failing observation.
2. Minimize the reproduction without changing its meaning.
3. Rank a small set of falsifiable hypotheses; instrument only what separates them.
4. Test hypotheses and identify the root cause, not merely a correlated symptom.
5. If the request is diagnosis-only, report evidence and stop without product edits.
6. If a fix is authorized, change the narrowest approved seam, add a regression,
   and rerun both the minimized and original feedback loops.
7. Remove temporary instrumentation and report residual uncertainty.

Escalate before changing a public interface, migration, security boundary,
dependency, permission, or external state. Never hide a failing test, widen
timeouts blindly, or claim root cause from one untested hypothesis.
