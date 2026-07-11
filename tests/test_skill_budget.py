#!/usr/bin/env python3
"""Tests for the Codex initial skill-registry description budget."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-skill-budget.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True)


result = run()
assert result.returncode == 0, result.stderr
assert "25 skills" in result.stdout
print("OK   repository budget")

with tempfile.TemporaryDirectory(prefix="skill-budget-test.") as raw:
    root = Path(raw)
    skill = root / "oversized"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: oversized\ndescription: >-\n  " + "x" * 101 + "\nallowed-tools: Read\n---\n# Body\n",
        encoding="utf-8",
    )
    result = run("--skills", str(root), "--limit", "100")
    assert result.returncode == 1
    assert "SKILL_BUDGET_EXCEEDED" in result.stderr
    print("OK   oversized budget rejected")

print("\nAll skill budget tests passed.")
