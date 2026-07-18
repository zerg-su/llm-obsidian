#!/usr/bin/env python3
"""Hermetic checks for the non-blocking SessionStart preflight."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/session-preflight.py"


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"OK   {name}")


with tempfile.TemporaryDirectory(prefix="session-preflight-test.") as raw:
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(ROOT), "--session-id", "test-session", "--runtime", "codex", "--model", "gpt-5.6-sol", "--effort", "high", "--json"],
        text=True, capture_output=True, env=env, check=False,
    )
    check("preflight never blocks session", result.returncode == 0)
    payload = json.loads(result.stdout)
    check("effective route is visible", payload["routing"]["model"] == "gpt-5.6-sol" and payload["routing"]["source"] == "runtime-environment")
    check("retrieval degradation is explicit", payload["retrieval"] in {"hybrid", "sparse-fallback"})
    check("repairs are exact commands", all(item["repair"] for item in payload["issues"]))
    snapshot = ROOT / ".vault-meta/session-routing/test-session.json"
    check("session route snapshot created", snapshot.is_file())
    snapshot.unlink()

print("session preflight tests passed")
