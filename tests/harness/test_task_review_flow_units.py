#!/usr/bin/env python3
"""Deterministic state table for resuming bound review attention."""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import (  # noqa: E402
    ContractError,
    OperationRecord,
    RuntimeRoute,
)
from harness.store import OperationStore  # noqa: E402
from harness.workflows.review import (  # noqa: E402
    ReviewContext,
    ReviewOperationRequest,
    ReviewResult,
)
from harness.workflows.review_gate import (  # noqa: E402
    ReviewGateController,
    ReviewPreset,
)
from task_review_flow import (  # noqa: E402
    _ready_result_is_recorded,
    _resume_bound_attention,
)
from task_review_request import _callback_path  # noqa: E402
from task_review_resolution_flow import _prompt_effect_id  # noqa: E402
from harness.runtime_session_contracts import continuation_effect_id  # noqa: E402


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


@dataclass(frozen=True)
class SessionResult:
    record: OperationRecord
    checkpoint: str
    action: str = "observed"
    process_status: str = "alive"
    surface_status: str = "alive"


class FakeRuntime:
    """Fake only the provider transport around real gate/store state."""

    def __init__(self, store: OperationStore) -> None:
        self.store = store

    def start(self, request: object, *, on_surface_opened=None) -> SessionResult:
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

    def status(self, owner_id: str, operation_id: str) -> SessionResult:
        return SessionResult(
            self.store.read(owner_id, operation_id), "checkpoint-live"
        )

    def register_callback_target(self, *_args: object) -> None:
        return None


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
    callback = _callback_path(runtime_root, lane.axis)
    callback.parent.mkdir(parents=True)
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

    prompt = runtime_root / "prompts" / "verify.md"
    prompt.parent.mkdir()
    prompt.write_text("# Verify exact HEAD\n", encoding="utf-8")
    target = base / "callback-target.json"
    target.write_text(
        '{"generation":2,"operation_id":"old-round","run_id":"old-run",'
        '"schema_version":1}\n',
        encoding="utf-8",
    )
    expected = continuation_effect_id(
        "# Verify exact HEAD\n\n3\nnew-round\nnew-run"
    )
    pending_effect = _prompt_effect_id(
        runtime_root,
        "prompts/verify.md",
        callback_target_path=target,
        callback_operation_id="new-round",
        callback_run_id="new-run",
    )
    target.write_text(
        '{"generation":3,"operation_id":"new-round","run_id":"new-run",'
        '"schema_version":1}\n',
        encoding="utf-8",
    )
    replay_effect = _prompt_effect_id(
        runtime_root,
        "prompts/verify.md",
        callback_target_path=target,
        callback_operation_id="new-round",
        callback_run_id="new-run",
    )
    check(
        "gate continuation identity matches runtime generation binding",
        pending_effect == expected and replay_effect == expected,
    )


print("\nAll task review flow unit tests passed.")
