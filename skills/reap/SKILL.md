---
name: reap
version: 1.1.0
description: |
  Symmetric inverse of /dispatch: collect "## Wiki Summary" from a cmux task-split, file into wiki/, update index/log/hot, allocate address, preserve .task-meta.json provenance. Modes (mandatory pre-flight): interim (keep split+worktree, safe default) / final (close split + remove worktree).
  Use when: task-Claude produced a Wiki Summary block, or /reap directly.
  Triggers (EN): reap, rip, close task-split, collect task summary, file task into wiki.
  Triggers (RU): зафиксируй task, сними task в вики, закрой task-split, финализируй task.
allowed-tools: Read Write Edit Glob Grep Bash AskUserQuestion
---

# /reap — filing the task result into the wiki

After the task Claude in a cmux task split finishes its work and produces a `## Wiki Summary` block, `/reap` collects that block (file-first, `cmux read-screen` fallback), files it into the wiki by the /save rules (3 phases), and offers cleanup (close the split surface, remove the worktree).

## Input

```
/reap <task-name>
```

or just `/reap` if there is exactly one active task split — resolved from the latest `dispatch | <name>` entry in `wiki/log.md`.

---

## Phase 0: Pre-flight (mandatory — AskUserQuestion)

`/reap` has **two modes** and MUST ask the user before filing:

1. **interim** — file the summary into the wiki NOW, **keep the split + worktree**; the task Claude keeps working. Useful for intermediate saves: phase 1 of a big task is done, we want to record the findings, but phase 2 continues in the same task.
2. **final** — file the summary + close the split + remove the worktree. The standard "task done".

**Default = interim** (safer — no work is lost). The user confirms the choice explicitly.

```python
AskUserQuestion(
    question="Reap mode for task-<name>?",
    options=[
        {"label": "Interim save (Recommended)",
         "description": "File the summary into the wiki, KEEP the split + worktree. The task continues."},
        {"label": "Final reap",
         "description": "File + close the split + remove the worktree. The standard 'task done'."},
    ],
)
```

If the user specified the mode in the command (`/reap final <name>` or `/reap interim`) — skip the mode pre-flight, but still ask on a session mismatch (see Phase 1.1b).

---

## Phase 1: Fetch + Plan (no writes)

### 1.1 Resolve the task name

If an argument was given — take it as `<task-name>`.

If not — `Grep -m 1 "## \[.*\] dispatch \|" wiki/log.md` → parse `<task-name>` from the latest entry. Not found — ask the user.

### 1.1b Read .task-meta.json + session-mismatch check

Before reading the summary — read `<worktree>/.task-meta.json` (created by /dispatch):

```bash
if [ -f "$WORKTREE/.task-meta.json" ]; then
    META=$(cat "$WORKTREE/.task-meta.json")
    ORIGIN_SESSION=$(echo "$META" | python3 -c "import json,sys;print(json.load(sys.stdin).get('origin_session',''))")
    SUGGESTED_AGENTS=$(echo "$META" | python3 -c "import json,sys;a=json.load(sys.stdin).get('suggested_agents',[]);print(','.join(x['name'] for x in a))")
    SPAWNED_AT=$(echo "$META" | python3 -c "import json,sys;print(json.load(sys.stdin).get('spawned_at',''))")
    PLAN_FILE=$(echo "$META" | python3 -c "import json,sys;print(json.load(sys.stdin).get('plan_file') or '')")
fi
```

`PLAN_FILE` (if non-empty and the file exists) — the plan /dispatch handed to the split: on **mode=final** close the loop in Phase 2 via `plan_close` (status executed + link to the result + exec session). On interim — leave it alone.

**Cross-session mismatch warning:**

```python
if ORIGIN_SESSION and ORIGIN_SESSION != os.environ.get("CLAUDE_CODE_SESSION_ID"):
    AskUserQuestion(
        question=f"This task was dispatched from session {ORIGIN_SESSION[:8]}... at {SPAWNED_AT}. You are session {curr[:8]}.... Continue filing?",
        options=[
            {"label": "Yes, continue (Recommended)",
             "description": "Both sessions go into the frontmatter (sessions: [<orig>, <current>])."},
            {"label": "No, abort",
             "description": "Do not file. Worth checking this is the right task."},
        ],
    )
```

If `.task-meta.json` is missing (an old dispatch or a crashed one) — warning only, not a blocker. Continue filing with the current session as the only entry in the `sessions:` frontmatter.

Keep `SUGGESTED_AGENTS` for the Phase 1.6 frontmatter generation.

### 1.2 Fetch the summary (file-first, screen-fallback)

