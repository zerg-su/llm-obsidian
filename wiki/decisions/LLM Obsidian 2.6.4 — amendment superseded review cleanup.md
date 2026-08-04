---
type: decision
title: "LLM Obsidian 2.6.4 — amendment superseded review cleanup"
address: c-000109
status: accepted
created: 2026-08-04
updated: 2026-08-04
tags:
  - decision
  - llm-obsidian
  - v2-6-4
  - harness
  - lifecycle
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-04
related:
  - "[[2026-08-04-112015-llm-obsidian-2-6-4-unattended-callback-submit-watchdog|LLM Obsidian 2.6.4 — unattended callback-submit watchdog]]"
  - "[[LLM Obsidian 2.6.4 — amendment plan-review outcome]]"
---

# LLM Obsidian 2.6.4 — amendment superseded review cleanup

## Решение

Пользователь подтвердил, что superseded review sessions не должны оставаться открытыми после перехода к новой exact boundary. Наблюдаемый инцидент: пять старых provider surfaces сохранились после двух `awaiting-resolution` boundaries и одного ошибочного implementation review; повторно использовать их было уже нельзя.

В 2.6.4 включается D-264-13 и новое evidence:

`E13-superseded-review-cleanup`: после durable callback/finding/resolution receipts terminal или явно superseded review boundary автоматически закрывает только exact-owned provider process/surface и reconcile доводит parent до resource-free terminal state; active/current boundary и unknown ownership никогда не закрываются; cleanup failure становится typed attention. Повторный cleanup идемпотентен и не удаляет исторические evidence bytes.

## Ограничения

- Никаких широких `cmux kill`, process globs или ручного store edit.
- Supersession должна быть identity-bound к новому authorized boundary, а не выводиться по возрасту окна.
- Callback, finding, resolution, telemetry и review archive сохраняются до close effect.
- Cleanup не запускает provider, review, verification или callback повторно.

Это coordinator-owned amendment к Outcome digest `83a8f81c78ac13a276e106cf33cd29d0300d0ca5f3e0307049183f53d493910d`. Новые contract bytes требуют fresh Opus intent boundary; текущий reviewer старых bytes сохраняется только как provenance.

