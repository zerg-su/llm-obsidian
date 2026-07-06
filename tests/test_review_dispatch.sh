#!/usr/bin/env bash
# Regression tests for review-dispatch deterministic mode plumbing.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/skills/review-dispatch/scripts/spawn_review.py"
SANDBOX="$(mktemp -d /tmp/review-dispatch-test.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

pass=0
fail=0
failures=()
ok()  { pass=$((pass+1)); printf '  OK   %s\n' "$1"; }
bad() { fail=$((fail+1)); failures+=("$1: $2"); printf '  FAIL %s - %s\n' "$1" "$2"; }

expect_eq() {
  local name="$1" got="$2" want="$3"
  [[ "$got" == "$want" ]] && ok "$name" || bad "$name" "got $got (want $want)"
}

write_fixture() {
  local dir="$1"
  mkdir -p "$dir"
  git -C "$dir" init -q
  git -C "$dir" config user.email test@example.com
  git -C "$dir" config user.name test
  printf 'base\n' > "$dir/file.txt"
  git -C "$dir" add file.txt
  git -C "$dir" commit -qm init
  printf 'changed\n' > "$dir/file.txt"
  cat > "$dir/.task-meta.json" <<'JSON'
{"task_name":"review-dispatch-test","base_branch":"HEAD","branch":"task/review-dispatch-test","executor_runtime":"codex","model":"gpt-5.5"}
JSON
  printf '# Task: review-dispatch-test\n' > "$dir/.task-prompt.md"
  printf '00000000-0000-0000-0000-000000000001\n' > "$dir/.task-cmux-surface"
}

json_get() {
  local file="$1" expr="$2"
  python3 - "$file" "$expr" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(data.get(sys.argv[2], ""))
PY
}

echo "== review-dispatch mode plumbing =="

LIGHT="$SANDBOX/light"
write_fixture "$LIGHT"
"$SCRIPT" start --light --no-spawn --worktree "$LIGHT" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/light.err"
expect_eq "start-light-exit" "$?" 0
expect_eq "start-light-meta" "$(json_get "$LIGHT/.review-meta.json" review_mode)" "light"
grep -q 'Review mode: `light`' "$LIGHT/.review-prompt.md" && ok "start-light-prompt" || bad "start-light-prompt" "missing light marker"
grep -q 'top 5 actionable findings' "$LIGHT/.review-prompt.md" && ok "start-light-instructions" || bad "start-light-instructions" "missing light instructions"

cat > "$LIGHT/.task-review.md" <<'MD'
# Cross-Model Review: review-dispatch-test

Verdict: approve

## Findings

Findings: none
MD
cat > "$LIGHT/.task-review-resolution.md" <<'MD'
# Review Resolution

No findings applied.
MD
"$SCRIPT" verify --no-send --worktree "$LIGHT" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/verify.err"
expect_eq "verify-light-exit" "$?" 0
expect_eq "verify-light-preserved" "$(json_get "$LIGHT/.review-meta.json" review_mode)" "light"
expect_eq "verify-file-reference" "$(json_get "$LIGHT/.review-meta.json" send_mode)" "file-reference"
grep -q 'Review mode: `light`' "$LIGHT/.review-prompt-verify.md" && ok "verify-light-prompt" || bad "verify-light-prompt" "missing light marker"

FULL="$SANDBOX/full"
write_fixture "$FULL"
"$SCRIPT" start --no-spawn --worktree "$FULL" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/full.err"
expect_eq "start-full-exit" "$?" 0
expect_eq "start-full-default" "$(json_get "$FULL/.review-meta.json" review_mode)" "full"
grep -q 'Review mode: `full`' "$FULL/.review-prompt.md" && ok "start-full-prompt" || bad "start-full-prompt" "missing full marker"

BAD="$SANDBOX/bad"
write_fixture "$BAD"
"$SCRIPT" start --light --no-spawn --worktree "$BAD" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/bad-start.err"
python3 - "$BAD/.review-meta.json" <<'PY'
import json
import sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
data["review_mode"] = "sideways"
open(path, "w", encoding="utf-8").write(json.dumps(data, indent=2) + "\n")
PY
"$SCRIPT" verify --no-send --worktree "$BAD" --vault-root "$REPO_ROOT" >/dev/null 2>"$SANDBOX/bad.err"
expect_eq "invalid-mode-exit" "$?" 1
grep -q "review mode must be full or light, got 'sideways'" "$SANDBOX/bad.err" && ok "invalid-mode-message" || bad "invalid-mode-message" "missing value-oriented error"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
if (( fail > 0 )); then
  printf 'Failures:\n'
  printf '  - %s\n' "${failures[@]}"
  exit 1
fi