**Priority — `.task-summary.md`** in the worktree, placed there by the task Claude's `/reap-send` RPC. It contains a clean `## Wiki Summary` block with no shell wrapping or scrollback noise.

```bash
WORKTREES_DIR="${LLM_OBSIDIAN_WORKTREES:-$HOME/Projects/worktrees}"
WORKTREE="$WORKTREES_DIR/<repo>-<task-name>"
if [ -f "$WORKTREE/.task-summary.md" ]; then
    cat "$WORKTREE/.task-summary.md"
else
    # Fallback: cmux read-screen for when the task Claude did not use /reap-send
    SURFACE_ID=$(cat "$WORKTREE/.task-cmux-surface" 2>/dev/null)
    cmux read-screen --surface "$SURFACE_ID" --scrollback --lines 500 2>&1
fi
```

**When `.task-summary.md` is used** (RPC mode — the normal path):
- the task Claude wrote the file via `/reap-send` and injected `/reap` into the wiki split via `cmux send`
- the wiki Claude (us, here) received that injected `/reap` as "user" input
- read the file; no cmux read-screen magic needed

**When the fallback is used** (legacy path):
- `.task-summary.md` is absent — the task Claude printed the block straight into its chat
- read via read-screen and parse from the output

If both sources are empty — stop:
```
Neither .task-summary.md nor the surface output produced a `## Wiki Summary` block.
Switch to the task split and say /reap-send (or print the block by hand
and /reap here).
```

### 1.3-1.4 Extract and parse the block (deterministically, by script)

Pipe the fetched output (file or read-screen) through the parser — do NOT dissect the block by hand:

```bash
python3 scripts/parse-wiki-summary.py --file "$WORKTREE/.task-summary.md"
# fallback path: cmux read-screen --surface "$SURFACE_ID" --scrollback --lines 500 | python3 scripts/parse-wiki-summary.py
```

stdout — JSON `{type, title, session, body}`. The rules live in the script: the **last** `## Wiki Summary` wins, `type` is validated against the enum (session|decision|runbook|incident|service-update|repo-touch), `title` must be non-empty and not a `<placeholder>`.

- **Exit 2** = the block is broken/missing — relay the stderr reason into the task split ("finalize the task and produce a Wiki Summary block as described in .task-prompt.md"). Stop, do not file.
- `session` → `EXEC_SESSION` — the executor session (the task Claude in the split; NOT equal to origin_session or the current reap session). Goes into the `sessions:` of the result page and the plan page. `null` (an old split; the script prints a stderr warning) — continue without it, note it in the echo.
- `type` — determines the folder and the behavior (new page vs update); `title` — the filename and H1; `body` — the markdown content.

### 1.5-1.6 Routing + frontmatter

**Read** `references/filing-rules.md` — the full routing table (type -> target path -> new/update) and the frontmatter schema (address via `./scripts/allocate-address.sh`, sessions provenance from `.task-meta.json` + current, suggested_agents, interim=developing / final=per pre-flight).

### 1.7 Pre-fetch (update-mode only)

- `Read <target-page-path>` (if update-mode). No need to read log/hot/index — `scripts/vault-write.py` does the bookkeeping.

### 1.8 Draft payloads

Prepare **in your head** before the Write phase:

1. **The new or updated page** — full markdown with frontmatter and body.
2. **vault-write payload** (a single JSON):
   - `log_entry`: prepend a full prose entry:
     ```
     ## [YYYY-MM-DD] reap | <task-name>

     `c-NNNNNN` [[Title]]. Long-form summary of what was done in task-<name>: which changes in repo <repo>, which commits (by hash or essence), which [[wikilinks]] to adjacent pages. History compounds — long is fine, this is a journal.
     ```
   - `hot_bullet`: `YYYY-MM-DD: [[Title]] — one-sentence essence (\`c-NNNNNN\`)` (the script evicts beyond 15 and bumps `updated:` itself).
3. **`wiki/index.md`** — do NOT touch (a thin map; folder listings are autogenerated by reindex). A manual link only if the page became a key hub of its domain.
4. **`wiki/hot.md`** directly — do NOT touch at all (including the frontmatter `related:`).

### 1.9 Echo-confirm to the user

```
Ready to file:
  type:     decision
  title:    Static search index for the blog
  path:     wiki/decisions/Static search index for the blog.md
  address:  c-000130
  index:    will add to the Decisions section
  log:      prepend entry (~5 lines)
  hot:      one-line bullet in Recent Changes, bump updated:
  plan:     wiki/plans/<file>.md → executed + link + exec session (plan_close)   # only mode=final with plan_file
File it?
```

Wait for "yes / no / edit". No Write without explicit consent.

