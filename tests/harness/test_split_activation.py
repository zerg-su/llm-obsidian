#!/usr/bin/env python3
"""Activated Split wiring stays bounded, child-local, and receipt-driven."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.split_activation import (  # noqa: E402
    SplitDispatchBinding,
    SplitLaunchReceipt,
    SplitTerminalReceipt,
    compile_activation,
    drive_split,
    join_split,
    split_child_policy,
    split_child_policy_payload,
)
from harness.split_contracts import (  # noqa: E402
    ChildBudget,
    FrozenSplitBudget,
    JoinSpec,
    ParentContract,
    SplitCandidate,
    build_split_preview,
    manifest_to_dict,
    seal_manifest,
)
from harness.split_join import ChildReceipt  # noqa: E402
from outcome_contract import extract_from_bytes  # noqa: E402
from split_dispatch import (  # noqa: E402
    DispatchChildRequest,
    drive_split_dispatch,
    prepare_split_dispatch,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
PIPELINE = "engineering/change"
EVIDENCE = ("split-contracts", "split-validation", "split-execution", "split-join")
NON_GOALS = ("No release effects.", "No permission expansion.")
IDS = ("contracts", "validation", "execution", "join")
HEADS = {subplan_id: str(index) * 40 for index, subplan_id in enumerate(IDS, 1)}


def candidate(
    subplan_id: str,
    evidence_id: str,
    *,
    dependencies: tuple[str, ...] = (),
) -> SplitCandidate:
    return SplitCandidate(
        subplan_id=subplan_id,
        title=subplan_id.title(),
        pipeline=PIPELINE,
        route_alias="task-default",
        owned_paths=(f"product/{subplan_id}.py",),
        evidence_ids=(evidence_id,),
        dependencies=dependencies,
        inherited_non_goals=NON_GOALS,
        budget=ChildBudget(token_limit=1_000, time_budget_seconds=100),
        independence_proven=True,
    )


parent = ParentContract(
    plan_sha256=SHA_A,
    outcome_contract_sha256=SHA_B,
    base_sha="0" * 40,
    evidence_ids=EVIDENCE,
    non_goals=NON_GOALS,
)
manifest = build_split_preview(
    parent=parent,
    candidates=(
        candidate("contracts", EVIDENCE[0]),
        candidate("validation", EVIDENCE[1]),
        candidate("execution", EVIDENCE[2], dependencies=("contracts", "validation")),
        candidate("join", EVIDENCE[3], dependencies=("execution",)),
    ),
    frozen_budget=FrozenSplitBudget(
        subplan_limit=4,
        max_parallel=2,
        total_token_limit=4_000,
        total_time_budget_seconds=400,
    ),
    requested_max_parallel=2,
    coordination_cost=1,
    parallel_benefit=5,
    fallback_pipeline=PIPELINE,
    fallback_route_alias="task-default",
    join=JoinSpec(),
).manifest


def bindings() -> tuple[SplitDispatchBinding, ...]:
    return tuple(
        SplitDispatchBinding(
            manifest_sha256=manifest.manifest_sha256,
            base_sha=manifest.parent.base_sha,
            subplan_id=item.subplan_id,
            request_id=f"request-{item.subplan_id}",
            pipeline=item.pipeline,
            route_alias=item.route_alias,
            worktree_path=f"/worktrees/{item.subplan_id}",
            placement="workspace",
            budget=item.budget,
        )
        for item in manifest.subplans
    )


activation = compile_activation(
    manifest,
    current_plan_sha256=SHA_A,
    current_outcome_contract_sha256=SHA_B,
    registered_pipelines={PIPELINE},
    bindings=bindings(),
)
assert activation.accepted and activation.validated is not None
assert tuple(tuple(child.subplan_id for child in wave.children) for wave in activation.execution.waves) == (
    ("contracts", "validation"),
    ("execution",),
    ("join",),
)
assert all(
    child.locality.executor_placement == "child-workspace"
    and child.locality.review_placement == "child-workspace"
    and child.locality.verification_placement == "child-workspace"
    for wave in activation.execution.waves
    for child in wave.children
)
print("OK   activated Split compiles exact workspace-local waves")


launch_calls: list[str] = []


def launch(binding: SplitDispatchBinding) -> SplitLaunchReceipt:
    launch_calls.append(binding.subplan_id)
    return SplitLaunchReceipt(
        manifest_sha256=binding.manifest_sha256,
        base_sha=binding.base_sha,
        subplan_id=binding.subplan_id,
        request_id=binding.request_id,
        workspace_id=f"workspace-{binding.subplan_id}",
        worktree_path=binding.worktree_path,
        surface_id=f"surface-{binding.subplan_id}",
        placement="workspace",
    )


first = drive_split(activation, terminal_receipts=(), launch_receipts=(), launch=launch)
assert first.disposition == "awaiting-children"
assert launch_calls == ["contracts", "validation"]
assert tuple(item.subplan_id for item in first.launch_receipts) == (
    "contracts",
    "validation",
)

replayed = drive_split(
    activation,
    terminal_receipts=(),
    launch_receipts=first.launch_receipts,
    launch=launch,
)
assert replayed.disposition == "awaiting-children"
assert replayed.launch_receipts == first.launch_receipts
assert launch_calls == ["contracts", "validation"]
print("OK   first wave is bounded and exact launch receipts prevent replay")


def child_receipt(subplan_id: str, status: str = "approved") -> ChildReceipt:
    item = next(item for item in manifest.subplans if item.subplan_id == subplan_id)
    return ChildReceipt(
        manifest_sha256=manifest.manifest_sha256,
        base_sha=manifest.parent.base_sha,
        base_ancestor=True,
        subplan_id=subplan_id,
        branch=f"task/{subplan_id}",
        head_sha=HEADS[subplan_id],
        summary_sha256=SHA_C,
        review_receipt_sha256=SHA_D,
        evidence_ids=item.evidence_ids,
        status=status,
    )


def terminal(subplan_id: str, status: str = "approved") -> SplitTerminalReceipt:
    launched = next(item for item in all_launches if item.subplan_id == subplan_id)
    return SplitTerminalReceipt(
        child=child_receipt(subplan_id, status),
        request_id=launched.request_id,
        workspace_id=launched.workspace_id,
        worktree_path=launched.worktree_path,
        executor_placement="child-workspace",
        review_placement="child-workspace",
        verification_placement="child-workspace",
        resources_closed=True,
    )


first_terminals = tuple(
    SplitTerminalReceipt(
        child=child_receipt(item.subplan_id),
        request_id=item.request_id,
        workspace_id=item.workspace_id,
        worktree_path=item.worktree_path,
        executor_placement="child-workspace",
        review_placement="child-workspace",
        verification_placement="child-workspace",
        resources_closed=True,
    )
    for item in first.launch_receipts
)
second = drive_split(
    activation,
    terminal_receipts=first_terminals,
    launch_receipts=first.launch_receipts,
    launch=launch,
)
assert launch_calls == ["contracts", "validation", "execution"]
assert tuple(item.subplan_id for item in second.launch_receipts) == (
    "contracts",
    "validation",
    "execution",
)
execution_terminal = SplitTerminalReceipt(
    child=child_receipt("execution"),
    request_id=second.launch_receipts[-1].request_id,
    workspace_id=second.launch_receipts[-1].workspace_id,
    worktree_path=second.launch_receipts[-1].worktree_path,
    executor_placement="child-workspace",
    review_placement="child-workspace",
    verification_placement="child-workspace",
    resources_closed=True,
)
third = drive_split(
    activation,
    terminal_receipts=(*first_terminals, execution_terminal),
    launch_receipts=second.launch_receipts,
    launch=launch,
)
assert launch_calls[-1] == "join"
all_launches = third.launch_receipts
all_terminals = (*first_terminals, execution_terminal, terminal("join"))
complete = drive_split(
    activation,
    terminal_receipts=all_terminals,
    launch_receipts=all_launches,
    launch=launch,
)
assert complete.disposition == "ready-to-join"
assert launch_calls == ["contracts", "validation", "execution", "join"]
print("OK   dependency waves advance only from exact approved terminal receipts")


joined = join_split(
    activation,
    launch_receipts=all_launches,
    terminal_receipts=all_terminals,
    current_heads=HEADS,
)
assert joined.disposition == "ready"
assert tuple(item.subplan_id for item in joined.integration_order) == IDS

unclean = list(all_terminals)
unclean[-1] = dataclasses.replace(unclean[-1], resources_closed=False)
assert join_split(
    activation,
    launch_receipts=all_launches,
    terminal_receipts=tuple(unclean),
    current_heads=HEADS,
).disposition == "attention-required"
print("OK   deterministic join requires exact receipts and resource-free children")


stopped_calls: list[str] = []
attention = dataclasses.replace(
    first_terminals[1], child=child_receipt("validation", "attention-required")
)
stopped = drive_split(
    activation,
    terminal_receipts=(first_terminals[0], attention),
    launch_receipts=first.launch_receipts,
    launch=lambda binding: stopped_calls.append(binding.subplan_id),
)
assert stopped.disposition == "attention-required"
assert stopped_calls == []
print("OK   terminal child attention stops later waves with zero new effects")


invalid_manifests = []
invalid_manifests.append(("stale-digest", manifest, SHA_D, SHA_B, {PIPELINE}))
invalid_manifests.append(
    (
        "uncovered-evidence",
        seal_manifest(
            dataclasses.replace(
                manifest,
                manifest_sha256="",
                subplans=manifest.subplans[:-1],
                subplan_count=manifest.subplan_count - 1,
            )
        ),
        SHA_A,
        SHA_B,
        {PIPELINE},
    )
)
overlap = list(manifest.subplans)
overlap[1] = dataclasses.replace(overlap[1], owned_paths=overlap[0].owned_paths)
invalid_manifests.append(
    ("overlapping-ownership", seal_manifest(dataclasses.replace(manifest, manifest_sha256="", subplans=tuple(overlap))), SHA_A, SHA_B, {PIPELINE})
)
cycle = list(manifest.subplans)
cycle[0] = dataclasses.replace(cycle[0], dependencies=("join",))
invalid_manifests.append(
    ("dependency-cycle", seal_manifest(dataclasses.replace(manifest, manifest_sha256="", subplans=tuple(cycle))), SHA_A, SHA_B, {PIPELINE})
)
invalid_manifests.append(
    ("missing-join", seal_manifest(dataclasses.replace(manifest, manifest_sha256="", join=None)), SHA_A, SHA_B, {PIPELINE})
)
weakened = list(manifest.subplans)
weakened[0] = dataclasses.replace(weakened[0], inherited_non_goals=NON_GOALS[:-1])
invalid_manifests.append(
    ("weakened-non-goal", seal_manifest(dataclasses.replace(manifest, manifest_sha256="", subplans=tuple(weakened))), SHA_A, SHA_B, {PIPELINE})
)
unknown = list(manifest.subplans)
unknown[0] = dataclasses.replace(unknown[0], pipeline="unknown/pipeline")
invalid_manifests.append(
    ("unregistered-pipeline", seal_manifest(dataclasses.replace(manifest, manifest_sha256="", subplans=tuple(unknown))), SHA_A, SHA_B, {PIPELINE})
)
expensive = list(manifest.subplans)
expensive[0] = dataclasses.replace(expensive[0], budget=ChildBudget(9_000, 100))
invalid_manifests.append(
    ("budget-exceeded", seal_manifest(dataclasses.replace(manifest, manifest_sha256="", subplans=tuple(expensive))), SHA_A, SHA_B, {PIPELINE})
)
for expected, invalid, plan_sha, outcome_sha, pipelines in invalid_manifests:
    rejected = compile_activation(
        invalid,
        current_plan_sha256=plan_sha,
        current_outcome_contract_sha256=outcome_sha,
        registered_pipelines=pipelines,
        bindings=bindings(),
    )
    assert not rejected.accepted
    assert rejected.issue_codes == (expected,)
    effects: list[str] = []
    result = drive_split(
        rejected,
        terminal_receipts=(),
        launch_receipts=(),
        launch=lambda binding: effects.append(binding.subplan_id),
    )
    assert result.disposition == "rejected"
    assert effects == []
print("OK   all eight validation classes remain zero-effect after activation")


dispatch_candidates = tuple(
    dataclasses.replace(
        item,
        budget=ChildBudget(token_limit=200_000, time_budget_seconds=1_800),
    )
    for item in manifest.subplans
)
dispatch_manifest = seal_manifest(
    dataclasses.replace(
        manifest,
        manifest_sha256="",
        subplans=dispatch_candidates,
        frozen_budget=FrozenSplitBudget(
            subplan_limit=4,
            max_parallel=2,
            total_token_limit=800_000,
            total_time_budget_seconds=7_200,
        ),
    )
)
dispatch_children = tuple(
    DispatchChildRequest(
        subplan_id=item.subplan_id,
        request_sha256=chr(ord("a") + index) * 64,
        request={
            "request_id": f"dispatch-{item.subplan_id}",
            "pipeline": item.pipeline,
            "placement": "workspace",
            "worktree": Path(f"/worktrees/dispatch-{item.subplan_id}"),
            "base_sha": dispatch_manifest.parent.base_sha,
            "split": split_child_policy_payload(
                split_child_policy(dispatch_manifest, item)
            ),
        },
    )
    for index, item in enumerate(dispatch_manifest.subplans)
)
prepared = prepare_split_dispatch(
    dispatch_manifest,
    current_plan_sha256=SHA_A,
    current_outcome_contract_sha256=SHA_B,
    registered_pipelines={PIPELINE},
    children=dispatch_children,
)
dispatch_effects: list[str] = []


def existing_dispatch(request, request_sha256):
    dispatch_effects.append(str(request["request_id"]))
    subplan_id = str(request["split"]["subplan_id"])
    return {
        "request_id": request["request_id"],
        "task_workspace": f"workspace-{subplan_id}",
        "task_surface": f"surface-{subplan_id}",
        "worktree": str(request["worktree"]),
        "placement": request["placement"],
        "request_sha256": request_sha256,
    }


dispatch_first = drive_split_dispatch(
    prepared,
    terminal_receipts=(),
    launch_receipts=(),
    start_dispatch=existing_dispatch,
)
assert dispatch_effects == ["dispatch-contracts", "dispatch-validation"]
assert all(item.placement == "workspace" for item in dispatch_first.launch_receipts)
assert tuple(item.workspace_id for item in dispatch_first.launch_receipts) == (
    "workspace-contracts",
    "workspace-validation",
)

under_budget = list(dispatch_children)
under_budget[0] = dataclasses.replace(
    under_budget[0],
    request={
        **dict(under_budget[0].request),
        "split": {
            **dict(under_budget[0].request["split"]),
            "budget": {"token_limit": 1_000, "time_budget_seconds": 100},
        },
    },
)
try:
    prepare_split_dispatch(
        dispatch_manifest,
        current_plan_sha256=SHA_A,
        current_outcome_contract_sha256=SHA_B,
        registered_pipelines={PIPELINE},
        children=tuple(under_budget),
    )
except Exception as exc:
    assert "policy drifted" in str(exc) or "exceeds" in str(exc)
else:
    raise AssertionError("under-budget child dispatch reached the effect adapter")
assert dispatch_effects == ["dispatch-contracts", "dispatch-validation"]
print("OK   activated waves use existing workspace dispatch results and frozen budgets")

drifted_base = list(dispatch_children)
drifted_base[0] = dataclasses.replace(
    drifted_base[0],
    request={
        **dict(drifted_base[0].request),
        "base_sha": "9" * 40,
    },
)
try:
    prepare_split_dispatch(
        dispatch_manifest,
        current_plan_sha256=SHA_A,
        current_outcome_contract_sha256=SHA_B,
        registered_pipelines={PIPELINE},
        children=tuple(drifted_base),
    )
except Exception as exc:
    assert "base SHA drifted" in str(exc)
else:
    raise AssertionError("drifted child base reached Split activation")
assert dispatch_effects == ["dispatch-contracts", "dispatch-validation"]
print("OK   child request base drift fails before any dispatch effect")


with tempfile.TemporaryDirectory(prefix="split-runner.") as raw_tmp:
    temporary = Path(raw_tmp)
    target_repo = temporary / "target"
    target_repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=target_repo,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "split-test@example.invalid"],
        cwd=target_repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Split Test"],
        cwd=target_repo,
        check=True,
    )
    (target_repo / "marker.txt").write_text("sealed\n", encoding="utf-8")
    subprocess.run(["git", "add", "marker.txt"], cwd=target_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "sealed base"],
        cwd=target_repo,
        text=True,
        capture_output=True,
        check=True,
    )
    sealed_base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=target_repo, text=True
    ).strip()
    plan_path = (
        ROOT
        / "wiki/plans/2026-08-05-113349-llm-obsidian-2-6-5-event-driven-lifecycle-bounded-finalizati.md"
    )
    cli_parent = ParentContract(
        plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        outcome_contract_sha256=extract_from_bytes(plan_path.read_bytes()).sha256,
        base_sha=sealed_base,
        evidence_ids=("split-dogfood-proof",),
        non_goals=("No publish.",),
    )
    cli_candidate = SplitCandidate(
        subplan_id="whole-plan",
        title="Bounded Split dogfood",
        pipeline="lifecycle/default",
        route_alias="task-default",
        owned_paths=("docs/acceptance/split-dogfood.md",),
        evidence_ids=cli_parent.evidence_ids,
        dependencies=(),
        inherited_non_goals=cli_parent.non_goals,
        budget=ChildBudget(200_000, 1_800),
        independence_proven=True,
    )
    cli_manifest = build_split_preview(
        parent=cli_parent,
        candidates=(cli_candidate,),
        frozen_budget=FrozenSplitBudget(1, 1, 200_000, 1_800),
        requested_max_parallel=1,
        coordination_cost=0,
        parallel_benefit=1,
        fallback_pipeline="lifecycle/default",
        fallback_route_alias="task-default",
        join=JoinSpec(),
    ).manifest
    child_id = str(uuid.UUID("11111111-1111-4111-8111-111111111111"))
    raw_dispatch = {
        "schema_version": 1,
        "request_id": child_id,
        "task_name": "split-dogfood",
        "description": "Produce one bounded local Split acceptance artifact.",
        "vault_root": str(ROOT),
        "target_repo": str(target_repo),
        "worktree": str(temporary / "split-dogfood"),
        "branch": "task/split-dogfood",
        "base_branch": "HEAD",
        "plan_file": str(plan_path),
        "origin_surface": "22222222-2222-4222-8222-222222222222",
        "origin_session": "split-runner-test",
        "session_route": {
            "runtime": "codex",
            "model": "gpt-5.6-sol",
            "effort": "high",
            "source": "unit-test",
        },
        "executor": {},
        "placement": "workspace",
        "pipeline": "lifecycle/default",
        "wiki_context": [],
        "suggested_agents": [],
        "reap": {
            "type": "repo-touch",
            "title": "Split dogfood",
            "plan_mode": "shared",
        },
        "split": split_child_policy_payload(
            split_child_policy(cli_manifest, cli_manifest.subplans[0])
        ),
    }
    activation_spec = {
        "schema_version": 1,
        "manifest": manifest_to_dict(cli_manifest),
        "current_parent": {
            "plan_sha256": cli_parent.plan_sha256,
            "outcome_contract_sha256": cli_parent.outcome_contract_sha256,
        },
        "registered_pipelines": ["lifecycle/default"],
        "children": [{"subplan_id": "whole-plan", "dispatch": raw_dispatch}],
    }
    spec_path = temporary / "activation.json"
    spec_path.write_text(json.dumps(activation_spec), encoding="utf-8")
    cli = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/split-runner.py"),
            "validate",
            "--spec",
            str(spec_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert cli.returncode == 0, cli.stderr
    cli_payload = json.loads(cli.stdout)
    assert cli_payload["status"] == "valid"
    assert cli_payload["waves"] == [["whole-plan"]]
    assert cli_payload["effects"] == {
        "dispatches": 0,
        "provider_calls": 0,
        "surfaces_created": 0,
        "worktrees_created": 0,
    }
    assert not (temporary / "split-dogfood").exists()

    (target_repo / "marker.txt").write_text("moved\n", encoding="utf-8")
    subprocess.run(["git", "add", "marker.txt"], cwd=target_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "move source branch"],
        cwd=target_repo,
        text=True,
        capture_output=True,
        check=True,
    )
    moved = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/split-runner.py"),
            "validate",
            "--spec",
            str(spec_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert moved.returncode == 3, f"stdout={moved.stdout!r} stderr={moved.stderr!r}"
    assert "base SHA drifted" in moved.stderr
    assert not (temporary / "split-dogfood").exists()
print("OK   public Split activation validation stays zero-effect")


print("\nAll activated Split integration tests passed.")
