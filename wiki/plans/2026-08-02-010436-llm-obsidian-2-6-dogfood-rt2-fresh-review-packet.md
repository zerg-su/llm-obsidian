---
type: plan
title: "LLM Obsidian 2.6 dogfood RT2 — fresh review packet identity"
address: c-000080
session_id: 019fab00-3160-7380-8920-4b20183afb76
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-02
source_cwd: "/Users/zak/Projects/llm-obsidian"
status: pending
created: 2026-08-02
updated: 2026-08-02
tags:
  - plan
  - manual-save
  - dogfood
  - v2.6
---

# LLM Obsidian 2.6 dogfood RT2 — fresh review packet identity

## Outcome Contract

```json
{"schema_version":1,"purpose":"Проверить debug/TDD на наблюдённом lifecycle-дефекте, где свежая review boundary могла оставить устаревший materialized packet.","desired_outcome":"После разрешённой fresh review boundary файл .task-review.json в task worktree всегда представляет текущую принятую review operation и её findings; packet предыдущей boundary не может быть принят, показан executor или использован для resolution нового review.","success_evidence":[{"evidence_id":"rt2-red-repro","observable":"Одна детерминированная regression-команда на pre-fix состоянии демонстрирует stale packet или доказывает, что исходный симптом уже устранён конкретным существующим механизмом."},{"evidence_id":"rt2-identity-binding","observable":"Тест связывает materialized packet с точными operation, callback и HEAD identities текущей fresh boundary."},{"evidence_id":"rt2-stale-rejection","observable":"Повторно воспроизведённый старый packet отклоняется или заменяется до executor resolution intake."},{"evidence_id":"rt2-suite","observable":"Focused review-gate/runtime-task-summary tests и полный затронутый harness suite проходят вместе."}],"non_goals":["Изменение review iteration budgets или severity semantics.","Создание новой review surface или повтор provider effect.","Миграция исторических v1-v3 review artifacts."]}
```

## Контекст

Во время 2.6 dogfood наблюдалась ситуация: fresh review gate содержал новый finding, а materialized `.task-review.json` в worktree ещё показывал findings предыдущей boundary. Нужно сначала доказать, сохраняется ли дефект на текущем HEAD.

## Работа

1. Использовать `debug`: построить минимальный deterministic repro на текущих `ReviewGateController`, callback materialization и runtime task-summary seams.
2. Если текущий код уже устраняет симптом, не придумывать fix: связать доказательство с существующим commit/test и завершить как `achieved` без product mutation.
3. Если red воспроизводится, установить root cause до изменения продукта.
4. Использовать `tdd`: показать red на сохранённом base, внести минимальный fix, показать green на исходном repro и focused suite.
5. Пройти simple review; не расширять задачу до общей переработки review lifecycle.

## Проверка

- `python3 tests/harness/test_review_gate.py`
- `python3 tests/harness/test_runtime_task_summary.py`
- соответствующий новый focused regression
- `git diff --check`

