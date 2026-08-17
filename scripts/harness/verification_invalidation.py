"""Exact read-only classification of superseded verification attempts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .contracts import OperationRecord
from .store import OperationStore
from .verification import VerificationAuthority, VerificationAuthorityError
from .verification_attempt import (
    VerificationAttempt,
    pipeline_verify_effect_id,
    pipeline_verify_identity,
    verification_input_sha256,
)
FIELDS = {
    "schema_version",
    "operation_id",
    "parent_operation_id",
    "profile_sha256",
    "predecessor_attempt_sha256",
    "predecessor_effect_id",
    "successor_operation_id",
    "successor_attempt_sha256",
    "successor_effect_id",
    "current_head_sha",
    "status",
}


def superseded_verification_ids(
    store: OperationStore,
    parent: OperationRecord,
    records: Iterable[OperationRecord],
) -> frozenset[str]:
    """Return predecessors proven obsolete by one exact completed successor."""

    by_id = {record.spec.operation_id: record for record in records}
    runtime = (
        store.root
        / "owners"
        / parent.spec.owner_id
        / "runtime"
        / parent.spec.operation_id
        / "pipeline-verification"
    )
    superseded: set[str] = set()
    for predecessor in by_id.values():
        if (
            predecessor.spec.kind != "pipeline-verify"
            or predecessor.spec.parent_operation_id != parent.spec.operation_id
            or predecessor.state != "attention-required"
        ):
            continue
        path = runtime / predecessor.spec.operation_id / "invalidation.json"
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 20_000:
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        successor = by_id.get(str(value.get("successor_operation_id") or "")) if isinstance(value, dict) else None
        predecessor_id = predecessor.spec.operation_id
        try:
            profile_sha256 = str(value.get("profile_sha256") or "")
            head_sha = str(value.get("current_head_sha") or "")
            attempt0 = VerificationAttempt(
                parent.spec.operation_id,
                predecessor.spec.verification_profile,
                profile_sha256,
                head_sha,
                0,
            )
            attempt1 = attempt0.same_head_retry()
            input_sha256 = verification_input_sha256(
                predecessor.spec.contract_sha256,
                head_sha,
                profile_sha256,
                1,
            )
            expected0 = pipeline_verify_identity(
                parent.spec,
                definition_sha256=predecessor.spec.contract_sha256,
                input_sha256=input_sha256,
                profile=predecessor.spec.verification_profile,
                attempt_index=0,
            )
            expected1 = pipeline_verify_identity(
                parent.spec,
                definition_sha256=predecessor.spec.contract_sha256,
                input_sha256=input_sha256,
                profile=predecessor.spec.verification_profile,
                attempt_index=1,
            )
        except (AttributeError, TypeError, ValueError):
            continue
        if (
            not isinstance(value, dict)
            or set(value) != FIELDS
            or value.get("schema_version") != 1
            or value.get("status") != "invalidated"
            or value.get("operation_id") != predecessor_id
            or value.get("parent_operation_id") != parent.spec.operation_id
            or predecessor.spec != expected0[0]
            or predecessor.lane_id != expected0[1]
            or predecessor.run_id != expected0[2]
            or successor is None
            or successor.state != "complete"
            or successor.spec.kind != "pipeline-verify"
            or successor.spec.parent_operation_id != parent.spec.operation_id
            or successor.spec.owner_id != predecessor.spec.owner_id
            or successor.spec.contract_sha256 != predecessor.spec.contract_sha256
            or successor.spec.verification_profile != predecessor.spec.verification_profile
            or successor.spec != expected1[0]
            or successor.lane_id != expected1[1]
            or successor.run_id != expected1[2]
            or value.get("predecessor_attempt_sha256") != attempt0.sha256
            or value.get("successor_attempt_sha256") != attempt1.sha256
            or value.get("predecessor_effect_id")
            != pipeline_verify_effect_id(input_sha256, 0)
            or value.get("successor_effect_id")
            != pipeline_verify_effect_id(input_sha256, 1)
        ):
            continue
        receipt_path = runtime / successor.spec.operation_id / "receipt.json"
        try:
            authority = VerificationAuthority.load(
                receipt_path,
                store=store,
                parent=parent,
                runtime_root=runtime.parent,
                expected_definition_sha256=predecessor.spec.contract_sha256,
                expected_profile=predecessor.spec.verification_profile,
                expected_profile_sha256=profile_sha256,
                expected_head_sha=head_sha,
                allowed_statuses=("complete",),
                child_states=("complete",),
                require_released=True,
                require_effect_succeeded=True,
            )
        except VerificationAuthorityError:
            continue
        if (
            authority.attempt != attempt1
            or authority.effect_id != value["successor_effect_id"]
            or authority.command_ids
            != tuple(
                f"{authority.profile}-{index + 1}"
                for index in range(len(authority.command_ids))
            )
        ):
            continue
        superseded.add(predecessor_id)
    return frozenset(superseded)
