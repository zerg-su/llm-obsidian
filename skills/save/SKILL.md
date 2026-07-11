---
name: save
metadata:
  version: 1.0.0
description: >-
  Save the conversation/insight into the wiki as a structured note: frontmatter, folder, DragonScale address, session provenance; log/hot bookkeeping через scripts/vault-write.py. Smart fast-path: infer type/folder/title, one-line plan, ask only when ambiguous. Triggers: /save, сохрани (это/сессию), зафайли, запиши в вики, добавь в вольт, save this, file this, keep this.
allowed-tools: Read Write Edit Glob Grep AskUserQuestion Bash
---

# save: File Conversations Into the Wiki

Good answers and insights shouldn't disappear into chat history. This skill takes what was just discussed and files it as a permanent wiki page.

The wiki compounds. Save often.

---

## Note Type Decision

Determine the best type from the conversation content:

| Type | Folder | Use when |
|------|--------|---------|
| synthesis | wiki/questions/ | Multi-step analysis, comparison, or answer to a specific question |
| concept | wiki/concepts/ | Explaining or defining an idea, pattern, or framework |
| source | wiki/sources/ | Summary of external material discussed in the session |
| decision | wiki/decisions/ | Architectural, project, or strategic decision that was made |
| session | wiki/meta/sessions/ | Full session summary: captures everything discussed |

If the user specifies a type, use that. If not, pick the best fit based on the content. When in doubt, use `synthesis`.

---

## Save Workflow

Three phases. Plan everything first, then dispatch all writes in one batched assistant message, then confirm. **Never write files one-by-one across separate turns** — that produces sequential model roundtrips and staggered Stop-hook commits, and `/save` ends up feeling slow.

### Role split — what goes where

The four touched files have **different jobs**. Don't duplicate content across them.

- **`wiki/<folder>/<note>.md`** — source of truth: full content, frontmatter, evidence. It is included in the same `vault-write.py` transaction as bookkeeping; never Write/Edit it directly.
- **`wiki/log.md` + `wiki/hot.md`** — bookkeeping in that same payload. The writer prepends entries, enforces caps, and rolls a crashed multi-file transaction forward on its next call.
- **`wiki/index.md`** — thin curated map. **Default: do NOT touch it.** Folder listings regenerate automatically (`reindex.py --folder-indexes` in the Stop hook). Add a link manually only when the page is a key hub for its domain.
- **Folder `_index.md`** — never touch; the AUTO-INDEX block is machine-owned.

Long prose lives in the new note + the log entry. The hot bullet is a wikilink + one sentence — never a restatement (the script hard-truncates anyway).

### Phase 0 — Smart pre-flight (fast-path by default)

/save is the most-used skill (~95 calls/month); a 3-5 question pre-flight on every call is the
single biggest friction point in the pipeline. So: **infer, show, proceed** — ask only when
genuinely ambiguous.

1. **Infer** from the conversation: note type (table above), target folder, note title,
   update-vs-new (check for an existing page on the same subject — Grep/index).
