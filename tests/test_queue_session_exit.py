#!/usr/bin/env python3
"""Hermetic exact-surface graceful-exit runner checks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/queue-session-exit.py"
SURFACE = "00000000-0000-0000-0000-000000000123"


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"OK   {label}")


with tempfile.TemporaryDirectory(prefix="queue-session-exit-test.") as raw:
    tmp = Path(raw)
    log = tmp / "cmux.log"
    cmux = tmp / "cmux"
    cmux.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CMUX_TEST_LOG\"\n", encoding="utf-8")
    cmux.chmod(0o755)
    base = dict(os.environ, CMUX_BUNDLED_CLI_PATH=str(cmux), CMUX_SURFACE_ID=SURFACE, CMUX_TEST_LOG=str(log))

    claude_env = {key: value for key, value in base.items() if key not in {"CODEX_THREAD_ID", "CODEX_CI", "CODEX_MANAGED_BY_NPM"}}
    result = subprocess.run([sys.executable, str(SCRIPT)], text=True, capture_output=True, env=claude_env, check=False)
    payload = json.loads(result.stdout)
    calls = log.read_text().splitlines()
    check(
        "Claude queues direct exit to exact surface",
        payload["status"] == "queued"
        and calls == [
            f"send-key --surface {SURFACE} ctrl+u",
            f"send --surface {SURFACE} /exit",
            f"send-key --surface {SURFACE} Enter",
        ],
    )
    check("runner never closes surface", all("close-surface" not in call for call in calls) and payload["surface_closed"] is False)

    log.write_text("", encoding="utf-8")
    codex_env = dict(base, CODEX_THREAD_ID="thread")
    result = subprocess.run([sys.executable, str(SCRIPT)], text=True, capture_output=True, env=codex_env, check=False)
    payload = json.loads(result.stdout)
    calls = log.read_text().splitlines()
    check("Codex uses bounded clear and Tab queue", len(calls) == 42 and calls[-2:] == [f"send --surface {SURFACE} /exit", f"send-key --surface {SURFACE} tab"])
    check("Codex retains manual fallback", payload["manual_fallback"] is True)

    missing_env = dict(claude_env)
    missing_env.pop("CMUX_SURFACE_ID")
    result = subprocess.run([sys.executable, str(SCRIPT)], text=True, capture_output=True, env=missing_env, check=False)
    check("missing exact surface degrades without guessing", json.loads(result.stdout)["status"] == "manual")

print("All session-exit runner tests passed.")
