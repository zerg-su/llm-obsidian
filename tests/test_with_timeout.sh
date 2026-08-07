#!/usr/bin/env bash
# Regression tests for scripts/with-timeout. Keep this macOS-compatible.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_ROOT/scripts/test-scratch.sh"
WT="$REPO_ROOT/scripts/with-timeout"
SCRATCH="$(llm_obsidian_test_scratch_dir with-timeout-test)"
trap 'rm -rf "$SCRATCH"' EXIT

pass=0
fail=0
failures=()

ok() {
  pass=$((pass + 1))
  printf '  OK   %s\n' "$1"
}

bad() {
  fail=$((fail + 1))
  failures+=("$1: $2")
  printf '  FAIL %s - %s\n' "$1" "$2"
}

expect_exit() {
  local name="$1" got="$2" want="$3"
  [[ "$got" == "$want" ]] && ok "$name" || bad "$name" "exit $got (want $want)"
}

echo "== with-timeout =="

out=$("$WT" 2 sh -c 'printf ok' 2>"$SCRATCH/stderr")
rc=$?
expect_exit "passes-command-exit" "$rc" 0
[[ "$out" == "ok" ]] && ok "passes-command-output" || bad "passes-command-output" "got '$out'"

"$WT" 1 sh -c 'sleep 5' >"$SCRATCH/stdout" 2>"$SCRATCH/stderr"
rc=$?
expect_exit "times-out" "$rc" 124

"$WT" 1 sh -c 'exit 7' >"$SCRATCH/stdout" 2>"$SCRATCH/stderr"
rc=$?
expect_exit "preserves-exit-code" "$rc" 7

"$WT" 1m sh -c 'exit 0' >"$SCRATCH/stdout" 2>"$SCRATCH/stderr"
rc=$?
expect_exit "accepts-minute-suffix" "$rc" 0

"$WT" nope true >"$SCRATCH/stdout" 2>"$SCRATCH/stderr"
rc=$?
expect_exit "bad-duration" "$rc" 2

if (( fail > 0 )); then
  printf '\nFailures:\n'
  printf ' - %s\n' "${failures[@]}"
  exit 1
fi

printf '\nPassed: %d\n' "$pass"
