---
type: meta
title: "Hot Cache"
created: 2026-07-05
updated: 2026-08-05
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

- 2026-08-05: [[LLM Obsidian 2.6.5 Subplan B provider events and delivery]] — finalized task result (`c-000124`)
- 2026-08-04: [[LLM Obsidian 2.6.4 Subplan D result]] — finalized task result (`c-000116`)
- 2026-08-04: [[LLM Obsidian 2.6.3 Russian technical documentation]] — finalized task result (`c-000105`)
- 2026-08-04: [[LLM Obsidian 2.6.3 — MCP runtime bootstrap authorization]] — bounded default runtime.env bootstrap (`c-000103`)
- 2026-08-04: [[LLM Obsidian 2.6.3 — manual parallel task documentation amendment]] — handbook получает manual parallel task workflow без обещания… (`c-000102`)
- 2026-08-04: [[LLM Obsidian 2.6.3 — review recovery inclusion disposition]] — D-264-06 и D-264-08 включены в 2.6.3 (`c-000101`)
- 2026-08-02: [[LLM Obsidian 2.6 paired design rollback validation]] — finalized task result (`c-000094`)
- 2026-08-02: [[LLM Obsidian 2.6 common dogfood fixes]] — finalized task result (`c-000092`)
- 2026-08-02: [[LLM Obsidian 2.6 dogfood RT2 fresh review packet identity]] — finalized task result (`c-000090`)
- 2026-08-02: [[LLM Obsidian 2.6 dogfood RT1 callback watchdog architecture]] — finalized task result (`c-000088`)
- 2026-08-02: [[LLM Obsidian 2.6 dogfood RT4 callback fallback prototype]] — finalized task result (`c-000086`)
- 2026-08-02: [[LLM Obsidian 2.6 paired fix post-change final]] — finalized task result (`c-000084`)
- 2026-08-01: [[LLM Obsidian 2.6 skill workstream A]] — finalized task result (`c-000078`)
- 2026-08-01: [[LLM Obsidian 2.6 skill workstream C]] — finalized task result (`c-000076`)
- 2026-08-01: [[LLM Obsidian 2.6 paired design baseline]] — finalized task result (`c-000074`)

## Active Threads

- [open] пройти [[getting-started]] и настроить вольт под себя (ollama, MCP-гейтвей, первые ингесты)
