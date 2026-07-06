#!/usr/bin/env bash
# Regression suite for .claude/hooks/stop.sh:
#   - cross-session flock (WS1): loser prints STOP_LOCK_BUSY, skips commit, exit 0
#   - auto-commit opt-out (WS4): .vault-meta/auto-commit.disabled short-circuits
#   - happy path: reindex + auto-commit land in git history
#
# Runs against a SANDBOX git repo under mktemp — the live vault is never touched
# (stop.sh resolves VAULT_ROOT from CLAUDE_PROJECT_DIR).
#
# Run from repo root: ./tests/test_stop_hook.sh
set -uo pipefail
export LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$REPO_ROOT/.claude/hooks/stop.sh"
SANDBOX="$(mktemp -d /tmp/stop-hook-test.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

pass=0; fail=0; failures=()
ok()  { pass=$((pass+1)); printf '  OK   %s\n' "$1"; }
bad() { fail=$((fail+1)); failures+=("$1: $2"); printf '  FAIL %s — %s\n' "$1" "$2"; }

# ---------- case 0: plugin Stop hook must run under Codex ----------
python3 - "$REPO_ROOT/hooks/hooks.json" <<'PY' >/dev/null 2>&1
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
assert "LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS=1" in cmd
assert "CODEX_THREAD_ID" not in cmd
assert "CODEX_CI" not in cmd
assert "CODEX_MANAGED_BY_NPM" not in cmd
assert "stop-hook-last.log" in cmd
assert ">" in cmd
PY
[[ "$?" == "0" ]] && ok "json-codex-stop-enabled" || bad "json-codex-stop-enabled" "Stop hook still no-ops under Codex"

# flock binary (same fallback chain as the hook itself)
FLOCK_BIN="$(command -v flock 2>/dev/null || true)"
[ -z "$FLOCK_BIN" ] && [ -x /opt/homebrew/opt/util-linux/bin/flock ] && FLOCK_BIN=/opt/homebrew/opt/util-linux/bin/flock

# ---------- sandbox vault ----------
mkdir -p "$SANDBOX/wiki" "$SANDBOX/.raw" "$SANDBOX/.vault-meta" "$SANDBOX/.claude-memory" "$SANDBOX/scripts"
cp "$REPO_ROOT/scripts/reindex.py" "$SANDBOX/scripts/"
touch "$SANDBOX/.raw/.gitkeep" "$SANDBOX/.claude-memory/.gitkeep"
printf -- '---\ntype: concept\ntitle: "Seed"\nstatus: draft\ncreated: 2026-01-01\nupdated: 2026-01-01\ntags: [seed]\nsessions: []\n---\n\n# Seed\n' \
  > "$SANDBOX/wiki/seed.md"
git -C "$SANDBOX" init -q
git -C "$SANDBOX" config user.email test@test && git -C "$SANDBOX" config user.name test

run_hook() { CLAUDE_PROJECT_DIR="$SANDBOX" bash "$HOOK"; }
commit_count() { git -C "$SANDBOX" rev-list --count HEAD 2>/dev/null || echo 0; }

# ---------- case 1: happy path commits ----------
out=$(run_hook); rc=$?
[[ "$rc" == "0" ]] && ok "sh-happy-exit0" || bad "sh-happy-exit0" "exit $rc"
[[ "$(commit_count)" == "1" ]] && ok "sh-happy-commit" || bad "sh-happy-commit" "commits=$(commit_count) (want 1)"
git -C "$SANDBOX" log --oneline -1 | grep -q '^.* wiki:' && ok "sh-happy-msg" || bad "sh-happy-msg" "no wiki: commit subject"

