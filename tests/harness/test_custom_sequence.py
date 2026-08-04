#!/usr/bin/env python3
"""Hermetic checks for custom sequential model-step execution."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import OperationSpec, RuntimeRoute
from harness.custom_pipelines import parse_pipeline_spec
from harness.store import OperationStore
from harness.workflows.custom_sequence import (
    CustomSequenceError,
    accept_custom_step,
    custom_step_envelope,
    custom_step_request,
    load_custom_receipt,
    prepare_custom_step,
    reconcile_custom_sequence,
)

failures: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"ok - {name}")
    else:
        failures.append(name)
        print(f"not ok - {name}: {detail}")


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def raw_spec(*, loop: bool = False) -> dict[str, object]:
    transitions: list[dict[str, object]] = [
        {"from_step": "design", "outcome": "complete", "target": "implement", "max_traversals": 3 if loop else 1},
        {"from_step": "implement", "outcome": "complete", "target": "verify", "max_traversals": 1},
        {"from_step": "verify", "outcome": "complete", "target": "review", "max_traversals": 1},
        {"from_step": "review", "outcome": "complete", "target": "terminal:completed", "max_traversals": 1},
    ]
    controls: list[dict[str, object]] = []
    if loop:
        transitions.insert(2, {"from_step": "implement", "outcome": "revise", "target": "design", "max_traversals": 2})
        controls.append({"primitive_id": "bounded_loop", "version": "1.0.0"})
    return {
        "schema_version": 1,
        "spec_id": "custom-sequence",
        "version": "1.0.0",
        "intent": "engineering-change",
        "task_profile": "change",
        "baseline_pipeline": "engineering/change",
        "route_alias": "executor-default",
        "required_capabilities": ["route:resolved"],
        "input_schema": "approved-plan/v1",
        "output_schema": "reap-ready/v1",
        "steps": [
            {"step_id": "design", "primitive_id": "model_step", "primitive_version": "1.0.0", "input_schema": "approved-plan/v1", "output_schema": "approved-plan/v1", "session_mode": "worktree", "semantic_skills": ["dispatch"]},
            {"step_id": "implement", "primitive_id": "model_step", "primitive_version": "1.0.0", "input_schema": "approved-plan/v1", "output_schema": "implementation-result/v1", "session_mode": "parent-child", "semantic_skills": ["tdd"]},
            {"step_id": "verify", "primitive_id": "verify", "primitive_version": "1.0.0", "input_schema": "implementation-result/v1", "output_schema": "verified-result/v1", "session_mode": "verification", "semantic_skills": []},
            {"step_id": "review", "primitive_id": "review", "primitive_version": "1.0.0", "input_schema": "verified-result/v1", "output_schema": "reap-ready/v1", "session_mode": "review", "semantic_skills": ["review"]},
        ],
        "transitions": transitions,
        "controls": controls,
        "budget": {"attempt_limit": 2, "model_restart_limit": 1, "time_budget_seconds": 900, "token_limit": 50000},
        "completion_policy": "attention",
        "requested_permissions": ["git-write", "product-worktree"],
        "requested_side_effects": ["git-write", "worktree"],
        "context_pointers": [],
        "verification_checks": ["diff-check"],
        "review_mode": "simple",
        "human_gates": ["initial-approval"],
        "terminal_outcomes": ["completed", "attention-required"],
    }


def parent(store: OperationStore, definition: str):
    route = RuntimeRoute("codex", "sol", "high", "executor", sha("route"))
    operation = OperationSpec(
        operation_id="custom-parent",
        idempotency_key=sha("parent"),
        kind="dispatch",
        owner_id="custom-owner",
        route=route,
        context_manifest="context.json",
        verification_profile="scoped",
        contract_sha256=definition,
    )
    return store.create(operation, lane_id="custom-lane", run_id="custom-run")


with tempfile.TemporaryDirectory(prefix="custom-sequence.") as raw:
    root = Path(raw)
    store = OperationStore(root / "store")
    definition = sha("custom-definition")
    plan = sha("approved-plan")
    head = "a" * 40
    spec = parse_pipeline_spec(raw_spec(loop=True))
    parent_record = parent(store, definition)

    design = prepare_custom_step(store, parent_record, spec, definition_sha256=definition, approved_plan_sha256=plan, initial_head_sha=head, receipts=())
    request = custom_step_request(design)
    check(
        "first custom visit is a deterministic child with bounded outcomes",
        design.step_id == "design" and design.visit == 0
        and design.allowed_outcomes == ("complete",)
        and design.spec.parent_operation_id == parent_record.spec.operation_id
        and request["workflow_kind"] == "custom"
        and request["allowed_outcomes"] == ["complete"]
        and store.read("custom-owner", design.spec.operation_id).state == "awaiting-callback",
        (design, request),
    )

    design_receipt_path = root / "receipts" / "00-design.json"
    design_receipt = accept_custom_step(
        store,
        design,
        custom_step_envelope(design, outcome="complete", output_pointer="design.md", output_sha256=sha("design-output"), head_sha=head),
        current_head_sha=head,
        receipt_path=design_receipt_path,
    )
    implement = prepare_custom_step(store, parent_record, spec, definition_sha256=definition, approved_plan_sha256=plan, initial_head_sha=head, receipts=(design_receipt,))
    check(
        "next visit binds the accepted receipt and exposes declared decision outcomes",
        implement.step_id == "implement" and implement.visit == 1
        and implement.prior_receipt_sha256 == design_receipt.receipt_sha256
        and implement.allowed_outcomes == ("complete", "revise")
        and load_custom_receipt(design_receipt_path) == design_receipt,
        implement,
    )

    revise_receipt = accept_custom_step(
        store,
        implement,
        custom_step_envelope(implement, outcome="revise", output_pointer="implementation.md", output_sha256=sha("revise-output"), head_sha=head),
        current_head_sha=head,
        receipt_path=root / "receipts" / "01-implement.json",
    )
    looped_design = prepare_custom_step(store, parent_record, spec, definition_sha256=definition, approved_plan_sha256=plan, initial_head_sha=head, receipts=(design_receipt, revise_receipt))
    check(
        "declared backward decision replays as one bounded next visit",
        looped_design.step_id == "design" and looped_design.visit == 2
        and looped_design.spec.operation_id != design.spec.operation_id,
        looped_design,
    )
    try:
        custom_step_envelope(looped_design, outcome="invented", output_pointer="bad.md", output_sha256=sha("bad"), head_sha=head)
    except CustomSequenceError as exc:
        check("undeclared model decisions fail closed", "not allowed" in str(exc), exc)
    else:
        check("undeclared model decisions fail closed", False)

    loop_receipts = [design_receipt, revise_receipt]
    for visit, expected_step, outcome in (
        (2, "design", "complete"),
        (3, "implement", "revise"),
        (4, "design", "complete"),
        (5, "implement", "revise"),
    ):
        round_ = prepare_custom_step(
            store,
            parent_record,
            spec,
            definition_sha256=definition,
            approved_plan_sha256=plan,
            initial_head_sha=head,
            receipts=tuple(loop_receipts),
        )
        check(
            f"loop visit {visit} targets the declared step",
            round_.visit == visit and round_.step_id == expected_step,
            round_,
        )
        loop_receipts.append(
            accept_custom_step(
                store,
                round_,
                custom_step_envelope(
                    round_,
                    outcome=outcome,
                    output_pointer=f"loop-{visit}.md",
                    output_sha256=sha(f"loop-{visit}"),
                    head_sha=head,
                ),
                current_head_sha=head,
                receipt_path=root / "receipts" / f"{visit:02d}.json",
            )
        )
    exhausted = reconcile_custom_sequence(
        parent_record,
        spec,
        definition_sha256=definition,
        approved_plan_sha256=plan,
        initial_head_sha=head,
        receipts=tuple(loop_receipts),
    )
    check(
        "loop traversal exhaustion becomes a typed attention boundary",
        exhausted.action == "attention"
        and exhausted.terminal_outcome == "loop-exhausted"
        and exhausted.prior_receipt == loop_receipts[-1],
        exhausted,
    )

    linear_spec = parse_pipeline_spec(raw_spec())
    linear_store = OperationStore(root / "linear-store")
    linear_parent = parent(linear_store, definition)
    first = prepare_custom_step(linear_store, linear_parent, linear_spec, definition_sha256=definition, approved_plan_sha256=plan, initial_head_sha=head, receipts=())
    first_receipt = accept_custom_step(
        linear_store, first,
        custom_step_envelope(first, outcome="complete", output_pointer="a.md", output_sha256=sha("a"), head_sha=head),
        current_head_sha=head, receipt_path=root / "linear" / "00.json",
    )
    second = prepare_custom_step(linear_store, linear_parent, linear_spec, definition_sha256=definition, approved_plan_sha256=plan, initial_head_sha=head, receipts=(first_receipt,))
    second_receipt = accept_custom_step(
        linear_store, second,
        custom_step_envelope(second, outcome="complete", output_pointer="b.md", output_sha256=sha("b"), head_sha=head),
        current_head_sha=head, receipt_path=root / "linear" / "01.json",
    )
    progress = reconcile_custom_sequence(linear_parent, linear_spec, definition_sha256=definition, approved_plan_sha256=plan, initial_head_sha=head, receipts=(first_receipt, second_receipt))
    check(
        "custom model transport stops at the code-owned verification boundary",
        progress.action == "complete" and progress.completed_steps == ("design", "implement"),
        progress,
    )

if failures:
    raise SystemExit(f"{len(failures)} custom sequence checks failed")
print("\nAll custom sequence tests passed.")
