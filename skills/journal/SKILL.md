---
name: journal
version: 1.0.0
description: >-
  Date-keyed journal wiki/daily/YYYY/MM/DD.md: открыть/создать сегодняшнюю страницу (carry-over невыполненного) или записать план/напоминание на дату. Секции: Планы/Напоминания/Инциденты/Сделано. Not for: EOD-статус (/daily), dateless capture (/backlog). Triggers: /journal, дневник, план на завтра/<дата>, запиши на <дата>, напомни на <дата>, перенеси невыполненное, plan for tomorrow, remind me on <date>.
allowed-tools: Read Write Edit Glob Grep Bash AskUserQuestion Skill
---

# journal: date-keyed daily journal + forward planning

One page per calendar date at `wiki/daily/<YYYY>/<MM>/<YYYY-MM-DD>.md`. This skill
owns the **forward / today** side of the daily loop. The **end-of-day status**
(`## Сделано`) is owned by `/daily` (see that skill); `/journal` never synthesizes
the day's status itself.

Daily loop context:

```
Morning:   /journal               open today + carry over unfinished + glance
During:    "на завтра X"          NL capture into the right date's page
           "напомни в пт Y"
Evening:   /daily                 fills ## Сделано + pbcopy + compact line in Daily Status Log
```

## The date page (canonical skeleton)

Every date page has the SAME four sections, in this order (page reads top-down:
forward intent first, retrospective last). When creating a page, write exactly:

```markdown
---
type: daily
title: "<DATE>"
date: <DATE>
created: <DATE>
updated: <DATE>
tags:
  - daily
status: evergreen
sessions:
  - <SESSION_ID>
---

# <DATE>

## Планы

## Напоминания

## Инциденты

## Сделано

## Сессии

## Заметки
```

`<DATE>` = `YYYY-MM-DD`. This MUST match `_templates/daily.md` (the same skeleton the
Obsidian "Open today's daily note" hotkey inserts) so the two never drift.

Section semantics (keep them distinct):

- **Планы** — actionable tasks as checkboxes `- [ ]`. Carryover-eligible. Render as
  task-dots in the Calendar plugin.
- **Напоминания** — plain bullets `- ...`. Date-bound FYI (deadlines, "today is X").
  NOT carried over.
- **Инциденты** — one line per episode, `HH:MM short + [[YYYY-MM-DD-slug]]` pointing to
  the full post-mortem in `wiki/incidents/` (authored by `/incident`). Never clone the
  post-mortem here.
- **Сделано** — filled by `/daily` at EOD. `/journal` leaves it alone.
- **Сессии** — map of today's Claude Code sessions to their tasks, for `claude --resume`
  next day (so you continue a story instead of spawning new sessions). Lines are
  `- <label> · \`<full-session-uuid>\``. Populated cheaply by `scripts/session-map.py`
  (deterministic; labels from wiki pages each session touched) — `/daily` runs it at
  EOD; `/journal sessions` refreshes it on demand. Internal only (never in external
  write-ups / pbcopy). `/journal` leaves it to the script.
- **Заметки** — free-form scratch / working area for the day: throwaway snippets, code,
  commands, paste-buffer, half-thoughts. No structure imposed; not carried over, not
  surfaced by `/morning`. The bottom drawer of the page.

No `address:` field — `type: daily` pages are address-exempt (would burn ~365/year).

## Phase 0: pre-flight (capture fast-path)

This skill mutates files, but it is a low-ambiguity capture tool — use the `/save`
smart fast-path model: infer mode + target date, show a one-line plan, then act.
Ask a single `AskUserQuestion` ONLY when genuinely ambiguous (e.g. "на 3 мая" and
the year is unclear, or intent plan-vs-reminder is unclear). Do not run a 5-question
battery for a one-line capture.

## Modes (NL-first; explicit subcommands optional)

Parse the request into one mode. Free-form phrasing must work without exact syntax.

### Mode `today` (default — `/journal` with no args, "открой дневник")

1. **Resolve path** for today and ensure the page exists:
   ```bash
   TODAY=$(date +%F)                    # 2026-06-26
   DIR="wiki/daily/$(date +%Y)/$(date +%m)"
   PAGE="$DIR/$TODAY.md"
   mkdir -p "$DIR"
   ```
   If `$PAGE` does not exist, create it from the skeleton (substitute `<DATE>`=`$TODAY`,
   `<SESSION_ID>`=`./scripts/current-session-id.sh` output). If it exists, leave content.

2. **Carryover.** Find the most recent PRIOR date page that has unchecked `- [ ]`
   lines under `## Планы` (scan back up to ~5 days so Monday picks up Friday):
   ```bash
   for i in 1 2 3 4 5; do
     d=$(date -v-${i}d +%F); y=$(date -v-${i}d +%Y); m=$(date -v-${i}d +%m)
     p="wiki/daily/$y/$m/$d.md"
     [ -f "$p" ] && grep -q '^- \[ \]' "$p" && echo "$p" && break
   done
   ```
   If found, read the unchecked `- [ ]` items under that page's `## Планы`, show them,
   and ask once (`AskUserQuestion` or a one-line confirm) whether to move them into
   today's `## Планы`. On yes: append them under today's `## Планы` (dedup against
   lines already there) and tick them as `- [x]` (moved) on the source page, or leave
   the source as-is per user preference — default: copy forward, leave source intact
   (avoid rewriting history). Never carry the same item twice (dedup on text).

