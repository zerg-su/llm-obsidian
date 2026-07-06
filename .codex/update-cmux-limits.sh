#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

if [ -z "${CMUX_WORKSPACE_ID:-}${CMUX_SURFACE_ID:-}" ]; then
  exit 0
fi

/usr/bin/python3 "$REPO_ROOT/.codex/codex-limits-status.py" --with-pct --compact --cmux-set >/dev/null 2>&1 || true
