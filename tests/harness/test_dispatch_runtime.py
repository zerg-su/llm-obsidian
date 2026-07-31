#!/usr/bin/env python3
"""Dispatch clean-cut contract over the generic provider runtime."""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import OperationRecord, OwnedResources, RuntimeRoute
from harness.custom_pipelines import (
    CustomPipelinePolicy,
    ExplicitPipelineApproval,
    compile_custom_spec,
    freeze_custom_pipeline,
    parse_pipeline_spec,
    render_custom_approval,
)
from harness.pipeline_builtins import builtin_registry
from harness.runtime_sessions import RuntimeSessionResult
from harness.store import OperationStore
from harness.workflows.dispatch import (
    DispatchRequest,
    ReviewPolicy,
    start_dispatch,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


class FakeRuntime:
    def __init__(self, root: Path) -> None:
        self.requests: list[object] = []
        self.prepared: list[object] = []
        self.store = OperationStore(root)

    def start(self, request: object, *, on_surface_opened=None) -> object:
        self.requests.append(request)
        record = OperationRecord(
            request.spec,
            "starting",
            2,
            request.lane_id,
            request.run_id,
            OwnedResources(
                "22222222-2222-4222-8222-222222222222"
            ),
        )
        opened = RuntimeSessionResult(
            record,
            "surface-opened",
            surface_ref="surface:2",
            workspace_id="33333333-3333-4333-8333-333333333333",
            workspace_ref="workspace:3",
            window_id="44444444-4444-4444-8444-444444444444",
            window_ref="window:4",
        )
        if on_surface_opened is not None:
            on_surface_opened(opened)
            self.prepared.append(opened)
        return replace(opened, action="started")


with tempfile.TemporaryDirectory() as raw:
    cwd = Path(raw).resolve()
    (cwd / ".task-prompt.md").write_text("execute approved plan\n", encoding="utf-8")
    request = DispatchRequest(
        task_id="11111111-1111-4111-8111-111111111111",
        owner_id="11111111-1111-4111-8111-111111111111",
        plan_sha256="a" * 64,
        context_manifest="wiki/plans/approved.md",
        route=RuntimeRoute(
            "codex",
            "gpt-5.6-sol",
            "high",
            "executor",
            "b" * 64,
        ),
        placement="workspace",
        review=ReviewPolicy(),
    )
    runtime = FakeRuntime(cwd / "runtime-store")
    prepared: list[object] = []
    result = start_dispatch(
        request,
        runtime,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=cwd,
        prompt_pointer=".task-prompt.md",
        summary_pointer=".task-summary.json",
        on_surface_opened=prepared.append,
    )
    session = runtime.requests[0]
    check(
        "dispatch creates one provider-backed task-summary session",
        len(runtime.requests) == 1
        and session.spec.kind == "dispatch"
        and session.callback_mode == "task-summary"
        and session.callback_pointer == ".task-summary.json"
        and session.placement == "workspace",
    )
    check(
        "dispatch lane and run identities are deterministic",
        session.lane_id == result.record.lane_id
        and session.run_id == result.record.run_id,
    )
    check(
        "task metadata hook receives exact container identity before prompt",
        prepared
        and prepared[0].record.resources.surface_id
        == "22222222-2222-4222-8222-222222222222"
        and prepared[0].workspace_id
        == "33333333-3333-4333-8333-333333333333",
    )
    check(
        "task summary transport stays canonical and code-owned",
        session.task_summary_pointer == ".task-summary.json",
    )

    raw_custom = {
        "schema_version": 1,
        "spec_id": "custom-runtime",
        "version": "1.0.0",
        "intent": "engineering-change",
        "task_profile": "change",
        "baseline_pipeline": "engineering/change",
        "input_schema": "approved-plan/v1",
        "output_schema": "reap-ready/v1",
        "steps": [
            {"step_id": "implement", "primitive_id": "model_step", "primitive_version": "1.0.0", "input_schema": "approved-plan/v1", "output_schema": "implementation-result/v1", "session_mode": "worktree", "semantic_skills": ["tdd"]},
            {"step_id": "verify", "primitive_id": "verify", "primitive_version": "1.0.0", "input_schema": "implementation-result/v1", "output_schema": "verified-result/v1", "session_mode": "verification", "semantic_skills": []},
            {"step_id": "review", "primitive_id": "review", "primitive_version": "1.0.0", "input_schema": "verified-result/v1", "output_schema": "reap-ready/v1", "session_mode": "review", "semantic_skills": ["review"]},
        ],
        "transitions": [
            {"from_step": "implement", "outcome": "complete", "target": "verify", "max_traversals": 1},
            {"from_step": "verify", "outcome": "complete", "target": "review", "max_traversals": 1},
            {"from_step": "review", "outcome": "complete", "target": "terminal:completed", "max_traversals": 1},
        ],
        "controls": [],
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
    custom_spec = parse_pipeline_spec(raw_custom)
    custom_policy = CustomPipelinePolicy.default()
    compiled = compile_custom_spec(
        custom_spec,
        builtin_registry(),
        policy=custom_policy,
        capabilities=("route:resolved",),
    )
    card = render_custom_approval(custom_spec, compiled, policy=custom_policy)
    approval = ExplicitPipelineApproval.for_card(
        definition_sha256=compiled.definition_sha256,
        approval_card=card,
        actor="user",
        decision="approve",
    )
    frozen = freeze_custom_pipeline(custom_spec, compiled, approval, card)
    custom_runtime = FakeRuntime(cwd / "custom-runtime-store")
    custom_request = DispatchRequest(
        task_id="55555555-5555-4555-8555-555555555555",
        owner_id="55555555-5555-4555-8555-555555555555",
        plan_sha256="c" * 64,
        context_manifest="wiki/plans/custom.md",
        route=request.route,
        placement="workspace",
        review=ReviewPolicy(),
        pipeline_name="custom",
        custom_pipeline=frozen,
    )
    start_dispatch(
        custom_request,
        custom_runtime,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=cwd,
        initial_head_sha="d" * 40,
    )
    custom_session = custom_runtime.requests[0]
    step_request = (cwd / ".task-pipeline-step-request.json").read_text(
        encoding="utf-8"
    )
    check(
        "custom dispatch freezes before launch and targets its first typed child",
        custom_session.callback_pointer == ".task-pipeline-step-callback.json"
        and "-custom-0-" in custom_session.initial_callback_operation_id
        and custom_session.initial_callback_operation_id != custom_request.task_id
        and '"workflow_kind": "custom"' in step_request
        and '"step_id": "implement"' in step_request,
    )
