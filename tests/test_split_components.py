#!/usr/bin/env python3
"""Behavior and property checks for disabled Split components."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "split"
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import ContractError  # noqa: E402
from harness.split_contracts import (  # noqa: E402
    ChildBudget,
    FrozenSplitBudget,
    JoinSpec,
    ParentContract,
    SplitCandidate,
    build_split_preview,
    manifest_from_dict,
    manifest_to_dict,
    seal_manifest,
)
from harness.split_execution import (  # noqa: E402
    WorkspaceLocality,
    schedule_waves,
)
from harness.split_join import (  # noqa: E402
    ChildReceipt,
    evaluate_join,
)
from harness.split_validation import validate_manifest  # noqa: E402


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
HEAD_1 = "1" * 40
HEAD_2 = "2" * 40
HEAD_3 = "3" * 40
HEAD_4 = "4" * 40
PIPELINE = "engineering/change"
EVIDENCE = (
    "D1-governed-split-preview",
    "D2-eight-rejections",
    "D3-bounded-waves-locality",
    "D4-deterministic-join",
)
NON_GOALS = (
    "Do not activate dispatch.",
    "Do not open child provider effects.",
)


def candidate(
    subplan_id: str,
    evidence_id: str,
    owned_path: str,
    *,
    dependencies: tuple[str, ...] = (),
    independence_proven: bool = True,
) -> SplitCandidate:
    return SplitCandidate(
        subplan_id=subplan_id,
        title=subplan_id.replace("-", " ").title(),
        pipeline=PIPELINE,
        route_alias="task-default",
        owned_paths=(owned_path,),
        evidence_ids=(evidence_id,),
        dependencies=dependencies,
        inherited_non_goals=NON_GOALS,
        budget=ChildBudget(token_limit=1_000, time_budget_seconds=100),
        independence_proven=independence_proven,
    )


def fanout_preview():
    parent = ParentContract(
        plan_sha256=SHA_A,
        outcome_contract_sha256=SHA_B,
        base_sha="0" * 40,
        evidence_ids=EVIDENCE,
        non_goals=NON_GOALS,
    )
    candidates = (
        candidate(
            "contracts",
            EVIDENCE[0],
            "scripts/harness/split_contracts.py",
        ),
        candidate(
            "validation",
            EVIDENCE[1],
            "scripts/harness/split_validation.py",
        ),
        candidate(
            "execution",
            EVIDENCE[2],
            "scripts/harness/split_execution.py",
            dependencies=("contracts", "validation"),
        ),
        candidate(
            "join",
            EVIDENCE[3],
            "scripts/harness/split_join.py",
            dependencies=("execution",),
        ),
    )
    return build_split_preview(
        parent=parent,
        candidates=candidates,
        frozen_budget=FrozenSplitBudget(
            subplan_limit=6,
            max_parallel=2,
            total_token_limit=5_000,
            total_time_budget_seconds=500,
        ),
        requested_max_parallel=2,
        coordination_cost=2,
        parallel_benefit=8,
        fallback_pipeline=PIPELINE,
        fallback_route_alias="task-default",
        join=JoinSpec(),
    )


def validate(manifest):
    return validate_manifest(
        manifest,
        current_plan_sha256=SHA_A,
        current_outcome_contract_sha256=SHA_B,
        registered_pipelines=frozenset({PIPELINE}),
    )


preview = fanout_preview()
manifest = preview.manifest
expected_manifest = json.loads(
    (FIXTURES / "expected-manifest.json").read_text(encoding="utf-8")
)
assert preview.effect_counts == {
    "dispatches": 0,
    "provider_calls": 0,
    "surfaces_created": 0,
    "worktrees_created": 0,
}
assert manifest.subplan_count == 4
assert manifest.max_parallel == 2
assert manifest.selection.mode == "fan-out"
assert tuple(item.subplan_id for item in manifest.subplans) == (
    "contracts",
    "validation",
    "execution",
    "join",
)
assert set().union(*(set(item.evidence_ids) for item in manifest.subplans)) == set(
    EVIDENCE
)
assert all(item.inherited_non_goals == NON_GOALS for item in manifest.subplans)
assert manifest.manifest_sha256 == seal_manifest(
    dataclasses.replace(manifest, manifest_sha256="")
).manifest_sha256
assert manifest_to_dict(manifest) == expected_manifest
round_trip = manifest_from_dict(manifest_to_dict(manifest))
assert round_trip == manifest
unknown = manifest_to_dict(manifest)
unknown["unexpected"] = True
try:
    manifest_from_dict(unknown)
except ContractError:
    pass
else:
    raise AssertionError("unknown manifest field was accepted")
tampered = json.loads(json.dumps(manifest_to_dict(manifest)))
tampered["subplans"][0]["title"] = "Tampered"
try:
    manifest_from_dict(tampered)
except ContractError:
    pass
else:
    raise AssertionError("manifest digest drift was accepted")
print("OK   exact variable-count manifest preview has zero effects")

fallback = build_split_preview(
    parent=manifest.parent,
    candidates=(
        dataclasses.replace(manifest.subplans[0], independence_proven=False),
        dataclasses.replace(manifest.subplans[1], independence_proven=True),
    ),
    frozen_budget=manifest.frozen_budget,
    requested_max_parallel=2,
    coordination_cost=1,
    parallel_benefit=10,
    fallback_pipeline=PIPELINE,
    fallback_route_alias="task-default",
    join=JoinSpec(),
)
assert fallback.manifest.selection.mode == "one-child-fallback"
assert fallback.manifest.selection.reason == "independence-unproven"
assert fallback.manifest.subplan_count == 1
assert fallback.manifest.max_parallel == 1
assert fallback.manifest.subplans[0].evidence_ids == EVIDENCE
assert fallback.manifest.subplans[0].inherited_non_goals == NON_GOALS
assert fallback.effect_counts == preview.effect_counts
print("OK   unproven independence selects one-child fallback")

coordination_fallback = build_split_preview(
    parent=manifest.parent,
    candidates=tuple(manifest.subplans[:2]),
    frozen_budget=manifest.frozen_budget,
    requested_max_parallel=2,
    coordination_cost=10,
    parallel_benefit=10,
    fallback_pipeline=PIPELINE,
    fallback_route_alias="task-default",
    join=JoinSpec(),
)
assert coordination_fallback.manifest.selection.reason == "coordination-cost"
print("OK   coordination cost cannot manufacture fan-out")

accepted = validate(manifest)
assert accepted.accepted and accepted.validated is not None
assert accepted.issue_codes == () and accepted.effects == ()
print("OK   valid manifest produces one effect-free validation capability")


def reseal(**changes):
    return seal_manifest(
        dataclasses.replace(manifest, manifest_sha256="", **changes)
    )


invalid_cases = []
invalid_cases.append(
    (
        "stale-digest",
        validate_manifest(
            manifest,
            current_plan_sha256=SHA_C,
            current_outcome_contract_sha256=SHA_B,
            registered_pipelines=frozenset({PIPELINE}),
        ),
    )
)
invalid_cases.append(
    (
        "uncovered-evidence",
        validate(
            reseal(
                subplans=manifest.subplans[:-1],
                subplan_count=manifest.subplan_count - 1,
            )
        ),
    )
)
overlap = list(manifest.subplans)
overlap[1] = dataclasses.replace(
    overlap[1], owned_paths=overlap[0].owned_paths
)
invalid_cases.append(
    ("overlapping-ownership", validate(reseal(subplans=tuple(overlap))))
)
cyclic = list(manifest.subplans)
cyclic[0] = dataclasses.replace(cyclic[0], dependencies=("join",))
invalid_cases.append(("dependency-cycle", validate(reseal(subplans=tuple(cyclic)))))
invalid_cases.append(("missing-join", validate(reseal(join=None))))
weakened = list(manifest.subplans)
weakened[0] = dataclasses.replace(
    weakened[0], inherited_non_goals=NON_GOALS[:-1]
)
invalid_cases.append(
    ("weakened-non-goal", validate(reseal(subplans=tuple(weakened))))
)
unregistered = list(manifest.subplans)
unregistered[0] = dataclasses.replace(unregistered[0], pipeline="custom/unknown")
invalid_cases.append(
    ("unregistered-pipeline", validate(reseal(subplans=tuple(unregistered))))
)
over_budget = list(manifest.subplans)
over_budget[0] = dataclasses.replace(
    over_budget[0], budget=ChildBudget(token_limit=9_000, time_budget_seconds=100)
)
invalid_cases.append(
    ("budget-exceeded", validate(reseal(subplans=tuple(over_budget))))
)

rejection_fixture = json.loads(
    (FIXTURES / "validation-rejections.json").read_text(encoding="utf-8")
)
assert tuple(name for name, _result in invalid_cases) == tuple(
    item["invalid_class"] for item in rejection_fixture["receipts"]
)
assert rejection_fixture["effect_counts"] == preview.effect_counts
for expected_code, result in invalid_cases:
    assert not result.accepted
    assert result.validated is None
    assert result.issue_codes == (expected_code,), (
        expected_code,
        result.issue_codes,
    )
    assert result.effects == ()
composite = validate_manifest(
    reseal(join=None, subplans=tuple(overlap)),
    current_plan_sha256=SHA_C,
    current_outcome_contract_sha256=SHA_B,
    registered_pipelines=frozenset({PIPELINE}),
)
assert composite.issue_codes == (
    "stale-digest",
    "overlapping-ownership",
    "missing-join",
)
print("OK   all eight invalid classes reject with zero effects")

localities = {
    subplan_id: WorkspaceLocality(
        subplan_id=subplan_id,
        workspace_id=f"workspace-{subplan_id}",
        worktree_path=f"/worktrees/{subplan_id}",
        executor_placement="child-workspace",
        review_placement="child-workspace",
        verification_placement="child-workspace",
    )
    for subplan_id in reversed(tuple(item.subplan_id for item in manifest.subplans))
}
execution = schedule_waves(accepted.validated, localities)
wave_fixture = json.loads(
    (FIXTURES / "waves-locality.json").read_text(encoding="utf-8")
)
assert execution.subplan_count == wave_fixture["subplan_count"]
assert execution.max_parallel == wave_fixture["max_parallel"]
assert tuple(
    tuple(child.subplan_id for child in wave.children) for wave in execution.waves
) == tuple(tuple(wave) for wave in wave_fixture["waves"])
assert all(
    child.locality.workspace_id == f"workspace-{child.subplan_id}"
    and child.locality.executor_placement == "child-workspace"
    and child.locality.review_placement == "child-workspace"
    for wave in execution.waves
    for child in wave.children
)
assert schedule_waves(accepted.validated, dict(reversed(tuple(localities.items())))) == execution
print("OK   ready waves and workspace-locality data are deterministic")

heads = {
    "contracts": HEAD_1,
    "validation": HEAD_2,
    "execution": HEAD_3,
    "join": HEAD_4,
}


def receipts(status_by_id: dict[str, str] | None = None):
    statuses = status_by_id or {}
    return tuple(
        ChildReceipt(
            manifest_sha256=manifest.manifest_sha256,
            base_sha=manifest.parent.base_sha,
            base_ancestor=True,
            subplan_id=item.subplan_id,
            branch=f"task/{item.subplan_id}",
            head_sha=heads[item.subplan_id],
            summary_sha256=SHA_C,
            review_receipt_sha256=SHA_D,
            evidence_ids=item.evidence_ids,
            status=statuses.get(item.subplan_id, "approved"),
        )
        for item in manifest.subplans
    )


ready = evaluate_join(accepted.validated, receipts(), current_heads=heads)
join_fixture = json.loads(
    (FIXTURES / "join-receipts.json").read_text(encoding="utf-8")
)
assert ready.disposition == join_fixture["expected_disposition"]
assert ready.parent_evidence_proven == EVIDENCE
assert tuple(item.subplan_id for item in ready.integration_order) == tuple(
    item.subplan_id for item in manifest.subplans
)
assert tuple(item.head_sha for item in ready.integration_order) == (
    HEAD_1,
    HEAD_2,
    HEAD_3,
    HEAD_4,
)
assert tuple(item.subplan_id for item in ready.integration_order) == tuple(
    item["subplan_id"] for item in join_fixture["receipts"]
)
print("OK   exact approved receipts join in manifest order")

for status in ("attention-required", "failed", "cancelled", "conflict"):
    decision = evaluate_join(
        accepted.validated,
        receipts({"validation": status}),
        current_heads=heads,
    )
    assert decision.disposition == status
    assert decision.integration_order == ()
stale_heads = dict(heads)
stale_heads["execution"] = "9" * 40
assert evaluate_join(
    accepted.validated, receipts(), current_heads=stale_heads
).disposition == "stale-head"
assert evaluate_join(
    accepted.validated,
    tuple(reversed(receipts())),
    current_heads=heads,
).disposition == "receipt-invalid"
wrong_manifest = list(receipts())
wrong_manifest[0] = dataclasses.replace(wrong_manifest[0], manifest_sha256=SHA_C)
assert evaluate_join(
    accepted.validated,
    tuple(wrong_manifest),
    current_heads=heads,
).disposition == "receipt-invalid"
wrong_base = list(receipts())
wrong_base[0] = dataclasses.replace(wrong_base[0], base_sha="9" * 40)
assert evaluate_join(
    accepted.validated,
    tuple(wrong_base),
    current_heads=heads,
).disposition == "receipt-invalid"
unrelated = list(receipts())
unrelated[0] = dataclasses.replace(unrelated[0], base_ancestor=False)
assert evaluate_join(
    accepted.validated,
    tuple(unrelated),
    current_heads=heads,
).disposition == "receipt-invalid"
assert set(join_fixture["stop_dispositions"]) == {
    "attention-required",
    "failed",
    "cancelled",
    "conflict",
    "stale-head",
    "receipt-invalid",
}
print("OK   join stops on attention/failure/cancel/conflict/stale/order drift")

schema = json.loads(
    (ROOT / "schemas" / "split-manifest-v1.schema.json").read_text(encoding="utf-8")
)
assert schema["$schema"].endswith("2020-12/schema")
assert schema["additionalProperties"] is False
assert set(schema["required"]) == set(manifest_to_dict(manifest))
assert schema["properties"]["schema_version"] == {"const": 1}
print("OK   published split schema matches the exact manifest envelope")

request_path = FIXTURES / "preview-request.json"
if request_path.is_file():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "split-preview.py"), str(request_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    cli = json.loads(proc.stdout)
    assert cli["mode"] == "preview"
    assert cli["effects"] == preview.effect_counts
    assert cli["validation"] == {"accepted": True, "issues": []}
    assert manifest_from_dict(cli["manifest"]).subplan_count == 4
    forbidden = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "split-preview.py"), "--dispatch"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert forbidden.returncode != 0
    print("OK   preview facade exposes no dispatch activation path")

print("\nAll disabled Split component tests passed.")
