"""Code-owned semantic pipeline catalog for state-free harness reconciliation."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .pipelines import (
    CompletionPolicy,
    CompiledPipeline,
    PipelineBudget,
    PipelineDefinition,
    PipelineStep,
    PrimitiveDefinition,
    PrimitiveReference,
    PrimitiveRegistry,
    compile_pipeline,
)


VERSION = "1.0.0"
StepSpec = tuple[str, str, str, str, str, tuple[str, ...]]
EXECUTABLE_BUILTINS = frozenset(
    {"lifecycle/default", "engineering/change", "engineering/fix"}
)

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
                required_capabilities=("route:resolved",),
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
                required_capabilities=("route:resolved",),
            ),
            PrimitiveDefinition(
                "human_gate",
                VERSION,
                session_modes=(),
                primitive_kind="control",
            ),
            PrimitiveDefinition(
                "bounded_loop",
                VERSION,
                session_modes=(),
                primitive_kind="control",
            ),
        ),
        semantic_skills=("debug", "dispatch", "review", "tdd"),
    )


def _definition(
    pipeline_id: str,
    profile: str,
    steps: tuple[StepSpec, ...],
) -> PipelineDefinition:
    is_fix = pipeline_id == "engineering" and profile == "fix"
    return PipelineDefinition(
        pipeline_id=pipeline_id,
        version=VERSION,
        profile=profile,
        input_schema="approved-plan/v1",
        output_schema="reap-ready/v1",
        control_primitives=(
            (
                PrimitiveReference("bounded_loop", VERSION),
                PrimitiveReference("human_gate", VERSION),
            )
            if is_fix
            else ()
        ),
        pass_budget=PipelineBudget(),
        completion_policies=(
            (
                CompletionPolicy("attention", 2),
                CompletionPolicy("autonomous", 3),
            )
            if is_fix
            else (CompletionPolicy("attention", 2),)
        ),
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


def compiled_builtin(name: str) -> CompiledPipeline:
    """Compile one exact code-owned built-in against runtime route support."""

    try:
        definition = builtin_definitions()[name]
    except KeyError as exc:
        raise ValueError(f"unknown built-in pipeline: {name}") from exc
    return compile_pipeline(
        definition,
        builtin_registry(),
        capabilities=("route:resolved",),
    )


def compiled_executable_for_contract(
    definition_sha256: str,
) -> tuple[str, CompiledPipeline]:
    """Resolve an exact production contract without adding runtime state."""

    for name in sorted(EXECUTABLE_BUILTINS):
        compiled = compiled_builtin(name)
        if compiled.definition_sha256 == definition_sha256:
            return name, compiled
    raise ValueError("unknown executable pipeline contract")
