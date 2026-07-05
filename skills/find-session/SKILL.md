---
name: find-session
version: 1.1.0
description: |
  Find prior Claude Code sessions matching a task: wiki sessions: frontmatter + history.jsonl, top-5 by recency x hits. Read-only.
  Triggers: найди похожую сессию, было ли подобное, find similar past task, /find-session.
allowed-tools: Read Glob Grep Bash AskUserQuestion
---

# /find-session — boring retrieval, no semantic

Boring path by design: files + jsonl indexes + grep. No embeddings — deterministic, cheap, explainable.

## Input

```
/find-session <task description / keywords>
```

Examples:
- `/find-session similar refactoring task in the parser module`
- `/find-session past research on vector databases`
- `/find-session flaky integration test in CI`

If the argument is empty — ask in pre-flight.

---

## Phase 0: Pre-flight (default: no questions — pure retrieval)

**AUTO-SKIP RULE:** this is a read-only search with safe defaults — do NOT ask questions.
One-line plan: `Searching sessions: <topic>, last 30 days, current project, preview top-5` —
then straight into Phase 1. Defaults: window `last 30 days`, scope `current project only`,
output `preview only` (the user decides what to do with the results).

The only reason for a single `AskUserQuestion`: the search topic cannot be extracted
from the prompt at all ("find that session" with no hint of the subject).

If invoked from a `/dispatch` chain — scope is pre-loaded from the dispatch.

---

## Phase 1: Extract keywords

Extract technical nouns from the task description:
- Tool / library names (`pytest`, `webpack`, `sqlite`, `ollama`).
- Project / module names (`parser`, `auth-service`, `blog`).
- Error / symptom strings (`OOM`, `timeout`, `flaky test`, `segfault`).
- Topic terms (`vector database`, `rate limiting`, `migration`).

Multi-word phrases group as a single keyword (`vector database` → `"vector database"`).

Simple extractor:

```python
# tokenize → lower → filter stopwords → ngram top-K
keywords = extract_technical_nouns(prompt, max=8)
```

If keywords < 2 — the prompt is too vague, ask for refinement.

---

## Phase 2: Two-pronged retrieval (parallel, read-only)

### Prong A: wiki pages → sessions

```bash
# Grep wiki/ for keywords (top 3-5)
grep -l -i -e "<kw1>" -e "<kw2>" -e "<kw3>" wiki/**/*.md 2>/dev/null

# From each matched page — Read frontmatter, extract the `sessions:` array
# (Or faster: .vault-meta/index.jsonl is already structured)
jq -c 'select(.path | contains("<keyword>"))' .vault-meta/index.jsonl
```

Reverse lookup: given a page path → `.vault-meta/session-to-pages.jsonl` → sessions which touched that page.

```bash
# Find sessions for matched pages:
grep -l "<page-path>" .vault-meta/session-to-pages.jsonl
```

### Prong B: ~/.claude/history.jsonl → sessions

```bash
# Filter by keyword + project (if scope=current)
grep -i "<keyword>" ~/.claude/history.jsonl | \
  python3 -c 'import json,sys
for line in sys.stdin:
    obj = json.loads(line)
    if scope == "current" and obj.get("project") != "<repo-root>": continue
    print(obj["sessionId"], obj["timestamp"], obj["display"][:80])'
```

Window filter: `timestamp >= now() - window_days * 86400 * 1000` (ms).

---

## Phase 3: Combine + score + dedupe

Each candidate: `{sid, ts, project, hits, first_keyword_seen, preview}`.

Score formula:

```
score = recency_weight * keyword_hits

recency_weight = exp(-days_since / 30)   # half-life ~ 21 days
keyword_hits = count of distinct keywords matched in (prompt OR linked page title)
```

Dedupe by `sid` — keep the highest-score record per session.

Sort DESC by score. Top 5.

---

## Phase 4: Output

```markdown
## Similar past sessions (top 5)

| # | Date | sid (short) | Project | Match | Preview |
|---|---|---|---|---|---|
| 1 | 2026-05-18 | e211...8060 | llm-obsidian | parser, refactor | "extracted tokenizer into its own module, tests..." |
| 2 | 2026-05-19 | a4f2...c012 | blog | webpack, upgrade | "webpack 5 migration analysis..." |
| 3 | 2026-04-26 | 91bc...028e | notes-tools | vector database | "compared embedding stores for local search..." |
| ... |

**Suggested actions:**
- Open one in resume mode: `claude --resume e211483a-9b6a-4d84-b360-88b08324d060`
- (if there is a c-address) Read the related wiki page: `wiki/questions/Vector store choice.md` (c-000180)
- Auto-inject top-1 summary into the current task prompt? (if mode=auto-inject — proceed; else suggest only)
```

`claude --resume` is run by the user in their terminal (the skill does not invoke it).

### Auto-inject mode (if the user chose it in pre-flight)

If mode=auto-inject:

1. Read the top-1 candidate's page(s) — take linked wiki pages from `session-to-pages.jsonl`.
2. Compose a ≤300-word summary from the page content.
3. Inject into the current chat context as a block:

   ```
   ## Context from prior session <sid> (2026-05-18)

   <summary>

   Full context: wiki/<folder>/<page>.md (c-000180)
   Resume: claude --resume e211483a-9b6a-4d84-b360-88b08324d060
   ```

NEVER auto-resume — that is an explicit user decision.

---

## Edge cases

### No matches

```markdown
## Similar past sessions — 0 matches

Considered:
- wiki pages with keywords <kw1>, <kw2>: 0
- history.jsonl (last 30 days, current project): 0

This is the first touch of this topic. Suggested next step:
- Just proceed — once filed via /save, it becomes findable by future find-session runs.
```

### Window expansion suggestion

If the 30-day window returns 0 but the keywords look fine:

```markdown
Note: searched the last 30 days. If nothing turned up — try `--all-time` (1 year of history) or relax the keywords.
```

### Partial source failure

If `~/.claude/history.jsonl` is unavailable (e.g. fresh install) — skip Prong B, mention it in the output. If `.vault-meta/session-to-pages.jsonl` is empty (vault just initialized) — skip the Prong A reverse lookup, fall back to grepping wiki/ directly.

---

## Reuse pointers

- `~/.claude/history.jsonl` — global Claude Code history.
- `.vault-meta/index.jsonl` — vault frontmatter index (regenerated by the Stop hook).
- `.vault-meta/session-to-pages.jsonl` — reverse map sid → pages.
- `.vault-meta/address-map.tsv` — for cross-ref c-NNNNNN ↔ page path.
- Convention: every wiki page carries `sessions:` provenance in frontmatter — that is what makes Prong A possible.

---

## Anti-patterns

- ❌ Auto-resuming another session — destructive; the user decides explicitly in the terminal.
- ❌ Semantic embedding / vector search — out of scope here by design; propose separately if boring retrieval proves insufficient.
- ❌ Reading full session JSONL from `~/.claude/projects/<project>/<sid>.jsonl` for previews — large files. Use `history.jsonl` for the preview string; the full session only if the user actively opens it via `claude --resume`.
- ❌ Output > top-5 — the user will not scan more.
- ❌ Scoring without recency decay — year-old sessions drown out recent relevant ones.
- ❌ Skipping dedupe by sid — the same session lands in Prong A AND Prong B → noise.
