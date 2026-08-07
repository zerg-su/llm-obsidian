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

from harness.review_program_contracts import ReviewBoundaryInput  # noqa: E402
from outcome_contract import extract_from_bytes  # noqa: E402


RELEASE_FIXTURE = json.loads(
    (ROOT / "tests/fixtures/rc4/release-evidence.json").read_text(encoding="utf-8")
)


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
        try:
            store.reserve(
                attempt_id="00000000-0000-4000-8000-000000000099",
                subject_head_sha=subject,
                profile_sha256="a" * 64,
                execution_relation="release-candidate",
                runner_sha256="b" * 64,
            )
        except ledger_module.AttemptLedgerError as exc:
            assert "already reserved" in str(exc)
        else:
            raise AssertionError("concurrent release attempts must fail closed")

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
        for index in range(2, 8):
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
                attempt_id="00000000-0000-4000-8000-000000000008",
                subject_head_sha=subject,
                profile_sha256="a" * 64,
                execution_relation="release-candidate",
                runner_sha256="b" * 64,
            )
        except ledger_module.AttemptLedgerError as exc:
            assert "authorization" in str(exc)
        else:
            raise AssertionError("eighth attempt requires immutable authorization")

        authorization = write_attempt_authorization(root)
        extended = store.authorize_extension(authorization)
        assert extended["maximum_attempts"] == 8
        assert extended["authorizations"][0]["artifact_pointer"] == authorization.name

        eighth = store.reserve(
            attempt_id="00000000-0000-4000-8000-000000000008",
            subject_head_sha=subject,
            profile_sha256="a" * 64,
            execution_relation="release-candidate",
            runner_sha256="b" * 64,
        )
        eighth_artifact = root / "artifacts" / "attempt-8.json"
        eighth_artifact.write_text('{"status":"passed"}\n', encoding="utf-8")
        store.finalize(
            attempt_id=eighth["attempt_id"],
            classification="published",
            exit_status=0,
            artifact_path=eighth_artifact,
        )
        try:
            store.reserve(
                attempt_id="00000000-0000-4000-8000-000000000009",
                subject_head_sha=subject,
                profile_sha256="a" * 64,
                execution_relation="release-candidate",
                runner_sha256="b" * 64,
            )
        except ledger_module.AttemptLedgerError as exc:
            assert "ninth" in str(exc)
        else:
            raise AssertionError("ninth authoritative release attempt must fail closed")

        original_authorization = authorization.read_bytes()
        authorization.write_text('{"tampered":true}\n', encoding="utf-8")
        try:
            store.load()
        except ledger_module.AttemptLedgerError as exc:
            assert "authorization digest drift" in str(exc)
        else:
            raise AssertionError("attempt authorization bytes must be immutable")
        authorization.write_bytes(original_authorization)

        accepted_ledger_bytes = store.path.read_bytes()
        with tempfile.TemporaryDirectory(prefix="rc4-outside-ledger.") as outside_raw:
            outside_authorization = Path(outside_raw) / authorization.name
            outside_authorization.write_bytes(original_authorization)
            authorization.unlink()
            authorization.symlink_to(outside_authorization)
            try:
                try:
                    store.load()
                except ledger_module.AttemptLedgerError as exc:
                    assert "outside ledger root" in str(exc)
                else:
                    raise AssertionError(
                        RELEASE_FIXTURE["authorization_escape"] + " was accepted"
                    )
            finally:
                authorization.unlink()
                authorization.write_bytes(original_authorization)

        assert store.load()["maximum_attempts"] == 8
        assert store.path.read_bytes() == accepted_ledger_bytes


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


def write_attempt_authorization(root: Path) -> Path:
    authorization = root / "attempt-authorization.json"
    authorization.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release": "2.6.6-rc3",
                "authorization_id": "rc3-review-closure-attempt-8",
                "previous_maximum_attempts": 7,
                "maximum_attempts": 8,
                "authorized_by": "operator",
                "finding_ids": [
                    "RC3.RELEASE_EVIDENCE_ARTIFACTS_ABSENT",
                    "RC3.RELEASE_REVIEW_SCHEMA_MISMATCH",
                    "RC3.REVIEW_FINDINGS_NOT_DISPOSITIONED",
                    "rc3-rel-ceiling-extension-not-artifact-bound",
                    "rc3-rel-stale-attempt-ceiling-docs",
                ],
                "rationale": "One bounded final correction after independent release review.",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return authorization


