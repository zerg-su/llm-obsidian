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

    spec = json.loads((ROOT / "evals/acceptance/skills.json").read_text(encoding="utf-8"))
    missing = tmp / "missing.json"
    missing_data = json.loads(json.dumps(spec))
    missing_data["skills"].pop(skills[0])
    missing.write_text(json.dumps(missing_data), encoding="utf-8")
    result = run("--spec", str(missing), "check")
    check("missing skill rejected", result.returncode == 3 and "coverage mismatch" in result.stderr)

    stale = tmp / "stale.json"
    stale_data = json.loads(json.dumps(spec))
    stale_data["skills"]["stale-skill"] = {"scenario": "stale", "expected": "Never exists."}
    stale.write_text(json.dumps(stale_data), encoding="utf-8")
    result = run("--spec", str(stale), "check")
    check("stale skill rejected", result.returncode == 3 and "coverage mismatch" in result.stderr)

    scenarios = json.loads((ROOT / "evals/acceptance/scenarios.json").read_text(encoding="utf-8"))
    missing_scenario = tmp / "missing-scenario.json"
    missing_scenario_data = json.loads(json.dumps(scenarios))
    missing_scenario_data["scenarios"].pop(next(iter(missing_scenario_data["scenarios"])))
    missing_scenario.write_text(json.dumps(missing_scenario_data), encoding="utf-8")
    result = run("--scenarios", str(missing_scenario), "check")
    check("missing scenario rejected", result.returncode == 3 and "scenario coverage mismatch" in result.stderr)

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
    check("expected retained", all(row["expected"] for row in payload["rows"]))
    check("duration measured", all(row["duration_seconds"] is not None for row in payload["rows"]))

    runner.write_text(
        "import json,sys\n"
        "row=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'pass'}))\n",
        encoding="utf-8",
    )
    result = run(
        "run", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("evidence-free pass blocks", result.returncode == 1 and payload["summary"]["verdicts"]["blocked"] > 0)

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

    runner.write_text(
        "import json,sys\n"
        "row=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'n-a','decision':'approved architecture boundary'}))\n",
        encoding="utf-8",
    )
    result = run(
        "run", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("approved n-a is neutral", result.returncode == 0 and payload["summary"]["accepted"] == len(payload["rows"]))

    result = run(
        "run", "--phase", "final", "--runner", str(tmp / "missing-runner"),
        "--timeout", "1", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("missing runner blocks every row", result.returncode == 1 and payload["summary"]["verdicts"]["blocked"] == len(payload["rows"]))

    runner.write_text(
        "import json,sys\n"
        "row=json.load(sys.stdin)\n"
        "print(json.dumps({**row,'verdict':'pass','model':'fixture','effort':'high',"
        "'actual':'token=abcdefghijk','cleanup':'none','evidence':'fixture'}))\n",
        encoding="utf-8",
    )
    result = run(
        "run", "--phase", "final", "--runner", f"{sys.executable} {runner}",
        "--timeout", "5", "--report", str(report),
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    check("credential text sanitized", result.returncode == 0 and all("abcdefghijk" not in row["actual"] for row in payload["rows"]))

print("\nAll release acceptance tests passed.")
