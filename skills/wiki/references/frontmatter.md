# Frontmatter Schema

Every wiki page starts with flat YAML frontmatter. No nested objects. Obsidian's Properties UI requires flat structure.

---

## Universal Fields

Every page, no exceptions:

```yaml
---
type: <source|entity|concept|domain|comparison|question|overview|meta>
title: "Human-Readable Title"
created: 2026-04-07
updated: 2026-04-07
tags:
  - <domain-tag>
  - <type-tag>
status: <seed|developing|mature|evergreen>
related:
  - "[[Other Page]]"
sources:
  - "[[.raw/articles/source-file.md]]"
sessions:
  - a6508cf3-866f-4d31-91a6-19d6538d1669
---
```

**sessions field**: append-only list of agent session/thread IDs that created or modified this page. Pull current ID with `./scripts/current-session-id.sh` (`CLAUDE_CODE_SESSION_ID` in Claude Code, `CODEX_THREAD_ID` in Codex). On create, write a single-item list. On every meaningful edit, **append** the current session ID to the end of the list — never remove, replace, or reorder prior entries. Single exception: if the current session ID is already the last item (same session editing the same page repeatedly), don't duplicate it. Order is chronological by definition. Builds a full history of every session that touched the page, so future-you can reopen any of them for fuller context.

`wiki/log.md` and the writer-owned `wiki/hot.md` cache keep their seeded
`sessions:` marker instead of appending every bookkeeping mutation; both are
mutated only by `vault-write.py`. Durable content pages, plans, and review
archives carry concrete session provenance.

**status values:**
- `seed`: exists, barely populated
- `developing`: has real content, not yet complete
- `mature`: comprehensive, well-linked
- `evergreen`: unlikely to need updates

---

## Type-Specific Additions

### source

Add these fields after the universal fields:

```yaml
source_type: article    # article | video | podcast | paper | book | transcript | data
author: ""
date_published: YYYY-MM-DD
url: ""
confidence: high        # high | medium | low
key_claims:
  - "First key claim from this source"
  - "Second key claim"
```

### entity

```yaml
entity_type: person     # person | organization | product | repository | place
role: ""
first_mentioned: "[[Source Title]]"
```

### concept

```yaml
complexity: intermediate  # basic | intermediate | advanced
domain: ""
aliases:
  - "alternative name"
  - "abbreviation"
```

### comparison

```yaml
subjects:
  - "[[Thing A]]"
  - "[[Thing B]]"
dimensions:
  - "performance"
  - "cost"
  - "ease of use"
verdict: "One-line conclusion."
```

### question

```yaml
question: "The original query as asked."
answer_quality: solid   # draft | solid | definitive
```

### domain

```yaml
subdomain_of: ""        # leave empty for top-level domains
page_count: 0
```

---

## Rules

1. Use flat YAML only. Never nest objects.
2. Dates as `YYYY-MM-DD` strings, not ISO datetime.
3. Lists always use the `- item` format, not inline `[a, b, c]`.
4. Wikilinks in YAML fields must be quoted: `"[[Page Name]]"`.
5. Keep `related` and `sources` as wikilinks, not plain URLs.
6. Update `updated` every time you edit the page content.
7. Append current session id from `./scripts/current-session-id.sh` to `sessions:` on create and on every meaningful edit. Append only — never remove or overwrite old IDs (skip only if current ID is already the last entry). The list is a chronological history of every session that touched the page; global log bookkeeping is handled by `vault-write.py`.
