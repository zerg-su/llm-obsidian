"""State-free engineering/fix phase operations over the existing harness store."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from ..callbacks import CallbackBroker, CallbackError
from ..contracts import (
    CallbackEnvelope,
    ContractError,
    OperationRecord,
    OperationSpec,
)
from ..store import OperationStore, StoreError


from .engineering_fix_model import (
    FIX_PHASES,
    GIT_OID_RE,
    IDENTIFIER_RE,
    PAYLOAD_FIELDS,
    PHASE_SCHEMAS,
    RECEIPT_FIELDS,
    RETRY_PHASES,
    SHA256_RE,
    FixPhaseRound,
    FixProgress,
    FixStepReceipt,
    FixWorkflowError,
    _canonical,
    _digest,
    _expected_retry_round,
    _expected_round,
    _git_oid,
    _identifier,
    _input_sha256,
    _relative,
    _round_identity,
    _sha256,
    _validate_retry_context,
    reconcile_fix,
    reconcile_retry_fix,
)

def _prepare_round(
    store: OperationStore,
    round_: FixPhaseRound,
) -> FixPhaseRound:
    try:
        record = store.create(
            round_.spec,
            lane_id=round_.lane_id,
            run_id=round_.run_id,
        )
        if record.state == "created":
            for state in (
                "preflight",
                "starting",
                "running",
                "awaiting-callback",
            ):
                store.transition(
                    round_.spec.owner_id,
                    round_.spec.operation_id,
                    state,
                )
        elif record.state != "awaiting-callback":
            raise FixWorkflowError(
                "fix phase operation is not awaiting its exact callback"
            )
    except (ContractError, StoreError) as exc:
        raise FixWorkflowError("fix phase operation identity changed") from exc
    return round_


def prepare_next_phase(
    store: OperationStore,
    parent: OperationRecord,
    *,
    definition_sha256: str,
    approved_plan_sha256: str,
    initial_head_sha: str,
    receipts: Sequence[FixStepReceipt],
    iteration: int,
) -> FixPhaseRound:
    """Create or replay the exact awaiting child for the first missing phase."""

    progress = reconcile_fix(
        parent,
        definition_sha256=definition_sha256,
        approved_plan_sha256=approved_plan_sha256,
        initial_head_sha=initial_head_sha,
        receipts=receipts,
        iteration=iteration,
    )
    if progress.action != "start":
        raise FixWorkflowError(
            f"fix workflow cannot prepare a phase from {progress.action}"
        )
    round_ = _expected_round(
        parent,
        definition_sha256=definition_sha256,
        approved_plan_sha256=approved_plan_sha256,
        initial_head_sha=initial_head_sha,
        step_id=progress.step_id,
        iteration=iteration,
        prior_receipt=progress.prior_receipt,
    )
    return _prepare_round(store, round_)


def prepare_retry_phase(
    store: OperationStore,
    parent: OperationRecord,
    *,
    definition_sha256: str,
    reproduction_receipt: FixStepReceipt,
    verification_sha256: str,
    failed_head_sha: str,
    current_head_sha: str,
    receipts: Sequence[FixStepReceipt],
    iteration: int,
) -> FixPhaseRound:
    """Create or replay the first missing phase of one bounded retry."""

    progress = reconcile_retry_fix(
        parent,
        definition_sha256=definition_sha256,
        reproduction_receipt=reproduction_receipt,
        verification_sha256=verification_sha256,
        failed_head_sha=failed_head_sha,
        current_head_sha=current_head_sha,
        receipts=receipts,
        iteration=iteration,
    )
    if progress.action != "start":
        raise FixWorkflowError(
            f"fix retry cannot prepare a phase from {progress.action}"
        )
    round_ = _expected_retry_round(
        parent,
        definition_sha256=definition_sha256,
        reproduction_receipt=reproduction_receipt,
        verification_sha256=verification_sha256,
        failed_head_sha=failed_head_sha,
        current_head_sha=current_head_sha,
        step_id=progress.step_id,
        iteration=iteration,
        prior_receipt=progress.prior_receipt,
    )
    return _prepare_round(store, round_)


def fix_phase_request(round_: FixPhaseRound) -> dict[str, object]:
    """Build the only request mapping for one engineering/fix phase."""

    return {
        "schema_version": 1,
        "operation_id": round_.spec.operation_id,
        "run_id": round_.run_id,
        "parent_operation_id": round_.parent_operation_id,
        "lane_id": round_.lane_id,
        "definition_sha256": round_.spec.contract_sha256,
        "step_id": round_.step_id,
        "iteration": round_.iteration,
        "input_schema": round_.input_schema,
        "input_sha256": round_.input_sha256,
        "input_head_sha": round_.input_head_sha,
        "prior_receipt_sha256": round_.prior_receipt_sha256,
        "verification_sha256": round_.verification_sha256,
        "output_schema": round_.output_schema,
        "result_pointer": (
            f".task-pipeline/results/pass-{round_.iteration}/{round_.step_id}.json"
        ),
        "output_pointer": (
            f".task-pipeline/outputs/pass-{round_.iteration}/{round_.step_id}.md"
        ),
    }


def phase_envelope(
    round_: FixPhaseRound,
    *,
    status: str,
    output_pointer: str,
    output_sha256: str,
    head_sha: str,
) -> CallbackEnvelope:
    """Build the only callback payload accepted for one fixed phase."""

    if status not in {"complete", "cannot-reproduce"}:
        raise FixWorkflowError("fix phase status is invalid")
    if status == "cannot-reproduce" and round_.step_id != "reproduce":
        raise FixWorkflowError(
            "cannot-reproduce is valid only for the reproduce phase"
        )
    payload: dict[str, object] = {
        "schema_version": 1,
        "parent_operation_id": round_.parent_operation_id,
        "definition_sha256": round_.spec.contract_sha256,
        "step_id": round_.step_id,
        "iteration": round_.iteration,
        "input_schema": round_.input_schema,
        "input_sha256": round_.input_sha256,
        "input_head_sha": round_.input_head_sha,
        "prior_receipt_sha256": round_.prior_receipt_sha256,
        "verification_sha256": round_.verification_sha256,
        "output_schema": round_.output_schema,
        "output_pointer": _relative(output_pointer, "output_pointer"),
        "output_sha256": _sha256(output_sha256, "output_sha256"),
        "head_sha": _git_oid(head_sha, "head_sha"),
        "status": status,
    }
    encoded = _canonical(payload)
    payload_sha256 = hashlib.sha256(encoded).hexdigest()
    try:
        return CallbackEnvelope(
            callback_id=f"result-{payload_sha256[:24]}",
            operation_id=round_.spec.operation_id,
            run_id=round_.run_id,
            kind="result",
            payload=payload,
            payload_sha256=payload_sha256,
        )
    except ContractError as exc:
        raise FixWorkflowError("fix phase envelope is invalid") from exc


def _receipt_from_envelope(
    round_: FixPhaseRound,
    envelope: CallbackEnvelope,
    *,
    current_head_sha: str,
) -> FixStepReceipt:
    if (
        envelope.operation_id != round_.spec.operation_id
        or envelope.run_id != round_.run_id
        or envelope.kind != "result"
        or set(envelope.payload) != PAYLOAD_FIELDS
    ):
        raise FixWorkflowError("fix phase callback identity changed")
    payload = dict(envelope.payload)
    expected = {
        "schema_version": 1,
        "parent_operation_id": round_.parent_operation_id,
        "definition_sha256": round_.spec.contract_sha256,
        "step_id": round_.step_id,
        "iteration": round_.iteration,
        "input_schema": round_.input_schema,
        "input_sha256": round_.input_sha256,
        "input_head_sha": round_.input_head_sha,
        "prior_receipt_sha256": round_.prior_receipt_sha256,
        "verification_sha256": round_.verification_sha256,
        "output_schema": round_.output_schema,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise FixWorkflowError("fix phase callback identity changed")
    head_sha = _git_oid(str(payload.get("head_sha") or ""), "callback head_sha")
    if head_sha != _git_oid(current_head_sha, "current_head_sha"):
        raise FixWorkflowError("fix phase callback HEAD changed before acceptance")
    try:
        return FixStepReceipt(
            callback_id=envelope.callback_id,
            operation_id=envelope.operation_id,
            parent_operation_id=str(payload["parent_operation_id"]),
            lane_id=round_.lane_id,
            run_id=envelope.run_id,
            definition_sha256=str(payload["definition_sha256"]),
            step_id=str(payload["step_id"]),
            iteration=int(payload["iteration"]),
            input_schema=str(payload["input_schema"]),
            input_sha256=str(payload["input_sha256"]),
            input_head_sha=str(payload["input_head_sha"]),
            prior_receipt_sha256=str(payload["prior_receipt_sha256"]),
            verification_sha256=str(payload["verification_sha256"]),
            output_schema=str(payload["output_schema"]),
            output_pointer=str(payload.get("output_pointer") or ""),
            output_sha256=str(payload.get("output_sha256") or ""),
            head_sha=head_sha,
            status=str(payload.get("status") or ""),
        )
    except (TypeError, ValueError) as exc:
        raise FixWorkflowError("fix phase callback payload is invalid") from exc


def _receipt_bytes(receipt: FixStepReceipt) -> bytes:
    return _canonical(receipt.to_dict()) + b"\n"


def _write_receipt(path: Path, receipt: FixStepReceipt) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise FixWorkflowError("fix receipt path cannot be a symlink")
    encoded = _receipt_bytes(receipt)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise FixWorkflowError("accepted fix receipt is unreadable") from exc
        if current != encoded:
            raise FixWorkflowError("accepted fix receipt changed")
        return
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def load_receipt(path: Path) -> FixStepReceipt:
    """Load one exact receipt without accepting schema or key drift."""

    if path.is_symlink():
        raise FixWorkflowError("fix receipt path cannot be a symlink")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixWorkflowError("fix receipt is unreadable") from exc
    if not isinstance(value, dict) or set(value) != RECEIPT_FIELDS:
        raise FixWorkflowError("fix receipt keys changed")
    try:
        return FixStepReceipt(**value)
    except TypeError as exc:
        raise FixWorkflowError("fix receipt shape changed") from exc


def accept_phase(
    store: OperationStore,
    round_: FixPhaseRound,
    envelope: CallbackEnvelope,
    *,
    current_head_sha: str,
    receipt_path: Path,
) -> FixStepReceipt:
    """Accept, persist, and terminally close one resource-less phase child."""

    receipt = _receipt_from_envelope(
        round_, envelope, current_head_sha=current_head_sha
    )
    try:
        CallbackBroker(store, round_.spec.owner_id).accept(envelope)
    except (CallbackError, ContractError, StoreError) as exc:
        raise FixWorkflowError("fix phase callback acceptance failed") from exc
    _write_receipt(receipt_path, receipt)
    try:
        record = store.read(
            round_.spec.owner_id, round_.spec.operation_id
        )
        if record.state not in {"complete", "failed", "cancelled"}:
            if record.state != "finalizing":
                raise FixWorkflowError(
                    "accepted fix phase child is not finalizing"
                )
            store.transition(
                round_.spec.owner_id, round_.spec.operation_id, "exiting"
            )
            store.transition(
                round_.spec.owner_id, round_.spec.operation_id, "complete"
            )
    except (ContractError, StoreError) as exc:
        raise FixWorkflowError(
            "accepted fix phase child could not complete"
        ) from exc
    return receipt
