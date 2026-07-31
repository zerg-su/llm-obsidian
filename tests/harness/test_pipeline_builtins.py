#!/usr/bin/env python3
"""Built-in catalog contracts for the thinned 2.4 fallback."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.pipeline_builtins import (
    EXECUTABLE_BUILTINS,
    builtin_definitions,
    builtin_registry,
    compiled_builtin,
    compiled_executable_for_contract,
)
from harness.pipelines import compile_pipeline


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


registry = builtin_registry()
definitions = builtin_definitions()

check(
    "registry exposes execution and typed control descriptors",
    {primitive.primitive_id for primitive in registry.primitives}
    == {"bounded_loop", "human_gate", "model_step", "review", "verify"},
)
controls = {
    primitive.primitive_id: primitive
    for primitive in registry.primitives
    if primitive.primitive_kind == "control"
}
check(
    "human gate and bounded loop are immutable non-step controls",
    set(controls) == {"human_gate", "bounded_loop"}
    and all(not primitive.session_modes for primitive in controls.values()),
)
check(
    "built-in model work requires only the preflight fact it can prove",
    {
        capability
        for primitive in registry.primitives
        for capability in primitive.required_capabilities
    }
    == {"route:resolved"},
)
check(
    "built-ins cover lifecycle and both engineering profiles",
    set(definitions) == {"engineering/change", "engineering/fix", "lifecycle/default"},
)

compiled = {
    name: compile_pipeline(
        definition,
        registry,
        capabilities=("route:resolved",),
    )
    for name, definition in definitions.items()
}

check(
    "fix contract explicitly resolves the bounded controls",
    compiled["engineering/fix"].resolved_control_primitives
    == ("bounded_loop@1.0.0", "human_gate@1.0.0")
    and tuple(
        (item.policy, item.total_pass_limit)
        for item in compiled["engineering/fix"].definition.completion_policies
    )
    == (("attention", 2), ("autonomous", 3)),
)
check(
    "fix compiler exposes the static worst-case of three passes",
    compiled["engineering/fix"].worst_case_budget.attempt_limit == 9
    and compiled["engineering/fix"].worst_case_budget.model_restart_limit == 3
    and compiled["engineering/fix"].worst_case_budget.time_budget_seconds
    == 5_400
    and compiled["engineering/fix"].worst_case_budget.token_limit == 600_000,
)
check(
    "both request-time completion modes share one immutable fix contract",
    compiled_builtin("engineering/fix").definition_sha256
    == compiled["engineering/fix"].definition_sha256,
)
check(
    "engineering fix is executable only by exact compiled hash",
    "engineering/fix" in EXECUTABLE_BUILTINS
    and compiled_executable_for_contract(
        compiled["engineering/fix"].definition_sha256
    )[0]
    == "engineering/fix",
)
try:
    compile_pipeline(
        replace(
            definitions["engineering/fix"],
            completion_policies=(
                definitions["engineering/fix"].completion_policies[0],
            ),
        ),
        registry,
        capabilities=("route:resolved",),
    )
except Exception as exc:
    check(
        "fix compiler rejects a partial completion policy set",
        "exact bounded completion controls" in str(exc),
    )
else:
    check("fix compiler rejects a partial completion policy set", False)

check(
    "approval is a precondition rather than a fake runtime primitive",
    all(
        item.definition.input_schema == "approved-plan/v1"
        and all(step.step_id != "approve" for step in item.definition.steps)
        for item in compiled.values()
    ),
)
check(
    "lifecycle describes dispatch review reap-ready semantics",
    tuple(step.step_id for step in compiled["lifecycle/default"].definition.steps)
    == ("dispatch", "review"),
)
check(
    "change profile describes the engineering sequence",
    tuple(step.step_id for step in compiled["engineering/change"].definition.steps)
    == ("tdd-slices", "verify", "review"),
)
check(
    "fix profile describes reproduce root-cause regression minimal-fix",
    tuple(step.step_id for step in compiled["engineering/fix"].definition.steps)
    == (
        "reproduce",
        "root-cause",
        "regression-test",
        "minimal-fix",
        "verify",
        "review",
    ),
)
check(
    "all built-ins end at the coordinator-owned reap-ready boundary",
    all(
        item.definition.output_schema == "reap-ready/v1"
        and item.definition.steps[-1].primitive_id == "review"
        for item in compiled.values()
    ),
)
fix_steps = {
    step.step_id: step
    for step in compiled["engineering/fix"].definition.steps
}
check(
    "engineering descriptors retain installed skill semantics",
    compiled["engineering/change"].definition.steps[0].semantic_skills
    == ("tdd",)
    and fix_steps["reproduce"].semantic_skills == ("debug",)
    and fix_steps["regression-test"].semantic_skills == ("debug", "tdd")
    and fix_steps["review"].semantic_skills == ("review",),
)
