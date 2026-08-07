#!/usr/bin/env python3
"""Normalize and compare exact-tree RC3 harness coverage observations."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
MAX_COUNTER_DELTA = 2


class CoverageError(ValueError):
    """Reject invalid or materially drifted coverage evidence."""


def _exact_subject(root: Path, value: object) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise CoverageError("coverage subject must be an exact Git SHA")
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{value}^{{commit}}"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode:
        raise CoverageError("coverage subject commit is unavailable")
    return value


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise CoverageError(f"coverage {field} must be a positive integer")
    return value


def validate_observation(
    payload: object, root: Path = ROOT
) -> dict[str, object]:
    required = {
        "schema_version",
        "subject_head_sha",
        "profile_sha256",
        "covered_lines",
        "executable_lines",
        "weighted_percent",
        "critical_floor_results",
        "transition_matrix_cases",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise CoverageError("coverage observation fields are invalid")
    if payload["schema_version"] != 1:
        raise CoverageError("coverage observation schema is invalid")
    _exact_subject(root, payload["subject_head_sha"])
    profile = payload["profile_sha256"]
    if not isinstance(profile, str) or not DIGEST.fullmatch(profile):
        raise CoverageError("coverage profile digest is invalid")
    covered = _positive_int(payload["covered_lines"], "covered_lines")
    executable = _positive_int(payload["executable_lines"], "executable_lines")
    weighted = payload["weighted_percent"]
    if type(weighted) not in {int, float} or weighted != round(covered * 100 / executable, 2):
        raise CoverageError("coverage weighted percent does not match counters")
    floors = payload["critical_floor_results"]
    if not isinstance(floors, dict) or not floors:
        raise CoverageError("coverage critical floor results are required")
    for module, row in floors.items():
        if (
            not isinstance(module, str)
            or not module
            or not isinstance(row, dict)
            or set(row) != {"percent", "floor", "passed"}
            or type(row["percent"]) not in {int, float}
            or type(row["floor"]) not in {int, float}
            or type(row["passed"]) is not bool
            or row["passed"] != (row["percent"] >= row["floor"])
            or not row["passed"]
        ):
            raise CoverageError("coverage critical floor result is invalid")
    _positive_int(payload["transition_matrix_cases"], "transition_matrix_cases")
    return payload


def compare_observations(
    first: object, second: object, root: Path = ROOT
) -> dict[str, object]:
    left = validate_observation(first, root)
    right = validate_observation(second, root)
    if left["subject_head_sha"] != right["subject_head_sha"]:
        raise CoverageError("coverage subject HEAD drift")
    if left["profile_sha256"] != right["profile_sha256"]:
        raise CoverageError("coverage profile digest drift")
    if left["weighted_percent"] != right["weighted_percent"]:
        raise CoverageError("coverage weighted percent drift")
    if left["critical_floor_results"] != right["critical_floor_results"]:
        raise CoverageError("coverage critical floor drift")
    if left["transition_matrix_cases"] != right["transition_matrix_cases"]:
        raise CoverageError("coverage transition matrix drift")
    covered_delta = int(right["covered_lines"]) - int(left["covered_lines"])
    executable_delta = int(right["executable_lines"]) - int(left["executable_lines"])
    if max(abs(covered_delta), abs(executable_delta)) > MAX_COUNTER_DELTA:
        raise CoverageError("coverage counter delta exceeds typed tolerance")
    return {
        "schema_version": 1,
        "accepted": True,
        "mode": "exact" if covered_delta == executable_delta == 0 else "typed-tolerance",
        "covered_line_delta": covered_delta,
        "executable_line_delta": executable_delta,
        "maximum_counter_delta": MAX_COUNTER_DELTA,
    }


def from_harness_payload(
    root: Path,
    *,
    subject_head_sha: str,
    profile_sha256: str,
    payload: object,
) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise CoverageError("harness coverage payload is invalid")
    if payload.get("errors"):
        raise CoverageError("harness coverage payload contains gate errors")
    floors = payload.get("critical_floors")
    modules = payload.get("modules")
    if not isinstance(floors, dict) or not isinstance(modules, dict):
        raise CoverageError("harness coverage critical results are missing")
    results: dict[str, dict[str, object]] = {}
    for module, floor in floors.items():
        row = modules.get(module)
        if not isinstance(row, dict) or type(row.get("percent")) not in {int, float}:
            raise CoverageError(f"harness coverage module is missing: {module}")
        percent = row["percent"]
        results[module] = {
            "percent": percent,
            "floor": floor,
            "passed": percent >= floor,
        }
    observation = {
        "schema_version": 1,
        "subject_head_sha": subject_head_sha,
        "profile_sha256": profile_sha256,
        "covered_lines": payload.get("covered_lines"),
        "executable_lines": payload.get("executable_lines"),
        "weighted_percent": payload.get("weighted_percent"),
        "critical_floor_results": results,
        "transition_matrix_cases": payload.get("transition_matrix_cases"),
    }
    return validate_observation(observation, root)


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoverageError(f"cannot load coverage JSON: {path}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    observe = subparsers.add_parser("observe")
    observe.add_argument("--coverage-json", type=Path, required=True)
    observe.add_argument("--subject-head-sha", required=True)
    observe.add_argument("--profile-sha256", required=True)
    compare = subparsers.add_parser("compare")
    compare.add_argument("first", type=Path)
    compare.add_argument("second", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "observe":
            result = from_harness_payload(
                ROOT,
                subject_head_sha=args.subject_head_sha,
                profile_sha256=args.profile_sha256,
                payload=_load(args.coverage_json),
            )
        else:
            result = compare_observations(_load(args.first), _load(args.second))
        print(json.dumps(result, indent=2, sort_keys=True))
    except CoverageError as exc:
        print(f"RC3 coverage: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
