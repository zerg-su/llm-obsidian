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


SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
GIT_OID_RE = re.compile(r"[0-9a-f]{40,64}\Z")
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
FIX_PHASES = (
    "reproduce",
    "root-cause",
    "regression-test",
    "minimal-fix",
)
PHASE_SCHEMAS = {
    "reproduce": ("approved-plan/v1", "reproduction/v1"),
    "root-cause": ("reproduction/v1", "diagnosis/v1"),
    "regression-test": ("diagnosis/v1", "regression-test/v1"),
    "minimal-fix": ("regression-test/v1", "implementation-result/v1"),
}
RECEIPT_FIELDS = {
    "schema_version",
    "callback_id",
    "operation_id",
    "parent_operation_id",
    "lane_id",
    "run_id",
    "definition_sha256",
    "step_id",
    "iteration",
    "input_schema",
    "input_sha256",
    "input_head_sha",
    "prior_receipt_sha256",
    "output_schema",
    "output_pointer",
    "output_sha256",
    "head_sha",
    "status",
}
PAYLOAD_FIELDS = RECEIPT_FIELDS - {
    "callback_id",
    "operation_id",
    "lane_id",
    "run_id",
}


class FixWorkflowError(RuntimeError):
    """The fixed engineering workflow cannot advance safely."""


def _sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise FixWorkflowError(f"{label} must be a lowercase sha256")
    return value


def _git_oid(value: str, label: str) -> str:
    if not isinstance(value, str) or not GIT_OID_RE.fullmatch(value):
        raise FixWorkflowError(f"{label} must be a Git object id")
    return value


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise FixWorkflowError(f"{label} must be a bounded identifier")
    return value


def _relative(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise FixWorkflowError(f"{label} must be owner-relative")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise FixWorkflowError(f"{label} must be owner-relative")
    return path.as_posix()


def _canonical(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            dict(value), sort_keys=True, separators=(",", ":")
        ).encode()
    except (TypeError, ValueError) as exc:
        raise FixWorkflowError("fix workflow value must be canonical JSON") from exc


def _digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True)
class FixStepReceipt:
    callback_id: str
    operation_id: str
    parent_operation_id: str
    lane_id: str
    run_id: str
    definition_sha256: str
    step_id: str
    iteration: int
    input_schema: str
    input_sha256: str
    input_head_sha: str
    prior_receipt_sha256: str
    output_schema: str
    output_pointer: str
    output_sha256: str
    head_sha: str
    status: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise FixWorkflowError("unsupported fix receipt schema")
        for value, label in (
            (self.callback_id, "receipt callback_id"),
            (self.operation_id, "receipt operation_id"),
            (self.parent_operation_id, "receipt parent_operation_id"),
            (self.lane_id, "receipt lane_id"),
            (self.run_id, "receipt run_id"),
        ):
            _identifier(value, label)
        if self.step_id not in FIX_PHASES:
            raise FixWorkflowError("receipt step is not an engineering/fix phase")
        if (
            not isinstance(self.iteration, int)
            or isinstance(self.iteration, bool)
            or self.iteration < 0
        ):
            raise FixWorkflowError("receipt iteration must be non-negative")
        expected_input, expected_output = PHASE_SCHEMAS[self.step_id]
        if (
            self.input_schema != expected_input
            or self.output_schema != expected_output
        ):
            raise FixWorkflowError("receipt phase schema changed")
        _sha256(self.definition_sha256, "receipt definition_sha256")
        _sha256(self.input_sha256, "receipt input_sha256")
        if self.prior_receipt_sha256:
            _sha256(
                self.prior_receipt_sha256,
                "receipt prior_receipt_sha256",
            )
        _sha256(self.output_sha256, "receipt output_sha256")
        _git_oid(self.input_head_sha, "receipt input_head_sha")
        _git_oid(self.head_sha, "receipt head_sha")
        _relative(self.output_pointer, "receipt output_pointer")
        if self.status not in {"complete", "cannot-reproduce"}:
            raise FixWorkflowError("receipt status is invalid")
        if self.status == "cannot-reproduce" and self.step_id != "reproduce":
            raise FixWorkflowError(
                "cannot-reproduce is valid only for the reproduce phase"
            )

    def to_dict(self) -> dict[str, object]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @property
    def receipt_sha256(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True)
