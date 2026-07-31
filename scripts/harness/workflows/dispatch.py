"""Dispatch policy facade over OperationSpec and automatic simple review."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, TypeVar

from ..contracts import (
    EffectOutcome,
    OperationRecord,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from ..custom_pipelines import FrozenCustomPipeline, FrozenPipelineStore
from ..pipeline_builtins import EXECUTABLE_BUILTINS, compiled_builtin
from ..state_machine import TERMINAL
from ..store import OperationStore
from ..supervisor import OperationSupervisor, SupervisorError
from ..runtime_sessions import (
    RuntimeSessionManager,
    RuntimeSessionRequest,
    RuntimeSessionResult,
)
from .engineering_fix import FixPhaseRound, prepare_next_phase
from .custom_sequence import (
    CustomStepRound,
    custom_step_request,
    prepare_custom_step,
)


T = TypeVar("T", bound=Mapping[str, object])


def _store(value: OperationStore | Path) -> OperationStore:
    return value if isinstance(value, OperationStore) else OperationStore(value)


@dataclass(frozen=True)
class ReviewPolicy:
    depth: str = "simple"
    cross_model: bool = False
    enabled: bool = True
    runtime: str = ""
    model: str = ""
    effort: str = ""
    verification_profile: str = ""
    verification_profile_sha256: str = ""

    def __post_init__(self) -> None:
        if self.depth not in {"simple", "deep"}:
            raise ValueError("review depth must be simple or deep")
        if self.runtime and self.runtime not in {"claude", "codex"}:
            raise ValueError("review runtime must be claude or codex")
        if self.model and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.model
        ):
            raise ValueError("review model must be a bounded alias")
        if self.effort and self.effort not in {
            "minimal", "low", "medium", "high", "xhigh", "max"
        }:
            raise ValueError("review effort is invalid")
        if bool(self.verification_profile) != bool(
            self.verification_profile_sha256
        ):
            raise ValueError(
                "review verification profile and digest must be bound together"
            )
        if self.verification_profile and (
            not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
                self.verification_profile,
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}", self.verification_profile_sha256
            )
        ):
            raise ValueError("review verification profile binding is invalid")
        if not self.enabled and any(
            (
                self.depth != "simple",
                self.cross_model,
                self.runtime,
                self.model,
                self.effort,
            )
        ):
            raise ValueError("disabled review cannot carry review overrides")

    @property
    def mode(self) -> str:
        return self.depth if self.enabled else "skip"

    @property
    def max_verify_iterations(self) -> int:
        return {"simple": 1, "deep": 2, "skip": 0}[self.mode]


@dataclass(frozen=True)
class DispatchRequest:
    task_id: str
    owner_id: str
    plan_sha256: str
    context_manifest: str
    route: RuntimeRoute
    placement: str = "split"
    review: ReviewPolicy = ReviewPolicy()
    pipeline_name: str = "lifecycle/default"
    completion_policy: str = "attention"
    custom_pipeline: FrozenCustomPipeline | None = None

    def __post_init__(self) -> None:
        if self.placement not in {"split", "workspace"}:
            raise ValueError("dispatch placement must be split or workspace")
        if len(self.plan_sha256) != 64:
            raise ValueError("dispatch requires an approved plan sha256")
        if self.pipeline_name == "custom":
            if self.custom_pipeline is None:
                raise ValueError("custom dispatch requires an approved pipeline")
            baseline = self.custom_pipeline.spec.baseline_pipeline
            if baseline not in EXECUTABLE_BUILTINS:
                raise ValueError("custom pipeline baseline is not executable")
            if self.review.mode != self.custom_pipeline.spec.review_mode:
                raise ValueError("custom review policy differs from approval")
            if self.completion_policy != self.custom_pipeline.spec.completion_policy:
                raise ValueError("custom completion policy differs from approval")
        elif self.pipeline_name not in EXECUTABLE_BUILTINS:
            raise ValueError("dispatch requires an executable pipeline")
        elif self.custom_pipeline is not None:
            raise ValueError("built-in dispatch cannot carry a custom pipeline")
        if self.completion_policy not in {"attention", "autonomous"}:
            raise ValueError("dispatch completion policy is invalid")
        custom_has_loop = (
            self.custom_pipeline is not None
            and any(
                item.primitive_id == "bounded_loop"
                for item in self.custom_pipeline.spec.controls
            )
        )
        if (
            self.pipeline_name != "engineering/fix"
            and not custom_has_loop
            and self.completion_policy != "attention"
        ):
            raise ValueError(
                "autonomous completion is supported only by engineering/fix"
            )


def _compiled_contract(request: DispatchRequest):
    if request.custom_pipeline is not None:
        return request.custom_pipeline.compiled
    return compiled_builtin(request.pipeline_name)


def operation_spec(request: DispatchRequest) -> OperationSpec:
    contract = _compiled_contract(request)
    identity = json.dumps(
        {
            "task_id": request.task_id,
            "owner_id": request.owner_id,
            "plan_sha256": request.plan_sha256,
            "context_manifest": request.context_manifest,
            "route": {
                "runtime": request.route.runtime,
                "model": request.route.model,
                "effort": request.route.effort,
                "profile": request.route.profile,
                "routing_sha256": request.route.routing_sha256,
            },
            "placement": request.placement,
            "contract_sha256": contract.definition_sha256,
            "custom_spec_sha256": (
                request.custom_pipeline.spec_sha256
                if request.custom_pipeline is not None
                else ""
            ),
            "completion_policy": request.completion_policy,
            "review": {
                "mode": request.review.mode,
                "cross_model": request.review.cross_model,
                "runtime": request.review.runtime,
                "model": request.review.model,
                "effort": request.review.effort,
                "max_verify_iterations": (
                    request.review.max_verify_iterations
                ),
                "verification_profile": (
                    request.review.verification_profile
                ),
                "verification_profile_sha256": (
                    request.review.verification_profile_sha256
                ),
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return OperationSpec(
        operation_id=request.task_id,
        idempotency_key=hashlib.sha256(identity.encode()).hexdigest(),
        kind="dispatch",
        owner_id=request.owner_id,
        route=request.route,
        context_manifest=request.context_manifest,
        verification_profile=(
            request.review.verification_profile or "full"
        ),
        contract_sha256=contract.definition_sha256,
    )


@dataclass(frozen=True)
class DispatchRun:
    record: OperationRecord
    result: Mapping[str, object] | None


def _assignment(spec: OperationSpec, role: str) -> str:
    try:
        namespace = uuid.UUID(spec.operation_id)
    except ValueError:
        namespace = uuid.NAMESPACE_URL
    return str(uuid.uuid5(namespace, f"{role}:{spec.idempotency_key}"))


def _fix_phase_request(round_: FixPhaseRound) -> dict[str, object]:
    base = f".task-pipeline/pass-{round_.iteration + 1}/{round_.step_id}"
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
        "result_pointer": f"{base}-result.json",
        "output_pointer": f"{base}-output.md",
    }


def _write_pipeline_step_request(
    cwd: Path,
    round_: FixPhaseRound | CustomStepRound,
) -> None:
    path = cwd / ".task-pipeline-step-request.json"
    request = (
        custom_step_request(round_)
        if isinstance(round_, CustomStepRound)
        else _fix_phase_request(round_)
    )
    payload = (
        json.dumps(
            request,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise ValueError("fix phase request must be a regular file")
        if path.read_text(encoding="utf-8") != payload:
            raise ValueError("fix phase request changed during replay")
        return
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def start_dispatch(
    request: DispatchRequest,
    runtime: RuntimeSessionManager,
    *,
    origin_surface: str,
    cwd: Path,
    prompt_pointer: str = ".task-prompt.md",
    summary_pointer: str = ".task-summary.json",
    initial_head_sha: str = "",
    on_surface_opened: Callable[[RuntimeSessionResult], None] | None = None,
) -> RuntimeSessionResult:
    """Start one dispatch through the generic provider runtime.

    The provider worker validates and relays the canonical task summary; the
    executor model never owns coordinator wake-up or cmux lifecycle mechanics.
    """

    spec = operation_spec(request)
    contract = _compiled_contract(request)
    budget = contract.worst_case_budget
    baseline_name = (
        request.custom_pipeline.spec.baseline_pipeline
        if request.custom_pipeline is not None
        else request.pipeline_name
    )
    if baseline_name == "engineering/fix" and request.custom_pipeline is None:
        total_pass_limit = {
            item.policy: item.total_pass_limit
            for item in contract.definition.completion_policies
        }[request.completion_policy]
        budget = contract.definition.pass_budget.scaled(total_pass_limit)
    lane_id = _assignment(spec, "dispatch-lane")
    run_id = _assignment(spec, "dispatch-run")
    callback_pointer = summary_pointer
    initial_callback_operation_id = ""
    initial_callback_run_id = ""
    if request.custom_pipeline is not None:
        FrozenPipelineStore(
            runtime.store.root
            / "owners"
            / request.owner_id
            / "runtime"
        ).save(
            operation_id=request.task_id,
            spec=request.custom_pipeline.spec,
            frozen=request.custom_pipeline,
            approval=request.custom_pipeline.approval,
        )
    if baseline_name == "engineering/fix" and request.custom_pipeline is None:
        if not re.fullmatch(r"[0-9a-f]{40,64}", initial_head_sha):
            raise ValueError("engineering/fix requires the initial Git HEAD")
        parent = runtime.store.create(
            spec,
            lane_id=lane_id,
            run_id=run_id,
        )
        round_ = prepare_next_phase(
            runtime.store,
            parent,
            definition_sha256=spec.contract_sha256,
            approved_plan_sha256=request.plan_sha256,
            initial_head_sha=initial_head_sha,
            receipts=(),
            iteration=0,
        )
        _write_pipeline_step_request(cwd, round_)
        callback_pointer = ".task-pipeline-step-callback.json"
        initial_callback_operation_id = round_.spec.operation_id
        initial_callback_run_id = round_.run_id
    elif request.custom_pipeline is not None:
        if not re.fullmatch(r"[0-9a-f]{40,64}", initial_head_sha):
            raise ValueError("custom pipeline requires the initial Git HEAD")
        parent = runtime.store.create(spec, lane_id=lane_id, run_id=run_id)
        round_ = prepare_custom_step(
            runtime.store,
            parent,
            request.custom_pipeline.spec,
            definition_sha256=spec.contract_sha256,
            approved_plan_sha256=request.plan_sha256,
            initial_head_sha=initial_head_sha,
            receipts=(),
        )
        _write_pipeline_step_request(cwd, round_)
        callback_pointer = ".task-pipeline-step-callback.json"
        initial_callback_operation_id = round_.spec.operation_id
        initial_callback_run_id = round_.run_id
    return runtime.start(
        RuntimeSessionRequest(
            spec=spec,
            lane_id=lane_id,
            run_id=run_id,
            origin_surface=origin_surface,
            cwd=cwd,
            prompt_pointer=prompt_pointer,
            callback_pointer=callback_pointer,
            placement=request.placement,
            product_root=cwd,
            callback_mode="task-summary",
            task_summary_pointer=summary_pointer,
            initial_callback_operation_id=initial_callback_operation_id,
            initial_callback_run_id=initial_callback_run_id,
            attempt_limit=budget.attempt_limit,
            model_restart_limit=budget.model_restart_limit,
            time_budget_seconds=budget.time_budget_seconds,
            token_limit=budget.token_limit,
        ),
        on_surface_opened=on_surface_opened,
    )


def run_dispatch(
    request: DispatchRequest,
    store: OperationStore | Path,
    *,
    launch: Callable[[OperationRecord], T],
    persist_result: Callable[[OperationRecord, T], None],
) -> DispatchRun:
    """Persist and launch one dispatch through the restartable harness seam."""

    store = _store(store)
    spec = operation_spec(request)
    record = store.create(
        spec,
        lane_id=_assignment(spec, "dispatch-lane"),
        run_id=_assignment(spec, "dispatch-run"),
    )
    supervisor = OperationSupervisor(
        store, request.owner_id, request.task_id
    )
    if record.state in TERMINAL or record.state == "awaiting-callback":
        return DispatchRun(record, None)
    if record.state == "created":
        record = supervisor.transition("preflight")
    if record.state == "preflight":
        record = supervisor.transition("starting")
    if record.state != "starting":
        raise SupervisorError(
            f"dispatch operation cannot launch from {record.state}"
        )
    effected = supervisor.effect(
        "dispatch-launch",
        launch,
        persist_result=persist_result,
    )
    result = effected.value
    if result is not None:
        surface_id = str(result.get("task_surface") or "")
        if surface_id:
            supervisor.bind_resources(OwnedResources(surface_id=surface_id))
    record = supervisor.transition("running")
    record = supervisor.transition("awaiting-callback")
    return DispatchRun(record, result)


def recover_launched(
    store: OperationStore | Path,
    *,
    owner_id: str,
    operation_id: str,
    run_id: str,
) -> OperationRecord:
    """Commit a launch whose exact result cache survived an interrupted return."""

    store = _store(store)
    supervisor = OperationSupervisor(store, owner_id, operation_id)
    record = supervisor.read()
    if record.run_id != run_id:
        raise SupervisorError("cached launch belongs to a different harness run")
    if record.state == "awaiting-callback" or record.state in TERMINAL:
        return record
    if record.state != "starting":
        raise SupervisorError(
            f"cached launch cannot reconcile operation from {record.state}"
        )
    if record.pending_effect == "dispatch-launch":
        store.resolve_effect(owner_id, operation_id, EffectOutcome.SUCCEEDED)
    elif not (
        record.effect_id == "dispatch-launch"
        and record.effect_outcome == EffectOutcome.SUCCEEDED
    ):
        raise SupervisorError("cached launch has no matching effect intent")
    supervisor.transition("running")
    return supervisor.transition("awaiting-callback")
