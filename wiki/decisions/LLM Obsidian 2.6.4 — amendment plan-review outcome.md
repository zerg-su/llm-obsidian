---
type: decision
title: "LLM Obsidian 2.6.4 — amendment plan-review outcome"
address: c-000108
status: accepted
created: 2026-08-04
updated: 2026-08-04
tags:
  - decision
  - llm-obsidian
  - v2-6-4
  - review
  - outcome-contract
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-04
related:
  - "[[2026-08-04-112015-llm-obsidian-2-6-4-unattended-callback-submit-watchdog|LLM Obsidian 2.6.4 — unattended callback-submit watchdog]]"
---

# LLM Obsidian 2.6.4 — amendment plan-review outcome

## Решение

Пользователь явно потребовал включить в 2.6.4 защиту от дорогого запуска review плана как `implementation` и возможность проверять исправленный план в тех же reviewer lanes. Требования фиксируются как D-264-11 и D-264-12 и входят в release scope, а не откладываются.

Outcome Contract плана получает дополнительное evidence:

`E12-plan-review-lifecycle`: code-owned plan-review facade строит `purpose=intent` boundary из валидированного плана; invalid или ambiguous invocation завершается до `RuntimeSessionManager.start`; frozen Outcome, dispositions и evidence-map остаются fail-closed, а разрешённая design-only правка связывается reviewed/resolved plan digests и exact Git delta и продолжается в retained lanes без новой reviewer session.

## Уточнение E8

Amendment изменяет исходный E8 observable: удалена clause `без изменения утверждённых байтов`. Replacement rule: in-place Outcome rewrite допустим только при наличии отдельного amendment record, fresh exact boundary и нового approval; прежний callback не считается approval новых bytes. E8 сохраняет требование не терять решения: latest attention marker становится pointer-only. Все известные readers и writers мигрируют атомарно на append-only chain. Чтение старого full marker поддерживается только как legacy input с deterministic backfill; новые writers не создают full marker.

## Boundary amendment

Это coordinator-owned amendment к digest `a4a1e8e10a99a56e4d5bb2a90244ec80752e5f98b6300d0d4e8eb343e7184150`. После внесения E12 Outcome Contract re-freeze выполняется новым exact intent boundary и новым Opus approval. Старые callback и finding receipts сохраняются как provenance и не считаются approval новых contract bytes.

Intent compiler материализует Outcome, design, capability dispositions и success-evidence map как четыре отдельные digest-bound artifacts. Same-session plan resolution может rebind только design subject через typed resolution и exact delta; изменение Outcome, disposition table или evidence map требует отдельного amendment и fresh boundary.

