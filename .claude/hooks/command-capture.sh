#!/usr/bin/env bash
# command-capture.sh — PostToolUse[Bash] hook wrapper.
#
# Reads the stdin JSON payload from Claude Code:
#   {"tool_name": "Bash", "tool_input": {"command": "..."},
#    "tool_response": {"stdout": "...", "stderr": "..."}}
#
# Appends a sanitized command record to .vault-meta/command-log.jsonl
# (see command-capture.py). No output; never blocks the tool call.

set -u

if [ "${LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS:-}" != "1" ]; then
  # Skip in Codex — the Claude hook layer is a no-op there. Shared detector with a
  # self-contained env fallback so a missing script never disables the guard.
  _dr="$(dirname -- "$0")/../../scripts/detect-runtime.sh"
  if [ -x "$_dr" ]; then
    [ "$("$_dr")" = codex ] && exit 0
  elif [ -n "${CODEX_THREAD_ID:-}${CODEX_CI:-}${CODEX_MANAGED_BY_NPM:-}" ]; then
    exit 0
  fi
fi

exec python3 "$(dirname "$0")/command-capture.py"
