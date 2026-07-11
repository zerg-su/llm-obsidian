---
type: concept
address: c-000006
title: "Persistent Wiki Artifact"
created: 2026-04-24
updated: 2026-07-11
tags:
  - llm-wiki
  - knowledge-management
  - agent-memory
status: developing
related:
  - "[[LLM Wiki Pattern]]"
  - "[[Compounding Knowledge]]"
  - "[[Source-First Synthesis]]"
  - "[[Query-Time Retrieval]]"
sessions:
  - public-template-v2
---

# Persistent Wiki Artifact

Persistent wiki artifact это поддерживаемый Markdown-слой между сырыми источниками и будущими вопросами. В описании LLM Wiki от Карпаты, агент читает source-материал, извлекает ключевую информацию, интегрирует в interconnected вики вместо того чтобы только доставать chunks на момент ответа: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

## Что фиксирует эта концепция

LLM Wiki компаундит знания, но это не делает явной артефакт как единицу памяти. Эта страница делает границу явной: память хранится в файлах, которые можно просматривать, связывать, ревьюить, ревизировать.

## Извлечённые claims

- LLM Wiki pattern определяет сырые источники, сгенерированную вики, schema-документ как отдельные слои: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- В этом паттерне коллекция сырых источников рассматривается как immutable, а wiki-слой owned и поддерживается LLM: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- Паттерн фреймит вики как компаундящий артефакт, чьи cross-references, contradiction-флаги, синтез persist через будущие вопросы: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- Obsidian поддерживает wikilinks вида `[[Three laws of motion]]`, что позволяет Markdown-файлам формировать internal-сеть заметок: https://obsidian.md/help/links
- Obsidian может автоматически обновлять internal-ссылки при переименовании файла, в зависимости от настройки вольта: https://obsidian.md/help/links

## Импликации для этого вольта

- Durable memory-объект это страница, не chat-turn.
- Странице нужны frontmatter, стабильный заголовок, wikilinks, source URL'ы чтобы будущие агенты могли инспектировать провенанс.
- Страница должна оставаться достаточно компактной чтобы её можно было ревизировать напрямую, потому что LLM Wiki pattern зависит от обновления существующего синтеза при поступлении новых источников: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

## Primary Sources

- https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- https://obsidian.md/help/links
