#!/usr/bin/env python3
"""Deterministic state table for resuming bound review attention."""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.callbacks import CallbackBroker  # noqa: E402
from harness.contracts import (  # noqa: E402
    CapabilityReport,
    ContractError,
    OperationRecord,
    OwnedResources,
    RuntimeRoute,
    to_dict,
)
from harness.store import OperationStore  # noqa: E402
from harness.review_finalization import StructuralPivotPending  # noqa: E402
from harness.review_attempt import ReviewAttemptError  # noqa: E402
from harness.workflows.review import (  # noqa: E402
    ReviewContext,
    ReviewFinding,
    ReviewOperationRequest,
    ReviewResult,
    review_round_envelope,
)
from harness.workflows.review_gate import (  # noqa: E402
    ReviewGateController,
    ReviewPreset,
)
from task_review_flow import (  # noqa: E402
    _archive_prior_terminal_callbacks,
    _archive_resolution_callbacks,
    _complete_ready_results,
    _ready_result_is_recorded,
    _resolution_source_state,
    _review_origin_surface,
    _reserve_or_reviewing,
    _resume_bound_attention,
)
from task_review_request import _callback_path  # noqa: E402
from task_review_shared import StaleRoundCallbackError  # noqa: E402
from task_review_transport import (  # noqa: E402
    _collect_ready_results,
    _write_round_meta,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


class SurfaceProbe:
    def __init__(self, states: dict[str, str]) -> None:
        self.states = states

    def status(self, surface_id: str) -> str:
        return self.states.get(surface_id, "unknown")


class SurfaceRuntime:
    def __init__(self, states: dict[str, str]) -> None:
        self.cmux = SurfaceProbe(states)


surface_meta = {"task_surface": "task-old", "wiki_surface": "wiki-live"}
check(
    "effect-free retry anchors to the live coordinator after task surface loss",
    _review_origin_surface(
        surface_meta,
        SurfaceRuntime({"task-old": "dead", "wiki-live": "alive"}),
        allow_fallback=True,
    )
    == "wiki-live",
)


check(
    "ordinary review never changes its frozen task origin",
    _review_origin_surface(
        surface_meta,
        SurfaceRuntime({"task-old": "dead", "wiki-live": "alive"}),
        allow_fallback=False,
    )
    == "task-old",
)


def pending_pivot_reservation() -> object:
    raise StructuralPivotPending("structural pivot is awaiting its callback")


check(
    "pending structural pivot returns a reviewing receipt",
    _reserve_or_reviewing(
        pending_pivot_reservation,
        lambda: {"status": "reviewing"},
    )
    == {"status": "reviewing"},
)


@dataclass(frozen=True)
class SessionResult:
    record: OperationRecord
    checkpoint: str
    action: str = "observed"
    process_status: str = "alive"
    surface_status: str = "alive"


class FakeRuntime:
    """Fake only the provider transport around real gate/store state."""

    def __init__(
        self, store: OperationStore, owner_id: str = "flow-owner"
    ) -> None:
        self.store = store
        self.owner_id = owner_id
        self.started = 0
        self.accepted = 0

    def start(
        self,
        request: object,
        *,
        on_surface_opened=None,
        admit_provider_start=None,
    ) -> SessionResult:
        if admit_provider_start is not None:
            admit_provider_start()
        self.started += 1
        record = self.store.create(
            request.spec,
            lane_id=request.lane_id,
            run_id=request.run_id,
        )
        updated = replace(
            record,
            resources=replace(
                record.resources,
                surface_id="11111111-aaaa-4aaa-8aaa-111111111111",
            ),
            revision=record.revision + 1,
        )
        self.store.save(updated, expected_revision=record.revision)
        result = SessionResult(updated, "checkpoint-1")
        if on_surface_opened is not None:
            on_surface_opened(result)
        return result

    def preflight_routes(
        self,
        requests: tuple[tuple[RuntimeRoute, Path, str], ...],
    ) -> tuple[CapabilityReport, ...]:
        return tuple(
            CapabilityReport(route, True, ("provider:profile-valid",))
            for route, _callback_dir, _origin_surface in requests
        )

    def status(self, owner_id: str, operation_id: str) -> SessionResult:
        return SessionResult(
            self.store.read(owner_id, operation_id), "checkpoint-1"
        )

    def register_callback_target(self, *_args: object) -> None:
        return None

    def accept_callback(self, envelope: object) -> object:
        self.accepted += 1
        return CallbackBroker(self.store, self.owner_id).accept(envelope)

    def request_exit(self, owner_id: str, operation_id: str) -> object:
        record = self.store.read(owner_id, operation_id)
        if record.state not in {"complete", "failed", "cancelled"}:
            if record.state in {
                "created",
                "preflight",
                "starting",
                "attention-required",
            }:
                self.store.transition(owner_id, operation_id, "cancelling")
            elif record.state != "finalizing":
                self.store.transition(owner_id, operation_id, "finalizing")
            self.store.transition(owner_id, operation_id, "exiting")
        return self.store.read(owner_id, operation_id)

    def cleanup(self, owner_id: str, operation_id: str) -> object:
        record = self.store.read(owner_id, operation_id)
        if record.state == "exiting":
            self.store.transition(owner_id, operation_id, "complete")
        completed = self.store.read(owner_id, operation_id)
        if completed.resources != OwnedResources():
            self.store.save(
                replace(
                    completed,
                    resources=OwnedResources(),
                    revision=completed.revision + 1,
                ),
                expected_revision=completed.revision,
            )
        return self.store.read(owner_id, operation_id)


with tempfile.TemporaryDirectory(prefix="review-resolution-outbox.") as raw:
    base = Path(raw)
    runtime_root = base / "runtime"
    runtime_root.mkdir()
    product_root = base / "product"
    product_root.mkdir()
    store = OperationStore(base / "store")
    owner_id = "resolution-owner"
    runtime = FakeRuntime(store, owner_id=owner_id)
    gate = ReviewGateController(base / "gate", runtime, store)
    context = ReviewContext(
        "packets/review/manifest.json",
        "a" * 40,
        "scoped",
        "b" * 64,
    )
    preset = ReviewPreset.from_flags()
    request = ReviewOperationRequest(
        preset.request("resolution-review", selected_provider="openai"),
        owner_id,
        RuntimeRoute(
            "codex",
            "gpt-5.6-sol",
            "xhigh",
            "reviewer-callback",
            "c" * 64,
        ),
        context,
    )
    run = gate.begin_attempt(
        dispatch_operation_id="resolution-review",
        finalization_lineage_id="resolution-review",
        cycle=1,
        plan_sha256="d" * 64,
        outcome_sha256="e" * 64,
        request=request,
        origin_surface="22222222-bbbb-4bbb-8bbb-222222222222",
        cwd=runtime_root,
        product_root=product_root,
        prompt_pointer="prompts/review.md",
        callback_root="callbacks",
    )
    lane = run.execution.lanes[0]
    round_ = run.rounds[lane.axis]
    callback = _callback_path(runtime_root, lane.axis)
    callback.parent.mkdir(parents=True, exist_ok=True)
    callback.write_text(
        json.dumps(
            to_dict(
                review_round_envelope(
                    round_,
                    ReviewResult(
                        lane.axis,
                        "changes-requested",
                        (
                            ReviewFinding(
                                finding_id="F-resolution",
                                axis=lane.axis,
                                severity="important",
                                summary="Resolution is required.",
                                evidence="The exact callback must be archived.",
                                file="product.py",
                                line=1,
                                recommendation="Apply the bounded repair.",
                            ),
                        ),
                        0,
                    ),
                )
            ),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    ready = _collect_ready_results(run, runtime_root, base, base)
    _complete_ready_results(
        gate=gate,
        run=run,
        ready=ready,
        worktree=base,
        vault=base,
        runtime_root=runtime_root,
    )
    changes_state = gate.read()
    callback_bytes = callback.read_bytes()
    _archive_resolution_callbacks(runtime_root, changes_state)
    boundary = changes_state["review_notification_evidence"][lane.axis]
    archive = (
        callback.parent
        / "accepted"
        / f"{boundary['callback_sha256']}.review-callback.json"
    )
    _archive_resolution_callbacks(runtime_root, changes_state)
    check(
        "accepted resolution callback is archived idempotently before retry",
        not callback.exists()
        and archive.is_file()
        and archive.read_bytes() == callback_bytes,
    )
    (gate.root / "attempts").mkdir()
    (gate.root / "attempts" / "cycle-1.json").write_text(
        json.dumps(changes_state, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    zero_lane = json.loads(json.dumps(changes_state))
    zero_lane["status"] = "attention-required"
    zero_lane["attempt"]["identity"]["cycle"] = 2
    zero_lane["attempt"]["identity"]["exact_head_sha"] = "f" * 40
    zero_lane["attempt"]["terminal"] = {
        "schema_version": 1,
        "result": "attention-required",
        "exact_head_sha": "f" * 40,
        "lane_results": [],
    }
    zero_lane.pop("review_notification_evidence", None)
    check(
        "zero-lane restart recovers the preceding exact finding boundary",
        _resolution_source_state(gate.root, zero_lane) == changes_state,
    )
    archive.replace(callback)
    _archive_prior_terminal_callbacks(
        runtime_root,
        gate.root,
        zero_lane,
        store,
    )
    check(
        "zero-lane retry archives the exact callback from a prior terminal cycle",
        not callback.exists()
        and archive.is_file()
        and archive.read_bytes() == callback_bytes,
    )
    archive.replace(callback)
    foreign = json.loads(callback.read_text(encoding="utf-8"))
    foreign["callback_id"] = "review-foreign-callback"
    callback.write_text(
        json.dumps(foreign, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        _archive_prior_terminal_callbacks(
            runtime_root,
            gate.root,
            zero_lane,
            store,
        )
    except ReviewAttemptError as exc:
        rejected = "ownership is ambiguous" in str(exc)
    else:
        rejected = False
    check(
        "zero-lane retry preserves and rejects an unowned callback",
        rejected and callback.is_file() and not archive.exists(),
    )


with tempfile.TemporaryDirectory(prefix="review-flow-recorded.") as raw:
    base = Path(raw)
    store = OperationStore(base / "store")
    gate = ReviewGateController(base / "gate", FakeRuntime(store), store)
    result = ReviewResult("openai-intent", "approve", (), 2)
    result_path = gate.root / "review" / "round-openai-intent-1.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        '{"axis":"openai-intent","verdict":"changes-requested",'
        '"findings":[],"verification_iteration":1}\n',
        encoding="utf-8",
    )
    state = {
        "round_results": {
            "openai-intent": "review/round-openai-intent-1.json"
        }
    }
    check(
        "older result for the same axis does not hide a ready iteration",
        not _ready_result_is_recorded(gate, state, result),
    )
    result_path.write_text(
        '{"axis":"openai-intent","verdict":"approve",'
        '"findings":[],"verification_iteration":2}\n',
        encoding="utf-8",
    )
    check(
        "the exact recorded axis iteration remains idempotently filtered",
        _ready_result_is_recorded(gate, state, result),
    )


with tempfile.TemporaryDirectory(prefix="review-flow-units.") as raw:
    base = Path(raw)
    runtime_root = base / "runtime"
    runtime_root.mkdir()
    product_root = base / "product"
    product_root.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    gate = ReviewGateController(base / "gate", runtime, store)
    owner_id = "flow-owner"
    context = ReviewContext(
        "packets/review/manifest.json",
        "c" * 40,
        "scoped",
        "d" * 64,
    )
    preset = ReviewPreset.from_flags()
    request = ReviewOperationRequest(
        preset.request("flow-review", selected_provider="openai"),
        owner_id,
        RuntimeRoute(
            "codex",
            "gpt-5.6-sol",
            "high",
            "reviewer-callback",
            "f" * 64,
        ),
        context,
    )
    run = gate.begin(
        dispatch_operation_id="flow-dispatch",
        request=request,
        origin_surface="22222222-bbbb-4bbb-8bbb-222222222222",
        cwd=runtime_root,
        product_root=product_root,
        prompt_pointer="prompts/review.md",
        callback_root="callbacks",
    )
    lane = run.execution.lanes[0]
    _write_round_meta(
        runtime_root=runtime_root,
        vault=product_root,
        worktree=product_root,
        task_id="flow-review",
        depth="simple",
        context=context,
        lane_operation_id=lane.operation_id,
        round_=run.rounds[lane.axis],
    )
    round_meta = __import__("json").loads(
        (runtime_root / "callbacks" / lane.axis / ".review-meta.json").read_text(
            encoding="utf-8"
        )
    )
    check(
        "purpose-bound round metadata preserves the exact configured route",
        round_meta["route"] == {
            "runtime": "codex",
            "model": "gpt-5.6-sol",
            "effort": "high",
            "profile": "reviewer-callback",
            "routing_sha256": "f" * 64,
        },
    )
    callback = _callback_path(runtime_root, lane.axis)
    callback.parent.mkdir(parents=True, exist_ok=True)
    callback.write_text("{}\n", encoding="utf-8")
    gate._replace(status="attention-required")
    base_state = gate.read()

    invalid_states: tuple[tuple[str, dict[str, object]], ...] = (
        ("non-attention status", {**base_state, "status": "reviewing"}),
        ("empty lanes", {**base_state, "lanes": []}),
        ("missing owner", {**base_state, "owner_id": ""}),
        ("non-object lane", {**base_state, "lanes": ["openai-holistic"]}),
        (
            "missing axis",
            {
                **base_state,
                "lanes": [{**base_state["lanes"][0], "axis": ""}],
            },
        ),
        (
            "missing operation",
            {
                **base_state,
                "lanes": [
                    {**base_state["lanes"][0], "operation_id": ""}
                ],
            },
        ),
        (
            "missing store row",
            {
                **base_state,
                "lanes": [
                    {
                        **base_state["lanes"][0],
                        "operation_id": "unknown-operation",
                    }
                ],
            },
        ),
        (
            "one invalid lane in a multi-lane state",
            {
                **base_state,
                "lanes": [
                    base_state["lanes"][0],
                    {
                        **base_state["lanes"][0],
                        "operation_id": "unknown-operation",
                    },
                ],
            },
        ),
    )
    for label, state in invalid_states:
        try:
            returned_state, returned_status = _resume_bound_attention(
                gate, store, runtime_root, state
            )
        except ContractError:
            returned_state = returned_status = None
        check(
            f"bound attention remains paused for {label}",
            gate.read()["status"] == "attention-required"
            and (
                returned_state is None
                or (
                    returned_state == state
                    and returned_status == str(state.get("status") or "")
                )
            ),
        )

    created_state, created_status = _resume_bound_attention(
        gate, store, runtime_root, base_state
    )
    check(
        "bound attention remains paused while its runtime row is created",
        created_state == base_state
        and created_status == "attention-required"
        and gate.read()["status"] == "attention-required",
    )

    callback.unlink()
    missing_callback_state, missing_callback_status = _resume_bound_attention(
        gate, store, runtime_root, base_state
    )
    check(
        "bound attention remains paused without its exact callback",
        missing_callback_state == base_state
        and missing_callback_status == "attention-required",
    )
    symlink_target = base / "callback-target.json"
    symlink_target.write_text("{}\n", encoding="utf-8")
    callback.symlink_to(symlink_target)
    symlink_state, symlink_status = _resume_bound_attention(
        gate, store, runtime_root, base_state
    )
    check(
        "bound attention remains paused for a symlink callback",
        symlink_state == base_state
        and symlink_status == "attention-required",
    )
    callback.unlink()
    callback.write_text("{}\n", encoding="utf-8")

    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(owner_id, lane.operation_id, state)
    resumed_state, resumed_status = _resume_bound_attention(
        gate, store, runtime_root, base_state
    )
    check(
        "exact callback and awaiting runtime row resume the real gate once",
        resumed_status == "reviewing"
        and resumed_state["status"] == "reviewing"
        and gate.read()["status"] == "reviewing",
    )

    gate._replace(
        status="attention-required",
        awaiting_resolution={
            lane.axis: {
                "reviewed_head_sha": context.head_sha,
                "round_operation_id": "accepted-round-1",
            }
        },
    )
    resolution_state = gate.read()
    rebound_state, rebound_status = _resume_bound_attention(
        gate, store, runtime_root, resolution_state
    )
    check(
        "resolved runtime attention restores the awaiting-resolution phase",
        rebound_status == "awaiting-resolution"
        and rebound_state["status"] == "awaiting-resolution"
        and gate.read()["status"] == "awaiting-resolution",
    )

    store.transition(owner_id, lane.operation_id, "running")
    gate._replace(status="attention-required")
    resumed_continuation_state, resumed_continuation_status = (
        _resume_bound_attention(
            gate,
            store,
            runtime_root,
            gate.read(),
        )
    )
    check(
        "resolved running continuation restores awaiting-resolution",
        resumed_continuation_status == "awaiting-resolution"
        and resumed_continuation_state["status"] == "awaiting-resolution"
        and gate.read()["status"] == "awaiting-resolution",
    )

    gate._replace(status="attention-required", awaiting_resolution={})
    unbound_running_state, unbound_running_status = _resume_bound_attention(
        gate,
        store,
        runtime_root,
        gate.read(),
    )
    check(
        "running review without a resolution boundary remains paused",
        unbound_running_status == "attention-required"
        and unbound_running_state["status"] == "attention-required"
        and gate.read()["status"] == "attention-required",
    )


with tempfile.TemporaryDirectory(prefix="review-prefix-ingestion.") as raw:
    base = Path(raw)
    runtime_root = base / "runtime"
    runtime_root.mkdir()
    product_root = base / "product"
    product_root.mkdir()
    store = OperationStore(base / "store")
    owner_id = "prefix-owner"
    runtime = FakeRuntime(store, owner_id=owner_id)
    gate = ReviewGateController(base / "gate", runtime, store)
    context = ReviewContext(
        "packets/review/manifest.json",
        "e" * 40,
        "scoped",
        "f" * 64,
    )
    preset = ReviewPreset.from_flags(deep=True)
    policy = preset.request("prefix-review")
    anthropic = RuntimeRoute(
        "claude",
        "claude-opus-5",
        "xhigh",
        "reviewer-callback",
        "1" * 64,
    )
    openai = RuntimeRoute(
        "codex",
        "gpt-5.6-sol",
        "xhigh",
        "reviewer-callback",
        "1" * 64,
    )
    request = ReviewOperationRequest(
        policy,
        owner_id,
        anthropic,
        context,
        axis_routes={
            "anthropic-holistic": anthropic,
            "openai-holistic": openai,
        },
    )
    run = gate.begin_attempt(
        dispatch_operation_id="prefix-review",
        finalization_lineage_id="prefix-review",
        cycle=1,
        plan_sha256="2" * 64,
        outcome_sha256="3" * 64,
        request=request,
        origin_surface="22222222-bbbb-4bbb-8bbb-222222222222",
        cwd=runtime_root,
        product_root=product_root,
        prompt_pointer="prompts/review.md",
        callback_root="callbacks",
    )
    first_lane, second_lane = run.execution.lanes

    def publish(lane: object) -> None:
        round_ = run.rounds[lane.axis]
        callback = _callback_path(runtime_root, lane.axis)
        callback.parent.mkdir(parents=True, exist_ok=True)
        callback.write_text(
            json.dumps(
                to_dict(
                    review_round_envelope(
                        round_, ReviewResult(lane.axis, "approve", (), 0)
                    )
                ),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    publish(first_lane)
    first_ready = _collect_ready_results(run, runtime_root, base, base)
    first_decisions = _complete_ready_results(
        gate=gate,
        run=run,
        ready=first_ready,
        worktree=base,
        vault=base,
        runtime_root=runtime_root,
    )
    prefix_state = gate.read()
    check(
        "the first exact callback is durable before its sibling exists",
        first_decisions == ("awaiting-axes",)
        and prefix_state["status"] == "reviewing"
        and prefix_state["attempt"]["status"] == "awaiting-callback"
        and set(prefix_state["final_results"]) == {first_lane.axis}
        and runtime.accepted == 1
        and runtime.started == 2,
    )

    recovered = gate.rehydrate_attempt()
    check(
        "restart preserves the accepted prefix without reviewer replay",
        runtime.started == 2
        and set(gate.read()["final_results"]) == {first_lane.axis},
    )
    publish(second_lane)
    final_ready = _collect_ready_results(recovered, runtime_root, base, base)
    final_decisions = _complete_ready_results(
        gate=gate,
        run=recovered,
        ready=final_ready,
        worktree=base,
        vault=base,
        runtime_root=runtime_root,
    )
    terminal_state = gate.read()
    check(
        "the first missing callback terminalizes once after restart",
        final_decisions == ("approved",)
        and terminal_state["status"] == "approved"
        and terminal_state["attempt"]["status"] == "terminal"
        and set(terminal_state["final_results"])
        == {first_lane.axis, second_lane.axis}
        and runtime.accepted == 2
        and runtime.started == 2,
    )


def prefix_request(
    *,
    mode: str,
    operation_id: str,
    owner_id: str,
    context: ReviewContext,
) -> ReviewOperationRequest:
    anthropic = RuntimeRoute(
        "claude",
        "claude-opus-5",
        "xhigh",
        "reviewer-callback",
        "4" * 64,
    )
    openai = RuntimeRoute(
        "codex",
        "gpt-5.6-sol",
        "xhigh",
        "reviewer-callback",
        "5" * 64,
    )
    preset = ReviewPreset.from_flags(
        deep=mode == "deep", full=mode == "full"
    )
    policy = preset.request(
        operation_id,
        selected_provider="openai" if mode != "full" else "",
    )
    if mode == "full":
        axis_routes = {
            "anthropic-intent": anthropic,
            "anthropic-engineering": anthropic,
            "openai-intent": openai,
            "openai-engineering": openai,
        }
        return ReviewOperationRequest(
            policy,
            owner_id,
            anthropic,
            context,
            axis_routes=axis_routes,
        )
    return ReviewOperationRequest(policy, owner_id, openai, context)


for mode, expected_lanes in (("simple", 1), ("deep", 2), ("full", 4)):
    with tempfile.TemporaryDirectory(
        prefix=f"review-{mode}-prefix-matrix."
    ) as raw:
        base = Path(raw)
        runtime_root = base / "runtime"
        runtime_root.mkdir()
        product_root = base / "product"
        product_root.mkdir()
        store = OperationStore(base / "store")
        owner_id = f"{mode}-prefix-owner"
        runtime = FakeRuntime(store, owner_id=owner_id)
        gate = ReviewGateController(base / "gate", runtime, store)
        context = ReviewContext(
            "packets/review/manifest.json",
            "6" * 40,
            "scoped",
            "7" * 64,
        )
        operation_id = f"{mode}-prefix-review"
        request = prefix_request(
            mode=mode,
            operation_id=operation_id,
            owner_id=owner_id,
            context=context,
        )
        run = gate.begin_attempt(
            dispatch_operation_id=operation_id,
            finalization_lineage_id=operation_id,
            cycle=1,
            plan_sha256="8" * 64,
            outcome_sha256="9" * 64,
            request=request,
            origin_surface="22222222-bbbb-4bbb-8bbb-222222222222",
            cwd=runtime_root,
            product_root=product_root,
            prompt_pointer="prompts/review.md",
            callback_root="callbacks",
        )
        lanes = run.execution.lanes
        check(
            f"{mode} prefix matrix starts every reviewer exactly once",
            len(lanes) == expected_lanes
            and runtime.started == expected_lanes
            and runtime.accepted == 0,
        )

        initial = gate.rehydrate_attempt()
        check(
            f"{mode} zero-prefix restart performs no reviewer replay",
            runtime.started == expected_lanes
            and not gate.read()["final_results"],
        )

        foreign_lane = lanes[0]
        foreign_round = initial.rounds[foreign_lane.axis]
        foreign_path = _callback_path(runtime_root, foreign_lane.axis)
        foreign_path.parent.mkdir(parents=True, exist_ok=True)
        foreign_payload = to_dict(
            review_round_envelope(
                foreign_round,
                ReviewResult(foreign_lane.axis, "approve", (), 0),
            )
        )
        foreign_payload["operation_id"] = "foreign-operation"
        foreign_path.write_text(
            json.dumps(foreign_payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            _collect_ready_results(initial, runtime_root, base, base)
        except StaleRoundCallbackError:
            foreign_rejected = True
        else:
            foreign_rejected = False
        check(
            f"{mode} foreign callback rejects before durable mutation",
            foreign_rejected
            and runtime.accepted == 0
            and not gate.read()["final_results"],
        )
        foreign_path.unlink()

        recovered = initial
        for prefix, lane in enumerate(lanes, start=1):
            round_ = recovered.rounds[lane.axis]
            callback = _callback_path(runtime_root, lane.axis)
            callback.parent.mkdir(parents=True, exist_ok=True)
            callback.write_text(
                json.dumps(
                    to_dict(
                        review_round_envelope(
                            round_, ReviewResult(lane.axis, "approve", (), 0)
                        )
                    ),
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            ready = _collect_ready_results(
                recovered, runtime_root, base, base
            )
            decisions = _complete_ready_results(
                gate=gate,
                run=recovered,
                ready=ready,
                worktree=base,
                vault=base,
                runtime_root=runtime_root,
            )
            state = gate.read()
            terminal = prefix == expected_lanes
            check(
                f"{mode} accepted prefix {prefix} is durable and exact",
                decisions
                == (("approved",) if terminal else ("awaiting-axes",))
                and len(state["final_results"]) == prefix
                and runtime.accepted == prefix
                and runtime.started == expected_lanes
                and state["status"]
                == ("approved" if terminal else "reviewing"),
            )
            parent = store.read(lane.owner_id, lane.operation_id)
            child = store.read(round_.owner_id, round_.operation_id)
            check(
                f"{mode} accepted prefix {prefix} releases exact resources",
                parent.state == "complete"
                and parent.resources == OwnedResources()
                and child.state == "complete"
                and child.resources == OwnedResources(),
            )

            duplicate_ready = _collect_ready_results(
                recovered, runtime_root, base, base
            )
            duplicate_decisions = _complete_ready_results(
                gate=gate,
                run=recovered,
                ready=duplicate_ready,
                worktree=base,
                vault=base,
                runtime_root=runtime_root,
            )
            check(
                f"{mode} duplicate prefix {prefix} performs zero effects",
                duplicate_decisions == ()
                and runtime.accepted == prefix
                and runtime.started == expected_lanes,
            )
            if not terminal:
                recovered = gate.rehydrate_attempt()
                check(
                    f"{mode} restart after prefix {prefix} resumes first missing",
                    len(gate.read()["final_results"]) == prefix
                    and runtime.accepted == prefix
                    and runtime.started == expected_lanes,
                )

# 2.7.5 F274.POST_CHECK_LAUNCH_RACE (with F275.RECEIPT_ADMISSION_NOT_CONSUMED
# applied): the exact-HEAD flow consumes the durable verification receipt
# named by the pipeline's launch admission through the real
# VerificationAuthority ingress and verifies it against the actual review
# context before reservation or provider effect.  A verify-owning contract
# requires the admission — absence is fail-closed, never compatibility — and
# any HEAD, receipt-identity, clean-state, or launch-input mismatch, or a
# malformed/symlinked admission, is never treated as absent.
import hashlib  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

from harness.contracts import (  # noqa: E402
    EffectOutcome,
    OperationSpec,
    VerificationEvidence,
)
from harness.pipeline_builtins import compiled_builtin  # noqa: E402
from harness.verification import (  # noqa: E402
    VerificationAuthority,
    load_profiles,
)
from harness.verification_attempt import (  # noqa: E402
    VERIFICATION_STEP_SCHEMA_VERSION,
    VerificationAttempt,
    pipeline_verify_effect_id,
    pipeline_verify_identity,
    verification_input_sha256,
)
from task_review_flow import _admitted_review_launch  # noqa: E402
from task_review_shared import TaskReviewError  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

with tempfile.TemporaryDirectory(prefix="launch-admission.") as raw:
    base = Path(raw)
    worktree = base / "product"
    worktree.mkdir()
    for argv in (
        ("init", "-b", "main"),
        ("config", "user.email", "admission@example.invalid"),
        ("config", "user.name", "Admission World"),
    ):
        subprocess.run(
            ["git", "-C", str(worktree), *argv], check=True, capture_output=True
        )

    def land_commit(name: str) -> str:
        (worktree / "product.txt").write_text(name + "\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(worktree), "add", "product.txt"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(worktree), "commit", "-m", name],
            check=True,
            capture_output=True,
        )
        return subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    head_b = land_commit("base")
    vault = base / "vault"
    (vault / "config").mkdir(parents=True)
    shutil.copy(
        ROOT / "config" / "verification-profiles.toml",
        vault / "config" / "verification-profiles.toml",
    )
    profile = load_profiles(ROOT / "config" / "verification-profiles.toml")[
        "scoped"
    ]
    meta = {
        "review_policy": {
            "verification_profile": "scoped",
            "verification_profile_sha256": profile.sha256,
        }
    }
    admission_route = RuntimeRoute(
        "claude", "sonnet", "medium", "executor", "6" * 64
    )
    verify_contract = compiled_builtin("engineering/change").definition_sha256
    no_verify_contract = compiled_builtin(
        "lifecycle/default"
    ).definition_sha256
    task_id = "admission-task"
    store = OperationStore(vault / ".vault-meta" / "harness")
    parent = store.create(
        OperationSpec(
            task_id,
            f"{task_id}-key",
            "dispatch",
            task_id,
            admission_route,
            "packets/task.json",
            "scoped",
            contract_sha256=verify_contract,
        ),
        lane_id="task-lane",
        run_id="task-run",
    )
    dispatch_runtime = store.root / "owners" / task_id / "runtime" / task_id

    input_sha = verification_input_sha256(
        verify_contract, head_b, profile.sha256, VERIFICATION_STEP_SCHEMA_VERSION
    )
    attempt = VerificationAttempt(
        task_id, profile.name, profile.sha256, head_b, 0
    )
    child_spec, child_lane, child_run = pipeline_verify_identity(
        parent.spec,
        definition_sha256=verify_contract,
        input_sha256=input_sha,
        profile=profile.name,
        attempt_index=0,
    )
    store.create(child_spec, lane_id=child_lane, run_id=child_run)
    for state in ("preflight", "starting", "running", "verifying"):
        store.transition(task_id, child_spec.operation_id, state)
    effect_id = pipeline_verify_effect_id(input_sha, 0)
    store.begin_effect(task_id, child_spec.operation_id, effect_id)
    store.resolve_effect(
        task_id, child_spec.operation_id, EffectOutcome.SUCCEEDED
    )
    for state in ("finalizing", "exiting", "complete"):
        store.transition(task_id, child_spec.operation_id, state)
    receipt_dir = (
        dispatch_runtime / "pipeline-verification" / child_spec.operation_id
    )
    evidence_rows = []
    for index in range(len(profile.commands)):
        output = receipt_dir / "evidence" / f"scoped-{index + 1}.log"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"ok\n")
        evidence_rows.append(
            VerificationEvidence(
                profile.name,
                profile.sha256,
                head_b,
                f"scoped-{index + 1}",
                ".",
                0,
                "1",
                "2",
                output.relative_to(dispatch_runtime).as_posix(),
                hashlib.sha256(b"ok\n").hexdigest(),
                3,
                2,
            )
        )
    authority = VerificationAuthority.issue(
        store=store,
        parent=parent,
        runtime_root=dispatch_runtime,
        definition_sha256=verify_contract,
        input_sha256=input_sha,
        profile=profile.name,
        profile_sha256=profile.sha256,
        attempt=attempt,
        evidence=tuple(evidence_rows),
        expected_command_ids=tuple(
            f"scoped-{index + 1}" for index in range(len(profile.commands))
        ),
    )
    receipt = authority.to_dict()
    receipt_path = receipt_dir / "receipt.json"
    receipt_bytes = (
        json.dumps(receipt, sort_keys=True) + "\n"
    ).encode("utf-8")
    receipt_path.write_bytes(receipt_bytes)
    receipt_sha256 = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    runtime_root = base / "review-runtime"
    runtime_root.mkdir()
    admission_path = runtime_root / "review-launch-admission.json"
    exact_admission = {
        "schema_version": 1,
        "operation_id": task_id,
        "verification_operation_id": receipt["operation_id"],
        "verification_lane_id": receipt["lane_id"],
        "verification_run_id": receipt["run_id"],
        "receipt_sha256": receipt_sha256,
        "receipt_pointer": str(receipt_path.resolve()),
        "head_sha": head_b,
        "status": "admitted",
    }

    def write_admission(value: object) -> None:
        if admission_path.is_symlink() or admission_path.exists():
            admission_path.unlink()
        if isinstance(value, str):
            admission_path.write_text(value, encoding="utf-8")
        else:
            admission_path.write_text(
                json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
            )

    def context_at(head: str) -> ReviewContext:
        return ReviewContext(
            manifest="packets/review/manifest.json",
            head_sha=head,
            verification_profile="scoped",
            verification_profile_sha256=profile.sha256,
            purpose="implementation",
            boundary_input_sha256="",
        )

    def admission_verdict(head: str, *, task: str = task_id) -> str:
        try:
            _admitted_review_launch(
                meta, vault, runtime_root, worktree, task, context_at(head)
            )
        except TaskReviewError as exc:
            return str(exc)
        return "admitted"

    check(
        "a verify-owning contract requires the launch admission — absence "
        "fails closed",
        "required" in admission_verdict(head_b),
    )
    write_admission(exact_admission)
    check(
        "the exact durable receipt/HEAD pair admits the clean launch",
        admission_verdict(head_b) == "admitted",
    )
    check(
        "a context bound to another HEAD is refused before reservation",
        "another HEAD" in admission_verdict("c" * 40),
    )
    write_admission({**exact_admission, "receipt_sha256": "a" * 64})
    check(
        "a well-formed admission that disagrees with the durable receipt "
        "digest fails closed",
        "disagrees" in admission_verdict(head_b),
    )
    write_admission(
        {**exact_admission, "verification_lane_id": "forged-lane"}
    )
    check(
        "an admission naming a foreign lane identity fails closed",
        "disagrees" in admission_verdict(head_b),
    )
    forged_pointer = receipt_path.resolve().parent.parent / "forged-verify-op" / "receipt.json"
    write_admission(
        {
            **exact_admission,
            "verification_operation_id": "forged-verify-op",
            "receipt_pointer": str(forged_pointer),
        }
    )
    check(
        "an admission naming a receiptless verification identity fails "
        "closed",
        "no durable verification receipt" in admission_verdict(head_b),
    )
    write_admission(
        {
            **exact_admission,
            "receipt_pointer": "pipeline-verification/x/receipt.json",
        }
    )
    check(
        "a relative receipt pointer fails closed",
        "invalid" in admission_verdict(head_b),
    )
    write_admission(
        {
            **exact_admission,
            "receipt_pointer": str(
                receipt_path.resolve().with_name("other.json")
            ),
        }
    )
    check(
        "a receipt pointer outside the exact admitted identity fails closed",
        "invalid" in admission_verdict(head_b),
    )
    write_admission(exact_admission)
    receipt_path.unlink()
    check(
        "a deleted durable receipt refuses the admitted launch",
        "no durable verification receipt" in admission_verdict(head_b),
    )
    mutated = dict(receipt)
    mutated["head_sha"] = "d" * 40
    receipt_path.write_text(
        json.dumps(mutated, sort_keys=True) + "\n", encoding="utf-8"
    )
    check(
        "a mutated durable receipt never validates through the authority "
        "ingress",
        "no durable verification receipt" in admission_verdict(head_b),
    )
    receipt_path.write_bytes(receipt_bytes)
    check(
        "the restored exact receipt admits again",
        admission_verdict(head_b) == "admitted",
    )
    (worktree / "junk.txt").write_text("dirt\n", encoding="utf-8")
    check(
        "same-HEAD dirt refuses the admitted launch",
        "stale" in admission_verdict(head_b),
    )
    (worktree / "junk.txt").unlink()
    admission_rows = (
        ("foreign task", {**exact_admission, "operation_id": "foreign"}),
        ("drifted schema", {**exact_admission, "schema_version": 2}),
        ("unadmitted status", {**exact_admission, "status": "pending"}),
        (
            "short receipt digest",
            {**exact_admission, "receipt_sha256": "a" * 63},
        ),
        (
            "empty lane identity",
            {**exact_admission, "verification_lane_id": ""},
        ),
        ("extra launch input", {**exact_admission, "verified": True}),
        ("malformed bytes", "{malformed admission"),
    )
    for admission_case, admission_value in admission_rows:
        write_admission(admission_value)
        check(
            f"a {admission_case} launch admission fails closed, never absent",
            "invalid" in admission_verdict(head_b)
            or "unavailable" in admission_verdict(head_b),
        )
    admission_path.unlink()
    admission_path.symlink_to(runtime_root / "missing-admission.json")
    check(
        "a symlinked launch admission fails closed",
        "invalid" in admission_verdict(head_b),
    )
    admission_path.unlink()
    write_admission(exact_admission)
    head_c = land_commit("moved")
    check(
        "a clean commit after the admission refuses the stale pair at the "
        "effect boundary",
        "stale" in admission_verdict(head_b)
        and "another HEAD" in admission_verdict(head_c),
    )
    subprocess.run(
        ["git", "-C", str(worktree), "reset", "--hard", head_b],
        check=True,
        capture_output=True,
    )

    no_verify_task = "no-verify-task"
    store.create(
        OperationSpec(
            no_verify_task,
            f"{no_verify_task}-key",
            "dispatch",
            no_verify_task,
            admission_route,
            "packets/task.json",
            "scoped",
            contract_sha256=no_verify_contract,
        ),
        lane_id="plain-lane",
        run_id="plain-run",
    )
    plain_runtime = base / "plain-runtime"
    plain_runtime.mkdir()

    def plain_verdict(task: str) -> str:
        try:
            _admitted_review_launch(
                meta, vault, plain_runtime, worktree, task, context_at(head_b)
            )
        except TaskReviewError as exc:
            return str(exc)
        return "admitted"

    check(
        "a contract without a verification owner keeps its existing gates "
        "when no admission exists",
        plain_verdict(no_verify_task) == "admitted",
    )
    legacy_empty_task = "legacy-empty-task"
    store.create(
        OperationSpec(
            legacy_empty_task,
            f"{legacy_empty_task}-key",
            "dispatch",
            legacy_empty_task,
            admission_route,
            "packets/task.json",
            "scoped",
            contract_sha256="",
        ),
        lane_id="legacy-empty-lane",
        run_id="legacy-empty-run",
    )
    check(
        "a legacy dispatch record with an empty contract keeps its existing "
        "gates when no admission exists",
        plain_verdict(legacy_empty_task) == "admitted",
    )
    check(
        "a task without a dispatch record keeps its existing gates when no "
        "admission exists",
        plain_verdict("recordless-task") == "admitted",
    )
    unresolved_task = "unresolved-contract-task"
    unresolved_record = store.create(
        OperationSpec(
            unresolved_task,
            f"{unresolved_task}-key",
            "dispatch",
            unresolved_task,
            admission_route,
            "packets/task.json",
            "scoped",
            contract_sha256="f" * 64,
        ),
        lane_id="unresolved-lane",
        run_id="unresolved-run",
    )
    check(
        "a present nonempty contract that canonical resolvers cannot compile "
        "fails closed before any reservation or provider effect",
        "cannot be resolved" in plain_verdict(unresolved_task)
        and store.list(unresolved_task) == [unresolved_record],
    )
    (plain_runtime / "review-launch-admission.json").write_text(
        json.dumps(
            {**exact_admission, "operation_id": "recordless-task"},
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    check(
        "a present admission without a resolvable dispatch contract fails "
        "closed",
        "no resolvable dispatch contract" in plain_verdict("recordless-task"),
    )

print("\nAll task review flow unit tests passed.")
