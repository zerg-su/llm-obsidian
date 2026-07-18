---
type: meta
title: "Concepts Index"
created: 2026-04-07
updated: 2026-07-11
tags:
  - meta
  - index
  - concept
domain: knowledge-management
status: evergreen
related:
  - "[[index]]"
  - "[[dashboard]]"
  - "[[Wiki Map]]"
  - "[[LLM Wiki Pattern]]"
  - "[[Hot Cache]]"
  - "[[Compounding Knowledge]]"
  - "[[DragonScale Memory]]"
sessions:
  - public-template-v2
---

# Концепты

Navigation: [[index]] | [[entities/_index|Entities]] | [[sources/_index|Sources]]

Концепты — паттерны, идеи, фреймворки. Делятся на две группы: концепты самого вольта (LLM Wiki, DragonScale) и концепты предметной области по мере наполнения.

---

## Концепты вольта (LLM Wiki / DragonScale)

- [[LLM Wiki Pattern]] — архитектура персистентной компаундящей базы знаний
- [[Hot Cache]] — ~500-словный кэш свежего контекста, обновляется после каждой операции
- [[Compounding Knowledge]] — почему вики растёт ценнее со временем, в отличие от RAG
- [[Persistent Wiki Artifact]] — Markdown-страница как объект памяти LLM
- [[Source-First Synthesis]] — провенанс: источники immutable, синтез отдельно и цитирует
- [[Query-Time Retrieval]] — query-путь синтезирует с цитатами, дополняет встроенный поиск Obsidian
- [[DragonScale Memory]] — спека memory-layer с четырьмя механизмами (fold, addresses, tiling, boundary-autoresearch)
- [[DragonScale on macOS]] — три macOS-ловушки (flock, PEP 604, BSD wc) и минимальные фиксы
- [[SVG Diagram Style Guide]] — каноничный визуальный стиль для архитектурных диаграмм
- [[cherry-picks]] — feature backlog плагина из 16+ Obsidian/Claude проектов

---

## Концепты предметной области

*(заполняется по мере того как с чем-то сталкиваюсь или исследую: eBPF, GitOps, mTLS, service mesh, observability и т.д.)*

```dataview
LIST
FROM "wiki/concepts"
WHERE type = "concept" AND !contains(tags, "llm-wiki") AND !contains(tags, "knowledge-management") AND !contains(tags, "dragonscale") AND !contains(tags, "cherry-picks") AND !contains(tags, "product-roadmap") AND !contains(tags, "llm-obsidian")
SORT title ASC
```

<!-- AUTO-INDEX START -->
_11 pages, updated 2026-07-11_

- [[Compounding Knowledge]] — mature, 2026-07-11
- [[DragonScale Memory]] — shipped, 2026-07-11 `c-000001`
- [[DragonScale on macOS]] — developing, 2026-07-11 `c-000005`
- [[Hot Cache]] — mature, 2026-07-11
- [[LLM Wiki Pattern]] — mature, 2026-07-11
- [[Persistent Wiki Artifact]] — developing, 2026-07-11 `c-000006`
- [[Query-Time Retrieval]] — developing, 2026-07-11 `c-000002`
- [[SVG Diagram Style Guide]] — evergreen, 2026-07-11
- [[Source-First Synthesis]] — developing, 2026-07-11 `c-000003`
- [[Unattended Pipeline]] — evergreen, 2026-07-11 `c-000004`
- [[cherry-picks]] — current, 2026-07-11
<!-- AUTO-INDEX END -->
