---
type: overview
title: "Wiki Overview"
created: 2026-04-07
updated: 2026-04-26
tags:
  - meta
  - overview
status: evergreen
related:
  - "[[index]]"
  - "[[hot]]"
  - "[[log]]"
  - "[[dashboard]]"
  - "[[LLM Wiki Pattern]]"
sources:
---

# Обзор вольта

Navigation: [[index]] | [[hot]] | [[log]] | [[dashboard]]

---

## Назначение

Это демо-вольт плагина llm-obsidian (русскоязычный fork). Демонстрирует [[LLM Wiki Pattern]] — систему построения персистентных компаундящих баз знаний на стыке Claude и Obsidian.

Запустите `/wiki` чтобы заскаффолдить этот вольт под ваш домен и заменить overview под ваш use case.

---

## Текущий seed-контент

**Концепты вольта:**
- [[LLM Wiki Pattern]] — базовая архитектура
- [[Hot Cache]] — механизм session-контекста
- [[Compounding Knowledge]] — почему паттерн работает
- [[DragonScale Memory]] — спека memory-layer с четырьмя механизмами
- [[DragonScale on macOS]] — три macOS-фикса при первой установке
- [[Persistent Wiki Artifact]], [[Source-First Synthesis]], [[Query-Time Retrieval]] — теоретические концепты паттерна
- [[Wiki vs RAG]] — почему вики выигрывает у RAG на масштабе <1000 страниц
- [[SVG Diagram Style Guide]] — стиль для диаграмм
- [[cherry-picks]] — feature backlog для эволюции плагина

**Сущности:**
- [[Andrej Karpathy]] — автор паттерна

**Источники:**
- *(пусто, заполняется при первом ингесте)*

---

## Текущее состояние

- Источники ингестнутые: 0
- Wiki-страниц: ~12 (seed)
- Готов к scaffolding под ваш use case

---

## Ключевые темы

**Знания накапливаются.** В отличие от RAG, вики pre-compile синтез. Cross-references уже на месте. Противоречия флагуются. Каждый ингест обогащает существующие страницы вместо добавления изолированных chunks.

**Hot cache это force multiplier.** ~500-словный файл захватывает свежий контекст. Новые сессии стартуют с полным контекстом за минимальную token-стоимость.

**Obsidian это IDE, Claude это программист.** Graph view показывает что связано. Человек курирует источники и задаёт вопросы. Claude пишет и поддерживает всё остальное.
