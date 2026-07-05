#!/usr/bin/env bash
# UserPromptSubmit entry-point. Reads JSON payload from stdin, delegates to python router.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/skill-router.py"
