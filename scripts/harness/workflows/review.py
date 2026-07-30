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
from ..runtime_sessions import RuntimeSessionRequest
from ..state_machine import TERMINAL
from review_contract import AXES, SEVERITIES, VERIFY_BUDGETS


@dataclass(frozen=True)
class ReviewRequest:
    operation_id: str
    depth: str = "simple"
    cross_model: bool = False
    model: str = ""
    runtime: str = ""
    effort: str = ""
    max_verify_iterations: int = 1

    def __post_init__(self) -> None:
        if self.depth not in AXES:
            raise ValueError("review depth must be simple or deep")
        expected = VERIFY_BUDGETS[self.depth]
        if self.max_verify_iterations < 0 or self.max_verify_iterations > expected:
            raise ValueError("verification count exceeds review depth budget")
        if self.runtime and self.runtime not in {"claude", "codex"}:
            raise ValueError("review runtime must be claude or codex")

    @property
    def axes(self) -> tuple[str, ...]:
        return AXES[self.depth]


@dataclass(frozen=True)
class ReviewContext:
    """Immutable evidence proving the request is ready for the operation store."""

    manifest: str
    head_sha: str
    verification_profile: str
    verification_profile_sha256: str

    def __post_init__(self) -> None:
        path = PurePosixPath(self.manifest)
        if (
            not self.manifest
            or "\\" in self.manifest
            or path.is_absolute()
            or ".." in path.parts
            or "." in path.parts
        ):
            raise ValueError("review context manifest must be owner-relative")
        if not re.fullmatch(r"[0-9a-f]{40,64}", self.head_sha):
            raise ValueError("review context HEAD must be a git object id")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.verification_profile):
            raise ValueError("review verification profile must be a bounded identifier")
        if not re.fullmatch(r"[0-9a-f]{64}", self.verification_profile_sha256):
            raise ValueError("review verification profile digest must be a sha256")


@dataclass(frozen=True)
class ReviewOperationRequest:
    policy: ReviewRequest
    owner_id: str
    route: RuntimeRoute
    context: ReviewContext
    axis_routes: Mapping[str, RuntimeRoute] | None = None
    lane_ids: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        profiles = {"reviewer-readonly", "reviewer-callback"}
        if self.route.profile not in profiles:
            raise ValueError("review operation requires a review-only route")
        if self.axis_routes is not None:
            routes = dict(self.axis_routes)
            if set(routes) != set(self.policy.axes):
                raise ValueError("review axis routes must cover every exact axis")
            if any(
                route.profile not in profiles
                for route in routes.values()
            ):
                raise ValueError(
                    "every review axis requires a review-only route"
                )
            object.__setattr__(
                self, "axis_routes", MappingProxyType(routes)
            )
        if self.lane_ids is not None:
            lanes = dict(self.lane_ids)
            if set(lanes) != set(self.policy.axes) or any(
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", lane)
                for lane in lanes.values()
            ):
                raise ValueError(
                    "review lane ids must cover every axis with bounded ids"
                )
            if len(set(lanes.values())) != len(lanes):
                raise ValueError("deep review axes require independent lanes")
            object.__setattr__(self, "lane_ids", MappingProxyType(lanes))

    def route_for(self, axis: str) -> RuntimeRoute:
        if axis not in self.policy.axes:
            raise ValueError("route requested for an unknown review axis")
        return (
            self.route
            if self.axis_routes is None
            else self.axis_routes[axis]
        )

    def lane_for(self, axis: str, spec: OperationSpec) -> str:
        if axis not in self.policy.axes:
            raise ValueError("lane requested for an unknown review axis")
        if self.lane_ids is not None:
            return self.lane_ids[axis]
        return hashlib.sha256(
            f"{spec.idempotency_key}:lane".encode()
        ).hexdigest()[:32]


class ReviewStore(Protocol):
    """Narrow store seam used by the review workflow."""

    def create(
        self,
        spec: OperationSpec,
        *,
        lane_id: str,
        run_id: str,
    ) -> OperationRecord: ...


