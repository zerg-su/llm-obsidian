#!/usr/bin/env python3
"""Strict compile-only contract for model-authored pipeline specs."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.custom_pipelines import (  # noqa: E402
    ExplicitPipelineApproval,
    CustomPipelinePolicy,
    compile_custom_spec,
    freeze_custom_pipeline,
    parse_pipeline_spec,
    render_authoring_contract,
    render_custom_approval,
    select_builtin_baseline,
)
from harness.pipeline_builtins import builtin_registry  # noqa: E402


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


VALID = {
    "schema_version": 1,
    "spec_id": "change-with-extra-review",
    "version": "1.0.0",
    "intent": "engineering-change",
    "task_profile": "change",
    "baseline_pipeline": "engineering/change",
    "input_schema": "approved-plan/v1",
    "output_schema": "reap-ready/v1",
    "steps": [
        {
            "step_id": "implement",
            "primitive_id": "model_step",
            "primitive_version": "1.0.0",
            "input_schema": "approved-plan/v1",
            "output_schema": "implementation-result/v1",
            "session_mode": "worktree",
            "semantic_skills": ["tdd"],
        },
        {
            "step_id": "verify",
            "primitive_id": "verify",
            "primitive_version": "1.0.0",
            "input_schema": "implementation-result/v1",
            "output_schema": "verified-result/v1",
            "session_mode": "verification",
            "semantic_skills": [],
        },
        {
            "step_id": "review",
            "primitive_id": "review",
            "primitive_version": "1.0.0",
            "input_schema": "verified-result/v1",
            "output_schema": "reap-ready/v1",
            "session_mode": "review",
            "semantic_skills": ["review"],
        },
    ],
    "controls": [
        {"primitive_id": "bounded_loop", "version": "1.0.0"},
        {"primitive_id": "human_gate", "version": "1.0.0"},
    ],
    "budget": {
        "attempt_limit": 2,
        "model_restart_limit": 1,
        "time_budget_seconds": 900,
        "token_limit": 50000,
    },
    "completion_policy": "autonomous",
    "requested_permissions": ["git-write", "product-worktree"],
    "requested_side_effects": ["git-write", "worktree"],
    "context_pointers": [
        {
            "pointer_id": "approved-plan",
            "content_sha256": "a" * 64,
            "byte_limit": 65536,
        }
    ],
    "verification_checks": ["focused-tests", "diff-check"],
    "review_mode": "simple",
    "human_gates": ["initial-approval"],
    "terminal_outcomes": ["completed", "attention-required"],
}


policy = CustomPipelinePolicy.default()
spec = parse_pipeline_spec(json.dumps(VALID))
check(
    "code-owned selector chooses the baseline",
    select_builtin_baseline(spec.intent, spec.task_profile) == "engineering/change",
)
compiled = compile_custom_spec(
    spec,
    builtin_registry(),
    policy=policy,
    capabilities=("route:resolved",),
)
recompiled = compile_custom_spec(
    parse_pipeline_spec(json.dumps(VALID, sort_keys=True)),
    builtin_registry(),
    policy=policy,
    capabilities=("route:resolved",),
)

check("strict parser produces a stable compiled hash", compiled.definition_sha256 == recompiled.definition_sha256)
check("custom compiler uses the existing compiler", compiled.compiler_version.startswith("1."))
check("custom budget is bounded by the code-owned ceiling", compiled.worst_case_budget.attempt_limit <= policy.worst_case_budget.attempt_limit)

approval = render_custom_approval(spec, compiled, policy=policy)
check(
    "approval renders baseline delta and absolute ceiling",
    "Baseline: engineering/change" in approval
    and "Absolute ceiling:" in approval
    and "Requested permissions:" in approval
    and "Explicit user approval required" in approval,
)
authoring = render_authoring_contract(
    intent=spec.intent,
    task_profile=spec.task_profile,
)
check(
    "authoring contract names the schema and forbids executable content",
    "pipeline-spec-v1.schema.json" in authoring
    and "Do not emit shell, Python" in authoring
    and "Baseline: engineering/change" in authoring,
)
approval_receipt = ExplicitPipelineApproval.for_card(
    definition_sha256=compiled.definition_sha256,
    approval_card=approval,
    actor="user",
    decision="approve",
)
frozen = freeze_custom_pipeline(spec, compiled, approval_receipt, approval)
check(
    "exact explicit approval freezes the compiled hash",
    frozen.definition_sha256 == compiled.definition_sha256
    and frozen.approval_sha256 == approval_receipt.approval_card_sha256,
)


def expect_rejection(label: str, mutation, token: str) -> None:
    value = deepcopy(VALID)
    mutation(value)
    try:
        parsed = parse_pipeline_spec(json.dumps(value))
        compile_custom_spec(
            parsed,
            builtin_registry(),
            policy=policy,
            capabilities=("route:resolved",),
        )
    except Exception as exc:
        check(label, token in str(exc))
    else:
        check(label, False)


expect_rejection(
    "unknown top-level fields fail closed",
    lambda value: value.__setitem__("shell", "rm -rf /"),
    "unknown fields",
)

wrong_baseline = deepcopy(VALID)
wrong_baseline["baseline_pipeline"] = "engineering/fix"
try:
    compile_custom_spec(
        parse_pipeline_spec(wrong_baseline),
        builtin_registry(),
        policy=policy,
        capabilities=("route:resolved",),
    )
except Exception as exc:
    check("model cannot choose a permissive delta baseline", "deterministic baseline" in str(exc))
else:
    check("model cannot choose a permissive delta baseline", False)

equivalent = deepcopy(VALID)
equivalent["steps"][0]["step_id"] = "tdd-slices"
equivalent["controls"] = []
equivalent["completion_policy"] = "attention"
equivalent["verification_checks"] = []
try:
    compile_custom_spec(
        parse_pipeline_spec(equivalent),
        builtin_registry(),
        policy=policy,
        capabilities=("route:resolved",),
    )
except Exception as exc:
    check("built-in-equivalent proposals are rejected", "built-in already fits" in str(exc))
else:
    check("built-in-equivalent proposals are rejected", False)

for label, receipt, token in (
    (
        "model cannot approve its own proposal",
        ExplicitPipelineApproval.for_card(
            definition_sha256=compiled.definition_sha256,
            approval_card=approval,
            actor="model",
            decision="approve",
        ),
        "user",
    ),
    (
        "approval cannot be replayed for a different definition",
        ExplicitPipelineApproval.for_card(
            definition_sha256="b" * 64,
            approval_card=approval,
            actor="user",
            decision="approve",
        ),
        "definition",
    ),
):
    try:
        freeze_custom_pipeline(spec, compiled, receipt, approval)
    except Exception as exc:
        check(label, token in str(exc))
    else:
        check(label, False)
expect_rejection(
    "arbitrary commands fail closed",
    lambda value: value["verification_checks"].append("python -c evil"),
    "verification check",
)
expect_rejection(
    "permission expansion fails closed",
    lambda value: value["requested_permissions"].append("network"),
    "permission ceiling",
)
expect_rejection(
    "budget expansion fails closed",
    lambda value: value["budget"].__setitem__("token_limit", 999999999),
    "budget ceiling",
)
expect_rejection(
    "graph bombs fail closed",
    lambda value: value.__setitem__("steps", value["steps"] * 10),
    "step limit",
)
expect_rejection(
    "filesystem paths cannot hide in context pointers",
    lambda value: value["context_pointers"][0].__setitem__("path", "/tmp/prompt"),
    "unknown fields",
)

print("\nAll custom pipeline tests passed.")
