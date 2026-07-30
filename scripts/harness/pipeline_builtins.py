"""Code-owned semantic pipeline catalog for the 2.4 fallback."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .pipelines import (
    PipelineDefinition,
    PipelineStep,
    PrimitiveDefinition,
    PrimitiveRegistry,
)


VERSION = "1.0.0"
StepSpec = tuple[str, str, str, str, str, tuple[str, ...]]

BUILTINS: dict[str, tuple[str, str, tuple[StepSpec, ...]]] = {
    "lifecycle/default": (
        "lifecycle",
        "default",
        (
            (
                "dispatch",
                "model_step",
                "approved-plan/v1",
                "implementation-result/v1",
                "worktree",
                ("dispatch",),
            ),
            (
                "review",
                "review",
                "implementation-result/v1",
                "reap-ready/v1",
                "review",
                ("review",),
            ),
        ),
    ),
    "engineering/change": (
        "engineering",
        "change",
        (
            (
                "tdd-slices",
                "model_step",
                "approved-plan/v1",
                "implementation-result/v1",
                "worktree",
                ("tdd",),
            ),
            (
                "verify",
                "verify",
                "implementation-result/v1",
                "verified-result/v1",
                "verification",
                (),
            ),
            (
                "review",
                "review",
                "verified-result/v1",
                "reap-ready/v1",
                "review",
                ("review",),
            ),
        ),
    ),
    "engineering/fix": (
        "engineering",
        "fix",
        (
            (
                "reproduce",
                "model_step",
                "approved-plan/v1",
                "reproduction/v1",
                "worktree",
                ("debug",),
            ),
            (
                "root-cause",
                "model_step",
                "reproduction/v1",
                "diagnosis/v1",
                "parent-child",
                ("debug",),
            ),
            (
                "regression-test",
                "model_step",
                "diagnosis/v1",
                "regression-test/v1",
                "parent-child",
                ("debug", "tdd"),
            ),
            (
                "minimal-fix",
                "model_step",
                "regression-test/v1",
                "implementation-result/v1",
                "parent-child",
                ("debug", "tdd"),
            ),
            (
                "verify",
                "verify",
                "implementation-result/v1",
                "verified-result/v1",
                "verification",
                (),
            ),
            (
                "review",
                "review",
                "verified-result/v1",
                "reap-ready/v1",
                "review",
                ("review",),
            ),
        ),
    ),
}


def builtin_registry() -> PrimitiveRegistry:
    return PrimitiveRegistry(
        primitives=(
            PrimitiveDefinition(
                "model_step",
                VERSION,
                session_modes=("parent-child", "worktree"),
                required_capabilities=("provider:authenticated",),
            ),
            PrimitiveDefinition(
                "verify",
                VERSION,
                session_modes=("verification",),
            ),
            PrimitiveDefinition(
                "review",
                VERSION,
                session_modes=("review",),
                required_capabilities=("provider:authenticated",),
            ),
        ),
        semantic_skills=("debug", "dispatch", "review", "tdd"),
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
        input_schema="approved-plan/v1",
        output_schema="reap-ready/v1",
        steps=tuple(
            PipelineStep(
                step_id,
                primitive_id,
                VERSION,
                input_schema,
                output_schema,
                session_mode,
                semantic_skills,
            )
            for (
                step_id,
                primitive_id,
                input_schema,
                output_schema,
                session_mode,
                semantic_skills,
            ) in steps
        ),
    )


def builtin_definitions() -> Mapping[str, PipelineDefinition]:
    return MappingProxyType(
        {
            name: _definition(pipeline_id, profile, steps)
            for name, (pipeline_id, profile, steps) in BUILTINS.items()
        }
    )
