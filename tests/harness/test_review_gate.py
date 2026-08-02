#!/usr/bin/env python3
"""Production-seam regressions for the automatic review gate."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import shlex
import subprocess
import tempfile
import uuid
from contextlib import redirect_stdout
from dataclasses import dataclass, replace
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT / "scripts"))

from harness.callbacks import CallbackBroker
from harness import cli as harness_cli
from harness.adapters.claude import ClaudeDriver
from harness.contracts import (
    AttentionReason,
    EffectOutcome,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
    to_dict,
)
from harness.state_machine import TERMINAL
from harness.store import OperationStore
from harness.verification import load_profiles
from harness.runtime_worker import _pipeline_verify_identity
from harness.custom_pipelines import (
    CustomPipelinePolicy,
    ExplicitPipelineApproval,
    FrozenPipelineStore,
    compile_custom_spec,
    freeze_custom_pipeline,
    parse_pipeline_spec,
    render_custom_approval,
)
from harness.pipeline_builtins import builtin_registry, compiled_builtin
from harness.workflows.review import (
    ReviewContext,
    ReviewFinding,
    ReviewOperationRequest,
    ReviewResult,
    review_round_envelope,
    start_review,
)
from harness.workflows.review_gate import (
    ReviewGateController,
    ReviewPreset,
    ReviewScopeBoundary,
    authorize_task_finalization,
    review_context_sha256,
)
from review_resolution import (
    FindingResolution,
    ReviewResolutionEvidence,
    review_transport_identity_sha256,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


regression_failures: list[str] = []


def resolution_evidence(
    operation_id: str,
    axis: str,
    reviewed_head: str,
    resolved_head: str,
    *finding_ids: str,
) -> ReviewResolutionEvidence:
    rows = {
        finding_id: FindingResolution(
            finding_id,
            "applied",
            "The fix and its regression check are present on the resolved HEAD.",
        )
        for finding_id in finding_ids
    }
    return ReviewResolutionEvidence(
        operation_id,
        axis,
        reviewed_head,
        resolved_head,
        hashlib.sha256(b"bounded fix delta").hexdigest(),
        tuple(finding_ids),
        rows,
    )


def resolution_transport_identity(
    controller: ReviewGateController,
) -> str:
    state = controller.read()
    persisted = str(
        state.get("resolution_transport_identity_sha256") or ""
    )
    if persisted:
        return persisted
    awaiting = state["awaiting_resolution"]
    review_operation_ids = {
        str(boundary["review_operation_id"])
        for boundary in awaiting.values()
    }
    assert len(review_operation_ids) == 1
    callbacks = [
        {
            "axis": axis,
            "round_operation_id": boundary["round_operation_id"],
            "round_run_id": boundary["round_run_id"],
            "callback_id": boundary["callback_id"],
            "callback_sha256": boundary["callback_sha256"],
        }
        for axis, boundary in sorted(awaiting.items())
    ]
    return review_transport_identity_sha256(
        next(iter(review_operation_ids)), callbacks
    )


simple_preset = ReviewPreset.from_flags()
deep_cross = ReviewPreset.from_flags(deep=True, cross_model=True)
override = ReviewPreset.from_flags(model="opus", effort="high")
disabled = ReviewPreset.from_flags(no_review=True)
check(
    "review presets are deterministic",
    simple_preset.depth == "simple"
    and simple_preset.max_verify_iterations == 1
    and deep_cross.depth == "deep"
    and deep_cross.cross_model
    and deep_cross.max_verify_iterations == 2
    and override.model == "opus"
    and override.effort == "high"
    and not disabled.enabled,
)
try:
    ReviewPreset.from_flags(deep=True, no_review=True)
except ValueError:
    check("no-review cannot hide an enabled preset", True)
else:
    check("no-review cannot hide an enabled preset", False)


@dataclass(frozen=True)
class FakeSessionResult:
    record: object
    checkpoint: str
    action: str = ""
    process_status: str = ""
    surface_status: str = ""


class FakeRuntime:
    def __init__(self, store: OperationStore) -> None:
        self.store = store
        self.started: list[object] = []
        self.continued: list[tuple[str, str, str, str]] = []
        self.registered: list[tuple[str, str, str, str, str]] = []
        self.exits: list[tuple[str, str]] = []
        self.cleanups: list[tuple[str, str]] = []
        self.cleanup_attention = False
        self.cleanup_terminate_once = False

    def start(self, request: object, *, on_surface_opened=None) -> FakeSessionResult:
        self.started.append(request)
        record = self.store.create(
            request.spec, lane_id=request.lane_id, run_id=request.run_id
        )
        if not record.resources.surface_id:
            record = replace(
                record,
                resources=replace(
                    record.resources,
                    surface_id=(
                        f"{len(self.started):08d}-AAAA-4AAA-8AAA-"
                        f"{len(self.started):012d}"
                    ),
                ),
            )
            self.store.save(record, expected_revision=record.revision)
        result = FakeSessionResult(record, f"checkpoint-{len(self.started)}")
        if on_surface_opened is not None:
            on_surface_opened(result)
        return result

    def status(self, owner_id: str, operation_id: str) -> object:
        return FakeSessionResult(
            self.store.read(owner_id, operation_id),
            "checkpoint-live",
            "observed",
            "alive",
            "alive",
        )

    def continue_session(
        self,
        owner_id: str,
        operation_id: str,
        checkpoint: str,
        prompt_pointer: str,
    ) -> FakeSessionResult:
        self.continued.append(
            (owner_id, operation_id, checkpoint, prompt_pointer)
        )
        return FakeSessionResult(
            self.store.read(owner_id, operation_id), checkpoint
        )

    def register_callback_target(
        self,
        owner_id: str,
        parent_operation_id: str,
        child_operation_id: str,
        child_run_id: str,
        callback_pointer: str,
    ) -> None:
        self.registered.append(
            (
                owner_id,
                parent_operation_id,
                child_operation_id,
                child_run_id,
                callback_pointer,
            )
        )

    def accept_callback(self, envelope: object) -> object:
        matches = [
            path.parents[1].name
            for path in (
                self.store.root / "owners"
            ).glob(
                f"*/operations/{envelope.operation_id}.json"
            )
        ]
        if len(matches) != 1:
            raise AssertionError("fake runtime callback owner is ambiguous")
        return CallbackBroker(self.store, matches[0]).accept(envelope)

    def request_exit(self, owner_id: str, operation_id: str) -> None:
        self.exits.append((owner_id, operation_id))
        record = self.store.read(owner_id, operation_id)
        if record.state in {"complete", "failed", "cancelled"}:
            return record
        if record.state in {
            "created",
            "preflight",
            "starting",
            "attention-required",
        }:
            self.store.transition(
                owner_id, operation_id, "cancelling"
            )
        elif record.state != "finalizing":
            self.store.transition(
                owner_id, operation_id, "finalizing"
            )
        self.store.transition(owner_id, operation_id, "exiting")
        return self.store.read(owner_id, operation_id)

    def cleanup(self, owner_id: str, operation_id: str) -> None:
        self.cleanups.append((owner_id, operation_id))
        if self.cleanup_terminate_once:
            self.cleanup_terminate_once = False
            return FakeSessionResult(
                self.store.read(owner_id, operation_id),
                "",
                "terminate-orphan",
            )
        if self.cleanup_attention:
            self.store.transition(
                owner_id,
                operation_id,
                "attention-required",
                reason=AttentionReason.CLEANUP_INCOMPLETE,
            )
        else:
            self.store.transition(owner_id, operation_id, "complete")
        return self.store.read(owner_id, operation_id)


def request_for(
    operation_id: str,
    *,
    depth: str = "simple",
    context: ReviewContext,
) -> ReviewOperationRequest:
    primary = RuntimeRoute(
        "claude",
        "fable",
        "xhigh" if depth == "deep" else "high",
        "reviewer-callback",
        "a" * 64,
    )
    preset = ReviewPreset.from_flags(deep=depth == "deep")
    policy = preset.request(operation_id)
    axes = (
        {
            "spec": primary,
            "standards-correctness-architecture-security": RuntimeRoute(
                "codex",
                "gpt-5.6-sol",
                "xhigh",
                "reviewer-callback",
                "b" * 64,
            ),
        }
        if depth == "deep"
        else None
    )
    return ReviewOperationRequest(
        policy, "owner-1", primary, context, axis_routes=axes
    )


def begin(
    controller: ReviewGateController,
    request: ReviewOperationRequest,
    scratch: Path,
) -> object:
    return controller.begin(
        dispatch_operation_id="dispatch-1",
        request=request,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=ROOT,
        prompt_pointer="prompts/review.md",
        callback_root=f"callbacks/{request.policy.operation_id}",
    )


context = ReviewContext(
    manifest="packets/review/manifest.json",
    head_sha="c" * 40,
    verification_profile="scoped",
    verification_profile_sha256="d" * 64,
)

with tempfile.TemporaryDirectory(prefix="review-gate.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(controller, request_for("review-auto", context=context), scratch)
    check(
        "automatic simple gate starts one persistent holistic lane",
        len(run.execution.lanes) == 1
        and run.execution.lanes[0].axis == "holistic"
        and len(runtime.started) == 1
        and controller.read()["status"] == "reviewing",
    )
    lane = run.execution.lanes[0]
    waiting = controller.complete_round(
        run,
        lane,
        run.rounds["holistic"],
        ReviewResult(
            "holistic",
            "changes-requested",
            (
                ReviewFinding(
                    "F-gate-1",
                    "holistic",
                    "important",
                    "material regression",
                    "focused test fails",
                ),
            ),
        ),
    )
    resolved_context = replace(context, head_sha="f" * 40)
    try:
        controller.continue_after_resolution(
            run,
            lane,
            context=resolved_context,
            resolution=resolution_evidence(
                "review-auto", "holistic", context.head_sha,
                resolved_context.head_sha, "F-gate-1",
            ),
            review_identity_sha256="0" * 64,
            verification_prompt_pointer="prompts/verify.md",
            callback_pointer=(
                "callbacks/review-auto/holistic/.review-callback.json"
            ),
        )
    except ValueError:
        pass
    else:
        regression_failures.append(
            "controller accepted a prior-boundary review identity"
        )
    check(
        "controller rejects prior-boundary review identity",
        controller.read()["status"] == "awaiting-resolution",
    )
    first = controller.continue_after_resolution(
        run,
        lane,
        context=resolved_context,
        resolution=resolution_evidence(
            "review-auto", "holistic", context.head_sha,
            resolved_context.head_sha, "F-gate-1",
        ),
        review_identity_sha256=resolution_transport_identity(controller),
        verification_prompt_pointer="prompts/verify.md",
        callback_pointer="callbacks/review-auto/holistic/.review-callback.json",
    )
    check(
        "material finding waits for resolution, then continues exact session",
        waiting.action == "awaiting-resolution"
        and first.action == "verify"
        and first.lane is not None
        and first.lane.operation_id == lane.operation_id
        and first.lane.surface_id == lane.surface_id
        and first.lane.verification_iteration == 1
        and len(runtime.started) == 1
        and len(runtime.continued) == 1,
    )
    resolution_pointer = controller.read()["resolution_evidence"]["holistic:0"]
    check(
        "review gate persists per-finding resolution evidence before verification",
        (base / "gate" / resolution_pointer).is_file()
        and json.loads(
            (base / "gate" / resolution_pointer).read_text(encoding="utf-8")
        )["previous_finding_ids"] == ["F-gate-1"],
    )
    run = controller.rehydrate()
    first = replace(
        first,
        lane=run.execution.lanes[0],
        round=run.rounds["holistic"],
    )
    approved = controller.complete_round(
        run,
        first.lane,
        first.round,
        ReviewResult("holistic", "approve", verification_iteration=1),
    )
    authorization = authorize_task_finalization(
        base / "gate",
        dispatch_operation_id="dispatch-1",
        expected_head_sha=resolved_context.head_sha,
        expected_profile=context.verification_profile,
        expected_profile_sha256=context.verification_profile_sha256,
    )
    check(
        "approved evidence unlocks task summary and reap",
        approved.action == "approved"
        and authorization.approved
        and not authorization.skipped
        and authorization.evidence["verdict"] == "approve",
    )
    try:
        authorize_task_finalization(
            base / "gate",
            dispatch_operation_id="dispatch-1",
            expected_head_sha="e" * 40,
            expected_profile=context.verification_profile,
            expected_profile_sha256=context.verification_profile_sha256,
        )
    except ValueError:
        check("stale review evidence never unlocks finalization", True)
    else:
        check("stale review evidence never unlocks finalization", False)

with tempfile.TemporaryDirectory(prefix="review-bounded-summary.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-bounded-summary", context=context),
        scratch,
    )
    lane = run.execution.lanes[0]
    decision = controller.complete_round(
        run,
        lane,
        run.rounds["holistic"],
        ReviewResult(
            "holistic",
            "approve",
            (
                ReviewFinding(
                    "F-long-summary",
                    "holistic",
                    "minor",
                    "s" * 376,
                    "full evidence remains available",
                ),
            ),
        ),
    )
    authorization = authorize_task_finalization(
        base / "gate",
        dispatch_operation_id="dispatch-1",
        expected_head_sha=context.head_sha,
        expected_profile=context.verification_profile,
        expected_profile_sha256=context.verification_profile_sha256,
    )
    bounded = authorization.evidence["axes"][0]["findings"][0]
    check(
        "model-authored finding summaries are bounded before final evidence",
        decision.action == "approved"
        and len(bounded["summary"]) == 300
        and bounded["summary"].endswith("…")
        and bounded["evidence"] == "full evidence remains available",
    )

with tempfile.TemporaryDirectory(prefix="review-cleanup-attention.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    runtime.cleanup_attention = True
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-cleanup-attention", context=context),
        scratch,
    )
    lane = run.execution.lanes[0]
    decision = controller.complete_round(
        run,
        lane,
        run.rounds[lane.axis],
        ReviewResult("holistic", "approve"),
    )
    state = controller.read()
    check(
        "cleanup attention blocks approval after bounded exact-lane retries",
        decision.action == "attention-required"
        and state["status"] == "attention-required"
        and state["final_results"] == {}
        and state["evidence"] == {}
        and len(runtime.cleanups) == 3,
    )

with tempfile.TemporaryDirectory(prefix="review-callback-timeout.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-callback-timeout", context=context),
        scratch,
    )
    lane = run.execution.lanes[0]
    store.transition(
        lane.owner_id,
        lane.operation_id,
        "attention-required",
        reason=AttentionReason.CALLBACK_TIMEOUT,
    )
    decision = controller.complete_round(
        run,
        lane,
        run.rounds[lane.axis],
        ReviewResult("holistic", "approve"),
    )
    state = controller.read()
    check(
        "late reviewer approval cannot erase durable callback timeout",
        decision.action == "attention-required"
        and state["status"] == "attention-required"
        and state["final_results"] == {}
        and state["evidence"] == {},
    )
    store.transition(
        lane.owner_id,
        lane.operation_id,
        "awaiting-callback",
    )
    controller.resume_bound_attention()
    check(
        "explicit runtime recovery rearms only the existing bound review gate",
        controller.read()["status"] == "reviewing"
        and controller.read()["lanes"][0]["operation_id"]
        == lane.operation_id,
    )

with tempfile.TemporaryDirectory(prefix="review-defer-callback-timeout.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for(
            "review-defer-callback-timeout",
            depth="deep",
            context=context,
        ),
        scratch,
    )
    lane = run.execution.lanes[0]
    store.transition(
        lane.owner_id,
        lane.operation_id,
        "attention-required",
        reason=AttentionReason.CALLBACK_TIMEOUT,
    )
    decision = controller.defer_round_for_resolution(
        run,
        lane,
        run.rounds[lane.axis],
        ReviewResult(lane.axis, "approve"),
    )
    state = controller.read()
    check(
        "deep resolution cannot mask durable callback timeout",
        decision.action == "attention-required"
        and state["status"] == "attention-required"
        and state["round_results"] == {}
        and not state.get("awaiting_resolution"),
    )

with tempfile.TemporaryDirectory(prefix="review-cleanup-terminate.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    runtime.cleanup_terminate_once = True
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-cleanup-terminate", context=context),
        scratch,
    )
    lane = run.execution.lanes[0]
    decision = controller.complete_round(
        run,
        lane,
        run.rounds[lane.axis],
        ReviewResult("holistic", "approve"),
    )
    check(
        "orphan termination remains a bounded cleanup wait before approval",
        decision.action == "approved"
        and controller.read()["status"] == "approved"
        and len(runtime.cleanups) == 2,
    )

with tempfile.TemporaryDirectory(prefix="review-gate-budget.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    product = base / "product"
    product.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = controller.begin(
        dispatch_operation_id="review-budget",
        request=request_for("review-budget", context=context),
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=product,
        prompt_pointer="prompts/review.md",
        callback_root="callbacks/review-budget",
    )
    lane = run.execution.lanes[0]
    waiting = controller.complete_round(
        run,
        lane,
        run.rounds["holistic"],
        ReviewResult(
            "holistic",
            "changes-requested",
            (
                ReviewFinding(
                    "F-budget-1",
                    "holistic",
                    "important",
                    "first defect",
                    "first failure",
                ),
            ),
        ),
    )
    first = controller.continue_after_resolution(
        run,
        lane,
        context=replace(context, head_sha="1" * 40),
        resolution=resolution_evidence(
            "review-budget", "holistic", context.head_sha,
            "1" * 40, "F-budget-1",
        ),
        review_identity_sha256=resolution_transport_identity(controller),
        verification_prompt_pointer="prompts/verify.md",
        callback_pointer="callbacks/review-budget/holistic/.review-callback.json",
    )
    run = controller.rehydrate()
    first = replace(
        first,
        lane=run.execution.lanes[0],
        round=run.rounds["holistic"],
    )
    second_waiting = controller.complete_round(
        run,
        first.lane,
        first.round,
        ReviewResult(
            "holistic",
            "changes-requested",
            (
                ReviewFinding(
                    "F-budget-2",
                    "holistic",
                    "important",
                    "residual defect",
                    "verification still fails",
                ),
            ),
            verification_iteration=1,
        ),
    )
    exhausted = controller.continue_after_resolution(
        run,
        first.lane,
        context=replace(context, head_sha="2" * 40),
        resolution=resolution_evidence(
            "review-budget", "holistic", "1" * 40,
            "2" * 40, "F-budget-2",
        ),
        review_identity_sha256=resolution_transport_identity(controller),
        verification_prompt_pointer="prompts/verify-2.md",
        callback_pointer="callbacks/review-budget/holistic/.review-callback.json",
    )
    check(
        "verification exhaustion is durable attention instead of ValueError",
        second_waiting.action == "awaiting-resolution"
        and exhausted.action == "attention-required"
        and controller.read()["status"] == "attention-required"
        and store.read("owner-1", first.lane.operation_id).state
        == "attention-required",
    )
    active_context = run.execution.request.context
    new_context = replace(
        active_context, manifest="packets/review/expanded.json"
    )
    boundary = ReviewScopeBoundary(
        "context",
        review_context_sha256(active_context),
        review_context_sha256(new_context),
        "approved context packet changed",
    )
    boundary_authorization = {
        "schema_version": 1,
        "operation_id": "review-budget",
        "kind": boundary.kind,
        "previous_context_sha256": boundary.previous_context_sha256,
        "next_context_sha256": boundary.next_context_sha256,
        "reason": boundary.reason,
        "authorization_provenance": "coordinator-approved",
        "verification_operation_id": "verification-test",
        "verification_receipt_sha256": "a" * 64,
        "status": "authorized",
    }
    boundary_authorization_path = (
        controller.root / "fresh-boundary-authorization.json"
    )
    boundary_authorization_path.write_text(
        json.dumps(boundary_authorization, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    controller.authorize_fresh_boundary(
        run,
        boundary=boundary,
        authorization_pointer=boundary_authorization_path.name,
        authorization_sha256=hashlib.sha256(
            boundary_authorization_path.read_bytes()
        ).hexdigest(),
    )
    stale_packet = product / ".task-review.json"
    stale_resolution = product / ".task-review-resolution.json"
    stale_packet.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": "review-budget",
                "reviewed_head_sha": active_context.head_sha,
                "material_finding_ids": ["F-budget-2"],
                "findings": [{"finding_id": "F-budget-2"}],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    stale_resolution.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": "review-budget",
                "reviewed_head_sha": active_context.head_sha,
                "resolved_head_sha": "2" * 40,
                "resolutions": [
                    {
                        "finding_id": "F-budget-2",
                        "disposition": "applied",
                        "rationale": "Prior-boundary resolution.",
                        "follow_up": "",
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    fresh = controller.restart_for_boundary(
        run,
        boundary=boundary,
        context=new_context,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=product,
        prompt_pointer="prompts/compact.md",
        callback_root="callbacks/review-budget-fresh",
        max_verify_iterations=0,
    )
    check(
        "fresh boundary invalidates the previous executor review transport",
        not stale_packet.exists() and not stale_resolution.exists(),
    )
    repeated = controller.restart_for_boundary(
        fresh,
        boundary=boundary,
        context=new_context,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=product,
        prompt_pointer="prompts/compact.md",
        callback_root="callbacks/review-budget-fresh",
        max_verify_iterations=0,
    )
    check(
        "one explicit scope/context boundary permits one fresh compact run",
        fresh is not None
        and len(runtime.started) == 2
        and repeated is not None
        and fresh.execution.request.policy.max_verify_iterations == 0
        and fresh.execution.lanes[0].max_verify_iterations == 0
        and controller.read()["status"] == "reviewing",
    )
    assert fresh is not None
    fresh_lane = fresh.execution.lanes[0]
    fresh_result = ReviewResult(
        "holistic",
        "changes-requested",
        (
            ReviewFinding(
                "F-fresh-1",
                "holistic",
                "important",
                "fresh context defect",
                "fresh review found a product gap",
            ),
        ),
    )
    fresh_waiting = controller.complete_round(
        fresh,
        fresh_lane,
        fresh.rounds["holistic"],
        fresh_result,
    )
    fresh_round = fresh.rounds["holistic"]
    fresh_callback = review_round_envelope(fresh_round, fresh_result)
    fresh_boundary = controller.read()["awaiting_resolution"]["holistic"]
    check(
        "fresh resolution boundary binds operation callback findings and HEAD",
        fresh_boundary["review_operation_id"]
        == fresh.execution.request.policy.operation_id
        and fresh_boundary["round_operation_id"]
        == fresh_round.operation_id
        and fresh_boundary["round_run_id"] == fresh_round.run_id
        and fresh_boundary["callback_id"] == fresh_callback.callback_id
        and fresh_boundary["callback_sha256"]
        == fresh_callback.payload_sha256
        and fresh_boundary["material_finding_ids"] == ["F-fresh-1"]
        and fresh_boundary["reviewed_head_sha"] == new_context.head_sha,
    )
    fresh_exhausted = controller.continue_after_resolution(
        fresh,
        fresh_lane,
        context=replace(new_context, head_sha="3" * 40),
        resolution=resolution_evidence(
            "review-budget",
            "holistic",
            new_context.head_sha,
            "3" * 40,
            "F-fresh-1",
        ),
        review_identity_sha256=resolution_transport_identity(controller),
        verification_prompt_pointer="prompts/fresh-verify.md",
        callback_pointer="callbacks/review-budget-fresh/holistic/.review-callback.json",
    )
    check(
        "fresh review resolutions remain bound to the dispatch identity",
        fresh_waiting.action == "awaiting-resolution"
        and fresh_exhausted.action == "attention-required",
    )

with tempfile.TemporaryDirectory(prefix="review-gate-deep.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-deep-gate", depth="deep", context=context),
        scratch,
    )
    first_axis = run.execution.lanes[0]
    first = controller.complete_round(
        run,
        first_axis,
        run.rounds[first_axis.axis],
        ReviewResult(first_axis.axis, "approve"),
    )
    second_axis = run.execution.lanes[1]
    second = controller.complete_round(
        run,
        second_axis,
        run.rounds[second_axis.axis],
        ReviewResult(second_axis.axis, "approve"),
    )
    check(
        "deep gate preserves independent lanes and approves only after both axes",
        len(runtime.started) == 2
        and len({lane.lane_id for lane in run.execution.lanes}) == 2
        and first.action == "awaiting-axes"
        and second.action == "approved",
    )

with tempfile.TemporaryDirectory(prefix="review-gate-deep-attention.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-deep-attention", depth="deep", context=context),
        scratch,
    )
    spec_lane, standards_lane = run.execution.lanes
    blocked = controller.complete_round(
        run,
        spec_lane,
        run.rounds[spec_lane.axis],
        ReviewResult(spec_lane.axis, "blocked"),
    )
    later = controller.complete_round(
        run,
        standards_lane,
        run.rounds[standards_lane.axis],
        ReviewResult(standards_lane.axis, "approve"),
    )
    state = controller.read()
    check(
        "later deep-axis approval cannot mask durable blocked attention",
        blocked.action == "attention-required"
        and later.action == "attention-required"
        and state["status"] == "attention-required"
        and state["evidence"] == {},
    )

with tempfile.TemporaryDirectory(prefix="review-gate-defer-blocked.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-defer-blocked", depth="deep", context=context),
        scratch,
    )
    lane = run.execution.lanes[0]
    decision = controller.defer_round_for_resolution(
        run,
        lane,
        run.rounds[lane.axis],
        ReviewResult(lane.axis, "blocked"),
    )
    state = controller.read()
    check(
        "deep defer keeps blocked verdict as durable attention",
        decision.action == "attention-required"
        and state["status"] == "attention-required"
        and not state.get("awaiting_resolution"),
    )

with tempfile.TemporaryDirectory(prefix="review-gate-skip.") as raw:
    gate = Path(raw)
    ReviewGateController.skip(
        gate,
        dispatch_operation_id="dispatch-skip",
        owner_id="owner-1",
        preset=disabled,
        context=context,
        product_root=ROOT,
    )
    skipped = authorize_task_finalization(
        gate,
        dispatch_operation_id="dispatch-skip",
        expected_head_sha=context.head_sha,
        expected_profile=context.verification_profile,
        expected_profile_sha256=context.verification_profile_sha256,
    )
    check(
        "explicit no-review is the only evidence-free finalization route",
        skipped.skipped and not skipped.approved,
    )

with tempfile.TemporaryDirectory(prefix="review-gate-deep-resolution.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-deep-resolution", depth="deep", context=context),
        scratch,
    )
    spec_lane, standards_lane = run.execution.lanes
    controller.defer_round_for_resolution(
        run,
        spec_lane,
        run.rounds[spec_lane.axis],
        ReviewResult(spec_lane.axis, "approve"),
    )
    controller.defer_round_for_resolution(
        run,
        standards_lane,
        run.rounds[standards_lane.axis],
        ReviewResult(
            standards_lane.axis,
            "changes-requested",
            (
                ReviewFinding(
                    "F-deep-resolution",
                    standards_lane.axis,
                    "important",
                    "shared HEAD needs a fix",
                    "the correctness axis found a regression",
                ),
            ),
        ),
    )
    resolved = replace(context, head_sha="9" * 40)
    for lane in run.execution.lanes:
        controller.continue_after_resolution(
            run,
            lane,
            context=resolved,
            resolution=resolution_evidence(
                "review-deep-resolution",
                lane.axis,
                context.head_sha,
                resolved.head_sha,
                *(
                    ("F-deep-resolution",)
                    if lane.axis == standards_lane.axis
                    else ()
                ),
            ),
            review_identity_sha256=resolution_transport_identity(controller),
            verification_prompt_pointer=f"prompts/{lane.axis}.md",
            callback_pointer=(
                f"callbacks/deep/{lane.axis}/.review-callback.json"
            ),
        )
    check(
        "deep material resolution keeps both independent parents for the new HEAD",
        len(runtime.started) == 2
        and len(runtime.continued) == 2
        and not runtime.exits
        and controller.read()["status"] == "verifying"
        and {
            row["axis"]: row["verification_iteration"]
            for row in controller.read()["lanes"]
        }
        == {
            "spec": 1,
            "standards-correctness-architecture-security": 1,
        },
    )

with tempfile.TemporaryDirectory(prefix="task-review-runner.") as raw:
    base = Path(raw)
    vault = base / "coordinator-vault"
    product = base / "generic-product-worktree"
    (vault / "wiki/plans").mkdir(parents=True)
    (vault / "skills/review").mkdir(parents=True)
    (vault / "scripts").mkdir()
    (vault / "config").mkdir()
    (vault / ".vault-meta/harness").mkdir(parents=True)
    plan = vault / "wiki/plans/approved.md"
    plan.write_text("# Approved task\n\nImplement the bounded change.\n", encoding="utf-8")
    (vault / "skills/review/SKILL.md").write_text(
        "# Review\n\nInspect the exact ContextPacket and product HEAD.\n",
        encoding="utf-8",
    )
    (vault / "config/model-routing.toml").write_bytes(
        (ROOT / "config/model-routing.toml").read_bytes()
    )
    (vault / "config/verification-profiles.toml").write_bytes(
        (ROOT / "config/verification-profiles.toml").read_bytes()
    )
    product.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "review@example.invalid"],
        cwd=product,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Review Gate Test"],
        cwd=product,
        check=True,
    )
    (product / "product.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.py"], cwd=product, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    )
    task_id = str(uuid.uuid4())
    profile_sha = load_profiles(
        vault / "config/verification-profiles.toml"
    )["scoped"].sha256
    meta = {
        "version": 3,
        "project_id": str(uuid.uuid4()),
        "task_id": task_id,
        "task_name": "automatic task review",
        "executor_runtime": "codex",
        "origin_session": "session-1",
        "task_surface": "22222222-2222-4222-8222-222222222222",
        "worktree": str(product),
        "vault_root": str(vault),
        "plan_file": str(plan),
        "approved_plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "interaction_policy": "unattended",
        "pipeline_policy": {
            "name": "lifecycle/default",
            "definition_sha256": compiled_builtin(
                "lifecycle/default"
            ).definition_sha256,
            "completion_policy": "attention",
            "total_pass_limit": 2,
        },
        "routing": {
            "session": {
                "runtime": "codex",
                "model": "gpt-5.6-sol",
                "effort": "high",
                "source": "test-session",
            }
        },
        "review_policy": {
            "mode": "simple",
            "cross_model": False,
            "runtime": "",
            "model": "",
            "effort": "",
            "max_verify_iterations": 1,
            "verification_profile": "scoped",
            "verification_profile_sha256": profile_sha,
            "auto_resolve_severities": ["warning", "nit"],
            "escalate_severities": ["blocking"],
        },
        "reap_policy": {
            "mode": "final",
            "auto_file": True,
            "allowed_types": ["session"],
            "title": "automatic task review",
        },
        "surface_policy": {"auto_close": True, "placement": "split"},
        "forbidden_actions": [
            "push",
            "deploy",
            "publish",
            "delete-worktree",
            "delete-branch",
            "expand-scope",
        ],
    }
    (product / ".task-meta.json").write_text(
        json.dumps(meta, sort_keys=True) + "\n", encoding="utf-8"
    )
    module_spec = importlib.util.spec_from_file_location(
        "task_review_runner_module", ROOT / "scripts/task-review-runner.py"
    )
    check(
        "task review runner is a public code-owned lifecycle facade",
        module_spec is not None and module_spec.loader is not None,
    )
    runner_source = (ROOT / "scripts/task-review-runner.py").read_text(
        encoding="utf-8"
    )
    check(
        "deep resolution stops after the first exhausted lane",
        'if decision.action == "attention-required":\n                break'
        in runner_source,
    )
    task_review_runner = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(task_review_runner)
    task_store = OperationStore(vault / ".vault-meta/harness")
    task_dispatch_spec = OperationSpec(
        operation_id=task_id,
        idempotency_key="task-review-dispatch",
        kind="dispatch",
        owner_id=task_id,
        route=RuntimeRoute(
            "codex",
            "gpt-5.6-sol",
            "high",
            "executor",
            "f" * 64,
        ),
        context_manifest="wiki/plans/approved.md",
        verification_profile="scoped",
        contract_sha256=meta["pipeline_policy"]["definition_sha256"],
    )
    task_store.create(
        task_dispatch_spec,
        lane_id="task-review-dispatch-lane",
        run_id="task-review-dispatch-run",
    )
    for dispatch_state in (
        "preflight",
        "starting",
        "running",
        "awaiting-callback",
    ):
        task_store.transition(task_id, task_id, dispatch_state)
    task_runtime = FakeRuntime(task_store)
    started = task_review_runner.run_task_review(
        product, runtime_manager=task_runtime
    )
    gate_root = (
        vault
        / ".vault-meta/harness/review-data"
        / task_id
        / task_id
    )
    started_state = json.loads(
        (gate_root / "review-gate.json").read_text(encoding="utf-8")
    )
    packet_manifest = (
        Path(str(started["context_manifest"]))
    )
    check(
        "v3 task start separates coordinator vault, product, and scratch ContextPacket",
        started["status"] == "reviewing"
        and started["worktree"] == str(product.resolve())
        and started["vault_root"] == str(vault.resolve())
        and len(task_runtime.started) == 1
        and task_runtime.started[0].product_root == product.resolve()
        and task_runtime.started[0].cwd != product.resolve()
        and packet_manifest.is_file()
        and product.resolve() not in packet_manifest.parents
        and started_state["dispatch_operation_id"] == task_id,
    )
    initial_lane = started["lanes"][0]
    callback_path = Path(initial_lane["callback_path"])
    submit = shlex.join(
        (
            str(Path(sys.executable).resolve()),
            str(vault.resolve() / "scripts/harness/review_submit.py"),
            "--worktree",
            str(product.resolve()),
            "--state-dir",
            str(callback_path.parent),
            "--input-file",
            str(callback_path.with_name(".review-input.json")),
        )
    )
    initial_request = task_runtime.started[0]
    prompt_text = (
        initial_request.cwd / initial_request.prompt_pointer
    ).read_text(encoding="utf-8")
    claude_command = ClaudeDriver(Path("/usr/bin/claude")).command(
        RuntimeRoute(
            "claude",
            "fable",
            "high",
            "reviewer-callback",
            "f" * 64,
        ),
        callback_pointer=callback_path,
        product_root=product.resolve(),
        session_root=initial_request.cwd,
    )
    check(
        "review prompt submit command has one exact Claude Bash permission",
        f"`{submit}`" in prompt_text
        and f"Bash({submit})" in claude_command
        and "Read, Glob, and Grep with absolute paths" in prompt_text
        and "review-inspect.py" in prompt_text
        and "Do not run cd or copy packet files" in prompt_text,
    )
    round_ = task_review_runner.load_active_round(
        gate_root,
        task_store,
        task_runtime,
        axis="holistic",
    )
    callback = review_round_envelope(
        round_.round,
        ReviewResult(
            "holistic",
            "changes-requested",
            (
                ReviewFinding(
                    "F-task-1",
                    "holistic",
                    "important",
                    "fix the product value",
                    "VALUE still equals one",
                    file="product.py",
                ),
            ),
        ),
    )
    callback_path.write_text(
        json.dumps(to_dict(callback), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    waiting = task_review_runner.run_task_review(
        product, runtime_manager=task_runtime
    )
    check(
        "drive consumes the exact callback and persists material findings",
        waiting["status"] == "awaiting-resolution"
        and len(task_runtime.continued) == 0
        and json.loads(
            (gate_root / "review-gate.json").read_text(encoding="utf-8")
        )["status"]
        == "awaiting-resolution",
    )
    review_events = [
        json.loads(line)
        for line in (
            vault / ".vault-meta/pipeline-events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("op", "").startswith("review-")
    ]
    check(
        "production review emits content-free start callback and completion telemetry",
        [event["op"] for event in review_events]
        == ["review-round-start", "review-callback", "review-round-complete"]
        and review_events[1]["counts"]["accepted_callbacks"] == 1
        and review_events[2]["counts"]["important_findings"] == 1
        and review_events[2]["counts"]["critical_findings"] == 0
        and review_events[2]["counts"]["minor_findings"] == 0
        and review_events[2]["identifiers"]
        == {
            "axis": "holistic",
            "reviewer_runtime": "codex",
            "terminal_status": "changes-requested",
        }
        and "F-task-1" not in json.dumps(review_events, sort_keys=True),
    )
    reviewed_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (product / "product.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.py"], cwd=product, check=True)
    subprocess.run(
        ["git", "commit", "-m", "resolve review"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    )
    resolved_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (product / ".task-review-resolution.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": task_id,
                "review_identity_sha256": resolution_transport_identity(
                    ReviewGateController(gate_root, task_runtime, task_store)
                ),
                "reviewed_head_sha": reviewed_head,
                "resolved_head_sha": resolved_head,
                "resolutions": [
                    {
                        "finding_id": "F-task-1",
                        "disposition": "applied",
                        "rationale": (
                            "The corrected product value and its commit are "
                            "present on the resolved HEAD."
                        ),
                        "follow_up": "",
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    verifying = task_review_runner.run_task_review(
        product, runtime_manager=task_runtime
    )
    verification_manifest = json.loads(
        Path(str(verifying["context_manifest"])).read_text(encoding="utf-8")
    )
    verification_inputs = {
        item["name"] for item in verification_manifest["inputs"]
    }
    check(
        "restart-safe verify rehydrates and continues the same parent session",
        verifying["status"] == "verifying"
        and len(task_runtime.started) == 1
        and len(task_runtime.continued) == 1
        and task_runtime.continued[0][1] == initial_lane["operation_id"]
        and json.loads(
            (gate_root / "review-gate.json").read_text(encoding="utf-8")
        )["context"]["head_sha"]
        == subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=product,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
    )
    check(
        "same-session verification receives previous findings and bounded fix delta",
        {"resolution-evidence.json", "fix-delta.patch"}
        <= verification_inputs,
    )

    # Recovery must consume coordinator-owned verification evidence and must
    # preserve the exact reviewer-seen ruling across a verification repair.
    finalizing = task_review_runner.load_active_round(
        gate_root,
        task_store,
        task_runtime,
        axis="holistic",
    )
    approved_result = ReviewResult(
        "holistic",
        "approve",
        (),
        verification_iteration=1,
    )
    approved_envelope = review_round_envelope(
        finalizing.round,
        approved_result,
    )
    callback_path.write_text(
        json.dumps(to_dict(approved_envelope), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    task_runtime.accept_callback(approved_envelope)
    interrupted_child = task_store.read(
        task_id,
        finalizing.round.operation_id,
    )
    (product / "verification-repair.txt").write_text(
        "bounded verification repair\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "verification-repair.txt"],
        cwd=product,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "repair verification"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    )
    repaired_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    resolution_path = product / ".task-review-resolution.json"
    exact_resolution = json.loads(
        resolution_path.read_text(encoding="utf-8")
    )
    exact_resolution["resolved_head_sha"] = repaired_head
    exact_resolution_bytes = (
        json.dumps(exact_resolution, sort_keys=True) + "\n"
    ).encode()
    resolution_path.write_bytes(exact_resolution_bytes)
    (product / ".task-summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "type": "session",
                "title": "automatic task review",
                "session": "session-1",
                "body": "Preserve exact accepted review evidence during recovery.",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    verification_input_sha = hashlib.sha256(
        b"failed verification input"
    ).hexdigest()
    (
        verification_spec,
        verification_lane_id,
        verification_run_id,
    ) = _pipeline_verify_identity(
        task_dispatch_spec,
        definition_sha256=meta["pipeline_policy"]["definition_sha256"],
        input_sha256=verification_input_sha,
        profile="scoped",
    )
    verification_operation_id = verification_spec.operation_id
    verification_effect_id = (
        f"pipeline-verify-{verification_input_sha[:32]}"
    )
    task_store.create(
        verification_spec,
        lane_id=verification_lane_id,
        run_id=verification_run_id,
    )
    for verification_state in (
        "preflight",
        "starting",
        "running",
        "verifying",
    ):
        task_store.transition(
            task_id,
            verification_operation_id,
            verification_state,
        )
    task_store.transition(
        task_id,
        verification_operation_id,
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )
    verification_record = task_store.read(
        task_id, verification_operation_id
    )
    task_store.save(
        replace(
            verification_record,
            effect_id=verification_effect_id,
            effect_outcome=EffectOutcome.SUCCEEDED,
            revision=verification_record.revision + 1,
        ),
        expected_revision=verification_record.revision,
    )
    owner_runtime = (
        vault
        / ".vault-meta/harness/owners"
        / task_id
        / "runtime"
        / task_id
    )
    verification_root = (
        owner_runtime
        / "pipeline-verification"
        / verification_operation_id
    )
    evidence_path = verification_root / "evidence/scoped-1.log"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text("deterministic failed verification\n", encoding="utf-8")
    verification_receipt = {
        "schema_version": 1,
        "operation_id": verification_operation_id,
        "parent_operation_id": task_id,
        "lane_id": verification_lane_id,
        "run_id": verification_run_id,
        "definition_sha256": meta["pipeline_policy"]["definition_sha256"],
        "step_id": "verify",
        "head_sha": resolved_head,
        "input_sha256": verification_input_sha,
        "profile": "scoped",
        "profile_sha256": profile_sha,
        "effect_id": verification_effect_id,
        "status": "failed",
        "evidence": [
            {
                "command_id": "scoped-1",
                "cwd": ".",
                "exit_code": 2,
                "started_at": "1.0",
                "finished_at": "2.0",
                "head_sha": resolved_head,
                "profile": "scoped",
                "profile_sha256": profile_sha,
                "output_pointer": (
                    evidence_path.relative_to(owner_runtime).as_posix()
                ),
            }
        ],
    }
    receipt_path = verification_root / "receipt.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(verification_receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (owner_runtime / "pipeline-step-verify.json").write_text(
        json.dumps(verification_receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    packet = {
        "schema_version": 1,
        "operation_id": task_id,
        "verification_operation_id": verification_operation_id,
        "verification_lane_id": verification_lane_id,
        "verification_run_id": verification_run_id,
        "definition_sha256": meta["pipeline_policy"]["definition_sha256"],
        "step_id": "verify",
        "head_sha": resolved_head,
        "status": "attention-required",
        "reason": "verification-failed",
        "safe_boundary": "tdd-slices-complete",
        "allowed_responses": ["fix-and-resubmit", "escalate"],
        "response_pointer": ".task-verification-response.json",
        "receipt_pointer": str(receipt_path.resolve()),
        "evidence": [
            {
                "command_id": "scoped-1",
                "exit_code": 2,
                "output_pointer": str(evidence_path.resolve()),
            }
        ],
    }

    def write_resubmit(
        packet_value: dict[str, object],
    ) -> dict[str, object]:
        (product / ".task-verification.json").write_text(
            json.dumps(packet_value, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        packet_bytes = json.dumps(
            packet_value, sort_keys=True, separators=(",", ":")
        ).encode()
        response_value = {
            "schema_version": 1,
            "operation_id": task_id,
            "verification_operation_id": packet_value[
                "verification_operation_id"
            ],
            "failed_head_sha": resolved_head,
            "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
            "response": "fix-and-resubmit",
            "resubmitted_head_sha": repaired_head,
        }
        (product / ".task-verification-response.json").write_text(
            json.dumps(response_value, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return response_value

    recover_finalizing = getattr(
        task_review_runner,
        "recover_finalizing_review",
        None,
    )
    response_receipt_path = verification_root / "response-receipt.json"
    if recover_finalizing is None:
        regression_failures.extend(
            (
                "HOL-R01 durable verification receipt binding",
                "HOL-R02 exact reviewer-seen finding ruling",
                "HOL-R03 interruption-safe finalizing replay",
            )
        )
    else:
        forged_packet = {
            **packet,
            "verification_operation_id": "forged-verification-child",
            "verification_lane_id": "forged-verification-lane",
            "verification_run_id": "forged-verification-run",
            "receipt_pointer": str(
                product / "missing-forged-verification-receipt.json"
            ),
        }
        write_resubmit(forged_packet)
        try:
            recover_finalizing(product, runtime_manager=task_runtime)
        except task_review_runner.TaskReviewError:
            forged_rejected = True
        else:
            forged_rejected = False
        if (
            not forged_rejected
            or response_receipt_path.exists()
            or task_store.read(
                task_id, finalizing.round.operation_id
            ).state
            != "finalizing"
        ):
            regression_failures.append(
                "HOL-R01 durable verification receipt binding"
            )

        write_resubmit(packet)
        drifted_resolution = json.loads(
            exact_resolution_bytes.decode()
        )
        drifted_resolution["resolutions"][0].update(
            {
                "disposition": "rejected",
                "rationale": "executor-rewritten rationale after review",
                "follow_up": "",
            }
        )
        resolution_path.write_text(
            json.dumps(drifted_resolution, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            recover_finalizing(product, runtime_manager=task_runtime)
        except task_review_runner.TaskReviewError:
            ruling_rejected = True
        else:
            ruling_rejected = False
        if not ruling_rejected:
            regression_failures.append(
                "HOL-R02 exact reviewer-seen finding ruling"
            )
        resolution_path.write_bytes(exact_resolution_bytes)

        parent_record = task_store.read(task_id, task_id)
        if parent_record.state != "attention-required":
            task_store.transition(
                task_id,
                task_id,
                "attention-required",
                reason=AttentionReason.ATTENTION_REQUIRED,
            )
        session_path = (
            task_store.root
            / "owners"
            / task_id
            / "runtime"
            / task_id
            / "session.json"
        )
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation_id": task_id,
                    "run_id": task_store.read(task_id, task_id).run_id,
                    "cwd": str(product),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        cli_output = StringIO()
        if "review_runtime_manager" in inspect.signature(
            harness_cli.main
        ).parameters:
            with redirect_stdout(cli_output):
                cli_resume_rc = harness_cli.main(
                    [
                        "--store",
                        str(task_store.root),
                        "--owner",
                        task_id,
                        "--json",
                        "resume",
                        task_id,
                    ],
                    process_adapter=object(),
                    cmux_adapter=object(),
                    review_runtime_manager=task_runtime,
                )
        else:
            cli_resume_rc = 2
            try:
                recover_finalizing(
                    product, runtime_manager=task_runtime
                )
            except task_review_runner.TaskReviewError:
                pass
        staged_state = json.loads(
            (gate_root / "review-gate.json").read_text(encoding="utf-8")
        )
        staged = {
            "status": (
                "verifying"
                if staged_state.get("status")
                == "recovery-verification-required"
                else "error"
            )
        }
        if not (
            cli_resume_rc == 0
            and task_store.read(task_id, task_id).state
            == "awaiting-callback"
        ):
            regression_failures.append(
                "HOL-003 dispatch resume accepts staged recovery"
            )
        accepted_response = (
            json.loads(response_receipt_path.read_text(encoding="utf-8"))
            if response_receipt_path.is_file()
            else {}
        )
        if not (
            staged.get("status") == "verifying"
            and staged_state["context"]["head_sha"] == resolved_head
            and staged_state.get("status")
            == "recovery-verification-required"
            and staged_state.get("final_results") == {}
            and accepted_response.get("verification_operation_id")
            == verification_operation_id
            and accepted_response.get("resubmitted_head_sha")
            == repaired_head
            and task_store.read(
                task_id, finalizing.round.operation_id
            ).state
            == "complete"
        ):
            regression_failures.append(
                "HOL-001 repaired HEAD cannot inherit prior approval"
            )

        unauthorized_fresh_rejected = False
        if staged.get("status") == "verifying":
            dispatch_record = task_store.read(task_id, task_id)
            if dispatch_record.state != "attention-required":
                task_store.transition(
                    task_id,
                    task_id,
                    "attention-required",
                    reason=AttentionReason.ATTENTION_REQUIRED,
                )
            for terminal_record in task_store.list(task_id):
                if (
                    terminal_record.state in TERMINAL
                    and terminal_record.resources != OwnedResources()
                ):
                    task_store.save(
                        replace(
                            terminal_record,
                            resources=OwnedResources(),
                            revision=terminal_record.revision + 1,
                        ),
                        expected_revision=terminal_record.revision,
                    )
            try:
                task_review_runner.restart_task_review_for_boundary(
                    product,
                    kind="scope",
                    reason="caller-manufactured scope expansion",
                    runtime_manager=task_runtime,
                )
            except task_review_runner.TaskReviewError:
                unauthorized_fresh_rejected = True
        if not unauthorized_fresh_rejected:
            regression_failures.append(
                "HOL-002 coordinator-owned fresh-boundary authorization"
            )

        successful_verification_operation_id = ""
        if staged.get("status") == "verifying":
            successful_input_sha = hashlib.sha256(
                b"successful repaired-head verification"
            ).hexdigest()
            (
                successful_spec,
                successful_lane_id,
                successful_run_id,
            ) = _pipeline_verify_identity(
                task_dispatch_spec,
                definition_sha256=meta["pipeline_policy"][
                    "definition_sha256"
                ],
                input_sha256=successful_input_sha,
                profile="scoped",
            )
            successful_verification_operation_id = (
                successful_spec.operation_id
            )
            task_store.create(
                successful_spec,
                lane_id=successful_lane_id,
                run_id=successful_run_id,
            )
            for successful_state in (
                "preflight",
                "starting",
                "running",
                "verifying",
                "finalizing",
                "exiting",
                "complete",
            ):
                task_store.transition(
                    task_id,
                    successful_spec.operation_id,
                    successful_state,
                )
            successful_record = task_store.read(
                task_id, successful_spec.operation_id
            )
            successful_effect_id = (
                f"pipeline-verify-{successful_input_sha[:32]}"
            )
            task_store.save(
                replace(
                    successful_record,
                    effect_id=successful_effect_id,
                    effect_outcome=EffectOutcome.SUCCEEDED,
                    revision=successful_record.revision + 1,
                ),
                expected_revision=successful_record.revision,
            )
            successful_root = (
                owner_runtime
                / "pipeline-verification"
                / successful_spec.operation_id
            )
            successful_evidence = []
            for command_index in range(1, 4):
                successful_output = (
                    successful_root
                    / f"evidence/scoped-{command_index}.log"
                )
                successful_output.parent.mkdir(
                    parents=True, exist_ok=True
                )
                successful_output.write_text(
                    "deterministic successful verification\n",
                    encoding="utf-8",
                )
                successful_evidence.append(
                    {
                        "command_id": f"scoped-{command_index}",
                        "cwd": ".",
                        "exit_code": 0,
                        "started_at": f"{command_index}.0",
                        "finished_at": f"{command_index}.5",
                        "head_sha": repaired_head,
                        "profile": "scoped",
                        "profile_sha256": profile_sha,
                        "output_pointer": successful_output.relative_to(
                            owner_runtime
                        ).as_posix(),
                    }
                )
            successful_receipt = {
                "schema_version": 1,
                "operation_id": successful_spec.operation_id,
                "parent_operation_id": task_id,
                "lane_id": successful_lane_id,
                "run_id": successful_run_id,
                "definition_sha256": meta["pipeline_policy"][
                    "definition_sha256"
                ],
                "step_id": "verify",
                "head_sha": repaired_head,
                "input_sha256": successful_input_sha,
                "profile": "scoped",
                "profile_sha256": profile_sha,
                "effect_id": successful_effect_id,
                "status": "complete",
                "evidence": successful_evidence,
            }
            successful_receipt_path = successful_root / "receipt.json"
            successful_receipt_path.parent.mkdir(
                parents=True, exist_ok=True
            )
            successful_receipt_path.write_text(
                json.dumps(successful_receipt, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (owner_runtime / "pipeline-step-verify.json").write_text(
                json.dumps(successful_receipt, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            authorize = getattr(
                ReviewGateController, "authorize_fresh_boundary", None
            )
            if authorize is None:
                interrupted = False
                replayed = {"status": "error"}
                terminal_replay = {"status": "error"}
            else:
                original_authorize = authorize

                def interrupt_after_authorization(
                    controller: ReviewGateController,
                    *args: object,
                    **kwargs: object,
                ) -> object:
                    original_authorize(controller, *args, **kwargs)
                    raise OSError(
                        "simulated interruption after boundary authorization"
                    )

                ReviewGateController.authorize_fresh_boundary = (
                    interrupt_after_authorization
                )
                try:
                    try:
                        recover_finalizing(
                            product, runtime_manager=task_runtime
                        )
                    except task_review_runner.TaskReviewError:
                        interrupted = True
                    else:
                        interrupted = False
                finally:
                    ReviewGateController.authorize_fresh_boundary = (
                        original_authorize
                    )
                try:
                    replayed = recover_finalizing(
                        product, runtime_manager=task_runtime
                    )
                except task_review_runner.TaskReviewError as exc:
                    replayed = {"status": "error", "error": str(exc)}
                try:
                    terminal_replay = recover_finalizing(
                        product, runtime_manager=task_runtime
                    )
                except task_review_runner.TaskReviewError as exc:
                    terminal_replay = {
                        "status": "error",
                        "error": str(exc),
                    }
        else:
            interrupted = False
            replayed = {"status": "error"}
            terminal_replay = {"status": "error"}

        recovered_state = json.loads(
            (gate_root / "review-gate.json").read_text(encoding="utf-8")
        )
        authorization_pointer = recovered_state.get(
            "fresh_boundary_authorization", {}
        ).get("pointer", "")
        authorization_path = gate_root / authorization_pointer
        authorization = (
            json.loads(authorization_path.read_text(encoding="utf-8"))
            if authorization_path.is_file()
            else {}
        )
        if not (
            interrupted
            and replayed.get("status") == "reviewing"
            and terminal_replay.get("status") == "reviewing"
            and recovered_state.get("status") == "reviewing"
            and recovered_state["context"]["head_sha"] == repaired_head
            and recovered_state.get("final_results") == {}
            and authorization.get("operation_id") == task_id
            and authorization.get("kind") == "context"
            and authorization.get("authorization_provenance")
            == "pipeline-verification"
            and authorization.get("verification_operation_id")
            == successful_verification_operation_id
        ):
            regression_failures.append(
                "HOL-R03 interruption-safe finalizing replay"
            )

    custom_recovery_id = str(uuid.uuid4())
    custom_pipeline_raw = {
        "schema_version": 1,
        "spec_id": "review-recovery-extra-check",
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
            {
                "from_step": "implement",
                "outcome": "complete",
                "target": "verify",
                "max_traversals": 1,
            },
            {
                "from_step": "verify",
                "outcome": "complete",
                "target": "review",
                "max_traversals": 1,
            },
            {
                "from_step": "review",
                "outcome": "complete",
                "target": "terminal:completed",
                "max_traversals": 1,
            },
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
        "verification_checks": ["diff-check"],
        "review_mode": "simple",
        "human_gates": ["initial-approval"],
        "terminal_outcomes": ["completed", "attention-required"],
    }
    custom_policy = CustomPipelinePolicy.default()
    custom_spec = parse_pipeline_spec(custom_pipeline_raw)
    custom_compiled = compile_custom_spec(
        custom_spec,
        builtin_registry(),
        policy=custom_policy,
        capabilities=("route:resolved",),
    )
    custom_card = render_custom_approval(
        custom_spec,
        custom_compiled,
        policy=custom_policy,
    )
    custom_approval = ExplicitPipelineApproval.for_card(
        definition_sha256=custom_compiled.definition_sha256,
        approval_card=custom_card,
        actor="user",
        decision="approve",
    )
    custom_frozen = freeze_custom_pipeline(
        custom_spec,
        custom_compiled,
        custom_approval,
        custom_card,
    )
    FrozenPipelineStore(
        task_store.root
        / "owners"
        / custom_recovery_id
        / "runtime"
    ).save(
        operation_id=custom_recovery_id,
        spec=custom_spec,
        frozen=custom_frozen,
        approval=custom_approval,
    )
    custom_dispatch_spec = OperationSpec(
        operation_id=custom_recovery_id,
        idempotency_key="custom-review-recovery",
        kind="dispatch",
        owner_id=custom_recovery_id,
        route=RuntimeRoute(
            "codex",
            "gpt-5.6-sol",
            "high",
            "executor",
            "f" * 64,
        ),
        context_manifest="wiki/plans/approved.md",
        verification_profile="scoped",
        contract_sha256=custom_compiled.definition_sha256,
    )
    task_store.create(
        custom_dispatch_spec,
        lane_id="custom-recovery-dispatch-lane",
        run_id="custom-recovery-dispatch-run",
    )
    custom_input_sha = hashlib.sha256(
        b"custom repaired-head verification"
    ).hexdigest()
    (
        custom_verification_spec,
        custom_verification_lane,
        custom_verification_run,
    ) = _pipeline_verify_identity(
        custom_dispatch_spec,
        definition_sha256=custom_compiled.definition_sha256,
        input_sha256=custom_input_sha,
        profile="scoped",
    )
    task_store.create(
        custom_verification_spec,
        lane_id=custom_verification_lane,
        run_id=custom_verification_run,
    )
    for custom_state in (
        "preflight",
        "starting",
        "running",
        "verifying",
        "finalizing",
        "exiting",
        "complete",
    ):
        task_store.transition(
            custom_recovery_id,
            custom_verification_spec.operation_id,
            custom_state,
        )
    custom_effect_id = f"pipeline-verify-{custom_input_sha[:32]}"
    custom_record = task_store.read(
        custom_recovery_id,
        custom_verification_spec.operation_id,
    )
    task_store.save(
        replace(
            custom_record,
            effect_id=custom_effect_id,
            effect_outcome=EffectOutcome.SUCCEEDED,
            revision=custom_record.revision + 1,
        ),
        expected_revision=custom_record.revision,
    )
    custom_owner_runtime = (
        task_store.root
        / "owners"
        / custom_recovery_id
        / "runtime"
        / custom_recovery_id
    )
    custom_verification_root = (
        custom_owner_runtime
        / "pipeline-verification"
        / custom_verification_spec.operation_id
    )
    custom_evidence = []
    custom_command_count = len(
        load_profiles(vault / "config/verification-profiles.toml")[
            "scoped"
        ].commands
    ) + 1
    for command_index in range(1, custom_command_count + 1):
        custom_output = (
            custom_verification_root
            / f"evidence/scoped-{command_index}.log"
        )
        custom_output.parent.mkdir(parents=True, exist_ok=True)
        custom_output.write_text(
            "custom pipeline verification passed\n",
            encoding="utf-8",
        )
        custom_evidence.append(
            {
                "command_id": f"scoped-{command_index}",
                "cwd": ".",
                "exit_code": 0,
                "started_at": f"{command_index}.0",
                "finished_at": f"{command_index}.5",
                "head_sha": repaired_head,
                "profile": "scoped",
                "profile_sha256": profile_sha,
                "output_pointer": custom_output.relative_to(
                    custom_owner_runtime
                ).as_posix(),
            }
        )
    custom_receipt = {
        "schema_version": 1,
        "operation_id": custom_verification_spec.operation_id,
        "parent_operation_id": custom_recovery_id,
        "lane_id": custom_verification_lane,
        "run_id": custom_verification_run,
        "definition_sha256": custom_compiled.definition_sha256,
        "step_id": "verify",
        "head_sha": repaired_head,
        "input_sha256": custom_input_sha,
        "profile": "scoped",
        "profile_sha256": profile_sha,
        "effect_id": custom_effect_id,
        "status": "complete",
        "evidence": custom_evidence,
    }
    custom_receipt_path = custom_verification_root / "receipt.json"
    custom_receipt_path.parent.mkdir(parents=True, exist_ok=True)
    custom_receipt_path.write_text(
        json.dumps(custom_receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (custom_owner_runtime / "pipeline-step-verify.json").write_text(
        json.dumps(custom_receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    custom_meta = {
        "pipeline_policy": {
            "name": "custom",
            "source": "custom",
            "baseline": "engineering/change",
            "definition_sha256": custom_compiled.definition_sha256,
        },
        "review_policy": {
            "verification_profile": "scoped",
            "verification_profile_sha256": profile_sha,
        },
    }
    try:
        custom_verified = (
            task_review_runner._durable_successful_verification(
                custom_meta,
                vault,
                task_store,
                custom_recovery_id,
                repaired_head,
            )
        )
    except task_review_runner.TaskReviewError:
        custom_verified = None
    if custom_verified != (
        custom_verification_spec.operation_id,
        hashlib.sha256(custom_receipt_path.read_bytes()).hexdigest(),
    ):
        regression_failures.append(
            "HOL-004 custom verification commands remain recoverable"
        )

    recovery_id = str(uuid.uuid4())
    recovery_meta = {**meta, "task_id": recovery_id}
    (product / ".task-meta.json").write_text(
        json.dumps(recovery_meta, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    dispatch_spec = OperationSpec(
        operation_id=recovery_id,
        idempotency_key="review-recovery-dispatch",
        kind="dispatch",
        owner_id=recovery_id,
        route=RuntimeRoute(
            "codex",
            "gpt-5.6-sol",
            "high",
            "executor",
            "f" * 64,
        ),
        context_manifest="wiki/plans/approved.md",
        verification_profile="scoped",
    )
    task_store.create(
        dispatch_spec,
        lane_id="review-recovery-lane",
        run_id="review-recovery-run",
    )
    for state in (
        "preflight",
        "starting",
        "running",
        "awaiting-callback",
    ):
        task_store.transition(recovery_id, recovery_id, state)
    task_store.transition(
        recovery_id,
        recovery_id,
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )

    class FailBeforeReviewLane(FakeRuntime):
        def __init__(self, store: OperationStore) -> None:
            super().__init__(store)
            self.fail_once = True

        def start(
            self, request: object, *, on_surface_opened=None
        ) -> FakeSessionResult:
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("simulated review drive interruption")
            return super().start(
                request, on_surface_opened=on_surface_opened
            )

    recovery_runtime = FailBeforeReviewLane(task_store)
    try:
        task_review_runner.run_task_review(
            product, runtime_manager=recovery_runtime
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("review drive interruption fixture did not fail")
    recovery_gate = ReviewGateController(
        vault
        / ".vault-meta/harness/review-data"
        / recovery_id
        / recovery_id,
        recovery_runtime,
        task_store,
    )
    recovery_gate.mark_pending_attention()
    still_paused = task_review_runner.run_task_review(
        product, runtime_manager=recovery_runtime
    )
    task_store.transition(
        recovery_id,
        recovery_id,
        "awaiting-callback",
    )
    recovered = task_review_runner.run_task_review(
        product, runtime_manager=recovery_runtime
    )
    check(
        "resumed dispatch restarts only an evidence-free pre-launch review gate",
        still_paused["status"] == "attention-required"
        and recovered["status"] == "reviewing"
        and len(recovered["lanes"]) == 1
        and recovery_gate.read()["status"] == "reviewing",
    )
    skip_id = str(uuid.uuid4())
    skip_meta = {
        **meta,
        "task_id": skip_id,
        "review_policy": {
            **meta["review_policy"],
            "mode": "skip",
            "max_verify_iterations": 0,
        },
    }
    (product / ".task-meta.json").write_text(
        json.dumps(skip_meta, sort_keys=True) + "\n", encoding="utf-8"
    )
    skipped_task = task_review_runner.run_task_review(
        product, runtime_manager=task_runtime
    )
    skip_state = json.loads(
        (
            vault
            / ".vault-meta/harness/review-data"
            / skip_id
            / skip_id
            / "review-gate.json"
        ).read_text(encoding="utf-8")
    )
    check(
        "production no-review is an explicit typed exact-HEAD skip",
        skipped_task["status"] == "skipped"
        and skip_state["status"] == "skipped"
        and skip_state["policy"]["enabled"] is False,
    )

with tempfile.TemporaryDirectory(prefix="current-review-runner.") as raw:
    base = Path(raw)
    product = base / "current-checkout"
    scratch = base / "external-scratch"
    (product / "wiki").mkdir(parents=True)
    (product / "skills/review").mkdir(parents=True)
    (product / "scripts/harness").mkdir(parents=True)
    (product / "config").mkdir()
    (product / "skills/review/SKILL.md").write_text(
        "# Review\n\nInspect the exact ContextPacket and product HEAD.\n",
        encoding="utf-8",
    )
    (product / "scripts/harness/review_submit.py").write_text(
        "# test fixture\n", encoding="utf-8"
    )
    (product / "config/model-routing.toml").write_bytes(
        (ROOT / "config/model-routing.toml").read_bytes()
    )
    (product / "config/verification-profiles.toml").write_bytes(
        (ROOT / "config/verification-profiles.toml").read_bytes()
    )
    (product / "AGENTS.md").write_text(
        "# Product instructions\n", encoding="utf-8"
    )
    (product / "product.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "review@example.invalid"],
        cwd=product,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Review Gate Test"],
        cwd=product,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=product, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    )

    class FailOnceCurrentRuntime(FakeRuntime):
        def __init__(self, store: OperationStore) -> None:
            super().__init__(store)
            self.fail_once = True

        def start(
            self, request: object, *, on_surface_opened=None
        ) -> FakeSessionResult:
            callback = request.cwd / request.callback_pointer
            if not callback.parent.is_dir():
                raise AssertionError(
                    "current review must prepare callback scratch before start"
                )
            if self.fail_once:
                self.fail_once = False
                self.store.create(
                    request.spec,
                    lane_id=request.lane_id,
                    run_id=request.run_id,
                )
                raise RuntimeError("simulated pre-launch interruption")
            return super().start(
                request, on_surface_opened=on_surface_opened
            )

    current_store = OperationStore(product / ".vault-meta/harness")
    current_runtime = FailOnceCurrentRuntime(current_store)
    old_environment = {
        name: os.environ.get(name)
        for name in (
            "LLM_OBSIDIAN_SESSION_RUNTIME",
            "LLM_OBSIDIAN_SESSION_MODEL",
            "LLM_OBSIDIAN_SESSION_EFFORT",
        )
    }
    os.environ["LLM_OBSIDIAN_SESSION_RUNTIME"] = "codex"
    os.environ["LLM_OBSIDIAN_SESSION_MODEL"] = "gpt-5.6-sol"
    os.environ["LLM_OBSIDIAN_SESSION_EFFORT"] = "high"
    try:
        try:
            task_review_runner.run_current_review(
                product,
                origin_surface="33333333-3333-4333-8333-333333333333",
                scratch_root=scratch,
                runtime_manager=current_runtime,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError(
                "current review interruption fixture did not fail"
            )
        started = task_review_runner.run_current_review(
            product,
            scratch_root=scratch,
            runtime_manager=current_runtime,
        )
        current_gate_root = (
            product
            / ".vault-meta/harness/review-data"
            / started["task_id"]
            / started["task_id"]
        )
        manifest = Path(started["context_manifest"])
        check(
            "current checkout review needs no dispatch metadata and keeps scratch external",
            started["status"] == "reviewing"
            and not (product / ".task-meta.json").exists()
            and started["worktree"] == str(product.resolve())
            and started["vault_root"] == str(product.resolve())
            and len(current_runtime.started) == 1
            and current_runtime.started[0].product_root == product.resolve()
            and current_runtime.started[0].cwd != product.resolve()
            and "Typed current-review callback is ready"
            in current_runtime.started[0].callback_wake
            and "task-review-runner.py current"
            in current_runtime.started[0].callback_wake
            and f"--worktree {product.resolve()}"
            in current_runtime.started[0].callback_wake
            and manifest.is_file()
            and product.resolve() not in manifest.parents,
        )
        check(
            "current checkout review resumes its initialized pre-launch gate",
            json.loads(
                (current_gate_root / "review-gate.json").read_text(
                    encoding="utf-8"
                )
            )["status"]
            == "reviewing",
        )
        lane = started["lanes"][0]
        active = task_review_runner.load_active_round(
            current_gate_root,
            current_store,
            current_runtime,
            axis="holistic",
        )
        callback = review_round_envelope(
            active.round,
            ReviewResult(
                "holistic",
                "changes-requested",
                (
                    ReviewFinding(
                        "F-current-1",
                        "holistic",
                        "important",
                        "fix the current checkout",
                        "VALUE still equals one",
                        file="product.py",
                    ),
                ),
            ),
        )
        Path(lane["callback_path"]).write_text(
            json.dumps(to_dict(callback), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        waiting = task_review_runner.run_current_review(
            product,
            scratch_root=scratch,
            runtime_manager=current_runtime,
        )
        current_reviewed_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=product,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (product / "product.py").write_text("VALUE = 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "product.py"], cwd=product, check=True)
        subprocess.run(
            ["git", "commit", "-m", "resolve current review"],
            cwd=product,
            check=True,
            capture_output=True,
            text=True,
        )
        current_resolved_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=product,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (product / ".task-review-resolution.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation_id": started["task_id"],
                    "review_identity_sha256": resolution_transport_identity(
                        ReviewGateController(
                            current_gate_root,
                            current_runtime,
                            current_store,
                        )
                    ),
                    "reviewed_head_sha": current_reviewed_head,
                    "resolved_head_sha": current_resolved_head,
                    "resolutions": [
                        {
                            "finding_id": "F-current-1",
                            "disposition": "applied",
                            "rationale": (
                                "The current-checkout correction is present "
                                "on the resolved HEAD."
                            ),
                            "follow_up": "",
                        }
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        verifying = task_review_runner.run_current_review(
            product,
            scratch_root=scratch,
            runtime_manager=current_runtime,
        )
        check(
            "current checkout review reuses its durable gate and parent session",
            waiting["status"] == "awaiting-resolution"
            and verifying["status"] == "verifying"
            and verifying["task_id"] == started["task_id"]
            and len(current_runtime.started) == 1
            and len(current_runtime.continued) == 1
            and current_runtime.continued[0][1] == lane["operation_id"],
        )
        current_gate_state = json.loads(
            (current_gate_root / "review-gate.json").read_text(
                encoding="utf-8"
            )
        )
        current_gate_state["status"] = "attention-required"
        (current_gate_root / "review-gate.json").write_text(
            json.dumps(current_gate_state, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for record in current_store.list(started["task_id"]):
            if record.state in TERMINAL:
                continue
            current_store.transition(
                started["task_id"],
                record.spec.operation_id,
                "cancelling",
            )
            current_store.transition(
                started["task_id"],
                record.spec.operation_id,
                "exiting",
            )
            current_store.transition(
                started["task_id"],
                record.spec.operation_id,
                "cancelled",
            )
            cancelled = current_store.read(
                started["task_id"], record.spec.operation_id
            )
            if cancelled.resources != OwnedResources():
                current_store.save(
                    replace(
                        cancelled,
                        resources=OwnedResources(),
                        revision=cancelled.revision + 1,
                    ),
                    expected_revision=cancelled.revision,
                )
        restarted = task_review_runner.run_current_review(
            product,
            origin_surface="33333333-3333-4333-8333-333333333333",
            scratch_root=scratch,
            runtime_manager=current_runtime,
        )
        check(
            "quiescent attention gate permits one fresh current review",
            restarted["status"] == "reviewing"
            and restarted["task_id"] != started["task_id"],
        )
    finally:
        for name, value in old_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

with tempfile.TemporaryDirectory(prefix="pending-review-terminal.") as raw:
    unsafe_store = OperationStore(Path(raw) / "store")
    unsafe_context = ReviewContext(
        manifest="packets/review/manifest.json",
        head_sha="8" * 40,
        verification_profile="scoped",
        verification_profile_sha256="7" * 64,
    )
    unsafe_request = request_for(
        "pending-terminal",
        context=unsafe_context,
    )
    unsafe_identity = task_review_runner.review_session_specs(
        unsafe_request
    )[0]
    unsafe_store.create(
        unsafe_identity.spec,
        lane_id=unsafe_identity.lane_id,
        run_id=unsafe_identity.run_id,
    )
    unsafe_store.transition(
        unsafe_request.owner_id,
        unsafe_identity.spec.operation_id,
        "failed",
    )

    class PendingGateMarker:
        marked = False

        def mark_pending_attention(self) -> None:
            self.marked = True

    class UnusedRuntime:
        def status(self, owner_id: str, operation_id: str) -> object:
            raise AssertionError("terminal parent must not be probed")

    pending_gate = PendingGateMarker()
    check(
        "terminal parent cannot turn a pending gate into reviewing",
        not task_review_runner._pending_replay_is_safe(
            unsafe_request,
            unsafe_store,
            pending_gate,
            UnusedRuntime(),
        )
        and pending_gate.marked
        and unsafe_store.read(
            unsafe_request.owner_id,
            unsafe_identity.spec.operation_id,
        ).state
        == "failed",
    )

with tempfile.TemporaryDirectory(prefix="pending-review-dead.") as raw:
    dead_store = OperationStore(Path(raw) / "store")
    dead_context = ReviewContext(
        manifest="packets/review/manifest.json",
        head_sha="6" * 40,
        verification_profile="scoped",
        verification_profile_sha256="5" * 64,
    )
    dead_request = request_for("pending-dead", context=dead_context)
    dead_identity = task_review_runner.review_session_specs(dead_request)[0]
    dead_record = dead_store.create(
        dead_identity.spec,
        lane_id=dead_identity.lane_id,
        run_id=dead_identity.run_id,
    )
    dead_record = replace(
        dead_record,
        resources=replace(
            dead_record.resources,
            surface_id="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
            process_group=4321,
            supervisor_pid=4322,
            process_identity="4" * 64,
            supervisor_identity="3" * 64,
        ),
    )
    dead_store.save(dead_record, expected_revision=0)
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        dead_store.transition(
            dead_request.owner_id,
            dead_identity.spec.operation_id,
            state,
        )
    dead_record = dead_store.read(
        dead_request.owner_id, dead_identity.spec.operation_id
    )

    class DeadStatusRuntime:
        calls = 0

        def status(self, owner_id: str, operation_id: str) -> object:
            self.calls += 1
            return FakeSessionResult(
                dead_record,
                "checkpoint-dead",
                "observed",
                "dead",
                "alive",
            )

    dead_runtime = DeadStatusRuntime()
    dead_gate = PendingGateMarker()
    check(
        "dead identified parent cannot turn a pending gate into reviewing",
        not task_review_runner._pending_replay_is_safe(
            dead_request,
            dead_store,
            dead_gate,
            dead_runtime,
        )
        and dead_runtime.calls == 2
        and dead_gate.marked
        and dead_store.read(
            dead_request.owner_id,
            dead_identity.spec.operation_id,
        ).state
        == "attention-required",
    )

with tempfile.TemporaryDirectory(prefix="pending-review-retry.") as raw:
    retry_store = OperationStore(Path(raw) / "store")
    retry_context = ReviewContext(
        manifest="packets/review/manifest.json",
        head_sha="2" * 40,
        verification_profile="scoped",
        verification_profile_sha256="1" * 64,
    )
    retry_request = request_for("pending-retry", context=retry_context)
    retry_identity = task_review_runner.review_session_specs(
        retry_request
    )[0]
    retry_record = retry_store.create(
        retry_identity.spec,
        lane_id=retry_identity.lane_id,
        run_id=retry_identity.run_id,
    )
    retry_record = replace(
        retry_record,
        resources=replace(
            retry_record.resources,
            surface_id="BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB",
            process_group=5321,
            supervisor_pid=5322,
            process_identity="2" * 64,
            supervisor_identity="1" * 64,
        ),
    )
    retry_store.save(retry_record, expected_revision=0)
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        retry_store.transition(
            retry_request.owner_id,
            retry_identity.spec.operation_id,
            state,
        )

    class RetryStatusRuntime:
        calls = 0

        def status(self, owner_id: str, operation_id: str) -> object:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient read-only probe failure")
            return FakeSessionResult(
                retry_store.read(owner_id, operation_id),
                "checkpoint-retry",
                "observed",
                "alive",
                "alive",
            )

    retry_runtime = RetryStatusRuntime()
    retry_gate = PendingGateMarker()
    check(
        "pending replay retries one inconclusive read-only liveness probe",
        task_review_runner._pending_replay_is_safe(
            retry_request,
            retry_store,
            retry_gate,
            retry_runtime,
        )
        and retry_runtime.calls == 2
        and not retry_gate.marked,
    )

with tempfile.TemporaryDirectory(prefix="review-raced-parent.") as raw:
    raced_root = Path(raw)
    raced_store = OperationStore(raced_root / "store")
    raced_context = ReviewContext(
        manifest="packets/review/manifest.json",
        head_sha="9" * 40,
        verification_profile="scoped",
        verification_profile_sha256="8" * 64,
    )
    raced_request = request_for("raced-parent", context=raced_context)

    class RacedDeadRuntime(FakeRuntime):
        def start(
            self, request: object, *, on_surface_opened=None
        ) -> FakeSessionResult:
            record = self.store.create(
                request.spec,
                lane_id=request.lane_id,
                run_id=request.run_id,
            )
            record = replace(
                record,
                resources=replace(
                    record.resources,
                    surface_id="CCCCCCCC-CCCC-4CCC-8CCC-CCCCCCCCCCCC",
                    process_group=6321,
                    supervisor_pid=6322,
                    process_identity="9" * 64,
                    supervisor_identity="8" * 64,
                ),
            )
            self.store.save(record, expected_revision=0)
            for state in (
                "preflight",
                "starting",
                "running",
                "awaiting-callback",
            ):
                self.store.transition(
                    request.spec.owner_id,
                    request.spec.operation_id,
                    state,
                )
            return FakeSessionResult(
                self.store.read(
                    request.spec.owner_id, request.spec.operation_id
                ),
                "checkpoint-raced",
                "attention-required",
                "alive",
                "alive",
            )

    try:
        start_review(
            raced_request,
            RacedDeadRuntime(raced_store),
            origin_surface="44444444-4444-4444-8444-444444444444",
            cwd=raced_root,
            product_root=ROOT,
            prompt_pointer="prompts/review.md",
            callback_root="callbacks",
            round_store=raced_store,
        )
    except ValueError as exc:
        check(
            "raced supervisor-dead parent must prove resumable liveness",
            str(exc) == "stored review runtime is not live and resumable",
        )
    else:
        raise AssertionError("dead raced reviewer must not become reviewing")

with tempfile.TemporaryDirectory(prefix="fresh-review-preflight.") as raw:
    preflight_root = Path(raw)
    (preflight_root / "scratch").mkdir()
    preflight_store = OperationStore(preflight_root / "store")
    preflight_runtime = FakeRuntime(preflight_store)
    preflight_gate = ReviewGateController(
        preflight_root / "gate",
        preflight_runtime,
        preflight_store,
    )
    preflight_run = begin(
        preflight_gate,
        request_for("fresh-preflight", context=context),
        preflight_root / "scratch",
    )
    next_context = replace(
        context,
        manifest="packets/review/expanded-manifest.json",
    )
    preflight_boundary = ReviewScopeBoundary(
        "context",
        review_context_sha256(context),
        review_context_sha256(next_context),
        "approved bounded ContextPacket expansion",
    )
    preflight_authorization = {
        "schema_version": 1,
        "operation_id": "fresh-preflight",
        "kind": preflight_boundary.kind,
        "previous_context_sha256": (
            preflight_boundary.previous_context_sha256
        ),
        "next_context_sha256": preflight_boundary.next_context_sha256,
        "reason": preflight_boundary.reason,
        "authorization_provenance": "coordinator-approved",
        "verification_operation_id": "verification-preflight",
        "verification_receipt_sha256": "b" * 64,
        "status": "authorized",
    }
    preflight_authorization_path = (
        preflight_gate.root / "fresh-boundary-authorization.json"
    )
    preflight_authorization_path.write_text(
        json.dumps(preflight_authorization, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    preflight_gate._replace(status="attention-required")
    preflight_gate.authorize_fresh_boundary(
        preflight_run,
        boundary=preflight_boundary,
        authorization_pointer=preflight_authorization_path.name,
        authorization_sha256=hashlib.sha256(
            preflight_authorization_path.read_bytes()
        ).hexdigest(),
    )
    state_before_preflight = preflight_gate.read()
    try:
        preflight_gate.restart_for_boundary(
            preflight_run,
            boundary=preflight_boundary,
            context=next_context,
            origin_surface="55555555-5555-4555-8555-555555555555",
            cwd=ROOT,
            product_root=ROOT,
            prompt_pointer="prompts/compact.md",
            callback_root="callbacks/fresh-preflight",
        )
    except Exception as exc:
        isolation_rejected = (
            "isolated from product root" in str(exc)
        )
    else:
        isolation_rejected = False
    if not (
        isolation_rejected
        and preflight_gate.read() == state_before_preflight
    ):
        regression_failures.append(
            "scratch isolation preflight precedes fresh intent persistence"
        )

if regression_failures:
    raise AssertionError(
        "RED review recovery regressions: "
        + "; ".join(regression_failures)
    )

print("\nAll review gate tests passed.")
