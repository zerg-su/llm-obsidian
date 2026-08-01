---
name: prototype
description: Answer one technical question in a disposable worktree. Use for bounded, non-production spikes.
---

# Prototype

State one falsifiable question and a success/failure criterion. Ask the harness
to create an owned disposable worktree and minimal ContextPacket.

Implement only enough to answer the question; production code remains
unchanged. Provide one run command and capture bounded evidence. Do not add
production tests, polish, compatibility layers, or unrelated features.

Durably record:

- `Question`: the exact technical question and success/failure criterion;
- `Evidence`: the command and bounded observation that answers it;
- `Decision`: the conclusion supported by that evidence;
- `Limitations`: what the spike did not establish and remaining uncertainty;
- `Provenance`: the ContextPacket, owned worktree, base revision, and relevant
  runtime or tool versions.

State how this local answer informs the incoming Outcome Contract. A successful
spike does not establish the desired outcome or claim production completion.

The artifact remains off main. Harness cleanup may remove only the exact owned
worktree after the decision is durably captured and no process uses it.
Unknown ownership becomes `attention-required`; never guess or run broad
worktree cleanup. Promotion into production requires separate authorization.
