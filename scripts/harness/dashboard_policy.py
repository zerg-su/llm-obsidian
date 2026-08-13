"""Immutable dashboard values and pure read-only classification policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .contracts import OperationRecord
from .state_machine import TERMINAL
from .status_segment import CONTROLLER_KINDS
from .verification_attempt import (
    MAX_SAME_HEAD_ATTEMPT_INDEX,
    verification_input_sha256,
)


MAX_ISSUES = 5
MAX_PROGRAMS = 8
MAX_LANES = 8
MAX_CHILDREN = 8
MAX_DEPTH = 4
MAX_REVIEW_CYCLES = 5
FINALIZATION_INDEPENDENT_KINDS = frozenset({"structural-pivot"})

UNKNOWN = "unknown"
HEALTHY = "healthy"
ACTIVE = "in-progress"
WAITING = "waiting"
ATTENTION = "attention-required"
COORDINATOR = "request-coordinator-classification"
CLASSIFICATION_ORDER = (HEALTHY, ACTIVE, WAITING, ATTENTION, COORDINATOR)
SURFACE_BOUND_STATES = frozenset({"running", "awaiting-callback"})

# Keep this vocabulary aligned with the review observations produced by
# RuntimeWorkerSummaryMixin.advance_compiled_pipeline. Unknown values fail
# closed through the caller instead of being optimistically reinterpreted.
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
class TimingView:
    """One display-only interval derived from accepted durable timestamps."""

    mode: str = UNKNOWN
    seconds: int | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"elapsed", "duration", UNKNOWN}:
            raise ValueError("dashboard timing mode is invalid")
        if self.mode == UNKNOWN:
            if self.seconds is not None:
                raise ValueError("unknown dashboard timing has no seconds")
        elif (
            isinstance(self.seconds, bool)
            or not isinstance(self.seconds, int)
            or self.seconds < 0
        ):
            raise ValueError("dashboard timing seconds must be non-negative")


UNKNOWN_TIMING = TimingView()


@dataclass(frozen=True)
class ReviewSummaryView:
    """Bounded scalar review evidence; unavailable values stay unknown."""

    cycle: int | None = None
    limit: int | None = None
    findings: int | None = None
    material_findings: int | None = None

    def __post_init__(self) -> None:
        for value in (
            self.cycle,
            self.limit,
            self.findings,
            self.material_findings,
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError("dashboard review summary is invalid")


UNKNOWN_REVIEW = ReviewSummaryView()


@dataclass(frozen=True)
class TaskResultView:
    """Content-free terminal Wiki Summary projection."""

    status: str = UNKNOWN
    disposition: str = UNKNOWN
    evidence_count: int = 0
    gap_count: int = 0
    plan_close_status: str = UNKNOWN

    def __post_init__(self) -> None:
        if (
            self.status not in {UNKNOWN, "complete"}
            or self.disposition
            not in {UNKNOWN, "achieved", "partially-achieved", "not-achieved"}
            or type(self.evidence_count) is not int
            or self.evidence_count < 0
            or type(self.gap_count) is not int
            or self.gap_count < 0
            or self.plan_close_status not in {UNKNOWN, "closed", "conflict", "retained"}
        ):
            raise ValueError("dashboard task result is invalid")


UNKNOWN_TASK_RESULT = TaskResultView()


@dataclass(frozen=True)
class ChildView:
    operation_id: str
    kind: str
    state: str
    status: str
    route: RouteView
    children: tuple["ChildView", ...] = ()
    timing: TimingView = UNKNOWN_TIMING


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
    timing: TimingView = UNKNOWN_TIMING
    review: ReviewSummaryView = UNKNOWN_REVIEW


@dataclass(frozen=True)
class LifecyclePhaseView:
    """One display-only phase in the bounded exact-HEAD correction history."""

    kind: str
    cycle: int
    status: str
    operation_id: str = ""
    route: RouteView = UNKNOWN_ROUTE
    children: tuple[ChildView, ...] = ()
    timing: TimingView = UNKNOWN_TIMING
    review: ReviewSummaryView = UNKNOWN_REVIEW

    def __post_init__(self) -> None:
        if (
            self.kind not in {"review", "fix", "reverify"}
            or isinstance(self.cycle, bool)
            or not isinstance(self.cycle, int)
            or not 1 <= self.cycle <= MAX_REVIEW_CYCLES
            or self.status
            not in {"pending", "running", "complete", "attention", "unknown"}
        ):
            raise ValueError("dashboard lifecycle phase is invalid")


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
    timing: TimingView = UNKNOWN_TIMING
    task_name: str = UNKNOWN
    self_healed_count: int = 0
    current_stage: str = UNKNOWN
    task_result: TaskResultView = UNKNOWN_TASK_RESULT
    history: tuple[LifecyclePhaseView, ...] = ()
    active_route: RouteView = UNKNOWN_ROUTE
    active_stage: str = ""


@dataclass(frozen=True)
class DashboardProjection:
    owner_id: str
    classification: str
    surface_probe: str
    programs: tuple[ProgramView, ...] = ()
    issues: tuple[IssueView, ...] = ()
    truncated: Mapping[str, int] = field(default_factory=dict)
    schema_version: int = 1
    observed_at: float | None = None


def record_activity(record: OperationRecord) -> str:
    """Classify one durable record without inventing unowned live work.

    Only controller kinds are held to the surface-resource rule: leaf work can
    be durably pending without owning a cmux surface. A pending effect is also
    deliberately left to program policy instead of becoming attention here.
    """

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
    """Fold child activity into the status consumed by pipeline policy.

    Stopped children count as finished work because Harness closes a completed
    review parent by cancelling its now-unneeded provider operation.
    """

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


def active_finalization_operation(
    root: OperationRecord,
    records: Sequence[OperationRecord],
) -> tuple[RouteView, str] | None:
    """Select one exact active independent finalization display override."""

    root_id = root.spec.operation_id
    active = tuple(
        record
        for record in records
        if record.spec.kind in FINALIZATION_INDEPENDENT_KINDS
        and record.spec.parent_operation_id == root_id
        and record.spec.root_operation_id == root_id
        and record.state not in TERMINAL
    )
    if not active:
        return None
    if len(active) != 1:
        return UNKNOWN_ROUTE, UNKNOWN
    return route_view(active[0]), active[0].spec.kind


def current_verification_ids(
    record: OperationRecord,
    step: Any,
    gate: Mapping[str, Any] | None,
) -> frozenset[str]:
    """Derive only verification children bound to the gate's exact attempt."""

    # Local import keeps the immutable value module independent of the
    # receipt reader that consumes these values.
    from .dashboard_receipts import verification_identity

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
    input_sha256 = verification_input_sha256(
        record.spec.contract_sha256,
        head_sha,
        profile_sha256,
        step.schema_version,
    )
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
    """Classify the executor separately from downstream pipeline work.

    Once a downstream step owns the frontier, the root executor is waiting for
    that transition; absence of its old runtime resource is not a live-work
    failure and must not eclipse the downstream operation.
    """

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