def write_review_bundle(
    root: Path,
    role: str,
    subject: str,
    *,
    boundary_input_sha256: str,
    verdict: str = "approve",
    findings: list[dict[str, object]] | None = None,
) -> tuple[Path, Path]:
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
        "review_boundary_input_sha256": boundary_input_sha256,
        "run_id": run,
        "verification_iteration": 0,
        "verification_profile": {
            "name": disposition._REVIEW_PROFILE.name,
            "sha256": disposition._REVIEW_PROFILE.sha256,
        },
        "route": {
            "runtime": "claude" if role == "fable" else "codex",
            "model": "fable" if role == "fable" else "gpt-5.6-sol",
            "effort": "xhigh",
        },
    }
    payload = {
        "schema_version": 1,
        "axis": axis,
        "parent_session_operation_id": f"review-parent-{role}",
        "verification_iteration": 0,
        "verdict": verdict,
        "findings": findings or [],
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


def write_accepted_deviations(root: Path, subject: str) -> Path:
    path = root / "accepted-deviations.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "integration_head_sha": subject,
                "deviations": [
                    {
                        "id": "historical-rc2-slice-receipts",
                        "disposition": "accepted",
                        "rationale": "Historical RC2 receipts are never reconstructed.",
                    }
                ],
                "forbidden": ["No publication effect."],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_release_boundary(
    root: Path,
    subject: str,
    *,
    plan: Path,
    outcome_evidence: Path,
    accepted_deviations: Path,
) -> tuple[Path, ReviewBoundaryInput]:
    boundary = ReviewBoundaryInput(
        purpose="release",
        outcome_contract_sha256=extract_from_bytes(plan.read_bytes()).sha256,
        plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
        integration_head_sha=subject,
        outcome_evidence_map_sha256=hashlib.sha256(
            outcome_evidence.read_bytes()
        ).hexdigest(),
        outcome_evidence_map_path=outcome_evidence.name,
        accepted_deviations_sha256=hashlib.sha256(
            accepted_deviations.read_bytes()
        ).hexdigest(),
        accepted_deviations_path=accepted_deviations.name,
    )
    path = root / "review-boundary.json"
    path.write_text(
        json.dumps(boundary.payload(), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path, boundary


def test_release_review_bundle_uses_canonical_transport_vocabulary() -> None:
    subject = git("rev-parse", "HEAD")
    for severity in ("critical", "important", "minor"):
        with tempfile.TemporaryDirectory(prefix=f"rc3-review-{severity}.") as raw:
            root = Path(raw)
            finding = {
                "finding_id": f"RC3.REVIEW.{severity.upper()}",
                "severity": severity,
                "file": "scripts/rc3_release_disposition.py",
                "line": 1,
                "summary": "Canonical review finding.",
                "evidence": "Exact callback evidence.",
                "recommendation": "Keep canonical transport vocabulary.",
            }
            meta, callback = write_review_bundle(
                root,
                "fable",
                subject,
                boundary_input_sha256="9" * 64,
                verdict="changes-requested",
                findings=[finding],
            )
            compiled = disposition._review_bundle(
                "fable", meta, callback, subject, "9" * 64
            )
            assert compiled["verdict"] == "changes-requested"
            assert compiled["finding_ids"] == [finding["finding_id"]]

    with tempfile.TemporaryDirectory(prefix="rc3-review-invalid.") as raw:
        root = Path(raw)
        finding = {
            "finding_id": "RC3.REVIEW.WARNING",
            "severity": "warning",
            "file": "scripts/rc3_release_disposition.py",
            "line": 1,
            "summary": "Non-canonical severity.",
            "evidence": "A legacy vocabulary value.",
            "recommendation": "Reject it.",
        }
        meta, callback = write_review_bundle(
            root,
            "fable",
            subject,
            boundary_input_sha256="9" * 64,
            verdict="changes-requested",
            findings=[finding],
        )
        try:
            disposition._review_bundle("fable", meta, callback, subject, "9" * 64)
        except disposition.DispositionError as exc:
            assert "review findings are invalid" in str(exc)
        else:
            raise AssertionError("non-canonical finding severity must fail closed")


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
        for index in range(2, 8):
            attempt_id = f"00000000-0000-4000-8000-{index:012d}"
            ledger.reserve(
                attempt_id=attempt_id,
                subject_head_sha=subject,
                profile_sha256=gate_value["profile_sha256"],
                execution_relation="release-candidate",
                runner_sha256=gate_value["runner_sha256"],
            )
            artifact = evidence / f"attempt-{index}.json"
            artifact.write_text('{"status":"failed"}\n', encoding="utf-8")
            ledger.finalize(
                attempt_id=attempt_id,
                classification="unpublished",
                exit_status=1,
                artifact_path=artifact,
            )
        ledger.authorize_extension(write_attempt_authorization(evidence))

        expected_finding = {
            "finding_id": "RC3.REVIEW.MINOR",
            "severity": "minor",
            "file": "scripts/rc3_release_disposition.py",
            "line": 1,
            "summary": "A fully dispositioned non-blocking review finding.",
            "evidence": "The finding is bound to exact proof bytes.",
            "recommendation": "Keep the exact finding-to-proof set complete.",
        }
        plan = evidence / "approved-plan.md"
        plan.write_text(RELEASE_FIXTURE["plan_markdown"], encoding="utf-8")
        proof = evidence / "focused-tests.log"
        proof.write_text("RC3 focused evidence GREEN\n", encoding="utf-8")
        accepted_deviations = write_accepted_deviations(evidence, subject)
        outcome_evidence = evidence / "outcome-evidence.json"
        outcome_evidence_payload = copy.deepcopy(
            RELEASE_FIXTURE["outcome_evidence_map"]
        )
        outcome_evidence_payload["integration_head_sha"] = subject
        outcome_evidence.write_text(
            json.dumps(outcome_evidence_payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        findings = evidence / "findings.json"
        findings.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "subject_head_sha": subject,
                    "findings": [
                        {
                            "finding_id": "RC3.REVIEW.MINOR",
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
        review_boundary, boundary = write_release_boundary(
            evidence,
            subject,
            plan=plan,
            outcome_evidence=outcome_evidence,
            accepted_deviations=accepted_deviations,
        )

        manifest_rows = []
        for role in ("fable", "independent-configured"):
            meta, callback = write_review_bundle(
                evidence,
                role,
                subject,
                boundary_input_sha256=boundary.input_sha256,
                findings=[expected_finding] if role == "fable" else [],
            )
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

        compile_inputs = {
            "subject_head_sha": subject,
            "gate_receipt_path": gate,
            "attempt_ledger_root": evidence,
            "review_boundary_path": review_boundary,
            "plan_path": plan,
            "outcome_evidence_path": outcome_evidence,
            "review_manifest_path": manifest,
            "finding_evidence_path": findings,
            "accepted_deviations_path": accepted_deviations,
        }

        def compile_current(**overrides: object) -> dict[str, object]:
            return disposition.compile_disposition(
                ROOT, **{**compile_inputs, **overrides}
            )

        def assert_rejected(
            expected_error: str,
            failure: str,
            **overrides: object,
        ) -> None:
            try:
                compile_current(**overrides)
            except disposition.DispositionError as exc:
                assert expected_error in str(exc)
            else:
                raise AssertionError(failure)

        compiled = compile_current()
        assert compiled["outcome"] == "approved"
        assert compiled["review_boundary_input_sha256"] == boundary.input_sha256
        assert compiled["reviews"][0]["verdict"] == "approve"
        assert compiled["accepted_deviations"]["deviation_ids"] == [
            "historical-rc2-slice-receipts"
        ]
        disposition.validate_disposition(
            ROOT,
            compiled,
            gate_receipt_path=gate,
            attempt_ledger_root=evidence,
            review_boundary_path=review_boundary,
            plan_path=plan,
            outcome_evidence_path=outcome_evidence,
            review_manifest_path=manifest,
            finding_evidence_path=findings,
            accepted_deviations_path=accepted_deviations,
        )
        substituted_disposition = copy.deepcopy(compiled)
        substituted_disposition["review_boundary_input_sha256"] = "0" * 64
        try:
            disposition.validate_disposition(
                ROOT,
                substituted_disposition,
                gate_receipt_path=gate,
                attempt_ledger_root=evidence,
                review_boundary_path=review_boundary,
                plan_path=plan,
                outcome_evidence_path=outcome_evidence,
                review_manifest_path=manifest,
                finding_evidence_path=findings,
                accepted_deviations_path=accepted_deviations,
            )
        except disposition.DispositionError as exc:
            assert "bytes do not match compiled evidence" in str(exc)
        else:
            raise AssertionError("substituted compiled disposition was accepted")

        baseline_boundary = json.loads(review_boundary.read_text(encoding="utf-8"))
        for field in RELEASE_FIXTURE["required_release_boundary_fields"]:
            for mutation in ("missing", "empty"):
                candidate = copy.deepcopy(baseline_boundary)
                if mutation == "missing":
                    candidate.pop(field)
                else:
                    candidate[field] = ""
                review_boundary.write_text(
                    json.dumps(candidate, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                assert_rejected(
                    "release review boundary",
                    f"release boundary {field}={mutation} must fail closed",
                )
        review_boundary.write_text(
            json.dumps(baseline_boundary, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        stale_head_boundary = copy.deepcopy(baseline_boundary)
        stale_head_boundary["integration_head_sha"] = "0" * 40
        review_boundary.write_text(
            json.dumps(stale_head_boundary, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        assert_rejected(
            "integration HEAD",
            "release boundary integration HEAD substitution was accepted",
        )
        review_boundary.write_text(
            json.dumps(baseline_boundary, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for field, expected_error in (
            ("outcome_evidence_map_path", "outcome evidence path"),
            ("accepted_deviations_path", "accepted deviations path"),
        ):
            candidate = copy.deepcopy(baseline_boundary)
            candidate[field] = f"substituted-{candidate[field]}"
            review_boundary.write_text(
                json.dumps(candidate, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            assert_rejected(
                expected_error,
                f"release boundary {field} substitution was accepted",
            )
        review_boundary.write_text(
            json.dumps(baseline_boundary, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with tempfile.TemporaryDirectory(
            prefix="rc3-disposition-substitute."
        ) as substitute_raw:
            substituted_outcome = Path(substitute_raw) / outcome_evidence.name
            substituted_outcome.write_bytes(outcome_evidence.read_bytes())
            assert_rejected(
                "outcome evidence path",
                "out-of-root same-suffix outcome evidence was accepted",
                outcome_evidence_path=substituted_outcome,
            )

        with tempfile.TemporaryDirectory(
            prefix="rc3-disposition-symlink."
        ) as substitute_raw:
            outside = Path(substitute_raw) / "linked"
            outside.mkdir()
            outside_outcome = outside / outcome_evidence.name
            outside_outcome.write_bytes(outcome_evidence.read_bytes())
            linked = evidence / "linked"
            linked.symlink_to(outside, target_is_directory=True)
            linked_boundary = copy.deepcopy(baseline_boundary)
            linked_boundary["outcome_evidence_map_path"] = (
                f"linked/{outcome_evidence.name}"
            )
            review_boundary.write_text(
                json.dumps(linked_boundary, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            assert_rejected(
                "outcome evidence path",
                "out-of-root outcome evidence through a symlink component was accepted",
                outcome_evidence_path=linked / outcome_evidence.name,
            )
            linked.unlink()
        review_boundary.write_text(
            json.dumps(baseline_boundary, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        exact_inputs = (
            (
                "plan",
                plan,
                "\n<!-- same HEAD, substituted plan bytes -->\n",
                "plan digest",
            ),
            (
                "outcome_evidence_map",
                outcome_evidence,
                " ",
                "outcome evidence digest",
            ),
            (
                "accepted_deviations",
                accepted_deviations,
                " ",
                "accepted deviations digest",
            ),
        )
        assert [row[0] for row in exact_inputs] == RELEASE_FIXTURE[
            "same_head_substitutions"
        ]
        for _identity, path, suffix, expected_error in exact_inputs:
            original = path.read_bytes()
            path.write_bytes(original + suffix.encode())
            try:
                assert_rejected(
                    expected_error,
                    f"same-HEAD {path.name} substitution was accepted",
                )
            finally:
                path.write_bytes(original)

        original_plan = plan.read_bytes()
        plan.write_bytes(
            original_plan.replace(
                b"Same-HEAD substitutions and escaped authorization artifacts "
                b"fail closed.",
                b"Substituted Outcome Contract bytes must fail closed.",
            )
        )
        outcome_drift_boundary = copy.deepcopy(baseline_boundary)
        outcome_drift_boundary["plan_sha256"] = hashlib.sha256(
            plan.read_bytes()
        ).hexdigest()
        review_boundary.write_text(
            json.dumps(outcome_drift_boundary, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        assert_rejected(
            "Outcome Contract digest",
            "Outcome Contract substitution was accepted",
        )
        plan.write_bytes(original_plan)

        for path, digest_field in (
            (outcome_evidence, "outcome_evidence_map_sha256"),
            (accepted_deviations, "accepted_deviations_sha256"),
        ):
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            rebound_payload = copy.deepcopy(baseline_boundary)
            rebound_payload[digest_field] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            review_boundary.write_text(
                json.dumps(rebound_payload, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            assert_rejected(
                "review boundary",
                f"stale same-HEAD review verdicts were reused for {path.name}",
            )
            path.write_bytes(original)
        review_boundary.write_text(
            json.dumps(baseline_boundary, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        for role in ("fable", "independent-configured"):
            meta_path = evidence / role / ".review-meta.json"
            original_meta = meta_path.read_bytes()
            missing_boundary_meta = json.loads(original_meta)
            missing_boundary_meta.pop("review_boundary_input_sha256")
            meta_path.write_text(
                json.dumps(missing_boundary_meta, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            assert_rejected(
                "review boundary identity",
                f"{role} release review metadata omitted its boundary digest",
            )
            meta_path.write_bytes(original_meta)

        drifted_deviations = evidence / "drifted-accepted-deviations.json"
        drifted_payload = json.loads(accepted_deviations.read_text(encoding="utf-8"))
        drifted_payload["integration_head_sha"] = "0" * 40
        drifted_deviations.write_text(
            json.dumps(drifted_payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        assert_rejected(
            "accepted deviations path",
            "accepted-deviation subject drift must fail closed",
            accepted_deviations_path=drifted_deviations,
        )

        missing = evidence / "missing-findings.json"
        missing.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "subject_head_sha": subject,
                    "findings": [],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        assert_rejected(
            "finding dispositions do not match review findings",
            "missing review finding disposition must fail closed",
            finding_evidence_path=missing,
        )

        extra_payload = json.loads(findings.read_text(encoding="utf-8"))
        extra_payload["findings"].append(
            {
                "finding_id": "RC3.UNRELATED",
                "disposition": "accepted-deviation",
                "evidence": proof.name,
            }
        )
        extra = evidence / "extra-findings.json"
        extra.write_text(json.dumps(extra_payload, sort_keys=True) + "\n", encoding="utf-8")
        assert_rejected(
            "finding dispositions do not match review findings",
            "unrelated review finding disposition must fail closed",
            finding_evidence_path=extra,
        )

        callback_path = evidence / "fable" / ".review-callback.json"
        callback = json.loads(callback_path.read_text(encoding="utf-8"))
        callback["payload"]["verdict"] = "changes-requested"
        callback_path.write_text(json.dumps(callback) + "\n", encoding="utf-8")
        assert_rejected(
            "review receipt identity",
            "callback payload drift must fail closed",
        )


if __name__ == "__main__":
    test_machine_inventory_recomputes_exact_values_and_rejects_drift()
    test_release_attempt_ledger_is_append_only_gap_free_and_artifact_bound()
    test_release_final_runner_records_success_and_failure_without_caller_rows()
    test_prospective_slice_receipts_are_immutable_and_never_backfill_rc2()
    test_coverage_comparator_accepts_only_the_narrow_typed_tolerance()
    test_shell_scratch_helper_honors_constrained_tmpdir()
    test_release_review_bundle_uses_canonical_transport_vocabulary()
    test_release_disposition_binds_actual_gate_reviews_and_findings()
    print("RC3 release evidence contracts passed")
