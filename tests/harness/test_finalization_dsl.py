#!/usr/bin/env python3
"""Additive PipelineSpec v1 and task finalization policy contracts."""

from __future__ import annotations

import json
import sys
import tempfile
import uuid
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.custom_pipelines import (  # noqa: E402
    CustomPipelinePolicy,
    compile_custom_spec,
    parse_pipeline_spec,
    pipeline_spec_payload,
    render_custom_approval,
)
from harness.finalization_policy import AvailabilityEvidence  # noqa: E402
from harness.finalization_ledger import FinalizationLedger  # noqa: E402
from harness.pipeline_builtins import builtin_registry  # noqa: E402
from harness.review_finalization import (  # noqa: E402
    compile_task_finalization_routes,
    reserve_task_finalization_cycle,
    task_finalization_policy,
)
from model_routing_config import load_tracked_config  # noqa: E402


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def compile_spec(value):
    spec = parse_pipeline_spec(value)
    compiled = compile_custom_spec(
        spec,
        builtin_registry(),
        policy=CustomPipelinePolicy.default(),
        capabilities=("route:resolved",),
    )
    return spec, compiled


examples = sorted((ROOT / "examples" / "pipelines").glob("*.json"))
check("at least one committed PipelineSpec example exists", bool(examples))
for path in examples:
    before = path.read_bytes()
    spec, compiled = compile_spec(before)
    check(
        f"legacy example parses without policy rewrite: {path.name}",
        path.read_bytes() == before
        and spec.finalization_policy is None,
    )
    if path.name == "document-project-v1.json":
        check(
            "the committed compatibility fixture keeps its exact compiled hash",
            compiled.definition_sha256
            == "57b16329192f9268a0cceb380c337976d729bea7011d01ee43fb13eaa939491a",
        )

schema = json.loads(
    (ROOT / "schemas" / "pipeline-spec-v1.schema.json").read_text(
        encoding="utf-8"
    )
)
check(
    "PipelineSpec v1 required fields are unchanged by the additive property",
    "finalization_policy" not in schema["required"]
    and "finalization_policy" in schema["properties"],
)

base = json.loads(examples[0].read_text(encoding="utf-8"))
policy_payload = {
    "max_cycles": 5,
    "add_independent_model_after": 3,
    "execution": "ephemeral",
    "primary_route_alias": "finalization-primary",
    "independent_route_alias": "finalization-independent",
}
with_policy = deepcopy(base)
with_policy["finalization_policy"] = deepcopy(policy_payload)
policy_spec, policy_compiled = compile_spec(with_policy)
check(
    "optional finalization policy is canonical and hash-bound",
    pipeline_spec_payload(policy_spec)["finalization_policy"] == policy_payload
    and policy_compiled.definition_sha256
    != "57b16329192f9268a0cceb380c337976d729bea7011d01ee43fb13eaa939491a",
)
approval = render_custom_approval(
    policy_spec,
    policy_compiled,
    policy=CustomPipelinePolicy.default(),
)
check(
    "approval exposes the bounded finalization delta",
    "Finalization: cycles<=5" in approval
    and "independent-after=3" in approval
    and "finalization-primary" in approval
    and "finalization-independent" in approval,
)


def expect_rejection(label: str, mutate, expected: str) -> None:
    value = deepcopy(with_policy)
    mutate(value["finalization_policy"])
    try:
        compile_spec(value)
    except Exception as exc:
        check(label, expected in str(exc))
    else:
        check(label, False)


for label, mutate, expected in (
    (
        "cycle ceiling is code-owned",
        lambda value: value.__setitem__("max_cycles", 6),
        "max_cycles",
    ),
    (
        "independent route cannot start before cycle 4",
        lambda value: value.__setitem__("add_independent_model_after", 2),
        "add_independent_model_after",
    ),
    (
        "finalization execution cannot expand to interactive",
        lambda value: value.__setitem__("execution", "interactive"),
        "ephemeral",
    ),
    (
        "unknown primary alias is rejected before effects",
        lambda value: value.__setitem__("primary_route_alias", "unknown-route"),
        "not registered",
    ),
    (
        "inline model override is not part of the DSL",
        lambda value: value.__setitem__("primary_model", "sol"),
        "unknown fields",
    ),
    (
        "pre-expanded provider arrays are not part of the DSL",
        lambda value: value.__setitem__("providers", ["codex", "claude"]),
        "unknown fields",
    ),
):
    expect_rejection(label, mutate, expected)

config = load_tracked_config(ROOT)
task_meta = {
    "version": 4,
    "finalization_policy": deepcopy(policy_payload),
    "review_policy": {"runtime": "", "model": "", "effort": ""},
}
availability = AvailabilityEvidence(
    route_alias="finalization-independent",
    status="available",
    source="provider-adapter",
    checked_at_epoch=990,
    valid_until_epoch=1_010,
)
task_decision = compile_task_finalization_routes(
    task_meta,
    config=config,
    cycle_number=4,
    independent_permitted=True,
    availability=availability,
    now_epoch=1_000,
)
check(
    "task metadata compiles the same adaptive finalization contract",
    task_finalization_policy(task_meta) == policy_spec.finalization_policy
    and task_decision is not None
    and len(task_decision.routes) == 2,
)
explicit_meta = deepcopy(task_meta)
explicit_meta["review_policy"] = {
    "runtime": "claude",
    "model": "opus",
    "effort": "xhigh",
}
explicit_decision = compile_task_finalization_routes(
    explicit_meta,
    config=config,
    cycle_number=5,
    independent_permitted=True,
    availability=availability,
    now_epoch=1_000,
)
check(
    "task review explicit single-model wins over adaptive expansion",
    explicit_decision is not None
    and len(explicit_decision.routes) == 1
    and explicit_decision.reason == "explicit-single-model",
)
check(
    "historical v4 task metadata remains readable without policy",
    task_finalization_policy(
        {"version": 4, "review_policy": explicit_meta["review_policy"]}
    )
    is None,
)

with tempfile.TemporaryDirectory(prefix="finalization-dsl-ledger.") as raw:
    ledger = FinalizationLedger(
        Path(raw),
        lineage_id=str(uuid.UUID(int=500)),
        origin_task_id=str(uuid.UUID(int=501)),
        plan_sha256="e" * 64,
        outcome_contract_sha256="f" * 64,
    )
    for cycle in range(1, 5):
        reserved = reserve_task_finalization_cycle(
            task_meta,
            ledger=ledger,
            config=config,
            attempt_id=str(uuid.UUID(int=510 + cycle)),
            exact_head=f"{cycle:040x}",
            task_id=str(uuid.UUID(int=520 + cycle)),
            worktree=f"/tmp/finalization-dsl-{cycle}",
            independent_permitted=True,
            availability=availability,
            now_epoch=1_000,
        )
        check(
            f"atomic task reservation selects cycle {cycle} policy",
            reserved is not None
            and reserved.cycle.allowed
            and reserved.cycle.cycle_number == cycle
            and reserved.routes is not None
            and len(reserved.routes.routes) == (1 if cycle <= 3 else 2),
        )
        ledger.record_terminal(
            attempt_id=str(uuid.UUID(int=510 + cycle)),
            terminal_result="changes-requested",
        )

print("\nAll finalization DSL tests passed.")
