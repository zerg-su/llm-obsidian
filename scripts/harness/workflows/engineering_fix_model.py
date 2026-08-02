"""Pure receipts, identities, and progress model for engineering/fix."""

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
RETRY_PHASES = FIX_PHASES[1:]
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
    "verification_sha256",
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
    verification_sha256: str
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
        if self.iteration == 0:
            if self.verification_sha256:
                raise FixWorkflowError(
                    "initial receipt verification_sha256 must be empty"
                )
        else:
            _sha256(
                self.verification_sha256,
                "receipt verification_sha256",
            )
            if self.step_id == "reproduce":
                raise FixWorkflowError(
                    "retry receipt cannot repeat the reproduce phase"
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
    verification_sha256: str
    output_schema: str

    def __post_init__(self) -> None:
        if self.spec.kind != "pipeline-model-step":
            raise FixWorkflowError("fix phase must use a model-step operation")
        _identifier(self.parent_operation_id, "fix phase parent_operation_id")
        if self.step_id not in FIX_PHASES:
            raise FixWorkflowError("fix phase is unknown")
        if (
            not isinstance(self.iteration, int)
            or isinstance(self.iteration, bool)
            or self.iteration < 0
        ):
            raise FixWorkflowError(
                "fix phase iteration must be non-negative"
            )
        if self.spec.contract_sha256 == "":
            raise FixWorkflowError("fix phase requires a compiled contract")
        for value, label in (
            (self.lane_id, "fix phase lane_id"),
            (self.run_id, "fix phase run_id"),
        ):
            _identifier(value, label)
        if self.iteration == 0:
            if self.verification_sha256:
                raise FixWorkflowError(
                    "initial phase verification_sha256 must be empty"
                )
        else:
            _sha256(
                self.verification_sha256,
                "fix phase verification_sha256",
            )
            if self.step_id == "reproduce":
                raise FixWorkflowError(
                    "retry phase cannot repeat the reproduce phase"
                )


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
    verification_sha256: str,
    failed_head_sha: str,
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
            "verification_sha256": (
                _sha256(verification_sha256, "verification_sha256")
                if verification_sha256
                else ""
            ),
            "failed_head_sha": (
                _git_oid(failed_head_sha, "failed_head_sha")
                if failed_head_sha
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
    verification_sha256: str,
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
        verification_sha256=verification_sha256,
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
        verification_sha256="",
        failed_head_sha="",
    )
    return _round_identity(
        parent,
        definition_sha256=definition_sha256,
        step_id=step_id,
        iteration=iteration,
        input_sha256=input_sha256,
        input_head_sha=input_head_sha,
        prior_receipt_sha256=prior_sha256,
        verification_sha256="",
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
    if iteration != 0 or isinstance(iteration, bool):
        raise FixWorkflowError("initial iteration must be zero")
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
            or receipt.verification_sha256
            != expected.verification_sha256
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


def _validate_retry_context(
    parent: OperationRecord,
    *,
    definition_sha256: str,
    reproduction_receipt: FixStepReceipt,
    verification_sha256: str,
    failed_head_sha: str,
    current_head_sha: str,
    iteration: int,
) -> None:
    _sha256(definition_sha256, "definition_sha256")
    _sha256(verification_sha256, "verification_sha256")
    _git_oid(failed_head_sha, "failed_head_sha")
    _git_oid(current_head_sha, "current_head_sha")
    if parent.spec.contract_sha256 != definition_sha256:
        raise FixWorkflowError(
            "fix definition does not match the immutable parent contract"
        )
    if (
        not isinstance(iteration, int)
        or isinstance(iteration, bool)
        or iteration < 1
    ):
        raise FixWorkflowError("retry iteration must be at least one")
    if (
        reproduction_receipt.step_id != "reproduce"
        or reproduction_receipt.iteration != 0
        or reproduction_receipt.status != "complete"
        or reproduction_receipt.output_schema != "reproduction/v1"
        or reproduction_receipt.verification_sha256
        or reproduction_receipt.definition_sha256 != definition_sha256
        or reproduction_receipt.parent_operation_id
        != parent.spec.operation_id
        or reproduction_receipt.lane_id != parent.lane_id
    ):
        raise FixWorkflowError(
            "retry requires the original accepted reproduction receipt"
        )


def _expected_retry_round(
    parent: OperationRecord,
    *,
    definition_sha256: str,
    reproduction_receipt: FixStepReceipt,
    verification_sha256: str,
    failed_head_sha: str,
    current_head_sha: str,
    step_id: str,
    iteration: int,
    prior_receipt: FixStepReceipt | None,
) -> FixPhaseRound:
    input_schema, _output_schema = PHASE_SCHEMAS[step_id]
    if prior_receipt is None:
        if step_id != RETRY_PHASES[0]:
            raise FixWorkflowError(
                "fix retry receipts are not an ordered prefix"
            )
        input_head_sha = current_head_sha
        prior_sha256 = reproduction_receipt.receipt_sha256
    else:
        expected_index = RETRY_PHASES.index(prior_receipt.step_id) + 1
        if (
            expected_index >= len(RETRY_PHASES)
            or RETRY_PHASES[expected_index] != step_id
            or prior_receipt.status != "complete"
            or prior_receipt.output_schema != input_schema
        ):
            raise FixWorkflowError(
                "fix retry receipts are not an ordered prefix"
            )
        input_head_sha = prior_receipt.head_sha
        prior_sha256 = prior_receipt.receipt_sha256
    input_sha256 = _input_sha256(
        definition_sha256=definition_sha256,
        step_id=step_id,
        iteration=iteration,
        input_schema=input_schema,
        input_head_sha=input_head_sha,
        approved_plan_sha256="",
        prior_receipt_sha256=prior_sha256,
        verification_sha256=verification_sha256,
        failed_head_sha=failed_head_sha,
    )
    return _round_identity(
        parent,
        definition_sha256=definition_sha256,
        step_id=step_id,
        iteration=iteration,
        input_sha256=input_sha256,
        input_head_sha=input_head_sha,
        prior_receipt_sha256=prior_sha256,
        verification_sha256=verification_sha256,
    )


def reconcile_retry_fix(
    parent: OperationRecord,
    *,
    definition_sha256: str,
    reproduction_receipt: FixStepReceipt,
    verification_sha256: str,
    failed_head_sha: str,
    current_head_sha: str,
    receipts: Sequence[FixStepReceipt],
    iteration: int,
) -> FixProgress:
    """Reconstruct one bounded post-verification retry from exact receipts."""

    _validate_retry_context(
        parent,
        definition_sha256=definition_sha256,
        reproduction_receipt=reproduction_receipt,
        verification_sha256=verification_sha256,
        failed_head_sha=failed_head_sha,
        current_head_sha=current_head_sha,
        iteration=iteration,
    )
    if len(receipts) > len(RETRY_PHASES):
        raise FixWorkflowError(
            "fix retry receipts exceed the bounded phase count"
        )

    prior: FixStepReceipt | None = None
    for index, receipt in enumerate(receipts):
        expected_step = RETRY_PHASES[index]
        if receipt.step_id != expected_step:
            raise FixWorkflowError(
                "fix retry receipts are not an ordered prefix"
            )
        expected = _expected_retry_round(
            parent,
            definition_sha256=definition_sha256,
            reproduction_receipt=reproduction_receipt,
            verification_sha256=verification_sha256,
            failed_head_sha=failed_head_sha,
            current_head_sha=current_head_sha,
            step_id=expected_step,
            iteration=iteration,
            prior_receipt=prior,
        )
        if (
            receipt.definition_sha256 != definition_sha256
            or receipt.parent_operation_id != parent.spec.operation_id
            or receipt.iteration != iteration
            or receipt.operation_id != expected.spec.operation_id
            or receipt.lane_id != expected.lane_id
            or receipt.run_id != expected.run_id
            or receipt.input_schema != expected.input_schema
            or receipt.input_sha256 != expected.input_sha256
            or receipt.input_head_sha != expected.input_head_sha
            or receipt.prior_receipt_sha256
            != expected.prior_receipt_sha256
            or receipt.verification_sha256
            != expected.verification_sha256
            or receipt.output_schema != expected.output_schema
        ):
            raise FixWorkflowError(
                "fix retry receipt replay identity changed"
            )
        prior = receipt

    completed = tuple(item.step_id for item in receipts)
    if len(receipts) == len(RETRY_PHASES):
        return FixProgress("complete", "", completed, prior)
    return FixProgress(
        "start", RETRY_PHASES[len(receipts)], completed, prior
    )



