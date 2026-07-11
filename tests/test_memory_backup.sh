#!/usr/bin/env bash
# Hermetic opt-in, sanitization, and fail-closed tests for memory-backup.py.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX="$(mktemp -d /tmp/memory-backup-test.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

pass=0; fail=0; failures=()
ok()  { pass=$((pass+1)); printf '  OK   %s\n' "$1"; }
bad() { fail=$((fail+1)); failures+=("$1: $2"); printf '  FAIL %s - %s\n' "$1" "$2"; }

mkdir -p "$SANDBOX/scripts" "$SANDBOX/source-memory" "$SANDBOX/fake-home/.claude/projects"
cp "$REPO_ROOT/scripts/memory-backup.py" "$REPO_ROOT/scripts/lib_sanitize.py" "$SANDBOX/scripts/"
SCRIPT="$SANDBOX/scripts/memory-backup.py"
SRC="$SANDBOX/source-memory"
BK="$SANDBOX/.claude-memory"
CONFIG="$SANDBOX/.vault-meta/memory-backup.json"

# No environment/config means disabled, even when the old guessed project path exists.
slug=$(printf '%s' "$SANDBOX" | sed 's/[^A-Za-z0-9]/-/g')
guessed="$SANDBOX/fake-home/.claude/projects/$slug/memory"
mkdir -p "$guessed"
printf 'must never be guessed\n' > "$guessed/guessed.md"
out=$(HOME="$SANDBOX/fake-home" python3 "$SCRIPT"); rc=$?
[[ "$rc" == 0 ]] && ok "disabled-exit0" || bad "disabled-exit0" "exit $rc"
printf '%s' "$out" | grep -q 'memory-backup: disabled' && ok "disabled-message" || bad "disabled-message" "$out"
[[ ! -e "$BK" ]] && ok "disabled-no-write" || bad "disabled-no-write" "backup directory created"
out=$(HOME="$SANDBOX/fake-home" python3 "$SCRIPT" --status); rc=$?
printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["enabled"] is False and d["source"] is None and d["reason"] == "not configured"' \
  && ok "disabled-status-json" || bad "disabled-status-json" "$out (exit $rc)"

mkdir -p "$(dirname "$CONFIG")"
printf '{"enabled": false}\n' > "$CONFIG"
out=$(python3 "$SCRIPT"); rc=$?
[[ "$rc" == 0 && ! -e "$BK" ]] && ok "explicit-disabled-no-write" || bad "explicit-disabled-no-write" "exit $rc; backup exists"
rm "$CONFIG"

# Explicit environment opt-in: filter work/index entries, sanitize, sync, and prune.
printf 'token=abcdef123456\npassword=hunter2\nplain text\n' > "$SRC/main.md"
printf 'WhaleKit Jira DEVOPS work memory\n' > "$SRC/project_whalekit.md"
printf -- '- [Work](project_whalekit.md) — excluded index\n' > "$SRC/MEMORY.md"
printf 'ignored non-markdown\n' > "$SRC/not-md.txt"
mkdir -p "$BK"
printf 'old file\n' > "$BK/stale.md"

out=$(CLAUDE_MEMORY_DIR="$SRC" python3 "$SCRIPT"); rc=$?
[[ "$rc" == 0 ]] && ok "env-backup-exit0" || bad "env-backup-exit0" "exit $rc"
printf '%s' "$out" | grep -q '3 source files, 1 included, 2 skipped, 1 updated, 1 pruned, 2 redactions' \
  && ok "env-backup-summary" || bad "env-backup-summary" "$out"
[[ -f "$BK/main.md" && ! -e "$BK/stale.md" ]] && ok "env-sync-prune" || bad "env-sync-prune" "missing main or stale remains"
[[ ! -e "$BK/project_whalekit.md" && ! -e "$BK/MEMORY.md" && ! -e "$BK/not-md.txt" ]] \
  && ok "env-filter" || bad "env-filter" "excluded file copied"
grep -q 'token=REDACTED' "$BK/main.md" && grep -q 'password: REDACTED' "$BK/main.md" \
  && ok "env-redactions" || bad "env-redactions" "sanitized values missing"
grep -q 'hunter2' "$BK/main.md" && bad "env-raw-secret-absent" "raw password present" || ok "env-raw-secret-absent"

out=$(CLAUDE_MEMORY_DIR="$SRC" python3 "$SCRIPT" --check); rc=$?
[[ "$rc" == 0 ]] && ok "check-fresh-exit0" || bad "check-fresh-exit0" "exit $rc: $out"
printf '%s' "$out" | grep -q 'backup fresh' && ok "check-fresh-output" || bad "check-fresh-output" "$out"
printf '\nnew line\n' >> "$SRC/main.md"
out=$(CLAUDE_MEMORY_DIR="$SRC" python3 "$SCRIPT" --check); rc=$?
[[ "$rc" == 1 ]] && ok "check-stale-exit1" || bad "check-stale-exit1" "exit $rc: $out"
printf '%s' "$out" | grep -q 'changed=1' && ok "check-stale-output" || bad "check-stale-output" "$out"
CLAUDE_MEMORY_DIR="$SRC" python3 "$SCRIPT" >/dev/null
rm "$BK/main.md"
out=$(CLAUDE_MEMORY_DIR="$SRC" python3 "$SCRIPT" --check); rc=$?
[[ "$rc" == 1 ]] && ok "check-missing-exit1" || bad "check-missing-exit1" "exit $rc: $out"
printf '%s' "$out" | grep -q 'missing=1' && ok "check-missing-output" || bad "check-missing-output" "$out"
out=$(CLAUDE_MEMORY_DIR="$SANDBOX/missing" python3 "$SCRIPT" --check 2>&1); rc=$?
[[ "$rc" == 2 ]] && ok "missing-source-exit2" || bad "missing-source-exit2" "exit $rc: $out"

