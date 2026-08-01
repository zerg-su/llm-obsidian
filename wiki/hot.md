---
type: meta
title: "Hot Cache"
created: 2026-07-05
updated: 2026-08-02
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

- 2026-08-02: [[LLM Obsidian 2.6 paired fix post-change final]] — finalized task result (`c-000084`)
- 2026-08-01: [[LLM Obsidian 2.6 skill workstream A]] — finalized task result (`c-000078`)
- 2026-08-01: [[LLM Obsidian 2.6 skill workstream C]] — finalized task result (`c-000076`)
- 2026-08-01: [[LLM Obsidian 2.6 paired design baseline]] — finalized task result (`c-000074`)
- 2026-08-01: [[LLM Obsidian 2.6 paired fix baseline]] — finalized task result (`c-000072`)
- 2026-08-01: [[LLM Obsidian 2.6 upstream live drift verification]] — finalized task result (`c-000066`)
- 2026-08-01: [[LLM Obsidian 2.6 skill quality baseline audit]] — finalized task result (`c-000064`)
- 2026-08-01: [[LLM Obsidian 2.6.0 technical foundation]] — finalized task result (`c-000061`)
- 2026-08-01: runbook [[RT10 Foundation Verification]] — RT10 foundation verification checks (`c-000060`)
- 2026-08-01: [[Superpowers vs Matt Pocock Skills]] — upstream comparison refreshed from pins; live drift unverified (`c-000058`)
- 2026-08-01: [[2.5 real dogfood RT08 upstream skills research]] — finalized task result (`c-000057`)
- 2026-08-01: [[2.5 real dogfood RT09 vault health audit]] — finalized task result (`c-000055`)
- 2026-08-01: [[2.5 real dogfood RT07 stale identity diagnostic]] — finalized task result (`c-000053`)
- 2026-08-01: [[2.5 real dogfood RT04 invalid review callbacks]] — finalized task result (`c-000051`)
- 2026-08-01: [[2.5 real dogfood RT06 runtime-neutral telemetry]] — finalized task result (`c-000049`)

## Active Threads

- [open] пройти [[getting-started]] и настроить вольт под себя (ollama, MCP-гейтвей, первые ингесты)
