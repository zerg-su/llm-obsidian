---
name: wiki-query
version: 1.0.0
description: >-
  Answer questions from the wiki: hot → index → pages; synthesize with citations, file good answers back. Modes: quick/standard/deep. Read-only. Triggers: что ты знаешь про, что у нас по, поищи в вики, найди в вики, what do you know about, query:, find in wiki.
allowed-tools: Read Glob Grep Bash WebSearch WebFetch AskUserQuestion
---

# wiki-query: Query the Wiki

The wiki has already done the synthesis work. Read strategically, answer precisely, and file good answers back so the knowledge compounds.

---

## Query Modes

Three depths. Choose based on question complexity + scope.

| Mode | Trigger | Reads | Token cost | Best for |
|------|---------|-------|------------|---------|
| **Quick** | `query quick: ...` or simple factual Q | hot.md + index.md only | ~1,500 | "What is X?", date lookups, quick facts |
| **Standard** | default (no flag) | hot.md + index + 3-5 pages | ~3,000 | Most questions |
| **Deep** | `query deep: ...` or "thorough", "comprehensive" | Full wiki + optional web (WebSearch/WebFetch) | ~8,000+ | "Compare A vs B across everything", synthesis, gap analysis |

---

## Quick Mode

Use when the answer is likely in the hot cache or index summary.

1. Read `wiki/hot.md`. If it answers the question, respond immediately.
2. If not, read `wiki/index.md`. Scan descriptions for the answer.
3. If found in index summary, respond and do not open any pages.
4. If not found, say "Not in quick cache. Run as standard query?"

Do not open individual wiki pages in quick mode.

---

## Standard Query Workflow

1. **Read** `wiki/hot.md` first. It may already have the answer or directly relevant context.
2. **Read** `wiki/index.md` to find the most relevant pages (scan for titles and descriptions).
3. **Tag prefilter**: `./scripts/tag-search.py "<question>" --top 10` — narrow candidates via the reverse tag index (frontmatter tags are the cheapest precise relevance signal) BEFORE opening pages. Pages hit by both the index scan and tag-search are read first. exit 3 (index missing) → silently skip this step.
4. **Read** those pages. Follow wikilinks to depth-2 for key entities. No deeper.
5. **Synthesize** the answer in chat. Cite sources with wikilinks: `(Source: [[Page Name]])`.
6. **Offer to file** the answer: "This analysis seems worth keeping. Should I save it as `wiki/questions/answer-name.md`?"
7. If the question reveals a **gap**: say "I don't have enough on X. Want to find a source?"

---

## Deep Mode

Use for synthesis questions, comparisons, or "tell me everything about X."

**Pre-flight (deep only):** ask scope before running (deep mode uses tokens):
- include WebSearch/WebFetch supplement? (defaults yes if wiki coverage thin)
- file result as new wiki page after? (defaults yes)

Steps:

1. Read `wiki/hot.md` and `wiki/index.md`.
2. Identify all relevant sections (concepts, entities, sources, comparisons); candidates = index scan ∪ `./scripts/tag-search.py "<question>" --top 10`.
3. Read every relevant page. No skipping.
4. If wiki coverage is thin AND user opted in — supplement with WebSearch (queries) + WebFetch (cited URLs).
5. Synthesize comprehensive answer with full citations (wikilinks for wiki pages, URLs for web sources).
6. Always file the result back as a wiki page. Deep answers are too valuable to lose.

---

## Hybrid assist (any mode)

Когда keyword-поиск (Grep/index) ничего не дал или вопрос наверняка перефразирован относительно текста страниц — используй гибридный поиск (RRF-fusion: dense-эмбеддинги tiling-кэша + sparse BM25 по всем страницам):

```bash
./scripts/semantic-search.py "<вопрос как есть>" --hybrid --top 5
```

Дальше Read топ-страниц как обычно. Заметки:

- BM25-канал (`.vault-meta/bm25/index.json`, Stop-хук перестраивает каждый turn) покрывает ВЕСЬ `wiki/` кроме `log.md`, `_templates/`, `_index.md` — включая `meta/`, `plans/`, `folds/`, которых нет в dense-канале. Grep-fallback нужен только для `log.md`.
- Деградации автоматические: ollama недоступен → bm25-only, BM25-индекса нет → dense-only. Ненулевой exit (оба канала мертвы) → молча вернуться к Grep, не считать ошибкой.
- Пометки `(dense#i, bm25#j)` показывают, какой канал поднял страницу: `bm25#-` = чисто семантический хит, `dense#-` = чисто лексический (имена, версии, ID).
- Если весь топ выглядит нерелевантным — ответа в вики скорее всего нет; проверь memory (MEMORY.md) и предложи `/investigate`.

---

## Token Discipline

Read the minimum needed:

| Start with | Cost (approx) | When to stop |
|------------|---------------|--------------|
| hot.md | ~500 tokens | If it has the answer |
| index.md | ~1000 tokens | If you can identify 3-5 relevant pages |
| 3-5 wiki pages | ~300 tokens each | Usually sufficient |
| 10+ wiki pages | expensive | Only for synthesis across the entire wiki |

If hot.md has the answer, respond without reading further.

---

## Index Format Reference

The master index (`wiki/index.md`) looks like:

```markdown
## Domains
- [[Domain Name]]: description (N sources)

## Entities
- [[Entity Name]]: role (first: [[Source]])

## Concepts
- [[Concept Name]]: definition (status: developing)

## Sources
- [[Source Title]]: author, date, type

## Questions
- [[Question Title]]: answer summary
```

Scan the section headers first to determine which sections to read.

---

## Domain Sub-Index Format

Each domain folder has a `_index.md` for focused lookups:

```markdown
---
type: meta
title: "Entities Index"
updated: YYYY-MM-DD
---
# Entities

## People
- [[Person Name]]: role, org

## Organizations
- [[Org Name]]: what they do

## Products
- [[Product Name]]: category
```

Use sub-indexes when the question is scoped to one domain. Avoid reading the full master index for narrow queries.

---

## Filing Answers Back

Good answers compound into the wiki. Don't let insights disappear into chat history.

When filing an answer:

```yaml
---
type: question
title: "Short descriptive title"
question: "The exact query as asked."
answer_quality: solid
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags: [question, <domain>]
related:
  - "[[Page referenced in answer]]"
sources:
  - "[[wiki/sources/relevant-source.md]]"
status: developing
---
```

Then write the answer as the page body. Include citations. Link every mentioned concept or entity.

After filing, add an entry to `wiki/index.md` under Questions and append to `wiki/log.md`.

---

## Gap Handling

If the question cannot be answered from the wiki:

1. Say clearly: "I don't have enough in the wiki to answer this well."
2. Identify the specific gap: "I have nothing on [subtopic]."
3. Suggest: "Want to find a source on this? I can help you search or process one."
4. Do not fabricate. Do not answer from training data if the question is about the specific domain in this wiki.
