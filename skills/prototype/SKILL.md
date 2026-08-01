---
name: prototype
description: Answer one technical question in a disposable worktree. Use for bounded, non-production spikes.
---

# Prototype

Ask one falsifiable question with a success/failure criterion in a harness-owned disposable
worktree/minimal ContextPacket.
Answer with one run command/bounded evidence; production code remains
unchanged. No production tests/polish/compatibility/extras.

Durably record `Question`, `Evidence`, `Decision`, `Limitations`, `Provenance`
and how it informs the incoming Outcome Contract. A successful
spike proves neither desired outcome nor production completion.
Keep off main. Clean only its exact worktree after decision is durably captured
and idle. Unknown ownership -> `attention-required`; never guess/clean broadly.
Promotion requires separate authorization.
