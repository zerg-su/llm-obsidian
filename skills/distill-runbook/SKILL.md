---
name: distill-runbook
version: 1.0.0
description: >-
  Distill this session's shell commands (.vault-meta/command-log.jsonl, PostToolUse capture) into a human-executable runbook in wiki/runbooks/ — AI-outage resilience: процедуры живут как copy-paste bash без ИИ. Triggers: distill runbook, runbook from session, сделай ранбук из сессии, ранбук из команд, сохрани команды сессии, дистиллируй ранбук.
allowed-tools: Read Grep Glob Bash Write Edit AskUserQuestion
---

# distill-runbook: Session Commands → Human-Executable Runbook

Every Bash call in every session is captured (sanitized) into
`.vault-meta/command-log.jsonl` by the PostToolUse hook:
`{ts, session_id, cwd, command, is_error}`. This skill turns the current
session's command trail into a runbook a HUMAN can execute without AI —
the vault's insurance policy for «ИИ отключился».

## Phase 1 — Collect

1. Get the current session's commands:
   ```bash
   SESSION_ID=$(./scripts/current-session-id.sh)
   grep "$SESSION_ID" .vault-meta/command-log.jsonl
   ```
   If the helper returns `unknown`, take the tail of the file and confirm the range
   with the user («команды с HH:MM по HH:MM — они?»).
2. **Filter noise** (drop): pure navigation/inspection (`cd`, `ls`, `pwd`,
   `cat`, `head`, `tail`, `grep`, `find`, `echo`, `which`, `wc`), repeated
   retries of the same command (keep the final successful form), one-off
   debugging probes that didn't shape the outcome.
3. **Keep**: state-changing commands (deploys, restarts, chown/chmod, git
   operations, ansible/kubectl/aws CLI actions), verification commands that
   PROVE a step worked (status checks between steps), and instructive
   failures (`is_error: true` followed by the corrected form — these become
   «гоча» callouts).

## Phase 2 — Draft (smart fast-path, /save-style)

1. Group the kept commands into ordered procedure steps; give each step a
   one-line intent («зачем»), the command block, and the expected output.
2. Infer: runbook title (imperative, Title Case), tags, related pages
   (Grep the wiki for pages mentioning the same hosts/services).
3. Show a one-line plan:
   `Ранбук: wiki/runbooks/<Title>.md — N шагов, M команд, гочи: K`
   Proceed unless the user objects; ask ONE AskUserQuestion only if scope is
   genuinely ambiguous (e.g. two unrelated procedures in one session — which
   one?).

## Phase 3 — Write

1. Allocate address: `./scripts/allocate-address.sh`.
2. `Write` the runbook. Frontmatter per vault convention (`type: runbook`,
   `status: stable`, `sessions:` provenance, `last_validated: <today>` —
   the session that produced the commands IS the validation run).
   **Session refs = ID, never a bare date.** `sessions:` frontmatter AND any
   provenance / validation prose in the body reference the session by its **ID**
   (`./scripts/current-session-id.sh`), ideally as **date + ID**. A bare date is
   not resumable (`claude --resume <id>`) nor grep-findable in `command-log.jsonl`.
   Body requirements (this is the AI-less contract):
   - every command copy-paste ready, full paths, explicit hosts/IPs (from
     the log's `cwd` and command args) — no «подставь свой хост»;
   - expected output / success check after each step;
   - «What NOT to do» section if instructive failures were captured;
   - zero steps that require asking an AI.
3. Bookkeeping via the dispatcher (one call, caps enforced):
   ```bash
   python3 scripts/vault-write.py <<'PAYLOAD'
   {"log_entry": "## [YYYY-MM-DD] distill-runbook | <Title>\n\n`c-NNNNNN` [[<Title>]]. Дистиллирован из N команд сессии <sid>: <one-line что за процедура>.",
    "hot_bullet": "YYYY-MM-DD: runbook [[<Title>]] — <essence> (`c-NNNNNN`)"}
   PAYLOAD
   ```
4. Confirm: «Ранбук [[<Title>]] готов, N шагов. `last_validated: <today>`.»

## Panic tier (optional)

If the user calls the procedure critical («panic», «критический», restore/
revive/outage class), add `tier: panic` to frontmatter. Panic runbooks are
lint-enforced by `validate-vault.py`: `last_validated` ≤ 180 days and no
"ask Claude" steps — expect a re-drill nudge when stale.

## What NOT to do

- Do not include commands from other sessions without confirming.
- Do not invent commands that are not in the log (paraphrasing flags is
  inventing — copy the exact successful form).
- Do not inline secrets: the log is pre-sanitized (`REDACTED` markers stay
  as placeholders with a comment «см. secret store»).
- Do not file trivial sessions (< ~5 meaningful commands) — decline and say
  why.
