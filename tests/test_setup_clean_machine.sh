#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
mkdir -p "$SCRATCH/home"

test -f "$ROOT/requirements-tooling.txt" || {
  echo "missing requirements-tooling.txt" >&2
  exit 1
}
grep -qx 'PyYAML==6.0.3' "$ROOT/requirements-tooling.txt"

OUTPUT="$({
  HOME="$SCRATCH/home" bash "$ROOT/bin/setup-clean-machine.sh" \
    --check --skip-vault --skip-proxy --skip-docling
} 2>&1)"

grep -F -- '-m pip install' <<<"$OUTPUT" >/dev/null
grep -F -- '--requirement' <<<"$OUTPUT" >/dev/null
grep -F -- "$ROOT/requirements-tooling.txt" <<<"$OUTPUT" >/dev/null
grep -F -- 'yaml' <<<"$OUTPUT" >/dev/null
test -z "$(find "$SCRATCH/home" -mindepth 1 -print -quit)"

echo "setup-clean-machine tooling dependency test passed"
