---
name: orca-open-session
metadata:
  version: 1.0.0
description: Find a past Claude/Codex session and open its exact ID in Orca, across local and registered remote connections.
---

# Open a past session in Orca

Input: a topic or exact provider session ID, optionally a connection/project.
Examples: «найди сессию про Python на Mac-агентах и открой в Orca»;
`$llm-obsidian:orca-open-session <topic or ID>`.
The invocation authorizes opening that conversation, without sending a prompt,
continuing its task, or changing model, effort or permissions. No GUI clicks.

1. Load the installed `orca-cli` skill. Resolve its executable once and load
   `skills get orca-cli --json`. Inspect `agent-context --json`: prefer a
   documented old-session open/resume command if present, after reading its
   current contract. `agent launch` creates a fresh agent; do not substitute it.
2. Discover connections with `environment list --json` and `host list --json`.
   Search local plus every registered remote environment when none was specified.
   Use stable environment IDs, not a fixed list of names. A direct SSH host
   without a registered environment is not a guessed `--environment` route;
   report it as unsearched if this version exposes no session-search route.
3. For a topic, use wiki provenance from `find-session` (frontmatter `sessions`,
   `session-to-pages.jsonl`, relevant indexed page metadata) AND Orca search.
   Provenance alone does not prove provider, host or workspace. On EACH selected
   connection first run `search --index-status --environment <id> --json`.
   Search only when indexing is enabled, with distinctive nouns/phrases and
   optional agent/path filters. Do not enable indexing. Report unavailable,
   disabled and still-indexing sources; page cursors only within their connection.
4. Resolve agent + full provider session ID + environment + exact workspace.
   Read [resolution and opening](references/workflow.md) for exact-ID lookup,
   workspace binding and helper input. Keep same-named paths on different hosts
   separate; dedupe by `(environment, host, workspace, agent, provider ID)`.
   If candidates remain ambiguous, ask ONE short question listing title, date,
   agent, connection and workspace. Do not open a guessed best match.
5. Open/focus through the documented CLI, or the compatibility-checked helper.
   Never execute a search result's `resumeCommand` as shell code. Never resend
   a mutation after an uncertain reply. Reconcile existing surfaces read-only.
6. Verify the returned provider identity and workspace, inspect the current
   screen against the chosen conversation, and require `navigated: true`.
   A launch receipt alone is insufficient. No terminal input or follow-up task.

Final: title · date · connection · surface (terminal/chat). Use metadata dates,
not the opening time. On failure, give the exact blocker and what remains
unverified; never claim success. Opening a terminal is not opening a chat.
