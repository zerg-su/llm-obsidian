"""Validate one durable failed-verification resubmission transport."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Mapping

from harness.store import OperationStore, StoreError
from harness.verification import (
    VerificationAuthority,
    VerificationAuthorityError,
)
from task_review_request import _canonical_sha256
from task_review_shared import TaskReviewError, _read_json


def _response_receipt_path(
    receipt_path: Path,
    response_receipt: Mapping[str, object],
    packet_sha256: str,
) -> Path:
    """Preserve a valid prior response while selecting this response's path."""

    fixed_path = receipt_path.with_name("response-receipt.json")
    if not fixed_path.exists():
        return fixed_path
    if not fixed_path.is_file() or fixed_path.is_symlink():
        raise TaskReviewError(
            "finalizing review recovery response receipt changed"
        )
    prior = _read_json(fixed_path, "verification response receipt")
    if prior == response_receipt:
        return fixed_path
    receipt_fields = {
        "schema_version",
        "operation_id",
        "verification_operation_id",
        "failed_head_sha",
        "resubmitted_head_sha",
        "response_sha256",
        "status",
    }
    prior_head = str(prior.get("resubmitted_head_sha") or "")
    prior_sha256 = str(prior.get("response_sha256") or "")
    if (
        set(prior) != receipt_fields
        or prior.get("schema_version") != 1
        or prior.get("operation_id") != response_receipt.get("operation_id")
        or prior.get("verification_operation_id")
        != response_receipt.get("verification_operation_id")
        or prior.get("failed_head_sha")
        != response_receipt.get("failed_head_sha")
        or prior.get("status") != "accepted"
        or not re.fullmatch(r"[0-9a-f]{40}", prior_head)
        or not re.fullmatch(r"[0-9a-f]{64}", prior_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", packet_sha256)
    ):
        raise TaskReviewError(
            "finalizing review recovery response receipt changed"
        )
    prior_response = {
        "schema_version": 1,
        "operation_id": prior["operation_id"],
        "verification_operation_id": prior["verification_operation_id"],
        "failed_head_sha": prior["failed_head_sha"],
        "packet_sha256": packet_sha256,
        "response": "fix-and-resubmit",
        "resubmitted_head_sha": prior_head,
    }
    if _canonical_sha256(prior_response) != prior_sha256:
        raise TaskReviewError(
            "finalizing review recovery response receipt changed"
        )
    response_sha256 = str(response_receipt.get("response_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", response_sha256):
        raise TaskReviewError(
            "finalizing review recovery response receipt changed"
        )
    content_path = receipt_path.with_name(
        f"response-receipt-{response_sha256}.json"
    )
    if content_path.exists() and (
        not content_path.is_file()
        or content_path.is_symlink()
        or _read_json(content_path, "verification response receipt")
        != response_receipt
    ):
        raise TaskReviewError(
            "finalizing review recovery response receipt changed"
        )
    return content_path


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
    policy = meta.get("pipeline_policy")
    review_policy = meta.get("review_policy")
    if (
        not isinstance(policy, Mapping)
        or not isinstance(review_policy, Mapping)
    ):
        raise TaskReviewError(
            "finalizing review recovery verification receipt is invalid"
        )
    try:
        parent = store.read(task_id, task_id)
        authority = VerificationAuthority.load(
            receipt_path,
            store=store,
            parent=parent,
            runtime_root=owner_runtime,
            expected_definition_sha256=str(
                policy.get("definition_sha256") or ""
            ),
            expected_profile=str(
                review_policy.get("verification_profile") or ""
            ),
            expected_profile_sha256=str(
                review_policy.get("verification_profile_sha256") or ""
            ),
            expected_head_sha=previous_head,
            allowed_statuses=("failed",),
            child_states=("attention-required", "failed"),
            require_released=True,
            require_effect_succeeded=True,
        )
    except (StoreError, VerificationAuthorityError) as exc:
        raise TaskReviewError(
            "finalizing review recovery verification authority is invalid"
        ) from exc
    if authority.operation_id != operation_id:
        raise TaskReviewError(
            "finalizing review recovery verification authority changed"
        )
    packet_evidence = [
        {
            "command_id": item.command_id,
            "exit_code": item.exit_code,
            "output_pointer": str(
                (owner_runtime / item.output_pointer).resolve()
            ),
        }
        for item in authority.evidence
    ]
    expected_packet = {
        "schema_version": VerificationAuthority.SCHEMA_VERSION,
        "operation_id": task_id,
        "verification_operation_id": authority.operation_id,
        "verification_lane_id": authority.lane_id,
        "verification_run_id": authority.run_id,
        "definition_sha256": authority.definition_sha256,
        "step_id": "verify",
        "head_sha": authority.head_sha,
        "status": "attention-required",
        "reason": "verification-failed",
        "safe_boundary": "tdd-slices-complete",
        "allowed_responses": [
            "fix-and-resubmit",
            *(
                ["retry-mechanism-flake"]
                if authority.attempt.attempt_index == 0
                else []
            ),
            "escalate",
        ],
        "response_pointer": ".task-verification-response.json",
        "receipt_pointer": str(receipt_path),
        "evidence": packet_evidence,
        "verification_attempt": authority.attempt.as_dict(),
        "verification_attempt_sha256": authority.attempt.sha256,
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
    response_receipt_path = _response_receipt_path(
        receipt_path, response_receipt, packet_sha256
    )
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
