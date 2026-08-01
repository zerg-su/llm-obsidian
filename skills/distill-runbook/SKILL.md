---
name: distill-runbook
metadata:
  version: 1.0.0
description: >-
  Distill this session's shell commands (.vault-meta/command-log.jsonl, PostToolUse capture) into a human-executable runbook in wiki/runbooks/ — AI-outage resilience: процедуры живут как copy-paste bash без ИИ. Triggers: distill runbook, runbook from session, сделай ранбук из сессии, ранбук из команд, сохрани команды сессии, дистиллируй ранбук.
allowed-tools: Read Grep Glob Bash Write Edit AskUserQuestion
---

# distill-runbook: Session Commands → Human-Executable Runbook

Turn sanitized command evidence into a runbook a HUMAN can execute without AI.
Records distinguish resumable wiki `provenance_session` from worker
`execution_session`, label `origin` (`agent-executed` or `user-reported`), and
carry `outcome` (`success`, `error`, or `unknown`).

## Phase 1 — Collect

1. Resolve both identities. In task worktrees the helper intentionally returns
   the task-origin ID for wiki provenance; the environment retains the worker ID:
   ```bash
   PROVENANCE_SESSION=$(./scripts/current-session-id.sh)
   EXECUTION_SESSION=${CODEX_THREAD_ID:-${CLAUDE_CODE_SESSION_ID:-unknown}}
   python3 scripts/command_evidence.py sessions --provenance-session "$PROVENANCE_SESSION"
   python3 scripts/command_evidence.py collect --provenance-session "$PROVENANCE_SESSION" --execution-session "$EXECUTION_SESSION"
   ```
   Zero direct matches for a valid provenance ID is not an empty session: use
   its grouped execution IDs. If execution is `unknown`, choose the sole group;
   ask once if several are ambiguous. Never infer evidence from prompt text.
2. Accept user commands only through this typed path (one object per command):
   ```bash
   python3 scripts/command_evidence.py ingest-user <<'JSON'
   {"schema_version":1,"command":"<exact command>","cwd":"<absolute cwd>","provenance_session":"<provenance ID>","origin":"user-reported","outcome":"success|error|unknown","result_excerpt":"<optional bounded excerpt>"}
   JSON
   ```
   It sanitizes, size-checks, rejects residual secrets, and deduplicates.
   User-reported means user-attested, never agent-verified. `unknown` is not
   proof; `error` is a gotcha.
3. Drop navigation/inspection noise (`cd`, `ls`, `pwd`, `cat`, `head`, `tail`,
   `grep`, `find`, `echo`, `which`, `wc`), retries except the final successful
   form, and irrelevant probes. Keep state changes, checks that prove a step,
   and instructive errors followed by corrections.

## Phase 2 — Draft (smart fast-path, /save-style)

1. Group commands into ordered steps: intent, exact command, expected output.
   PostToolUse stores no output. Use optional reported excerpts only when labeled
   user-attested.
2. Infer imperative Title Case title, tags, and related wiki pages.
3. Show `Ранбук: wiki/runbooks/<Title>.md — N шагов, M команд, гочи: K`.
   Proceed unless objected; ask once only for genuinely ambiguous scope.

## Phase 3 — Write

1. Allocate address: `./scripts/allocate-address.sh`.
2. Draft frontmatter (`type: runbook`, `status: stable`,
   `sessions: [<provenance ID>]`, `last_validated: <today>`). Body names both
   IDs, uses copy-paste-ready commands/full paths/explicit hosts, gives a success
   check per step, adds What NOT to do for errors, and has zero AI-dependent
   steps. Include: “Validation evidence: N agent-executed commands from execution
   session X; M user-attested commands for provenance session Y. User-attested
   outcomes were not agent-verified.” Use IDs, never bare dates.
3. Write runbook and bookkeeping in one transaction:
   ```bash
   python3 scripts/vault-write.py <<'PAYLOAD'
   {"actor":"distill-runbook","session":"<provenance_session>",
    "pages":[{"op":"create","path":"wiki/runbooks/<Title>.md","content":"<full markdown, JSON-escaped>"}],
    "log_entry":"## [YYYY-MM-DD] distill-runbook | <Title>\n\n`c-NNNNNN` [[<Title>]]. N commands; provenance <provenance_session>; execution <execution_session>; A agent-executed + U user-attested: <summary>.",
    "hot_bullet":"YYYY-MM-DD: runbook [[<Title>]] — <essence> (`c-NNNNNN`)"}
   PAYLOAD
   ```
4. Confirm: `Ранбук [[<Title>]] готов, N шагов. last_validated: <today>.`

## Panic tier (optional)

For critical/panic restore procedures add `tier: panic`; lint then requires
`last_validated` ≤ 180 days and forbids “ask Claude” steps.

## What NOT to do

- Do not mix execution sessions without confirmation or lose their provenance.
- Do not invent/paraphrase commands, mine prompts, present user reports as
  agent-verified, or treat `unknown` as proof.
- Keep `REDACTED` placeholders with «см. secret store»; never inline secrets.
- Decline trivial sessions with fewer than about five meaningful commands.
