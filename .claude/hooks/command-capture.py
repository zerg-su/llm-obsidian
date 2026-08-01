#!/usr/bin/env python3
"""PostToolUse allowlisted-shell hook: typed sanitized command capture.

Reads one hook payload from stdin and delegates strict normalization, session
attribution, sanitization, and replay-safe append to command_evidence.py. Tool
output is never persisted. Never raises: capture must not break the shell call
it observes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(
    os.environ.get("LLM_OBSIDIAN_PROJECT_ROOT")
    or os.environ.get("CLAUDE_PROJECT_DIR")
    or Path(__file__).resolve().parents[2]
).resolve()


def capture_command(payload: dict) -> None:
    try:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from command_evidence import capture_agent_payload

        capture_agent_payload(REPO_ROOT, payload)
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
