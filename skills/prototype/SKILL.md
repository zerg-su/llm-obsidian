---
name: prototype
description: Answer one technical question in a disposable worktree. Use for bounded, non-production spikes.
---

# Prototype

State one falsifiable question and a success/failure criterion. Ask the harness
to create an owned disposable worktree and minimal ContextPacket.

Implement only enough to answer the question. Provide one run command and
capture bounded evidence. Do not add production tests, polish, compatibility
layers, or unrelated features. Record the decision and limitations.

The artifact remains off main. Harness cleanup may remove only the exact owned
worktree after the decision is durably captured and no process uses it.
Unknown ownership becomes `attention-required`; never guess or run broad
worktree cleanup. Promotion into production requires separate authorization.
