#!/usr/bin/env python3
"""Build and validate the exact-tree 2.6.6 RC3 machine inventory."""

from __future__ import annotations

import argparse
import ast
import copy
import json
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SHA = re.compile(r"[0-9a-f]{40}\Z")
CLASSIC_PATHS = (
    "scripts/cmux_agent_supervisor.py",
    "scripts/cmux_supervisor_policy.py",
    "scripts/cmux_supervisor_review.py",
    "scripts/cmux_supervisor_contracts.py",
    "scripts/cmux_task_watchdog.py",
    "scripts/cmux_trust_prompt.py",
    "scripts/archive_task_reviews.py",
)
RETAINED_PATHS = (
    "scripts/cmux_agent_support.py",
    "scripts/cmux_surface_lifecycle.py",
    "scripts/cmux_workspace_lifecycle.py",
    "scripts/task_sessions.py",
    "scripts/harness/adapters/cmux.py",
    "scripts/harness/runtime_sessions.py",
)
DISPOSITIONS = {
    "machine_inventory": "implemented-rc3",
    "prospective_slice_receipts": "implemented-prospective-only-rc3",
    "historical_rc2_slice_receipts": "accepted-deviation-never-backfill",
    "coverage_observation_nondeterminism": "typed-tolerance-rc3",
    "portable_shell_scratch": "implemented-rc3",
    "candidate_attempt_budget": "implemented-rc3",
}


class InventoryError(ValueError):
    """Reject an invalid or drifted release inventory."""


def _git(root: Path, *args: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=text,
    )
    if result.returncode:
        raise InventoryError("cannot inspect exact inventory subject")
    return result.stdout


def _exact_commit(root: Path, subject_sha: str) -> str:
    if not isinstance(subject_sha, str) or not SHA.fullmatch(subject_sha):
        raise InventoryError("inventory subject must be an exact commit SHA")
    resolved = str(_git(root, "rev-parse", "--verify", f"{subject_sha}^{{commit}}")).strip()
    if resolved != subject_sha:
        raise InventoryError("inventory subject did not resolve byte-exactly")
    return resolved


def _tree_paths(root: Path, subject_sha: str) -> tuple[str, ...]:
    raw = _git(root, "ls-tree", "-r", "-z", "--full-tree", subject_sha, text=False)
    assert isinstance(raw, bytes)
    paths = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        metadata, encoded_path = entry.split(b"\t", 1)
        if metadata.split(b" ", 2)[1] == b"blob":
            paths.append(encoded_path.decode("utf-8", "surrogateescape"))
    return tuple(sorted(paths))


def _source(root: Path, subject_sha: str, relative: str) -> str:
    value = _git(root, "show", f"{subject_sha}:{relative}")
    assert isinstance(value, str)
    return value


def _classic_callers(
    root: Path, subject_sha: str, script_paths: tuple[str, ...]
) -> int:
    classic_modules = {Path(path).stem for path in CLASSIC_PATHS}
    callers = 0
    for relative in script_paths:
        tree = ast.parse(_source(root, subject_sha, relative), filename=relative)
        imports = {
            node.module.split(".")[-1]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imports.update(
            alias.name.split(".")[-1]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        if imports & classic_modules:
            callers += 1
    return callers


def _authority_counts(root: Path, subject_sha: str) -> tuple[int, int, int]:
    result = subprocess.run(
        [
            "python3",
            "scripts/code-quality-audit.py",
            "--rc1-authority-json",
            "--rc1-authority-subject",
            subject_sha,
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise InventoryError("cannot recompute runtime authority inventory")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise InventoryError("runtime authority inventory is invalid") from exc
    return (
        len(payload["production_files"]),
        len(payload["writable_authorities"]),
        len(payload["incident_literals"]),
    )


def _snapshot(root: Path, subject_sha: str) -> dict[str, Any]:
    subject_sha = _exact_commit(root, subject_sha)
    tree_paths = _tree_paths(root, subject_sha)
    script_paths = tuple(
        path for path in tree_paths if path.startswith("scripts/") and path.endswith(".py")
    )
    runtime_paths, writable, incident_literals = _authority_counts(root, subject_sha)
    return {
        "commit_sha": subject_sha,
        "tree_sha": str(_git(root, "rev-parse", f"{subject_sha}^{{tree}}")).strip(),
        "tracked_files": len(tree_paths),
        "script_python_files": len(script_paths),
        "script_python_loc": sum(
            len(_source(root, subject_sha, path).splitlines()) for path in script_paths
        ),
        "classic_paths_present": len(set(tree_paths) & set(CLASSIC_PATHS)),
        "classic_production_callers": _classic_callers(root, subject_sha, script_paths),
        "retained_paths_present": len(set(tree_paths) & set(RETAINED_PATHS)),
        "runtime_authority_paths_present": runtime_paths,
        "writable_authorities": writable,
        "incident_authority_literals": incident_literals,
    }


@lru_cache(maxsize=16)
def _cached_snapshot(root: str, subject_sha: str) -> dict[str, Any]:
    return _snapshot(Path(root), subject_sha)


def snapshot(root: Path, subject_sha: str) -> dict[str, Any]:
    """Return an isolated copy of the immutable exact-commit snapshot."""

    return copy.deepcopy(_cached_snapshot(str(root.resolve()), subject_sha))


def build_inventory(
    root: Path, baseline_sha: str, candidate_sha: str
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "release": "2.6.6-rc3",
        "counter_scope": "tracked blobs plus scripts/**/*.py physical lines",
        "baseline": snapshot(root, baseline_sha),
        "candidate": snapshot(root, candidate_sha),
        "dispositions": dict(DISPOSITIONS),
    }


def validate_inventory(root: Path, payload: object) -> None:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise InventoryError("inventory schema is invalid")
    if payload.get("release") != "2.6.6-rc3" or payload.get("dispositions") != DISPOSITIONS:
        raise InventoryError("inventory release dispositions drift")
    baseline = payload.get("baseline")
    candidate = payload.get("candidate")
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        raise InventoryError("inventory subjects are missing")
    if baseline != snapshot(root, baseline.get("commit_sha")):
        raise InventoryError("baseline counters drift")
    if candidate != snapshot(root, candidate.get("commit_sha")):
        raise InventoryError("candidate counters drift")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--baseline", required=True)
    build.add_argument("--candidate", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "build":
            payload = build_inventory(ROOT, args.baseline, args.candidate)
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            payload = json.loads(args.path.read_text(encoding="utf-8"))
            validate_inventory(ROOT, payload)
            print("RC3 machine inventory: valid")
    except (InventoryError, OSError, json.JSONDecodeError, SyntaxError) as exc:
        print(f"RC3 machine inventory: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
