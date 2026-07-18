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

python3 - "$REPO_ROOT" >"$OUT" 2>&1 <<'PYEOF'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
plugin = json.loads((root / ".claude-plugin/plugin.json").read_text())
market = json.loads((root / ".claude-plugin/marketplace.json").read_text())
entry = market["plugins"][0]
assert plugin["name"] == entry["name"] == "llm-obsidian"
assert plugin["version"] == entry["version"] == market["metadata"]["version"]
assert plugin["repository"] == "https://github.com/zerg-su/llm-obsidian"
assert entry["source"] == {
    "source": "github",
    "repo": "zerg-su/llm-obsidian",
    "ref": "main",
}
print("CLAUDE_PACKAGE_OK")
PYEOF
expect_grep "A5 public Claude package metadata is aligned" "$OUT" "CLAUDE_PACKAGE_OK"
python3 - "$REPO_ROOT" >"$OUT" 2>&1 <<'PYEOF'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
for relative in (
    ".codex/config.toml",
    ".codex/profiles/default.toml",
    ".codex/profiles/wiki-write.toml",
    ".codex/profiles/reviewer-readonly.toml",
):
    text = (root / relative).read_text()
    assert 'model = "gpt-5.6-sol"' in text, relative
    assert 'model_reasoning_effort = "high"' in text, relative
deep = (root / ".codex/profiles/deep.toml").read_text()
assert 'model = "gpt-5.6-sol"' in deep
assert 'model_reasoning_effort = "max"' in deep
dispatch = (root / ".codex/dispatch-env.toml").read_text()
for expected in (
    'codex_review_model = "gpt-5.6-sol"',
    'codex_review_effort = "high"',
    'claude_review_model = "fable"',
    'claude_review_effort = "high"',
):
    assert expected in dispatch, expected
print("MODEL_DEFAULTS_OK")
PYEOF
expect_grep "A6 repo model defaults are aligned" "$OUT" "MODEL_DEFAULTS_OK"

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
assert plugin["author"] == {"name": "zerg-su"}
assert plugin["interface"]["displayName"] == "llm-obsidian"
assert market["name"] == "llm-obsidian-codex"
assert market["plugins"][0]["name"] == "llm-obsidian"
assert market["plugins"][0]["source"]["path"] == "./"
print("SHAPE_OK")
PYEOF
expect_grep "C1 generated shape" "$OUT" "SHAPE_OK"
python3 "$FIX/scripts/codex-adapter.py" --repo-root "$FIX" --check >"$OUT" 2>&1
expect_exit "C2 second check clean" "$?" 0
expect_grep "C3 no changes message" "$OUT" "codex-adapter: no changes"

python3 - "$FIX/.codex-plugin/plugin.json" <<'PYEOF'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1]); data = json.loads(path.read_text())
data["version"] = "1.0.0+codex.test-cache"
path.write_text(json.dumps(data, indent=2) + "\n")
PYEOF
python3 "$FIX/scripts/codex-adapter.py" --repo-root "$FIX" --apply >"$OUT" 2>&1
expect_exit "C4 cachebuster apply exits 0" "$?" 0
expect_grep "C5 cachebuster preserved" "$FIX/.codex-plugin/plugin.json" '"version": "1.0.0+codex.test-cache"'

echo "D. fork plugin names"
python3 - "$FIX" >"$OUT" 2>&1 <<'PYEOF'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
path = root / ".claude-plugin/plugin.json"
data = json.loads(path.read_text())
data["name"] = "llm-obsidian-custom"
data["homepage"] = "https://github.com/example/llm-obsidian-custom"
data["repository"] = "https://github.com/example/llm-obsidian-custom"
path.write_text(json.dumps(data, indent=2) + "\n")
print("FORK_SOURCE_OK")
PYEOF
expect_grep "D1 fork source updated" "$OUT" "FORK_SOURCE_OK"
python3 "$FIX/scripts/codex-adapter.py" --repo-root "$FIX" --apply >"$OUT" 2>&1
expect_exit "D2 fork apply exits 0" "$?" 0
python3 - "$FIX" >"$OUT" 2>&1 <<'PYEOF'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
plugin = json.loads((root / ".codex-plugin/plugin.json").read_text())
market = json.loads((root / ".agents/plugins/marketplace.json").read_text())
assert plugin["name"] == "llm-obsidian-custom"
assert plugin["interface"]["displayName"] == "llm-obsidian-custom"
assert market["name"] == "llm-obsidian-custom-codex"
assert market["interface"]["displayName"] == "llm-obsidian-custom Codex"
assert market["plugins"][0]["name"] == "llm-obsidian-custom"
print("FORK_SHAPE_OK")
PYEOF
expect_grep "D3 fork generated shape" "$OUT" "FORK_SHAPE_OK"

echo
echo "codex-adapter tests: $pass passed, $fail failed"
if [ "$fail" -gt 0 ]; then
  printf '  - %s\n' "${failures[@]}"
  exit 1
fi
