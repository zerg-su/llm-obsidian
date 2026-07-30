#!/usr/bin/env python3
"""Production-seam regressions for the automatic review gate."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT / "scripts"))

from harness.callbacks import CallbackBroker
from harness.adapters.claude import ClaudeDriver
from harness.contracts import AttentionReason, RuntimeRoute, to_dict
from harness.store import OperationStore
from harness.verification import load_profiles
from harness.workflows.review import (
    ReviewContext,
    ReviewFinding,
    ReviewOperationRequest,
    ReviewResult,
    review_round_envelope,
)
from harness.workflows.review_gate import (
    ReviewGateController,
    ReviewPreset,
    ReviewScopeBoundary,
    authorize_task_finalization,
    review_context_sha256,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


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
        return self.store.read(owner_id, operation_id)

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
    first = controller.continue_after_resolution(
        run,
        lane,
        context=resolved_context,
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
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(controller, request_for("review-budget", context=context), scratch)
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
    fresh = controller.restart_for_boundary(
        run,
        boundary=boundary,
        context=new_context,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=ROOT,
        prompt_pointer="prompts/compact.md",
        callback_root="callbacks/review-budget-fresh",
    )
    repeated = controller.restart_for_boundary(
        fresh,
        boundary=boundary,
        context=new_context,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=ROOT,
        prompt_pointer="prompts/compact.md",
        callback_root="callbacks/review-budget-fresh",
    )
    check(
        "one explicit scope/context boundary permits one fresh compact run",
        fresh is not None
        and len(runtime.started) == 2
        and repeated is None
        and controller.read()["status"] == "attention-required",
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
    task_review_runner = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(task_review_runner)
    task_store = OperationStore(vault / ".vault-meta/harness")
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
            str(product.resolve() / "scripts/harness/review_submit.py"),
            "--worktree",
            str(product.resolve()),
            "--state-dir",
            str(callback_path.parent),
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
        f"`{submit}`" in prompt_text and f"Bash({submit})" in claude_command,
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
    (product / "product.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.py"], cwd=product, check=True)
    subprocess.run(
        ["git", "commit", "-m", "resolve review"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    )
    verifying = task_review_runner.run_task_review(
        product, runtime_manager=task_runtime
    )
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
        (product / "product.py").write_text("VALUE = 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "product.py"], cwd=product, check=True)
        subprocess.run(
            ["git", "commit", "-m", "resolve current review"],
            cwd=product,
            check=True,
            capture_output=True,
            text=True,
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
    finally:
        for name, value in old_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

print("\nAll review gate tests passed.")
