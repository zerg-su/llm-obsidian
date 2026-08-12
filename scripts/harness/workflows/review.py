"""Unified simple/deep and same/cross-model review request contract."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Callable, Mapping, Protocol

from ..contracts import (
    AttentionReason,
    CallbackEnvelope,
    OperationRecord,
    OperationSpec,
    RuntimeRoute,
)
from ..state_machine import TERMINAL
from ..runtime_session_contracts import RuntimeSessionError
from ..dashboard_facade import launch_bound_facade_dashboard
from review_contract import MATERIAL_SEVERITIES, SEVERITIES, VERIFY_BUDGETS


from .review_contracts import (
    MAX_REVIEW_SUMMARY_CHARS,
    ReviewContext,
    ReviewExecution,
    ReviewLaneSession,
    ReviewOperationRequest,
    ReviewRequest,
    ReviewRound,
    ReviewRoundStore,
    ReviewRuntimePort,
    ReviewSessionRequest,
    ReviewSessionIdentity,
    ReviewStore,
    RETRYABLE_CLEANUP_ATTENTION,
    REVIEW_CLEANUP_WAIT_ACTIONS,
    _bounded_finding_summary,
    _derived_id,
    _owner_relative,
    _runtime_lane,
    _runtime_record,
    _session_spec,
    enqueue,
    operation_spec,
    review_session_specs,
    review_round_spec,
    runtime_status_is_live,
)
from .review_results import (
    ReviewFinding,
    ReviewLaneIdentity,
    ReviewResult,
    aggregate,
    aggregate_review_evidence,
    namespace_review_result,
    resolution_required,
    review_evidence_envelope,
    verify_lane,
    verify_session,
)


def start_review(
    request: ReviewOperationRequest,
    runtime: ReviewRuntimePort,
    *,
    origin_surface: str,
    cwd: Path,
    product_root: Path,
    prompt_pointer: str,
    callback_root: str,
    round_store: ReviewRoundStore,
    callback_wake: str = "",
    prompt_pointers: Mapping[str, str] | None = None,
    prepare_lane: (
        Callable[
            [str, ReviewSessionRequest, object, ReviewRound], None
        ]
        | None
    ) = None,
) -> ReviewExecution:
    """Start one simple lane or two independent deep lanes through the runtime."""

    cwd = cwd.expanduser().resolve()
    product_root = product_root.expanduser().resolve()
    if prompt_pointers is not None and set(prompt_pointers) != set(
        request.policy.axes
    ):
        raise ValueError("review prompt pointers must cover every exact axis")
    if any(
        request.route_for(axis).profile != "reviewer-callback"
        for axis in request.policy.axes
    ):
        raise ValueError(
            "provider review sessions require the reviewer-callback profile"
        )
    _owner_relative(prompt_pointer, "review prompt pointer")
    callback_root = _owner_relative(
        callback_root, "review callback root"
    ).rstrip("/")
    launch_bound_facade_dashboard(
        worktree=product_root,
        facade=(
            "plan-review"
            if request.context.purpose == "intent"
            else "review"
        ),
        root_operation_id=request.root_operation_id,
    )
    lanes: list[ReviewLaneSession] = []
    started_lanes: list[ReviewLaneSession] = []
    initial_rounds: list[ReviewRound] = []
    prepared_sessions: list[
        tuple[
            ReviewSessionIdentity,
            ReviewSessionRequest,
            OperationRecord | None,
            bool,
        ]
    ] = []
    for identity in review_session_specs(request):
        axis = identity.axis
        spec = identity.spec
        axis_prompt = (
            prompt_pointer
            if prompt_pointers is None
            else prompt_pointers[axis]
        )
        session_request = ReviewSessionRequest(
            spec=spec,
            lane_id=identity.lane_id,
            run_id=identity.run_id,
            origin_surface=origin_surface,
            cwd=cwd,
            product_root=product_root,
            prompt_pointer=axis_prompt,
            placement="workspace",
            callback_pointer=(
                f"{callback_root}/{axis}/.review-callback.json"
            ),
            callback_wake=callback_wake,
        )
        try:
            existing = round_store.read(spec.owner_id, spec.operation_id)
        except Exception:
            existing = None
        restartable_created = (
            existing is not None
            and existing.state == "created"
            and not existing.pending_effect
            and not any(
                (
                    existing.resources.surface_id,
                    existing.resources.process_group,
                    existing.resources.supervisor_pid,
                    existing.resources.process_identity,
                    existing.resources.supervisor_identity,
                )
            )
        )
        prepared_sessions.append(
            (identity, session_request, existing, restartable_created)
        )
    if request.policy.depth == "full":
        fresh = tuple(
            session_request
            for _identity, session_request, existing, restartable_created
            in prepared_sessions
            if existing is None or restartable_created
        )
        reports = runtime.preflight_routes(
            tuple(
                (
                    session_request.spec.route,
                    (cwd / session_request.callback_pointer).parent,
                    origin_surface,
                )
                for session_request in fresh
            )
        )
        if len(reports) != len(fresh) or any(
            not report.compatible for report in reports
        ):
            raise RuntimeSessionError(
                "Full review is unavailable before provider start; "
                "use single-model Deep"
            )
    try:
        for (
            identity,
            session_request,
            existing,
            restartable_created,
        ) in prepared_sessions:
            axis = identity.axis
            spec = identity.spec
            lane_id = identity.lane_id
            run_id = identity.run_id
            if existing is not None and not restartable_created:
                if (
                    existing.spec != spec
                    or existing.lane_id != lane_id
                    or existing.run_id != run_id
                ):
                    raise ValueError(
                        "stored review session identity does not match request"
                    )
                observed: object = (
                    existing
                    if existing.state in TERMINAL
                    else runtime.status(spec.owner_id, spec.operation_id)
                )
                lanes.append(
                    _runtime_lane(
                        axis=axis,
                        spec=spec,
                        value=observed,
                        max_verify_iterations=(
                            request.policy.max_verify_iterations
                        ),
                    )
                )
                continue

            def on_surface_opened(
                result: object,
                *,
                axis: str = axis,
                session_request: ReviewSessionRequest = session_request,
            ) -> None:
                record = _runtime_record(result)
                initial_lane = _runtime_lane(
                    axis=axis,
                    spec=session_request.spec,
                    value=result,
                    max_verify_iterations=(
                        request.policy.max_verify_iterations
                    ),
                )
                round_ = prepare_review_round(round_store, initial_lane)
                initial_rounds.append(round_)
                if prepare_lane is not None:
                    prepare_lane(
                        axis, session_request, result, round_
                    )
                runtime.register_callback_target(
                    record.spec.owner_id,
                    record.spec.operation_id,
                    round_.operation_id,
                    round_.run_id,
                    session_request.callback_pointer,
                )
            result = runtime.start(
                session_request,
                on_surface_opened=on_surface_opened,
            )
            lane = _runtime_lane(
                axis=axis,
                spec=spec,
                value=result,
                max_verify_iterations=request.policy.max_verify_iterations,
            )
            lanes.append(lane)
            started_lanes.append(lane)
    except Exception:
        for round_ in initial_rounds:
            try:
                current = round_store.read(
                    round_.owner_id, round_.operation_id
                )
                if current.state not in {
                    "complete",
                    "failed",
                    "cancelled",
                }:
                    round_store.transition(
                        round_.owner_id, round_.operation_id, "failed"
                    )
            except Exception:
                pass
        for lane in reversed(started_lanes):
            try:
                runtime.request_exit(lane.owner_id, lane.operation_id)
                runtime.cleanup(lane.owner_id, lane.operation_id)
            except Exception:
                pass
        raise
    return ReviewExecution(request, tuple(lanes))


def verify_review_lane(
    runtime: ReviewRuntimePort,
    lane: ReviewLaneSession,
    *,
    prompt_pointer: str,
    callback_pointer: str,
    round_store: ReviewRoundStore,
    prepare_round: (
        Callable[[ReviewLaneSession, ReviewRound], None] | None
    ) = None,
) -> ReviewLaneSession:
    """Continue a reviewer only after proving the exact parent session is alive."""

    if lane.verification_iteration >= lane.max_verify_iterations:
        current = round_store.read(lane.owner_id, lane.operation_id)
        if current.state not in TERMINAL and current.state != "attention-required":
            round_store.transition(
                lane.owner_id,
                lane.operation_id,
                "attention-required",
                reason=AttentionReason.RETRY_EXHAUSTED,
            )
        return replace(lane, state="attention-required")
    if lane.state in TERMINAL:
        raise ValueError("terminal review session cannot be verified")
    _owner_relative(prompt_pointer, "review verification prompt pointer")
    _owner_relative(callback_pointer, "review verification callback pointer")
    before = _runtime_record(runtime.status(lane.owner_id, lane.operation_id))
    if before.resources.surface_id != lane.surface_id:
        raise ValueError("same-session verification lost its exact surface")
    next_lane = replace(
        lane, verification_iteration=lane.verification_iteration + 1
    )
    round_ = prepare_review_round(round_store, next_lane)
    if prepare_round is not None:
        prepare_round(next_lane, round_)
    runtime.register_callback_target(
        lane.owner_id,
        lane.operation_id,
        round_.operation_id,
        round_.run_id,
        callback_pointer,
    )
    result = runtime.continue_session(
        lane.owner_id,
        lane.operation_id,
        lane.checkpoint,
        prompt_pointer,
    )
    continued = _runtime_lane(
        axis=lane.axis,
        spec=lane.spec,
        value=result,
        max_verify_iterations=lane.max_verify_iterations,
    )
    verify_session(
        ReviewLaneIdentity(lane.axis, lane.lane_id, lane.surface_id),
        ReviewLaneIdentity(
            continued.axis, continued.lane_id, continued.surface_id
        ),
    )
    return replace(
        continued,
        verification_iteration=next_lane.verification_iteration,
    )


def prepare_review_round(
    store: ReviewRoundStore, lane: ReviewLaneSession
) -> ReviewRound:
    """Create one durable one-shot callback receipt beside its parent session."""

    spec = review_round_spec(lane)
    run_id = hashlib.sha256(
        f"{spec.idempotency_key}:run".encode()
    ).hexdigest()[:32]
    record = store.create(spec, lane_id=lane.lane_id, run_id=run_id)
    if record.state == "created":
        for state in ("preflight", "starting", "running", "awaiting-callback"):
            store.transition(spec.owner_id, spec.operation_id, state)
        record = store.read(spec.owner_id, spec.operation_id)
    return ReviewRound(
        parent_operation_id=lane.operation_id,
        operation_id=spec.operation_id,
        owner_id=spec.owner_id,
        lane_id=record.lane_id,
        run_id=record.run_id,
        axis=lane.axis,
        verification_iteration=lane.verification_iteration,
        spec=spec,
    )


def review_round_envelope(
    round_: ReviewRound, result: ReviewResult
) -> CallbackEnvelope:
    """Encode one axis result for the round's one-shot callback broker receipt."""

    if (
        result.axis != round_.axis
        or result.verification_iteration != round_.verification_iteration
    ):
        raise ValueError("review result does not match its callback round")
    payload = review_round_payload(round_.parent_operation_id, result)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return CallbackEnvelope(
        callback_id=f"review-{digest[:24]}",
        operation_id=round_.operation_id,
        run_id=round_.run_id,
        kind="review",
        payload=payload,
        payload_sha256=digest,
    )


