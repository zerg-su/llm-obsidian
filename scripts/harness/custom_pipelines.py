"""Strict model-authored pipeline specs compiled by the 2.4 harness compiler.

This module accepts data, never source code.  Parsing, policy comparison, and
canonical hashing are code-owned and happen before any runtime effect.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractError, ID_RE, SHA256_RE, to_dict
from .context import OUTCOME_POINTER_ID
from .pipelines import (
    PERMISSION_BINDINGS,
    SIDE_EFFECT_BINDINGS,
    CompletionPolicy,
    CompiledPipeline,
    PipelineBudget,
    PipelineDefinition,
    PipelineStep,
    PrimitiveReference,
    PrimitiveRegistry,
    SCHEMA_RE,
    SEMVER_RE,
    compile_pipeline,
)

from .custom_pipeline_contracts import (
    BASELINES,
    CONTEXT_FIELDS,
    CONTROL_FIELDS,
    CUSTOM_COMPILER_VERSION,
    CUSTOM_SPEC_VERSION,
    ENFORCEABLE_CUSTOM_PERMISSIONS,
    ENFORCEABLE_CUSTOM_SIDE_EFFECTS,
    REVIEW_MODES,
    STEP_FIELDS,
    TOP_FIELDS,
    TRANSITION_FIELDS,
    VERIFICATION_CHECKS,
    BUDGET_FIELDS,
    ContextPointer,
    CustomPipelinePolicy,
    ExplicitPipelineApproval,
    FrozenCustomPipeline,
    FrozenPipelineStore,
    PipelineSpec,
    PipelineTransition,
    _approval_receipt_sha256,
    _identifier,
    parse_pipeline_spec,
    pipeline_spec_payload,
    resolve_custom_executable,
)

def select_builtin_baseline(intent: str, task_profile: str) -> str:
    """Choose the comparison baseline without trusting model output."""

    _identifier(intent, "intent")
    _identifier(task_profile, "task_profile")
    return BASELINES.get(task_profile, "lifecycle/default")


def render_authoring_contract(*, intent: str, task_profile: str) -> str:
    """Bounded prompt fragment for one model proposal step."""

    baseline = select_builtin_baseline(intent, task_profile)
    return "\n".join(
        (
            "Return exactly one JSON object matching schemas/pipeline-spec-v1.schema.json.",
            f"Intent: {intent}; task profile: {task_profile}.",
            f"Baseline: {baseline}.",
            "Use route_alias=executor-default and only capabilities proven by the harness.",
            "Recommend the built-in when it satisfies the task; propose custom only for a semantic gap.",
            "Use only registered primitive ids, bounded identifiers, and declared context hashes.",
            "Do not emit shell, Python, filesystem paths, credentials, dependencies, or plugins.",
            "The harness validates, renders, and requires explicit user approval before effects.",
        )
    ) + "\n"


def _built_in_semantically_fits(spec: PipelineSpec) -> bool:
    from .pipeline_builtins import compiled_builtin

    baseline = compiled_builtin(spec.baseline_pipeline).definition
    def semantics(step: PipelineStep) -> tuple[object, ...]:
        return (
            step.primitive_id,
            step.primitive_version,
            step.input_schema,
            step.output_schema,
            step.session_mode,
            step.semantic_skills,
        )

    return (
        spec.input_schema == baseline.input_schema
        and spec.output_schema == baseline.output_schema
        and tuple(map(semantics, spec.steps))
        == tuple(map(semantics, baseline.steps))
        and spec.controls == baseline.control_primitives
        and spec.completion_policy
        in {item.policy for item in baseline.completion_policies}
        and not spec.verification_checks
        and spec.review_mode == "simple"
    )


def _budget_within(value: PipelineBudget, ceiling: PipelineBudget) -> bool:
    return all(
        left <= right
        for left, right in (
            (value.attempt_limit, ceiling.attempt_limit),
            (value.model_restart_limit, ceiling.model_restart_limit),
            (value.time_budget_seconds, ceiling.time_budget_seconds),
            (value.token_limit, ceiling.token_limit),
        )
    )


def _flow_bounds(spec: PipelineSpec) -> tuple[int, int]:
    """Return conservative static bounds without exploring task-authored graphs."""

    step_by_id = {step.step_id: step for step in spec.steps}
    maximum_nodes = 1 + sum(
        item.max_traversals
        for item in spec.transitions
        if not item.target.startswith("terminal:")
    )
    maximum_models = int(spec.steps[0].primitive_id == "model_step") + sum(
        item.max_traversals
        for item in spec.transitions
        if not item.target.startswith("terminal:")
        and step_by_id[item.target].primitive_id == "model_step"
    )
    return maximum_nodes, maximum_models


def _validate_custom_flow(spec: PipelineSpec, policy: CustomPipelinePolicy) -> None:
    """Validate the deliberately small sequential/decision grammar for v2.5."""

    steps = spec.steps
    step_by_id = {step.step_id: step for step in steps}
    positions = {step.step_id: index for index, step in enumerate(steps)}
    primitives = tuple(step.primitive_id for step in steps)
    model_count = sum(item == "model_step" for item in primitives)
    if (
        model_count < 1
        or primitives[-1] != "review"
        or primitives.count("review") != 1
        or primitives.count("verify") > 1
        or any(
            primitive not in {"model_step", "verify", "review"}
            for primitive in primitives
        )
        or any(
            primitive != "model_step"
            for primitive in primitives[:model_count]
        )
        or tuple(primitives[model_count:])
        not in {("review",), ("verify", "review")}
    ):
        raise ContractError(
            "custom execution requires model steps followed by optional verify and final review"
        )
    if model_count > policy.max_model_calls:
        raise ContractError("PipelineSpec exceeds the model-call limit")
    if len(spec.transitions) > policy.max_transitions:
        raise ContractError("PipelineSpec exceeds the transition limit")

    keyed: dict[tuple[str, str], PipelineTransition] = {}
    has_loop = False
    for transition in spec.transitions:
        key = (transition.from_step, transition.outcome)
        if key in keyed:
            raise ContractError("transition outcomes must be unique per step")
        keyed[key] = transition
        source = step_by_id.get(transition.from_step)
        if source is None:
            raise ContractError("transition source step is unknown")
        if transition.max_traversals > policy.max_loop_traversals:
            raise ContractError("transition exceeds the traversal limit")
        if transition.target.startswith("terminal:"):
            terminal = transition.target.removeprefix("terminal:")
            if terminal not in spec.terminal_outcomes:
                raise ContractError("transition terminal outcome is undeclared")
            if terminal == "completed" and source is not steps[-1]:
                raise ContractError("completed terminal is valid only after review")
            continue
        target = step_by_id.get(transition.target)
        if target is None:
            raise ContractError("transition target step is unknown")
        if source.output_schema != target.input_schema:
            raise ContractError("transition schemas are incompatible")
        if positions[target.step_id] <= positions[source.step_id]:
            has_loop = True
            if transition.max_traversals < 2:
                raise ContractError("backward transitions require a bounded retry")

    for index, step in enumerate(steps):
        complete = keyed.get((step.step_id, "complete"))
        expected = (
            f"terminal:completed"
            if index == len(steps) - 1
            else steps[index + 1].step_id
        )
        if complete is None or complete.target != expected:
            raise ContractError(
                "every step requires the exact linear complete transition"
            )
    control_ids = {item.primitive_id for item in spec.controls}
    if has_loop and "bounded_loop" not in control_ids:
        raise ContractError("backward transitions require bounded_loop control")
    if not has_loop and "bounded_loop" in control_ids:
        raise ContractError("bounded_loop control requires a backward transition")
    maximum_nodes, maximum_models = _flow_bounds(spec)
    if maximum_nodes > policy.max_steps * policy.max_loop_traversals:
        raise ContractError("PipelineSpec exceeds the bounded node-visit limit")
    if maximum_models > policy.max_model_calls:
        raise ContractError("PipelineSpec exceeds the worst-case model-call limit")


def compile_custom_spec(
    spec: PipelineSpec,
    registry: PrimitiveRegistry,
    *,
    policy: CustomPipelinePolicy,
    capabilities: tuple[str, ...] = (),
) -> CompiledPipeline:
    """Compile a model proposal without creating any operation or effect."""

    selected_baseline = select_builtin_baseline(spec.intent, spec.task_profile)
    if spec.baseline_pipeline != selected_baseline:
        raise ContractError(
            "PipelineSpec baseline differs from the deterministic baseline"
        )
    if spec.finalization_policy is not None:
        from .finalization_policy import (
            FinalizationPolicyError,
            require_registered_finalization_routes,
        )

        try:
            require_registered_finalization_routes(spec.finalization_policy)
        except FinalizationPolicyError as exc:
            raise ContractError(str(exc)) from exc
    if _built_in_semantically_fits(spec):
        raise ContractError("built-in already fits; custom pipeline is unnecessary")

    if len(spec.steps) > policy.max_steps:
        raise ContractError("PipelineSpec exceeds the code-owned step limit")
    if spec.route_alias not in policy.route_aliases:
        raise ContractError("PipelineSpec route alias is not code-owned")
    if not set(spec.required_capabilities).issubset(policy.capability_ceiling):
        raise ContractError("PipelineSpec exceeds the capability ceiling")
    if not set(spec.required_capabilities).issubset(capabilities):
        raise ContractError("PipelineSpec route lacks required capabilities")
    if len(spec.controls) > policy.max_controls:
        raise ContractError("PipelineSpec exceeds the code-owned control limit")
    if len(spec.context_pointers) > policy.max_context_pointers:
        raise ContractError("PipelineSpec exceeds the context pointer limit")
    if any(
        item.pointer_id == OUTCOME_POINTER_ID for item in spec.context_pointers
    ):
        raise ContractError(
            "PipelineSpec cannot override the reserved outcome-contract context pointer"
        )
    if len({item.pointer_id for item in spec.context_pointers}) != len(
        spec.context_pointers
    ):
        raise ContractError("PipelineSpec context pointer ids must be unique")
    if sum(item.byte_limit for item in spec.context_pointers) > policy.max_context_bytes:
        raise ContractError("PipelineSpec exceeds the context byte limit")
    if len(spec.verification_checks) > policy.max_verification_checks:
        raise ContractError("PipelineSpec exceeds the verification check limit")
    unknown_checks = set(spec.verification_checks) - set(VERIFICATION_CHECKS)
    if unknown_checks:
        raise ContractError(
            "PipelineSpec uses an unregistered verification check: "
            + ",".join(sorted(unknown_checks))
        )
    _validate_custom_flow(spec, policy)
    if not set(spec.requested_permissions).issubset(policy.permission_ceiling):
        raise ContractError("PipelineSpec exceeds the code-owned permission ceiling")
    if not set(spec.requested_side_effects).issubset(policy.side_effect_ceiling):
        raise ContractError("PipelineSpec exceeds the code-owned side-effect ceiling")

    policies = [CompletionPolicy("attention", 2)]
    if spec.completion_policy == "autonomous":
        policies.append(CompletionPolicy("autonomous", 3))
    effective_permissions = policy.permission_ceiling
    effective_side_effects = policy.side_effect_ceiling
    definition = PipelineDefinition(
        pipeline_id="custom",
        version=spec.version,
        profile=spec.task_profile,
        input_schema=spec.input_schema,
        output_schema=spec.output_schema,
        steps=spec.steps,
        control_primitives=spec.controls,
        pass_budget=spec.budget,
        # Custom execution uses the existing dispatch/runtime/review harness.
        # Its effective authority is code-owned and cannot be narrowed by a
        # model merely omitting fields from the proposal.
        permission_ceiling=effective_permissions,
        side_effects=effective_side_effects,
        completion_policies=tuple(policies),
    )
    compiled = compile_pipeline(definition, registry, capabilities=capabilities)
    if not _budget_within(compiled.worst_case_budget, policy.worst_case_budget):
        raise ContractError("PipelineSpec exceeds the code-owned budget ceiling")

    canonical = json.dumps(
        {
            "custom_compiler_version": CUSTOM_COMPILER_VERSION,
            "pipeline_spec": pipeline_spec_payload(spec),
            "compiled_pipeline": json.loads(compiled.canonical_definition),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return replace(
        compiled,
        canonical_definition=canonical,
        definition_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def freeze_custom_pipeline(
    spec: PipelineSpec,
    compiled: CompiledPipeline,
    approval: ExplicitPipelineApproval,
    approval_card: str,
) -> FrozenCustomPipeline:
    """Bind exact user consent to an immutable compiled definition."""

    if approval.actor not in {"user", "host-user-dialog"} or approval.decision != "approve":
        raise ContractError("only explicit user approval can freeze a pipeline")
    if approval.definition_sha256 != compiled.definition_sha256:
        raise ContractError("approval definition does not match compiled definition")
    card_sha256 = hashlib.sha256(approval_card.encode()).hexdigest()
    if approval.approval_card_sha256 != card_sha256:
        raise ContractError("approval card does not match the reviewed contract")
    spec_sha256 = hashlib.sha256(
        json.dumps(
            pipeline_spec_payload(spec),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return FrozenCustomPipeline(
        spec=spec,
        compiled=compiled,
        approval=approval,
        approval_card=approval_card,
        spec_sha256=spec_sha256,
        approval_sha256=approval.approval_card_sha256,
        approval_receipt_sha256=_approval_receipt_sha256(approval),
    )


def render_custom_approval(
    spec: PipelineSpec,
    compiled: CompiledPipeline,
    *,
    policy: CustomPipelinePolicy,
) -> str:
    """Render a bounded human-readable pre-effect approval card."""

    budget = compiled.worst_case_budget
    ceiling = policy.worst_case_budget
    maximum_nodes, maximum_models = _flow_bounds(spec)
    loop_edges = tuple(
        item
        for item in spec.transitions
        if not item.target.startswith("terminal:")
        and next(
            index
            for index, step in enumerate(spec.steps)
            if step.step_id == item.target
        )
        <= next(
            index
            for index, step in enumerate(spec.steps)
            if step.step_id == item.from_step
        )
    )
    lines = [
            f"Custom pipeline: {spec.spec_id}@{spec.version}",
            f"Definition: {compiled.definition_sha256}",
            f"Baseline: {spec.baseline_pipeline}",
            f"Route: {spec.route_alias}; capabilities: "
            + (", ".join(spec.required_capabilities) or "none"),
            "Delta: " + " -> ".join(step.step_id for step in spec.steps),
            "Control flow: "
            f"transitions={len(spec.transitions)}, loops={len(loop_edges)}, "
            f"node-visits<={maximum_nodes}, model-calls<={maximum_models}",
            "Loop bounds: "
            + (
                ", ".join(
                    f"{item.from_step}:{item.outcome}->{item.target} x{item.max_traversals}"
                    for item in loop_edges
                )
                or "none"
            ),
            "Worst case: "
            f"attempts={budget.attempt_limit}, restarts={budget.model_restart_limit}, "
            f"deadline={budget.time_budget_seconds}s, tokens={budget.token_limit}",
            "Absolute ceiling: "
            f"attempts={ceiling.attempt_limit}, restarts={ceiling.model_restart_limit}, "
            f"deadline={ceiling.time_budget_seconds}s, tokens={ceiling.token_limit}",
            "Declared permissions: "
            + (", ".join(spec.requested_permissions) or "none"),
            "Effective permissions: "
            + (", ".join(compiled.permission_bindings) or "none"),
            "Declared side effects: "
            + (", ".join(spec.requested_side_effects) or "none"),
            "Effective side effects: "
            + (", ".join(compiled.side_effect_bindings) or "none"),
    ]
    if spec.finalization_policy is not None:
        finalization = spec.finalization_policy
        lines.append(
            "Finalization: "
            f"cycles<={finalization.max_cycles}; "
            "independent-after="
            f"{finalization.add_independent_model_after}; "
            f"execution={finalization.execution}; routes="
            f"{finalization.primary_route_alias},"
            f"{finalization.independent_route_alias}"
        )
    lines.extend(
        [
            f"Review: {spec.review_mode}; completion: {spec.completion_policy}",
            "Stops: " + ", ".join(spec.terminal_outcomes),
            "Explicit user approval required before freeze or execution.",
        ]
    )
    rendered = "\n".join(lines) + "\n"
    if len(rendered.encode()) > 8_192:
        raise ContractError("custom approval summary exceeds size cap")
    return rendered
