"""Immutable review requests, session identities, and runtime ports."""

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
from ..review_program import PURPOSES
from review_contract import (
    MATERIAL_SEVERITIES,
    MODES,
    SEVERITIES,
    VERIFY_BUDGETS,
    compile_review_axes,
    review_axis_responsibility,
    review_parent_kind,
)


MAX_REVIEW_SUMMARY_CHARS = 300


def _bounded_finding_summary(value: str) -> str:
    """Keep round detail while emitting canonical review-v1 evidence."""

    if len(value) <= MAX_REVIEW_SUMMARY_CHARS:
        return value
    return value[: MAX_REVIEW_SUMMARY_CHARS - 1].rstrip() + "…"


@dataclass(frozen=True)
class ReviewRequest:
    operation_id: str
    depth: str = "simple"
    cross_model: bool = False
    model: str = ""
    runtime: str = ""
    effort: str = ""
    max_verify_iterations: int = 1
    purpose: str = "implementation"
    selected_provider: str = ""

    def __post_init__(self) -> None:
        if self.depth not in MODES:
            raise ValueError("review depth must be simple, deep, or full")
        if self.purpose not in PURPOSES:
            raise ValueError("review purpose is invalid")
        expected = VERIFY_BUDGETS[self.depth]
        if self.max_verify_iterations < 0 or self.max_verify_iterations > expected:
            raise ValueError("verification count exceeds review depth budget")
        if self.purpose == "release" and self.max_verify_iterations != 0:
            raise ValueError("release review cannot open a verification fix loop")
        if self.purpose == "intent" and self.max_verify_iterations > 1:
            raise ValueError("intent review verification budget exceeds one")
        if self.runtime and self.runtime not in {"claude", "codex"}:
            raise ValueError("review runtime must be claude or codex")
        if (
            self.depth == "deep"
            and (self.runtime or self.model)
            and not self.selected_provider
        ):
            raise ValueError("single-model deep requires its selected provider")
        compile_review_axes(self.depth, selected_provider=self.selected_provider)

    @property
    def axes(self) -> tuple[str, ...]:
        return compile_review_axes(
            self.depth,
            selected_provider=self.selected_provider,
        )


@dataclass(frozen=True)
class ReviewContext:
    """Immutable evidence proving the request is ready for the operation store."""

    manifest: str
    head_sha: str
    verification_profile: str
    verification_profile_sha256: str
    implementer_summary_sha256: str = ""
    purpose: str = "implementation"
    boundary_input_sha256: str = ""

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
        if self.implementer_summary_sha256 and not re.fullmatch(
            r"[0-9a-f]{64}", self.implementer_summary_sha256
        ):
            raise ValueError("review implementer summary digest must be a sha256")
        if self.purpose not in PURPOSES:
            raise ValueError("review context purpose is invalid")
        if self.boundary_input_sha256 and not re.fullmatch(
            r"[0-9a-f]{64}", self.boundary_input_sha256
        ):
            raise ValueError("review boundary input digest must be a sha256")
        if self.purpose != "implementation" and not self.boundary_input_sha256:
            raise ValueError("non-legacy review purpose requires a boundary digest")


@dataclass(frozen=True)
class ReviewOperationRequest:
    policy: ReviewRequest
    owner_id: str
    route: RuntimeRoute
    context: ReviewContext
    axis_routes: Mapping[str, RuntimeRoute] | None = None
    lane_ids: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if self.policy.purpose != self.context.purpose:
            raise ValueError("review policy and context purposes must match")
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
            **(
                {
                    "implementer_summary_sha256": (
                        request.context.implementer_summary_sha256
                    )
                }
                if request.context.implementer_summary_sha256
                else {}
            ),
            **(
                {
                    "purpose": request.context.purpose,
                    "boundary_input_sha256": request.context.boundary_input_sha256,
                }
                if request.context.boundary_input_sha256
                else {}
            ),
        },
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return OperationSpec(
        operation_id=policy.operation_id,
        idempotency_key=hashlib.sha256(canonical).hexdigest(),
        kind=review_parent_kind(policy.axes[0]),
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

    def rearm_callback_timeout(
        self,
        owner_id: str,
        operation_id: str,
    ) -> object: ...

    def status(self, owner_id: str, operation_id: str) -> object: ...

    def hydrate_durable_checkpoint(
        self,
        owner_id: str,
        operation_id: str,
        lane_id: str,
    ) -> object: ...

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
    checkpoint_sha256: str = ""

    def __post_init__(self) -> None:
        try:
            review_axis_responsibility(self.axis)
        except ValueError as exc:
            raise ValueError("invalid review lane axis") from exc
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
    try:
        short = review_axis_responsibility(role)
    except ValueError:
        short = "round"
    suffix = f"-{short}-{hashlib.sha256(role.encode()).hexdigest()[:8]}"
    return f"{parent[: 128 - len(suffix)]}{suffix}"


def review_round_spec(lane: ReviewLaneSession) -> OperationSpec:
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
        kind=review_parent_kind(axis),
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
        str(getattr(value, "action", "") or "")
        in {"observed", "already-started"}
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
    if action in {
        "observed",
        "already-started",
        "attention-required",
        "terminal",
    } and not runtime_status_is_live(value):
        raise ValueError("stored review runtime is not live and resumable")
    checkpoint = str(getattr(value, "checkpoint", "") or "")
    checkpoint_sha256 = str(
        getattr(value, "checkpoint_sha256", "") or ""
    )
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
        checkpoint_sha256=checkpoint_sha256,
    )
