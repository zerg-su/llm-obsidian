#!/usr/bin/env python3
"""Dynamic, sanitized cross-runtime release acceptance matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib_sanitize import residual_credential_kinds, sanitize


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC = ROOT / "evals" / "acceptance" / "skills.json"
DEFAULT_SCENARIOS = ROOT / "evals" / "acceptance" / "scenarios.json"
RUNTIMES = ("claude", "codex")
PHASES = ("baseline", "final")
VERDICTS = {"pass", "fail", "blocked", "n-a"}
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]*")


class AcceptanceError(ValueError):
    pass


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AcceptanceError(f"{path} must contain an object")
    return value


def discovered_skills(root: Path) -> list[str]:
    return sorted(path.parent.name for path in (root / "skills").glob("*/SKILL.md"))


def load_spec(path: Path, root: Path) -> dict[str, dict[str, str]]:
    raw = read_object(path)
    if raw.get("schema_version") != 1 or not isinstance(raw.get("skills"), dict):
        raise AcceptanceError("acceptance spec must use schema_version 1 and contain skills")
    skills: dict[str, dict[str, str]] = {}
    for name, item in raw["skills"].items():
        if not isinstance(name, str) or not SAFE_ID.fullmatch(name) or not isinstance(item, dict):
            raise AcceptanceError(f"invalid skill entry: {name!r}")
        scenario = item.get("scenario")
        expected = item.get("expected")
        fixture = item.get("fixture")
        if not isinstance(scenario, str) or not SAFE_ID.fullmatch(scenario):
            raise AcceptanceError(f"{name}: invalid scenario")
        if not isinstance(expected, str) or not expected.strip() or len(expected) > 300:
            raise AcceptanceError(f"{name}: invalid expected result")
        if not isinstance(fixture, str) or not fixture.strip() or len(fixture) > 1000:
            raise AcceptanceError(f"{name}: invalid live fixture")
        skills[name] = {
            "scenario": scenario,
            "expected": expected.strip(),
            "fixture": fixture.strip(),
        }
    discovered = set(discovered_skills(root))
    declared = set(skills)
    if discovered != declared:
        missing = ", ".join(sorted(discovered - declared)) or "none"
        stale = ", ".join(sorted(declared - discovered)) or "none"
        raise AcceptanceError(f"skill coverage mismatch; missing={missing}; stale={stale}")
    return skills


def validate_scenario_coverage(path: Path, skills: dict[str, dict[str, str]]) -> None:
    raw = read_object(path)
    scenarios = raw.get("scenarios")
    if raw.get("schema_version") != 1 or not isinstance(scenarios, dict):
        raise AcceptanceError("acceptance scenarios must use schema_version 1")
    declared = set(scenarios)
    used = {item["scenario"] for item in skills.values()}
    if declared != used:
        missing = ", ".join(sorted(used - declared)) or "none"
        stale = ", ".join(sorted(declared - used)) or "none"
        raise AcceptanceError(f"scenario coverage mismatch; missing={missing}; stale={stale}")


def matrix_rows(skills: dict[str, dict[str, str]], phase: str) -> list[dict[str, Any]]:
    return [
        {
            "schema_version": 1,
            "phase": phase,
            "skill": name,
            "runtime": runtime,
            "scenario": item["scenario"],
            "expected": item["expected"],
        }
        for name, item in sorted(skills.items())
        for runtime in RUNTIMES
    ]


def validate_result(row: dict[str, Any], result: Any) -> dict[str, Any]:
    if not isinstance(result, dict) or result.get("schema_version") != 1:
        raise AcceptanceError("runner result must be a schema_version 1 object")
    for field in ("phase", "skill", "runtime", "scenario"):
        if result.get(field) != row[field]:
            raise AcceptanceError(f"runner result {field} does not match request")
    verdict = result.get("verdict")
    if verdict not in VERDICTS:
        raise AcceptanceError(f"invalid verdict: {verdict!r}")
    bounded: dict[str, Any] = {key: result.get(key) for key in (
        "schema_version", "phase", "skill", "runtime", "scenario", "expected", "verdict",
        "model", "effort", "actual", "cleanup", "defect", "decision", "evidence",
        "duration_seconds",
    )}
    bounded["expected"] = row["expected"]
    for field in ("model", "effort", "actual", "cleanup", "defect", "decision", "evidence"):
        value = bounded.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > 600):
            raise AcceptanceError(f"result field {field} must be a bounded string")
        if isinstance(value, str):
            clean, _ = sanitize(value)
            residual = residual_credential_kinds(clean)
            if residual:
                raise AcceptanceError(f"result field {field} contains residual credential patterns")
            bounded[field] = clean
    duration = bounded.get("duration_seconds")
    if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0):
        raise AcceptanceError("duration_seconds must be non-negative")
    required = ("model", "effort", "actual", "cleanup", "evidence")
    if verdict in {"pass", "fail"} and any(not str(bounded.get(field) or "").strip() for field in required):
        raise AcceptanceError(f"{verdict} requires model, effort, actual, cleanup, and evidence")
    if verdict in {"fail", "blocked"} and not str(bounded.get("defect") or "").strip():
        raise AcceptanceError(f"{verdict} requires a defect")
    if verdict == "n-a" and not str(bounded.get("decision") or "").strip():
        raise AcceptanceError("n-a requires an explicit decision")
    return bounded


def row_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return tuple(str(row[key]) for key in ("phase", "skill", "runtime", "scenario", "expected"))


def source_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=False
    )
    commit = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise AcceptanceError("cannot resolve the acceptance source commit")
    return commit


def matrix_fingerprint(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_resume_results(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    phase: str,
    commit: str,
    fingerprint: str,
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    report = read_object(path)
    if (
        report.get("schema_version") != 1
        or report.get("phase") != phase
        or report.get("source_commit") != commit
        or report.get("matrix_fingerprint") != fingerprint
        or not isinstance(report.get("rows"), list)
    ):
        raise AcceptanceError(
            "existing acceptance report does not match this commit/matrix; use --restart"
        )
    expected = {row_key(row): row for row in rows}
    resumed: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for raw in report["rows"]:
        if not isinstance(raw, dict):
            raise AcceptanceError("existing acceptance report contains a malformed row")
        try:
            key = row_key(raw)
        except KeyError as exc:
            raise AcceptanceError("existing acceptance report contains a malformed row") from exc
        if key in seen or key not in expected:
            raise AcceptanceError("existing acceptance report contains duplicate or stale rows")
        resumed.append(validate_result(expected[key], raw))
        seen.add(key)
    return resumed


def run_matrix(
    rows: list[dict[str, Any]],
    command: list[str],
    timeout: float,
    *,
    prior: list[dict[str, Any]] | None = None,
    checkpoint: Any = None,
) -> list[dict[str, Any]]:
    prior_by_key = {row_key(item): item for item in (prior or [])}
    results: list[dict[str, Any]] = []
    for row in rows:
        previous = prior_by_key.get(row_key(row))
        if previous is not None:
            results.append(previous)
            continue
        started = datetime.now(timezone.utc)
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(
                    json.dumps(row, ensure_ascii=False) + "\n", timeout=timeout
                )
            except (KeyboardInterrupt, subprocess.TimeoutExpired):
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGINT)
                    try:
                        proc.communicate(timeout=45)
                    except subprocess.TimeoutExpired:
                        proc.terminate()
                        try:
                            proc.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.communicate()
                raise
            proc = subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
            if proc.returncode != 0:
                raise AcceptanceError(f"runner exit {proc.returncode}")
            result = validate_result(row, json.loads(proc.stdout))
        except (AcceptanceError, json.JSONDecodeError, subprocess.TimeoutExpired, OSError) as exc:
            if isinstance(exc, subprocess.TimeoutExpired):
                defect = "acceptance runner timed out"
            elif isinstance(exc, OSError):
                defect = "acceptance runner is unavailable or not executable"
            else:
                defect, _ = sanitize(str(exc)[:300])
            result = {
                **row,
                "verdict": "blocked",
                "actual": "Acceptance runner did not return a valid bounded result.",
                "defect": defect,
            }
        if result.get("duration_seconds") is None:
            result["duration_seconds"] = (datetime.now(timezone.utc) - started).total_seconds()
        results.append(result)
        if checkpoint is not None:
            checkpoint(results)
    return results


def report_payload(
    phase: str,
    rows: list[dict[str, Any]],
    *,
    planned_total: int | None = None,
    commit: str = "",
    fingerprint: str = "",
) -> dict[str, Any]:
    planned = len(rows) if planned_total is None else planned_total
    counts = {verdict: 0 for verdict in sorted(VERDICTS)}
    for row in rows:
        counts[str(row.get("verdict") or "blocked")] += 1
    passed = counts["pass"]
    accepted = passed + counts["n-a"]
    return {
        "schema_version": 1,
        "phase": phase,
        "source_commit": commit,
        "matrix_fingerprint": fingerprint,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": planned, "completed": len(rows), "pending": planned - len(rows),
            "complete": len(rows) == planned,
            "passed": passed, "accepted": accepted,
            "failed": len(rows) - accepted, "verdicts": counts,
        },
        "rows": rows,
    }


def write_json(value: Any, path: Path | None, *, announce: bool = True) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path is None:
        sys.stdout.write(text)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        if announce:
            print(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    matrix = sub.add_parser("matrix")
    matrix.add_argument("--phase", choices=PHASES, required=True)
    matrix.add_argument("--output", type=Path)
    run = sub.add_parser("run")
    run.add_argument("--phase", choices=PHASES, required=True)
    run.add_argument("--runner", required=True)
    run.add_argument("--timeout", type=float, default=900.0)
    run.add_argument("--report", type=Path, required=True)
    run.add_argument("--restart", action="store_true", help="ignore a matching partial/completed report")
    args = parser.parse_args()
    try:
        skills = load_spec(args.spec, args.root.resolve())
        validate_scenario_coverage(args.scenarios, skills)
        if args.command == "check":
            print(f"release-acceptance: {len(skills)} skills x {len(RUNTIMES)} runtimes")
            return 0
        rows = matrix_rows(skills, args.phase)
        if args.command == "matrix":
            write_json({"schema_version": 1, "phase": args.phase, "rows": rows}, args.output)
            return 0
        commit = source_commit(args.root.resolve())
        fingerprint = matrix_fingerprint(rows)
        prior = [] if args.restart else load_resume_results(
            args.report, rows, phase=args.phase, commit=commit, fingerprint=fingerprint
        )

        def checkpoint(completed: list[dict[str, Any]]) -> None:
            write_json(
                report_payload(
                    args.phase, completed, planned_total=len(rows),
                    commit=commit, fingerprint=fingerprint,
                ),
                args.report,
                announce=False,
            )

        checkpoint(prior)
        results = run_matrix(
            rows, shlex.split(args.runner), args.timeout,
            prior=prior, checkpoint=checkpoint,
        )
        report = report_payload(
            args.phase, results, planned_total=len(rows), commit=commit, fingerprint=fingerprint
        )
        write_json(report, args.report)
        return 0 if report["summary"]["failed"] == 0 else 1
    except AcceptanceError as exc:
        print(f"release-acceptance: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
