"""Immutable dashboard values and pure read-only classification policy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from .contracts import OperationRecord
from .dashboard_receipts import verification_identity
from .state_machine import TERMINAL
from .status_segment import CONTROLLER_KINDS
from .verification_attempt import MAX_SAME_HEAD_ATTEMPT_INDEX


MAX_ISSUES = 5
MAX_PROGRAMS = 8
MAX_LANES = 8
MAX_CHILDREN = 8
MAX_DEPTH = 4

UNKNOWN = "unknown"
HEALTHY = "healthy"
ACTIVE = "in-progress"
WAITING = "waiting"
ATTENTION = "attention-required"
COORDINATOR = "request-coordinator-classification"
CLASSIFICATION_ORDER = (HEALTHY, ACTIVE, WAITING, ATTENTION, COORDINATOR)
SURFACE_BOUND_STATES = frozenset({"running", "awaiting-callback"})

REVIEW_OBSERVATIONS = {
    "approved": "complete",
    "skipped": "complete",
    "reviewing": "running",
    "verifying": "running",
    "awaiting-resolution": "running",
    "changes-requested": "running",
    "recovery-verification-required": "running",
    "fresh-boundary-authorized": "running",
    "attention-required": "attention",
    "blocked": "attention",
    "stopped": "attention",
}


def escalate(current: str, candidate: str) -> str:
    """Return the more severe of two classifications; unknown wins outright."""

    if current not in CLASSIFICATION_ORDER or candidate not in CLASSIFICATION_ORDER:
        return COORDINATOR
    return max(current, candidate, key=CLASSIFICATION_ORDER.index)


@dataclass(frozen=True)
class RouteView:
    """The frozen execution metadata one step or record actually consumes."""

    runtime: str = UNKNOWN
    model: str = UNKNOWN
    effort: str = UNKNOWN
    preset: str = UNKNOWN


UNKNOWN_ROUTE = RouteView()


@dataclass(frozen=True)
class ChildView:
    operation_id: str
    kind: str
    state: str
    status: str
    route: RouteView
    children: tuple["ChildView", ...] = ()


@dataclass(frozen=True)
class StepView:
    step_id: str
    primitive: str
    session_mode: str
    status: str
    visits: int
    route: RouteView = UNKNOWN_ROUTE
    children: tuple[ChildView, ...] = ()
    evidence_issue: str = ""


@dataclass(frozen=True)
class LaneView:
    lane_id: str
    scope: str
    members: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class IssueView:
    code: str
    operation_id: str
    detail: str
    classification: str


@dataclass(frozen=True)
class ProgramView:
    operation_id: str
    kind: str
    state: str
    revision: int
    pipeline: str
    definition_sha256: str
    controls: tuple[str, ...]
    steps: tuple[StepView, ...]
    lanes: tuple[LaneView, ...]
    next_action: str
    loop_passes: int
    loop_limit: int
    surface: str
    classification: str
    executor: RouteView = UNKNOWN_ROUTE
    executor_status: str = UNKNOWN
    children: tuple[ChildView, ...] = ()
    dropped_children: int = 0
    dropped_lanes: int = 0


@dataclass(frozen=True)
class DashboardProjection:
    owner_id: str
    classification: str
    surface_probe: str
    programs: tuple[ProgramView, ...] = ()
    issues: tuple[IssueView, ...] = ()
    truncated: Mapping[str, int] = field(default_factory=dict)
    schema_version: int = 1


def record_activity(record: OperationRecord) -> str:
    """Classify one durable record without inventing unowned live work."""

    if record.state == "attention-required":
        return "attention"
    if record.state == "complete":
        return "complete"
    if record.state in TERMINAL:
        return "stopped"
    if (
        record.state in SURFACE_BOUND_STATES
        and record.spec.kind in CONTROLLER_KINDS
        and not record.resources.surface_id
    ):
        return "attention"
    if record.state in {"created", "preflight"}:
        return "pending"
    return "running"


def aggregate(statuses: tuple[str, ...]) -> str:
    """Fold child activity into the status consumed by pipeline policy."""

    if not statuses:
        return "pending"
    if any(status == "attention" for status in statuses):
        return "attention"
    if any(status == "running" for status in statuses):
        return "running"
    if all(status in {"complete", "stopped"} for status in statuses):
        return "complete"
    return "pending"


def route_view(record: OperationRecord) -> RouteView:
    """Report the frozen route of one record without filling metadata gaps."""

    route = record.spec.route
    preset = (
        record.spec.verification_profile
        if record.spec.kind == "pipeline-verify"
        else route.profile
    )
    return RouteView(
        route.runtime or UNKNOWN,
        route.model or UNKNOWN,
        route.effort or UNKNOWN,
        preset or UNKNOWN,
    )


def current_verification_ids(
    record: OperationRecord,
    step: Any,
    gate: Mapping[str, Any] | None,
) -> frozenset[str]:
    """Derive only verification children bound to the gate's exact attempt."""

    context = gate.get("context") if isinstance(gate, Mapping) else None
    if not isinstance(context, Mapping):
        return frozenset()
    head_sha = str(context.get("head_sha") or "")
    profile = str(context.get("verification_profile") or "")
    profile_sha256 = str(context.get("verification_profile_sha256") or "")
    if (
        profile != record.spec.verification_profile
        or len(head_sha) not in {40, 64}
        or len(profile_sha256) != 64
        or any(character not in "0123456789abcdef" for character in head_sha)
        or any(
            character not in "0123456789abcdef"
            for character in profile_sha256
        )
    ):
        return frozenset()
    input_sha256 = hashlib.sha256(
        json.dumps(
            {
                "definition_sha256": record.spec.contract_sha256,
                "head_sha": head_sha,
                "profile_sha256": profile_sha256,
                "schema_version": step.schema_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return frozenset(
        verification_identity(
            record.spec,
            record.spec.contract_sha256,
            input_sha256,
            attempt_index,
        )[0]
        for attempt_index in range(MAX_SAME_HEAD_ATTEMPT_INDEX + 1)
    )


def program_classification(
    record: OperationRecord,
    *,
    surface: str,
    next_action: str,
    pipeline_resolved: bool,
    executor_status: str = "",
) -> str:
    if record.state == "attention-required" or record.pending_effect:
        return ATTENTION
    if surface in {"ambiguous", "unknown"} or not pipeline_resolved:
        return COORDINATOR
    if next_action == "unknown":
        return COORDINATOR
    if next_action == "attention":
        return ATTENTION
    if (
        surface in {"missing", "unbound"}
        and record.state in SURFACE_BOUND_STATES
        and executor_status != "awaiting-transition"
    ):
        return ATTENTION
    if record.state == "complete":
        return HEALTHY
    if record.state in {"failed", "cancelled"}:
        return ATTENTION
    if record.state == "awaiting-callback":
        return WAITING
    return ACTIVE


def executor_status(record: OperationRecord, steps: tuple[StepView, ...]) -> str:
    """Classify the executor separately from downstream pipeline work."""

    if record.state == "attention-required" or record.pending_effect:
        return "attention"
    if record.state in TERMINAL:
        return record_activity(record)
    downstream = any(
        view.status in {"running", "attention"}
        for view in steps
        if view.primitive.split("@", 1)[0] != "model_step"
    )
    return "awaiting-transition" if downstream else record_activity(record)
