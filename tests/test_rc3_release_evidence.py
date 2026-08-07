#!/usr/bin/env python3
"""Focused contracts for the 2.6.6 RC3 release-evidence primitives."""

from __future__ import annotations

import copy
import importlib.util
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_SHA = "b86a33d779bd8852915a4b875f12ef9a9b7366b3"


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


inventory = load_script("rc3_inventory.py")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def independent_script_counts(subject_sha: str) -> tuple[int, int]:
    paths = tuple(
        path
        for path in git("ls-tree", "-r", "--name-only", subject_sha, "--", "scripts").splitlines()
        if path.endswith(".py")
    )
    loc = sum(len(git("show", f"{subject_sha}:{path}").splitlines()) for path in paths)
    return len(paths), loc


def test_machine_inventory_recomputes_exact_values_and_rejects_drift() -> None:
    payload = inventory.build_inventory(ROOT, BASE_SHA, BASE_SHA)
    inventory.validate_inventory(ROOT, payload)

    expected_files, expected_loc = independent_script_counts(BASE_SHA)
    candidate = payload["candidate"]
    assert candidate["commit_sha"] == BASE_SHA
    assert candidate["tree_sha"] == git("rev-parse", f"{BASE_SHA}^{{tree}}")
    assert candidate["script_python_files"] == expected_files
    assert candidate["script_python_loc"] == expected_loc
    assert candidate["classic_paths_present"] == 0
    assert candidate["classic_production_callers"] == 0
    assert candidate["retained_paths_present"] == 6
    assert candidate["runtime_authority_paths_present"] > 0
    assert candidate["writable_authorities"] == 0
    assert payload["dispositions"]["historical_rc2_slice_receipts"] == (
        "accepted-deviation-never-backfill"
    )

    tampered = copy.deepcopy(payload)
    tampered["candidate"]["script_python_loc"] += 1
    try:
        inventory.validate_inventory(ROOT, tampered)
    except inventory.InventoryError as exc:
        assert "candidate counters drift" in str(exc)
    else:
        raise AssertionError("mutated inventory must fail closed")


if __name__ == "__main__":
    test_machine_inventory_recomputes_exact_values_and_rejects_drift()
    print("RC3 release evidence contracts passed")
