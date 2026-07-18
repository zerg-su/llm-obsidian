#!/usr/bin/env python3
"""Hermetic tests for the dynamic release acceptance contract."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release-acceptance.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True)


def check(label: str, ok: bool, detail: str = "") -> None:
    if not ok:
        raise SystemExit(f"FAIL {label}: {detail}")
    print(f"OK   {label}")


result = run("check")
check("dynamic coverage", result.returncode == 0 and "runtimes" in result.stdout, result.stderr)

with tempfile.TemporaryDirectory(prefix="release-acceptance-test.") as raw:
    tmp = Path(raw)
    matrix = tmp / "matrix.json"
    result = run("matrix", "--phase", "baseline", "--output", str(matrix))
    data = json.loads(matrix.read_text(encoding="utf-8"))
    skills = sorted(path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md"))
    check("matrix exit", result.returncode == 0, result.stderr)
    check("matrix complete", len(data["rows"]) == len(skills) * 2)
    check("matrix runtime parity", {row["runtime"] for row in data["rows"]} == {"claude", "codex"})

    runner = tmp / "runner.py"
    runner.write_text(
        "import json,sys\n"
        "row=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'pass','model':'fixture','effort':'high',"
        "'actual':'bounded pass','cleanup':'none','evidence':'fixture'}))\n",
        encoding="utf-8",
    )
    report = tmp / "report.json"
    result = run(
        "run", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("green report", result.returncode == 0 and payload["summary"]["failed"] == 0, result.stderr)

    runner.write_text(
        "import json,sys\n"
        "row=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'n-a','decision':''}))\n",
        encoding="utf-8",
    )
    result = run(
        "run", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("invalid n-a blocks", result.returncode == 1 and payload["summary"]["verdicts"]["blocked"] > 0)

print("\nAll release acceptance tests passed.")
