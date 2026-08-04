---
type: decision
title: "LLM Obsidian 2.6.4 — amendment Harness control plane"
address: c-000110
status: accepted
created: 2026-08-04
updated: 2026-08-04
tags:
  - decision
  - llm-obsidian
  - v2-6-4
  - harness
  - outcome-contract
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-04
related:
  - "[[2026-08-04-112015-llm-obsidian-2-6-4-unattended-callback-submit-watchdog|LLM Obsidian 2.6.4 — unattended callback-submit watchdog]]"
  - "[[LLM Obsidian 2.6.4 — amendment superseded review cleanup]]"
---

# LLM Obsidian 2.6.4 — amendment Harness control plane

## Decision

Пользователь уточнил архитектурный контракт: Harness является control plane всего исполнения уже выбранного плана — steps, loops, review, verification, bounded fix/retry, checkpoints, callbacks и cleanup. LLM производит содержательные typed artifacts и не управляет lifecycle. Semantic task decomposition и PipelineSpec parallel/join остаются 2.7/out of scope.

E14 добавляется как отдельный regression-and-gap-closure outcome. Stage-by-stage baseline в Slice 0 отделяет уже harness-owned stages от реальных manual/prose gaps; Slice 5b имеет право менять только подтверждённые red owners и не меняет DSL/scheduler/public FSM.

## Digest binding

- amended Outcome digest before E14: `35fe25cdc82c844121d7f7200f6ba6926727317f645a51e8320fc5ceea668074`;
- unapproved proposal at HEAD `6830062dbde914e65e58dc2b567e489555ee65fd`: Outcome `1c64c2a373d2401f0ba8d25c14316e42ba2f8e4ea87121f1716419926633ac15`, plan `8e2654629b4a8c57f6e94a8eef56df65eb12dfc3aa6200bc0fe9fd3ffaa12e6a`;
- corrected resulting Outcome digest: `af24873e06669632c5c45e9818a8646918e3a086a358285d1781a1a5540660ae`;
- corrected resulting plan digest: `2bcd5d57960a11afc9218f02acd20623b8d82e0c12c1fb3a2e0ae05e3b07745c`;
- amendment-introducing commit: `b204289f584a684e34d5036b20eb417d70a18659`.

Prior callbacks/findings are provenance only and do not approve these bytes. Approval requires a fresh exact Opus intent boundary.
