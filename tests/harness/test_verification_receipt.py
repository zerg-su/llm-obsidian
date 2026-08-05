#!/usr/bin/env python3
"""Immutable gate receipts bind HEAD, argv, exit, and retained output bytes."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import verification_receipt as receipts  # noqa: E402


def git(root: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


with tempfile.TemporaryDirectory(prefix="verification-receipt.") as raw:
    root = Path(raw)
    subject = root / "subject"
    subject.mkdir()
    git(subject, "init", "-b", "task/evidence")
    git(subject, "config", "user.name", "Evidence Test")
    git(subject, "config", "user.email", "evidence@example.invalid")
    (subject / "proof.txt").write_text("proof\n", encoding="utf-8")
    git(subject, "add", "proof.txt")
    git(subject, "commit", "-m", "proof")
    head = git(subject, "rev-parse", "HEAD")
    test_profile = receipts.GateProfile(
        "test-evidence",
        (
            receipts.GateCommand(
                "proof-output",
                (
                    sys.executable,
                    "-c",
                    "print('bounded output')",
                ),
            ),
            receipts.GateCommand(
                "clean-status",
                ("git", "status", "--short"),
                "empty-output",
            ),
        ),
    )
    receipts.PROFILES[test_profile.name] = test_profile
    output = root / "passed"
    receipt = receipts.execute_gate(
        test_profile,
        root=subject,
        output_dir=output,
        expected_head=head,
        execution_relation="release-candidate",
    )
    verified = receipts.verify_receipt(output / "receipt.json")
    assert verified == receipt
    assert verified["subject_head_sha"] == head
    assert verified["commands"][0]["exit_code"] == 0
    assert verified["commands"][1]["observations"] == {"empty": True}
    assert (output / "01-proof-output.log").read_text(encoding="utf-8") == (
        "bounded output\n"
    )
    print("OK   receipt retains and binds exact command output")

    (output / "01-proof-output.log").write_text("tampered\n", encoding="utf-8")
    try:
        receipts.verify_receipt(output / "receipt.json")
    except receipts.ReceiptError as exc:
        assert "digest changed" in str(exc)
    else:
        raise AssertionError("tampered gate output was accepted")
    print("OK   output tampering invalidates the receipt")

    failing = receipts.GateProfile(
        "test-failure",
        (
            receipts.GateCommand(
                "fails", (sys.executable, "-c", "raise SystemExit(7)")
            ),
        ),
    )
    failed_output = root / "failed"
    try:
        receipts.execute_gate(
            failing,
            root=subject,
            output_dir=failed_output,
            expected_head=head,
            execution_relation="exact-head-reconstruction",
        )
    except receipts.ReceiptError as exc:
        assert "exited 7" in str(exc)
    else:
        raise AssertionError("failed gate command was accepted")
    assert not failed_output.exists()
    print("OK   failed gate publishes no partial evidence bundle")

    wrong_head = "f" * 40
    try:
        receipts.execute_gate(
            test_profile,
            root=subject,
            output_dir=root / "wrong-head",
            expected_head=wrong_head,
            execution_relation="release-candidate",
        )
    except receipts.ReceiptError as exc:
        assert "HEAD changed" in str(exc)
    else:
        raise AssertionError("wrong subject HEAD was accepted")
    print("OK   expected HEAD mismatch fails before command execution")

print("verification receipt matrix: ok")
