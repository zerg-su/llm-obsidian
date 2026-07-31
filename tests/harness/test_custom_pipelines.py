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
    CustomPipelinePolicy,
    compile_custom_spec,
    parse_pipeline_spec,
    render_custom_approval,
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
