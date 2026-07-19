---
name: reap
metadata:
  version: 1.2.0
description: |
  Finalize a Claude/Codex cmux task: collect its typed summary, file it through the vault transaction, preserve provenance, and close safely.
  Triggers: reap, finalize task, collect task summary, зафиксируй task, финализируй task.
allowed-tools: Read Write Edit Glob Grep Bash AskUserQuestion
---

# /reap — filing the task result into the wiki

After the task agent produces a typed summary, `/reap` files it through the
single writer. Approved v2 unattended tasks use contract-bound final filing,
validation, and armed exact-surface close; legacy tasks remain interactive.

## Canonical v3 final runner

When `.task-meta.json` is v3, `interaction_policy=unattended`, and the command
is final, do not reproduce Phases 1–3 manually. Run once:

```bash
python3 <vault-root>/scripts/reap-runner.py \
  --vault-root <vault-root> --worktree <exact-worktree>
```

The runner validates the typed summary and exact session/plan/reap policy,
archives every completed broker review, appends only validated review links,
selects the collision-safe result route, allocates provenance/frontmatter,
prepares the plan close, performs one `vault-write.py` transaction, reindexes,
validates, completes the broker task, and arms exit for only the exact task
surface. It emits content-free duration telemetry. Any ambiguity,
executed-plan recovery, interactive/legacy task, unfinished review, update
conflict, or contract drift fails closed into the compatibility diagnostics
below; never retry a partially completed final reap blindly.

The remaining phases are the compatibility contract and recovery reference.

## Input

```
/reap <task-name>
```

or just `/reap` if there is exactly one active task split — resolved from the latest `dispatch | <name>` entry in `wiki/log.md`.

---

## Phase 0: Pre-flight (mandatory — AskUserQuestion)

Read `.task-meta.json` first. When v2 `interaction_policy=unattended`, require
`reap_policy.mode=final`, set final mode, and skip the mode question. Otherwise
`/reap` has two interactive modes:

1. **interim** — file the summary into the wiki NOW, **keep the split + worktree**; the task agent keeps working. Useful for intermediate saves: phase 1 of a big task is done, we want to record the findings, but phase 2 continues in the same task.
2. **final** — file the summary and mark the task done. Cmux surface is not closed; worktree cleanup is a separate explicit user action.

**Interactive default = interim.**

```python
AskUserQuestion(
    question="Reap mode for task-<name>?",
    options=[
        {"label": "Interim save (Recommended)",
         "description": "File the summary into the wiki, KEEP the split + worktree. The task continues."},
        {"label": "Final reap",
         "description": "File summary and mark task done. Do not close the cmux surface; worktree cleanup is separate."},
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
    WIKI_RUNTIME=$(echo "$META" | python3 -c "import json,sys;print(json.load(sys.stdin).get('wiki_runtime',''))")
    RUNTIME=$(echo "$META" | python3 -c "import json,sys;m=json.load(sys.stdin);print(m.get('executor_runtime') or m.get('runtime',''))")
    MODEL=$(echo "$META" | python3 -c "import json,sys;m=json.load(sys.stdin).get('model');print(m or '')")
    PLAN_FILE=$(echo "$META" | python3 -c "import json,sys;print(json.load(sys.stdin).get('plan_file') or '')")
    APPROVED_PLAN_SHA256=$(echo "$META" | python3 -c "import json,sys;print(json.load(sys.stdin).get('approved_plan_sha256') or '')")
fi
```

`PLAN_FILE` (if non-empty and the file exists) — the plan /dispatch handed to the split: on **mode=final** close the loop in Phase 2 via `plan_close` (status executed + link to the result + exec session). On interim — leave it alone.

**Cross-session mismatch:** unattended mode fails closed and leaves the task
surface open. Interactive mode uses the warning question below.

