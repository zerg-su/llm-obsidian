---
name: clarify
description: >-
  Clarify before code. Triggers: clarify, grill me.
---

# Clarify

## Interview loop

1. Inspect the codebase first for local facts. Keep this inspection read-only.
2. Pick the unresolved decision that constrains the most later choices. Ask
   exactly one question, explain why it matters, and recommend an answer. Use
   the interactive question tool when available, otherwise text.
3. Wait. Material decisions belong to the user; never choose one silently.
4. Retain agreed terms, invariants, contradictions, edge cases, and ADR
   candidates in context. Brainstorm or model the domain only when real
   ambiguity requires it.
5. Repeat without a question limit until requirements, constraints, acceptance
   criteria, edge cases, and important branches are resolved.

## Alignment gate

During the interview, do not edit files, write code or a final plan, run
implementation commands, enact the proposal, or bundle questions.

Before closing alignment, confirm that `desired_outcome`, `success_evidence`,
and `non_goals` are materially unambiguous; `purpose` is optional. If any
required field is still materially ambiguous, keep the interview open with the
one-question loop.

After the user confirms alignment or requests planning/implementation, form
exactly one user-grounded Outcome Contract with `desired_outcome`,
`success_evidence`, `non_goals`, and optional `purpose`. Summarize the agreed
facts and approach. Do not infer or invent a goal, evidence, or non-goal.

If no next action is authorized, ask which handoff they want; otherwise do not
ask again. For unresolved architecture/domain boundaries, hand agreed facts to
`design` instead of drafting alternatives here.
