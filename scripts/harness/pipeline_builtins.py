"""Code-owned built-in pipelines for the 2.4 shadow path."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .pipelines import (
    PipelineBudget,
    PipelineDefinition,
    PipelineStep,
    PolicyBinding,
    PrimitiveDefinition,
    PrimitiveRegistry,
)


VERSION = "1.0.0"
StepSpec = tuple[str, str, str, str, str]

BUILTINS: dict[str, tuple[str, str, tuple[StepSpec, ...]]] = {
    "lifecycle/default": (
        "lifecycle",
        "default",
        (
            (
                "approve",
                "human_gate",
                "task-contract/v1",
                "approved-plan/v1",
                "controller",
            ),
            (
                "dispatch",
                "model_step",
                "approved-plan/v1",
                "implementation-result/v1",
                "worktree",
            ),
            (
                "review",
                "review",
                "implementation-result/v1",
                "reap-ready/v1",
                "review",
            ),
        ),
    ),
    "engineering/change": (
        "engineering",
        "change",
        (
            (
                "approve",
                "human_gate",
                "task-contract/v1",
                "approved-plan/v1",
                "controller",
            ),
            (
                "tdd-slices",
                "model_step",
                "approved-plan/v1",
                "implementation-result/v1",
                "worktree",
            ),
            (
                "verify",
                "verify",
                "implementation-result/v1",
                "verified-result/v1",
                "verification",
            ),
            (
                "review",
                "review",
                "verified-result/v1",
                "reap-ready/v1",
                "review",
            ),
        ),
    ),
    "engineering/fix": (
        "engineering",
        "fix",
        (
            (
                "approve",
                "human_gate",
                "task-contract/v1",
                "approved-plan/v1",
                "controller",
            ),
            (
                "reproduce",
                "model_step",
                "approved-plan/v1",
                "reproduction/v1",
                "worktree",
            ),
            (
                "root-cause",
                "model_step",
                "reproduction/v1",
                "diagnosis/v1",
                "parent-child",
            ),
            (
                "regression-test",
                "model_step",
                "diagnosis/v1",
                "regression-test/v1",
                "parent-child",
            ),
            (
                "minimal-fix",
                "model_step",
                "regression-test/v1",
                "implementation-result/v1",
                "parent-child",
            ),
            (
                "verify",
                "verify",
                "implementation-result/v1",
                "verified-result/v1",
                "verification",
            ),
            (
                "review",
                "review",
                "verified-result/v1",
                "reap-ready/v1",
                "review",
            ),
        ),
    ),
}


def builtin_registry() -> PrimitiveRegistry:
    return PrimitiveRegistry(
        primitives=(
            PrimitiveDefinition("human_gate", VERSION),
            PrimitiveDefinition(
                "model_step",
                VERSION,
                budget=PipelineBudget(
                    model_calls=1,
                    token_limit=20_000,
                    deadline_seconds=600,
                    restart_limit=1,
                ),
                permissions=("product-write",),
                side_effects=("workspace-write",),
                required_capabilities=("provider:authenticated",),
            ),
            PrimitiveDefinition(
                "verify",
                VERSION,
                budget=PipelineBudget(
                    verification_calls=1,
                    deadline_seconds=120,
                ),
                side_effects=("verification-process",),
            ),
            PrimitiveDefinition(
                "review",
                VERSION,
                budget=PipelineBudget(
                    review_calls=1,
                    token_limit=40_000,
                    deadline_seconds=900,
                    restart_limit=1,
                ),
                required_capabilities=("provider:authenticated",),
            ),
            PrimitiveDefinition("bounded_loop", VERSION),
        ),
        bindings=(
            PolicyBinding(
                "permission",
                "product-write",
                "workspace-root",
                "sandbox-enforced",
            ),
            PolicyBinding(
                "side-effect",
                "workspace-write",
                "operation-supervisor",
                "sandbox-enforced",
            ),
            PolicyBinding(
                "side-effect",
                "verification-process",
                "verification-profile",
                "policy-only",
            ),
        ),
    )


def _definition(
    pipeline_id: str,
    profile: str,
    steps: tuple[StepSpec, ...],
) -> PipelineDefinition:
    return PipelineDefinition(
        pipeline_id=pipeline_id,
        version=VERSION,
        profile=profile,
        input_schema="task-contract/v1",
        output_schema="reap-ready/v1",
        steps=tuple(
            PipelineStep(
                step_id,
                primitive_id,
                VERSION,
                input_schema,
                output_schema,
                session_mode,
            )
            for (
                step_id,
                primitive_id,
                input_schema,
                output_schema,
                session_mode,
            ) in steps
        ),
        permission_ceiling=("product-write",),
        side_effect_ceiling=("verification-process", "workspace-write"),
    )


def builtin_definitions() -> Mapping[str, PipelineDefinition]:
    return MappingProxyType(
        {
            name: _definition(pipeline_id, profile, steps)
            for name, (pipeline_id, profile, steps) in BUILTINS.items()
        }
    )
