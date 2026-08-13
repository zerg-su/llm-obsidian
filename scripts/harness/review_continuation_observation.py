"""Durable I/O adapter for the pure review-continuation policy."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .adapters.process import ProcessAdapter
from .review_continuation_recovery import (
    AcceptedCallback,
    AttemptSnapshot,
    GateSnapshot,
    RecoverySnapshot,
    ResolutionSnapshot,
    ReviewLane,
    RootSnapshot,
    VerificationSnapshot,
)
from .runtime_callback_io import _bounded_file_sha256
from .runtime_worker_contracts import RuntimeWorkerError
from .store import StoreError


class ReviewContinuationObservationError(RuntimeWorkerError):
    """Durable review-continuation evidence is unavailable or malformed."""


def _gate(worker: Any) -> tuple[bytes, dict[str, object], dict[str, object], dict[str, object], list[object]]:
    path = worker.review.gate_root / "review-gate.json"
    if not path.is_file() or path.is_symlink():
        raise ReviewContinuationObservationError("review gate state is unavailable")
    raw = path.read_bytes()
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("dispatch_operation_id") != worker.spec["operation_id"]
    ):
        raise ReviewContinuationObservationError("review gate state is invalid")
    attempt = value.get("attempt")
    identity = attempt.get("identity") if isinstance(attempt, dict) else None
    context = value.get("context")
    lanes = value.get("lanes")
    if (
        not isinstance(attempt, dict)
        or not isinstance(identity, dict)
        or not isinstance(context, dict)
        or not isinstance(lanes, list)
    ):
        raise ReviewContinuationObservationError("review continuation evidence is invalid")
    return raw, value, attempt, identity, lanes


def _head(worker: Any) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worker.spec["cwd"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ReviewContinuationObservationError("review continuation HEAD is unavailable")
    return result.stdout.strip()


def _root(worker: Any) -> object | None:
    try:
        return worker.store.read(worker.spec["owner_id"], worker.spec["operation_id"])
    except StoreError:
        # A durable reviewer launch can precede visibility of the outer record.
        # This permits only wait classification, never a recovery receipt.
        return None


def _ready_identity(
    worker: Any,
    parent: Any,
    operation_id: str,
    *,
    launch_in_progress: bool,
    parent_identity_exact: bool,
    process: ProcessAdapter,
) -> tuple[bool, bool]:
    path = (
        Path(worker.store.root)
        / "owners"
        / worker.spec["owner_id"]
        / "runtime"
        / operation_id
        / "ready.json"
    )
    if path.is_symlink():
        return False, False
    if launch_in_progress:
        if not path.is_file():
            return parent_identity_exact, False
        early = json.loads(path.read_text(encoding="utf-8"))
        return (
            parent_identity_exact
            and isinstance(early, dict)
            and early.get("status") == "ready",
            False,
        )
    if not path.is_file():
        return False, False
    ready = json.loads(path.read_text(encoding="utf-8"))
    process_group = ready.get("process_group")
    process_identity = str(ready.get("process_identity") or "")
    exact = (
        parent_identity_exact
        and isinstance(ready, dict)
        and ready.get("status") == "ready"
        and type(process_group) is int
        and process_group > 1
        and bool(re.fullmatch("[0-9a-f]{64}", process_identity))
        and parent.resources.process_group == process_group
        and parent.resources.process_identity == process_identity
    )
    alive = exact and process.process_status(process_group, process_identity) == "alive"
    return exact, alive


def _lanes(
    worker: Any, raw_lanes: list[object], rows: list[Any]
) -> tuple[list[ReviewLane], dict[str, tuple[object, str, bool]]]:
    observed: list[ReviewLane] = []
    parents: dict[str, tuple[object, str, bool]] = {}
    process = getattr(worker, "process", None) or ProcessAdapter()
    for raw_lane in raw_lanes:
        if not isinstance(raw_lane, dict):
            raise ReviewContinuationObservationError("review lane evidence is invalid")
        axis = str(raw_lane.get("axis") or "")
        operation_id = str(raw_lane.get("operation_id") or "")
        run_id = str(raw_lane.get("run_id") or "")
        lane_id = str(raw_lane.get("lane_id") or "")
        parent = worker.store.read(worker.spec["owner_id"], operation_id)
        parent_exact = (
            parent.spec.kind.startswith(("simple-review-", "deep-review-", "full-review-"))
            and parent.run_id == run_id
            and parent.lane_id == lane_id
        )
        launching = (
            parent.state in {"preflight", "starting", "running"}
            or parent.pending_effect == "start-provider"
        )
        ready_exact, alive = _ready_identity(
            worker,
            parent,
            operation_id,
            launch_in_progress=launching,
            parent_identity_exact=parent_exact,
            process=process,
        )
        parents[operation_id] = (parent, axis, parent_exact)
        rounds = [
            row
            for row in rows
            if row.spec.kind == "review-round"
            and row.spec.parent_operation_id == operation_id
        ]
        if not rounds:
            observed.append(
                ReviewLane(
                    axis=axis,
                    operation_id=operation_id,
                    run_id=run_id,
                    lane_id=lane_id,
                    state=parent.state,
                    round_operation_id="",
                    round_run_id="",
                    round_state="",
                    pending_effect=parent.pending_effect,
                    launch_in_progress=launching,
                    ready_identity_exact=ready_exact,
                )
            )
        observed.extend(
            ReviewLane(
                axis=axis,
                operation_id=operation_id,
                run_id=run_id,
                lane_id=lane_id,
                state=parent.state,
                round_operation_id=row.spec.operation_id,
                round_run_id=row.run_id,
                round_state=row.state,
                pending_effect=parent.pending_effect or row.pending_effect,
                launch_in_progress=launching,
                ready_identity_exact=ready_exact,
                process_alive=alive,
            )
            for row in rounds
        )
    return observed, parents


def _callbacks(
    rows: list[Any],
    parents: dict[str, tuple[object, str, bool]],
    attempt_id: str,
    gate: dict[str, object],
) -> tuple[list[AcceptedCallback], frozenset[str], bool]:
    callbacks: list[AcceptedCallback] = []
    consumed: set[str] = set()
    replay = False
    results = gate.get("round_results")
    consumed_axes = set(results) if isinstance(results, dict) else set()
    for row in rows:
        parent_data = parents.get(row.spec.parent_operation_id)
        if (
            row.spec.kind != "review-round"
            or not row.accepted_callback_id
            or parent_data is None
        ):
            continue
        parent, axis, parent_exact = parent_data
        callbacks.append(
            AcceptedCallback(
                attempt_id=attempt_id,
                axis=axis,
                callback_id=row.accepted_callback_id,
                kind=row.accepted_callback_kind,
                lane_id=row.lane_id,
                operation_id=row.spec.operation_id,
                parent_operation_id=row.spec.parent_operation_id,
                payload_sha256=row.accepted_callback_sha256,
                run_id=row.run_id,
            )
        )
        if axis in consumed_axes:
            consumed.add(row.accepted_callback_id)
        replay = replay or bool(not parent_exact or parent.pending_effect or row.pending_effect)
    return callbacks, frozenset(consumed), replay


def _latch_status(worker: Any) -> str:
    path = worker.spec_path.parent / "callback-error.json"
    if not path.is_file() or path.is_symlink():
        return ""
    value = json.loads(path.read_text(encoding="utf-8"))
    return str(value.get("status") or "") if isinstance(value, dict) else ""


def _resolution(worker: Any, current_head: str) -> ResolutionSnapshot | None:
    path = worker.spec_path.parent / "pipeline-review-resolution-notify.json"
    if not path.is_file() or path.is_symlink():
        return None
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        return None
    return ResolutionSnapshot(
        reviewed_head=str(value.get("reviewed_head_sha") or ""),
        current_head=current_head,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _verification(worker: Any) -> VerificationSnapshot | None:
    if not hasattr(worker, "verification_receipt"):
        return None
    value = worker.verification_receipt()
    if not isinstance(value, dict):
        return None
    receipt_sha256 = _bounded_file_sha256(worker.verification_receipt_path)
    return VerificationSnapshot(
        status=str(value.get("status") or ""),
        head=str(value.get("head_sha") or ""),
        receipt_sha256=receipt_sha256 or "",
    )


def _cycle_limits(worker: Any, identity: dict[str, object]) -> tuple[int, int]:
    raw_cycle = identity.get("cycle")
    cycle = raw_cycle if type(raw_cycle) is int else -1
    meta = getattr(worker, "meta", None)
    policy = meta.get("finalization_policy") if isinstance(meta, dict) else None
    configured = policy.get("max_cycles") if isinstance(policy, dict) else None
    maximum = (
        configured
        if type(configured) is int and configured > 0
        else max(cycle + 1, 1)
    )
    return cycle, maximum


def observe_review_continuation(worker: Any) -> RecoverySnapshot:
    """Build one immutable policy snapshot from exact durable records."""

    gate_raw, gate, attempt, identity, raw_lanes = _gate(worker)
    current_head = _head(worker)
    root = _root(worker)
    rows = worker.store.list(worker.spec["owner_id"])
    lanes, parents = _lanes(worker, raw_lanes, rows)
    callbacks, consumed, replay = _callbacks(
        rows, parents, str(identity.get("attempt_id") or ""), gate
    )
    cycle, maximum = _cycle_limits(worker, identity)
    context = gate["context"]
    recovery_class = (
        "accepted-callback"
        if callbacks and gate.get("status") == "reviewing"
        else "review-drive"
    )
    return RecoverySnapshot(
        recovery_class=recovery_class,
        root=RootSnapshot(
            owner_id=worker.spec["owner_id"],
            operation_id=worker.spec["operation_id"],
            run_id=root.run_id if root is not None else "",
            revision=root.revision if root is not None else -1,
            state=root.state if root is not None else "awaiting-callback",
            resume_state=root.resume_state if root is not None else "",
            pending_effect=root.pending_effect if root is not None else "",
        ),
        gate=GateSnapshot(
            status=str(gate.get("status") or ""),
            sha256=hashlib.sha256(gate_raw).hexdigest(),
            context_head=str(context.get("head_sha") or ""),
        ),
        attempt=AttemptSnapshot(
            attempt_id=str(identity.get("attempt_id") or ""),
            status=str(attempt.get("status") or ""),
            exact_head=str(identity.get("exact_head_sha") or ""),
            cycle=cycle,
            max_cycles=maximum,
        ),
        current_head=current_head,
        attention_status=_latch_status(worker),
        resolution=_resolution(worker, current_head),
        verification=_verification(worker),
        lanes=tuple(lanes),
        accepted_callbacks=tuple(callbacks),
        consumed_callback_ids=consumed,
        effect_requires_replay=replay,
    )
