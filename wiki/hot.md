---
type: meta
title: "Hot Cache"
created: 2026-07-05
updated: 2026-07-18
tags:
  - meta
  - hot-cache
status: evergreen
related:
  - "[[index]]"
  - "[[log]]"
  - "[[overview]]"
  - "[[getting-started]]"
sessions:
  - "public-template-v2"
---

# Hot Cache

Recent-context кэш, грузится SessionStart-хуком. **Это кэш, не журнал.** Капы (≤800 слов, Recent Changes ≤15 × 160 chars, Active Threads ≤8, нарратив ≤120 слов) enforce'ит `scripts/vault-write.py` — скиллы пишут сюда ТОЛЬКО через него, прямые Edit'ы запрещены. Полная история — [[log]] и сами страницы.

Navigation: [[index]] | [[log]] | [[overview]]

## Last Updated

Шаблон обновлён до llm-obsidian v2.0.0: Claude Code и Codex используют общий безопасный write/retrieval pipeline, а cmux-оркестрация остаётся опциональной.

## Key Recent Facts

- Вольт создан из шаблона llm-obsidian; структура папок описана в [[index]].
- Запись log/hot идёт через `scripts/vault-write.py` (single-pass payload, детерминированные капы).
- Retrieval: `semantic-search.py --hybrid` (ollama bge-m3 + BM25, scope-aware fusion); свежесть кэшей держит Stop-хук.

## Recent Changes

- 2026-07-18: [[LLM Obsidian v2.0.8 RD upstream audit]] — подготовлен и проверен Fable/high локальный релиз-кандидат v2.0.8 (`c-000010`)
- 2026-07-11: [[Unattended Pipeline]] — public v2 pipeline and strict contracts (c-000004)
- 2026-07-06: [[Dispatch Reap Live Smoke gpt-5.5]] - live Codex gpt-5.5 dispatch/reap-send smoke filed (`c-000004`)
- 2026-07-05: вольт инициализирован из шаблона llm-obsidian v1.0.0

## Active Threads

- [open] пройти [[getting-started]] и настроить вольт под себя (ollama, MCP-гейтвей, первые ингесты)
