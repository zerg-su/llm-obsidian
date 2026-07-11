#!/usr/bin/env python3
"""Hermetic and opt-in live behavioral eval runner for agent workflows.

Cases are versioned JSONL.  ``smoke`` validates every case and grades its
checked-in fixture result.  ``live`` delegates execution to an explicitly
provided runner command, repeats each case, and stores a local JSON report.
The runner receives one case JSON object on stdin and must return one JSON
object on stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "cases"
DEFAULT_REPORT_DIR = ROOT / ".vault-meta" / "evals"
CAPABILITIES = {
    "save",
    "ingest",
    "query",
    "dispatch",
    "review",
    "reap",
    "hooks",
    "retrieval",
    "config",
    "daily",
}
ASSERTIONS = {"equals", "contains", "not_contains", "regex", "empty"}


class EvalConfigError(ValueError):
    """The eval corpus or runner contract is invalid."""


def load_path(value: Any, dotted: str) -> Any:
    current = value
    if not dotted:
        return current
    for part in dotted.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        raise KeyError(dotted)
    return current


def validate_case(case: Any, source: str) -> dict[str, Any]:
    if not isinstance(case, dict):
        raise EvalConfigError(f"{source}: case must be an object")
    required = {"schema_version", "id", "capability", "input", "assertions", "fixture_result"}
    missing = sorted(required - set(case))
    if missing:
        raise EvalConfigError(f"{source}: missing {', '.join(missing)}")
    if case["schema_version"] != 1:
        raise EvalConfigError(f"{source}: unsupported schema_version {case['schema_version']!r}")
    if not isinstance(case["id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]+", case["id"]):
        raise EvalConfigError(f"{source}: invalid id {case['id']!r}")
    if case["capability"] not in CAPABILITIES:
        raise EvalConfigError(f"{source}: invalid capability {case['capability']!r}")
    if not isinstance(case["input"], dict) or not isinstance(case["fixture_result"], dict):
        raise EvalConfigError(f"{source}: input and fixture_result must be objects")
    assertions = case["assertions"]
    if not isinstance(assertions, list) or not assertions:
        raise EvalConfigError(f"{source}: assertions must be a non-empty list")
    for index, assertion in enumerate(assertions):
        label = f"{source}: assertion {index + 1}"
        if not isinstance(assertion, dict) or assertion.get("kind") not in ASSERTIONS:
            raise EvalConfigError(f"{label}: invalid kind")
        if not isinstance(assertion.get("path"), str):
            raise EvalConfigError(f"{label}: path must be a string")
        if assertion["kind"] not in {"empty"} and "value" not in assertion:
            raise EvalConfigError(f"{label}: value is required")
    return case


def load_cases(path: Path) -> list[dict[str, Any]]:
    files = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
    if not files or any(not item.is_file() for item in files):
        raise EvalConfigError(f"no JSONL eval cases found at {path}")
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for file in files:
        for line_no, raw in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            try:
                case = validate_case(json.loads(raw), f"{file}:{line_no}")
            except json.JSONDecodeError as exc:
                raise EvalConfigError(f"{file}:{line_no}: {exc}") from exc
            if case["id"] in seen:
                raise EvalConfigError(f"{file}:{line_no}: duplicate id {case['id']}")
            seen.add(case["id"])
            cases.append(case)
    if not cases:
        raise EvalConfigError(f"no eval cases found at {path}")
    return cases


def grade(case: dict[str, Any], result: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for assertion in case["assertions"]:
        path = assertion["path"]
        kind = assertion["kind"]
        try:
            actual = load_path(result, path)
        except KeyError:
            failures.append(f"{path}: missing")
            continue
        expected = assertion.get("value")
        if kind == "equals" and actual != expected:
            failures.append(f"{path}: expected {expected!r}, got {actual!r}")
        elif kind == "contains" and str(expected) not in str(actual):
            failures.append(f"{path}: missing {expected!r}")
        elif kind == "not_contains" and str(expected) in str(actual):
            failures.append(f"{path}: forbidden {expected!r}")
        elif kind == "regex" and re.search(str(expected), str(actual)) is None:
            failures.append(f"{path}: does not match {expected!r}")
        elif kind == "empty" and actual not in (None, "", [], {}):
            failures.append(f"{path}: expected empty, got {actual!r}")
    return failures


def selected_cases(cases: list[dict[str, Any]], capability: str | None) -> list[dict[str, Any]]:
    chosen = [case for case in cases if capability is None or case["capability"] == capability]
    if not chosen:
        raise EvalConfigError(f"no cases selected for capability {capability!r}")
    return chosen


def smoke(cases: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for case in cases:
        failures = grade(case, case["fixture_result"])
        rows.append({"id": case["id"], "passed": not failures, "failures": failures})
    return report_payload("smoke", 1, rows)


def run_live_case(command: list[str], case: dict[str, Any], timeout: float) -> dict[str, Any]:
    payload = {key: value for key, value in case.items() if key != "fixture_result"}
    proc = subprocess.run(
        command,
        input=json.dumps(payload, ensure_ascii=False) + "\n",
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"runner exit {proc.returncode}: {proc.stderr.strip()[:300]}")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"runner returned invalid JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("runner result must be an object")
    return result


def live(
    cases: list[dict[str, Any]], command: list[str], trials: int, timeout: float
) -> dict[str, Any]:
    rows = []
    for case in cases:
        trial_rows = []
        for trial in range(1, trials + 1):
            try:
                result = run_live_case(command, case, timeout)
                failures = grade(case, result)
                trial_rows.append({"trial": trial, "passed": not failures, "failures": failures})
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                trial_rows.append({"trial": trial, "passed": False, "failures": [str(exc)]})
        rows.append(
            {
                "id": case["id"],
                "passed": all(row["passed"] for row in trial_rows),
                "trials": trial_rows,
            }
        )
    return report_payload("live", trials, rows)


def report_payload(mode: str, trials: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(bool(row["passed"]) for row in rows)
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "trials": trials,
        "summary": {"total": len(rows), "passed": passed, "failed": len(rows) - passed},
        "cases": rows,
    }


def write_report(report: dict[str, Any], path: Path | None) -> None:
    if path is None:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--capability", choices=sorted(CAPABILITIES))
    parser.add_argument("--report", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("smoke")
    live_parser = sub.add_parser("live")
    live_parser.add_argument("--runner", required=True, help="command that reads a case JSON and emits result JSON")
    live_parser.add_argument("--trials", type=int, default=3)
    live_parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()
    try:
        cases = selected_cases(load_cases(args.cases), args.capability)
        if args.command == "smoke":
            report = smoke(cases)
        else:
            if args.trials < 1:
                raise EvalConfigError("--trials must be >= 1")
            report = live(cases, shlex.split(args.runner), args.trials, args.timeout)
        write_report(report, args.report)
        return 0 if report["summary"]["failed"] == 0 else 1
    except EvalConfigError as exc:
        print(f"agent-evals: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
