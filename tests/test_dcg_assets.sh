#!/usr/bin/env bash
# Offline checks for portable dcg/Codex limit helper assets.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$(mktemp /tmp/dcg-assets-test.XXXXXX)"
trap 'rm -f "$OUT"' EXIT

pass=0; fail=0; failures=()
ok()  { pass=$((pass+1)); printf '  OK   %s\n' "$1"; }
bad() { fail=$((fail+1)); failures+=("$1: $2"); printf '  FAIL %s — %s\n' "$1" "$2"; }

expect_exit() {
  [ "$2" = "$3" ] && ok "$1" || bad "$1" "exit $2 (want $3)"
}
expect_grep() {
  grep -qF -- "$3" "$2" && ok "$1" || bad "$1" "pattern not found: $3"
}
expect_no_grep() {
  grep -qF -- "$3" "$2" && bad "$1" "unexpected pattern found: $3" || ok "$1"
}

echo "A. syntax"
bash -n "$REPO_ROOT/bin/setup-dcg.sh" "$REPO_ROOT/scripts/dcg-test-suite.sh" "$REPO_ROOT/.codex/update-cmux-limits.sh" 2>"$OUT"
expect_exit "A1 shell scripts parse" "$?" 0
python3 -m py_compile "$REPO_ROOT/.codex/codex-limits-status.py" "$REPO_ROOT/scripts/codex-limit-monitor.py" 2>"$OUT"
expect_exit "A2 python scripts compile" "$?" 0

echo "B. JSON/TOML shape"
python3 - "$REPO_ROOT" >"$OUT" 2>&1 <<'PY'
import json
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
hook = json.loads((root / ".github/hooks/dcg.json").read_text())
pre = hook["hooks"]["PreToolUse"]
assert len(pre) == 1
assert pre[0]["matcher"] == "Bash"
assert pre[0]["hooks"][0]["command"] == "__DCG_BIN__"

cfg_text = (root / "config/dcg/config.toml").read_text()
packs_match = re.search(r"(?ms)^\[packs\]\s*^enabled\s*=\s*\[(.*?)^\]", cfg_text)
assert packs_match, "packs.enabled block"
packs = set(re.findall(r'"([^"]+)"', packs_match.group(1)))
for name in ("core.filesystem", "core.git", "strict_git", "cloud.aws", "kubernetes.kubectl"):
    assert name in packs, name
interactive_match = re.search(r"(?ms)^\[interactive\](.*?)(?:^\[|\Z)", cfg_text)
assert interactive_match, "interactive block"
interactive = interactive_match.group(1)
for key in ("enabled", "verification", "timeout_seconds", "code_length", "max_attempts"):
    assert key in interactive, key
assert "/Users/" not in cfg_text
assert "WhaleKit" not in cfg_text
print("SHAPE_OK")
PY
expect_grep "B1 hook/config shape" "$OUT" "SHAPE_OK"

echo "C. helper behavior"
"$REPO_ROOT/.codex/codex-limits-status.py" --with-pct --compact >"$OUT" 2>&1
rc=$?
if [ "$rc" = "0" ] || [ "$rc" = "1" ]; then ok "C1 status helper exits 0/1"; else bad "C1 status helper exits 0/1" "exit $rc"; fi
expect_grep "C2 status helper prints limit labels" "$OUT" "5h"
"$REPO_ROOT/scripts/codex-limit-monitor.py" --help >"$OUT" 2>&1
expect_exit "C3 monitor help exits 0" "$?" 0
expect_grep "C4 monitor help names install command" "$OUT" "codex-limit-status"
"$REPO_ROOT/bin/setup-dcg.sh" --help >"$OUT" 2>&1
expect_exit "C5 setup help exits 0" "$?" 0
expect_grep "C6 setup help mentions --check" "$OUT" "--check"

echo "D. portability"
expect_no_grep "D1 installer has no user-specific path" "$REPO_ROOT/bin/setup-dcg.sh" "/Users/kirill"
expect_no_grep "D2 cmux updater has no old repo path" "$REPO_ROOT/.codex/update-cmux-limits.sh" "claude-obsidian"
expect_no_grep "D3 dcg hook has no absolute user path" "$REPO_ROOT/.github/hooks/dcg.json" "/Users/"

echo
echo "dcg asset tests: $pass passed, $fail failed"
if [ "$fail" -gt 0 ]; then
  printf '  - %s\n' "${failures[@]}"
  exit 1
fi
