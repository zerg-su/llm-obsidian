#!/usr/bin/env bash
# Print the current agent runtime: "codex" or "claude".
#
# Single source of truth for runtime detection, shared by the Claude plugin hooks
# (.claude/hooks/*.sh). Detection order:
#   1. Codex env vars (CODEX_THREAD_ID / CODEX_CI / CODEX_MANAGED_BY_NPM).
#   2. Process ancestry: a `codex` / `Codex.app` ancestor, for runners that do not
#      propagate the Codex environment into child processes.
# Anything else is treated as Claude.
#
# IMPORTANT: the ancestry check MUST live in a file and be invoked as a bare
# command (e.g. `scripts/detect-runtime.sh`), never inlined into a larger shell
# command. `ps -o command=` reports a process's argv, so an inline ancestry check
# would match its own `bash -c '<...codex...>'` wrapper whenever the command text
# contains the string "codex" (case-insensitive on the `[Cc]odex` letters) and
# would false-positive in every runtime. As a file, the parent argv is just this
# script's path, which contains no "codex" substring.
set -u

if [ -n "${CODEX_THREAD_ID:-}${CODEX_CI:-}${CODEX_MANAGED_BY_NPM:-}" ]; then
  printf 'codex\n'
  exit 0
fi

_p="${PPID:-}"
while [ -n "$_p" ] && [ "$_p" != "1" ]; do
  case "$(ps -p "$_p" -o command= 2>/dev/null || true)" in
    *[Cc]odex*|*Codex.app*) printf 'codex\n'; exit 0 ;;
  esac
  _p="$(ps -p "$_p" -o ppid= 2>/dev/null | tr -d ' ' || true)"
done

printf 'claude\n'
