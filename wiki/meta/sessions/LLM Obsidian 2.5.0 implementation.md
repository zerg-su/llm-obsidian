---
type: session
title: "LLM Obsidian 2.5.0 implementation"
address: c-000038
status: completed
created: 2026-07-31
updated: 2026-07-31
tags:
  - session
  - release
  - harness
  - pipeline
  - v2-5
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-07-31
related:
  - "[[2026-07-30-224926-llm-obsidian-2-5-model-authored-custom-pipelines]]"
  - "[[Unattended Pipeline]]"
---

# LLM Obsidian 2.5.0 implementation

Реализован model-authored custom pipeline как строгий versioned `PipelineSpec`, который проходит deterministic built-in-fit selection, code-owned compiler и exact user approval до эффектов. Исполнение использует существующие store/FSM/supervisor: последовательные typed model steps, bounded transitions/loops, verification, review и reap.

Добавлен model-free callback liveness ladder: stable result recovery, idle evidence, один nudge, один identity-bound restart и durable attention. Dogfood осознанно закрыт как mechanism-focused gate: 10 изолированных deterministic full-runtime custom runs плюс 2 живые задачи Claude и Codex в cmux. Оба live parent operation завершены и reaped; найденные race, cross-repo review transport и verification-resubmit дефекты получили узкие regression fixes.

Release evidence: `docs/acceptance/v2.5.0-dogfood.md`; release notes: `docs/releases/v2.5.0.md`. Финальный model-free gate зелёный; независимый Fable + Sol review выполняется перед выпуском.