---

## Phase 2: Write (batched, single assistant message)

Two tool calls **in parallel in one assistant message**:

- `Write <target-page-path>` — the new page (or `Edit` in update-mode).
- `Bash`: a single payload into the dispatcher (plan closure also happens here, NOT via a manual Edit):
  ```bash
  python3 scripts/vault-write.py <<'PAYLOAD'
  {"log_entry": "## [YYYY-MM-DD] reap | <task-name>\n\n`c-NNNNNN` [[Title]]. ...",
   "hot_bullet": "YYYY-MM-DD: [[Title]] — essence (`c-NNNNNN`)",
   "plan_close": {"file": "wiki/plans/<file>.md", "result_link": "[[Title]]", "exec_session": "<EXEC_SESSION|null>"}}
  PAYLOAD
  ```
  Include `plan_close` ONLY on mode=final with a `PLAN_FILE` present (interim / no plan — omit the key). The script itself does: `status: pending → executed`, bump `updated:`, the exec session into `sessions:` (the plan carries BOTH sessions — planning and executing), and a `Result: <link> (reaped ...)` line at the end of the body.

  Exit 2 = a violation with a reason (Active Threads > 8; the plan already executed / not pending — "wrong plan_file?") — fix the payload and rerun; do NOT bypass with direct Edits.

**Do not split across multiple turns.** The Stop hook (autocommit) fires once at the end of the turn — one tidy commit, not five.

---

## Phase 3: Cleanup (only if mode=final)

**If mode=interim** (chosen in Phase 0): skip Phase 3 entirely. Short output:

```
Saved as [[<Title>]] in wiki/<folder>/ (interim — task continues).

Task split <surface-id> and worktree <path> stay active.
Keep working there; call /reap again with mode=final when the task is done.
```

**If mode=final** — the standard Phase 3:

```
Saved as [[<Title>]] in wiki/<folder>/.

Task split <surface-id>:
  - repo changes committed: <yes/no based on `git -C <worktree> status --short`>
  - suggested:
      git -C <worktree> log --oneline -5         # review your commits
      rm <worktree>/.task-summary.md             # clear the handoff file (optional)
      rm <worktree>/.task-meta.json              # clear the meta file (optional)
      cmux close-surface --surface <surface-id>  # close the split surface (the wiki Claude stays)
      git worktree remove <worktree-path>        # remove the worktree

The branch task/<task_name> stays local — push when ready, by hand.

Run the cleanup?
```

On "yes" — run `cmux close-surface` + `git worktree remove` via Bash.

**Note**: if a destructive-command guard blocks `git worktree remove`, do not work around it — give the user the exact command to run manually in their terminal.

The branch is **never deleted**, even with consent — a safe default; the branch may be needed for a PR / push.

### If the worktree is dirty (unsaved edits)

```
The worktree has unsaved changes:
  <git status --short output>

Leaving the worktree as is — sort it out in the task split.
Deferring the cmux surface close too — it may hold needed context.
```

---

## Edge cases

1. **Several task splits open at once, /reap with no argument** — resolving by the latest dispatch may pick the wrong task. If the last 2-3 dispatches in log.md are not reaped yet — ask "which one exactly?".
2. **Wiki Summary with an unknown type** — ask the user, defaulting to `session`.
3. **Title collision** with an existing page of a different type — add a date suffix `<Title> <YYYY-MM-DD>.md`.
4. **Address allocation failure** — if `./scripts/allocate-address.sh` fails (busy / lock) — report; do not file a page without an address (it breaks lint).
5. **Service/repo update without an existing page** — switch to "new" mode, create a stub.
6. **The task Claude produced a summary with broken frontmatter in the body** — ignore its frontmatter, synthesize your own by the rules.
7. **The surface was closed before /reap** — `.task-cmux-surface` points to a non-existent ID. Report and suggest pasting the block by hand.
8. **The surface ID changed** (the user moved the split) — `cmux read-screen --surface <id>` is usually stable by ID. If it fails — `cmux list-pane-surfaces` and reconcile with the user.

---

## Do not

- Do NOT delete the branch (`git branch -D`) automatically — only the worktree.
- Do NOT close the cmux surface before the user has confirmed.
- Do NOT use `cmux close-workspace` (it would close the wiki Claude together with the task split) — only `close-surface --surface <id>`.
- Do NOT duplicate the summary across `log.md` and `hot.md` — long prose only in the new page + log.md; hot.md = one line.
- Do NOT read a `## Wiki Summary` block older than the latest in the transcript (the task Claude may reprint it after edits).
- Do NOT touch the `wiki/hot.md` frontmatter `related:` (curated).
