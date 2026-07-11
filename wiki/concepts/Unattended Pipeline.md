---
type: concept
title: "Unattended Pipeline"
address: c-000004
aliases:
  - "Dispatch Reap Live Smoke gpt-5.5"
  - "reap"
created: 2026-07-06
updated: 2026-07-11
tags:
  - orchestration
  - cmux
  - review
  - codex
  - claude
status: evergreen
related:
  - "[[LLM Wiki Pattern]]"
  - "[[Query-Time Retrieval]]"
sessions:
  - public-template-v2
---

# Unattended Pipeline

The optional cmux pipeline runs an approved task in a separate git worktree,
routes review to the opposite model family, and returns a typed result to the
coordinator. Watchdogs only observe visible progress; blocking findings, scope
changes, security decisions, and external effects remain human-owned.

The durable contract is implemented by the supervisor, task metadata schemas,
review callbacks, reap validation, and close-on-exit lifecycle. Operational
details and diagnostics live in docs/unattended-pipeline-operations.md.
