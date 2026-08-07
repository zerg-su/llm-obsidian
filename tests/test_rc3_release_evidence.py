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
    inventory.validate_inventory(
        ROOT,
        payload,
        expected_baseline_sha=BASE_SHA,
        expected_candidate_sha=BASE_SHA,
    )

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
        inventory.validate_inventory(
            ROOT,
            tampered,
            expected_baseline_sha=BASE_SHA,
            expected_candidate_sha=BASE_SHA,
        )
    except inventory.InventoryError as exc:
        assert "candidate counters drift" in str(exc)
    else:
        raise AssertionError("mutated inventory must fail closed")

    stale = copy.deepcopy(payload)
    stale["candidate"] = inventory.snapshot(ROOT, git("rev-parse", "HEAD"))
    try:
        inventory.validate_inventory(
            ROOT,
            stale,
            expected_baseline_sha=BASE_SHA,
            expected_candidate_sha=BASE_SHA,
        )
    except inventory.InventoryError as exc:
        assert "candidate subject drift" in str(exc)
    else:
        raise AssertionError("embedded candidate identity must not be authoritative")


def test_release_attempt_ledger_is_append_only_gap_free_and_artifact_bound() -> None:
    ledger_module = load_script("rc3_attempt_ledger.py")
    subject = git("rev-parse", "HEAD")
    with tempfile.TemporaryDirectory(prefix="rc3-attempt-ledger.") as raw:
        root = Path(raw)
        store = ledger_module.AttemptLedgerStore(root)
        first = store.reserve(
            attempt_id="00000000-0000-4000-8000-000000000001",
            subject_head_sha=subject,
            profile_sha256="a" * 64,
            execution_relation="release-candidate",
            runner_sha256="b" * 64,
        )
        assert first["ordinal"] == 1 and first["state"] == "reserved"

        artifact = root / "artifacts" / "attempt-1.json"
        artifact.parent.mkdir()
        artifact.write_text('{"status":"passed"}\n', encoding="utf-8")
        completed = store.finalize(
            attempt_id=first["attempt_id"],
            classification="published",
            exit_status=0,
            artifact_path=artifact,
        )
        assert completed["state"] == "published"
        assert completed["artifact_sha256"] == hashlib.sha256(
            artifact.read_bytes()
        ).hexdigest()
        assert store.load()["attempts"] == [completed]

        artifact.write_text('{"status":"tampered"}\n', encoding="utf-8")
        try:
            store.load()
        except ledger_module.AttemptLedgerError as exc:
            assert "artifact digest drift" in str(exc)
        else:
            raise AssertionError("ledger must bind actual immutable artifact bytes")

        artifact.write_text('{"status":"passed"}\n', encoding="utf-8")
        for index in range(2, 6):
            row = store.reserve(
                attempt_id=f"00000000-0000-4000-8000-{index:012d}",
                subject_head_sha=subject,
                profile_sha256="a" * 64,
                execution_relation="release-candidate",
                runner_sha256="b" * 64,
            )
            failed = root / "artifacts" / f"attempt-{index}.json"
            failed.write_text('{"status":"failed"}\n', encoding="utf-8")
            store.finalize(
                attempt_id=row["attempt_id"],
                classification="unpublished",
                exit_status=1,
                artifact_path=failed,
            )
        try:
            store.reserve(
                attempt_id="00000000-0000-4000-8000-000000000006",
                subject_head_sha=subject,
                profile_sha256="a" * 64,
                execution_relation="release-candidate",
                runner_sha256="b" * 64,
            )
        except ledger_module.AttemptLedgerError as exc:
            assert "sixth" in str(exc)
        else:
            raise AssertionError("sixth authoritative release attempt must fail closed")


