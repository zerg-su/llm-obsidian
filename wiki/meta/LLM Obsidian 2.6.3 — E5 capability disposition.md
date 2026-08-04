---
type: decision
title: "LLM Obsidian 2.6.3 — E5 capability disposition"
address: c-000100
created: 2026-08-04
updated: 2026-08-04
tags:
  - llm-obsidian
  - v2-6-3
  - documentation
  - capability-decision
status: active
decision_date: 2026-08-04
sessions:
  - id: 019fab00-3160-7380-8920-4b20183afb76
    date: 2026-08-04
related:
  - "[[2026-08-04-044240-llm-obsidian-2-6-3-russkaya-tekhnicheskaya-dokumentatsiya|LLM Obsidian 2.6.3 — русская техническая документация]]"
---

# LLM Obsidian 2.6.3 — E5 capability disposition

## Решение

Coordinator decision `e81aaee1-3196-4350-af9f-efb352b8d696` устанавливает авторитетную достигнутую форму `E5-document-project-skill` для frozen плана 2.6.3.

Fresh no-skill baseline прошёл все 4 обязательных сценария и воспроизвёл 0 failures при заранее утверждённом пороге не менее 3 из 4. Поэтому новый `document-project` skill не имеет доказанного поведенческого основания. Согласно stop condition плана candidate, его registry/DSL registrations и временное расширение skill budget удаляются, synthetic RED запрещён, а E5 получает typed disposition `not-adopted-per-stop-condition`.

Это не отказ от качества документации: capability matrix, quality/page contracts, dependency order понятий, runnable examples и outcome-preservation остаются обязательными и применяются через существующие `implementation-plan`, `tdd`, `improve-skills` и `review`.

## Связь с frozen планом

Approved plan остаётся байт-в-байт неизменным:

- plan SHA-256: `db4037cac1967b0907dbf1b6fd5850eefa2bfc5173080d2aa811a239fb36b8dc`;
- Outcome Contract SHA-256: `2c9728dc7c7fa3bc108ffb6ce5085bb41fcd9ba16310157e76276c6967b5bf5f`;
- dispatch operation: `8596fe76-7baa-4f73-b20c-23f33c0ba120`.

Эта страница является отдельной coordinator-owned authoritative amendment record. Она сохраняет frozen dispatch identity и разрешает противоречие между условным путём создания нового skill и уже содержащимся в плане stop condition: при недостигнутом RED threshold E5 считается установленным честным no-new-skill verdict, а не искусственной поставкой skill.

## Durable evidence

На product HEAD `f431e0959c52d89676e04688128ad00188e32dbc` решение подтверждают:

- `docs/acceptance/v2.6.3-document-project-skill-verdicts.json`;
- `docs/acceptance/v2.6.3-document-project-skill-audit.md`;
- `docs/acceptance/v2.6.3-document-project-skill-pressure.md`;
- `docs/acceptance/v2.6.3-documentation-baseline.md`;
- `docs/acceptance/v2.6.3-release-readiness.md`;
- regression assertion в `tests/test_russian_documentation.py`.

## Границы

- Не создавать и не восстанавливать `document-project` ради формального GREEN.
- Не менять frozen plan или Outcome Contract bytes после dispatch approval.
- Не считать E5 доказательством существования нового skill; доказательство — typed capability decision и отсутствие недоказанного расширения.
- Любое будущее создание отдельного documentation skill требует нового baseline gap, нового Outcome Contract и независимого forward-test.
