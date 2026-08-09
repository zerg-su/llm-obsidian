"""Authoritative read-only receipt classification for the dashboard."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .contracts import (
    ContractError,
    OperationRecord,
    OperationSpec,
    VerificationEvidence,
)
from .store import OperationStore, StoreError
from .verification import output_binding_valid
from .verification_attempt import VerificationAttempt, VerificationAttemptError
from .workflows.engineering_fix import FixWorkflowError, load_receipt
from .workflows.engineering_fix_model import PAYLOAD_FIELDS


MAX_VISITS = 16


def _read_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def fix_receipt_visits(
    store: OperationStore,
    record: OperationRecord,
    runtime: Path,
    step_id: str,
) -> tuple[tuple[int, ...], str]:
    """Return complete fix passes and one bounded invalid/failed issue code."""

    root = runtime / "pipeline-fix"
    if not root.is_dir():
        return (), ""
    visits: list[int] = []
    issue = ""
    for path in sorted(root.glob("pass-*")):
        suffix = path.name.removeprefix("pass-")
        if not suffix.isdigit():
            continue
        receipt_path = path / step_id / "receipt.json"
        if not receipt_path.exists() and not receipt_path.is_symlink():
            continue
        try:
            receipt = load_receipt(receipt_path)
            child = store.read(record.spec.owner_id, receipt.operation_id)
            receipt_fields = receipt.to_dict()
            payload = {key: receipt_fields[key] for key in PAYLOAD_FIELDS}
            payload_sha256 = hashlib.sha256(
                json.dumps(
                    payload, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            if (
                receipt.parent_operation_id != record.spec.operation_id
                or receipt.definition_sha256 != record.spec.contract_sha256
                or receipt.step_id != step_id
                or receipt.iteration != int(suffix)
                or child.spec.kind != "pipeline-model-step"
                or child.spec.parent_operation_id != record.spec.operation_id
                or child.spec.contract_sha256 != record.spec.contract_sha256
                or child.lane_id != receipt.lane_id
                or child.run_id != receipt.run_id
                or child.state != "complete"
                or child.accepted_callback_id != receipt.callback_id
                or child.accepted_callback_kind != "result"
                or child.accepted_callback_sha256 != payload_sha256
                or receipt.callback_id != f"result-{payload_sha256[:24]}"
            ):
                raise FixWorkflowError("fix receipt identity changed")
        except (FixWorkflowError, StoreError, OSError, ValueError):
            issue = "fix-receipt-invalid"
            continue
        if receipt.status == "complete":
            visits.append(int(suffix))
        else:
            issue = "fix-receipt-failed"
    return tuple(visits[:MAX_VISITS]), issue


def verification_identity(
    parent: OperationSpec,
    definition_sha256: str,
    input_sha256: str,
    attempt_index: int,
) -> tuple[str, str, str, str]:
    """Derive the production verification operation, lane, run, and effect."""

    suffix = f"-verify-{input_sha256[:16]}" + (
        f"-a{attempt_index}" if attempt_index else ""
    )
    operation_id = f"{parent.operation_id[: 128 - len(suffix)]}{suffix}"
    attempt = f":attempt:{attempt_index}" if attempt_index else ""
    key = hashlib.sha256(
        (
            f"{parent.idempotency_key}:pipeline-verify:{operation_id}:"
            f"{definition_sha256}:{input_sha256}:{parent.verification_profile}{attempt}"
        ).encode()
    ).hexdigest()
    lane = hashlib.sha256(f"{key}:lane".encode()).hexdigest()[:32]
    run = hashlib.sha256(f"{key}:run".encode()).hexdigest()[:32]
    effect = (
        "pipeline-verify-" + input_sha256[:32]
        if not attempt_index
        else "pipeline-verify-"
        + hashlib.sha256(
            f"{input_sha256}:attempt:{attempt_index}".encode()
        ).hexdigest()[:32]
    )
    return operation_id, lane, run, effect


def verification_receipt_status(
    store: OperationStore,
    record: OperationRecord,
    runtime: Path,
    path: Path,
) -> str:
    """Classify one verification receipt through its accepted durable identity."""

    value = _read_object(path)
    evidence = value.get("evidence") if value else None
    try:
        if (
            value is None
            or value.get("schema_version") not in {1, 2}
            or value.get("parent_operation_id") != record.spec.operation_id
            or value.get("definition_sha256") != record.spec.contract_sha256
            or value.get("step_id") != "verify"
            or value.get("profile") != record.spec.verification_profile
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(value.get("profile_sha256") or "")
            )
            or not re.fullmatch(
                r"[0-9a-f]{40,64}", str(value.get("head_sha") or "")
            )
            or value.get("status") not in {"complete", "failed"}
            or not isinstance(evidence, list)
            or not evidence
        ):
            raise ValueError
        attempt = _verification_attempt(value, record)
        input_sha = str(value.get("input_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", input_sha):
            raise ValueError
        operation_id, lane_id, run_id, effect_id = verification_identity(
            record.spec,
            record.spec.contract_sha256,
            input_sha,
            attempt.attempt_index,
        )
        child = store.read(record.spec.owner_id, operation_id)
        expected_path = (
            runtime / "pipeline-verification" / operation_id / "receipt.json"
        )
        if (
            attempt.parent_operation_id != record.spec.operation_id
            or attempt.profile != record.spec.verification_profile
            or attempt.profile_sha256 != value["profile_sha256"]
            or attempt.exact_head_sha != value["head_sha"]
            or value.get("operation_id") != operation_id
            or value.get("lane_id") != lane_id
            or value.get("run_id") != run_id
            or value.get("effect_id") != effect_id
            or child.spec.kind != "pipeline-verify"
            or child.spec.parent_operation_id != record.spec.operation_id
            or child.spec.contract_sha256 != record.spec.contract_sha256
            or child.lane_id != lane_id
            or child.run_id != run_id
            or path.resolve() != expected_path.resolve()
        ):
            raise ValueError
        exit_codes: list[int] = []
        for row in evidence:
            typed = VerificationEvidence(**row)
            if (
                typed.profile != record.spec.verification_profile
                or typed.profile_sha256 != value["profile_sha256"]
                or typed.head_sha != value["head_sha"]
                or not output_binding_valid(typed, pointer_root=runtime)
            ):
                raise ValueError
            exit_codes.append(typed.exit_code)
        succeeded = all(code == 0 for code in exit_codes)
        if (value["status"] == "complete") != succeeded:
            raise ValueError
        return str(value["status"])
    except (
        ContractError,
        StoreError,
        TypeError,
        ValueError,
        VerificationAttemptError,
    ):
        return "invalid"


def _verification_attempt(
    value: dict[str, Any], record: OperationRecord
) -> VerificationAttempt:
    """Load the exact attempt identity carried by one receipt generation."""

    if value.get("schema_version") == 2:
        attempt = VerificationAttempt.from_dict(
            value.get("verification_attempt")
        )
        if value.get("verification_attempt_sha256") != attempt.sha256:
            raise VerificationAttemptError(
                "verification attempt digest is invalid"
            )
        return attempt
    if value.get("schema_version") == 1:
        return VerificationAttempt(
            record.spec.operation_id,
            record.spec.verification_profile,
            str(value.get("profile_sha256") or ""),
            str(value.get("head_sha") or ""),
            0,
        )
    raise VerificationAttemptError("verification attempt schema is invalid")


def verification_receipt_visits(
    store: OperationStore,
    record: OperationRecord,
    runtime: Path,
    *,
    exact_head_sha: str = "",
) -> tuple[tuple[int, ...], str]:
    """Return bounded visit history and current exact-HEAD receipt truth."""

    root = runtime / "pipeline-verification"
    if not root.is_dir():
        return (
            (),
            "verification-receipt-missing" if exact_head_sha else "",
        )
    observations: list[tuple[VerificationAttempt | None, str, str]] = []
    for path in sorted(root.glob("*/receipt.json")):
        value = _read_object(path)
        try:
            attempt = (
                _verification_attempt(value, record)
                if value is not None
                else None
            )
        except VerificationAttemptError:
            attempt = None
        status = verification_receipt_status(store, record, runtime, path)
        head_sha = str(value.get("head_sha") or "") if value else ""
        observations.append((attempt, status, head_sha))
    complete_count = min(
        sum(status == "complete" for _attempt, status, _head in observations),
        MAX_VISITS,
    )
    visits = tuple(range(complete_count))
    current = observations
    if exact_head_sha:
        exact = tuple(
            item
            for item in observations
            if item[2] == exact_head_sha
        )
        if not exact:
            return visits, "verification-receipt-missing"
        valid_attempts = tuple(item[0] for item in exact if item[0] is not None)
        if not valid_attempts:
            current = exact
        else:
            latest_attempt = max(item.attempt_index for item in valid_attempts)
            current = [
                item
                for item in exact
                if item[0] is None or item[0].attempt_index == latest_attempt
            ]
    issue = ""
    for _attempt, status, _head in current:
        if status != "complete":
            issue = (
                "verification-receipt-failed"
                if status == "failed"
                else "verification-receipt-invalid"
            )
    return visits, issue