```python
curr = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "unknown"
if ORIGIN_SESSION and ORIGIN_SESSION != curr:
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

If `.task-meta.json` is missing (an old dispatch or a crashed one) — warning only, not a blocker. Continue filing with the current session (`./scripts/current-session-id.sh`) as the only entry in the `sessions:` frontmatter.

Keep `WIKI_RUNTIME`, `RUNTIME`, `MODEL`, and `SUGGESTED_AGENTS` for echo/frontmatter generation.

### 1.2 Fetch the summary (file-first, screen-fallback)

**Priority — `.task-summary.json`** in the worktree, placed there by the task agent's `/reap-send` RPC. It is the canonical validated v1 contract. `.task-summary.md` is the legacy/derived fallback.

```bash
WORKTREES_DIR="${LLM_OBSIDIAN_WORKTREES:-$HOME/Projects/worktrees}"
WORKTREE="$WORKTREES_DIR/<repo>-<task-name>"
if [ -f "$WORKTREE/.task-summary.json" ]; then
    python3 scripts/parse-wiki-summary.py --json-file "$WORKTREE/.task-summary.json"
elif [ -f "$WORKTREE/.task-summary.md" ]; then
    cat "$WORKTREE/.task-summary.md"
else
    # Fallback: cmux read-screen for when the task agent did not use /reap-send
    SURFACE_ID=$(cat "$WORKTREE/.task-cmux-surface" 2>/dev/null)
    cmux read-screen --surface "$SURFACE_ID" --scrollback --lines 500 2>&1
fi
```

**When `.task-summary.json` is used** (RPC mode — the normal path):
- the task agent wrote the file via `/reap-send` and injected `/reap` / `$llm-obsidian:reap` into the wiki split via `cmux send`
- the wiki agent (us, here) received that injected command as "user" input
- read the file; no cmux read-screen magic needed

**When the fallback is used** (legacy path):
- `.task-summary.md` is absent — the task agent printed the block straight into its chat
- read via read-screen and parse from the output

If both sources are empty — stop:
```
Neither .task-summary.json/.md nor the surface output produced a valid Wiki Summary.
Switch to the task split and say /reap-send / $llm-obsidian:reap-send (or print the block by hand
and /reap here).
```

### 1.3-1.4 Extract and parse the block (deterministically, by script)

Pipe the fetched output (file or read-screen) through the parser — do NOT dissect the block by hand:

```bash
python3 scripts/parse-wiki-summary.py --json-file "$WORKTREE/.task-summary.json"
# legacy fallback only:
python3 scripts/parse-wiki-summary.py --file "$WORKTREE/.task-summary.md"
# fallback path: cmux read-screen --surface "$SURFACE_ID" --scrollback --lines 500 | python3 scripts/parse-wiki-summary.py
```

stdout — v1 JSON `{schema_version,type,title,session,body}`. The legacy Markdown parser still uses the last block, but new handoffs must use canonical JSON.

- **Exit 2** = the block is broken/missing — relay the stderr reason into the task split ("finalize the task and produce a Wiki Summary block as described in .task-prompt.md"). Stop, do not file.
- `session` → `EXEC_SESSION` — the executor session (the task agent in the split; NOT equal to origin_session or the current reap session). Goes into the `sessions:` of the result page and the plan page. `null` (an old split; the script prints a stderr warning) — continue without it, note it in the echo.
- `type` — determines the folder and the behavior (new page vs update); `title` — the filename and H1; `body` — the markdown content.

For unattended mode, now run:

```bash
CURRENT_SESSION=$(./scripts/current-session-id.sh)
python3 scripts/task_contract.py check-handoff \
  --meta "$WORKTREE/.task-meta.json" \
  --summary "$WORKTREE/.task-summary.json" \
  --current-session "$CURRENT_SESSION"
```

Any plan hash, origin session, type, title, or auto-file mismatch stops before
address allocation or vault writes.

### 1.4b Archive cross-model review history

For v3 metadata, archive every exact broker review operation from the
**coordinator vault** before drafting the result page:

```bash
REVIEW_ARCHIVES=$(python3 scripts/archive_task_reviews.py \
  --worktree "$WORKTREE" \
  --vault-root "$(git rev-parse --show-toplevel)")