# ---------- case 2: lock busy -> skip, no commit, exit 0 ----------
if [ -n "$FLOCK_BIN" ]; then
  printf '\nchange\n' >> "$SANDBOX/wiki/seed.md"
  "$FLOCK_BIN" "$SANDBOX/.vault-meta/.stop-hook.lock" -c 'sleep 4' &
  holder=$!
  sleep 0.3   # let the holder grab the lock
  before=$(commit_count)
  out=$(run_hook); rc=$?
  [[ "$rc" == "0" ]] && ok "sh-busy-exit0" || bad "sh-busy-exit0" "exit $rc"
  printf '%s' "$out" | grep -q 'STOP_LOCK_BUSY' && ok "sh-busy-hint" || bad "sh-busy-hint" "no STOP_LOCK_BUSY in output"
  [[ "$(commit_count)" == "$before" ]] && ok "sh-busy-no-commit" || bad "sh-busy-no-commit" "commit happened under held lock"
  wait "$holder" 2>/dev/null

  # ---------- case 3: lock released -> next Stop catches up ----------
  out=$(run_hook); rc=$?
  [[ "$(commit_count)" == "$((before + 1))" ]] && ok "sh-catchup-commit" || bad "sh-catchup-commit" "commits=$(commit_count) (want $((before + 1)))"
else
  printf '  SKIP lock-busy cases — no flock binary available\n'
fi

# ---------- case 4: auto-commit opt-out flag ----------
printf '\nmore\n' >> "$SANDBOX/wiki/seed.md"
touch "$SANDBOX/.vault-meta/auto-commit.disabled"
before=$(commit_count)
out=$(run_hook); rc=$?
[[ "$rc" == "0" ]] && ok "sh-optout-exit0" || bad "sh-optout-exit0" "exit $rc"
printf '%s' "$out" | grep -q 'AUTO_COMMIT_DISABLED' && ok "sh-optout-hint" || bad "sh-optout-hint" "no AUTO_COMMIT_DISABLED in output"
[[ "$(commit_count)" == "$before" ]] && ok "sh-optout-no-commit" || bad "sh-optout-no-commit" "commit happened despite opt-out"
rm "$SANDBOX/.vault-meta/auto-commit.disabled"
out=$(run_hook)
[[ "$(commit_count)" == "$((before + 1))" ]] && ok "sh-optout-reenable" || bad "sh-optout-reenable" "commit did not resume after rm flag"

# ---------- case 5: latency telemetry ----------
LAT="$SANDBOX/.vault-meta/stop-hook-latency.jsonl"
printf '\nlatency probe\n' >> "$SANDBOX/wiki/seed.md"
out=$(run_hook)
[ -f "$LAT" ] && ok "sh-latency-file" || bad "sh-latency-file" "no stop-hook-latency.jsonl written"
tail -1 "$LAT" | python3 -c '
import json, sys
d = json.loads(sys.stdin.read())
assert isinstance(d["total_s"], int)
assert all(k in d for k in ("reindex_s", "bm25_s", "dense_s", "commit_s", "lock", "wiki_dirty"))
assert d["wiki_dirty"] == 1
' 2>/dev/null && ok "sh-latency-schema" || bad "sh-latency-schema" "last line fails schema check"
printf '%s' "$out" | grep -q 'STOP_HOOK_SLOW' && bad "sh-latency-fast-quiet" "SLOW warning on a fast run" || ok "sh-latency-fast-quiet"
# opt-out path must not write telemetry (hook exits before phases)
touch "$SANDBOX/.vault-meta/auto-commit.disabled"
lat_lines_before=$(wc -l < "$LAT" | tr -d '[:space:]')
out=$(run_hook)
lat_lines_after=$(wc -l < "$LAT" | tr -d '[:space:]')
[[ "$lat_lines_after" == "$lat_lines_before" ]] && ok "sh-latency-optout-skip" || bad "sh-latency-optout-skip" "opt-out run appended telemetry"
rm "$SANDBOX/.vault-meta/auto-commit.disabled"

# ---------- summary ----------
printf '\n%d passed, %d failed\n' "$pass" "$fail"
if (( fail > 0 )); then
  printf '\nFailures:\n'
  for f in "${failures[@]}"; do printf '  - %s\n' "$f"; done
  exit 1
fi
