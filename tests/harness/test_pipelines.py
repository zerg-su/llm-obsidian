#!/usr/bin/env python3
"""Thin compiled-contract fallback after the 2.4 net-value gate."""

from __future__ import annotations

import dataclasses
import sys
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.pipelines import (
    CompletionPolicy,
    PipelineBudget,
    PipelineDefinition,
    PipelineStep,
    PrimitiveDefinition,
    PrimitiveReference,
    PrimitiveRegistry,
    compile_pipeline,
    reconcile_pipeline,
    render_contract,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


registry = PrimitiveRegistry(
    primitives=(
        PrimitiveDefinition(
            "model_step",
            "1.0.0",
            session_modes=("parent-child", "worktree"),
            required_capabilities=("provider:authenticated",),
        ),
        PrimitiveDefinition(
            "verify",
            "1.0.0",
            session_modes=("verification",),
        ),
        PrimitiveDefinition(
            "review",
            "1.0.0",
            session_modes=("review",),
            required_capabilities=("provider:authenticated",),
        ),
        PrimitiveDefinition(
            "human_gate",
            "1.0.0",
            session_modes=(),
            primitive_kind="control",
        ),
        PrimitiveDefinition(
            "bounded_loop",
            "1.0.0",
            session_modes=(),
            primitive_kind="control",
        ),
    ),
    semantic_skills=("review", "tdd"),
)
definition = PipelineDefinition(
    pipeline_id="engineering",
    version="1.0.0",
    profile="change",
    input_schema="approved-plan/v1",
    output_schema="reap-ready/v1",
    control_primitives=(
        PrimitiveReference("human_gate", "1.0.0"),
        PrimitiveReference("bounded_loop", "1.0.0"),
    ),
    pass_budget=PipelineBudget(
        attempt_limit=2,
        model_restart_limit=1,
        time_budget_seconds=300,
        token_limit=10_000,
    ),
    permission_ceiling=("git-write", "product-worktree"),
    side_effects=("git-write", "worktree"),
    completion_policies=(
        CompletionPolicy("attention", 2),
        CompletionPolicy("autonomous", 3),
    ),
    steps=(
        PipelineStep(
            "implement",
            "model_step",
            "1.0.0",
            "approved-plan/v1",
            "change/v1",
            "worktree",
            ("tdd",),
        ),
        PipelineStep(
            "verify",
            "verify",
            "1.0.0",
            "change/v1",
            "verified/v1",
            "verification",
        ),
        PipelineStep(
            "review",
            "review",
            "1.0.0",
            "verified/v1",
            "reap-ready/v1",
            "review",
            ("review",),
        ),
    ),
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

check(
    "pipeline contracts are frozen",
    dataclasses.is_dataclass(compiled) and compiled.__dataclass_params__.frozen,
)
check("compiled contract pins compiler compatibility", compiled.compiler_version == "1.1.0")
check(
    "canonical definition hash is stable",
    compiled.definition_sha256 == recompiled.definition_sha256
    and len(compiled.definition_sha256) == 64,
)
check(
    "compiler resolves exact primitive versions",
    compiled.resolved_primitives
    == ("model_step@1.0.0", "verify@1.0.0", "review@1.0.0"),
)
check(
    "compiler resolves controls without adding fake linear steps",
    compiled.resolved_control_primitives
    == ("bounded_loop@1.0.0", "human_gate@1.0.0")
    and tuple(step.step_id for step in compiled.definition.steps)
    == ("implement", "verify", "review"),
)
check(
    "compiler exposes only declared capability requirements",
    compiled.required_capabilities == ("provider:authenticated",),
)
check(
    "compiler exposes a conservative immutable worst-case budget",
    compiled.worst_case_budget
    == PipelineBudget(
        attempt_limit=6,
        model_restart_limit=3,
        time_budget_seconds=900,
        token_limit=30_000,
    ),
)
check(
    "compiled policy ceiling and effects use code-owned bindings",
    compiled.permission_ceiling == ("git-write", "product-worktree")
    and compiled.side_effects == ("git-write", "worktree")
    and compiled.permission_bindings
    == ("git-write:sandbox-enforced", "product-worktree:sandbox-enforced")
    and compiled.side_effect_bindings
    == ("git-write:code-policy-enforced", "worktree:code-policy-enforced"),
)

summary = render_contract(compiled, completion_policy="autonomous")
check(
    "contract is bounded and labels its real enforcement boundary",
    len(summary.encode()) < 2_000
    and "engineering/change@1.0.0" in summary
    and compiled.definition_sha256 in summary
    and "state-free reconciliation" in summary
    and "Limits: attempts=6, model-restarts=3, deadline=900s, tokens=30000"
    in summary
    and "Completion: policy=autonomous, total-passes=3" in summary
    and "Controls: bounded_loop@1.0.0, human_gate@1.0.0" in summary
    and "Review: mode=simple, verification-iterations=1" in summary
    and "Side effects: git-write:code-policy-enforced, worktree:code-policy-enforced"
    in summary
    and "Permissions: git-write:sandbox-enforced, product-worktree:sandbox-enforced"
    in summary
    and "Returns: completed, escalation, attention-required, cancelled, timeout"
    in summary
    and "sandbox-enforced" in summary
    and "code-policy-enforced" in summary,
)

dispatch_wait = reconcile_pipeline(
    compiled,
    {
        "implement": "running",
        "verify": "pending",
        "review": "pending",
    },
)
check(
    "compiled order derives one wait action without controller state",
    dispatch_wait.action == "wait"
    and dispatch_wait.step_id == "implement"
    and dispatch_wait.completed_steps == (),
)
start_verify = reconcile_pipeline(
    compiled,
    {
        "implement": "complete",
        "verify": "pending",
        "review": "pending",
    },
)
check(
    "completed records derive the next executable step",
    start_verify.action == "start"
    and start_verify.step_id == "verify"
    and start_verify.completed_steps == ("implement",),
)
reap_ready = reconcile_pipeline(
    compiled,
    {
        "implement": "complete",
        "verify": "complete",
        "review": "complete",
    },
)
check(
    "the compiled terminal prefix derives reap-ready",
    reap_ready.action == "reap-ready"
    and reap_ready.step_id == ""
    and reap_ready.completed_steps == ("implement", "verify", "review"),
)
attention = reconcile_pipeline(
    compiled,
    {
        "implement": "complete",
        "verify": "attention",
        "review": "pending",
    },
)
check(
    "typed attention stops before later steps",
    attention.action == "attention"
    and attention.step_id == "verify"
    and attention.completed_steps == ("implement",),
)

try:
    reconcile_pipeline(
        compiled,
        {
            "implement": "pending",
            "verify": "complete",
            "review": "pending",
        },
    )
except Exception as exc:
    check(
        "out-of-order durable evidence fails closed",
        "ordered prefix" in str(exc),
    )
else:
    check("out-of-order durable evidence fails closed", False)


def expect_compile_error(
    label: str,
    value: PipelineDefinition,
    token: str,
    *,
    capabilities: tuple[str, ...] = ("provider:authenticated",),
) -> None:
    try:
        compile_pipeline(value, registry, capabilities=capabilities)
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
expect_compile_error(
    "compiler rejects an unsupported session mode",
    replace(
        definition,
        steps=(
            replace(definition.steps[0], session_mode="review"),
            *definition.steps[1:],
        ),
    ),
    "does not support session mode",
)
expect_compile_error(
    "compiler rejects a missing route capability",
    definition,
    "lacks capabilities",
    capabilities=(),
)
expect_compile_error(
    "compiler rejects unregistered skill semantics",
    replace(
        definition,
        steps=(
            replace(definition.steps[0], semantic_skills=("missing-skill",)),
            *definition.steps[1:],
        ),
    ),
    "unregistered semantic skills",
)
expect_compile_error(
    "compiler rejects unresolved control primitives",
    replace(
        definition,
        control_primitives=(PrimitiveReference("missing-control", "1.0.0"),),
    ),
    "unknown primitive",
)
expect_compile_error(
    "compiler rejects an execution primitive used as a control",
    replace(
        definition,
        control_primitives=(PrimitiveReference("verify", "1.0.0"),),
    ),
    "not a control primitive",
)

for policy, limit in (("attention", 3), ("autonomous", 2), ("unknown", 2)):
    try:
        replace(
            definition,
            completion_policies=(CompletionPolicy(policy, limit),),
        )
    except Exception as exc:
        check(
            f"completion policy {policy}/{limit} fails closed",
            "completion policy" in str(exc),
        )
    else:
        check(f"completion policy {policy}/{limit} fails closed", False)

for field, value in (
    ("permission_ceiling", ("arbitrary-root",)),
    ("side_effects", ("arbitrary-effect",)),
):
    try:
        replace(definition, **{field: value})
    except Exception as exc:
        check(
            f"unknown {field} fails closed",
            field.replace("_", " ") in str(exc),
        )
    else:
        check(f"unknown {field} fails closed", False)

changed_policies = replace(
    definition,
    completion_policies=(CompletionPolicy("attention", 2),),
)
check(
    "typed allowed completion policies participate in the canonical contract hash",
    compile_pipeline(
        changed_policies,
        registry,
        capabilities=("provider:authenticated",),
    ).definition_sha256
    != compiled.definition_sha256,
)
for label, changed in (
    (
        "budget",
        replace(
            definition,
            pass_budget=replace(
                definition.pass_budget,
                token_limit=definition.pass_budget.token_limit + 1,
            ),
        ),
    ),
    (
        "permission ceiling",
        replace(definition, permission_ceiling=("product-worktree",)),
    ),
    (
        "side effects",
        replace(definition, side_effects=("worktree",)),
    ),
):
    check(
        f"{label} participates in the canonical contract hash",
        compile_pipeline(
            changed,
            registry,
            capabilities=("provider:authenticated",),
        ).definition_sha256
        != compiled.definition_sha256,
    )

try:
    render_contract(compiled, completion_policy="missing")
except Exception as exc:
    check(
        "render rejects request-time policies outside the immutable contract",
        "completion policy" in str(exc),
    )
else:
    check(
        "render rejects request-time policies outside the immutable contract",
        False,
    )
