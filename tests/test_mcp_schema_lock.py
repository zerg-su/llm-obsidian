#!/usr/bin/env python3
"""Offline deterministic MCP inventory/schema lock tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mcp-gateway" / "schema-lock.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True)


with tempfile.TemporaryDirectory(prefix="mcp-schema-lock-test.") as raw:
    tmp = Path(raw)
    snapshot = tmp / "snapshot.json"
    lock = tmp / "lock.json"
    data = {
        "servers": {
            "docs": {
                "tools": [
                    {"name": "search", "description": "private prose must hash", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
                    {"name": "fetch", "description": "fetch", "inputSchema": {"type": "object"}},
                ]
            }
        }
    }
    snapshot.write_text(json.dumps(data), encoding="utf-8")
    result = run("--apply", "--snapshot", str(snapshot), "--lock", str(lock))
    assert result.returncode == 0, result.stderr
    text = lock.read_text(encoding="utf-8")
    assert "private prose" not in text
    assert "description_sha256" in text
    print("OK   lock stores inventory and hashes only")

    result = run("--check", "--snapshot", str(snapshot), "--lock", str(lock))
    assert result.returncode == 0, result.stderr
    print("OK   unchanged snapshot passes")

    data["servers"]["docs"]["tools"][0]["inputSchema"]["required"] = ["q"]
    snapshot.write_text(json.dumps(data), encoding="utf-8")
    result = run("--check", "--snapshot", str(snapshot), "--lock", str(lock))
    assert result.returncode == 1
    assert "MCP_SCHEMA_DRIFT" in result.stderr and "schema changed" in result.stderr
    print("OK   schema drift blocks check")

print("\nAll MCP schema lock tests passed.")
