"""Validate one durable failed-verification resubmission transport."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Mapping

from harness.contracts import EffectOutcome, OwnedResources
from harness.runtime_worker import _pipeline_verify_identity
from harness.store import OperationStore, StoreError
from task_review_request import _canonical_sha256
from task_review_shared import TaskReviewError, _read_json


def _durable_verification_resubmit(
    meta: Mapping[str, Any],
    worktree: Path,
    store: OperationStore,
    task_id: str,
    previous_head: str,
    current_head: str,
) -> tuple[Path, dict[str, object], str]:
    """Bind one worktree resubmit to its exact durable failed verification."""

    packet_path = worktree / ".task-verification.json"
    response_path = worktree / ".task-verification-response.json"
    for path, label in (
        (packet_path, "verification attention packet"),
        (response_path, "verification resubmission response"),
    ):
        if not path.is_file() or path.is_symlink():
            raise TaskReviewError(
                f"finalizing review recovery {label} is unavailable"
            )
    packet = _read_json(packet_path, "verification attention packet")
    response = _read_json(
        response_path, "verification resubmission response"
    )
    operation_id = str(packet.get("verification_operation_id") or "")
    owner_runtime = (
        store.root
        / "owners"
        / task_id
        / "runtime"
        / task_id
    ).resolve()
    receipt_path = (
        owner_runtime
        / "pipeline-verification"
        / operation_id
        / "receipt.json"
    ).resolve()
    if (
        not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", operation_id
        )
        or not receipt_path.is_file()
        or receipt_path.is_symlink()
    ):
        raise TaskReviewError(
            "finalizing review recovery verification receipt is unavailable"
        )
    receipt = _read_json(receipt_path, "verification receipt")
    receipt_fields = {
        "schema_version",
        "operation_id",
        "parent_operation_id",
        "lane_id",
        "run_id",
        "definition_sha256",
        "step_id",
        "head_sha",
        "input_sha256",
        "profile",
        "profile_sha256",
        "effect_id",
        "status",
        "evidence",
    }
    policy = meta.get("pipeline_policy")
    review_policy = meta.get("review_policy")
    evidence = receipt.get("evidence")
    input_sha256 = str(receipt.get("input_sha256") or "")
    profile = str(receipt.get("profile") or "")
    if (
        set(receipt) != receipt_fields
        or receipt.get("schema_version") != 1
        or receipt.get("operation_id") != operation_id
        or receipt.get("parent_operation_id") != task_id
        or not isinstance(policy, Mapping)
        or receipt.get("definition_sha256")
        != policy.get("definition_sha256")
        or receipt.get("step_id") != "verify"
        or receipt.get("head_sha") != previous_head
        or not re.fullmatch(r"[0-9a-f]{64}", input_sha256)
        or not isinstance(review_policy, Mapping)
        or profile != review_policy.get("verification_profile")
        or receipt.get("profile_sha256")
        != review_policy.get("verification_profile_sha256")
        or receipt.get("effect_id")
        != f"pipeline-verify-{input_sha256[:32]}"
        or receipt.get("status") != "failed"
        or not isinstance(evidence, list)
        or not evidence
        or len(evidence) > 100
    ):
        raise TaskReviewError(
            "finalizing review recovery verification receipt is invalid"
        )
    try:
        parent = store.read(task_id, task_id)
        expected_spec, expected_lane, expected_run = (
            _pipeline_verify_identity(
                parent.spec,
                definition_sha256=str(receipt["definition_sha256"]),
                input_sha256=input_sha256,
                profile=profile,
            )
        )
        child = store.read(task_id, operation_id)
    except (StoreError, ValueError) as exc:
        raise TaskReviewError(
            "finalizing review recovery verification operation is unavailable"
        ) from exc
    if (
        expected_spec.operation_id != operation_id
        or receipt.get("lane_id") != expected_lane
        or receipt.get("run_id") != expected_run
        or child.spec != expected_spec
        or child.lane_id != expected_lane
        or child.run_id != expected_run
        or child.state not in {"attention-required", "failed"}
        or child.resources != OwnedResources()
        or child.pending_effect
        or child.effect_id != receipt.get("effect_id")
        or child.effect_outcome != EffectOutcome.SUCCEEDED
    ):
        raise TaskReviewError(
            "finalizing review recovery verification operation changed"
        )
    packet_evidence: list[dict[str, object]] = []
    for row in evidence:
        if (
            not isinstance(row, dict)
            or set(row)
            != {
                "command_id",
                "cwd",
                "exit_code",
                "started_at",
                "finished_at",
                "head_sha",
                "profile",
                "profile_sha256",
                "output_pointer",
            }
            or not isinstance(row.get("command_id"), str)
            or not row["command_id"]
            or type(row.get("exit_code")) is not int
            or row.get("head_sha") != previous_head
            or row.get("profile") != profile
            or row.get("profile_sha256")
            != receipt.get("profile_sha256")
        ):
            raise TaskReviewError(
                "finalizing review recovery verification evidence is invalid"
            )
        pointer = Path(str(row.get("output_pointer") or ""))
        output = (owner_runtime / pointer).resolve()
        evidence_root = (owner_runtime / "pipeline-verification").resolve()
        if (
            pointer.is_absolute()
            or evidence_root not in output.parents
            or not output.is_file()
            or output.is_symlink()
        ):
            raise TaskReviewError(
                "finalizing review recovery verification evidence is unavailable"
            )
        packet_evidence.append(
            {
                "command_id": row["command_id"],
                "exit_code": row["exit_code"],
                "output_pointer": str(output),
            }
        )
    expected_packet = {
        "schema_version": 1,
        "operation_id": task_id,
        "verification_operation_id": operation_id,
        "verification_lane_id": expected_lane,
        "verification_run_id": expected_run,
        "definition_sha256": receipt["definition_sha256"],
        "step_id": "verify",
        "head_sha": previous_head,
        "status": "attention-required",
        "reason": "verification-failed",
        "safe_boundary": "tdd-slices-complete",
        "allowed_responses": ["fix-and-resubmit", "escalate"],
        "response_pointer": ".task-verification-response.json",
        "receipt_pointer": str(receipt_path),
        "evidence": packet_evidence,
    }
    packet_sha256 = _canonical_sha256(expected_packet)
    expected_response = {
        "schema_version": 1,
        "operation_id": task_id,
        "verification_operation_id": operation_id,
        "failed_head_sha": previous_head,
        "packet_sha256": packet_sha256,
        "response": "fix-and-resubmit",
        "resubmitted_head_sha": current_head,
    }
    if packet != expected_packet or response != expected_response:
        raise TaskReviewError(
            "finalizing review recovery verification transport changed"
        )
    response_receipt = {
        "schema_version": 1,
        "operation_id": task_id,
        "verification_operation_id": operation_id,
        "failed_head_sha": previous_head,
        "resubmitted_head_sha": current_head,
        "response_sha256": _canonical_sha256(expected_response),
        "status": "accepted",
    }
    response_receipt_path = receipt_path.with_name("response-receipt.json")
    if response_receipt_path.exists() and (
        not response_receipt_path.is_file()
        or response_receipt_path.is_symlink()
        or _read_json(
            response_receipt_path, "verification response receipt"
        )
        != response_receipt
    ):
        raise TaskReviewError(
            "finalizing review recovery response receipt changed"
        )
    return response_receipt_path, response_receipt, hashlib.sha256(
        receipt_path.read_bytes()
    ).hexdigest()