def test_release_final_runner_records_success_and_failure_without_caller_rows() -> None:
    verification = load_script("verification_receipt.py")
    subject = git("rev-parse", "HEAD")
    with tempfile.TemporaryDirectory(prefix="rc3-runner-ledger.") as raw:
        root = Path(raw)
        ledger_root = root / "ledger"
        staging = root / "staging"
        diagnostics = ledger_root / "diagnostics"
        profile = verification.GateProfile(
            "release-final",
            (
                verification.GateCommand(
                    "proof",
                    ("python3", "-c", "print('bounded proof')"),
                ),
            ),
        )
        passed = ledger_root / "attempts" / "passed"
        passed.parent.mkdir(parents=True)
        verification.execute_gate(
            profile,
            root=ROOT,
            output_dir=passed,
            diagnostic_root=diagnostics,
            staging_root=staging,
            attempt_id="10000000-0000-4000-8000-000000000001",
            expected_head=subject,
            execution_relation="release-candidate",
            attempt_ledger_root=ledger_root,
        )
        rows = load_script("rc3_attempt_ledger.py").AttemptLedgerStore(
            ledger_root
        ).load()["attempts"]
        assert len(rows) == 1 and rows[0]["state"] == "published"
        assert rows[0]["artifact_pointer"] == "attempts/passed/receipt.json"

        failing = verification.GateProfile(
            "release-final",
            (
                verification.GateCommand(
                    "failure",
                    ("python3", "-c", "raise SystemExit(7)"),
                ),
            ),
        )
        try:
            verification.execute_gate(
                failing,
                root=ROOT,
                output_dir=ledger_root / "attempts" / "failed",
                diagnostic_root=diagnostics,
                staging_root=staging,
                attempt_id="10000000-0000-4000-8000-000000000002",
                expected_head=subject,
                execution_relation="release-candidate",
                attempt_ledger_root=ledger_root,
            )
        except verification.ReceiptError as exc:
            assert "exited 7" in str(exc)
        else:
            raise AssertionError("failed release-final attempt was accepted")
        rows = load_script("rc3_attempt_ledger.py").AttemptLedgerStore(
            ledger_root
        ).load()["attempts"]
        assert [row["state"] for row in rows] == ["published", "unpublished"]


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


