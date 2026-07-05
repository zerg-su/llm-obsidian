---
type: meta
title: "Hot Cache"
created: 2026-07-05
updated: 2026-07-05
tags:
  - meta
  - hot-cache
status: evergreen
related:
  - "[[index]]"
  - "[[log]]"
  - "[[overview]]"
  - "[[getting-started]]"
sessions: []
---

# Hot Cache

Recent-context кэш, грузится SessionStart-хуком. **Это кэш, не журнал.** Капы (≤800 слов, Recent Changes ≤15 × 160 chars, Active Threads ≤8, нарратив ≤120 слов) enforce'ит `scripts/vault-write.py` — скиллы пишут сюда ТОЛЬКО через него, прямые Edit'ы запрещены. Полная история — [[log]] и сами страницы.

Navigation: [[index]] | [[log]] | [[overview]]

## Last Updated

Свежий вольт. Начни с [[getting-started]], потом `/wiki` для бутстрапа под себя. Первая запись появится здесь после первого `/save`.

## Key Recent Facts

- Вольт создан из шаблона llm-obsidian; структура папок описана в [[index]].
- Запись log/hot идёт через `scripts/vault-write.py` (single-pass payload, детерминированные капы).
- Retrieval: `semantic-search.py --hybrid` (ollama bge-m3 + BM25, scope-aware fusion); свежесть кэшей держит Stop-хук.

## Recent Changes

- 2026-07-05: вольт инициализирован из шаблона llm-obsidian v1.0.0

## Active Threads

- [open] пройти [[getting-started]] и настроить вольт под себя (ollama, MCP-гейтвей, первые ингесты)
