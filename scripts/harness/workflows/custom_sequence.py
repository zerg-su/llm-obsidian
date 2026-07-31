"""Replay-safe execution receipts for approved custom model-step sequences."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from ..callbacks import CallbackBroker, CallbackError
from ..contracts import CallbackEnvelope, ContractError, OperationRecord, OperationSpec
from ..custom_pipelines import PipelineSpec, PipelineTransition
from ..store import OperationStore, StoreError


SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
GIT_OID_RE = re.compile(r"[0-9a-f]{40,64}\Z")
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
RECEIPT_FIELDS = {
    "schema_version",
    "callback_id",
    "operation_id",
    "parent_operation_id",
    "lane_id",
    "run_id",
    "definition_sha256",
    "step_id",
    "visit",
    "input_schema",
    "input_sha256",
    "input_head_sha",
    "prior_receipt_sha256",
    "output_schema",
    "output_pointer",
    "output_sha256",
    "head_sha",
    "outcome",
}
PAYLOAD_FIELDS = RECEIPT_FIELDS - {
    "callback_id",
    "operation_id",
    "lane_id",
    "run_id",
}


class CustomSequenceError(RuntimeError):
    """The approved custom sequence cannot advance safely."""


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise CustomSequenceError(f"{label} must be a bounded identifier")
    return value


def _sha256(value: str, label: str, *, optional: bool = False) -> str:
    if optional and value == "":
        return ""
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise CustomSequenceError(f"{label} must be a lowercase sha256")
    return value


def _git_oid(value: str, label: str) -> str:
    if not isinstance(value, str) or not GIT_OID_RE.fullmatch(value):
        raise CustomSequenceError(f"{label} must be a Git object id")
    return value


def _relative(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise CustomSequenceError(f"{label} must be owner-relative")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise CustomSequenceError(f"{label} must be owner-relative")
    return path.as_posix()


def _canonical(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(dict(value), sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise CustomSequenceError("custom sequence value must be canonical JSON") from exc


def _digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True)
class CustomStepReceipt:
    callback_id: str
    operation_id: str
    parent_operation_id: str
    lane_id: str
    run_id: str
    definition_sha256: str
    step_id: str
    visit: int
    input_schema: str
    input_sha256: str
    input_head_sha: str
    prior_receipt_sha256: str
    output_schema: str
    output_pointer: str
    output_sha256: str
    head_sha: str
    outcome: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise CustomSequenceError("unsupported custom receipt schema")
        for value, label in (
            (self.callback_id, "receipt callback_id"),
            (self.operation_id, "receipt operation_id"),
            (self.parent_operation_id, "receipt parent_operation_id"),
            (self.lane_id, "receipt lane_id"),
            (self.run_id, "receipt run_id"),
            (self.step_id, "receipt step_id"),
            (self.outcome, "receipt outcome"),
        ):
            _identifier(value, label)
        if type(self.visit) is not int or self.visit < 0:
            raise CustomSequenceError("receipt visit must be non-negative")
        _sha256(self.definition_sha256, "receipt definition_sha256")
        _sha256(self.input_sha256, "receipt input_sha256")
        _sha256(
            self.prior_receipt_sha256,
            "receipt prior_receipt_sha256",
            optional=True,
        )
        _sha256(self.output_sha256, "receipt output_sha256")
        _git_oid(self.input_head_sha, "receipt input_head_sha")
        _git_oid(self.head_sha, "receipt head_sha")
        _relative(self.output_pointer, "receipt output_pointer")

    def to_dict(self) -> dict[str, object]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @property
    def receipt_sha256(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True)
class CustomStepRound:
    spec: OperationSpec
    parent_operation_id: str
    lane_id: str
    run_id: str
    step_id: str
    visit: int
    input_schema: str
    input_sha256: str
    input_head_sha: str
    prior_receipt_sha256: str
    output_schema: str
    allowed_outcomes: tuple[str, ...]


@dataclass(frozen=True)
class CustomSequenceProgress:
    action: str
    step_id: str = ""
    visit: int = 0
    completed_steps: tuple[str, ...] = ()
    terminal_outcome: str = ""
    prior_receipt: CustomStepReceipt | None = None

    def __post_init__(self) -> None:
        if self.action not in {"start", "attention", "complete"}:
            raise CustomSequenceError("custom sequence action is invalid")
        if self.action == "start":
            _identifier(self.step_id, "custom sequence step_id")
        elif self.step_id:
            raise CustomSequenceError("terminal custom progress cannot name a step")


def transition_map(spec: PipelineSpec) -> dict[tuple[str, str], PipelineTransition]:
    return {(item.from_step, item.outcome): item for item in spec.transitions}


def _allowed_outcomes(spec: PipelineSpec, step_id: str) -> tuple[str, ...]:
    outcomes = tuple(
        sorted(item.outcome for item in spec.transitions if item.from_step == step_id)
    )
    if not outcomes:
        raise CustomSequenceError("custom model step has no typed outcome")
    return outcomes


def _input_sha256(
    *,
    definition_sha256: str,
    step_id: str,
    visit: int,
    input_schema: str,
    input_head_sha: str,
    approved_plan_sha256: str,
    prior_receipt_sha256: str,
) -> str:
    return _digest(
        {
            "schema_version": 1,
            "definition_sha256": _sha256(definition_sha256, "definition_sha256"),
            "step_id": step_id,
            "visit": visit,
            "input_schema": input_schema,
            "input_head_sha": _git_oid(input_head_sha, "input_head_sha"),
            "approved_plan_sha256": (
                _sha256(approved_plan_sha256, "approved_plan_sha256")
                if not prior_receipt_sha256
                else ""
            ),
            "prior_receipt_sha256": _sha256(
                prior_receipt_sha256,
                "prior_receipt_sha256",
                optional=True,
            ),
        }
    )


def _expected_round(
    parent: OperationRecord,
    pipeline_spec: PipelineSpec,
    *,
    definition_sha256: str,
    approved_plan_sha256: str,
    initial_head_sha: str,
    step_id: str,
    visit: int,
    prior_receipt: CustomStepReceipt | None,
) -> CustomStepRound:
    step = next((item for item in pipeline_spec.steps if item.step_id == step_id), None)
    if step is None or step.primitive_id != "model_step":
        raise CustomSequenceError("custom round requires a registered model step")
    input_head_sha = initial_head_sha if prior_receipt is None else prior_receipt.head_sha
    prior_sha256 = "" if prior_receipt is None else prior_receipt.receipt_sha256
    input_sha256 = _input_sha256(
        definition_sha256=definition_sha256,
        step_id=step_id,
        visit=visit,
        input_schema=step.input_schema,
        input_head_sha=input_head_sha,
        approved_plan_sha256=approved_plan_sha256,
        prior_receipt_sha256=prior_sha256,
    )
    suffix = f"-custom-{visit}-{input_sha256[:12]}"
    operation_id = f"{parent.spec.operation_id[: 128 - len(suffix)]}{suffix}"
    idempotency_key = hashlib.sha256(
        (
            f"{parent.spec.idempotency_key}:custom-model-step:{operation_id}:"
            f"{definition_sha256}:{step_id}:{visit}:{input_sha256}"
        ).encode()
    ).hexdigest()
    operation_spec = OperationSpec(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        kind="pipeline-model-step",
        owner_id=parent.spec.owner_id,
        route=parent.spec.route,
        context_manifest=parent.spec.context_manifest,
        verification_profile=parent.spec.verification_profile,
        keep_open=False,
        contract_sha256=definition_sha256,
    )
    return CustomStepRound(
        spec=operation_spec,
        parent_operation_id=parent.spec.operation_id,
        lane_id=parent.lane_id,
        run_id=hashlib.sha256(f"{idempotency_key}:run".encode()).hexdigest()[:32],
        step_id=step_id,
        visit=visit,
        input_schema=step.input_schema,
        input_sha256=input_sha256,
        input_head_sha=input_head_sha,
        prior_receipt_sha256=prior_sha256,
        output_schema=step.output_schema,
        allowed_outcomes=_allowed_outcomes(pipeline_spec, step_id),
    )


def reconcile_custom_sequence(
    parent: OperationRecord,
    pipeline_spec: PipelineSpec,
    *,
    definition_sha256: str,
    approved_plan_sha256: str,
    initial_head_sha: str,
    receipts: Sequence[CustomStepReceipt],
) -> CustomSequenceProgress:
    """Replay the exact transition path and return the first missing model step."""

    if parent.spec.contract_sha256 != _sha256(definition_sha256, "definition_sha256"):
        raise CustomSequenceError("custom definition differs from the parent contract")
    _sha256(approved_plan_sha256, "approved_plan_sha256")
    _git_oid(initial_head_sha, "initial_head_sha")
    step_by_id = {item.step_id: item for item in pipeline_spec.steps}
    current = pipeline_spec.steps[0].step_id
    transitions = transition_map(pipeline_spec)
    traversals: dict[tuple[str, str], int] = {}
    prior: CustomStepReceipt | None = None
    completed: list[str] = []

    for visit, receipt in enumerate(receipts):
        expected = _expected_round(
            parent,
            pipeline_spec,
            definition_sha256=definition_sha256,
            approved_plan_sha256=approved_plan_sha256,
            initial_head_sha=initial_head_sha,
            step_id=current,
            visit=visit,
            prior_receipt=prior,
        )
        if (
            receipt.operation_id != expected.spec.operation_id
            or receipt.parent_operation_id != expected.parent_operation_id
            or receipt.lane_id != expected.lane_id
            or receipt.run_id != expected.run_id
            or receipt.definition_sha256 != definition_sha256
            or receipt.step_id != current
            or receipt.visit != visit
            or receipt.input_schema != expected.input_schema
            or receipt.input_sha256 != expected.input_sha256
            or receipt.input_head_sha != expected.input_head_sha
            or receipt.prior_receipt_sha256 != expected.prior_receipt_sha256
            or receipt.output_schema != expected.output_schema
            or receipt.outcome not in expected.allowed_outcomes
        ):
            raise CustomSequenceError("custom receipt replay identity changed")
        transition = transitions[(current, receipt.outcome)]
        key = (current, receipt.outcome)
        traversals[key] = traversals.get(key, 0) + 1
        if traversals[key] > transition.max_traversals:
            raise CustomSequenceError("custom transition traversal budget exhausted")
        completed.append(current)
        prior = receipt
        if transition.target.startswith("terminal:"):
            terminal = transition.target.removeprefix("terminal:")
            return CustomSequenceProgress(
                "complete" if terminal == "completed" else "attention",
                completed_steps=tuple(completed),
                terminal_outcome=terminal,
                prior_receipt=prior,
            )
        target = step_by_id[transition.target]
        if target.primitive_id != "model_step":
            return CustomSequenceProgress(
                "complete",
                completed_steps=tuple(completed),
                prior_receipt=prior,
            )
        current = target.step_id

    return CustomSequenceProgress(
        "start",
        step_id=current,
        visit=len(receipts),
        completed_steps=tuple(completed),
        prior_receipt=prior,
    )


def prepare_custom_step(
    store: OperationStore,
    parent: OperationRecord,
    pipeline_spec: PipelineSpec,
    *,
    definition_sha256: str,
    approved_plan_sha256: str,
    initial_head_sha: str,
    receipts: Sequence[CustomStepReceipt],
) -> CustomStepRound:
    progress = reconcile_custom_sequence(
        parent,
        pipeline_spec,
        definition_sha256=definition_sha256,
        approved_plan_sha256=approved_plan_sha256,
        initial_head_sha=initial_head_sha,
        receipts=receipts,
    )
    if progress.action != "start":
        raise CustomSequenceError(f"custom sequence cannot start from {progress.action}")
    round_ = _expected_round(
        parent,
        pipeline_spec,
        definition_sha256=definition_sha256,
        approved_plan_sha256=approved_plan_sha256,
        initial_head_sha=initial_head_sha,
        step_id=progress.step_id,
        visit=progress.visit,
        prior_receipt=progress.prior_receipt,
    )
    try:
        record = store.create(round_.spec, lane_id=round_.lane_id, run_id=round_.run_id)
        if record.state == "created":
            for state in ("preflight", "starting", "running", "awaiting-callback"):
                store.transition(round_.spec.owner_id, round_.spec.operation_id, state)
        elif record.state != "awaiting-callback":
            raise CustomSequenceError("custom step is not awaiting its callback")
    except (ContractError, StoreError) as exc:
        raise CustomSequenceError("custom step identity changed") from exc
    return round_


def custom_step_request(round_: CustomStepRound) -> dict[str, object]:
    base = f".task-pipeline/custom/{round_.visit:02d}-{round_.step_id}"
    return {
        "schema_version": 1,
        "workflow_kind": "custom",
        "operation_id": round_.spec.operation_id,
        "run_id": round_.run_id,
        "parent_operation_id": round_.parent_operation_id,
        "lane_id": round_.lane_id,
        "definition_sha256": round_.spec.contract_sha256,
        "step_id": round_.step_id,
        "visit": round_.visit,
        "input_schema": round_.input_schema,
        "input_sha256": round_.input_sha256,
        "input_head_sha": round_.input_head_sha,
        "prior_receipt_sha256": round_.prior_receipt_sha256,
        "output_schema": round_.output_schema,
        "allowed_outcomes": list(round_.allowed_outcomes),
        "result_pointer": f"{base}-result.json",
        "output_pointer": f"{base}-output.md",
    }


def custom_step_envelope(
    round_: CustomStepRound,
    *,
    outcome: str,
    output_pointer: str,
    output_sha256: str,
    head_sha: str,
) -> CallbackEnvelope:
    """Build the exact callback accepted for one custom model-step visit."""

    if outcome not in round_.allowed_outcomes:
        raise CustomSequenceError("custom callback outcome is not allowed")
    payload: dict[str, object] = {
        "schema_version": 1,
        "parent_operation_id": round_.parent_operation_id,
        "definition_sha256": round_.spec.contract_sha256,
        "step_id": round_.step_id,
        "visit": round_.visit,
        "input_schema": round_.input_schema,
        "input_sha256": round_.input_sha256,
        "input_head_sha": round_.input_head_sha,
        "prior_receipt_sha256": round_.prior_receipt_sha256,
        "output_schema": round_.output_schema,
        "output_pointer": _relative(output_pointer, "output_pointer"),
        "output_sha256": _sha256(output_sha256, "output_sha256"),
        "head_sha": _git_oid(head_sha, "head_sha"),
        "outcome": outcome,
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
        raise CustomSequenceError("custom callback envelope is invalid") from exc


def _receipt_from_envelope(
    round_: CustomStepRound,
    envelope: CallbackEnvelope,
    *,
    current_head_sha: str,
) -> CustomStepReceipt:
    if (
        envelope.operation_id != round_.spec.operation_id
        or envelope.run_id != round_.run_id
        or envelope.kind != "result"
        or set(envelope.payload) != PAYLOAD_FIELDS
    ):
        raise CustomSequenceError("custom callback identity changed")
    payload = dict(envelope.payload)
    expected = {
        "schema_version": 1,
        "parent_operation_id": round_.parent_operation_id,
        "definition_sha256": round_.spec.contract_sha256,
        "step_id": round_.step_id,
        "visit": round_.visit,
        "input_schema": round_.input_schema,
        "input_sha256": round_.input_sha256,
        "input_head_sha": round_.input_head_sha,
        "prior_receipt_sha256": round_.prior_receipt_sha256,
        "output_schema": round_.output_schema,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise CustomSequenceError("custom callback identity changed")
    outcome = str(payload.get("outcome") or "")
    if outcome not in round_.allowed_outcomes:
        raise CustomSequenceError("custom callback outcome is not allowed")
    head_sha = _git_oid(str(payload.get("head_sha") or ""), "callback head_sha")
    if head_sha != _git_oid(current_head_sha, "current_head_sha"):
        raise CustomSequenceError("custom callback HEAD changed before acceptance")
    return CustomStepReceipt(
        callback_id=envelope.callback_id,
        operation_id=envelope.operation_id,
        parent_operation_id=str(payload["parent_operation_id"]),
        lane_id=round_.lane_id,
        run_id=envelope.run_id,
        definition_sha256=str(payload["definition_sha256"]),
        step_id=str(payload["step_id"]),
        visit=int(payload["visit"]),
        input_schema=str(payload["input_schema"]),
        input_sha256=str(payload["input_sha256"]),
        input_head_sha=str(payload["input_head_sha"]),
        prior_receipt_sha256=str(payload["prior_receipt_sha256"]),
        output_schema=str(payload["output_schema"]),
        output_pointer=str(payload.get("output_pointer") or ""),
        output_sha256=str(payload.get("output_sha256") or ""),
        head_sha=head_sha,
        outcome=outcome,
    )


def _write_receipt(path: Path, receipt: CustomStepReceipt) -> None:
    encoded = _canonical(receipt.to_dict()) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise CustomSequenceError("custom receipt path cannot be a symlink")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != encoded:
            raise CustomSequenceError("accepted custom receipt changed")
        return
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def load_custom_receipt(path: Path) -> CustomStepReceipt:
    if path.is_symlink():
        raise CustomSequenceError("custom receipt path cannot be a symlink")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CustomSequenceError("custom receipt is unreadable") from exc
    if not isinstance(value, dict) or set(value) != RECEIPT_FIELDS:
        raise CustomSequenceError("custom receipt keys changed")
    try:
        return CustomStepReceipt(**value)
    except TypeError as exc:
        raise CustomSequenceError("custom receipt shape changed") from exc


def accept_custom_step(
    store: OperationStore,
    round_: CustomStepRound,
    envelope: CallbackEnvelope,
    *,
    current_head_sha: str,
    receipt_path: Path,
) -> CustomStepReceipt:
    receipt = _receipt_from_envelope(round_, envelope, current_head_sha=current_head_sha)
    try:
        CallbackBroker(store, round_.spec.owner_id).accept(envelope)
        _write_receipt(receipt_path, receipt)
        record = store.read(round_.spec.owner_id, round_.spec.operation_id)
        if record.state not in {"complete", "failed", "cancelled"}:
            if record.state != "finalizing":
                raise CustomSequenceError("accepted custom step is not finalizing")
            store.transition(round_.spec.owner_id, round_.spec.operation_id, "exiting")
            store.transition(round_.spec.owner_id, round_.spec.operation_id, "complete")
    except (CallbackError, ContractError, StoreError) as exc:
        raise CustomSequenceError("custom callback acceptance failed") from exc
    return receipt
