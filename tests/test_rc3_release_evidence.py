#!/usr/bin/env python3
"""Focused contracts for the 2.6.6 RC3 release-evidence primitives."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
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
disposition = load_script("rc3_release_disposition.py")


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
        env["TMPDIR"] = f"{constrained}/"
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; scratch=$(llm_obsidian_test_scratch_dir rc3-contract); '
                'case "$scratch" in "$2"/*) ;; *) exit 91 ;; esac; '
                'test -d "$scratch"; printf "%s" "$scratch"; rm -rf "$scratch"',
                "bash",
                str(helper),
                str(constrained),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert f"{constrained}//" not in result.stdout

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


def gate_receipt_bytes(subject_head_sha: str) -> bytes:
    return (
        json.dumps(
            {
                "schema_version": 1,
                "status": "passed",
                "subject_head_sha": subject_head_sha,
                "profile_sha256": "b" * 64,
                "commands": [
                    {
                        "command_id": "full-tests",
                        "exit_code": 0,
                        "output_sha256": "c" * 64,
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def review(role: str) -> dict[str, str]:
    return {
        "role": role,
        "review_id": f"review-{role}",
        "verdict": "approved",
        "receipt_sha256": "d" * 64,
    }


def test_candidate_budget_counts_every_attempt_and_stops_before_six() -> None:
    subject = git("rev-parse", "HEAD")
    attempts = [
        {
            "attempt_id": f"rc3-attempt-{index}",
            "classification": classification,
            "subject_head_sha": subject,
            "profile_sha256": "b" * 64,
            "receipt_sha256": f"{index}" * 64,
            "exit_status": 0 if classification == "published" else 1,
        }
        for index, classification in enumerate(
            ("published", "unpublished", "test-only", "unpublished", "published"),
            start=1,
        )
    ]
    ledger = {"schema_version": 1, "release": "2.6.6-rc3", "attempts": attempts}
    assert disposition.evaluate_attempt_budget(ROOT, ledger) == {
        "schema_version": 1,
        "maximum_attempts": 5,
        "attempts_consumed": 5,
        "attempts_remaining": 0,
        "state": "exhausted",
        "next_attempt_ordinal": None,
        "classification_counts": {
            "published": 2,
            "test-only": 1,
            "unpublished": 2,
        },
    }
    sixth = copy.deepcopy(ledger)
    sixth["attempts"].append({**attempts[-1], "attempt_id": "rc3-attempt-6"})
    try:
        disposition.evaluate_attempt_budget(ROOT, sixth)
    except disposition.DispositionError as exc:
        assert "sixth full-profile attempt" in str(exc)
    else:
        raise AssertionError("a sixth candidate attempt must have zero authority")


def test_release_disposition_binds_gate_reviews_findings_and_waivers() -> None:
    subject = git("rev-parse", "HEAD")
    gate = gate_receipt_bytes(subject)
    ledger = {
        "schema_version": 1,
        "release": "2.6.6-rc3",
        "attempts": [
            {
                "attempt_id": "rc3-attempt-1",
                "classification": "published",
                "subject_head_sha": subject,
                "profile_sha256": "b" * 64,
                "receipt_sha256": hashlib.sha256(gate).hexdigest(),
                "exit_status": 0,
            }
        ],
    }
    reviews = [review("fable"), review("independent-configured")]
    findings = [
        {
            "finding_id": "RC2.E1.MACHINE_INVENTORY_MISSING",
            "disposition": "fixed",
            "evidence_sha256": "e" * 64,
        },
        {
            "finding_id": "RC2.E3.SLICE_RECEIPTS_MISSING",
            "disposition": "waived",
            "waiver_id": "D-266-RC3-RC2-HISTORY",
        },
    ]
    waivers = [
        {
            "waiver_id": "D-266-RC3-RC2-HISTORY",
            "finding_id": "RC2.E3.SLICE_RECEIPTS_MISSING",
            "approved_by": "approved-plan",
            "rationale": "RC2 history remains absent; RC3 receipts are prospective only.",
            "evidence_sha256": "f" * 64,
        }
    ]
    compiled = disposition.compile_disposition(
        ROOT,
        subject_head_sha=subject,
        gate_receipt_bytes=gate,
        attempt_ledger=ledger,
        reviews=reviews,
        findings=findings,
        waivers=waivers,
    )
    assert compiled["outcome"] == "approved"
    assert compiled == disposition.compile_disposition(
        ROOT,
        subject_head_sha=subject,
        gate_receipt_bytes=gate,
        attempt_ledger=ledger,
        reviews=reviews,
        findings=findings,
        waivers=waivers,
    )
    disposition.validate_disposition(ROOT, compiled, gate_receipt_bytes=gate)

    for changed_gate, changed_reviews, changed_waivers, reason in (
        (
            gate_receipt_bytes(BASE_SHA),
            reviews,
            waivers,
            "gate subject does not match candidate HEAD",
        ),
        (gate, reviews[:1], waivers, "both configured review roles"),
        (gate, reviews, [], "waiver coverage is incomplete"),
    ):
        try:
            disposition.compile_disposition(
                ROOT,
                subject_head_sha=subject,
                gate_receipt_bytes=changed_gate,
                attempt_ledger=ledger,
                reviews=changed_reviews,
                findings=findings,
                waivers=changed_waivers,
            )
        except disposition.DispositionError as exc:
            assert reason in str(exc)
        else:
            raise AssertionError(f"release disposition accepted drift: {reason}")

    try:
        disposition.validate_disposition(
            ROOT, compiled, gate_receipt_bytes=gate + b"stale"
        )
    except disposition.DispositionError as exc:
        assert "gate receipt digest drift" in str(exc)
    else:
        raise AssertionError("stale gate bytes must invalidate the disposition")


if __name__ == "__main__":
    test_machine_inventory_recomputes_exact_values_and_rejects_drift()
    test_prospective_slice_receipts_are_immutable_and_never_backfill_rc2()
    test_coverage_comparator_accepts_only_the_narrow_typed_tolerance()
    test_shell_scratch_helper_honors_constrained_tmpdir()
    test_candidate_budget_counts_every_attempt_and_stops_before_six()
    test_release_disposition_binds_gate_reviews_findings_and_waivers()
    print("RC3 release evidence contracts passed")
