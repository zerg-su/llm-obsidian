#!/usr/bin/env python3
"""Built-in pipeline proof-of-use contracts."""

from __future__ import annotations

import dataclasses
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
    "registry exposes only the approved 2.4 primitive vocabulary",
    {primitive.primitive_id for primitive in registry.primitives}
    == {"bounded_loop", "human_gate", "model_step", "review", "verify"},
)
check(
    "built-ins cover lifecycle and both engineering profiles",
    set(definitions) == {"engineering/change", "engineering/fix", "lifecycle/default"},
)

compiled = {
    name: compile_pipeline(
        definition,
        registry,
        capabilities=("provider:authenticated",),
    )
    for name, definition in definitions.items()
}

check(
    "lifecycle keeps dispatch review reap-ready semantics",
    tuple(step.step_id for step in compiled["lifecycle/default"].definition.steps)
    == ("approve", "dispatch", "review")
    and compiled["lifecycle/default"].definition.output_schema == "reap-ready/v1",
)
check(
    "change profile encodes the approved engineering sequence",
    tuple(step.step_id for step in compiled["engineering/change"].definition.steps)
    == ("approve", "tdd-slices", "verify", "review"),
)
check(
    "fix profile encodes reproduce root-cause regression minimal-fix",
    tuple(step.step_id for step in compiled["engineering/fix"].definition.steps)
    == (
        "approve",
        "reproduce",
        "root-cause",
        "regression-test",
        "minimal-fix",
        "verify",
        "review",
    ),
)
check(
    "model work is statically visible in the worst-case budget",
    compiled["engineering/change"].budget.model_calls == 1
    and compiled["engineering/fix"].budget.model_calls == 4,
)
check(
    "all built-ins end at the same coordinator-owned boundary",
    all(
        item.definition.output_schema == "reap-ready/v1"
        and item.definition.steps[-1].primitive_id == "review"
        for item in compiled.values()
    ),
)
check(
    "lifecycle session mapping reuses the existing worktree and review lanes",
    tuple(
        step.session_mode
        for step in compiled["lifecycle/default"].definition.steps
    )
    == ("controller", "worktree", "review"),
)
check(
    "fix keeps diagnosis rounds in the original worktree session",
    tuple(
        step.session_mode
        for step in compiled["engineering/fix"].definition.steps
    )
    == (
        "controller",
        "worktree",
        "parent-child",
        "parent-child",
        "parent-child",
        "verification",
        "review",
    ),
)
fix_steps = {
    step.step_id: step
    for step in compiled["engineering/fix"].definition.steps
}
check(
    "engineering steps retain the installed skill semantics",
    compiled["engineering/change"].definition.steps[1].semantic_skills
    == ("tdd",)
    and fix_steps["reproduce"].semantic_skills == ("debug",)
    and fix_steps["root-cause"].semantic_skills == ("debug",)
    and fix_steps["regression-test"].semantic_skills == ("debug", "tdd")
    and fix_steps["minimal-fix"].semantic_skills == ("debug", "tdd")
    and fix_steps["review"].semantic_skills == ("review",),
)
unknown_semantics = dataclasses.replace(
    definitions["engineering/change"],
    steps=(
        definitions["engineering/change"].steps[:1]
        + (
            dataclasses.replace(
                definitions["engineering/change"].steps[1],
                semantic_skills=("missing-skill",),
            ),
        )
        + definitions["engineering/change"].steps[2:]
    ),
)
try:
    compile_pipeline(
        unknown_semantics,
        registry,
        capabilities=("provider:authenticated",),
    )
except Exception as exc:
    check(
        "compiler rejects unregistered skill semantics",
        "unregistered semantic skills" in str(exc),
    )
else:
    check("compiler rejects unregistered skill semantics", False)
