"""Minimal autonomous pipeline progress over existing operation ports."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Mapping

from .contracts import ContractError, RuntimeRoute
from .pipeline_state import (
    PipelineControllerState,
    PipelineLedger,
    StepReceipt,
)
from .pipelines import (
    CompiledPipeline,
    PipelineOperationBinding,
    PipelineStep,
    _identifier,
    _sha256,
    bind_step_operation,
)


class PipelineControllerError(RuntimeError):
    pass


@dataclass(frozen=True)
class PipelineRunRequest:
    owner_id: str
    pipeline_run_id: str
    approved_input_sha256: str
    context_manifest: str
    verification_profile: str
    routes: Mapping[str, RuntimeRoute]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported PipelineRunRequest schema")
        _identifier(self.owner_id, "pipeline owner_id")
        _identifier(self.pipeline_run_id, "pipeline_run_id")
        _sha256(self.approved_input_sha256, "approved pipeline input")
        if not isinstance(self.routes, Mapping):
            raise ContractError("pipeline routes must be a mapping")
        routes = dict(self.routes)
        if not routes or any(
            not isinstance(route, RuntimeRoute) for route in routes.values()
        ):
            raise ContractError("pipeline routes must contain RuntimeRoute values")
        object.__setattr__(self, "routes", MappingProxyType(routes))


@dataclass(frozen=True)
class PipelineStepResult:
    output_sha256: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported PipelineStepResult schema")
        _sha256(self.output_sha256, "pipeline step output")


@dataclass(frozen=True)
class PipelineRun:
    state: PipelineControllerState
    bindings: tuple[PipelineOperationBinding, ...]
    receipts: tuple[StepReceipt, ...]


PipelineExecutor = Callable[
    [PipelineOperationBinding, PipelineStep],
    PipelineStepResult,
]


def _operation_id(pipeline_run_id: str, step_id: str) -> str:
    suffix = f"-{step_id}-{hashlib.sha256(step_id.encode()).hexdigest()[:8]}"
    return f"{pipeline_run_id[: 128 - len(suffix)]}{suffix}"


def run_pipeline(
    compiled: CompiledPipeline,
    ledger: PipelineLedger,
    request: PipelineRunRequest,
    *,
    execute: PipelineExecutor,
) -> PipelineRun:
    """Drive sequential semantic steps; primitive ports own every effect."""

    state = ledger.start(
        request.owner_id,
        request.pipeline_run_id,
        compiled,
    )
    input_sha256 = request.approved_input_sha256
    bindings: list[PipelineOperationBinding] = []
    receipts: list[StepReceipt] = []
    semantic_steps = tuple(
        step
        for step in compiled.definition.steps
        if step.session_mode != "controller"
    )
    for step in semantic_steps:
        route = request.routes.get(step.session_mode)
        if route is None:
            raise PipelineControllerError(
                f"no route for pipeline session mode: {step.session_mode}"
            )
        binding = bind_step_operation(
            compiled,
            step_id=step.step_id,
            operation_id=_operation_id(
                request.pipeline_run_id,
                step.step_id,
            ),
            owner_id=request.owner_id,
            route=route,
            context_manifest=request.context_manifest,
            verification_profile=request.verification_profile,
            input_sha256=input_sha256,
        )
        existing = ledger.lookup(
            request.owner_id,
            request.pipeline_run_id,
            binding.replay_key,
        )
        if existing is None and step.step_id in state.completed_steps:
            raise PipelineControllerError(
                f"completed pipeline step has no exact receipt: {step.step_id}"
            )
        if existing is None:
            result = execute(binding, step)
            if not isinstance(result, PipelineStepResult):
                raise PipelineControllerError(
                    "pipeline executor returned no typed step result"
                )
            state, receipt = ledger.accept(
                request.owner_id,
                request.pipeline_run_id,
                binding,
                output_sha256=result.output_sha256,
            )
        else:
            state, receipt = ledger.accept(
                request.owner_id,
                request.pipeline_run_id,
                binding,
                output_sha256=existing.output_sha256,
            )
        bindings.append(binding)
        receipts.append(receipt)
        input_sha256 = receipt.output_sha256
    return PipelineRun(state, tuple(bindings), tuple(receipts))
