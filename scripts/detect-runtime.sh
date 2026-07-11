#!/usr/bin/env bash
# Print the current agent runtime: "codex" or "claude".
# With --three-way, print "codex", "claude", or "other".
#
# Single source of truth for runtime detection, shared by the Claude plugin hooks
# (.claude/hooks/*.sh). Detection order:
#   1. Codex env vars (CODEX_THREAD_ID / CODEX_CI / CODEX_MANAGED_BY_NPM).
#   2. Process ancestry: the executable token is `codex`, `Codex.app`, `claude`,
#      or `Claude.app`, for runners that do not propagate runtime environment
#      variables. Arguments are data and never participate in classification.
#   3. Claude env vars (CLAUDE_CODE_SESSION_ID / CLAUDE_PROJECT_DIR).
# Legacy no-argument mode treats anything else as Claude for hook compatibility.
# Three-way mode reports `other` instead, so portable skills can choose an
# explicit runtime-neutral fallback.
#
# IMPORTANT: the ancestry check MUST live in a file and be invoked as a bare
# command (e.g. `scripts/detect-runtime.sh`), never inlined into a larger shell
# command. `ps -o command=` reports a process's argv, so an inline ancestry check
# would match its own `bash -c '<...codex...>'` wrapper whenever the command text
# contains the string "codex" (case-insensitive on the `[Cc]odex` letters) and
# would false-positive in every runtime. As a file, the parent argv is just this
# script's path, which contains no "codex" substring.
set -u

case "${1:-}" in
  "") _three_way=0 ;;
  --three-way) _three_way=1 ;;
  *) printf 'usage: %s [--three-way]\n' "$0" >&2; exit 2 ;;
esac

if [ -n "${CODEX_THREAD_ID:-}${CODEX_CI:-}${CODEX_MANAGED_BY_NPM:-}" ]; then
  printf 'codex\n'
  exit 0
fi

runtime_from_command() {
  _command="$1"
  _command="${_command#"${_command%%[![:space:]]*}"}"
  _executable="${_command%%[[:space:]]*}"
  _basename="${_executable##*/}"
  _detected=""

  case "$_executable" in
    */Codex.app/Contents/MacOS/*) _detected="codex"; return ;;
    */Claude.app/Contents/MacOS/*) _detected="claude"; return ;;
  esac
  case "$_basename" in
    codex|Codex|CODEX) _detected="codex" ;;
    claude|Claude|CLAUDE) _detected="claude" ;;
  esac
}

_p="${PPID:-}"
while [ -n "$_p" ] && [ "$_p" != "1" ]; do
  runtime_from_command "$(ps -p "$_p" -o command= 2>/dev/null || true)"
  case "$_detected" in
    codex) printf 'codex\n'; exit 0 ;;
    claude) printf 'claude\n'; exit 0 ;;
  esac
  _p="$(ps -p "$_p" -o ppid= 2>/dev/null | tr -d ' ' || true)"
done

if [ -n "${CLAUDE_CODE_SESSION_ID:-}${CLAUDE_PROJECT_DIR:-}" ]; then
  printf 'claude\n'
  exit 0
fi

if [ "$_three_way" = 1 ]; then
  printf 'other\n'
else
  printf 'claude\n'
fi