# Local config is the second and only other opt-in mechanism.
printf '{"enabled": true, "source": "%s"}\n' "$SRC" > "$CONFIG"
out=$(python3 "$SCRIPT"); rc=$?
[[ "$rc" == 0 && -f "$BK/main.md" ]] && ok "config-opt-in" || bad "config-opt-in" "exit $rc: $out"
out=$(python3 "$SCRIPT" --status)
printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["enabled"] and d["reason"] == "config" and d["source_exists"]' \
  && ok "config-status" || bad "config-status" "$out"

# Residual secrets block before every write/prune. Existing backup remains intact.
before_main=$(shasum -a 256 "$BK/main.md" | awk '{print $1}')
printf 'keep until safe\n' > "$BK/must-stay.md"
printf '%s\n%s\n%s\n' \
  '-----BEGIN '"PRIVATE KEY"'-----' 'not-a-real-test-key' '-----END '"PRIVATE KEY"'-----' \
  > "$SRC/leak.md"
out=$(python3 "$SCRIPT" 2>&1); rc=$?
[[ "$rc" == 3 ]] && ok "residual-source-exit3" || bad "residual-source-exit3" "exit $rc: $out"
printf '%s' "$out" | grep -q 'source/leak.md: private-key' && ok "residual-source-label" || bad "residual-source-label" "$out"
[[ ! -e "$BK/leak.md" && -e "$BK/must-stay.md" ]] && ok "residual-source-no-mutation" || bad "residual-source-no-mutation" "write/prune occurred"
after_main=$(shasum -a 256 "$BK/main.md" | awk '{print $1}')
[[ "$before_main" == "$after_main" ]] && ok "residual-source-main-unchanged" || bad "residual-source-main-unchanged" "main changed"

rm "$SRC/leak.md" "$BK/must-stay.md"
printf '%s\n%s\n%s\n' \
  '-----BEGIN OPENSSH '"PRIVATE KEY"'-----' 'not-a-real-test-key' '-----END OPENSSH '"PRIVATE KEY"'-----' \
  > "$BK/old-leak.md"
out=$(python3 "$SCRIPT" --check 2>&1); rc=$?
[[ "$rc" == 3 ]] && ok "residual-existing-exit3" || bad "residual-existing-exit3" "exit $rc: $out"
printf '%s' "$out" | grep -q 'backup/old-leak.md: private-key' && ok "residual-existing-label" || bad "residual-existing-label" "$out"
rm "$BK/old-leak.md"

# Known token forms are redacted; the residual layer blocks only what remains.
printf 'github token %s%s\n' 'ghp_' 'ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890' > "$SRC/github.md"
out=$(python3 "$SCRIPT"); rc=$?
[[ "$rc" == 0 ]] && ok "known-token-sanitized" || bad "known-token-sanitized" "exit $rc: $out"
grep -q 'gh\*_REDACTED' "$BK/github.md" && ok "known-token-redaction-output" || bad "known-token-redaction-output" "redaction missing"
python3 "$SCRIPT" --check >/dev/null
[[ "$?" == 0 ]] && ok "known-token-check-clean" || bad "known-token-check-clean" "check failed"

printf '{"enabled": true}\n' > "$CONFIG"
out=$(python3 "$SCRIPT" 2>&1); rc=$?
[[ "$rc" == 2 ]] && ok "invalid-config-exit2" || bad "invalid-config-exit2" "exit $rc: $out"

python3 - "$SANDBOX/scripts" <<'PYEOF' >/dev/null
import sys
sys.path.insert(0, sys.argv[1])
from lib_sanitize import residual_credential_kinds, sanitize
clean, _ = sanitize("token=abcdef123456 Bearer abcdefghijklmnop " + "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890")
assert not residual_credential_kinds(clean), clean
assert residual_credential_kinds("-----BEGIN " + "PRIVATE KEY-----\nx\n") == ["private-key"]
PYEOF
[[ "$?" == 0 ]] && ok "sanitizer-residual-unit" || bad "sanitizer-residual-unit" "python assertion failed"

# Command capture shares the sanitizer and drops an event if anything remains.
printf '%s' '{"tool_input":{"command":"token=abcdef123456 echo safe"},"tool_response":{}}' \
  | CLAUDE_PROJECT_DIR="$SANDBOX" python3 "$REPO_ROOT/.claude/hooks/command-capture.py"
COMMAND_LOG="$SANDBOX/.vault-meta/command-log.jsonl"
grep -q 'token=REDACTED' "$COMMAND_LOG" && ok "capture-known-token-redacted" || bad "capture-known-token-redacted" "redaction missing"
before_lines=$(wc -l < "$COMMAND_LOG" | tr -d ' ')
python3 - <<'PYEOF' \
  | CLAUDE_PROJECT_DIR="$SANDBOX" python3 "$REPO_ROOT/.claude/hooks/command-capture.py"
import json
print(json.dumps({"tool_input": {"command": "-----BEGIN " + "PRIVATE KEY-----"}, "tool_response": {}}))
PYEOF
after_lines=$(wc -l < "$COMMAND_LOG" | tr -d ' ')
[[ "$before_lines" == "$after_lines" ]] && ok "capture-residual-event-dropped" || bad "capture-residual-event-dropped" "unsafe event appended"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
if (( fail > 0 )); then
  printf '\nFailures:\n'
  for item in "${failures[@]}"; do printf '  - %s\n' "$item"; done
  exit 1
fi
