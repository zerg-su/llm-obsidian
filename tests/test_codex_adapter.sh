#!/usr/bin/env bash
# Offline regression suite for scripts/codex-adapter.py.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX="$(mktemp -d /tmp/codex-adapter-test.XXXXXX)"
OUT="$SANDBOX/out.txt"
trap 'rm -rf "$SANDBOX"' EXIT

pass=0; fail=0; failures=()
ok()  { pass=$((pass+1)); printf '  OK   %s\n' "$1"; }
bad() { fail=$((fail+1)); failures+=("$1: $2"); printf '  FAIL %s — %s\n' "$1" "$2"; }

expect_exit() { [[ "$2" == "$3" ]] && ok "$1" || bad "$1" "exit $2 (want $3)"; }
expect_grep() { grep -qF -- "$3" "$2" && ok "$1" || bad "$1" "pattern not found: $3"; }
expect_file() { [ -f "$1" ] && ok "$2" || bad "$2" "missing file: $1"; }

FIX="$SANDBOX/repo"
mkdir -p "$FIX/.claude-plugin" "$FIX/skills/wiki" "$FIX/scripts"
cp "$REPO_ROOT/scripts/codex-adapter.py" "$FIX/scripts/"
cp "$REPO_ROOT/scripts/current-session-id.sh" "$FIX/scripts/"
cat > "$FIX/.claude-plugin/plugin.json" <<'EOF'
{
  "name": "llm-obsidian",
  "version": "1.0.0",
  "description": "LLM-powered self-organizing second brain for Obsidian.",
  "author": "zerg-su",
  "homepage": "https://github.com/zerg-su/llm-obsidian",
  "repository": "https://github.com/zerg-su/llm-obsidian",
  "license": "MIT",
  "keywords": ["obsidian", "wiki"]
}
EOF
cat > "$FIX/skills/wiki/SKILL.md" <<'EOF'
---
name: wiki
description: Test wiki skill.
---
# wiki
EOF

echo "A. syntax/session helper"
python3 -m py_compile "$FIX/scripts/codex-adapter.py" 2>"$OUT"; expect_exit "A1 codex-adapter.py compiles" "$?" 0
CLAUDE_CODE_SESSION_ID=claude-session CODEX_THREAD_ID=codex-thread "$FIX/scripts/current-session-id.sh" >"$OUT" 2>&1
expect_grep "A2 helper prefers Claude" "$OUT" "claude-session"
env -u CLAUDE_CODE_SESSION_ID CODEX_THREAD_ID=codex-thread "$FIX/scripts/current-session-id.sh" >"$OUT" 2>&1
expect_grep "A3 helper falls back to Codex" "$OUT" "codex-thread"
env -u CLAUDE_CODE_SESSION_ID -u CODEX_THREAD_ID "$FIX/scripts/current-session-id.sh" >"$OUT" 2>&1
expect_grep "A4 helper unknown fallback" "$OUT" "unknown"

echo "B. check/apply"
python3 "$FIX/scripts/codex-adapter.py" --repo-root "$FIX" --check >"$OUT" 2>&1
expect_exit "B1 initial check reports drift" "$?" 1
expect_grep "B2 check mentions plugin manifest" "$OUT" ".codex-plugin/plugin.json"
expect_grep "B3 check mentions marketplace" "$OUT" ".agents/plugins/marketplace.json"
python3 "$FIX/scripts/codex-adapter.py" --repo-root "$FIX" --apply >"$OUT" 2>&1
expect_exit "B4 apply exits 0" "$?" 0
expect_file "$FIX/.codex-plugin/plugin.json" "B5 root manifest written"
expect_file "$FIX/.agents/plugins/marketplace.json" "B6 marketplace written"

echo "C. generated shape"
python3 - "$FIX" >"$OUT" 2>&1 <<'PYEOF'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
plugin = json.loads((root / ".codex-plugin/plugin.json").read_text())
market = json.loads((root / ".agents/plugins/marketplace.json").read_text())
assert plugin["name"] == "llm-obsidian"
assert plugin["skills"] == "./skills/"
assert plugin["interface"]["displayName"] == "llm-obsidian"
assert market["name"] == "llm-obsidian-codex"
assert market["plugins"][0]["source"]["path"] == "./"
print("SHAPE_OK")
PYEOF
expect_grep "C1 generated shape" "$OUT" "SHAPE_OK"
python3 "$FIX/scripts/codex-adapter.py" --repo-root "$FIX" --check >"$OUT" 2>&1
expect_exit "C2 second check clean" "$?" 0
expect_grep "C3 no changes message" "$OUT" "codex-adapter: no changes"

echo
echo "codex-adapter tests: $pass passed, $fail failed"
if [ "$fail" -gt 0 ]; then
  printf '  - %s\n' "${failures[@]}"
  exit 1
fi
