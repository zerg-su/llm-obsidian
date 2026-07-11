---
type: comparison
title: "Wiki vs RAG"
address: c-000007
created: 2026-07-11
updated: 2026-07-11
tags:
  - llm-wiki
  - rag
  - retrieval
  - knowledge-management
status: mature
related:
  - "[[LLM Wiki Pattern]]"
  - "[[Query-Time Retrieval]]"
  - "[[Persistent Wiki Artifact]]"
  - "[[Source-First Synthesis]]"
  - "[[Compounding Knowledge]]"
sessions:
  - public-template-v2
---

# Wiki vs RAG

Wiki и RAG решают разные части одной задачи. RAG находит релевантные фрагменты
во время запроса; LLM-wiki заранее поддерживает связанный и ревьюируемый слой
синтеза. В этом vault wiki хранит устойчивое понимание, а retrieval быстро
выбирает нужную страницу или секцию.

| Критерий | LLM-wiki | Классический RAG |
| --- | --- | --- |
| Единица памяти | Поддерживаемая Markdown-страница | Фрагмент исходного документа |
| Синтез | При сохранении и обновлении знания | Обычно во время ответа |
| Связи | Явные wikilinks | Зависят от найденного контекста |
| Ревью | Обычные файлы в Obsidian и Git | Часто требует отдельного интерфейса |

Для небольшого human-scale vault каноническим слоем остаются страницы:
[[Persistent Wiki Artifact]] сохраняет проверяемый вывод, а
[[Query-Time Retrieval]] помогает найти его без подмены долговечной памяти
непрозрачной выдачей фрагментов.