```

The returned JSON `markers` array contains the exact operation-scoped approved
archive markers. Pass each marker to `parse-wiki-summary.py` with a repeated
`--review-archive-marker`; never select one by task name or recency. Terminal
failed cycles are listed in `failed_operations` and remain auditable broker
records, but they do not count as approval: at least one approved durable review
archive is required. Queued, running, or unarchived completed reviews
still block final reap.

For legacy v1/v2 metadata, if `$WORKTREE/.review-meta.json` exists, keep the
compatibility command:

```bash
python3 skills/review-dispatch/scripts/spawn_review.py archive \
  --worktree "$WORKTREE" \
  --vault-root "$(git rev-parse --show-toplevel)"
```

`archived` and `already-current` are success. `no-review`, invalid history, or
writer failure blocks a final reap: a started review must not disappear into a
one-line summary. The command is idempotent and writes the contentful page under
`wiki/meta/reviews/` through `vault-write.py`; it consumes any deferred
`.review-archive-request.json` and creates `.review-archive.json` in the
worktree. The bounded original task-description section is kept for context;
raw orchestration/reviewer prompts, payload tokens, command logs, sockets, and
cmux IDs are not archived. If `.review-meta.json` is absent, continue without
an archive.

Then parse the canonical summary again with every generated marker:

```bash
python3 scripts/parse-wiki-summary.py \
  --json-file "$WORKTREE/.task-summary.json" \
  --review-archive-marker <exact-marker-1> \
  --review-archive-marker <exact-marker-2-if-present>