class FixPhaseRound:
    spec: OperationSpec
    parent_operation_id: str
    lane_id: str
    run_id: str
    step_id: str
    iteration: int
    input_schema: str
    input_sha256: str
    input_head_sha: str
    prior_receipt_sha256: str
    output_schema: str

    def __post_init__(self) -> None:
        if self.spec.kind != "pipeline-model-step":
            raise FixWorkflowError("fix phase must use a model-step operation")
        _identifier(self.parent_operation_id, "fix phase parent_operation_id")
        if self.step_id not in FIX_PHASES:
            raise FixWorkflowError("fix phase is unknown")
        if self.spec.contract_sha256 == "":
            raise FixWorkflowError("fix phase requires a compiled contract")
        for value, label in (
            (self.lane_id, "fix phase lane_id"),
            (self.run_id, "fix phase run_id"),
        ):
            _identifier(value, label)


@dataclass(frozen=True)
class FixProgress:
    action: str
    step_id: str
    completed_steps: tuple[str, ...]
    prior_receipt: FixStepReceipt | None = None

    def __post_init__(self) -> None:
        if self.action not in {"start", "attention", "complete"}:
            raise FixWorkflowError("fix progress action is invalid")
        if self.action == "complete":
            if self.step_id:
                raise FixWorkflowError("complete fix progress cannot name a step")
        elif self.step_id not in FIX_PHASES:
            raise FixWorkflowError("fix progress step is invalid")


def _input_sha256(
    *,
    definition_sha256: str,
    step_id: str,
    iteration: int,
    input_schema: str,
    input_head_sha: str,
    approved_plan_sha256: str,
    prior_receipt_sha256: str,
) -> str:
    return _digest(
        {
            "schema_version": 1,
            "definition_sha256": _sha256(
                definition_sha256, "definition_sha256"
            ),
            "step_id": step_id,
            "iteration": iteration,
            "input_schema": input_schema,
            "input_head_sha": _git_oid(input_head_sha, "input_head_sha"),
            "approved_plan_sha256": (
                _sha256(approved_plan_sha256, "approved_plan_sha256")
                if not prior_receipt_sha256
                else ""
            ),
            "prior_receipt_sha256": (
                _sha256(prior_receipt_sha256, "prior_receipt_sha256")
                if prior_receipt_sha256
                else ""
            ),
        }
    )


