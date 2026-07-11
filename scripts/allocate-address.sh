#!/usr/bin/env bash
# Stable public CLI; the Python implementation uses portable stdlib fcntl.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/allocate-address.py" "$@"
