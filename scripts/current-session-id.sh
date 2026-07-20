#!/usr/bin/env bash
# Print the current agent session/thread id in a cross-runtime way.
set -u

if [ "${LLM_OBSIDIAN_ACCEPTANCE:-}" = "1" ] &&
   [[ "${LLM_OBSIDIAN_ACCEPTANCE_SESSION_ID:-}" =~ ^[A-Za-z0-9._:-]+$ ]]; then
  printf '%s\n' "$LLM_OBSIDIAN_ACCEPTANCE_SESSION_ID"
elif [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
  printf '%s\n' "$CLAUDE_CODE_SESSION_ID"
elif [ -n "${CODEX_THREAD_ID:-}" ]; then
  printf '%s\n' "$CODEX_THREAD_ID"
else
  printf 'unknown\n'
fi
