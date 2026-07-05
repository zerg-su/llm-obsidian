# claude-obsidian Hooks

Plugin hooks for the claude-obsidian wiki vault. All hooks are defined in `hooks.json`.

## Events

| Event | Type | Purpose |
|---|---|---|
| `SessionStart` | command + prompt | Loads `wiki/hot.md` into context. Command runs `[ -f wiki/hot.md ] && cat wiki/hot.md \|\| true` as the canonical safety check (works for non-vault sessions without erroring). Prompt complements with semantic context restoration. Matcher: `startup\|resume`. |
| `PostCompact` | prompt | Re-loads `wiki/hot.md` after context compaction. Hook-injected context does NOT survive compaction (only `CLAUDE.md` does), so this hook restores the hot cache mid-session. |
| `UserPromptSubmit` | command | `.claude/hooks/skill-router.sh` — soft-suggest skill + agent router (added 2026-05-22, P0a Step 1 of iter-2). Reads each prompt, matches against `.claude/skill-rules.json`, outputs `Hint:...` for matched skills/agents. Tone is non-mandatory (see memory rule `feedback_router_tone_soft`). Disable per-session via `SKILL_ROUTER_MUTE=1`. Logs to `.vault-meta/router-hits.jsonl` (gitignored). |
| `Stop` | command | Three things in one matcher=`""` hook: (1) runs `scripts/reindex.py --quiet` to regenerate `.vault-meta/index.jsonl` + `address-map.tsv` + `session-to-pages.jsonl` from `wiki/**/*.md` frontmatter (added 2026-05-22, P0a Step 3); (2) `git add wiki/ .raw/ .vault-meta/` + auto-commit if there is a diff; (3) emits `WIKI_CHANGED:` hint if wiki was dirty, asking the model to refresh `wiki/hot.md`. |

## What's NOT here: `PostToolUse`

Earlier versions of this plugin used a `PostToolUse` hook (matcher `Write\|Edit`) to auto-commit per-tool-call. That was **removed in favour of the `Stop` hook**: one commit per Claude turn instead of N commits per tool call. Cleaner history, fewer Stop-hook collisions, and `.vault-meta/` indexes can be regenerated once at end of turn rather than after every Edit.

If you see references to `PostToolUse` in older notes / wiki pages, treat them as stale — `hooks.json` has been authoritative since 2026-05-22.

## Known Issue: Plugin Hooks STDOUT Bug

`anthropics/claude-code#10875` documents that **plugin hook STDOUT may not be captured** by Claude Code, while identical inline hooks in `settings.json` work correctly.

**Impact**: If this bug is active in your Claude Code version, the prompt-type SessionStart and PostCompact hooks may not inject context as expected.

**Workaround**: The command-type SessionStart hook (`cat wiki/hot.md`) is the canonical safety check. It relies on STDOUT capture for context injection, so test against this issue if hot cache restoration fails. As a fallback, copy the hook config from `hooks.json` into your user-level `~/.claude/settings.json` instead of relying on plugin hooks.

**Test for the bug**: After installing the plugin, open a fresh Claude Code session in a directory containing a populated `wiki/hot.md`. Ask Claude "what's in the hot cache?". If Claude has no idea, the STDOUT bug is active in your version.

## Non-Vault Sessions

The SessionStart command hook uses `[ -f wiki/hot.md ] && cat wiki/hot.md || true` so it always exits 0, even when no vault is present. This makes the plugin safe to install globally without breaking non-vault Claude Code sessions.