def review_round_payload(
    parent_session_operation_id: str, result: ReviewResult
) -> dict[str, object]:
    """Return the validated internal payload shared by submitter and workflow."""

    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
        parent_session_operation_id,
    ):
        raise ValueError("review round parent must be a bounded identifier")
    return {
        "schema_version": 1,
        "parent_session_operation_id": parent_session_operation_id,
        "axis": result.axis,
        "verification_iteration": result.verification_iteration,
        "verdict": result.verdict,
        "findings": [
            {
                "finding_id": finding.finding_id,
                "severity": finding.severity,
                "file": finding.file,
                "line": finding.line,
                "summary": finding.summary,
                "evidence": finding.evidence,
                "recommendation": finding.recommendation,
            }
            for finding in result.findings
        ],
    }


def accept_review_round(
    runtime: ReviewRuntimePort,
    store: ReviewRoundStore,
    lane: ReviewLaneSession,
    round_: ReviewRound,
    envelope: CallbackEnvelope,
) -> OperationRecord | None:
    """Accept one round and close only a parent session with terminal approval."""

    payload = envelope.payload
    if (
        round_.parent_operation_id != lane.operation_id
        or round_.owner_id != lane.owner_id
        or round_.lane_id != lane.lane_id
        or round_.axis != lane.axis
        or round_.verification_iteration != lane.verification_iteration
        or envelope.operation_id != round_.operation_id
        or envelope.run_id != round_.run_id
        or payload.get("parent_session_operation_id") != lane.operation_id
        or payload.get("axis") != lane.axis
        or payload.get("verification_iteration")
        != lane.verification_iteration
    ):
        raise ValueError("review callback does not match its exact parent round")
    verdict = payload.get("verdict")
    if verdict not in {"approve", "changes-requested", "blocked"}:
        raise ValueError("review callback verdict is invalid")
    runtime.accept_callback(envelope)
    child = store.read(round_.owner_id, round_.operation_id)
    if child.state not in {"complete", "failed", "cancelled"}:
        if child.state != "finalizing":
            store.transition(
                child.spec.owner_id, child.spec.operation_id, "finalizing"
            )
        store.transition(
            child.spec.owner_id, child.spec.operation_id, "exiting"
        )
        store.transition(
            child.spec.owner_id, child.spec.operation_id, "complete"
        )
    if verdict == "approve":
        return finish_review_lane(runtime, lane)
    elif verdict == "blocked":
        parent = store.read(lane.owner_id, lane.operation_id)
        if parent.state != "attention-required":
            store.transition(
                lane.owner_id,
                lane.operation_id,
                "attention-required",
                reason=AttentionReason.ATTENTION_REQUIRED,
            )
    return None