3. **Glance in chat.** Print a compact rendering of today so the user need not switch
   to Obsidian:
   ```
   📓 2026-06-26
   Планы:       - [ ] item A   - [ ] item B
   Напоминания: - reminder
   Инциденты:   (нет)
   ```

4. **Optional open in Obsidian** (only if the user asked to open it):
   ```bash
   open "obsidian://open?path=$(python3 -c 'import urllib.parse,os;print(urllib.parse.quote(os.path.abspath("'"$PAGE"'")))')"
   ```

### Mode `plan` / `remind` (forward capture)

Trigger: "на завтра X", "запиши на 3 мая X", "напомни в пятницу Y", `/journal plan
<date> <text>`, `/journal remind <date> <text>`.

1. **Resolve target date** (see Date resolution below) → `TGT` (`YYYY-MM-DD`).
2. **Ensure the target page exists** (same path build + skeleton create as `today`,
   using `TGT` and its `$(...)` year/month). `mkdir -p` first.
3. **Append the item:**
   - Default / "план" / "сделать" → `- [ ]` task under `## Планы`.
   - "напомни" / "не забыть на дату" / pure FYI → plain `- ` bullet under `## Напоминания`.
   - "в заметки" / "в рабочую область" / "scratch" / a snippet or code block → append
     under `## Заметки` verbatim (wrap code in a fenced ``` block; no dedup, scratch is
     free-form). Defaults to today's page unless a date is given.
   - Dedup: skip if an identical-text item already exists in that section (Планы /
     Напоминания only).
4. **Bump** the target page `updated:` to today's date and add the current session id
   from `./scripts/current-session-id.sh`
   to `sessions:` if absent.
5. **Confirm** one line: `→ wiki/daily/2026/05/2026-05-03.md · Планы · "<text>"`.

### Mode `check` (tick a task done — optional)

Trigger: "отметь X сделанным", `/journal check <text>`. On today's page, flip the
matching `## Планы` line `- [ ]` → `- [x]`. If multiple match, ask which.

### Mode `sessions` (session map — cheap, no synthesis)

Trigger: "какие сессии сегодня", "карта сессий", `/journal sessions [<date>]`. This is
the on-demand refresh of `## Сессии` (the EOD path is `/daily`).

1. Run the deterministic collector (no AI for the lookup):
   ```bash
   python3 scripts/session-map.py            # today, markdown lines
   # ./scripts/session-map.py 2026-06-26     # explicit date
   ```
   It enumerates today's Claude transcripts and labels each session from the wiki
   pages it touched (`.vault-meta/index.jsonl`), falling back to the opening prompt.
2. Write the lines verbatim into today's `## Сессии` (replace the section, idempotent).
   Do a SINGLE light pass only to (a) fill any `⟨label?⟩` from the day's context and
   (b) shorten obviously long labels — do not re-derive what the script already labeled.
3. Show the list in chat so the user can `claude --resume <uuid>` straight away.

Never put `## Сессии` content into external write-ups / pbcopy — it is internal.

## Date resolution (macOS BSD `date`)

- `today` / `сегодня` → `date +%F`
- `завтра` / `tomorrow` → `date -v+1d +%F`
- `послезавтра` → `date -v+2d +%F`
- `через неделю` / `in a week` → `date -v+1w +%F`
- weekday ("в пятницу" / "friday") → `date -v+fri +%F` (weekday abbreviations
  Sun..Sat accepted by BSD `date -v`). NOTE: returns **today** if today already is
  that weekday — for strictly-future use `date -v+1d -v+fri +%F`. When ambiguous
  (today is that weekday), confirm via one `AskUserQuestion`.
- absolute `YYYY-MM-DD` → use as-is (validate with `date -j -f %Y-%m-%d "<d>" +%F`).
- day + month name ("3 мая" / "3 may") → map RU/EN month → MM, build
  `<thisYear>-MM-DD`; if that is strictly before today, roll to next year. Validate.
  If the year is still ambiguous to the user's intent, ONE `AskUserQuestion`.
- **Past date** (back-dating) is allowed, but warn in one line: `(дата в прошлом —
  пишу задним числом)`.

RU month map: января=01 февраля=02 марта=03 апреля=04 мая=05 июня=06 июля=07
августа=08 сентября=09 октября=10 ноября=11 декабря=12 (also nominative май, март…).

## Edge cases

- Year/month dirs missing → always `mkdir -p` before Write (Write does not guarantee
  parent dirs).
- Carryover across a weekend → the 5-day back-scan handles it (don't hardcode "yesterday").
- Re-running `today` the same day → idempotent: do not duplicate carried items; do not
  recreate the page.
- Filename `YYYY-MM-DD.md` is unique vault-wide, so `[[2026-06-26]]` wikilinks resolve
  despite the nested folder; the Calendar plugin still renders month/year on top.
- Do NOT touch `## Сделано` — that is `/daily`'s section.
- Incidents here are pointers only; the real post-mortem is `/incident` → `wiki/incidents/`.

## Anti-patterns

- ❌ Synthesizing the day's status yourself — that is `/daily` (it fills `## Сделано`).
- ❌ Dateless "remind me to …" capture — that is `/backlog`.
- ❌ Writing the full incident write-up into the date page — link to `/incident` output.
- ❌ A 5-question pre-flight for a one-line capture — fast-path, ask only on ambiguity.
- ❌ Allocating a `c-NNNNNN` address for a date page — `type: daily` is exempt.