def operation_spec(request: ReviewOperationRequest) -> OperationSpec:
    policy = request.policy
    route = request.route_for(policy.axes[0])
    identity = {
        "operation_id": policy.operation_id,
        "owner_id": request.owner_id,
        "depth": policy.depth,
        "cross_model": policy.cross_model,
        "axes": policy.axes,
        "max_verify_iterations": policy.max_verify_iterations,
        "route": {
            "runtime": route.runtime,
            "model": route.model,
            "effort": route.effort,
            "profile": route.profile,
            "routing_sha256": route.routing_sha256,
        },
        "context": {
            "manifest": request.context.manifest,
            "head_sha": request.context.head_sha,
            "verification_profile": request.context.verification_profile,
            "verification_profile_sha256": request.context.verification_profile_sha256,
        },
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return OperationSpec(
        operation_id=policy.operation_id,
        idempotency_key=hashlib.sha256(canonical).hexdigest(),
        kind="simple-review" if policy.depth == "simple" else "deep-review-spec",
        owner_id=request.owner_id,
        route=route,
        context_manifest=request.context.manifest,
        verification_profile=request.context.verification_profile,
    )


def enqueue(request: ReviewOperationRequest, store: ReviewStore) -> OperationRecord:
    """Persist one context-ready review operation through the harness store seam."""

    spec = operation_spec(request)
    lane_id = hashlib.sha256(f"{spec.idempotency_key}:lane".encode()).hexdigest()[:32]
    run_id = hashlib.sha256(f"{spec.idempotency_key}:run".encode()).hexdigest()[:32]
    return store.create(spec, lane_id=lane_id, run_id=run_id)


ReviewSessionRequest = RuntimeSessionRequest

RETRYABLE_CLEANUP_ATTENTION = {
    AttentionReason.ATTENTION_REQUIRED,
    AttentionReason.CLEANUP_INCOMPLETE,
}
REVIEW_CLEANUP_WAIT_ACTIONS = {
    "exit-requested",
    "terminate-orphan",
    "wait-for-exit",
    "wait-for-ownership",
    "wait-for-supervisor",
}


class ReviewRuntimePort(Protocol):
    """Narrow subset of RuntimeSessionManager owned by the review workflow."""

    def start(
        self,
        request: ReviewSessionRequest,
        *,
        on_surface_opened: Callable[[object], None] | None = None,
    ) -> object: ...

    def continue_session(
        self,
        owner_id: str,
        operation_id: str,
        checkpoint: str,
        prompt_pointer: str,
    ) -> object: ...

    def status(self, owner_id: str, operation_id: str) -> object: ...

    def accept_callback(self, envelope: CallbackEnvelope) -> object: ...

    def register_callback_target(
        self,
        owner_id: str,
        parent_operation_id: str,
        child_operation_id: str,
        child_run_id: str,
        callback_pointer: str,
    ) -> object: ...

    def request_exit(self, owner_id: str, operation_id: str) -> object: ...

    def cleanup(self, owner_id: str, operation_id: str) -> object: ...


@dataclass(frozen=True)
class ReviewLaneSession:
    axis: str
    owner_id: str
    operation_id: str
    lane_id: str
    run_id: str
    surface_id: str
    checkpoint: str
    spec: OperationSpec
    verification_iteration: int
    max_verify_iterations: int
    state: str = "running"

    def __post_init__(self) -> None:
        if self.axis not in {
            "holistic",
            "spec",
            "standards-correctness-architecture-security",
        }:
            raise ValueError("invalid review lane axis")
        if self.spec.operation_id != self.operation_id:
            raise ValueError("review lane must retain its parent session spec")
        if self.spec.owner_id != self.owner_id:
            raise ValueError("review lane owner does not match its session spec")
        if not all((self.lane_id, self.run_id)):
            raise ValueError("review runtime did not return complete session identity")
        if self.state not in TERMINAL and not self.surface_id:
            raise ValueError("live review session requires an exact surface")
        if (
            self.verification_iteration < 0
            or self.verification_iteration > self.max_verify_iterations
        ):
            raise ValueError("review verification iteration exceeds its budget")


@dataclass(frozen=True)
class ReviewExecution:
    request: ReviewOperationRequest
    lanes: tuple[ReviewLaneSession, ...]

    def __post_init__(self) -> None:
        if tuple(lane.axis for lane in self.lanes) != self.request.policy.axes:
            raise ValueError("review execution must preserve every ordered axis")


@dataclass(frozen=True)
class ReviewSessionIdentity:
    axis: str
    spec: OperationSpec
    lane_id: str
    run_id: str


class ReviewRoundStore(Protocol):
    def create(
        self,
        spec: OperationSpec,
        *,
        lane_id: str,
        run_id: str,
    ) -> OperationRecord: ...

    def transition(
        self,
        owner_id: str,
        operation_id: str,
        state: str,
        *,
        reason: object | None = None,
    ) -> object: ...

    def read(self, owner_id: str, operation_id: str) -> OperationRecord: ...


@dataclass(frozen=True)
class ReviewRound:
    parent_operation_id: str
    operation_id: str
    owner_id: str
    lane_id: str
    run_id: str
    axis: str
    verification_iteration: int
    spec: OperationSpec

    def __post_init__(self) -> None:
        if self.operation_id == self.parent_operation_id:
            raise ValueError("review round must not reuse its parent operation")
        if (
            self.spec.operation_id != self.operation_id
            or self.spec.owner_id != self.owner_id
            or self.spec.kind != "review-round"
        ):
            raise ValueError("review round does not match its child spec")
        if self.verification_iteration < 0:
            raise ValueError("review round iteration cannot be negative")


def _owner_relative(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
    ):
        raise ValueError(f"{label} must be owner-relative")
    return path.as_posix()


def _derived_id(parent: str, role: str) -> str:
    aliases = {
        "spec": "spec",
        "standards-correctness-architecture-security": "standards",
        "holistic": "holistic",
    }
    short = aliases.get(role, "round")
    suffix = f"-{short}-{hashlib.sha256(role.encode()).hexdigest()[:8]}"
    return f"{parent[: 128 - len(suffix)]}{suffix}"


def _round_spec(lane: ReviewLaneSession) -> OperationSpec:
    role = f"round-{lane.verification_iteration}"
    operation_id = _derived_id(lane.operation_id, role)
    identity = (
        f"{lane.spec.idempotency_key}:{lane.axis}:{role}:{operation_id}".encode()
    )
    return replace(
        lane.spec,
        operation_id=operation_id,
        idempotency_key=hashlib.sha256(identity).hexdigest(),
        kind="review-round",
        keep_open=False,
    )


def _session_spec(
    request: ReviewOperationRequest, axis: str
) -> OperationSpec:
    base = operation_spec(request)
    if request.policy.depth == "simple":
        return replace(base, route=request.route_for(axis))
    operation_id = _derived_id(base.operation_id, axis)
    route = request.route_for(axis)
    identity = (
        f"{base.idempotency_key}:session:{axis}:{operation_id}:"
        f"{route.runtime}:{route.model}:{route.effort}:"
        f"{route.routing_sha256}".encode()
    )
    return replace(
        base,
        operation_id=operation_id,
        idempotency_key=hashlib.sha256(identity).hexdigest(),
        kind=(
            "deep-review-spec"
            if axis == "spec"
            else "deep-review-correctness"
        ),
        route=route,
    )


def review_session_specs(
    request: ReviewOperationRequest,
) -> tuple[ReviewSessionIdentity, ...]:
    """Return deterministic parent identities without launching or replaying."""

    identities: list[ReviewSessionIdentity] = []
    for axis in request.policy.axes:
        spec = _session_spec(request, axis)
        identities.append(
            ReviewSessionIdentity(
                axis=axis,
                spec=spec,
                lane_id=request.lane_for(axis, spec),
                run_id=hashlib.sha256(
                    f"{spec.idempotency_key}:run".encode()
                ).hexdigest()[:32],
            )
        )
    return tuple(identities)


def _runtime_record(value: object) -> OperationRecord:
    record = (
        value
        if isinstance(value, OperationRecord)
        else getattr(value, "record", None)
    )
    if not isinstance(record, OperationRecord):
        raise ValueError("review runtime returned no typed operation record")
    return record


def runtime_status_is_live(value: object) -> bool:
    """Accept only a fully observed, resumable reviewer status result."""

    return (
        str(getattr(value, "action", "") or "") == "observed"
        and str(getattr(value, "process_status", "") or "") == "alive"
        and str(getattr(value, "surface_status", "") or "") == "alive"
        and bool(str(getattr(value, "checkpoint", "") or ""))
    )


def _runtime_lane(
    *,
    axis: str,
    spec: OperationSpec,
    value: object,
    max_verify_iterations: int,
) -> ReviewLaneSession:
    record = _runtime_record(value)
    if record.spec != spec:
        raise ValueError("review runtime returned a different operation")
    action = str(getattr(value, "action", "") or "")
    if action in {"observed", "attention-required", "terminal"} and not (
        runtime_status_is_live(value)
    ):
        raise ValueError("stored review runtime is not live and resumable")
    checkpoint = str(getattr(value, "checkpoint", "") or "")
    return ReviewLaneSession(
        axis=axis,
        owner_id=spec.owner_id,
        operation_id=spec.operation_id,
        lane_id=record.lane_id,
        run_id=record.run_id,
        surface_id=record.resources.surface_id,
        checkpoint=checkpoint,
        spec=spec,
        verification_iteration=0,
        max_verify_iterations=max_verify_iterations,
        state=record.state,
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
    lanes: list[ReviewLaneSession] = []
    started_lanes: list[ReviewLaneSession] = []
    initial_rounds: list[ReviewRound] = []
    try:
        for identity in review_session_specs(request):
            axis = identity.axis
            spec = identity.spec
            lane_id = identity.lane_id
            run_id = identity.run_id
            axis_name = (
                "standards"
                if axis
                == "standards-correctness-architecture-security"
                else axis
            )
            axis_prompt = (
                prompt_pointer
                if prompt_pointers is None
                else prompt_pointers[axis]
            )
            session_request = ReviewSessionRequest(
                spec=spec,
                lane_id=lane_id,
                run_id=run_id,
                origin_surface=origin_surface,
                cwd=cwd,
                product_root=product_root,
                prompt_pointer=axis_prompt,
                placement="workspace",
                callback_pointer=(
                    f"{callback_root}/{axis_name}/.review-callback.json"
                ),
            )
            try:
                existing = round_store.read(
                    spec.owner_id, spec.operation_id
                )
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

    spec = _round_spec(lane)
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


@dataclass(frozen=True)
class ReviewFinding:
    finding_id: str
    axis: str
    severity: str
    summary: str
    evidence: str
    file: str = "unknown"
    line: int | None = None
    recommendation: str = "Resolve this finding before approval."

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError("review severity must be critical, important, or minor")
        if not all(
            (
                self.finding_id,
                self.axis,
                self.summary,
                self.evidence,
                self.file,
                self.recommendation,
            )
        ):
            raise ValueError("review finding fields are required")
        if self.line is not None and (
            not isinstance(self.line, int)
            or isinstance(self.line, bool)
            or self.line < 1
        ):
            raise ValueError("review finding line must be positive or null")


@dataclass(frozen=True)
class ReviewResult:
    axis: str
    verdict: str
    findings: tuple[ReviewFinding, ...] = ()
    verification_iteration: int = 0

    def __post_init__(self) -> None:
        if self.verdict not in {"approve", "changes-requested", "blocked"}:
            raise ValueError("invalid review verdict")
        if any(row.axis != self.axis for row in self.findings):
            raise ValueError("review findings cannot cross axes")
        if (
            self.verdict == "approve"
            and any(
                finding.severity in {"critical", "important"}
                for finding in self.findings
            )
        ):
            raise ValueError("review axis cannot approve with material findings")
        if (
            not isinstance(self.verification_iteration, int)
            or isinstance(self.verification_iteration, bool)
            or self.verification_iteration < 0
        ):
            raise ValueError("verification iteration must be a non-negative integer")


def aggregate(request: ReviewRequest, results: Mapping[str, ReviewResult]) -> dict[str, object]:
    if set(results) != set(request.axes):
        raise ValueError("review aggregation requires every independent axis")
    ordered = tuple(results[axis] for axis in request.axes)
    if any(
        row.verification_iteration > request.max_verify_iterations for row in ordered
    ):
        raise ValueError("review result exceeds the verification iteration budget")
    finding_ids = [
        finding.finding_id for row in ordered for finding in row.findings
    ]
    if len(finding_ids) != len(set(finding_ids)):
        raise ValueError("review finding ids must be unique across axes")
    verdict = (
        "blocked" if any(row.verdict == "blocked" for row in ordered)
        else "changes-requested" if any(row.verdict == "changes-requested" for row in ordered)
        else "approve"
    )
    return {
        "verdict": verdict,
        "axes": [
            {
                "axis": row.axis,
                "verdict": row.verdict,
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
                    for finding in row.findings
                ],
                "verification_iteration": row.verification_iteration,
            }
            for row in ordered
        ],
    }


def aggregate_review_evidence(
    execution: ReviewExecution,
    results: Mapping[str, ReviewResult],
    *,
    verification_gaps: tuple[str, ...] = (),
    notes_for_executor: tuple[str, ...] = (),
    residual_risks: tuple[str, ...] = (),
) -> dict[str, object]:
    """Build the canonical review-v1 evidence after every lane has completed."""

    combined = aggregate(execution.request.policy, results)
    run_id = hashlib.sha256(
        (
            execution.request.policy.operation_id
            + ":"
            + ":".join(lane.run_id for lane in execution.lanes)
        ).encode()
    ).hexdigest()[:32]
    context = execution.request.context
    return {
        "schema_version": 1,
        "operation_id": execution.request.policy.operation_id,
        "run_id": run_id,
        "mode": execution.request.policy.depth,
        "head_sha": context.head_sha,
        "verification_profile": {
            "name": context.verification_profile,
            "sha256": context.verification_profile_sha256,
        },
        "verdict": combined["verdict"],
        "axes": combined["axes"],
        "verification_gaps": list(verification_gaps),
        "notes_for_executor": list(notes_for_executor),
        "residual_risks": list(residual_risks),
    }


def review_evidence_envelope(
    execution: ReviewExecution,
    results: Mapping[str, ReviewResult],
    **notes: tuple[str, ...],
) -> CallbackEnvelope:
    """Wrap final aggregate evidence for the existing archive transport."""

    payload = aggregate_review_evidence(execution, results, **notes)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return CallbackEnvelope(
        callback_id=f"review-{digest[:24]}",
        operation_id=str(payload["operation_id"]),
        run_id=str(payload["run_id"]),
        kind="review",
        payload=payload,
        payload_sha256=digest,
    )


def verify_lane(original_surface: str, verification_surface: str) -> None:
    if original_surface != verification_surface:
        raise ValueError("same-session verification cannot open a second surface")


@dataclass(frozen=True)
class ReviewLaneIdentity:
    axis: str
    lane_id: str
    surface_id: str

    def __post_init__(self) -> None:
        if self.axis not in {
            "holistic",
            "spec",
            "standards-correctness-architecture-security",
        }:
            raise ValueError("invalid review axis")
        if not self.lane_id or not self.surface_id:
            raise ValueError("review lane and surface identity are required")


def verify_session(
    original: ReviewLaneIdentity, verification: ReviewLaneIdentity
) -> None:
    """Fail closed unless verification reuses the exact axis/lane/surface."""

    if original != verification:
        raise ValueError(
            "same-session verification must reuse the exact axis, lane, and surface"
        )


def resolution_required(result: ReviewResult) -> bool:
    return result.verdict == "changes-requested" and any(
        finding.severity in {"critical", "important"} for finding in result.findings
    )