```

For the legacy Markdown path, use the same marker with `--file`. The parser
validates that the marker points only into `wiki/meta/reviews/` and appends one
deterministic `Review archive: [[...]]` line to the result body. Never handcraft
or trust a reviewer-provided wikilink.

### 1.5-1.6 Routing + frontmatter

**Read** `references/filing-rules.md` — the full routing table (type -> target path -> new/update) and the frontmatter schema (address via `./scripts/allocate-address.sh`, sessions provenance from `.task-meta.json` + current, executor_runtime/model, suggested_agents, interim=developing / final=per pre-flight).

### 1.7 Pre-fetch (update-mode only)

- `Read <target-page-path>` (if update-mode). No need to read log/hot/index — `scripts/vault-write.py` does the bookkeeping.

### 1.8 Draft payloads

Prepare **in your head** before the Write phase:

1. **The new or updated page operation** — full markdown with frontmatter and body. In update-mode capture `python3 scripts/vault-write.py --sha256 <target>` and include it as `expected_sha256`.
2. **vault-write payload** (a single JSON):
   - `log_entry`: prepend a full prose entry:
     ```
     ## [YYYY-MM-DD] reap | <task-name>

     `c-NNNNNN` <RESULT_LINK>. Long-form summary of what was done in task-<name>: which changes in repo <repo>, which commits (by hash or essence), which [[wikilinks]] to adjacent pages. History compounds — long is fine, this is a journal.
     ```
   - `hot_bullet`: `YYYY-MM-DD: <RESULT_LINK> — one-sentence essence (\`c-NNNNNN\`)` (the script evicts beyond 15 and bumps `updated:` itself).
3. **`wiki/index.md`** — do NOT touch (a thin map; folder listings are autogenerated by reindex). A manual link only if the page became a key hub of its domain.
4. **`wiki/hot.md`** directly — do NOT touch at all (including the frontmatter `related:`).

### 1.9 Echo-confirm to the user

Skip this section for a successful unattended contract check. The upfront
dispatch approval is the file authorization. Keep the echo for interactive
and legacy tasks:

```
Ready to file:
  type:     decision
  title:    Static search index for the blog
  path:     wiki/decisions/Static search index for the blog.md
  address:  c-000130
  runtime:  codex / gpt-5-codex
  index:    will add to the Decisions section
  log:      prepend entry (~5 lines)
  hot:      one-line bullet in Recent Changes, bump updated:
  plan:     wiki/plans/<file>.md → executed + link + exec session (plan_close)   # only mode=final with plan_file
File it?
```

Wait for "yes / no / edit" only in interactive mode.

---

## Phase 2: Write the task result (one transaction)

For unattended final mode, first bind the still-pending approved plan, current
summary, intended result path, and deterministic post-`plan_close` hash. This
must happen before the writer changes the plan:

```bash
python3 scripts/cmux_surface_lifecycle.py prepare-reap \
  --worktree "$WORKTREE" \
  --current-session "$CURRENT_SESSION" \
  --result-path "<absolute-created-or-updated-wiki-page>" \
  --vault-root "<vault-root>"
```

If preparation fails, do not write. The marker contains hashes and identifiers
only; `complete-reap` later rejects a changed summary, metadata, result route,
or plan that differs from the exact close transformation prepared here.
`prepare-reap` also detects a vault-wide filename collision and, for a new
target, deterministically routes it to `<Title> — Result.md`. Read
`result_path` and `result_link` back from `.task-reap-prepared.json` and use
those exact values for the page operation, log/hot entries, and `plan_close`;
the requested pre-prepare path is not authoritative.

Recovery after an older or failed reap may call `prepare-reap` when the plan
is already `executed`. In that case the marker also contains
`previous_closed_plan_sha256` and `previous_result_link`:

- if `previous_closed_plan_sha256 == closed_plan_sha256`, the rerun is
  idempotent and needs no plan mutation;
- if the hashes differ, do **not** call `plan_close` again. Read the executed
  plan, replace only its single exact `Результат: <previous_result_link>
  (reaped YYYY-MM-DD)` link with `<result_link>`, and include that full plan as
  a normal `pages` `op:update` in the same `vault-write.py` transaction, with
  `expected_sha256: <previous_closed_plan_sha256>`. The resulting content hash
  must equal `closed_plan_sha256`. Never edit the plan directly.

The review archive above is its own idempotent history transaction. Send the
task result page, log/hot, and optional plan closure in one separate dispatcher call:

  ```bash
  python3 scripts/vault-write.py <<'PAYLOAD'
  {"actor": "reap", "session": "<CURRENT_SESSION>",
   "pages": [{"op": "create", "path": "<result_path from marker, vault-relative>", "content": "<full markdown, JSON-escaped>"}],
   "log_entry": "## [YYYY-MM-DD] reap | <task-name>\n\n`c-NNNNNN` <result_link from marker>. ...",
   "hot_bullet": "YYYY-MM-DD: <result_link from marker> — essence (`c-NNNNNN`)",
   "plan_close": {"file": "wiki/plans/<file>.md", "result_link": "<exact result_link from .task-reap-prepared.json>", "exec_session": "<EXEC_SESSION|null>", "expected_sha256": "<APPROVED_PLAN_SHA256>"}}
  PAYLOAD
  ```
  Include `plan_close` ONLY on mode=final with a still-pending `PLAN_FILE`
  (interim / no plan / executed-plan recovery — omit the key). For a v2
  unattended task, include its `APPROVED_PLAN_SHA256` as `expected_sha256`; a
  concurrent or unprepared plan mutation then aborts the whole transaction
  before any page/log/hot write. The script itself does: `status: pending →
  executed`, bumps `updated:`, appends the exec session to `sessions:` (the
  plan carries BOTH sessions — planning and executing), and adds a
  `Результат: <link> (reaped ...)` line at the end of the body. For an
  already-executed recovery, use the optimistic `pages` update described
  above instead.

  Update-mode uses `op:update` plus `expected_sha256`. Exit 2 = cap/lifecycle violation; exit 4 = optimistic-concurrency conflict. Fix/re-read and rerun; do not bypass with direct Edits.

For unattended final mode, `prepare-reap` hashes the completed review marker.
`complete-reap` then revalidates the archive page hash and requires its exact
wikilink in the filed result page. A missing, changed, non-approved, or unlinked
review archive blocks close instead of silently losing the reasoning trail.

**Do not split across multiple turns.** The Stop hook (autocommit) fires once at the end of the turn — one tidy commit, not five.

For unattended final mode, synchronously run `python3 scripts/reindex.py` and
`scripts/validate-vault.py --summary` after the writer succeeds. If either
fails, leave the task surface open as `needs-attention`. If both pass, run:

```bash
python3 scripts/cmux_surface_lifecycle.py complete-reap \
  --worktree "$WORKTREE" \
  --current-session "$CURRENT_SESSION" \
  --result-path "<absolute-created-or-updated-wiki-page>" \
  --vault-root "<vault-root>"
python3 scripts/cmux_surface_lifecycle.py request-exit \
  --worktree "$WORKTREE" --kind task
```

`complete-reap` consumes the pre-write preparation, verifies the existing page
inside this vault's `wiki/`, and requires the plan to match the exact prepared
post-`plan_close` hash. Only then does it permit close arming. The task launch
wrapper closes the exact UUID only after its agent process returns. This never
deletes the worktree or branch.

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
  - unattended clean final: `/exit` is armed and its exact surface closes after process return
  - interactive/failed/dirty final: surface stays open for inspection
  - interactive-only suggested cleanup; unattended tasks keep worktree + branch:
      git -C <worktree> log --oneline -5         # review your commits
      rm <worktree>/.task-summary.json <worktree>/.task-summary.md  # optional handoff cleanup
      rm <worktree>/.task-meta.json              # clear the meta file (optional)
      git worktree remove <worktree-path>        # remove the worktree

The branch task/<task_name> stays local — push when ready, by hand.

Show cleanup commands?
```

**Note**: if a destructive-command guard blocks `git worktree remove`, do not work around it — give the user the exact command to run manually in their terminal.

The branch is **never deleted**, even with consent — a safe default; the branch may be needed for a PR / push.

### If the worktree is dirty (unsaved edits)

```
The worktree has unsaved changes:
  <git status --short output>

Leaving the worktree as is — sort it out in the task split.
Cmux surface is not closed — it may hold needed context.
```

---

## Edge cases

1. **Several task splits open at once, /reap with no argument** — resolving by the latest dispatch may pick the wrong task. If the last 2-3 dispatches in log.md are not reaped yet — ask "which one exactly?".
2. **Wiki Summary with an unknown type** — ask the user, defaulting to `session`.
3. **Title collision** with another Markdown filename — use the exact
   `result_path` / aliased `result_link` selected by `prepare-reap`. It normally
   chooses `<Title> — Result.md`; if that is also occupied, stop and choose an
   explicit unique route instead of weakening filename validation.
4. **Address allocation failure** — if `./scripts/allocate-address.sh` fails (busy / lock) — report; do not file a page without an address (it breaks lint).
5. **Service/repo update without an existing page** — switch to "new" mode, create a stub.
6. **The task agent produced a summary with broken frontmatter in the body** — ignore its frontmatter, synthesize your own by the rules.
7. **The surface was closed before /reap** — `.task-cmux-surface` points to a non-existent ID. Report and suggest pasting the block by hand.
8. **Surface ID/ref stale** — new dispatches write UUIDs, old ones may have written short `surface:N` refs. If `cmux read-screen --surface <id>` fails, do not guess a new target from surface lists and do not cleanup; confirm with the user or read `.task-summary.md`.

---

## Do not

- Do NOT delete the worktree or branch automatically; unattended tasks retain both.
- Do NOT call `cmux close-surface` directly. Only arm `request-exit`; the task
  wrapper closes its own persisted UUID after process return.
- Do NOT use `cmux close-workspace` (it would close the wiki agent together with the task split).
- Do NOT duplicate the summary across `log.md` and `hot.md` — long prose only in the new page + log.md; hot.md = one line.
- Do NOT read a `## Wiki Summary` block older than the latest in the transcript (the task agent may reprint it after edits).
- Do NOT touch the `wiki/hot.md` frontmatter `related:` (curated).
