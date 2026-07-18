---
name: clarify
description: >-
  Clarify before code. Triggers: clarify, grill me.
---

# Clarify

Interview the user until requirements, constraints, edge cases, acceptance
criteria, and important branches of the decision tree are resolved.

## Interview loop

1. Inspect the codebase first for facts available locally. Keep this inspection
   read-only.
2. Select the unresolved decision whose answer constrains the most later
   decisions.
3. Ask exactly one question, briefly explain why it matters, and recommend an
   answer. Use the runtime's interactive question tool when available;
   otherwise ask in plain text. The one-question rule applies in both cases.
4. Wait for the answer. Material decisions belong to the user; do not silently
   choose a material tradeoff for them.
5. Use the answer to choose the next branch. Continue until shared
   understanding is reached; do not impose an arbitrary question limit.

## Alignment gate

During the interview:

- Do not write code or edit files.
- Do not create a final implementation plan.
- Do not run implementation commands or enact the proposal.
- Do not bundle several questions into one message.

When the user confirms shared understanding or asks to start planning or
implementation, summarize the agreed requirements, constraints, edge cases,
acceptance criteria, and approach. If they confirmed alignment without
authorizing a next action, ask which handoff they want. If they already
authorized planning or implementation, do not ask for redundant confirmation.
