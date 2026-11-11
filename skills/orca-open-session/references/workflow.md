# Resolve and open

`ORCA` below means the executable selected by the installed `orca-cli` skill.
Pass every connection explicitly, including `--environment local`, so inherited
pairing variables cannot silently reroute commands. Remote names are discovered,
never built into this skill. Use saved environment IDs for durable identity.

## Find the tuple

- Topic: inspect relevant wiki provenance using `rg`/indexed metadata, then
  `ORCA search --index-status --environment <id> --json` before each connection's
  first `ORCA search "<distinctive words>" --environment <id> --json`.
- Exact ID: do not require two keywords or impose find-session's 30-day window.
  Join the full ID to wiki provenance/history and Orca search candidate metadata.
  Search indexes contain conversation text, so searching an ID is not guaranteed
  to find it. Read-only exact-ID lookup is available through
  `node scripts/orca-open-session.cjs --catalog <environment-id> --session-id <full-id>`
  on each selected connection. It checks the installed catalog schema, host
  version and `aiVault.v1` capability, then filters metadata by the exact ID.
  A capped/incomplete catalog cannot prove absence; report that limit. Never glob local provider files as evidence for a
  remote host. If agent/host/workspace cannot be established, ask for the missing
  tuple rather than launching the ID at the current directory.
- Do not treat a structured Orca chat ID, filename or shortened display ID as
  the provider ID. Claude `history.jsonl` has `sessionId`; Codex session metadata
  identifies its thread/session. Neither alone proves the remote execution host.
- Inspect `ORCA worktree ps --environment <id> --json` (increase `--limit` if
  truncated), then `worktree show --environment <id> --worktree <exact> --json`.
  Use `address.environment` and `address.worktree` from that connection's result.
  Bind `_meta.runtimeId`, `worktree.id` and `worktree.hostId` too. A path/name is
  only a discovery hint. A null address or inconsistent host is a blocker.
- Inspect existing agent/terminal surfaces in that exact workspace. Prefer an
  exact provider binding already open. If an existing native chat owns this
  conversation, use its documented focus operation if available; otherwise
  report the focus limitation, rather than opening a second writable instance.
  An agent type or title alone is not a provider-session binding.

## Current internal fallback

Inspected installation: Orca 1.4.256. This observation is not a universal API.
The helper imports that installation's `ExplicitEnsure` schema and
`getAgentResumeArgv` implementation before use. Both Claude and Codex currently
require `{key: "session_id", id: "<provider ID>"}`. Codex compiles to
`codex resume <id>`; Claude to `claude --resume <id>`. No preferences or prompt
are added. Structured-chat support is host-specific and is not probed by
creating a chat; `structured_agent_session_unsupported` does not forbid a CLI
terminal resume. This helper opens terminals only.

Write a temporary JSON target from the established tuple (do not copy secrets):

```json
{
  "environment": "<saved environment ID, or local>",
  "workspace": "<exact address.worktree>",
  "workspaceId": "<worktree.id>",
  "host": "<worktree.hostId>",
  "runtimeId": "<_meta.runtimeId>",
  "agent": "claude",
  "sessionId": "<full provider ID>"
}
```

Run from this repository (quote paths for the shell):

```text
node scripts/orca-open-session.cjs --target <absolute-target.json>
```

The helper resolves `ORCA_CLI_COMMAND`, then the dev/Linux/platform executable
rules. It discovers compiled runtime modules relative to the selected executable
and its resolved symlink, without a fixed application path. For other layouts,
`--runtime-root <compiled-out-directory>` selects the matching installed CLI's
modules explicitly. Missing Node/modules/schema, missing terminal identity metadata or a host/CLI version mismatch
is a blocker, never a reason to use another installation silently. The helper
refuses if CLI discovery advertises a public open/resume command: load that
command's current guide rather than guessing its argv.

It validates runtime/workspace/host and joins terminal handles to
`session.tabs.list` agent-status provider bindings. `terminal show` does not
carry that provider ID in the inspected build. It then sends
`terminal.ensureAgentSession` once with explicit worktree, agent, provider
session, `resumeOrigin: saved` and focused presentation. Saved-resume refusal
(including chat ownership or unverifiable ownership) blocks further opening. Any returned resume binding must match exactly. The launch envelope may carry
only a handle; live tab-status evidence must independently prove the provider ID.
It verifies the terminal's host/workspace/agent, reads `--screen`, and runs
`terminal switch` on the same environment and handle. Inspect the returned
screen for the expected conversation before reporting success. Missing identity
metadata, unsupported screen reads or `navigated: false` are explicit blockers.

## Unknown outcomes and repetition

A private write-ahead receipt and exclusive lock live under
`$XDG_CACHE_HOME/llm-obsidian/orca-open-session/` (default `~/.cache/`).
`--receipt-dir <absolute-directory>` changes this location for controlled use.
Use the same location on retries. Do not delete a receipt/lock to force a launch.
No transcript or pairing token is stored there; only routing/session identifiers.

`unknown` means the mutation might have happened. The next invocation may only
reconcile exact live identity and navigate; it cannot reissue ensure. A crashed
process leaves a lock for operator inspection. An unresolvable stale receipt
or runtime restart requires read-only diagnosis, not a fresh launch. Do not
retry a lost mutation through another CLI, host, agent or surface. If navigation
fails after opening, inspect/focus the known handle without creating again.