def _round_identity(
    parent: OperationRecord,
    *,
    definition_sha256: str,
    step_id: str,
    iteration: int,
    input_sha256: str,
    input_head_sha: str,
    prior_receipt_sha256: str,
) -> FixPhaseRound:
    input_schema, output_schema = PHASE_SCHEMAS[step_id]
    short = {
        "reproduce": "repro",
        "root-cause": "cause",
        "regression-test": "test",
        "minimal-fix": "fix",
    }[step_id]
    suffix = f"-{short}-{iteration}-{input_sha256[:12]}"
    operation_id = (
        f"{parent.spec.operation_id[: 128 - len(suffix)]}{suffix}"
    )
    idempotency_key = hashlib.sha256(
        (
            f"{parent.spec.idempotency_key}:pipeline-model-step:"
            f"{operation_id}:{definition_sha256}:{step_id}:{iteration}:"
            f"{input_sha256}:{input_schema}:{output_schema}"
        ).encode()
    ).hexdigest()
    spec = OperationSpec(
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
    return FixPhaseRound(
        spec=spec,
        parent_operation_id=parent.spec.operation_id,
        lane_id=parent.lane_id,
        run_id=hashlib.sha256(
            f"{idempotency_key}:run".encode()
        ).hexdigest()[:32],
        step_id=step_id,
        iteration=iteration,
        input_schema=input_schema,
        input_sha256=input_sha256,
        input_head_sha=input_head_sha,
        prior_receipt_sha256=prior_receipt_sha256,
        output_schema=output_schema,
    )


def _expected_round(
    parent: OperationRecord,
    *,
    definition_sha256: str,
    approved_plan_sha256: str,
    initial_head_sha: str,
    step_id: str,
    iteration: int,
    prior_receipt: FixStepReceipt | None,
) -> FixPhaseRound:
    input_schema, _output_schema = PHASE_SCHEMAS[step_id]
    if prior_receipt is None:
        if step_id != "reproduce":
            raise FixWorkflowError("fix receipts are not an ordered prefix")
        input_head_sha = initial_head_sha
        prior_sha256 = ""
    else:
        expected_index = FIX_PHASES.index(prior_receipt.step_id) + 1
        if (
            expected_index >= len(FIX_PHASES)
            or FIX_PHASES[expected_index] != step_id
            or prior_receipt.status != "complete"
            or prior_receipt.output_schema != input_schema
        ):
            raise FixWorkflowError("fix receipts are not an ordered prefix")
        input_head_sha = prior_receipt.head_sha
        prior_sha256 = prior_receipt.receipt_sha256
    input_sha256 = _input_sha256(
        definition_sha256=definition_sha256,
        step_id=step_id,
        iteration=iteration,
        input_schema=input_schema,
        input_head_sha=input_head_sha,
        approved_plan_sha256=approved_plan_sha256,
        prior_receipt_sha256=prior_sha256,
    )
    return _round_identity(
        parent,
        definition_sha256=definition_sha256,
        step_id=step_id,
        iteration=iteration,
        input_sha256=input_sha256,
        input_head_sha=input_head_sha,
        prior_receipt_sha256=prior_sha256,
    )


def reconcile_fix(
    parent: OperationRecord,
    *,
    definition_sha256: str,
    approved_plan_sha256: str,
    initial_head_sha: str,
    receipts: Sequence[FixStepReceipt],
    iteration: int,
) -> FixProgress:
    """Reconstruct the next fixed phase solely from exact accepted receipts."""

    _sha256(definition_sha256, "definition_sha256")
    _sha256(approved_plan_sha256, "approved_plan_sha256")
    _git_oid(initial_head_sha, "initial_head_sha")
    if parent.spec.contract_sha256 != definition_sha256:
        raise FixWorkflowError(
            "fix definition does not match the immutable parent contract"
        )
    if (
        not isinstance(iteration, int)
        or isinstance(iteration, bool)
        or iteration < 0
    ):
        raise FixWorkflowError("fix iteration must be non-negative")
    if len(receipts) > len(FIX_PHASES):
        raise FixWorkflowError("fix receipts exceed the fixed phase count")

    prior: FixStepReceipt | None = None
    for index, receipt in enumerate(receipts):
        expected_step = FIX_PHASES[index]
        if receipt.step_id != expected_step:
            raise FixWorkflowError("fix receipts are not an ordered prefix")
        expected = _expected_round(
            parent,
            definition_sha256=definition_sha256,
            approved_plan_sha256=approved_plan_sha256,
            initial_head_sha=initial_head_sha,
            step_id=expected_step,
            iteration=iteration,
            prior_receipt=prior,
        )
        if receipt.definition_sha256 != definition_sha256:
            raise FixWorkflowError("fix receipt definition changed")
        if receipt.parent_operation_id != parent.spec.operation_id:
            raise FixWorkflowError("fix receipt parent identity changed")
        if receipt.iteration != iteration:
            raise FixWorkflowError("fix receipt iteration changed")
        if (
            receipt.operation_id != expected.spec.operation_id
            or receipt.lane_id != expected.lane_id
            or receipt.run_id != expected.run_id
            or receipt.input_schema != expected.input_schema
            or receipt.input_sha256 != expected.input_sha256
            or receipt.input_head_sha != expected.input_head_sha
            or receipt.prior_receipt_sha256
            != expected.prior_receipt_sha256
            or receipt.output_schema != expected.output_schema
        ):
            raise FixWorkflowError("fix receipt replay identity changed")
        if receipt.status == "cannot-reproduce":
            if index != len(receipts) - 1:
                raise FixWorkflowError(
                    "cannot-reproduce must be the final observed receipt"
                )
            return FixProgress(
                "attention",
                "reproduce",
                tuple(item.step_id for item in receipts),
                receipt,
            )
        prior = receipt

    completed = tuple(item.step_id for item in receipts)
    if len(receipts) == len(FIX_PHASES):
        return FixProgress("complete", "", completed, prior)
    return FixProgress("start", FIX_PHASES[len(receipts)], completed, prior)


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
    try:
        record = store.create(
            round_.spec,
            lane_id=round_.lane_id,
            run_id=round_.run_id,
        )
        if record.state == "created":
            for state in ("preflight", "starting", "running", "awaiting-callback"):
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
