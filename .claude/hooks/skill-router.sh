#!/usr/bin/env bash
# UserPromptSubmit entry-point. Reads JSON payload from stdin, delegates to python router.
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

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/skill-router.py"
