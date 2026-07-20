#!/usr/bin/env python3
"""Tests for the Codex initial skill-registry description budget."""

from __future__ import annotations

import json
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
assert "28 skills" in result.stdout and "skill bodies:" in result.stdout
print("OK   repository description and body budgets")

with tempfile.TemporaryDirectory(prefix="skill-budget-test.") as raw:
    root = Path(raw)
    skill = root / "oversized"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: oversized\ndescription: >-\n  " + "x" * 101 + "\nallowed-tools: Read\n---\n# Body\n",
        encoding="utf-8",
    )
    baseline = root / "baseline.json"
    result = run("--skills", str(root), "--baseline", str(baseline), "--update-baseline")
    assert result.returncode == 0 and baseline.is_file()
    assert json.loads(baseline.read_text(encoding="utf-8"))["basis"] == (
        "optimized skill bodies and normal-path referenced Markdown"
    )
    result = run("--skills", str(root), "--baseline", str(baseline), "--limit", "100")
    assert result.returncode == 1
    assert "SKILL_BUDGET_EXCEEDED" in result.stderr
    print("OK   oversized budget rejected")

    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    (skill / "SKILL.md").write_text(text + "One extra normal-path line.\n", encoding="utf-8")
    result = run("--skills", str(root), "--baseline", str(baseline), "--limit", "1000")
    assert result.returncode == 1 and "SKILL_BODY_BASELINE_EXCEEDED" in result.stderr
    print("OK   unapproved body growth rejected")

print("\nAll skill budget tests passed.")
