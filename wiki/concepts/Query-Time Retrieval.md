---
type: concept
address: c-000002
title: "Query-Time Retrieval"
created: 2026-04-24
updated: 2026-07-11
tags:
  - rag
  - retrieval
  - llm-wiki
status: developing
related:
  - "[[Wiki vs RAG]]"
  - "[[LLM Wiki Pattern]]"
  - "[[Persistent Wiki Artifact]]"
  - "[[Source-First Synthesis]]"
sessions:
  - public-template-v2
---

# Query-Time Retrieval

Query-time retrieval это baseline memory-паттерн, с которым контрастирует LLM Wiki: релевантный материал достаётся когда пользователь задаёт вопрос, и ответ генерируется из retrieved-контекста.

## Что фиксирует эта концепция

LLM Wiki vs RAG контрастируется через накопление, но не определяет retrieval-сторону точно. Эта страница якорит контраст в оригинальном RAG-paper и в LLM Wiki gist'е.

## Извлечённые claims

- RAG-paper определяет retrieval-augmented generation как комбинирование parametric-памяти с non-parametric-памятью для language-generation: https://arxiv.org/abs/2005.11401
- RAG-paper описывает non-parametric-память как dense-vector-индекс Википедии, доступный neural-retriever'ом: https://arxiv.org/abs/2005.11401
- Paper репортит что RAG-модели генерировали более specific, diverse, и factual язык по сравнению с parametric-only seq2seq baseline в evaluated-generation задачах: https://arxiv.org/abs/2005.11401
- LLM Wiki gist Карпаты описывает обычные document-workflow'ы как uploading файлов, retrieving релевантных chunks на момент запроса, генерация ответа: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- LLM Wiki gist Карпаты говорит что этот query-time паттерн заставляет модель re-discover и assemble знания на каждый вопрос вместо накопления синтеза: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- MemGPT paper фреймит ограниченные LLM-контекст-окна как ограничение для extended-conversations и document-analysis, затем предлагает virtual-context management через memory-tiers: https://arxiv.org/abs/2310.08560

## Контраст с wiki-памятью

Query-time retrieval может предоставить external evidence на момент ответа. LLM Wiki pattern сдвигает часть работы раньше: компилирует source-материал в поддерживаемые страницы до прихода будущих запросов: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

## Primary Sources

- https://arxiv.org/abs/2005.11401
- https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- https://arxiv.org/abs/2310.08560
