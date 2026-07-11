#!/usr/bin/env bash
# Stable hook wrapper; all turn-end state and locking live in stop-hook.py.
set -u

if [ "${LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS:-}" != "1" ]; then
  _dr="$(dirname -- "$0")/../../scripts/detect-runtime.sh"
  if [ -x "$_dr" ]; then
    [ "$("$_dr")" = codex ] && exit 0
  elif [ -n "${CODEX_THREAD_ID:-}${CODEX_CI:-}${CODEX_MANAGED_BY_NPM:-}" ]; then
    exit 0
  fi
fi

VAULT_ROOT="${LLM_OBSIDIAN_PROJECT_ROOT:-${CLAUDE_PROJECT_DIR:-$PWD}}"
HELPER="$VAULT_ROOT/scripts/stop-hook.py"
[ -f "$HELPER" ] || exit 0
exec python3 "$HELPER"