def finish_review_lane(
    runtime: ReviewRuntimePort,
    lane: ReviewLaneSession,
    *,
    timeout_seconds: float = 30.0,
    poll_seconds: float = 0.1,
) -> OperationRecord:
    """Boundedly prove provider exit and exact reviewer-resource cleanup."""

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    poll_delay = max(0.0, poll_seconds)
    attention_retries = 0
    result = runtime.request_exit(lane.owner_id, lane.operation_id)
    while True:
        record = _runtime_record(result)
        if record.state == "complete":
            return record
        if record.state in {"failed", "cancelled"}:
            return record
        if record.state == "attention-required":
            if (
                record.attention_reason not in RETRYABLE_CLEANUP_ATTENTION
                or attention_retries >= 2
                or time.monotonic() >= deadline
            ):
                return record
            attention_retries += 1
            time.sleep(
                min(poll_delay, max(0.0, deadline - time.monotonic()))
            )
            result = runtime.request_exit(lane.owner_id, lane.operation_id)
            continue
        if record.state != "exiting":
            return record

        result = runtime.cleanup(lane.owner_id, lane.operation_id)
        record = _runtime_record(result)
        if record.state == "complete":
            return record
        if record.state in {"failed", "cancelled", "attention-required"}:
            continue
        if (
            str(getattr(result, "action", ""))
            not in REVIEW_CLEANUP_WAIT_ACTIONS
            or time.monotonic() >= deadline
        ):
            return record
        time.sleep(min(poll_delay, max(0.0, deadline - time.monotonic())))
