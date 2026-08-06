"""Immutable raw-byte evidence for stale review callback identity drift."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from harness.contracts import OwnedResources
from harness.state_machine import TERMINAL
from harness.store import OperationStore
from harness.workflows.review_gate import ReviewGateController, ReviewGateRun
from task_review_context import _callback_path
from task_review_drift_contract import DriftQuarantineAuthorization
from task_review_shared import TaskReviewError, _atomic_bytes, _atomic_json, _read_json


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
GIT_OID = re.compile(r"[0-9a-f]{40,64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
EVIDENCE_FIELDS = {
    "schema_version",
    "status",
    "operation_id",
    "review_operation_id",
    "reviewed_head_sha",
    "replacement_head_sha",
    "authorization_record_id",
    "authorization_record_sha256",
    "anchor_record_id",
    "anchor_record_sha256",
    "callback_effects_replayed",
    "provider_effects_replayed",
    "lanes",
}
LANE_FIELDS = {
    "axis",
    "parent_operation_id",
    "parent_run_id",
    "round_operation_id",
    "round_run_id",
    "accepted_callback_id",
    "accepted_callback_sha256",
    "callback_artifact_id",
    "callback_artifact_sha256",
    "identity_relation",
    "accepted_receipt_pointer",
    "accepted_receipt_file_sha256",
    "callback_artifact_pointer",
    "callback_artifact_file_sha256",
}
PROGRESS_FIELDS_V1 = {
    "schema_version",
    "status",
    "evidence_sha256",
    "cleaned_parents",
    "terminal_rounds",
}
PROGRESS_FIELDS_V2 = PROGRESS_FIELDS_V1 | {"retirement_receipts"}
PROGRESS_STATUSES = {
    "prepared",
    "cleaning",
    "terminalizing-rounds",
    "quarantined",
    "fresh-review-started",
}


def _bounded_json_bytes(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise TaskReviewError(f"{label} is unavailable")
    raw = path.read_bytes()
    if not raw or len(raw) > 65_536:
        raise TaskReviewError(f"{label} must be non-empty and bounded")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskReviewError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise TaskReviewError(f"{label} must be an object")
    return value, raw


def _archive_exact(path: Path, raw: bytes, label: str) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
            raise TaskReviewError(f"{label} archive changed")
        return
    _atomic_bytes(path, raw)


def _callback_identity(
    value: dict[str, Any], *, operation_id: str, run_id: str
) -> tuple[str, str]:
    if set(value) != {
        "schema_version",
        "callback_id",
        "kind",
        "operation_id",
        "run_id",
        "payload_sha256",
        "payload",
    }:
        raise TaskReviewError("retained review callback envelope is invalid")
    callback_id = str(value.get("callback_id") or "")
    payload_sha256 = str(value.get("payload_sha256") or "")
    payload = value.get("payload")
    if (
        value.get("schema_version") != 1
        or value.get("kind") != "review"
        or value.get("operation_id") != operation_id
        or value.get("run_id") != run_id
        or IDENTIFIER.fullmatch(callback_id) is None
        or SHA256.fullmatch(payload_sha256) is None
        or not isinstance(payload, dict)
    ):
        raise TaskReviewError("retained review callback identity is invalid")
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()
    if (
        hashlib.sha256(canonical).hexdigest() != payload_sha256
        or callback_id != f"review-{payload_sha256[:24]}"
    ):
        raise TaskReviewError("retained review callback digest is invalid")
    return callback_id, payload_sha256


def _receipt_identity(
    value: dict[str, Any], *, operation_id: str, run_id: str
) -> tuple[str, str]:
    callback_id = str(value.get("callback_id") or "")
    payload_sha256 = str(value.get("payload_sha256") or "")
    if (
        set(value)
        != {
            "schema_version",
            "status",
            "operation_id",
            "run_id",
            "callback_id",
            "payload_sha256",
            "generation",
        }
        or value.get("schema_version") != 1
        or value.get("status") != "accepted"
        or value.get("operation_id") != operation_id
        or value.get("run_id") != run_id
        or IDENTIFIER.fullmatch(callback_id) is None
        or SHA256.fullmatch(payload_sha256) is None
        or type(value.get("generation")) is not int
        or int(value["generation"]) < 1
    ):
        raise TaskReviewError("accepted callback receipt identity is invalid")
    return callback_id, payload_sha256


def evidence_root(
    gate: ReviewGateController, authorization: DriftQuarantineAuthorization
) -> Path:
    return gate.root / "drift-quarantine" / authorization.authorization_record_id


def _archived_path(gate: ReviewGateController, pointer: object) -> Path:
    relative = Path(str(pointer or ""))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise TaskReviewError("drift quarantine archive escapes gate state")
    candidate = gate.root / relative
    current = gate.root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise TaskReviewError("drift quarantine archive changed")
    try:
        candidate.resolve().relative_to(gate.root)
    except ValueError as exc:
        raise TaskReviewError("drift quarantine archive escapes gate state") from exc
    return candidate


def _validate_archived_lane(
    gate: ReviewGateController, row: dict[str, Any]
) -> None:
    if (
        set(row) != LANE_FIELDS
        or row.get("axis") not in {"openai-intent", "openai-engineering"}
        or any(
            IDENTIFIER.fullmatch(str(row.get(key) or "")) is None
            for key in (
                "parent_operation_id",
                "parent_run_id",
                "round_operation_id",
                "round_run_id",
                "accepted_callback_id",
                "callback_artifact_id",
            )
        )
        or any(
            SHA256.fullmatch(str(row.get(key) or "")) is None
            for key in (
                "accepted_callback_sha256",
                "callback_artifact_sha256",
                "accepted_receipt_file_sha256",
                "callback_artifact_file_sha256",
            )
        )
    ):
        raise TaskReviewError("drift quarantine evidence identity changed")
    receipt_path = _archived_path(gate, row["accepted_receipt_pointer"])
    callback_path = _archived_path(gate, row["callback_artifact_pointer"])
    receipt, receipt_raw = _bounded_json_bytes(
        receipt_path, "archived accepted callback receipt"
    )
    callback, callback_raw = _bounded_json_bytes(
        callback_path, "archived retained review callback"
    )
    receipt_identity = _receipt_identity(
        receipt,
        operation_id=str(row["round_operation_id"]),
        run_id=str(row["round_run_id"]),
    )
    callback_identity = _callback_identity(
        callback,
        operation_id=str(row["round_operation_id"]),
        run_id=str(row["round_run_id"]),
    )
    if (
        receipt_identity
        != (row["accepted_callback_id"], row["accepted_callback_sha256"])
        or callback_identity
        != (row["callback_artifact_id"], row["callback_artifact_sha256"])
        or hashlib.sha256(receipt_raw).hexdigest()
        != row["accepted_receipt_file_sha256"]
        or hashlib.sha256(callback_raw).hexdigest()
        != row["callback_artifact_file_sha256"]
    ):
        raise TaskReviewError("drift quarantine evidence identity changed")


def validate_archived_evidence(
    gate: ReviewGateController,
    authorization: DriftQuarantineAuthorization,
) -> dict[str, Any] | None:
    root = evidence_root(gate, authorization)
    evidence_path = root / "evidence.json"
    if not evidence_path.exists():
        return None
    evidence = _read_json(evidence_path, "drift quarantine evidence")
    if (
        set(evidence) != EVIDENCE_FIELDS
        or evidence.get("schema_version") != 1
        or evidence.get("status") != "quarantined-evidence"
        or IDENTIFIER.fullmatch(str(evidence.get("operation_id") or "")) is None
        or IDENTIFIER.fullmatch(str(evidence.get("review_operation_id") or ""))
        is None
        or GIT_OID.fullmatch(str(evidence.get("reviewed_head_sha") or "")) is None
        or GIT_OID.fullmatch(str(evidence.get("replacement_head_sha") or ""))
        is None
        or evidence.get("authorization_record_id")
        != authorization.authorization_record_id
        or evidence.get("authorization_record_sha256")
        != authorization.authorization_record_sha256
        or evidence.get("anchor_record_id") != authorization.anchor_record_id
        or evidence.get("anchor_record_sha256")
        != authorization.anchor_record_sha256
        or evidence.get("callback_effects_replayed") != 0
        or evidence.get("provider_effects_replayed") != 0
        or not isinstance(evidence.get("lanes"), list)
        or len(evidence["lanes"]) != 2
    ):
        raise TaskReviewError("drift quarantine evidence identity changed")
    axes: list[str] = []
    for row in evidence["lanes"]:
        if not isinstance(row, dict):
            raise TaskReviewError("drift quarantine evidence is invalid")
        _validate_archived_lane(gate, row)
        axes.append(str(row["axis"]))
    if sorted(axes) != ["openai-engineering", "openai-intent"]:
        raise TaskReviewError(
            "drift quarantine requires one exact callback identity drift"
        )
    _validate_relations(evidence["lanes"], authorization)
    return evidence


def _lane_evidence(
    *,
    authorization: DriftQuarantineAuthorization,
    gate: ReviewGateController,
    run: ReviewGateRun,
    store: OperationStore,
    runtime_root: Path,
    task_id: str,
) -> tuple[
    list[dict[str, object]],
    set[str],
    list[tuple[Path, bytes, str]],
]:
    expected_operations: set[str] = set()
    rows: list[dict[str, object]] = []
    archives: list[tuple[Path, bytes, str]] = []
    root = evidence_root(gate, authorization)
    for lane in run.execution.lanes:
        round_ = run.rounds[lane.axis]
        parent = store.read(task_id, lane.operation_id)
        child = store.read(task_id, round_.operation_id)
        expected_operations.update((lane.operation_id, round_.operation_id))
        if (
            parent.spec != lane.spec
            or parent.lane_id != lane.lane_id
            or parent.run_id != lane.run_id
            or parent.state != "awaiting-callback"
            or parent.pending_effect
            or child.spec != round_.spec
            or child.lane_id != round_.lane_id
            or child.run_id != round_.run_id
            or child.state != "verifying"
            or child.resources != OwnedResources()
            or child.pending_effect
            or child.accepted_callback_kind != "review"
            or not child.accepted_callback_id
            or SHA256.fullmatch(child.accepted_callback_sha256) is None
        ):
            raise TaskReviewError("drift quarantine lane identity is invalid")
        receipt_path = (
            store.root
            / "owners"
            / task_id
            / "runtime"
            / lane.operation_id
            / "callback-receipt.json"
        )
        receipt, receipt_raw = _bounded_json_bytes(
            receipt_path, "accepted callback receipt"
        )
        receipt_id, receipt_sha = _receipt_identity(
            receipt, operation_id=round_.operation_id, run_id=round_.run_id
        )
        if (
            receipt_id != child.accepted_callback_id
            or receipt_sha != child.accepted_callback_sha256
        ):
            raise TaskReviewError("accepted callback receipt changed")
        callback, callback_raw = _bounded_json_bytes(
            _callback_path(runtime_root, lane.axis),
            "retained review callback",
        )
        callback_id, callback_sha = _callback_identity(
            callback, operation_id=round_.operation_id, run_id=round_.run_id
        )
        relation = (
            "match"
            if (receipt_id, receipt_sha) == (callback_id, callback_sha)
            else "drift"
        )
        receipt_archive = root / "artifacts" / f"{lane.axis}-accepted-receipt.json"
        callback_archive = root / "artifacts" / f"{lane.axis}-callback-artifact.json"
        archives.extend(
            (
                (receipt_archive, receipt_raw, "accepted callback receipt"),
                (callback_archive, callback_raw, "retained callback"),
            )
        )
        rows.append(
            {
                "axis": lane.axis,
                "parent_operation_id": lane.operation_id,
                "parent_run_id": lane.run_id,
                "round_operation_id": round_.operation_id,
                "round_run_id": round_.run_id,
                "accepted_callback_id": receipt_id,
                "accepted_callback_sha256": receipt_sha,
                "callback_artifact_id": callback_id,
                "callback_artifact_sha256": callback_sha,
                "identity_relation": relation,
                "accepted_receipt_pointer": receipt_archive.relative_to(
                    gate.root
                ).as_posix(),
                "accepted_receipt_file_sha256": hashlib.sha256(
                    receipt_raw
                ).hexdigest(),
                "callback_artifact_pointer": callback_archive.relative_to(
                    gate.root
                ).as_posix(),
                "callback_artifact_file_sha256": hashlib.sha256(
                    callback_raw
                ).hexdigest(),
            }
        )
    return rows, expected_operations, archives


def _validate_relations(
    rows: list[dict[str, object]],
    authorization: DriftQuarantineAuthorization,
) -> None:
    relations = [str(row["identity_relation"]) for row in rows]
    axes = [str(row["axis"]) for row in rows]
    drift = next((row for row in rows if row["identity_relation"] == "drift"), None)
    if (
        sorted(relations) != ["drift", "match"]
        or sorted(axes) != ["openai-engineering", "openai-intent"]
        or drift is None
        or drift["axis"] != "openai-engineering"
        or (
            drift["accepted_callback_id"],
            drift["accepted_callback_sha256"],
            drift["callback_artifact_id"],
            drift["callback_artifact_sha256"],
        )
        != (
            authorization.accepted_callback_id,
            authorization.accepted_callback_sha256,
            authorization.artifact_callback_id,
            authorization.artifact_callback_sha256,
        )
    ):
        raise TaskReviewError(
            "drift quarantine requires one exact callback identity drift"
        )


def _validate_unrelated_ownership(
    store: OperationStore, task_id: str, expected_operations: set[str]
) -> None:
    for record in store.list(task_id):
        if record.spec.operation_id in expected_operations:
            continue
        if record.spec.operation_id == task_id and record.spec.kind == "dispatch":
            if record.pending_effect or record.state != "attention-required":
                raise TaskReviewError("drift quarantine dispatch ownership is invalid")
            continue
        if (
            record.state not in TERMINAL
            or record.resources != OwnedResources()
            or record.pending_effect
        ):
            raise TaskReviewError("drift quarantine found unrelated live ownership")


def validate_unrelated_ownership_from_evidence(
    store: OperationStore, task_id: str, evidence: dict[str, Any]
) -> None:
    rows = evidence.get("lanes")
    if not isinstance(rows, list):
        raise TaskReviewError("drift quarantine evidence is invalid")
    expected = {
        str(row.get(key) or "")
        for row in rows
        if isinstance(row, dict)
        for key in ("parent_operation_id", "round_operation_id")
    }
    if len(expected) != 4 or "" in expected:
        raise TaskReviewError("drift quarantine evidence identity changed")
    _validate_unrelated_ownership(store, task_id, expected)


def build_evidence(
    *,
    authorization: DriftQuarantineAuthorization,
    gate: ReviewGateController,
    run: ReviewGateRun,
    store: OperationStore,
    runtime_root: Path,
    task_id: str,
    current_head: str,
) -> dict[str, Any]:
    state = gate.read()
    attempt = state.get("attempt")
    identity = attempt.get("identity") if isinstance(attempt, dict) else None
    if (
        state.get("execution_protocol") != "exact-head-attempt-v1"
        or state.get("status") != "reviewing"
        or not isinstance(attempt, dict)
        or attempt.get("status") != "awaiting-callback"
        or not isinstance(identity, dict)
        or identity.get("exact_head_sha")
        != run.execution.request.context.head_sha
        or len(run.execution.lanes) != 2
    ):
        raise TaskReviewError(
            "drift quarantine is not at one exact retained review attempt"
        )
    rows, expected_operations, archives = _lane_evidence(
        authorization=authorization,
        gate=gate,
        run=run,
        store=store,
        runtime_root=runtime_root,
        task_id=task_id,
    )
    _validate_relations(rows, authorization)
    _validate_unrelated_ownership(store, task_id, expected_operations)
    for path, raw, label in archives:
        _archive_exact(path, raw, label)
    return {
        "schema_version": 1,
        "status": "quarantined-evidence",
        "operation_id": task_id,
        "review_operation_id": run.execution.request.policy.operation_id,
        "reviewed_head_sha": run.execution.request.context.head_sha,
        "replacement_head_sha": current_head,
        "authorization_record_id": authorization.authorization_record_id,
        "authorization_record_sha256": authorization.authorization_record_sha256,
        "anchor_record_id": authorization.anchor_record_id,
        "anchor_record_sha256": authorization.anchor_record_sha256,
        "callback_effects_replayed": 0,
        "provider_effects_replayed": 0,
        "lanes": rows,
    }


def persist_evidence(
    gate: ReviewGateController,
    authorization: DriftQuarantineAuthorization,
    evidence: dict[str, Any],
) -> Path:
    path = evidence_root(gate, authorization) / "evidence.json"
    if path.exists():
        if path.is_symlink() or _read_json(path, "drift quarantine evidence") != evidence:
            raise TaskReviewError("drift quarantine evidence changed")
    else:
        _atomic_json(path, evidence)
    return path


def write_progress(
    gate: ReviewGateController,
    authorization: DriftQuarantineAuthorization,
    *,
    evidence_path: Path,
    status: str,
    cleaned_parents: list[str],
    terminal_rounds: list[str],
    retirement_receipts: dict[str, str] | None = None,
) -> None:
    payload: dict[str, object] = {
        "schema_version": 2 if retirement_receipts is not None else 1,
        "status": status,
        "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        "cleaned_parents": sorted(cleaned_parents),
        "terminal_rounds": sorted(terminal_rounds),
    }
    if retirement_receipts is not None:
        payload["retirement_receipts"] = dict(sorted(retirement_receipts.items()))
    _atomic_json(
        evidence_root(gate, authorization) / "progress.json",
        payload,
    )


def validate_progress(
    gate: ReviewGateController,
    authorization: DriftQuarantineAuthorization,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Validate exact restart progress without silently resetting it."""

    path = evidence_root(gate, authorization) / "progress.json"
    if path.is_symlink() or not path.is_file():
        raise TaskReviewError("drift quarantine progress is unavailable")
    progress = _read_json(path, "drift quarantine progress")
    schema_version = progress.get("schema_version")
    expected_fields = PROGRESS_FIELDS_V1 if schema_version == 1 else PROGRESS_FIELDS_V2
    rows = evidence.get("lanes")
    if not isinstance(rows, list):
        raise TaskReviewError("drift quarantine progress identity changed")
    expected_parents = {
        str(row.get("parent_operation_id") or "")
        for row in rows
        if isinstance(row, dict)
    }
    expected_rounds = {
        str(row.get("round_operation_id") or "")
        for row in rows
        if isinstance(row, dict)
    }
    cleaned = progress.get("cleaned_parents")
    terminal = progress.get("terminal_rounds")
    receipts = progress.get("retirement_receipts", {})
    evidence_path = evidence_root(gate, authorization) / "evidence.json"
    if (
        schema_version not in {1, 2}
        or set(progress) != expected_fields
        or progress.get("status") not in PROGRESS_STATUSES
        or progress.get("evidence_sha256")
        != hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        or not isinstance(cleaned, list)
        or not all(isinstance(item, str) for item in cleaned)
        or cleaned != sorted(set(cleaned))
        or not set(cleaned).issubset(expected_parents)
        or not isinstance(terminal, list)
        or not all(isinstance(item, str) for item in terminal)
        or terminal != sorted(set(terminal))
        or not set(terminal).issubset(expected_rounds)
        or not isinstance(receipts, dict)
        or any(
            not isinstance(parent, str)
            or not isinstance(digest, str)
            or parent not in expected_parents
            or SHA256.fullmatch(str(digest)) is None
            for parent, digest in receipts.items()
        )
    ):
        raise TaskReviewError("drift quarantine progress identity changed")
    status = str(progress["status"])
    if (
        (status == "prepared" and (cleaned or terminal))
        or (status == "cleaning" and terminal)
        or (
            status in {"terminalizing-rounds", "quarantined", "fresh-review-started"}
            and set(cleaned) != expected_parents
        )
        or (
            status in {"quarantined", "fresh-review-started"}
            and set(terminal) != expected_rounds
        )
    ):
        raise TaskReviewError("drift quarantine progress identity changed")
    return progress
