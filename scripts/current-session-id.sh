#!/usr/bin/env bash
# Print the current agent session/thread id in a cross-runtime way.
set -u

task_root=$(git -C "${PWD:-.}" rev-parse --show-toplevel 2>/dev/null || true)
task_session=""
if [ -n "$task_root" ] && [ -f "$task_root/.task-origin-session" ]; then
  IFS= read -r task_session < "$task_root/.task-origin-session" || true
fi

if [[ "$task_session" =~ ^[A-Za-z0-9._:-]+$ ]]; then
  printf '%s\n' "$task_session"
elif [ "${LLM_OBSIDIAN_ACCEPTANCE:-}" = "1" ] &&
   [[ "${LLM_OBSIDIAN_ACCEPTANCE_SESSION_ID:-}" =~ ^[A-Za-z0-9._:-]+$ ]]; then
  printf '%s\n' "$LLM_OBSIDIAN_ACCEPTANCE_SESSION_ID"
else
  script_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
  acceptance_session_file="$script_root/.vault-meta/acceptance/session-id"
  acceptance_session=""
  if [ -f "$script_root/.acceptance-sandbox.json" ] &&
     [ -f "$acceptance_session_file" ]; then
    IFS= read -r acceptance_session < "$acceptance_session_file" || true
  fi
  if [[ "$acceptance_session" =~ ^[A-Za-z0-9._:-]+$ ]]; then
    printf '%s\n' "$acceptance_session"
  elif [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
    printf '%s\n' "$CLAUDE_CODE_SESSION_ID"
  elif [ -n "${CODEX_THREAD_ID:-}" ]; then
    printf '%s\n' "$CODEX_THREAD_ID"
  else
    printf 'unknown\n'
  fi
fi