def write_gate_bundle(root: Path, subject: str) -> Path:
    verification = load_script("verification_receipt.py")
    profile = verification.PROFILES["release-final"]
    bundle = root / "gate"
    bundle.mkdir()
    commands = []
    for index, command in enumerate(profile.commands, 1):
        name = f"{index:02d}-{command.command_id}.log"
        output = bundle / name
        output.write_bytes(b"")
        commands.append(
            {
                "command_id": command.command_id,
                "argv": list(command.argv),
                "cwd": ".",
                "exit_code": 0,
                "started_at": "2026-08-07T00:00:00Z",
                "finished_at": "2026-08-07T00:00:00Z",
                "output_pointer": name,
                "output_sha256": hashlib.sha256(b"").hexdigest(),
                "output_bytes": 0,
                "observations": {},
            }
        )
    receipt = {
        "schema_version": 1,
        "attempt_id": "00000000-0000-4000-8000-000000000001",
        "profile": "release-final",
        "profile_sha256": profile.sha256,
        "subject_head_sha": subject,
        "subject_tree_sha": git("rev-parse", f"{subject}^{{tree}}"),
        "execution_relation": "release-candidate",
        "started_at": "2026-08-07T00:00:00Z",
        "finished_at": "2026-08-07T00:00:00Z",
        "runner": "verification_receipt.py",
        "runner_sha256": hashlib.sha256(
            (ROOT / "scripts/verification_receipt.py").read_bytes()
        ).hexdigest(),
        "commands": commands,
        "status": "passed",
    }
    path = bundle / "receipt.json"
    path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_review_bundle(root: Path, role: str, subject: str) -> tuple[Path, Path]:
    axis = {
        "fable": "anthropic-holistic",
        "independent-configured": "openai-holistic",
    }[role]
    directory = root / role
    directory.mkdir()
    operation = f"review-operation-{role}"
    run = f"review-run-{role}"
    meta = {
        "schema_version": 1,
        "axis": axis,
        "head_sha": subject,
        "operation_id": operation,
        "parent_session_operation_id": f"review-parent-{role}",
        "review_id": f"review-{role}",
        "review_purpose": "release",
        "run_id": run,
        "verification_iteration": 0,
        "verification_profile": {"name": "release-final", "sha256": "e" * 64},
    }
    payload = {
        "schema_version": 1,
        "axis": axis,
        "parent_session_operation_id": f"review-parent-{role}",
        "verification_iteration": 0,
        "verdict": "approved",
        "findings": [],
    }
    callback = {
        "schema_version": 1,
        "callback_id": f"callback-{role}",
        "kind": "review",
        "operation_id": operation,
        "run_id": run,
        "payload": payload,
        "payload_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    meta_path = directory / ".review-meta.json"
    callback_path = directory / ".review-callback.json"
    meta_path.write_text(json.dumps(meta, sort_keys=True) + "\n", encoding="utf-8")
    callback_path.write_text(
        json.dumps(callback, sort_keys=True) + "\n", encoding="utf-8"
    )
    return meta_path, callback_path


def test_release_disposition_binds_actual_gate_reviews_and_findings() -> None:
    subject = git("rev-parse", "HEAD")
    ledger_module = load_script("rc3_attempt_ledger.py")
    with tempfile.TemporaryDirectory(prefix="rc3-disposition.") as raw:
        evidence = Path(raw)
        gate = write_gate_bundle(evidence, subject)
        gate_value = json.loads(gate.read_text(encoding="utf-8"))
        ledger = ledger_module.AttemptLedgerStore(evidence)
        ledger.reserve(
            attempt_id=gate_value["attempt_id"],
            subject_head_sha=subject,
            profile_sha256=gate_value["profile_sha256"],
            execution_relation="release-candidate",
            runner_sha256=gate_value["runner_sha256"],
        )
        ledger.finalize(
            attempt_id=gate_value["attempt_id"],
            classification="published",
            exit_status=0,
            artifact_path=gate,
        )

        manifest_rows = []
        for role in ("fable", "independent-configured"):
            meta, callback = write_review_bundle(evidence, role, subject)
            manifest_rows.append(
                {
                    "role": role,
                    "meta": meta.relative_to(evidence).as_posix(),
                    "callback": callback.relative_to(evidence).as_posix(),
                }
            )
        manifest = evidence / "reviews.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "subject_head_sha": subject,
                    "reviews": manifest_rows,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        proof = evidence / "focused-tests.log"
        proof.write_text("RC3 focused evidence GREEN\n", encoding="utf-8")
        findings = evidence / "findings.json"
        findings.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "subject_head_sha": subject,
                    "findings": [
                        {
                            "finding_id": "RC3.E1.EXACT_HEAD_INVENTORY_DRIFT",
                            "disposition": "fixed",
                            "evidence": proof.name,
                        }
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        compiled = disposition.compile_disposition(
            ROOT,
            subject_head_sha=subject,
            gate_receipt_path=gate,
            attempt_ledger_root=evidence,
            review_manifest_path=manifest,
            finding_evidence_path=findings,
        )
        assert compiled["outcome"] == "approved"
        disposition.validate_disposition(
            ROOT,
            compiled,
            gate_receipt_path=gate,
            attempt_ledger_root=evidence,
            review_manifest_path=manifest,
            finding_evidence_path=findings,
        )

        callback_path = evidence / "fable" / ".review-callback.json"
        callback = json.loads(callback_path.read_text(encoding="utf-8"))
        callback["payload"]["verdict"] = "changes-requested"
        callback_path.write_text(json.dumps(callback) + "\n", encoding="utf-8")
        try:
            disposition.compile_disposition(
                ROOT,
                subject_head_sha=subject,
                gate_receipt_path=gate,
                attempt_ledger_root=evidence,
                review_manifest_path=manifest,
                finding_evidence_path=findings,
            )
        except disposition.DispositionError as exc:
            assert "review receipt identity" in str(exc)
        else:
            raise AssertionError("callback payload drift must fail closed")


if __name__ == "__main__":
    test_machine_inventory_recomputes_exact_values_and_rejects_drift()
    test_release_attempt_ledger_is_append_only_gap_free_and_artifact_bound()
    test_release_final_runner_records_success_and_failure_without_caller_rows()
    test_prospective_slice_receipts_are_immutable_and_never_backfill_rc2()
    test_coverage_comparator_accepts_only_the_narrow_typed_tolerance()
    test_shell_scratch_helper_honors_constrained_tmpdir()
    test_release_disposition_binds_actual_gate_reviews_and_findings()
    print("RC3 release evidence contracts passed")
