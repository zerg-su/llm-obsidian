#!/usr/bin/env python3
"""Built-in catalog contracts for the thinned 2.4 fallback."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.pipeline_builtins import builtin_definitions, builtin_registry
from harness.pipelines import compile_pipeline


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


registry = builtin_registry()
definitions = builtin_definitions()

check(
    "registry exposes only descriptors backed by existing 2.3 operations",
    {primitive.primitive_id for primitive in registry.primitives}
    == {"model_step", "review", "verify"},
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
