#!/usr/bin/env python3
"""Hermetic tests for the agent behavioral eval contract and runner."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "agent-evals.py"
CASES = ROOT / "evals" / "cases"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True)


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise SystemExit(f"FAIL {label}: {detail}")
    print(f"OK   {label}")


with tempfile.TemporaryDirectory(prefix="agent-evals-test.") as raw:
    tmp = Path(raw)
    report = tmp / "smoke.json"
    result = run("--cases", str(CASES), "--report", str(report), "smoke")
    check("smoke exit", result.returncode == 0, result.stderr)
    data = json.loads(report.read_text(encoding="utf-8"))
    check("smoke cases", data["summary"]["total"] >= 13)
    check("smoke all pass", data["summary"]["failed"] == 0)

    bad = tmp / "bad.jsonl"
    bad.write_text('{"schema_version":2}\n', encoding="utf-8")
    result = run("--cases", str(bad), "smoke")
    check("invalid schema exit", result.returncode == 3)

    invalid_vocabulary = tmp / "invalid-vocabulary.jsonl"
    invalid_vocabulary.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "engineering.invalid-vocabulary",
                "capability": "codebase-design",
                "input": {"scenario": "choose"},
                "response_contract": {"decision": ["only-one-choice"]},
                "assertions": [
                    {"kind": "equals", "path": "artifacts.decision", "value": "only-one-choice"}
                ],
                "fixture_result": {
                    "output": "choice",
                    "artifacts": {"decision": "only-one-choice"},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = run("--cases", str(invalid_vocabulary), "smoke")
    check("single-choice response vocabulary fails closed", result.returncode == 3)

    runner = tmp / "runner.py"
    runner.write_text(
        "import json,sys\n"
        "case=json.load(sys.stdin)\n"
        "print(json.dumps(case['fixture_result']) if 'fixture_result' in case else "
        "json.dumps({'output':'saved','artifacts':{'address':'c-000047','hot_bullet':'- 2026 — [[X]] — c-000047'}}))\n",
        encoding="utf-8",
    )
    live_report = tmp / "live.json"
    result = run(
        "--cases",
        str(CASES),
        "--capability",
        "save",
        "--report",
        str(live_report),
        "live",
        "--runner",
        f"{sys.executable} {runner}",
        "--trials",
        "2",
    )
    check("live executes", result.returncode in {0, 1}, result.stderr)
    live_data = json.loads(live_report.read_text(encoding="utf-8"))
    check("live trials", live_data["trials"] == 2)
    check("live rows", live_data["summary"]["total"] == 3)

    engineering = tmp / "engineering.json"
    result = run(
        "--cases",
        str(CASES),
        "--capability",
        "codebase-design",
        "--report",
        str(engineering),
        "smoke",
    )
    engineering_data = json.loads(engineering.read_text(encoding="utf-8"))
    check(
        "engineering pressure cases are first-class evals",
        result.returncode == 0
        and engineering_data["summary"] == {"total": 2, "passed": 2, "failed": 0},
    )

print("\nAll agent eval tests passed.")