2. **If ALL of these hold, take the fast-path** — show a one-line plan and continue straight
   into Phase 1 without AskUserQuestion:
   - type is clear-cut (content fits exactly one row of the type table);
   - exactly one target folder candidate;
   - no existing page covers the same subject (else it's an update — also unambiguous: update it);
   - title is either user-given or derives directly from the subject.
   One-line plan format (user sees it and can interrupt):
   `Сохраняю: type=<type> → wiki/<folder>/<Note Title>.md, links: [[A]], [[B]]`
3. **If ANY check fails, ask ONE AskUserQuestion** with concrete options covering only the
   ambiguous dimension(s) (e.g. "synthesis в questions/ или concept в concepts/?",
   "обновить [[Existing Page]] или новая страница?"). Never a 3-5 question battery.
4. This fast-path is a sanctioned exception to the project-wide pre-flight policy
   (memory `feedback_skill_preflight_clarification`) — for /save only, the policy is
   "smart": full clarification stays mandatory for every other write-skill.

### Phase 1 — Plan (one turn, no writes)

In a single turn, before issuing any Write/Edit:

1. **Scan** the current conversation. Identify the most valuable content to preserve.
2. **Name** the note (from Phase 0; ask only if Phase 0 escalated). Keep the name short and descriptive. **The note's filename and every wikilink you write to it (hot.md bullet, index.md, log.md, cross-refs) must be the SAME string** — a "human" link name pointing at a slug-named file is the #1 source of dead links (34 fixed in lint 2026-06-09).
3. **Determine** note type using the table above (already inferred in Phase 0).
4. **Extract** all relevant content from the conversation. Rewrite it in declarative present tense (not "the user asked" but the actual content itself).
5. **Collect links**: identify any wiki pages mentioned in the conversation. Add them to `related` in the **new note's** frontmatter (NOT in `wiki/hot.md` frontmatter — see Phase 2 below).
6. **Draft** one transactional payload internally before writing anything (no pre-reads of log/hot needed — vault-write.py owns their structure):
   - Full content of the new note (path, frontmatter, body) — source of truth.
   - The vault-write JSON payload: `log_entry` (full prose, format below), `hot_bullet` (one line, format below), optional `hot_threads` `{"add": [...], "resolve": ["substring"]}` when the save opens/closes an Active Thread, optional `hot_narrative` (≤120 words) when the save IS the new headline event of the day.

Log entry format (full prose, history-grade):
```
## [YYYY-MM-DD] save | Note Title

`c-NNNNNN` [[Note Title]]. Multi-sentence prose summary of what was decided, discovered, or done. Include evidence, alternatives considered, references. Long is correct — this is the journal entry future-you reads.
```

Hot.md bullet format (terse, scannable, ~one line):
```
- YYYY-MM-DD: [[Note Title]] — one-sentence essence (`c-NNNNNN`)
```

For update-mode, capture the optimistic lock before drafting:

```bash
python3 scripts/vault-write.py --sha256 "wiki/<folder>/<Note Title>.md"
```

### Phase 2 — Write (one dispatcher call)

Pipe the page plus bookkeeping into one dispatcher payload:

  ```bash
  python3 scripts/vault-write.py <<'PAYLOAD'
  {"actor": "save", "session": "<SESSION_ID>",
   "pages": [{"op": "create", "path": "wiki/<folder>/Note Title.md", "content": "<full markdown, JSON-escaped>"}],
   "log_entry": "## [YYYY-MM-DD] save | Note Title\n\n`c-NNNNNN` [[Note Title]]. Full prose...",
   "hot_bullet": "YYYY-MM-DD: [[Note Title]] — one-sentence essence (`c-NNNNNN`)"}
  PAYLOAD
  ```

For an existing page use `"op":"update"` and include the previously captured `"expected_sha256":"..."`. Exit 2 is a cap/lifecycle violation; exit 4 is a concurrent-edit conflict—re-read and re-draft. Never bypass either with direct Edits.

**Do NOT** edit `wiki/hot.md` directly, its frontmatter `related:` included (curated evergreen hubs, managed by `/wiki-lint`).

### Phase 3 — Confirm (one short turn)

After the batch returns:

- **Confirm**: "Saved as [[Note Title]] in wiki/[folder]/."
- **Runbook nudge**: if this session ran a meaningful shell procedure (≥10 non-trivial successful commands in `.vault-meta/command-log.jsonl` for the current session — spot-check with `SESSION_ID=$(./scripts/current-session-id.sh); grep -c "$SESSION_ID" .vault-meta/command-log.jsonl` mentally, no need for exact counts) and no runbook was filed, append one line: «В сессии была содержательная shell-процедура — /distill-runbook может сделать из неё ранбук.»

---

## Frontmatter Template

**Every saved page MUST carry `sessions:` (provenance) and `address:` (DragonScale).**
The dispatcher owns the global log; every content page carries provenance.

```yaml
---
type: <synthesis|concept|source|decision|session|service|incident|runbook|question|goal|...>
title: "Note Title"
address: c-NNNNNN              # allocated via ./scripts/allocate-address.sh
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags:
  - <relevant-tag>
status: developing
sessions:                       # provenance — required (see feedback_session_id_in_frontmatter)
  - <SESSION_ID>                # substitute ./scripts/current-session-id.sh output, NOT the literal template
related:
  - "[[Any Wiki Page Mentioned]]"
sources:
  - "[[.raw/source-if-applicable.md]]"
---
```

How to get session ID inside the skill:
```bash
./scripts/current-session-id.sh
```
If the script returns `unknown` (rare), record `unknown` and flag it in the body so future
reindex pass catches it.

For `question` type, add:
```yaml
question: "The original query as asked."
answer_quality: solid
```

For `decision` type, add:
```yaml
decision_date: YYYY-MM-DD
status: active
```

---

## Writing Style

- Declarative, present tense. Write the knowledge, not the conversation.
- Not: "The user asked about X and Claude explained..."
- Yes: "X works by doing Y. The key insight is Z."
- Include all relevant context. Future sessions should be able to read this page cold.
- Link every mentioned concept, entity, or wiki page with wikilinks.
- Cite sources where applicable: `(Source: [[Page]])`.

---

## Content Quality & Detail

A wiki page is only worth saving if it is **informative for future-you reading
it cold months later**. No vague generalities. Two tracks of "informative",
depending on the page's nature:

### Internal-infra pages (service / incident / runbook / decision / goal про наше)

Include concrete environment-specific detail:

- **Environment names**: prod / staging / home-lab / dev — whichever the page is about.
- **Host / cluster identifiers**: the exact hostname or cluster name (e.g.
  `homelab-nas-01`), including any alias mismatch between systems.
- **Namespace / repo / module path**: full `github.com/<user>/<repo>` URL or an
  exact path reference. Not "in one of the repos" — the specific one.
- **Resource names**: service / container / unit-file / database / bucket names.
- **Versions**: package version, image tag, firmware revision, release number.
- **Addresses + cross-refs**: `c-NNNNNN` for every related page; wikilinks
  to all mentioned services / incidents / decisions.
- **Dates with absolute year**: `2026-05-20 17:55`, not "yesterday" / "on Wednesday".
- **Specific numbers**: replica counts, memory limits, request rates, error counts.
- **Ticket IDs**: `ISSUE-NNN` (or your tracker's equivalent) if any.
- **Decisions verbatim**: "we do not do X because Y" — full reasoning, not
  "we discussed it".

### General/technology pages (concept / source / comparison / external research)

Internal detail less relevant. Instead include:

- **Technology / library / spec versions** to which the content applies.
- **Source citations** with URLs and dates fetched.
- **Comparative scope**: «relative to alternative X, this differs in Y».
- **Applicability constraints**: «применимо к K8s ≥ 1.28», «only on AWS, not GCP».
- **Quotes / paraphrases** with attribution.
- **Date verified**: проверено-актуально-на.

If a page mixes both (например ADR про внутреннее решение, использующее external
tech) — give both tracks. Не «один-line summary и идём дальше».

### What "informative" is NOT

- Restatement of memory rules → cross-link `[[feedback_*]]` instead.
- Pure narrative «we discussed X, Y, Z» without facts → rewrite в declarative.
- Vague terms — «улучшилось», «стало стабильнее» → numbers / before-after.
- Page mostly composed of cross-refs without content → either expand or merge
  into related page.

---

## What to Save vs. Skip

Save:
- Non-obvious insights or synthesis
- Decisions with rationale
- Analyses that took significant effort
- Comparisons that are likely to be referenced again
- Research findings

Skip:
- Mechanical Q&A (lookup questions with obvious answers)
- Setup steps already documented elsewhere
- Temporary debugging sessions with no lasting insight
- Anything already in the wiki

If it's already in the wiki, update the existing page instead of creating a duplicate.