def program_issues(programs: tuple[ProgramView, ...]) -> list[IssueView]:
    """Classify bounded issues from already-projected program facts."""

    issues: list[IssueView] = []
    for program in programs:
        issues.extend(
            IssueView(
                step.evidence_issue,
                program.operation_id,
                f"{step.step_id} durable evidence is not accepted",
                ATTENTION,
            )
            for step in program.steps
            if step.evidence_issue
        )
        if program.state in {"failed", "cancelled"}:
            issues.append(IssueView(
                f"terminal-{program.state}", program.operation_id,
                f"operation terminated as {program.state}", ATTENTION,
            ))
        if program.pipeline == "unresolved":
            issues.append(IssueView(
                "pipeline-contract-unresolved", program.operation_id,
                "operation contract matches no compiled pipeline", COORDINATOR,
            ))
        if (
            program.surface == "unbound"
            and program.state in SURFACE_BOUND_STATES
            and program.executor_status != "awaiting-transition"
        ):
            issues.append(IssueView(
                "operation-resources-absent", program.operation_id,
                "nonterminal operation owns no recorded runtime resource", ATTENTION,
            ))
        if program.surface in {"missing", "ambiguous"}:
            issues.append(IssueView(
                f"surface-{program.surface}", program.operation_id,
                f"recorded surface is {program.surface} in the cmux tree",
                ATTENTION if program.surface == "missing" else COORDINATOR,
            ))
        if program.next_action == "unknown" and program.pipeline not in {
            "unresolved", "none",
        }:
            issues.append(IssueView(
                "pipeline-progress-unknown", program.operation_id,
                "durable step evidence does not form a compiled prefix", COORDINATOR,
            ))
    return issues
