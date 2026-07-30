#!/usr/bin/env python3
"""Typed pipeline compiler contracts."""

from __future__ import annotations

import dataclasses
import sys
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.pipelines import (
    PipelineBudget,
    PipelineDefinition,
    PipelineStep,
    PolicyBinding,
    PrimitiveDefinition,
    PrimitiveRegistry,
    compile_pipeline,
    render_contract,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


registry = PrimitiveRegistry(
    primitives=(
        PrimitiveDefinition(
            "human_gate",
            "1.0.0",
        ),
        PrimitiveDefinition(
            "model_step",
            "1.0.0",
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
            "1.0.0",
            budget=PipelineBudget(
                verification_calls=2,
                deadline_seconds=120,
            ),
            side_effects=("verification-process",),
        ),
        PrimitiveDefinition(
            "review",
            "1.0.0",
            budget=PipelineBudget(
                review_calls=1,
                token_limit=40_000,
                deadline_seconds=900,
                restart_limit=1,
            ),
            required_capabilities=("provider:authenticated",),
        ),
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
definition = PipelineDefinition(
    pipeline_id="engineering",
    version="1.0.0",
    profile="change",
    input_schema="task-contract/v1",
    output_schema="review-approved/v1",
    steps=(
        PipelineStep(
            "approve",
            "human_gate",
            "1.0.0",
            "task-contract/v1",
            "approved-contract/v1",
        ),
        PipelineStep(
            "implement",
            "model_step",
            "1.0.0",
            "approved-contract/v1",
            "change/v1",
        ),
        PipelineStep(
            "verify",
            "verify",
            "1.0.0",
            "change/v1",
            "change/v1",
        ),
        PipelineStep(
            "review",
            "review",
            "1.0.0",
            "change/v1",
            "review-approved/v1",
        ),
    ),
    permission_ceiling=("product-write",),
    side_effect_ceiling=("verification-process", "workspace-write"),
)

compiled = compile_pipeline(
    definition,
    registry,
    capabilities=("provider:authenticated",),
)
recompiled = compile_pipeline(
    definition,
    registry,
    capabilities=("provider:authenticated",),
)

check("pipeline contracts are frozen", dataclasses.is_dataclass(compiled) and compiled.__dataclass_params__.frozen)
check("compiled contract pins compiler compatibility", compiled.compiler_version == "1.0.0")
check("canonical definition hash is stable", compiled.definition_sha256 == recompiled.definition_sha256 and len(compiled.definition_sha256) == 64)
check(
    "compiler resolves exact primitive versions",
    compiled.resolved_primitives
    == (
        "human_gate@1.0.0",
        "model_step@1.0.0",
        "verify@1.0.0",
        "review@1.0.0",
    ),
)
check(
    "compiler calculates the static worst-case budget",
    compiled.budget
    == PipelineBudget(
        model_calls=1,
        review_calls=1,
        verification_calls=2,
        token_limit=60_000,
        deadline_seconds=1_620,
        restart_limit=2,
    ),
)
check(
    "compiler summarizes bound policy before execution",
    compiled.permissions == ("product-write",)
    and compiled.side_effects == ("verification-process", "workspace-write")
    and {binding.enforcement for binding in compiled.bindings}
    == {"policy-only", "sandbox-enforced"},
)

summary = render_contract(compiled)
check(
    "approval contract is bounded and user-readable",
    len(summary.encode()) < 2_000
    and "engineering/change@1.0.0" in summary
    and compiled.definition_sha256 in summary
    and "model=1 review=1 verify=2" in summary
    and "product-write [sandbox-enforced:workspace-root]" in summary,
)


def expect_compile_error(label: str, value: PipelineDefinition, token: str) -> None:
    try:
        compile_pipeline(
            value,
            registry,
            capabilities=("provider:authenticated",),
        )
    except Exception as exc:
        check(label, token in str(exc))
    else:
        check(label, False)


expect_compile_error(
    "compiler rejects a mismatched pipeline input schema",
    replace(definition, input_schema="wrong/v1"),
    "pipeline input schema",
)
expect_compile_error(
    "compiler rejects a mismatched pipeline output schema",
    replace(definition, output_schema="wrong/v1"),
    "pipeline output schema",
)
