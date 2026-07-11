#!/usr/bin/env bash
# Hermetic tests for legacy and explicit three-way runtime detection.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/detect-runtime-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/home"

cat > "$TMP/bin/ps" <<'EOF'
#!/usr/bin/env bash
case " $* " in
  *" -o command= "*)
    [ -n "${FAKE_PS_COMMAND:-}" ] && printf '%s\n' "$FAKE_PS_COMMAND"
    ;;
  *" -o ppid= "*) printf '1\n' ;;
esac
EOF
chmod +x "$TMP/bin/ps"

PASS=0
FAIL=0

check() {
  local label="$1" expected="$2"
  shift 2
  local actual
  actual="$(env -i PATH="$TMP/bin:/usr/bin:/bin" HOME="$TMP/home" "$@")"
  if [ "$actual" = "$expected" ]; then
    printf 'OK   %s\n' "$label"
    PASS=$((PASS + 1))
  else
    printf 'FAIL %s: expected=%s actual=%s\n' "$label" "$expected" "$actual" >&2
    FAIL=$((FAIL + 1))
  fi
}

check "three-way Codex env" codex CODEX_THREAD_ID=codex-test "$ROOT/scripts/detect-runtime.sh" --three-way
check "three-way Claude env" claude CLAUDE_CODE_SESSION_ID=claude-test "$ROOT/scripts/detect-runtime.sh" --three-way
check "three-way Codex ancestry" codex FAKE_PS_COMMAND=codex "$ROOT/scripts/detect-runtime.sh" --three-way
check "Codex ancestry outranks inherited Claude env" codex CLAUDE_CODE_SESSION_ID=claude-test FAKE_PS_COMMAND=codex "$ROOT/scripts/detect-runtime.sh" --three-way
check "three-way Claude ancestry" claude FAKE_PS_COMMAND=claude "$ROOT/scripts/detect-runtime.sh" --three-way
check "Claude executable ignores Codex arguments" claude FAKE_PS_COMMAND="claude --print review the codex executor" "$ROOT/scripts/detect-runtime.sh" --three-way
check "Codex executable ignores Claude arguments" codex FAKE_PS_COMMAND="codex exec --model gpt-5 ask claude" "$ROOT/scripts/detect-runtime.sh" --three-way
check "unrelated executable ignores Codex arguments" other FAKE_PS_COMMAND="python3 review-codex-output.py" "$ROOT/scripts/detect-runtime.sh" --three-way
check "hook path is not Claude ancestry" other FAKE_PS_COMMAND="bash /repo/.claude/hooks/stop.sh" "$ROOT/scripts/detect-runtime.sh" --three-way
check "three-way other fallback" other "$ROOT/scripts/detect-runtime.sh" --three-way
check "legacy fallback remains Claude" claude "$ROOT/scripts/detect-runtime.sh"

env -i PATH="$TMP/bin:/usr/bin:/bin" HOME="$TMP/home" "$ROOT/scripts/detect-runtime.sh" --bad > /dev/null 2>&1
if [ "$?" -eq 2 ]; then
  printf 'OK   invalid mode rejected\n'
  PASS=$((PASS + 1))
else
  printf 'FAIL invalid mode rejected\n' >&2
  FAIL=$((FAIL + 1))
fi

printf '\nPassed: %d\nFailed: %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
