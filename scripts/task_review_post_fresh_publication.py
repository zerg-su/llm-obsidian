"""Crash-idempotent publication of an already-started fresh review."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from time import time
from typing import Callable, Mapping

from harness.contracts import EffectOutcome, OwnedResources
from harness.post_verification_review_drive import (
    _apply_transition,
    _publish_fresh_marker,
    _validated_receipt,
)
from harness.post_verification_review_drive_boundary import (
    IDENTIFIER,
    MARKER_NAME,
    RECEIPT_NAME,
    SHA256,
    PostVerificationReviewDriveError,
    canonical,
    regular_json,
    review_drive_sha256,
    sha256,
)
from harness.store import OperationStore, StoreError
from harness.workflows.review_gate import ReviewGateController
from review_contract import review_parent_kind, validate_review_axes
from task_review_drift_contract import PostFreshPublicationSyncAuthorization
from task_review_drift_evidence import evidence_root
from task_review_drift_quarantine import synchronize_drift_quarantine_fresh
from task_review_post_fresh_state import (
    applied_publication,
    preserved_continuation,
)


SYNC_RECEIPT_NAME = "post-fresh-publication-sync.json"
SYNC_FIELDS = {
    "schema_version",
    "status",
    "operation_id",
    "fresh_review_operation_id",
    "authorization_record_id",
    "authorization_record_sha256",
    "continuation_authorization_record_id",
    "continuation_authorization_record_sha256",
    "continuation_receipt_sha256",
    "source_gate_sha256",
    "target_gate_sha256",
    "source_progress_sha256",
    "target_progress_sha256",
    "fresh_lane_bindings",
    "fresh_marker_sha256",
    "progress_at",
    "os_signals_sent",
    "cmux_signals_sent",
    "callback_effects_replayed",
    "provider_effects_replayed",
    "reviews_started",
    "binding_sha256",
}


def _binding(receipt: Mapping[str, object]) -> str:
    return sha256(
        canonical(
            {
                key: value
                for key, value in receipt.items()
                if key not in {"status", "binding_sha256"}
            }
        )
    )


def _encoded(value: Mapping[str, object]) -> bytes:
    return canonical(dict(value), newline=True)


def _write(path: Path, value: Mapping[str, object]) -> None:
    OperationStore._write(path, dict(value))


def _sync_receipt(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    value, _raw = regular_json(path, "post-fresh publication receipt")
    if (
        set(value) != SYNC_FIELDS
        or value.get("schema_version") != 1
        or value.get("status") not in {"prepared", "applied"}
        or value.get("binding_sha256") != _binding(value)
        or any(
            value.get(field) != 0
            for field in (
                "os_signals_sent",
                "cmux_signals_sent",
                "callback_effects_replayed",
                "provider_effects_replayed",
                "reviews_started",
            )
        )
    ):
        raise PostVerificationReviewDriveError(
            "post-fresh publication receipt is invalid"
        )
    for field in (
        "authorization_record_sha256",
        "continuation_authorization_record_sha256",
        "continuation_receipt_sha256",
        "source_gate_sha256",
        "target_gate_sha256",
        "source_progress_sha256",
        "target_progress_sha256",
        "fresh_marker_sha256",
        "binding_sha256",
    ):
        if not SHA256.fullmatch(str(value.get(field) or "")):
            raise PostVerificationReviewDriveError(
                "post-fresh publication digest is invalid"
            )
    if (
        not IDENTIFIER.fullmatch(str(value.get("operation_id") or ""))
        or not IDENTIFIER.fullmatch(
            str(value.get("fresh_review_operation_id") or "")
        )
        or not isinstance(value.get("fresh_lane_bindings"), list)
        or len(value["fresh_lane_bindings"]) != 2
        or not isinstance(value.get("progress_at"), (int, float))
        or isinstance(value.get("progress_at"), bool)
        or not math.isfinite(float(value["progress_at"]))
        or float(value["progress_at"]) <= 0
    ):
        raise PostVerificationReviewDriveError(
            "post-fresh publication identity is invalid"
        )
    return value


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files or root.is_symlink():
        raise PostVerificationReviewDriveError(
            "fresh provider evidence is unavailable"
        )
    for path in files:
        if path.is_symlink():
            raise PostVerificationReviewDriveError(
                "fresh provider evidence is invalid"
            )
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _provider_events(
    runtime_root: Path,
    *,
    owner_id: str,
    operation_id: str,
    run_id: str,
    resources: OwnedResources,
) -> str:
    provider_root = runtime_root / "provider-events"
    generations = sorted(
        path for path in provider_root.glob("generation-*") if path.is_dir()
    )
    if len(generations) != 1 or generations[0].name != "generation-2":
        raise PostVerificationReviewDriveError(
            "fresh provider generation is not exact"
        )
    event_paths = sorted((generations[0] / "events").glob("*.json"))
    events = [regular_json(path, "fresh provider event")[0] for path in event_paths]
    if len(events) != 2 or [event.get("kind") for event in events] != [
        "provider-started",
        "input-accepted",
    ]:
        raise PostVerificationReviewDriveError(
            "fresh provider event boundary is not exact"
        )
    for event in events:
        identity = event.get("identity")
        if (
            not isinstance(identity, dict)
            or identity.get("owner_id") != owner_id
            or identity.get("operation_id") != operation_id
            or identity.get("run_id") != run_id
            or identity.get("generation") != 2
            or identity.get("process_identity") != resources.process_identity
            or identity.get("surface_id") != resources.surface_id
        ):
            raise PostVerificationReviewDriveError(
                "fresh provider event identity drifted"
            )
    delivery, _raw = regular_json(
        generations[0] / "delivery" / "delivery-state.json",
        "fresh provider delivery state",
    )
    cursor = delivery.get("cursor")
    if (
        delivery.get("send_status") != "accepted"
        or delivery.get("send_attempts") != 1
        or delivery.get("callback_submits") != 0
        or not isinstance(cursor, dict)
        or cursor.get("last_sequence") != 2
        or cursor.get("provider_started") is not True
        or cursor.get("input_accepted") is not True
        or cursor.get("result_published") is not False
        or cursor.get("process_exited") is not False
        or cursor.get("resource_closed") is not False
        or cursor.get("event_gap") is not False
    ):
        raise PostVerificationReviewDriveError(
            "fresh provider delivery state drifted"
        )
    return _tree_sha256(provider_root)


def _runtime_binding(
    *,
    worktree: Path,
    store: OperationStore,
    owner_id: str,
    axis: str,
    lane: Mapping[str, object],
    parent: object,
    child: object,
) -> dict[str, object]:
    operation_id = str(lane.get("operation_id") or "")
    runtime_root = store.root / "owners" / owner_id / "runtime" / operation_id
    session, session_raw = regular_json(runtime_root / "session.json", "fresh session")
    launch, launch_raw = regular_json(runtime_root / "launch.json", "fresh launch")
    ready, ready_raw = regular_json(runtime_root / "ready.json", "fresh ready")
    target, target_raw = regular_json(
        runtime_root / "callback-target.json", "fresh callback target"
    )
    resources = parent.resources
    if (
        session.get("schema_version") != 1
        or session.get("operation_id") != operation_id
        or session.get("run_id") != parent.run_id
        or session.get("callback_mode") != "envelope"
        or session.get("placement") != "workspace"
        or Path(str(session.get("cwd") or "")).resolve() != worktree
        or Path(str(session.get("product_root") or "")).resolve() != worktree
        or launch.get("schema_version") != 1
        or launch.get("owner_id") != owner_id
        or launch.get("operation_id") != operation_id
        or launch.get("run_id") != parent.run_id
        or launch.get("surface_id") != resources.surface_id
        or launch.get("runtime") != "codex"
        or launch.get("callback_mode") != "envelope"
        or launch.get("reviewer_sandbox") is not True
        or Path(str(launch.get("cwd") or "")).resolve() != worktree
        or Path(str(launch.get("product_root") or "")).resolve() != worktree
        or ready
        != {
            "schema_version": 1,
            "status": "ready",
            "pid": resources.process_group,
            "process_group": resources.process_group,
            "process_identity": resources.process_identity,
            "supervisor_pid": resources.supervisor_pid,
            "supervisor_identity": resources.supervisor_identity,
        }
        or target.get("schema_version") != 1
        or target.get("generation") != 2
        or target.get("operation_id") != child.spec.operation_id
        or target.get("run_id") != child.run_id
        or not str(target.get("callback_pointer") or "")
    ):
        raise PostVerificationReviewDriveError(
            "fresh provider runtime identity drifted"
        )
    return {
        "axis": axis,
        "parent_operation_id": operation_id,
        "parent_run_id": parent.run_id,
        "parent_record_sha256": sha256(
            store._operation_path(owner_id, operation_id).read_bytes()
        ),
        "round_operation_id": child.spec.operation_id,
        "round_run_id": child.run_id,
        "round_record_sha256": sha256(
            store._operation_path(owner_id, child.spec.operation_id).read_bytes()
        ),
        "session_sha256": sha256(session_raw),
        "launch_sha256": sha256(launch_raw),
        "ready_sha256": sha256(ready_raw),
        "callback_target_sha256": sha256(target_raw),
        "provider_events_sha256": _provider_events(
            runtime_root,
            owner_id=owner_id,
            operation_id=operation_id,
            run_id=parent.run_id,
            resources=resources,
        ),
    }


def _fresh_gate_identity(
    *,
    worktree: Path,
    store: OperationStore,
    operation_id: str,
    continuation: Mapping[str, object],
    gate: Mapping[str, object],
    allow_published: bool,
) -> tuple[str, str, list[Mapping[str, object]], tuple[str, ...]]:
    policy = gate.get("policy")
    meta, _meta_raw = regular_json(worktree / ".task-meta.json", "task metadata")
    review_policy = meta.get("review_policy")
    context = gate.get("context")
    boundary = gate.get("fresh_boundary")
    authorization_identity = gate.get("fresh_boundary_authorization")
    lanes = gate.get("lanes")
    if (
        gate.get("schema_version") != 1
        or gate.get("dispatch_operation_id") != operation_id
        or gate.get("owner_id") != operation_id
        or gate.get("status")
        not in (
            {"attention-required", "reviewing"}
            if allow_published
            else {"attention-required"}
        )
        or gate.get("fresh_reevaluation_used") is not True
        or not isinstance(policy, dict)
        or policy.get("depth") != "deep"
        or policy.get("runtime") != "codex"
        or not IDENTIFIER.fullmatch(str(policy.get("model") or ""))
        or policy.get("effort") != "xhigh"
        or policy.get("cross_model") is not False
        or policy.get("max_verify_iterations") != 0
        or not isinstance(context, dict)
        or context.get("head_sha") != continuation.get("target_head_sha")
        or not isinstance(boundary, dict)
        or boundary.get("next_context_sha256") != context.get("sha256")
        or not isinstance(authorization_identity, dict)
        or authorization_identity.get("status") != "authorized"
        or not isinstance(lanes, list)
        or len(lanes) != 2
        or not isinstance(review_policy, dict)
        or review_policy.get("runtime") != "codex"
        or review_policy.get("model") != "sol"
        or review_policy.get("effort") != "xhigh"
    ):
        raise PostVerificationReviewDriveError("fresh review gate is not exact")
    kind = str(boundary.get("kind") or "")
    next_context = str(boundary.get("next_context_sha256") or "")
    role = f"fresh:{kind}:{next_context}"
    suffix = f"-fresh-{hashlib.sha256(role.encode()).hexdigest()[:8]}"
    old_review_id = str(continuation.get("active_review_operation_id") or "")
    expected_fresh_id = f"{old_review_id[:128-len(suffix)]}{suffix}"
    if gate.get("active_review_operation_id") != expected_fresh_id:
        raise PostVerificationReviewDriveError("fresh review identity drifted")
    gate_root = store.root / "review-data" / operation_id / operation_id
    pointer = Path(str(authorization_identity.get("pointer") or ""))
    authorization_path = (gate_root / pointer).resolve()
    try:
        authorization_path.relative_to(gate_root.resolve())
    except ValueError as exc:
        raise PostVerificationReviewDriveError(
            "fresh boundary authorization escapes the gate"
        ) from exc
    authorization, authorization_raw = regular_json(
        authorization_path, "fresh boundary authorization"
    )
    if (
        sha256(authorization_raw) != authorization_identity.get("sha256")
        or authorization.get("status") != "authorized"
        or authorization.get("operation_id") != old_review_id
        or authorization.get("dispatch_operation_id") != operation_id
        or authorization.get("kind") != kind
        or authorization.get("previous_context_sha256")
        != boundary.get("previous_context_sha256")
        or authorization.get("next_context_sha256") != next_context
        or authorization.get("reason") != boundary.get("reason")
        or authorization.get("verification_operation_id")
        != continuation.get("source_verification_operation_id")
        or authorization.get("verification_receipt_sha256")
        != continuation.get("source_verification_receipt_sha256")
    ):
        raise PostVerificationReviewDriveError(
            "fresh boundary authorization drifted"
        )
    axes = tuple(
        str(lane.get("axis") or "")
        for lane in lanes
        if isinstance(lane, dict)
    )
    try:
        validate_review_axes("deep", axes)
    except ValueError as exc:
        raise PostVerificationReviewDriveError("fresh review axes drifted") from exc
    if any(not axis.startswith("openai-") for axis in axes):
        raise PostVerificationReviewDriveError("fresh review provider drifted")
    return expected_fresh_id, str(policy["model"]), lanes, axes


def _fresh_operation_binding(
    *,
    worktree: Path,
    store: OperationStore,
    operation_id: str,
    raw_lane: Mapping[str, object],
    axis: str,
    expected_model: str,
    operations: list[object],
) -> tuple[dict[str, object], set[str]]:
    parent_id = str(raw_lane.get("operation_id") or "")
    try:
        parent = store.read(operation_id, parent_id)
    except StoreError as exc:
        raise PostVerificationReviewDriveError(
            "fresh review parent is unavailable"
        ) from exc
    children = [
        record
        for record in operations
        if record.spec.parent_operation_id == parent_id
        and record.spec.kind == "review-round"
    ]
    resources = parent.resources
    if (
        len(children) != 1
        or parent.spec.owner_id != operation_id
        or parent.spec.operation_id != parent_id
        or parent.spec.kind != review_parent_kind(axis)
        or parent.spec.route.runtime != "codex"
        or parent.spec.route.model != expected_model
        or parent.spec.route.effort != "xhigh"
        or parent.spec.route.profile != "reviewer-callback"
        or parent.state != "awaiting-callback"
        or parent.pending_effect
        or parent.effect_id != "start-provider"
        or parent.effect_outcome != EffectOutcome.SUCCEEDED
        or parent.accepted_callback_id
        or parent.accepted_callback_kind
        or parent.accepted_callback_sha256
        or not resources.surface_id
        or resources.process_group <= 1
        or resources.supervisor_pid <= 1
        or not SHA256.fullmatch(resources.process_identity)
        or not SHA256.fullmatch(resources.supervisor_identity)
        or raw_lane.get("lane_id") != parent.lane_id
        or raw_lane.get("run_id") != parent.run_id
        or raw_lane.get("surface_id") != resources.surface_id
        or raw_lane.get("state") != "awaiting-callback"
        or raw_lane.get("verification_iteration") != 0
    ):
        raise PostVerificationReviewDriveError(
            "fresh review parent identity drifted"
        )
    child = children[0]
    if (
        child.spec.owner_id != operation_id
        or child.spec.route != parent.spec.route
        or child.lane_id != parent.lane_id
        or child.state != "awaiting-callback"
        or child.resources != OwnedResources()
        or child.pending_effect
        or child.effect_id
        or child.accepted_callback_id
        or child.accepted_callback_kind
        or child.accepted_callback_sha256
    ):
        raise PostVerificationReviewDriveError(
            "fresh review round identity drifted"
        )
    return (
        _runtime_binding(
            worktree=worktree,
            store=store,
            owner_id=operation_id,
            axis=axis,
            lane=raw_lane,
            parent=parent,
            child=child,
        ),
        {parent_id, child.spec.operation_id},
    )


def _fresh_lanes(
    *,
    worktree: Path,
    store: OperationStore,
    operation_id: str,
    continuation: Mapping[str, object],
    gate: Mapping[str, object],
    allow_published: bool,
) -> tuple[str, list[dict[str, object]]]:
    expected_fresh_id, expected_model, lanes, axes = _fresh_gate_identity(
        worktree=worktree,
        store=store,
        operation_id=operation_id,
        continuation=continuation,
        gate=gate,
        allow_published=allow_published,
    )
    operations = store.list(operation_id)
    bindings: list[dict[str, object]] = []
    expected_ids: set[str] = set()
    for raw_lane, axis in zip(lanes, axes):
        binding, identifiers = _fresh_operation_binding(
            worktree=worktree,
            store=store,
            operation_id=operation_id,
            raw_lane=raw_lane,
            axis=axis,
            expected_model=expected_model,
            operations=operations,
        )
        expected_ids.update(identifiers)
        bindings.append(binding)
    fresh_operations = {
        record.spec.operation_id
        for record in operations
        if record.spec.operation_id.startswith(expected_fresh_id)
    }
    if fresh_operations != expected_ids:
        raise PostVerificationReviewDriveError(
            "fresh review operation set is ambiguous"
        )
    return expected_fresh_id, bindings


def synchronize_post_fresh_publication(
    worktree: Path | str,
    *,
    store: OperationStore,
    operation_id: str,
    authorization: PostFreshPublicationSyncAuthorization,
    now: float | None = None,
    fault_observer: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Finish publication only for the two existing fresh review lanes."""

    root = Path(worktree).expanduser().resolve(strict=True)
    observed = time() if now is None else float(now)
    if not math.isfinite(observed) or observed <= 0:
        raise PostVerificationReviewDriveError(
            "post-fresh publication time is invalid"
        )
    fault = fault_observer or (lambda _stage: None)
    runtime_root = store.root / "owners" / operation_id / "runtime" / operation_id
    sync_path = runtime_root / SYNC_RECEIPT_NAME
    sync = _sync_receipt(sync_path)
    if sync is not None and applied_publication(
        runtime_root=runtime_root,
        operation_id=operation_id,
        authorization=authorization,
        sync=sync,
    ):
        return sync
    continuation_path = runtime_root / RECEIPT_NAME
    continuation = _validated_receipt(continuation_path)
    if continuation is None:
        raise PostVerificationReviewDriveError(
            "prepared continuation receipt is unavailable"
        )
    gate, runtime_root = preserved_continuation(
        worktree=root,
        store=store,
        operation_id=operation_id,
        authorization=authorization,
        receipt=continuation,
    )
    fresh_review_id, fresh_bindings = _fresh_lanes(
        worktree=root,
        store=store,
        operation_id=operation_id,
        continuation=continuation,
        gate=gate,
        allow_published=sync is not None,
    )
    gate_path = store.root / "review-data" / operation_id / operation_id / "review-gate.json"
    progress_path = evidence_root(
        ReviewGateController(gate_path.parent, object(), store),
        authorization.continuation.supported_close.signal_free.drift,
    ) / "progress.json"
    progress, progress_raw = regular_json(progress_path, "quarantine progress")
    source_gate = {**gate, "status": "attention-required"}
    target_gate = {**gate, "status": "reviewing"}
    source_progress = {**progress, "status": "quarantined"}
    target_progress = {**progress, "status": "fresh-review-started"}
    source_gate_raw = _encoded(source_gate)
    target_gate_raw = _encoded(target_gate)
    source_progress_raw = _encoded(source_progress)
    target_progress_raw = _encoded(target_progress)
    expected_marker = {
        "schema_version": 1,
        "operation_id": operation_id,
        "definition_sha256": continuation["definition_sha256"],
        "drive_sha256": review_drive_sha256(
            runtime_root,
            target_gate_raw,
            str(continuation["target_head_sha"]),
        ),
        "status": "started",
    }
    marker_sha = sha256(_encoded(expected_marker))
    if sync is None:
        marker_path = runtime_root / MARKER_NAME
        if marker_path.exists():
            raise PostVerificationReviewDriveError(
                "fresh review marker predates synchronization receipt"
            )
        sync = {
            "schema_version": 1,
            "status": "prepared",
            "operation_id": operation_id,
            "fresh_review_operation_id": fresh_review_id,
            "authorization_record_id": authorization.authorization_record_id,
            "authorization_record_sha256": authorization.authorization_record_sha256,
            "continuation_authorization_record_id": (
                authorization.continuation.authorization_record_id
            ),
            "continuation_authorization_record_sha256": (
                authorization.continuation.authorization_record_sha256
            ),
            "continuation_receipt_sha256": sha256(continuation_path.read_bytes()),
            "source_gate_sha256": sha256(source_gate_raw),
            "target_gate_sha256": sha256(target_gate_raw),
            "source_progress_sha256": sha256(source_progress_raw),
            "target_progress_sha256": sha256(target_progress_raw),
            "fresh_lane_bindings": fresh_bindings,
            "fresh_marker_sha256": marker_sha,
            "progress_at": observed,
            "os_signals_sent": 0,
            "cmux_signals_sent": 0,
            "callback_effects_replayed": 0,
            "provider_effects_replayed": 0,
            "reviews_started": 0,
            "binding_sha256": "",
        }
        sync["binding_sha256"] = _binding(sync)
        _write(sync_path, sync)
        fault("prepared")
    elif (
        sync.get("operation_id") != operation_id
        or sync.get("fresh_review_operation_id") != fresh_review_id
        or sync.get("authorization_record_id")
        != authorization.authorization_record_id
        or sync.get("authorization_record_sha256")
        != authorization.authorization_record_sha256
        or sync.get("continuation_authorization_record_id")
        != authorization.continuation.authorization_record_id
        or sync.get("continuation_authorization_record_sha256")
        != authorization.continuation.authorization_record_sha256
        or (
            continuation.get("status") == "prepared"
            and sync.get("continuation_receipt_sha256")
            != sha256(continuation_path.read_bytes())
        )
        or sync.get("fresh_lane_bindings") != fresh_bindings
        or sync.get("fresh_marker_sha256") != marker_sha
        or sync.get("source_gate_sha256") != sha256(source_gate_raw)
        or sync.get("target_gate_sha256") != sha256(target_gate_raw)
        or sync.get("source_progress_sha256") != sha256(source_progress_raw)
        or sync.get("target_progress_sha256") != sha256(target_progress_raw)
    ):
        raise PostVerificationReviewDriveError(
            "post-fresh publication binding drifted"
        )
    gate_controller = ReviewGateController(gate_path.parent, object(), store)
    gate_controller.synchronize_fresh_publication(
        source_sha256=str(sync["source_gate_sha256"]),
        target_sha256=str(sync["target_gate_sha256"]),
    )
    fault("gate-written")
    synchronize_drift_quarantine_fresh(
        gate_controller,
        authorization.continuation.supported_close.signal_free.drift,
        source_sha256=str(sync["source_progress_sha256"]),
        target_sha256=str(sync["target_progress_sha256"]),
    )
    fault("progress-written")
    marker = _publish_fresh_marker(runtime_root, continuation)
    if marker != expected_marker or sha256((runtime_root / MARKER_NAME).read_bytes()) != marker_sha:
        raise PostVerificationReviewDriveError("fresh review marker drifted")
    fault("fresh-marker-written")
    applied_continuation = _apply_transition(
        store=store,
        operation_id=operation_id,
        runtime_root=runtime_root,
        receipt_path=continuation_path,
        receipt=dict(continuation),
        observed=float(sync["progress_at"]),
        fault=fault,
    )
    if applied_continuation.get("status") != "applied":
        raise PostVerificationReviewDriveError(
            "post-fresh continuation did not apply"
        )
    fault("continuation-applied")
    sync = {**sync, "status": "applied"}
    sync["binding_sha256"] = _binding(sync)
    _write(sync_path, sync)
    fault("applied")
    return sync
