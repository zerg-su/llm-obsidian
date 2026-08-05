#!/usr/bin/env python3
"""Immutable gate receipts bind HEAD, argv, exit, and retained output bytes."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import verification_receipt as receipts  # noqa: E402


PASS_ATTEMPT = "11111111-1111-4111-8111-111111111111"
FAIL_ATTEMPT = "22222222-2222-4222-8222-222222222222"
ISOLATED_ATTEMPT = "33333333-3333-4333-8333-333333333333"
WRONG_HEAD_ATTEMPT = "44444444-4444-4444-8444-444444444444"
BOOTSTRAP_ATTEMPT = "55555555-5555-4555-8555-555555555555"
BOOTSTRAP_FAIL_ATTEMPT = "66666666-6666-4666-8666-666666666666"


def git(root: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def bootstrap_subject(root: Path, *, bootstrap_exit: int = 0) -> tuple[Path, str]:
    subject = root
    gateway = subject / "scripts" / "mcp-gateway" / "mcp-gateway.sh"
    gateway.parent.mkdir(parents=True)
    git(subject, "init", "-b", "task/bootstrap")
    git(subject, "config", "user.name", "Bootstrap Test")
    git(subject, "config", "user.email", "bootstrap@example.invalid")
    (subject / ".gitignore").write_text(
        "scripts/mcp-gateway/runtime.env\n"
        "scripts/mcp-gateway/codex-sync-ran\n",
        encoding="utf-8",
    )
    gateway.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "if [ \"$1:$2\" = \"sync-config:--apply\" ]; then\n"
        f"  [ {bootstrap_exit} -eq 0 ] || exit {bootstrap_exit}\n"
        "  printf '%s\\n' initialized > scripts/mcp-gateway/runtime.env\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$1:$2\" = \"codex-sync:--check\" ]; then\n"
        "  printf '%s\\n' ran > scripts/mcp-gateway/codex-sync-ran\n"
        "  test -f scripts/mcp-gateway/runtime.env\n"
        "  exit 0\n"
        "fi\n"
        "exit 64\n",
        encoding="utf-8",
    )
    gateway.chmod(0o755)
    git(subject, "add", ".gitignore", "scripts/mcp-gateway/mcp-gateway.sh")
    git(subject, "commit", "-m", "bootstrap fixture")
    return subject, git(subject, "rev-parse", "HEAD")


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
    diagnostic_root = root / "diagnostics"
    receipt = receipts.execute_gate(
        test_profile,
        root=subject,
        output_dir=output,
        diagnostic_root=diagnostic_root,
        attempt_id=PASS_ATTEMPT,
        expected_head=head,
        execution_relation="release-candidate",
    )
    verified = receipts.verify_receipt(output / "receipt.json")
    assert verified == receipt
    assert verified["attempt_id"] == PASS_ATTEMPT
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
            diagnostic_root=diagnostic_root,
            attempt_id=FAIL_ATTEMPT,
            expected_head=head,
            execution_relation="exact-head-reconstruction",
        )
    except receipts.ReceiptError as exc:
        assert "exited 7" in str(exc)
    else:
        raise AssertionError("failed gate command was accepted")
    assert not failed_output.exists()
    failed_receipt_path = (
        diagnostic_root / FAIL_ATTEMPT / "diagnostic-receipt.json"
    )
    failed_receipt = json.loads(
        failed_receipt_path.read_text(encoding="utf-8")
    )
    failed_log = diagnostic_root / FAIL_ATTEMPT / "command.log"
    failed_raw = failed_log.read_bytes()
    assert failed_receipt["evidence_disposition"] == "not-verification-evidence"
    assert failed_receipt["attempt_id"] == FAIL_ATTEMPT
    assert failed_receipt["subject_head_sha"] == head
    assert failed_receipt["profile_sha256"] == failing.sha256
    assert failed_receipt["command_index"] == 1
    assert failed_receipt["command_id"] == "fails"
    assert failed_receipt["exit_code"] == 7
    assert failed_receipt["failure_kind"] == "command-exit"
    assert failed_receipt["stdout_stderr_bytes"] == len(failed_raw)
    assert failed_receipt["stdout_stderr_sha256"] == hashlib.sha256(
        failed_raw
    ).hexdigest()
    print("OK   failed gate retains a complete non-evidence diagnostic bundle")
    print("OK   failed gate publishes no partial evidence bundle")

    isolated_output = root / "isolated-failed"
    try:
        receipts.execute_gate(
            failing,
            root=subject,
            output_dir=isolated_output,
            diagnostic_root=diagnostic_root,
            attempt_id=ISOLATED_ATTEMPT,
            expected_head=head,
            execution_relation="exact-head-reconstruction",
        )
    except receipts.ReceiptError:
        pass
    else:
        raise AssertionError("isolated failed attempt was accepted")
    first_bytes = failed_receipt_path.read_bytes()
    reuse_output = root / "reused-failed"
    try:
        receipts.execute_gate(
            failing,
            root=subject,
            output_dir=reuse_output,
            diagnostic_root=diagnostic_root,
            attempt_id=FAIL_ATTEMPT,
            expected_head=head,
            execution_relation="exact-head-reconstruction",
        )
    except receipts.ReceiptError as exc:
        assert "already has diagnostics" in str(exc)
    else:
        raise AssertionError("failed attempt identity was reused")
    assert failed_receipt_path.read_bytes() == first_bytes
    assert not reuse_output.exists()
    assert (
        diagnostic_root / ISOLATED_ATTEMPT / "diagnostic-receipt.json"
    ).is_file()
    print("OK   failed diagnostics are isolated by immutable attempt identity")

    wrong_head = "f" * 40
    try:
        receipts.execute_gate(
            test_profile,
            root=subject,
            output_dir=root / "wrong-head",
            diagnostic_root=diagnostic_root,
            attempt_id=WRONG_HEAD_ATTEMPT,
            expected_head=wrong_head,
            execution_relation="release-candidate",
        )
    except receipts.ReceiptError as exc:
        assert "HEAD changed" in str(exc)
    else:
        raise AssertionError("wrong subject HEAD was accepted")
    print("OK   expected HEAD mismatch fails before command execution")

    stability = receipts.PROFILES["stability-gate"]
    stability_ids = tuple(item.command_id for item in stability.commands)
    bootstrap_index = stability_ids.index("mcp-sync-config")
    assert stability_ids[bootstrap_index : bootstrap_index + 2] == (
        "mcp-sync-config",
        "codex-mcp-sync",
    )
    bootstrap_profile = receipts.GateProfile(
        "test-stability-bootstrap",
        (
            stability.commands[bootstrap_index],
            stability.commands[bootstrap_index + 1],
            receipts.GateCommand(
                "clean-status",
                ("git", "status", "--short"),
                "empty-output",
            ),
        ),
    )
    receipts.PROFILES[bootstrap_profile.name] = bootstrap_profile
    fresh, fresh_head = bootstrap_subject(root / "fresh-bootstrap")
    bootstrap_output = root / "bootstrap-passed"
    bootstrap_receipt = receipts.execute_gate(
        bootstrap_profile,
        root=fresh,
        output_dir=bootstrap_output,
        diagnostic_root=diagnostic_root,
        attempt_id=BOOTSTRAP_ATTEMPT,
        expected_head=fresh_head,
        execution_relation="exact-head-reconstruction",
    )
    runtime_env = fresh / "scripts" / "mcp-gateway" / "runtime.env"
    assert runtime_env.read_text(encoding="utf-8") == "initialized\n"
    assert git(fresh, "status", "--short") == ""
    assert subprocess.run(
        ["git", "check-ignore", "scripts/mcp-gateway/runtime.env"],
        cwd=fresh,
        check=False,
    ).returncode == 0
    assert [
        item["command_id"] for item in bootstrap_receipt["commands"]
    ] == ["mcp-sync-config", "codex-mcp-sync", "clean-status"]
    print("OK   fresh stability checkout bootstraps ignored config before sync")

    failing_subject, failing_head = bootstrap_subject(
        root / "failed-bootstrap", bootstrap_exit=9
    )
    failing_bootstrap_output = root / "bootstrap-failed"
    try:
        receipts.execute_gate(
            bootstrap_profile,
            root=failing_subject,
            output_dir=failing_bootstrap_output,
            diagnostic_root=diagnostic_root,
            attempt_id=BOOTSTRAP_FAIL_ATTEMPT,
            expected_head=failing_head,
            execution_relation="exact-head-reconstruction",
        )
    except receipts.ReceiptError as exc:
        assert "mcp-sync-config exited 9" in str(exc)
    else:
        raise AssertionError("failed stability bootstrap was accepted")
    assert not failing_bootstrap_output.exists()
    assert (
        diagnostic_root
        / BOOTSTRAP_FAIL_ATTEMPT
        / "diagnostic-receipt.json"
    ).is_file()
    assert not (
        failing_subject / "scripts" / "mcp-gateway" / "codex-sync-ran"
    ).exists()
    print("OK   failed stability bootstrap publishes nothing and stops sync")

print("verification receipt matrix: ok")
