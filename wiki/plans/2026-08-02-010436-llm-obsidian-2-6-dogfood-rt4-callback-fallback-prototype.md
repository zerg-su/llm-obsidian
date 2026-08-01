---
type: plan
title: "LLM Obsidian 2.6 dogfood RT4 — code-owned callback fallback prototype"
address: c-000082
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-02
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-02
source_cwd: "/Users/zak/Projects/llm-obsidian"
status: executed
created: 2026-08-02
updated: 2026-08-02
tags:
  - plan
  - manual-save
  - dogfood
  - v2.6
---

# LLM Obsidian 2.6 dogfood RT4 — code-owned callback fallback prototype

## Outcome Contract

```json
{"schema_version":1,"purpose":"Проверить prototype skill и честный partially-achieved disposition на низкозатратном резервном liveness механизме.","desired_outcome":"Disposable prototype доказывает, что coordinator может без model call отличить живую долгую операцию, завершившегося без callback provider и потерянный callback, а затем выдать один безопасный resume/reconcile сигнал через существующий harness.","success_evidence":[{"evidence_id":"rt4-signal","observable":"Disposable code или replay fixture классифицирует минимум три состояния: live-progress, exited-no-callback и durable-callback-pending-ingestion."},{"evidence_id":"rt4-no-model-poll","observable":"Прототип не запускает модель, не повторяет provider effect и использует только существующие durable/runtime signals."},{"evidence_id":"rt4-portability","observable":"Результат показывает, какие сигналы доступны одинаково для Codex и Claude/cmux либо явно фиксирует непокрытую runtime-разницу."},{"evidence_id":"rt4-durable-result","observable":"Durable report содержит вопрос, evidence, решение, ограничения, provenance и честный achieved или partially-achieved disposition."}],"non_goals":["Перенос disposable prototype в production в этой задаче.","Периодические model prompts или token-consuming status checks.","Новый scheduler, daemon или cmux API."]}
```

## Контекст

Это исследовательская real-task проверка: несколько локальных подшагов могут быть зелёными, но если переносимый сигнал для обоих runtime не доказан, задача обязана завершиться `partially-achieved`, а не ложным completion.

## Работа

1. Через `prototype` сформулировать один вопрос: достаточно ли уже сохранённых OperationStore, supervisor/process identity, callback outbox/receipt и cmux surface state для code-owned fallback.
2. Создать disposable fixture вне production modules либо использовать существующие deterministic fixtures; production code не менять.
3. Проверить три состояния и отсутствие повторного provider effect.
4. Сохранить результат в `docs/acceptance/v2.6-dogfood-rt4-callback-fallback-prototype.md` с evidence, decision, limitations и provenance.
5. Пройти simple review и записать residual gap, если cross-runtime portability не доказана.

## Проверка

- Production diff отсутствует.
- Прототип/fixture воспроизводим одной локальной командой.
- Отчёт не заявляет больше, чем показывает evidence.
- Если хотя бы один обязательный evidence ID missing, Wiki Summary v2 использует `partially-achieved`.

Результат: [[LLM Obsidian 2.6 dogfood RT4 callback fallback prototype]] (reaped 2026-08-02)
