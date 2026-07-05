#!/usr/bin/env bash
# command-capture.sh — PostToolUse[Bash] hook wrapper.
#
# Reads the stdin JSON payload from Claude Code:
#   {"tool_name": "Bash", "tool_input": {"command": "..."},
#    "tool_response": {"stdout": "...", "stderr": "..."}}
#
# Appends a sanitized command record to .vault-meta/command-log.jsonl
# (see command-capture.py). No output; never blocks the tool call.

set -u

exec python3 "$(dirname "$0")/command-capture.py"
