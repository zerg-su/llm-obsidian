#!/usr/bin/env python3
"""Strict compile-only contract for model-authored pipeline specs."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.custom_pipelines import (  # noqa: E402
    ExplicitPipelineApproval,
    CustomPipelinePolicy,
    FrozenPipelineStore,
    compile_custom_spec,
    freeze_custom_pipeline,
    parse_pipeline_spec,
    render_authoring_contract,
    render_custom_approval,
    resolve_custom_executable,
    select_builtin_baseline,
)
from harness.pipeline_builtins import builtin_registry  # noqa: E402
from harness.contracts import RuntimeRoute  # noqa: E402
from harness.workflows.dispatch import (  # noqa: E402
    DispatchRequest,
    ReviewPolicy,
    operation_spec,
)


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
    "route_alias": "executor-default",
    "required_capabilities": ["route:resolved"],
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
    "transitions": [
        {"from_step": "implement", "outcome": "complete", "target": "verify", "max_traversals": 1},
        {"from_step": "verify", "outcome": "complete", "target": "review", "max_traversals": 1},
        {"from_step": "review", "outcome": "complete", "target": "terminal:completed", "max_traversals": 1},
    ],
    "controls": [],
    "budget": {
        "attempt_limit": 2,
        "model_restart_limit": 1,
        "time_budget_seconds": 900,
        "token_limit": 50000,
    },
    "completion_policy": "attention",
    "requested_permissions": ["git-write", "product-worktree"],
    "requested_side_effects": ["git-write", "worktree"],
    "context_pointers": [
        {
            "pointer_id": "approved-plan",
            "content_sha256": "a" * 64,
            "byte_limit": 65536,
        }
    ],
    "verification_checks": ["harness-tests", "diff-check"],
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
fix_raw = json.loads(json.dumps(VALID))
fix_raw.update(
    {
        "spec_id": "fix-with-compact-model-step",
        "intent": "engineering-fix",
        "task_profile": "fix",
        "baseline_pipeline": "engineering/fix",
    }
)
fix_spec = parse_pipeline_spec(fix_raw)
fix_compiled = compile_custom_spec(
    fix_spec,
    builtin_registry(),
    policy=policy,
    capabilities=("route:resolved",),
)
check(
    "custom compiler preserves deterministic fix semantics",
    select_builtin_baseline(fix_spec.intent, fix_spec.task_profile)
    == "engineering/fix"
    and fix_compiled.definition.profile == "fix",
)
recompiled = compile_custom_spec(
    parse_pipeline_spec(json.dumps(VALID, sort_keys=True)),
    builtin_registry(),
    policy=policy,
    capabilities=("route:resolved",),
)
reordered = deepcopy(VALID)
reordered["requested_permissions"].reverse()
reordered["requested_side_effects"].reverse()
reordered["terminal_outcomes"].reverse()
reordered["transitions"].reverse()
reordered_compiled = compile_custom_spec(
    parse_pipeline_spec(reordered),
    builtin_registry(),
    policy=policy,
    capabilities=("route:resolved",),
)

check("strict parser produces a stable compiled hash", compiled.definition_sha256 == recompiled.definition_sha256)
check("set-like spec ordering does not change the canonical hash", compiled.definition_sha256 == reordered_compiled.definition_sha256)
check("custom compiler uses the existing compiler", compiled.compiler_version.startswith("1."))
check("custom budget is bounded by the code-owned ceiling", compiled.worst_case_budget.attempt_limit <= policy.worst_case_budget.attempt_limit)

approval = render_custom_approval(spec, compiled, policy=policy)
check(
    "approval renders baseline delta and absolute ceiling",
    "Baseline: engineering/change" in approval
    and "Absolute ceiling:" in approval
    and "model-calls<=" in approval
    and "Loop bounds:" in approval
    and "Declared permissions:" in approval
    and "Effective permissions:" in approval
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
route = RuntimeRoute("codex", "sol", "high", "default", "c" * 64)
custom_dispatch = DispatchRequest(
    task_id="custom-dispatch-1",
    owner_id="owner-1",
    plan_sha256="d" * 64,
    context_manifest="context.json",
    route=route,
    review=ReviewPolicy(depth="simple"),
    pipeline_name="custom",
    completion_policy="attention",
    custom_pipeline=frozen,
)
check(
    "approved custom contract binds the existing dispatch operation",
    operation_spec(custom_dispatch).contract_sha256 == frozen.definition_sha256,
)

multi = deepcopy(VALID)
multi["spec_id"] = "change-with-design-step"
multi["steps"].insert(
    0,
    {
        "step_id": "design",
        "primitive_id": "model_step",
        "primitive_version": "1.0.0",
        "input_schema": "approved-plan/v1",
        "output_schema": "approved-plan/v1",
        "session_mode": "worktree",
        "semantic_skills": ["dispatch"],
    },
)
multi["transitions"].insert(
    0,
    {
        "from_step": "design",
        "outcome": "complete",
        "target": "implement",
        "max_traversals": 1,
    },
)
multi_spec = parse_pipeline_spec(multi)
multi_compiled = compile_custom_spec(
    multi_spec,
    builtin_registry(),
    policy=policy,
    capabilities=("route:resolved",),
)
multi_card = render_custom_approval(multi_spec, multi_compiled, policy=policy)
multi_frozen = freeze_custom_pipeline(
    multi_spec,
    multi_compiled,
    ExplicitPipelineApproval.for_card(
        definition_sha256=multi_compiled.definition_sha256,
        approval_card=multi_card,
        actor="user",
        decision="approve",
    ),
    multi_card,
)
multi_dispatch = DispatchRequest(
    task_id="custom-dispatch-multi",
    owner_id="owner-1",
    plan_sha256="1" * 64,
    context_manifest="context.json",
    route=route,
    review=ReviewPolicy(depth="simple"),
    pipeline_name="custom",
    completion_policy="attention",
    custom_pipeline=multi_frozen,
)
check(
    "approved custom composition may add registered sequential model steps",
    operation_spec(multi_dispatch).contract_sha256
    == multi_frozen.definition_sha256,
)
try:
    DispatchRequest(
        task_id="custom-dispatch-unapproved",
        owner_id="owner-1",
        plan_sha256="e" * 64,
        context_manifest="context.json",
        route=route,
        pipeline_name="custom",
    )
except Exception as exc:
    check("custom dispatch cannot exist before approval", "approved" in str(exc))
else:
    check("custom dispatch cannot exist before approval", False)
with tempfile.TemporaryDirectory(prefix="custom-pipeline-store.") as raw:
    store = FrozenPipelineStore(Path(raw) / "runtime")
    stored = store.save(
        operation_id="custom-operation-1",
        spec=spec,
        frozen=frozen,
        approval=approval_receipt,
    )
    loaded = store.load(
        operation_id="custom-operation-1",
        registry=builtin_registry(),
        policy=policy,
        capabilities=("route:resolved",),
    )
    check(
        "owner-only frozen store revalidates the exact approved contract",
        loaded.definition_sha256 == frozen.definition_sha256
        and stored.stat().st_mode & 0o077 == 0
        and stored.parent.stat().st_mode & 0o077 == 0,
    )
    baseline_name, executable, extra_commands, executable_spec = resolve_custom_executable(
        store_root=Path(raw) / "runtime",
        operation_id="custom-operation-1",
        definition_sha256=frozen.definition_sha256,
        registry=builtin_registry(),
        policy=policy,
        capabilities=("route:resolved",),
    )
    check(
        "runtime resolves custom execution through the existing baseline",
        baseline_name == "engineering/change"
        and executable.definition_sha256 == frozen.definition_sha256
        and extra_commands == ("make test-harness", "git diff --check")
        and executable_spec == spec,
    )
    tampered = json.loads(stored.read_text(encoding="utf-8"))
    tampered["definition_sha256"] = "f" * 64
    stored.write_text(json.dumps(tampered), encoding="utf-8")
    os.chmod(stored, 0o600)
    try:
        store.load(
            operation_id="custom-operation-1",
            registry=builtin_registry(),
            policy=policy,
            capabilities=("route:resolved",),
        )
    except Exception as exc:
        check("frozen store detects contract tampering", "definition" in str(exc))
    else:
        check("frozen store detects contract tampering", False)


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


def add_uncontrolled_loop(value: dict[str, object]) -> None:
    value["steps"][0]["output_schema"] = "approved-plan/v1"
    value["steps"][1]["input_schema"] = "approved-plan/v1"
    value["transitions"].append(
        {
            "from_step": "implement",
            "outcome": "retry",
            "target": "implement",
            "max_traversals": 2,
        }
    )


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
renamed = {"implement": "write-code", "verify": "run-checks", "review": "inspect"}
for step in equivalent["steps"]:
    step["step_id"] = renamed[step["step_id"]]
for transition in equivalent["transitions"]:
    transition["from_step"] = renamed[transition["from_step"]]
    if not transition["target"].startswith("terminal:"):
        transition["target"] = renamed[transition["target"]]
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

minimal_authority = deepcopy(VALID)
minimal_authority["requested_permissions"] = []
minimal_authority["requested_side_effects"] = []
minimal_spec = parse_pipeline_spec(minimal_authority)
minimal_compiled = compile_custom_spec(
    minimal_spec,
    builtin_registry(),
    policy=policy,
    capabilities=("route:resolved",),
)
minimal_card = render_custom_approval(
    minimal_spec,
    minimal_compiled,
    policy=policy,
)
check(
    "omitted authority cannot hide the effective harness envelope",
    set(minimal_compiled.definition.permission_ceiling)
    == set(policy.permission_ceiling)
    and set(minimal_compiled.definition.side_effects)
    == set(policy.side_effect_ceiling)
    and "Declared permissions: none" in minimal_card
    and "Effective permissions: callback:" in minimal_card,
)

expect_rejection(
    "custom provider restart authority is globally capped at one",
    lambda value: value["budget"].__setitem__("model_restart_limit", 2),
    "budget ceiling",
)

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
    "unknown verification checks fail closed",
    lambda value: value["verification_checks"].append("invented-check"),
    "unregistered verification check",
)
expect_rejection(
    "permission expansion fails closed",
    lambda value: value["requested_permissions"].append("network"),
    "permission ceiling",
)
expect_rejection(
    "policy-only permission claims are unavailable to custom specs",
    lambda value: value["requested_permissions"].append("cmux-target"),
    "permission ceiling",
)
expect_rejection(
    "policy-only side effects are unavailable to custom specs",
    lambda value: value["requested_side_effects"].append("cmux-surface"),
    "side-effect ceiling",
)
expect_rejection(
    "model-authored provider routes fail closed",
    lambda value: value.__setitem__("route_alias", "arbitrary-provider"),
    "route alias",
)
expect_rejection(
    "unavailable route capabilities fail before launch",
    lambda value: value["required_capabilities"].append("network:open"),
    "capability ceiling",
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
    "backward transitions require an explicit bounded-loop control",
    add_uncontrolled_loop,
    "bounded_loop",
)
expect_rejection(
    "transition traversal bombs fail closed",
    lambda value: value["transitions"][0].__setitem__("max_traversals", 99),
    "traversal limit",
)
expect_rejection(
    "filesystem paths cannot hide in context pointers",
    lambda value: value["context_pointers"][0].__setitem__("path", "/tmp/prompt"),
    "unknown fields",
)

print("\nAll custom pipeline tests passed.")
