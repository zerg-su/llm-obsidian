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
Records carry provenance/execution sessions, origin, and success/error/unknown.

## Phase 1 — Collect

1. Resolve both IDs. The helper returns task-origin wiki provenance; the
   environment retains the worker execution ID:
   ```bash
   PROVENANCE_SESSION=$(./scripts/current-session-id.sh)
   EXECUTION_SESSION=${CODEX_THREAD_ID:-${CLAUDE_CODE_SESSION_ID:-unknown}}
   python3 scripts/command_evidence.py sessions --provenance-session "$PROVENANCE_SESSION"
   python3 scripts/command_evidence.py collect --provenance-session "$PROVENANCE_SESSION" --execution-session "$EXECUTION_SESSION"
   ```
   Zero matches for a valid provenance ID is not empty: use its grouped worker
   IDs. If execution is unknown, choose the sole group or ask once if ambiguous.
   Never infer evidence from prompts.
2. Accept user commands only through this typed path (one object per command):
   ```bash
   python3 scripts/command_evidence.py ingest-user <<'JSON'
   {"schema_version":1,"command":"<exact command>","cwd":"<absolute cwd>","provenance_session":"<provenance ID>","origin":"user-reported","outcome":"success|error|unknown","result_excerpt":"<optional bounded excerpt>"}
   JSON
   ```
   This path sanitizes, bounds, rejects residual secrets, and deduplicates.
   User-reported is user-attested, never agent-verified; errors are gotchas.
3. PostToolUse without explicit exit/status remains `unknown`: discovery only,
   never validation. Prove checks with the code-owned runner and a unique ID:
   ```bash
   python3 scripts/command_evidence.py run-validation --run-id <id> --cwd "$PWD" -- <program> <args...>
   ```
   It runs argv without a shell, stores its exit code, and stores no output.
   Run `self-test-sanitization` through it; never search logs for literal secrets.
4. Drop navigation noise, irrelevant probes, and retries except the final good
   form. Keep state changes, proof checks, and corrected instructive errors.

## Phase 2 — Draft (smart fast-path, /save-style)

1. Group commands into ordered steps: intent, exact command, expected output.
   PostToolUse stores no output; label optional reported excerpts user-attested.
2. Infer imperative Title Case title, tags, and related wiki pages.
3. Show `Ранбук: wiki/runbooks/<Title>.md — N шагов, M команд, гочи: K`.
   Proceed unless objected; ask once only for ambiguous scope.

## Phase 3 — Write

1. Allocate address: `./scripts/allocate-address.sh`.
2. Frontmatter: `type: runbook`, provenance `sessions`. Use `status: stable` and
   `last_validated` only when successful `validation_grade` records prove the
   checks; otherwise `status: draft` with no `last_validated`. Name both IDs,
   keep commands copy-paste-ready, add per-step success checks and error gotchas,
   and require no AI. State N validation-grade agent checks, D unknown discovery
   commands from execution X, and M user-attested commands from provenance Y;
   user-attested outcomes are not agent-verified. Use IDs, never bare dates.
3. Write runbook and bookkeeping in one transaction:
   ```bash
   python3 scripts/vault-write.py <<'PAYLOAD'
   {"actor":"distill-runbook","session":"<provenance_session>",
    "pages":[{"op":"create","path":"wiki/runbooks/<Title>.md","content":"<full markdown, JSON-escaped>"}],
    "log_entry":"## [YYYY-MM-DD] distill-runbook | <Title>\n\n`c-NNNNNN` [[<Title>]]. N commands; provenance <provenance_session>; execution <execution_session>; A agent-executed + U user-attested: <summary>.",
    "hot_bullet":"YYYY-MM-DD: runbook [[<Title>]] — <essence> (`c-NNNNNN`)"}
   PAYLOAD
   ```
4. Confirm path, step count, status, and `last_validated` only when present.

## Panic tier (optional)

For critical restore add `tier: panic`; lint requires `last_validated` ≤180 days
and forbids “ask Claude” steps.

## What NOT to do

- Do not mix execution sessions without confirmation or lose provenance.
- Do not invent commands, mine prompts, label user reports agent-verified, or
  treat `unknown` as proof/set `last_validated` from it.
- Keep `REDACTED` placeholders with «см. secret store»; never inline secrets.
- Decline trivial sessions with fewer than about five meaningful commands.
