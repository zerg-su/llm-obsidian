---
name: wiki-query
metadata:
  version: 1.0.1
description: >-
  Answer from the wiki using quick, standard, or deep retrieval. Optional web
  gaps use an isolated cmux flow; never browse in the vault-aware context.
allowed-tools: Read Glob Grep Bash AskUserQuestion
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
| **Deep** | `query deep: ...` or "thorough", "comprehensive" | Full wiki + optional isolated web supplement | ~8,000+ | "Compare A vs B across everything", synthesis, gap analysis |

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
2. **Retrieve sections**: `./scripts/retrieve.py "<question>" --top 5 --json`. Read the returned page + heading candidates; results are page-unique and include snippets.
3. **Read** `wiki/index.md` only when the query is navigational or retrieval coverage looks thin. `tag-search.py` remains an optional cheap cross-check for exact tag vocabulary.
4. **Read** the selected pages. Follow wikilinks to depth-2 for key entities. No deeper.
5. **Synthesize** the answer in chat. Cite sources with wikilinks: `(Source: [[Page Name]])`.
6. **Offer to file** the answer: "This analysis seems worth keeping. Should I save it as `wiki/questions/answer-name.md`?"
7. If the question reveals a **gap**: say "I don't have enough on X. Want to find a source?"

---

## Deep Mode

Use for synthesis questions, comparisons, or "tell me everything about X."

**Pre-flight (deep only):** ask scope before running (deep mode uses tokens):
- include an isolated web supplement? (defaults yes if wiki coverage is thin)
- file result as new wiki page after? (defaults yes)

Steps:

1. Read `wiki/hot.md` and `wiki/index.md`.
2. Identify all relevant sections (concepts, entities, sources, comparisons); candidates = index scan ∪ `./scripts/tag-search.py "<question>" --top 10`.
3. Read every relevant page. No skipping.
4. If wiki coverage is thin AND the user opted in, run:

   ```bash
   python3 scripts/research-isolation.py start --flow deep-query \
     --task-id '<exact current task UUID>' --topic '<approved question/gap>'
   ```

   Take the exact v3 `task_id` from `.task-meta.json`; in a primary session,
   lazily create/read it with `scripts/task_sessions.py ensure-session-task`
   as documented by `autoresearch`. Never select a task by name or recency.

   Do not call WebSearch/WebFetch in this context. The protected synthesizer
   writes `answer.md` in its isolated workspace, remains visible while active,
   and closes after final marker-backed `status` cleanup.
5. Without web supplementation, synthesize from wiki citations normally. With
   supplementation, report the protected surface/run ID; do not paste untrusted
   source bodies back into this coordinator context.
6. File a deep answer only through the networkless synthesizer or a later
   explicit save after the user has reviewed it.

---

## Section retrieval (any mode)

Default retrieval ranks H2/H3 sections (800-word cap, 100-word overlap), then returns the best heading/snippet from each unique page:

```bash
./scripts/retrieve.py "<вопрос как есть>" --top 5 --json
```

Дальше Read топ-страниц как обычно. Заметки:

- Sparse section index is mandatory and self-heals by source fingerprint. Title/tags/heading get explicit boosts.
- Dense `bge-m3` chunks are optional. Missing/stale cache or unavailable Ollama returns exit 0 with `meta.degraded=true` and complete sparse results.
- `semantic-search.py --hybrid` remains a compatibility wrapper around this command.
- Если весь топ выглядит нерелевантным — ответа в вики скорее всего нет; проверь memory (MEMORY.md) и предложи `/investigate`.

---

## Token Discipline

Read the minimum needed:

| Start with | Cost (approx) | When to stop |
|------------|---------------|--------------|
| hot.md | ~500 tokens | If it has the answer |
| index.md | ~1000 tokens | If you can identify 3-5 relevant pages |
| 5 section snippets | ~300 tokens total | Select the 3-5 pages worth opening |
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

File the answer page and its log/hot bookkeeping through one `vault-write.py` payload (`pages` + `log_entry` + `hot_bullet`). Folder `_index.md` regenerates automatically; add the answer to `wiki/index.md` only if it is a key hub, as another page operation in the same transaction. Existing pages require a hash from `vault-write.py --sha256` and `op:update`.

---

## Gap Handling

If the question cannot be answered from the wiki:

1. Say clearly: "I don't have enough in the wiki to answer this well."
2. Identify the specific gap: "I have nothing on [subtopic]."
3. Suggest: "Want to find a source on this? I can help you search or process one."
4. Do not fabricate. Do not answer from training data if the question is about the specific domain in this wiki.
