"""Exact supported-close compatibility for retained drift-quarantine rounds."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable

from harness.contracts import (
    EffectOutcome,
    OperationRecord,
    OwnedResources,
    to_dict,
)
from harness.store import OperationStore
from harness.supervisor import OperationSupervisor, SupervisorError
from harness.workflows.review_gate import ReviewGateController
from task_review_drift_contract import (
    DriftQuarantineAuthorization,
    SupportedCloseRetirementAuthorization,
)
from task_review_drift_evidence import evidence_root, write_progress
from task_review_shared import TaskReviewError, _atomic_json, _read_json


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _persist_exact_json(
    path: Path, payload: dict[str, object], label: str
) -> None:
    if path.exists():
        if path.is_symlink() or _read_json(path, label) != payload:
            raise TaskReviewError(f"{label} changed")
        return
    _atomic_json(path, payload)


def _root(
    gate: ReviewGateController,
    authorization: DriftQuarantineAuthorization,
    supported_close: SupportedCloseRetirementAuthorization,
) -> Path:
    return (
        evidence_root(gate, authorization)
        / "supported-close"
        / supported_close.authorization_record_id
    )


def bind_evidence(
    *,
    gate: ReviewGateController,
    authorization: DriftQuarantineAuthorization,
    supported_close: SupportedCloseRetirementAuthorization,
    evidence_path: Path,
    current_head: str,
) -> None:
    """Persist exact authorization and zero-effect evidence once."""

    payload = {
        "schema_version": 1,
        "status": "authorized-supported-close-retirement",
        "parent_operation_ids": sorted(
            supported_close.parent_operation_ids
        ),
        "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        "replacement_head_sha": current_head,
        "drift_authorization_record_id": authorization.authorization_record_id,
        "drift_authorization_record_sha256": authorization.authorization_record_sha256,
        "signal_free_authorization_record_id": (
            supported_close.signal_free.authorization_record_id
        ),
        "signal_free_authorization_record_sha256": (
            supported_close.signal_free.authorization_record_sha256
        ),
        "authorization_record_id": supported_close.authorization_record_id,
        "authorization_record_sha256": supported_close.authorization_record_sha256,
        "os_signals_sent": 0,
        "cmux_signals_sent": 0,
        "provider_effects_replayed": 0,
        "callback_effects_replayed": 0,
    }
    _persist_exact_json(
        _root(gate, authorization, supported_close) / "evidence.json",
        payload,
        "supported-close retirement evidence",
    )


def validate_pair(
    parent: OperationRecord,
    child: OperationRecord,
    row: dict[str, object],
) -> None:
    """Reject anything except one exact supported-close parent/round pair."""

    if (
        parent.spec.operation_id != row["parent_operation_id"]
        or parent.run_id != row["parent_run_id"]
        or parent.spec.route.profile != "reviewer-callback"
        or parent.state != "cancelled"
        or parent.resources != OwnedResources()
        or parent.pending_effect
        or parent.effect_id != "request-exit"
        or parent.effect_outcome != EffectOutcome.SUCCEEDED
        or parent.attention_reason is not None
        or parent.resume_state
        or parent.accepted_callback_id
        or parent.accepted_callback_kind
        or parent.accepted_callback_sha256
    ):
        raise TaskReviewError("supported-close parent receipt changed")
    if (
        child.spec.operation_id != row["round_operation_id"]
        or child.spec.parent_operation_id != parent.spec.operation_id
        or child.run_id != row["round_run_id"]
        or child.lane_id != parent.lane_id
        or child.spec.kind != "review-round"
        or child.spec.route.profile != "reviewer-callback"
        or child.state not in {"verifying", "complete"}
        or child.resources != OwnedResources()
        or child.pending_effect
        or child.effect_id
        or child.effect_outcome != EffectOutcome.NONE
        or child.attention_reason is not None
        or child.resume_state
        or child.accepted_callback_kind != "review"
        or child.accepted_callback_id != row["accepted_callback_id"]
        or child.accepted_callback_sha256
        != row["accepted_callback_sha256"]
    ):
        raise TaskReviewError("supported-close round identity changed")


def validate_pairs(
    *,
    evidence: dict[str, object],
    store: OperationStore,
    task_id: str,
    supported_close: SupportedCloseRetirementAuthorization,
) -> None:
    """Validate both authorized pairs before the first durable mutation."""

    rows = evidence.get("lanes")
    if not isinstance(rows, list):
        raise TaskReviewError("drift quarantine evidence is invalid")
    evidence_parents = {
        str(row.get("parent_operation_id") or "")
        for row in rows
        if isinstance(row, dict)
    }
    if evidence_parents != set(supported_close.parent_operation_ids):
        raise TaskReviewError(
            "supported-close authorization parent identity changed"
        )
    for row in rows:
        if not isinstance(row, dict):
            raise TaskReviewError("drift quarantine evidence is invalid")
        parent = store.read(task_id, str(row["parent_operation_id"]))
        child = store.read(task_id, str(row["round_operation_id"]))
        validate_pair(parent, child, row)


def _receipt_path(
    gate: ReviewGateController,
    authorization: DriftQuarantineAuthorization,
    supported_close: SupportedCloseRetirementAuthorization,
    operation_id: str,
) -> Path:
    return (
        _root(gate, authorization, supported_close)
        / "parents"
        / f"{operation_id}.json"
    )


def _payload(
    *,
    parent: OperationRecord,
    child: OperationRecord,
    row: dict[str, object],
    evidence_path: Path,
    current_head: str,
    authorization: DriftQuarantineAuthorization,
    supported_close: SupportedCloseRetirementAuthorization,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "supported-close-consumed",
        "axis": row["axis"],
        "parent_operation_id": parent.spec.operation_id,
        "parent_run_id": parent.run_id,
        "parent_lane_id": parent.lane_id,
        "parent_operation_sha256": _canonical_sha256(to_dict(parent)),
        "parent_state": parent.state,
        "parent_effect_id": parent.effect_id,
        "parent_effect_outcome": parent.effect_outcome.value,
        "parent_resources": to_dict(parent.resources),
        "round_operation_id": child.spec.operation_id,
        "round_run_id": child.run_id,
        "original_round_sha256": _canonical_sha256(to_dict(child)),
        "accepted_callback_id": row["accepted_callback_id"],
        "accepted_callback_sha256": row["accepted_callback_sha256"],
        "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        "replacement_head_sha": current_head,
        "drift_authorization_record_id": authorization.authorization_record_id,
        "drift_authorization_record_sha256": authorization.authorization_record_sha256,
        "signal_free_authorization_record_id": (
            supported_close.signal_free.authorization_record_id
        ),
        "signal_free_authorization_record_sha256": (
            supported_close.signal_free.authorization_record_sha256
        ),
        "authorization_record_id": supported_close.authorization_record_id,
        "authorization_record_sha256": supported_close.authorization_record_sha256,
        "os_signals_sent": 0,
        "cmux_signals_sent": 0,
        "provider_effects_replayed": 0,
        "callback_effects_replayed": 0,
    }


def validate_receipt(
    *,
    path: Path,
    digest: str,
    parent: OperationRecord,
    child: OperationRecord,
    row: dict[str, object],
    evidence_path: Path,
    current_head: str,
    authorization: DriftQuarantineAuthorization,
    supported_close: SupportedCloseRetirementAuthorization,
) -> None:
    """Validate an immutable receipt after a crash or restart."""

    if path.is_symlink() or not path.is_file():
        raise TaskReviewError("supported-close retirement receipt is unavailable")
    raw = path.read_bytes()
    receipt = _read_json(path, "supported-close retirement receipt")
    required_zero = (
        "os_signals_sent",
        "cmux_signals_sent",
        "provider_effects_replayed",
        "callback_effects_replayed",
    )
    if (
        hashlib.sha256(raw).hexdigest() != digest
        or receipt.get("schema_version") != 1
        or receipt.get("status") != "supported-close-consumed"
        or receipt.get("axis") != row["axis"]
        or receipt.get("parent_operation_id") != parent.spec.operation_id
        or receipt.get("parent_run_id") != parent.run_id
        or receipt.get("parent_lane_id") != parent.lane_id
        or receipt.get("parent_operation_sha256")
        != _canonical_sha256(to_dict(parent))
        or receipt.get("parent_state") != "cancelled"
        or receipt.get("parent_effect_id") != "request-exit"
        or receipt.get("parent_effect_outcome") != "succeeded"
        or receipt.get("parent_resources") != to_dict(OwnedResources())
        or receipt.get("round_operation_id") != child.spec.operation_id
        or receipt.get("round_run_id") != child.run_id
        or re.fullmatch(
            r"[0-9a-f]{64}",
            str(receipt.get("original_round_sha256") or ""),
        )
        is None
        or receipt.get("accepted_callback_id")
        != row["accepted_callback_id"]
        or receipt.get("accepted_callback_sha256")
        != row["accepted_callback_sha256"]
        or receipt.get("evidence_sha256")
        != hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        or receipt.get("replacement_head_sha") != current_head
        or receipt.get("drift_authorization_record_id")
        != authorization.authorization_record_id
        or receipt.get("drift_authorization_record_sha256")
        != authorization.authorization_record_sha256
        or receipt.get("signal_free_authorization_record_id")
        != supported_close.signal_free.authorization_record_id
        or receipt.get("signal_free_authorization_record_sha256")
        != supported_close.signal_free.authorization_record_sha256
        or receipt.get("authorization_record_id")
        != supported_close.authorization_record_id
        or receipt.get("authorization_record_sha256")
        != supported_close.authorization_record_sha256
        or any(receipt.get(field) != 0 for field in required_zero)
    ):
        raise TaskReviewError("supported-close retirement receipt changed")


def consume_parent(
    *,
    row: dict[str, object],
    parent: OperationRecord,
    gate: ReviewGateController,
    store: OperationStore,
    task_id: str,
    authorization: DriftQuarantineAuthorization,
    supported_close: SupportedCloseRetirementAuthorization,
    evidence_path: Path,
    current_head: str,
    cleaned_parents: list[str],
    terminal_rounds: list[str],
    retirement_receipts: dict[str, str],
    fault_observer: Callable[[str], None] | None,
) -> OperationRecord:
    """Receipt and atomically complete one corresponding retained round."""

    operation_id = str(row["parent_operation_id"])
    child = store.read(task_id, str(row["round_operation_id"]))
    validate_pair(parent, child, row)
    receipt_path = _receipt_path(
        gate, authorization, supported_close, operation_id
    )
    if child.state == "complete":
        validate_receipt(
            path=receipt_path,
            digest=retirement_receipts.get(operation_id, ""),
            parent=parent,
            child=child,
            row=row,
            evidence_path=evidence_path,
            current_head=current_head,
            authorization=authorization,
            supported_close=supported_close,
        )
        return parent
    payload = _payload(
        parent=parent,
        child=child,
        row=row,
        evidence_path=evidence_path,
        current_head=current_head,
        authorization=authorization,
        supported_close=supported_close,
    )
    _persist_exact_json(
        receipt_path, payload, "supported-close retirement receipt"
    )
    receipt_digest = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    prior_digest = retirement_receipts.get(operation_id)
    if prior_digest not in {None, receipt_digest}:
        raise TaskReviewError("supported-close retirement receipt changed")
    retirement_receipts[operation_id] = receipt_digest
    write_progress(
        gate,
        authorization,
        evidence_path=evidence_path,
        status="cleaning",
        cleaned_parents=cleaned_parents,
        terminal_rounds=terminal_rounds,
        retirement_receipts=retirement_receipts,
    )
    if fault_observer is not None:
        fault_observer(f"supported-close-receipt:{row['axis']}")
    try:
        OperationSupervisor(
            store, task_id, child.spec.operation_id
        ).complete_round_after_supported_close(
            parent,
            parent_run_id=str(row["parent_run_id"]),
            round_run_id=str(row["round_run_id"]),
            accepted_callback_id=str(row["accepted_callback_id"]),
            accepted_callback_sha256=str(row["accepted_callback_sha256"]),
        )
    except SupervisorError as exc:
        raise TaskReviewError(str(exc)) from exc
    if fault_observer is not None:
        fault_observer(f"supported-close-round-completed:{row['axis']}")
    return parent


def receipt_path(
    gate: ReviewGateController,
    authorization: DriftQuarantineAuthorization,
    supported_close: SupportedCloseRetirementAuthorization,
    operation_id: str,
) -> Path:
    """Expose the exact receipt path to restart validation."""

    return _receipt_path(gate, authorization, supported_close, operation_id)
