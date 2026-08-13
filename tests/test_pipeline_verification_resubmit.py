#!/usr/bin/env python3
"""Regression checks for code-owned verification resubmission."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pipeline-verification-resubmit.py"
sys.path.insert(0, str(ROOT / "scripts"))

from harness.verification_attempt import VerificationAttempt  # noqa: E402
import task_escalation  # noqa: E402
from task_escalation_records import load_latest  # noqa: E402


with tempfile.TemporaryDirectory(prefix="verification-resubmit.") as raw:
    worktree = Path(raw)
    (worktree / ".task-meta.json").write_text(
        json.dumps(
            {
                "worktree": str(worktree.resolve()),
                "project_id": "33333333-3333-4333-8333-333333333333",
                "task_id": "44444444-4444-4444-8444-444444444444",
                "origin_session": "55555555-5555-4555-8555-555555555555",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=worktree, check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=worktree, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Verification Resubmit"],
        cwd=worktree, check=True,
    )
    product = worktree / "product.txt"
    product.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.txt"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "before"], cwd=worktree, check=True,
        capture_output=True,
    )
    failed_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree, check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    attempt_0 = VerificationAttempt(
        parent_operation_id="parent-operation",
        profile="scoped",
        profile_sha256="a" * 64,
        exact_head_sha=failed_head,
        attempt_index=0,
    )
    packet = {
        "schema_version": 2,
        "operation_id": "parent-operation",
        "verification_operation_id": "verify-operation",
        "verification_lane_id": "verify-lane",
        "verification_run_id": "verify-run",
        "definition_sha256": "a" * 64,
        "step_id": "verify",
        "head_sha": failed_head,
        "status": "attention-required",
        "reason": "verification-failed",
        "safe_boundary": "tdd-slices-complete",
        "allowed_responses": [
            "fix-and-resubmit",
            "retry-mechanism-flake",
            "escalate",
        ],
        "response_pointer": ".task-verification-response.json",
        "receipt_pointer": "/trusted/receipt.json",
        "evidence": [],
        "verification_attempt": attempt_0.as_dict(),
        "verification_attempt_sha256": attempt_0.sha256,
    }
    packet_path = worktree / ".task-verification.json"
    packet_path.write_text(
        json.dumps(packet, sort_keys=True) + "\n", encoding="utf-8"
    )
    blocked = subprocess.run(
        ["python3", str(SCRIPT), "--worktree", str(worktree)],
        text=True, capture_output=True, check=False,
    )
    if (
        blocked.returncode == 0
        or "commit a new HEAD or provide exact same-HEAD mechanism-flake authorization"
        not in blocked.stderr
    ):
        raise AssertionError(blocked)
    typed_escalation = {
        "schema_version": 1,
        "kind": "same-head-verification-retry",
        "category": "mechanism-failure",
        "operation_id": "parent-operation",
        "verification_operation_id": "verify-operation",
        "exact_head_sha": failed_head,
        "failed_attempt_sha256": attempt_0.sha256,
        "decision": "request-attempt-1",
        "action": "",
        "evidence_note": "",
    }
    (worktree / ".task-verification-contract.json").write_text(
        json.dumps(typed_escalation, sort_keys=True) + "\n", encoding="utf-8"
    )
    task_meta = json.loads(
        (worktree / ".task-meta.json").read_text(encoding="utf-8")
    )
    task_meta["task_name"] = "verification resubmit"
    task_escalation.load_unattended = lambda _worktree: (
        task_meta,
        {"interaction_policy": "unattended"},
    )
    task_escalation.read_surface = lambda *_args: (
        "11111111-1111-1111-1111-111111111111"
    )
    task_escalation.notify = lambda *_args: None
    relayed: list[str] = []
    task_escalation.send = lambda _surface, message, **_kwargs: relayed.append(message)
    task_escalation.emit_lifecycle_event = lambda *_args, **_kwargs: None
    prior_argv = sys.argv
    sys.argv = [
        "task_escalation.py",
        "raise",
        "--worktree",
        str(worktree),
        "--category",
        "mechanism-failure",
        "--verification-mechanism-flake",
        "--reason",
        "Isolated profile passed after the registered transport failed.",
        "--question",
        "Authorize one exact same-HEAD verification retry?",
    ]
    with mock.patch.dict(
        os.environ,
        {"CODEX_THREAD_ID": str(task_meta["origin_session"])},
        clear=True,
    ):
        try:
            task_escalation.main()
        finally:
            sys.argv = prior_argv
    raised = load_latest(worktree)
    if (
        raised is None
        or raised.payload.get("allowed_decisions")
        != ["retry-mechanism-flake", "stop", "repair-repository-mechanism"]
        or not relayed
        or "--decision retry-mechanism-flake" not in relayed[-1]
    ):
        raise AssertionError((raised, relayed))
    escalation_id = str(raised.payload["id"])
    with mock.patch.dict(
        os.environ,
        {"CODEX_THREAD_ID": str(task_meta["origin_session"])},
        clear=True,
    ):
        task_escalation.resolve_escalation(
            worktree, "retry-mechanism-flake"
        )
    resolved = load_latest(worktree)
    if resolved is None:
        raise AssertionError("typed resolution was not published")
    typed_resolution = resolved.payload.get("verification_resolution")
    latest = load_latest(worktree)
    if (
        latest is None
        or latest.payload.get("decision") != "authorize-one-same-head-retry"
        or latest.payload.get("verification_resolution") != typed_resolution
    ):
        raise AssertionError((latest, typed_resolution))
    packet_path.write_text(
        json.dumps(
            {
                **packet,
                "allowed_responses": ["fix-and-resubmit", "escalate"],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    unauthorized_same_head = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--worktree",
            str(worktree),
            "--same-head-mechanism-flake",
            escalation_id,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if (
        unauthorized_same_head.returncode == 0
        or "same-HEAD mechanism-flake retry is not authorized"
        not in unauthorized_same_head.stderr
    ):
        raise AssertionError(unauthorized_same_head)
    packet_path.write_text(
        json.dumps(packet, sort_keys=True) + "\n", encoding="utf-8"
    )
    same_head_ready = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--worktree",
            str(worktree),
            "--same-head-mechanism-flake",
            escalation_id,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if same_head_ready.returncode != 0:
        raise AssertionError(same_head_ready)
    response_path = worktree / ".task-verification-response.json"
    same_head_response = json.loads(response_path.read_text(encoding="utf-8"))
    canonical = json.dumps(
        packet, sort_keys=True, separators=(",", ":")
    ).encode()
    expected_same_head = {
        "schema_version": 2,
        "operation_id": "parent-operation",
        "verification_operation_id": "verify-operation",
        "failed_head_sha": failed_head,
        "packet_sha256": hashlib.sha256(canonical).hexdigest(),
        "response": "retry-mechanism-flake",
        "resubmitted_head_sha": failed_head,
        "failed_attempt_sha256": attempt_0.sha256,
        "next_attempt": attempt_0.same_head_retry().as_dict(),
        "next_attempt_sha256": attempt_0.same_head_retry().sha256,
        "mechanism_flake_decision_id": escalation_id,
        "mechanism_flake_decision_sha256": resolved.sha256,
    }
    if same_head_response != expected_same_head:
        raise AssertionError((same_head_response, expected_same_head))
    repeated_same_head = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--worktree",
            str(worktree),
            "--same-head-mechanism-flake",
            escalation_id,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if (
        repeated_same_head.returncode != 0
        or "already-ready" not in repeated_same_head.stdout
    ):
        raise AssertionError(repeated_same_head)
    product.write_text("after\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.txt"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "after"], cwd=worktree, check=True,
        capture_output=True,
    )
    current_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree, check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    stale = response_path
    stale.write_text("{}\n", encoding="utf-8")
    ready = subprocess.run(
        ["python3", str(SCRIPT), "--worktree", str(worktree)],
        text=True, capture_output=True, check=False,
    )
    if ready.returncode != 0:
        raise AssertionError(ready)
    response = json.loads(stale.read_text(encoding="utf-8"))
    canonical = json.dumps(
        packet, sort_keys=True, separators=(",", ":")
    ).encode()
    expected = {
        "schema_version": 1,
        "operation_id": "parent-operation",
        "verification_operation_id": "verify-operation",
        "failed_head_sha": failed_head,
        "packet_sha256": hashlib.sha256(canonical).hexdigest(),
        "response": "fix-and-resubmit",
        "resubmitted_head_sha": current_head,
    }
    if response != expected:
        raise AssertionError((response, expected))
    repeated = subprocess.run(
        ["python3", str(SCRIPT), "--worktree", str(worktree)],
        text=True, capture_output=True, check=False,
    )
    if repeated.returncode != 0 or "already-ready" not in repeated.stdout:
        raise AssertionError(repeated)

    attempt_1 = VerificationAttempt(
        parent_operation_id="parent-operation",
        profile="scoped",
        profile_sha256="a" * 64,
        exact_head_sha=current_head,
        attempt_index=1,
    )
    exhausted_packet = {
        **packet,
        "head_sha": current_head,
        "verification_operation_id": "verify-operation-attempt-1",
        "verification_attempt": attempt_1.as_dict(),
        "verification_attempt_sha256": attempt_1.sha256,
    }
    packet_path.write_text(
        json.dumps(exhausted_packet, sort_keys=True) + "\n", encoding="utf-8"
    )
    stale.unlink()
    exhausted = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--worktree",
            str(worktree),
            "--same-head-mechanism-flake",
            escalation_id,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if (
        exhausted.returncode == 0
        or "same-HEAD verification retry is exhausted" not in exhausted.stderr
        or stale.exists()
    ):
        raise AssertionError(exhausted)

print(
    "OK   verification resubmission separates changed-HEAD repair from one "
    "authorized same-HEAD mechanism-flake attempt"
)
