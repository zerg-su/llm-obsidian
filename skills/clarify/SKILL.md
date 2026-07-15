---
name: clarify
description: >-
  Clarify before code. Triggers: clarify, grill me.
---

# Clarify

Interview the user until the requirements, constraints, edge cases, acceptance
criteria, and important branches of the decision tree are resolved.

## Interview loop

1. Inspect the codebase first for facts that can be discovered locally. Keep
   this inspection read-only.
2. Select the next unresolved decision whose answer constrains the most later
   decisions.
3. Ask exactly one question. Explain briefly why it matters and give your
   recommended answer.
   Use the runtime's interactive question tool when available; otherwise ask
   in plain text. The one-question rule applies in both cases.
4. Wait for the user's answer before continuing. Decisions belong to the user;
   do not silently choose a material tradeoff for them.
5. Use each answer to choose the next branch. Continue until shared
   understanding is reached; do not impose an arbitrary question limit.

## Alignment gate

During the interview:

- Do not write code or edit files.
- Do not create a final implementation plan.
- Do not run implementation commands or enact the proposal.
- Do not bundle several questions into one message.

Only after the user explicitly says `we are aligned` or `start implementation`,
summarize the agreed requirements, constraints, edge cases, acceptance criteria,
and proposed implementation approach. Then ask for confirmation before coding.
