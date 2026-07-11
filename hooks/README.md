# llm-obsidian Hooks

Plugin hooks for the llm-obsidian wiki vault. All hooks are defined in `hooks.json`; the scripts live in `.claude/hooks/`.

Most hooks are Claude Code specific and keep a Codex guard. The `Stop` hook is
the exception: Codex plugin hooks also use it for the turn-end reindex and
auto-commit pipeline, so `hooks.json` runs `stop.sh` with
`LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS=1`. Codex requires hook stdout to be empty or
JSON, so the plugin command redirects `stop.sh` output to
`.vault-meta/stop-hook-last.log`.

## Events

| Event | Script | Purpose |
|---|---|---|
| `SessionStart` (`startup\|resume`) | inline | Loads `wiki/hot.md` into context via `[ -f wiki/hot.md ] && cat wiki/hot.md \|\| true` — safe no-op for non-vault sessions. |
| `SessionStart` (`startup`) | `session-nudge.sh` | 0..N one-line maintenance hints: lint age, fold due, tiling age, stale explicitly enabled memory backup, skill-of-the-day, retrieval-assist discipline, gateway probe (only if the MCP gateway is configured). Soft tone, never mandatory. |
| `PostCompact` | inline | Re-loads `wiki/hot.md` after context compaction — hook-injected context does NOT survive compaction. |
| `UserPromptSubmit` | `skill-router.sh` | Soft skill hints: matches the prompt against `.claude/skill-rules.json`, prints `Hint: ...` for matches. Disable per-session via `SKILL_ROUTER_MUTE=1`. Logs to `.vault-meta/router-hits.jsonl` (gitignored). |
| `PostToolUse` (`Bash`) | `command-capture.sh` | Appends a sanitized `{ts, session_id, cwd, command, is_error}` record to `.vault-meta/command-log.jsonl` (credentials masked; the event is dropped if a residual pattern remains). Raw material for `/distill-runbook` and usage telemetry. |
| `PostToolUse` (`ExitPlanMode`) | `plan-capture.sh` | Sends every approved plan plus its log entry through the transactional vault writer. |
| `Stop` | `stop.sh` → `stop-hook.py` | `fcntl`-serialized pipeline: transaction recovery → reindex/folder indexes → section sparse `ensure` even on a clean tree (plus legacy BM25 compatibility) → optional dense chunk refresh with retry marker → optional memory backup → strict validation gate → scoped Git commit that preserves unrelated staging. |

## Design notes

- Hook launchers prefer the immutable installed `PLUGIN_ROOT`, but fall back to
  a fingerprinted active vault/worktree root when that cache path disappears
  during a live `codex plugin add`. The launcher propagates the resolved root,
  and `run-hook.sh` independently validates its Python adapter before exec.
  This prevents code 127 and silent no-op failures until the next thread reloads
  the new plugin registry.

- **One scoped commit per turn.** The hook commits only `wiki/`, `.raw/`, `.vault-meta/`, and opted-in `.claude-memory/`; unrelated staged work remains staged. Stdlib `fcntl` closes the parallel-session race without an external CLI.
- **Memory backup is explicit and fail-closed.** No source is guessed. Set `CLAUDE_MEMORY_DIR` or copy `config/memory-backup.example.json` to `.vault-meta/memory-backup.json` and enable it. The sanitized candidate snapshot and existing backup are scanned before any write/prune; a residual credential pattern blocks the turn-end commit.
- **Hooks never fail the turn.** Every entry is wrapped in `[ -x ... ] && ... || true`; scripts exit 0 even on internal errors.
- **Non-vault sessions are safe.** Every hook feature-detects the vault (`wiki/hot.md`, script presence), so installing the plugin globally does not break other projects.
- **Codex sessions are safe.** Prompt/session/tool hooks exit immediately when `CODEX_THREAD_ID` or a Codex parent process is detected. `Stop` intentionally opts back in with `LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS=1` so Codex saves wiki changes at turn end too; its output is captured in `.vault-meta/stop-hook-last.log` instead of stdout.
- **Telemetry does not imply hook parity.** Shared scripts emit content-free operations for Claude, Codex, or manual runs. `pipeline-stats.py` keeps them separate from Claude-only history/transcript/router data; see `docs/runtime-capabilities.md`.

## Known Issue: Plugin Hooks STDOUT Bug

`anthropics/claude-code#10875` documents that **plugin hook STDOUT may not be captured** by Claude Code in some versions, while identical inline hooks in `settings.json` work correctly.

**Test for the bug**: after installing the plugin, open a fresh session in a directory with a populated `wiki/hot.md` and ask "what's in the hot cache?". If the model has no idea, copy the hook config from `hooks.json` into your user-level `~/.claude/settings.json` as a workaround.
