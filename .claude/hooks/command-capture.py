#!/usr/bin/env python3
"""PostToolUse[Bash] hook: sanitized command capture.

Reads the hook JSON payload from stdin and appends a sanitized
{ts, session_id, cwd, command, is_error} record to
.vault-meta/command-log.jsonl. This is raw material for /distill-runbook
(AI-less runbook resilience) and for retrieval-assist usage telemetry
(pipeline-stats.py). The file is gitignored and rotated by stop.sh at 1MB.

Credential-looking values are masked by scripts/lib_sanitize.py (shared
with memory-backup.py) before anything is written. Never raises: capture
must not break the Bash tool call it observes.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(
    os.environ.get("LLM_OBSIDIAN_PROJECT_ROOT")
    or os.environ.get("CLAUDE_PROJECT_DIR")
    or Path(__file__).resolve().parents[2]
).resolve()
COMMAND_LOG = REPO_ROOT / ".vault-meta" / "command-log.jsonl"


def capture_command(payload: dict) -> None:
    try:
        cmd = (payload.get("tool_input") or {}).get("command", "")
        if not cmd:
            return
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from lib_sanitize import residual_credential_kinds, sanitize
        clean_cmd, _ = sanitize(cmd)
        if residual_credential_kinds(clean_cmd):
            return
        resp = payload.get("tool_response") or {}
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "session_id": payload.get("session_id"),
            "cwd": payload.get("cwd"),
            "command": clean_cmd,
            "is_error": bool(resp.get("is_error") or resp.get("interrupted")),
        }
        COMMAND_LOG.parent.mkdir(parents=True, exist_ok=True)
        with COMMAND_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return 0
    capture_command(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
