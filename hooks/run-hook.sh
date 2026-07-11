#!/usr/bin/env bash
# Resolve the installed plugin root, then hand the wire payload to Python.
set -u
ROOT="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
if [ ! -f "$ROOT/hooks/run-hook.py" ]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
[ -f "$ROOT/hooks/run-hook.py" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
exec python3 "$ROOT/hooks/run-hook.py" "$@"
