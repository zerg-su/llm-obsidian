---
type: concept
address: c-000003
title: "Source-First Synthesis"
created: 2026-04-24
updated: 2026-07-11
tags:
  - llm-wiki
  - synthesis
  - provenance
status: developing
related:
  - "[[LLM Wiki Pattern]]"
  - "[[Compounding Knowledge]]"
  - "[[Persistent Wiki Artifact]]"
  - "[[Query-Time Retrieval]]"
sessions:
  - public-template-v2
---

# Source-First Synthesis

Source-first synthesis это практика LLM Wiki: держать сырые источники отдельно от сгенерированной вики, требуя при этом что вики цитирует и интегрирует эти источники. Паттерн Карпаты описывает сырые источники как source of truth и сгенерированную вики как поддерживаемый synthesis-слой: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

## Что фиксирует эта концепция

LLM Wiki интегрирует источники, но не прописывает discipline провенанса явно. Эта страница записывает правило: синтез разрешено перепи­сывать, но source-материал остаётся cited-якорем.

## Извлечённые claims

- LLM Wiki pattern Карпаты говорит, что сырые источники могут включать статьи, papers, картинки, data-файлы, и LLM читает их не модифицируя: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- Тот же источник описывает вики как summary, entity-страницы, concept-страницы, сравнения, overview, синтез поддерживаемые LLM: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- Ingest-операция может создавать source-summary, обновлять index'ы, обновлять релевантные entity и concept-страницы, добавлять log-запись: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- Query-операция читает релевантные wiki-страницы и синтезирует ответы с цитатами, и полезные ответы могут возвращаться в вики: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- RAG paper идентифицирует провенанс и обновление world-knowledge как открытые проблемы для knowledge-intensive generation систем: https://arxiv.org/abs/2005.11401

## Operating Rule

Source-first synthesis строже чем unsourced summarization. Новая concept-страница должна идентифицировать использованные источники, явно указывать что было из них извлечено, не трактовать сгенерированную страницу как замену source-документу.

## Primary Sources

- https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- https://arxiv.org/abs/2005.11401
