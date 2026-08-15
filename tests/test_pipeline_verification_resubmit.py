#!/usr/bin/env python3
"""Regression checks for code-owned verification resubmission.

The canonical public decision is exactly ``retry-mechanism-flake`` from raise
through the durable resolution record and the coordinator wake. Resolving that
decision automatically publishes the one identity-bound same-HEAD response; no
separate manual resubmit command exists for the same-HEAD path.
"""

from __future__ import annotations

import hashlib
import importlib.util
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


def load_resubmit_module():
    spec = importlib.util.spec_from_file_location(
        "pipeline_verification_resubmit", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resubmit = load_resubmit_module()


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
    response_path = worktree / ".task-verification-response.json"
    blocked = subprocess.run(
        ["python3", str(SCRIPT), "--worktree", str(worktree)],
        text=True, capture_output=True, check=False,
    )
    if (
        blocked.returncode == 0
        or "verification repair must commit a new HEAD" not in blocked.stderr
    ):
        raise AssertionError(blocked)
    manual_flag = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--worktree",
            str(worktree),
            "--same-head-mechanism-flake",
            "manual-escalation",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if (
        manual_flag.returncode == 0
        or "unrecognized arguments" not in manual_flag.stderr
    ):
        raise AssertionError(
            ("the manual same-HEAD resubmit command must not exist", manual_flag)
        )
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
    wakes_before = len(relayed)

    # The private alias and near-match decisions fail closed before any record.
    for near_match in ("authorize-one-same-head-retry", "retry-mechanism-flakes"):
        with mock.patch.dict(
            os.environ,
            {"CODEX_THREAD_ID": str(task_meta["origin_session"])},
            clear=True,
        ):
            try:
                task_escalation.resolve_escalation(worktree, near_match)
            except SystemExit:
                pass
            else:
                raise AssertionError(
                    f"near-match decision {near_match!r} was accepted"
                )
        still_pending = load_latest(worktree)
        if (
            still_pending is None
            or still_pending.record_type != "raise"
            or response_path.exists()
            or len(relayed) != wakes_before
        ):
            raise AssertionError((near_match, still_pending, relayed))

    # A foreign session must not resolve the escalation.
    with mock.patch.dict(
        os.environ, {"CODEX_THREAD_ID": "99999999-9999-4999-8999-999999999999"},
        clear=True,
    ):
        try:
            task_escalation.resolve_escalation(worktree, "retry-mechanism-flake")
        except SystemExit:
            pass
        else:
            raise AssertionError("foreign session resolved the escalation")
    if load_latest(worktree).record_type != "raise" or response_path.exists():
        raise AssertionError("foreign session left durable effects")

    # A failed response publication keeps the durable public decision but
    # must not wake the task with a false continuation.
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
    with mock.patch.dict(
        os.environ,
        {"CODEX_THREAD_ID": str(task_meta["origin_session"])},
        clear=True,
    ):
        try:
            task_escalation.resolve_escalation(worktree, "retry-mechanism-flake")
        except SystemExit:
            pass
        else:
            raise AssertionError(
                "an unauthorized packet still published a same-HEAD response"
            )
    resolved = load_latest(worktree)
    if (
        resolved is None
        or resolved.record_type != "resolution"
        or resolved.payload.get("decision") != "retry-mechanism-flake"
        or response_path.exists()
        or len(relayed) != wakes_before
    ):
        raise AssertionError((resolved, relayed))

    # Replaying the exact public decision after repairing the packet
    # publishes the identity-bound response and then wakes the task once.
    packet_path.write_text(
        json.dumps(packet, sort_keys=True) + "\n", encoding="utf-8"
    )
    with mock.patch.dict(
        os.environ,
        {"CODEX_THREAD_ID": str(task_meta["origin_session"])},
        clear=True,
    ):
        task_escalation.resolve_escalation(worktree, "retry-mechanism-flake")
    resolved = load_latest(worktree)
    if resolved is None:
        raise AssertionError("typed resolution was not published")
    typed_resolution = resolved.payload.get("verification_resolution")
    if (
        resolved.payload.get("decision") != "retry-mechanism-flake"
        or not isinstance(typed_resolution, dict)
        or typed_resolution.get("action") != "retry-mechanism-flake"
        or typed_resolution.get("decision") != "authorize-attempt-1"
        or len(relayed) != wakes_before + 1
        or f"escalation {escalation_id}" not in relayed[-1]
        or "retry-mechanism-flake" not in relayed[-1]
    ):
        raise AssertionError((resolved.payload, relayed))
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

    # Resolution replay returns the same already-ready response and never
    # creates a second attempt or a second durable resolution.
    response_bytes = response_path.read_bytes()
    with mock.patch.dict(
        os.environ,
        {"CODEX_THREAD_ID": str(task_meta["origin_session"])},
        clear=True,
    ):
        task_escalation.resolve_escalation(worktree, "retry-mechanism-flake")
    replay_status = resubmit.publish_same_head_response(worktree, escalation_id)
    replayed = load_latest(worktree)
    if (
        response_path.read_bytes() != response_bytes
        or replay_status["status"] != "already-ready"
        or replayed.sha256 != resolved.sha256
    ):
        raise AssertionError((replay_status, replayed))
    try:
        resubmit.publish_same_head_response(worktree, "some-other-escalation")
    except resubmit.ResubmitError as exc:
        if "authorization is invalid" not in str(exc):
            raise AssertionError(exc)
    else:
        raise AssertionError("a foreign escalation id published a response")

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
    try:
        resubmit.publish_same_head_response(worktree, escalation_id)
    except resubmit.ResubmitError as exc:
        if "same-HEAD verification retry is exhausted" not in str(exc):
            raise AssertionError(exc)
    else:
        raise AssertionError("a second same-HEAD retry published a response")
    if stale.exists():
        raise AssertionError("an exhausted retry left a response artifact")
    with mock.patch.dict(
        os.environ,
        {"CODEX_THREAD_ID": str(task_meta["origin_session"])},
        clear=True,
    ):
        try:
            task_escalation.raise_escalation(
                worktree,
                "mechanism-failure",
                "Second flake claim on attempt 1.",
                "Authorize another same-HEAD retry?",
                verification_mechanism_flake=True,
            )
        except SystemExit:
            pass
        else:
            raise AssertionError("an attempt-1 packet produced a typed raise")

    # An invalidated attempt — its probes ran at a HEAD the product has since
    # left — can never be revived through the public same-HEAD path: the
    # exact-HEAD binding fails closed and leaves no response artifact.
    packet_path.write_text(
        json.dumps(packet, sort_keys=True) + "\n", encoding="utf-8"
    )
    try:
        resubmit.publish_same_head_response(worktree, escalation_id)
    except resubmit.ResubmitError as exc:
        if "cannot replace changed-HEAD repair" not in str(exc):
            raise AssertionError(exc)
    else:
        raise AssertionError(
            "a stale-HEAD attempt was revived through the same-HEAD path"
        )
    if response_path.exists():
        raise AssertionError(
            "a refused stale-HEAD authorization left a response artifact"
        )

print(
    "OK   verification resubmission keeps retry-mechanism-flake canonical and "
    "publishes the one authorized same-HEAD response automatically"
)
