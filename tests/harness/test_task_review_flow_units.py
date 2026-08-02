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
)
from harness.workflows.review_gate import (  # noqa: E402
    ReviewGateController,
    ReviewPreset,
)
from task_review_flow import _resume_bound_attention  # noqa: E402
from task_review_request import _callback_path  # noqa: E402


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
        preset.request("flow-review"),
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
    callback = _callback_path(runtime_root, "holistic")
    callback.parent.mkdir(parents=True)
    callback.write_text("{}\n", encoding="utf-8")
    gate._replace(status="attention-required")
    base_state = gate.read()

    invalid_states: tuple[tuple[str, dict[str, object]], ...] = (
        ("non-attention status", {**base_state, "status": "reviewing"}),
        ("empty lanes", {**base_state, "lanes": []}),
        ("missing owner", {**base_state, "owner_id": ""}),
        ("non-object lane", {**base_state, "lanes": ["holistic"]}),
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


print("\nAll task review flow unit tests passed.")
