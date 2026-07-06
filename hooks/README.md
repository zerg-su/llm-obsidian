# llm-obsidian Hooks

Plugin hooks for the llm-obsidian wiki vault. All hooks are defined in `hooks.json`; the scripts live in `.claude/hooks/`.

These hooks are Claude Code specific. Codex uses the generated `.codex-plugin/`
skill package and does not run `.claude/hooks/`; every shell hook has a
`CODEX_THREAD_ID`/parent-process guard so an accidental Codex launch no-ops.

## Events

| Event | Script | Purpose |
|---|---|---|
| `SessionStart` (`startup\|resume`) | inline | Loads `wiki/hot.md` into context via `[ -f wiki/hot.md ] && cat wiki/hot.md \|\| true` — safe no-op for non-vault sessions. |
| `SessionStart` (`startup`) | `session-nudge.sh` | 0..N one-line maintenance hints: lint age, fold due, tiling age, stale memory backup, skill-of-the-day, retrieval-assist discipline, gateway probe (only if the MCP gateway is configured). Soft tone, never mandatory. |
| `PostCompact` | inline | Re-loads `wiki/hot.md` after context compaction — hook-injected context does NOT survive compaction. |
| `UserPromptSubmit` | `skill-router.sh` | Soft skill hints: matches the prompt against `.claude/skill-rules.json`, prints `Hint: ...` for matches. Disable per-session via `SKILL_ROUTER_MUTE=1`. Logs to `.vault-meta/router-hits.jsonl` (gitignored). |
| `PostToolUse` (`Bash`) | `command-capture.sh` | Appends a sanitized `{ts, session_id, cwd, command, is_error}` record to `.vault-meta/command-log.jsonl` (credentials masked by `scripts/lib_sanitize.py`). Raw material for `/distill-runbook` and usage telemetry. |
| `PostToolUse` (`ExitPlanMode`) | `plan-capture.sh` | Files every approved plan into `wiki/plans/<TS>-<slug>.md` with frontmatter, a DragonScale address and a log entry. |
| `Stop` | `stop.sh` | Turn-end pipeline under `flock` (parallel sessions serialize): reindex `.vault-meta/` + folder `_index.md` blocks → sanitized memory backup → BM25 rebuild → incremental dense-embedding refresh (both only when wiki changed) → auto-commit `wiki/ .raw/ .vault-meta/ .claude-memory/` → cap validation warnings → fold suggestion every 64 log entries → per-phase latency record (`.vault-meta/stop-hook-latency.jsonl`, `STOP_HOOK_SLOW` at ≥30s). Opt out of the auto-commit with `touch .vault-meta/auto-commit.disabled`. |

## Design notes

- **One commit per turn.** Earlier generations auto-committed per `Write|Edit` tool call; the `Stop` hook replaced that: cleaner history, indexes regenerate once, and the flock closes the race between parallel sessions.
- **Hooks never fail the turn.** Every entry is wrapped in `[ -x ... ] && ... || true`; scripts exit 0 even on internal errors.
- **Non-vault sessions are safe.** Every hook feature-detects the vault (`wiki/hot.md`, script presence), so installing the plugin globally does not break other projects.
- **Codex sessions are safe.** Hooks exit immediately when `CODEX_THREAD_ID` or a Codex parent process is detected. Regression tests can opt back in with `LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS=1`.

## Known Issue: Plugin Hooks STDOUT Bug

`anthropics/claude-code#10875` documents that **plugin hook STDOUT may not be captured** by Claude Code in some versions, while identical inline hooks in `settings.json` work correctly.

**Test for the bug**: after installing the plugin, open a fresh session in a directory with a populated `wiki/hot.md` and ask "what's in the hot cache?". If the model has no idea, copy the hook config from `hooks.json` into your user-level `~/.claude/settings.json` as a workaround.
