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
from harness.workflows.review import (  # noqa: E402
    ReviewContext,
    ReviewOperationRequest,
    ReviewResult,
    review_round_envelope,
)
from harness.workflows.review_gate import (  # noqa: E402
    ReviewGateController,
    ReviewPreset,
)
from task_review_flow import (  # noqa: E402
    _complete_ready_results,
    _ready_result_is_recorded,
    _review_origin_surface,
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

    def start(self, request: object, *, on_surface_opened=None) -> SessionResult:
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
        preset=preset,
        context=context,
        worktree=base,
        vault=base,
        runtime_root=runtime_root,
        exact_attempt=True,
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
        preset=preset,
        context=context,
        worktree=base,
        vault=base,
        runtime_root=runtime_root,
        exact_attempt=True,
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
                preset=ReviewPreset.from_flags(
                    deep=mode == "deep", full=mode == "full"
                ),
                context=context,
                worktree=base,
                vault=base,
                runtime_root=runtime_root,
                exact_attempt=True,
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
                preset=ReviewPreset.from_flags(
                    deep=mode == "deep", full=mode == "full"
                ),
                context=context,
                worktree=base,
                vault=base,
                runtime_root=runtime_root,
                exact_attempt=True,
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

print("\nAll task review flow unit tests passed.")
