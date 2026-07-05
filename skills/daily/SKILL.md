---
name: daily
version: 1.0.0
description: |
  Today's daily status as 3-7 bullets. Aggregates session pages, log entries, and git commits for today; appends to wiki/routines/Daily Status Log.md (newest first). Tone: one bullet-list version, concise, technical terms in English, no hashes/YAML/plans.
  Use when: end of work day, /daily, "day summary". Aggregation-only, single append.
  Triggers (EN): /daily, daily status, what was done today, day summary.
  Triggers (RU): статус за день, сводка за день, дневной лог, дневной статус.
allowed-tools: Read Write Edit Glob Grep Bash
---

# daily: Daily Status Update Generator

Aggregates today's work from the wiki and git, synthesizes a prose summary per the
style guide below, and appends it to [[Daily Status Log]].

Anti-cargo: the skill does **not** write "a report about the Claude Code session" —
it writes about the **user's work done during the day**, as if for an outside reader.

---

## Sources for the day

Aggregate from these places (filter by today's date):

1. **Session pages** — `wiki/meta/sessions/<YYYY-MM-DD>-*.md`
   - Parse frontmatter (`title`) + first paragraph (outcome) + the `## Outcome` section if present
   - This is the richest source
2. **`wiki/log.md`** — entries `## [<YYYY-MM-DD>]`
   - All save / wiki operations for the day
3. **Git log of this vault** for today:
   ```bash
   git log --since="00:00" --until="now" --oneline
   ```
   - Subject lines give short themes. If you track other personal project repos,
     run the same command in each (`git -C <repo> log ...`).
4. **Hot cache** (`wiki/hot.md`, Active Threads section) — only to corroborate what
   other sources already mention; not a source of new items by itself
5. **A ready summary from the current session** — if the user just produced one in
   chat, use it as-is or with minimal edits

---

## Style Guide

Write **one** version (not two). Tone: dry, precise, pitched between a technical
peer and an interested outsider.

### Format

```
### YYYY-MM-DD

- <name>: <action+outcome>, 1-2 sentences.
- <name>: <...>.
- ...
```

3-7 bullets per day. Each bullet = one logical unit of work.

### Each bullet contains

- **First person singular**: "set up / found / cleaned / traced / postponed" — the
  user forwards the entry as **their own** progress note (the work is joint, but the
  authorship is theirs). Keep other people's actions attributed by name.
- **Object name first**: `blog pipeline` / `home server` / `reading notes` /
  `wiki plugin`. English technical names stay in English.
- **Action + outcome**: what was done + what it led to. "Added a min-rate guard to
  the alert rule, the noise is gone" — NOT "worked on alerts".
- **Concrete without deep tech**: "the nightly disconnects stopped" is OK; a raw
  metric name or internal flag dump is NOT.

### Forbidden (filter before output)

- ❌ Commit hashes / branch names / repo paths / issue-tracker IDs / page IDs.
- ❌ YAML selectors, CLI flags, jargon-level abbreviations — replace with a plain phrasing.
- ❌ Process trivia: "updated memory", "filed a wiki page", "opened a PR for review". Process ≠ result.
- ❌ Plans for tomorrow. Only what was DONE.
- ❌ Cargo verbs / slang.
- ❌ Vague anonymous phrasing: "one of the services", "an internal tool" — name it.
- ❌ Plural / impersonal voice: "we did", "it was done" — first person singular only
  (other people's actions are the exception).

### Allowed

- ✅ English names for services / environments / mainstream terms (CI/CD, backup, pipeline).
- ✅ Rounded numbers: "3 episodes this week", "~50 RPS", "11 nodes".
- ✅ Names of collaborators when relevant for context (without process minutiae).

### Example

```
### 2026-05-22

- blog pipeline: moved image resizing into the build step, publish time dropped from 4 min to 40 s.
- home server: traced the nightly Wi-Fi drops to a DHCP lease conflict, pinned static leases; drops stopped.
- reading notes: filed three chapters of the current book into the wiki with cross-links.
- wiki plugin: rewrote 7 skill descriptions and added retrieval indexes.
```

### Self-test before output

1. An outside reader skims for 30 seconds → understands the class of work + direction?
2. A technical reader → sees what was concretely done (names), without drowning in YAML / hashes?
3. Read it aloud → sounds like "one adult telling another"? Otherwise rewrite.

---

## Workflow

1. **Determine date.** Default today (`date +%Y-%m-%d`). If the user explicitly gave
   a date argument — use it.
2. **Aggregate** all 5 sources above. Deduplicate meaning (a session page and the git
   log often describe the same thing).
3. **Group by theme.** Group findings into 3-7 bullets (one logical unit of work =
   one bullet).
4. **Write bullets.** Each bullet 1-2 sentences, format `- <name>: <action+outcome>.`
   per the style guide above.
5. **Self-review** against the checklist:
   - **Names**: concrete projects/services named — NOT "an internal tool"?
   - **No deep tech**: no commit hashes, YAML selectors, CLI flags, repo paths, branch names?
   - **No process trivia**: no "updated memory", "filed a wiki page"?
   - **First person singular**, no plural/impersonal voice (others' actions excepted)?
   - **No plans for tomorrow**.
   - If anything is violated — rewrite.
6. **Two-target write.** The full version goes to the date page; a compact index line
   goes to the log.
   - **6a. `## Сделано` on the date page (full).** Path
     `wiki/daily/$(date +%Y)/$(date +%m)/$(date +%F).md`. `mkdir -p` the directory; if
     the page does not exist — create it from the canonical skeleton (see
     `skills/journal/SKILL.md`). Write the full bullet list (3-7, action+outcome) into
     `## Сделано`, **replacing** its content on a re-run for the same day (do not
     append twice). Do not touch the other sections.
   - **6b. `Daily Status Log.md` (compact index).** Append under `## Журнал`, newest
     entries on top: `### YYYY-MM-DD`, then **one short line per task** (compress each
     bullet to its essence: `- <name>: <what>`, no second sentence), then a link line
     `→ [[YYYY-MM-DD]]` to the full day page. Append-only, never touch past entries.
   - **6c. `## Сессии` on the date page (session → task map, for `claude --resume`).**
     Run the **deterministic** collector — do NOT synthesize by hand:
     ```bash
     python3 scripts/session-map.py          # today; markdown lines `- <label> · \`<uuid>\``
     ```
     The script enumerates today's transcripts and labels each session by the wiki
     pages it touched (`.vault-meta/index.jsonl`), falling back to the first prompt.
     Write its lines into `## Сессии` (replace the section, idempotent). **One light**
     AI pass only to: (a) fill any `⟨label?⟩` from the day's context, (b) shorten
     obviously long labels, (c) align phrasing with `## Сделано` themes where easy.
     **Never** put `## Сессии` into the clipboard or any external write-up — it is
     internal navigation.
7. **Bump frontmatter** of both files: on the date page — `updated` and add
   `$CLAUDE_CODE_SESSION_ID` to `sessions:`; in `Daily Status Log.md` — `last_done`
   and `updated` = the entry date (otherwise the fields go stale).
8. **Copy to clipboard.** Copy the **full** bullet list (from `## Сделано`) to the
   clipboard so the user can paste it wherever they report status. Write to a temp
   file and `pbcopy < file` — do NOT pipe non-ASCII text through complex quoting:
   ```bash
   pbcopy < /tmp/daily-status.txt   # = the full bullet list from ## Сделано
   ```
9. **Show** the full bullet list in chat + a line "compact index with link appended
   to [[Daily Status Log]]", and confirm the clipboard is ready to paste.

---

## Bash helpers

Today's date:
```bash
TODAY=$(date +%Y-%m-%d)
```

Today's session pages:
```bash
ls wiki/meta/sessions/${TODAY}-*.md 2>/dev/null
```

Today's log entries:
```bash
awk "/^## \[${TODAY}\]/{flag=1} /^## \[/ && !/^## \[${TODAY}\]/{flag=0} flag" wiki/log.md
```

Today's commits in this vault:
```bash
git log --since="00:00" --until="now" --oneline
```

Copy the finished output to the clipboard (non-ASCII text — via a file, not a pipe):
```bash
pbcopy < /tmp/daily-status.txt
```

---

## Output template (two targets)

**1. Date page `## Сделано` (full — this is what gets copied):**

```markdown
## Сделано

- <name>: <action+outcome>, 1-2 sentences.
- <name>: <action+outcome>.
- <name>: <action+outcome>.
```

**2. `Daily Status Log.md` (compact index, append on top):**

```markdown
### 2026-05-22

- <name>: <what> (one line per task, compressed)
- <name>: <what>
→ [[2026-05-22]]
```

The full version (target 1) is copied to the clipboard (`pbcopy < /tmp/daily-status.txt`)
and shown in chat. The compact version (target 2) serves as a scannable timeline with
a link to the full day.

---

## Edge cases

- **Nothing happened today**: do not append an empty entry. Tell the user
  "found no changes for today — add something manually?".
- **An entry for this date already exists**: date page — REPLACE `## Сделано`
  (idempotent, no second block); `Daily Status Log.md` — ask replace/supplement/skip
  (it is append-only, duplicates are undesirable).
- **Only commits, no session pages**: OK, generate from git log subject lines + file context.
- **Only a session page, no commits**: OK, source of truth = the session page.
- **Several session pages in one day**: synthesize from all of them (typical case —
  morning + evening).
