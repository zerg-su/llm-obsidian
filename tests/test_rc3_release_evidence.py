#!/usr/bin/env python3
"""Focused contracts for the 2.6.6 RC3 release-evidence primitives."""

from __future__ import annotations

import copy
import importlib.util
import os
import subprocess
import tempfile
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
slice_receipt = load_script("rc3_slice_receipt.py")
coverage = load_script("rc3_coverage.py")


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


def test_prospective_slice_receipts_are_immutable_and_never_backfill_rc2() -> None:
    subject_sha = git("rev-parse", "HEAD")
    argv = ["python3", "tests/test_rc3_release_evidence.py"]
    with tempfile.TemporaryDirectory() as raw:
        output_root = Path(raw)
        published = slice_receipt.publish_receipt(
            ROOT,
            output_root,
            slice_id="A",
            subject_head_sha=subject_sha,
            argv=argv,
            profile="focused-red-green",
            exit_status=0,
            stdout=b"RC3 release evidence contracts passed\n",
            stderr=b"",
        )
        assert published.status == "published"
        payload = slice_receipt.load_and_validate(ROOT, published.path)
        assert payload["subject_head_sha"] == subject_sha
        assert payload["stdout"] == {
            "bytes": 38,
            "sha256": "a3b1325c0907853615f90ae4caae52343dc3c9625fd228af5079bd5b3b3b634b",
        }
        assert payload["stderr"]["bytes"] == 0

        repeated = slice_receipt.publish_receipt(
            ROOT,
            output_root,
            slice_id="A",
            subject_head_sha=subject_sha,
            argv=argv,
            profile="focused-red-green",
            exit_status=0,
            stdout=b"RC3 release evidence contracts passed\n",
            stderr=b"",
        )
        assert repeated.status == "idempotent" and repeated.path == published.path

        try:
            slice_receipt.publish_receipt(
                ROOT,
                output_root,
                slice_id="A",
                subject_head_sha=subject_sha,
                argv=argv,
                profile="focused-red-green",
                exit_status=1,
                stdout=b"drift\n",
                stderr=b"changed\n",
            )
        except slice_receipt.ReceiptError as exc:
            assert "receipt identity conflict" in str(exc)
        else:
            raise AssertionError("output drift under one execution identity must conflict")

        try:
            slice_receipt.publish_receipt(
                ROOT,
                output_root,
                slice_id="A",
                subject_head_sha=BASE_SHA,
                argv=argv,
                profile="focused-red-green",
                exit_status=0,
                stdout=b"",
                stderr=b"",
            )
        except slice_receipt.ReceiptError as exc:
            assert "historical RC2 backfill" in str(exc)
        else:
            raise AssertionError("RC2 historical receipts must never be reconstructed")

        original_link = slice_receipt._publish_link

        def crash_before_publication(_source: Path, _target: Path) -> None:
            raise OSError("synthetic crash")

        slice_receipt._publish_link = crash_before_publication
        try:
            try:
                slice_receipt.publish_receipt(
                    ROOT,
                    output_root,
                    slice_id="B",
                    subject_head_sha=subject_sha,
                    argv=argv,
                    profile="focused-red-green",
                    exit_status=0,
                    stdout=b"not published",
                    stderr=b"",
                )
            except OSError as exc:
                assert "synthetic crash" in str(exc)
            else:
                raise AssertionError("synthetic publication crash must escape")
        finally:
            slice_receipt._publish_link = original_link
        assert not tuple(output_root.rglob("*.json"))[1:]


def coverage_observation(covered_lines: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "subject_head_sha": git("rev-parse", "HEAD"),
        "profile_sha256": "a" * 64,
        "covered_lines": covered_lines,
        "executable_lines": 200_000,
        "weighted_percent": round(covered_lines * 100 / 200_000, 2),
        "critical_floor_results": {
            "scripts.harness.state_machine": {
                "percent": 98.0,
                "floor": 97.0,
                "passed": True,
            }
        },
        "transition_matrix_cases": 4_370,
    }


def test_coverage_comparator_accepts_only_the_narrow_typed_tolerance() -> None:
    first = coverage_observation(16_855)
    second = coverage_observation(16_853)
    assert first["weighted_percent"] == second["weighted_percent"] == 8.43
    result = coverage.compare_observations(first, second)
    assert result == {
        "schema_version": 1,
        "accepted": True,
        "mode": "typed-tolerance",
        "covered_line_delta": -2,
        "executable_line_delta": 0,
        "maximum_counter_delta": 2,
    }

    for updates, reason in (
        ({"covered_lines": 16_852}, "counter delta exceeds"),
        (
            {"executable_lines": 200_200, "weighted_percent": 8.42},
            "weighted percent drift",
        ),
        ({"transition_matrix_cases": 4_369}, "transition matrix drift"),
    ):
        drifted = copy.deepcopy(second)
        drifted.update(updates)
        try:
            coverage.compare_observations(first, drifted)
        except coverage.CoverageError as exc:
            assert reason in str(exc)
        else:
            raise AssertionError(f"coverage comparator accepted drift: {updates}")


def test_shell_scratch_helper_honors_constrained_tmpdir() -> None:
    helper = ROOT / "scripts/test-scratch.sh"
    with tempfile.TemporaryDirectory() as raw:
        constrained = Path(raw) / "reviewer scratch"
        constrained.mkdir()
        env = os.environ.copy()
        env["TMPDIR"] = str(constrained)
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; scratch=$(llm_obsidian_test_scratch_dir rc3-contract); '
                'case "$scratch" in "$TMPDIR"/*) ;; *) exit 91 ;; esac; '
                'test -d "$scratch"; rm -rf "$scratch"',
                "bash",
                str(helper),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    hardcoded_scratch = (
        "tests/test_allocate_address.sh",
        "tests/test_codex_adapter.sh",
        "tests/test_dcg_assets.sh",
        "tests/test_detect_runtime.sh",
        "tests/test_mcp_gateway.sh",
        "tests/test_memory_backup.sh",
        "tests/test_plan_capture.sh",
        "tests/test_setup_vault.sh",
        "tests/test_stop_hook.sh",
        "tests/test_vault_scripts.sh",
        "tests/test_with_timeout.sh",
        "scripts/dcg-test-suite.sh",
    )
    for relative in hardcoded_scratch:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "mktemp -d /tmp/" not in source
        assert "mktemp /tmp/" not in source
        assert ">/tmp/" not in source


if __name__ == "__main__":
    test_machine_inventory_recomputes_exact_values_and_rejects_drift()
    test_prospective_slice_receipts_are_immutable_and_never_backfill_rc2()
    test_coverage_comparator_accepts_only_the_narrow_typed_tolerance()
    test_shell_scratch_helper_honors_constrained_tmpdir()
    print("RC3 release evidence contracts passed")
