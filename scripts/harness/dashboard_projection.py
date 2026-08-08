"""Read-only projection of durable harness state for the terminal dashboard.

The projection never writes, never transitions, and never reads prompt,
callback, or review bodies: it reads typed durable metadata only.  Progress is
projected from the real compiled pipeline bound to the operation contract, not
from a second hand-maintained shape, so the dashboard cannot drift away from
the definition the supervisor actually executes.  Anything the projection
cannot resolve exactly becomes ``request-coordinator-classification`` rather
than an invented status, because a read-only surface must never let the
coordinator believe an unknown state is progress.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from .contracts import OperationRecord
from .diagnostics import observe
from .pipeline_builtins import compiled_executable_for_contract
from .pipelines import CompiledPipeline, reconcile_pipeline
from .state_machine import TERMINAL
from .status_segment import CONTROLLER_KINDS, LiveInventory, record_controller
from .store import OperationStore, StoreError


MAX_ISSUES = 8
MAX_PROGRAMS = 8
MAX_LANES = 8
MAX_VISITS = 16

HEALTHY = "healthy"
ACTIVE = "in-progress"
WAITING = "waiting"
ATTENTION = "attention-required"
COORDINATOR = "request-coordinator-classification"
CLASSIFICATION_ORDER = (HEALTHY, ACTIVE, WAITING, ATTENTION, COORDINATOR)

SURFACE_BOUND_STATES = frozenset({"running", "awaiting-callback"})
# Mirrors the production review observation vocabulary consumed by
# runtime_worker_summary.advance_compiled_pipeline; a status outside it stays
# unknown rather than being guessed into the nearest familiar bucket.
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
class StepView:
    step_id: str
    primitive: str
    session_mode: str
    status: str
    visits: int


@dataclass(frozen=True)
class LaneView:
    lane_id: str
    scope: str
    members: tuple[str, ...]
    active: bool


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


@dataclass(frozen=True)
class DashboardProjection:
    owner_id: str
    classification: str
    surface_probe: str
    programs: tuple[ProgramView, ...] = ()
    issues: tuple[IssueView, ...] = ()
    truncated: Mapping[str, int] = field(default_factory=dict)
    schema_version: int = 1


def _read_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _runtime_root(store: OperationStore, record: OperationRecord) -> Path:
    return (
        store.root
        / "owners"
        / record.spec.owner_id
        / "runtime"
        / record.spec.operation_id
    )


def _gate(store: OperationStore, owner_id: str) -> dict[str, Any] | None:
    return _read_object(
        store.root / "review-data" / owner_id / owner_id / "review-gate.json"
    )


def _program_gate(
    gate: Mapping[str, Any] | None,
    record: OperationRecord,
) -> Mapping[str, Any] | None:
    """Bind the owner review gate to the one program it actually reviews."""

    if gate is None:
        return None
    subject = str(
        gate.get("dispatch_operation_id") or record.spec.owner_id
    )
    return gate if subject == record.spec.operation_id else None


def _compiled(record: OperationRecord) -> tuple[str, CompiledPipeline | None]:
    """Resolve the exact compiled contract the operation was dispatched under."""

    if not record.spec.contract_sha256:
        return "", None
    try:
        name, compiled = compiled_executable_for_contract(
            record.spec.contract_sha256
        )
    except ValueError:
        return "", None
    return name, compiled


def _fix_visits(runtime: Path, step_id: str) -> tuple[int, ...]:
    """Return the bounded loop passes that durably receipted one fix step."""

    root = runtime / "pipeline-fix"
    if not root.is_dir():
        return ()
    visits: list[int] = []
    for path in sorted(root.glob("pass-*")):
        suffix = path.name.removeprefix("pass-")
        if not suffix.isdigit():
            continue
        if (path / step_id / "receipt.json").is_file():
            visits.append(int(suffix))
    return tuple(visits[:MAX_VISITS])


def _verification_visits(runtime: Path) -> tuple[int, ...]:
    root = runtime / "pipeline-verification"
    if not root.is_dir():
        return ()
    return tuple(range(len(sorted(root.glob("*/receipt.json"))[:MAX_VISITS])))


def _model_step_status(record: OperationRecord) -> str:
    if record.state == "attention-required":
        return "attention"
    if record.state in TERMINAL:
        return "complete"
    if record.state in {"created", "preflight"}:
        return "pending"
    return "running"


def _review_status(gate: Mapping[str, Any] | None) -> str:
    if gate is None:
        return "pending"
    return REVIEW_OBSERVATIONS.get(str(gate.get("status") or ""), "unknown")


def _step_view(
    step: Any,
    identity: str,
    *,
    record: OperationRecord,
    runtime: Path,
    gate: Mapping[str, Any] | None,
) -> tuple[StepView, bool]:
    """Bind one compiled step to durable evidence, or derive it from the record.

    The second value reports whether the status came from durable step
    evidence. A derived status is only ever a statement about the operation, so
    the caller may not read it as proof that this particular step is running.
    """

    if step.primitive_id == "review":
        visits: tuple[int, ...] = ()
        evidence = gate is not None
        status = _review_status(gate)
    elif step.primitive_id == "verify":
        visits = _verification_visits(runtime)
        evidence = bool(visits)
        status = "complete" if visits else _model_step_status(record)
    else:
        visits = _fix_visits(runtime, step.step_id)
        evidence = bool(visits)
        status = "complete" if visits else _model_step_status(record)
    return (
        StepView(step.step_id, identity, step.session_mode, status, len(visits)),
        evidence,
    )


def _steps(
    record: OperationRecord,
    compiled: CompiledPipeline,
    runtime: Path,
    gate: Mapping[str, Any] | None,
) -> tuple[tuple[StepView, ...], str]:
    """Project every compiled step, then ask the compiler for the next action."""

    raw = [
        _step_view(step, identity, record=record, runtime=runtime, gate=gate)
        for step, identity in zip(
            compiled.definition.steps,
            compiled.resolved_primitives,
            strict=True,
        )
    ]
    # Durable evidence for a later step proves that every earlier step already
    # finished, and nothing after the frontier can have started. Only derived
    # statuses are corrected this way, so a genuine evidence conflict still
    # reaches the compiler and still becomes an unknown.
    last_complete = max(
        (
            index
            for index, (view, evidence) in enumerate(raw)
            if evidence and view.status == "complete"
        ),
        default=-1,
    )
    collected: list[StepView] = []
    frontier = False
    for index, (view, evidence) in enumerate(raw):
        if not evidence and index < last_complete:
            view = replace(view, status="complete")
        elif not evidence and frontier:
            view = replace(view, status="pending")
        if view.status != "complete":
            frontier = True
        collected.append(view)
    views = tuple(collected)
    observations = {view.step_id: view.status for view in views}
    if any(view.status == "unknown" for view in views):
        return views, "unknown"
    try:
        progress = reconcile_pipeline(compiled, observations)
    except Exception:
        return views, "unknown"
    return views, progress.action


def _loop_limit(compiled: CompiledPipeline) -> int:
    if "bounded_loop" not in {
        identity.split("@", 1)[0]
        for identity in compiled.resolved_control_primitives
    }:
        return 1
    return max(
        item.total_pass_limit
        for item in compiled.definition.completion_policies
    )


def _lanes(
    members: list[OperationRecord],
    gate: Mapping[str, Any] | None,
) -> tuple[LaneView, ...]:
    """Project the durable operation lanes plus any declared review axes."""

    grouped: dict[str, list[OperationRecord]] = {}
    for member in members:
        grouped.setdefault(member.lane_id, []).append(member)
    lanes = [
        LaneView(
            lane_id,
            "operation",
            tuple(item.spec.operation_id for item in records),
            any(item.state not in TERMINAL for item in records),
        )
        for lane_id, records in sorted(grouped.items())
    ]
    axes = gate.get("lanes") if isinstance(gate, Mapping) else None
    if isinstance(axes, list):
        running = _review_status(gate) == "running"
        for axis in axes:
            name = axis.get("axis") if isinstance(axis, Mapping) else None
            if isinstance(name, str) and name:
                lanes.append(LaneView(name, "review-axis", (), running))
    return tuple(lanes[:MAX_LANES])


def _surface(
    record: OperationRecord,
    inventory: LiveInventory | None,
) -> str:
    surface_id = record.resources.surface_id
    if not surface_id:
        return "none" if record.state in TERMINAL else "unbound"
    if inventory is None:
        return "unknown"
    if inventory.ambiguous(surface_id):
        return "ambiguous"
    return "live" if inventory.contains(surface_id) else "missing"


def _program_classification(
    record: OperationRecord,
    *,
    surface: str,
    next_action: str,
    pipeline_resolved: bool,
) -> str:
    if record.state == "attention-required" or record.pending_effect:
        return ATTENTION
    if surface in {"ambiguous", "unknown"} or not pipeline_resolved:
        return COORDINATOR
    if next_action == "unknown":
        return COORDINATOR
    if surface == "missing" and record.state in SURFACE_BOUND_STATES:
        return ATTENTION
    if record.state in TERMINAL:
        return HEALTHY
    if record.state == "awaiting-callback":
        return WAITING
    return ACTIVE


def _uncompiled_program(
    record: OperationRecord,
    lanes: tuple[LaneView, ...],
    surface: str,
) -> ProgramView:
    """Project an operation that no compiled pipeline currently explains.

    An operation with no contract is not pipeline-bound at all — review and
    research programs are the ordinary case — so only a contract that exists
    and resolves to nothing is a real projection failure.
    """

    bound = bool(record.spec.contract_sha256)
    return ProgramView(
        record.spec.operation_id,
        record.spec.kind,
        record.state,
        record.revision,
        "unresolved" if bound else "none",
        record.spec.contract_sha256,
        (),
        (),
        lanes,
        "unknown" if bound else "none",
        0,
        0,
        surface,
        _program_classification(
            record,
            surface=surface,
            next_action="none",
            pipeline_resolved=not bound,
        ),
    )


def _program(
    store: OperationStore,
    record: OperationRecord,
    members: list[OperationRecord],
    *,
    gate: Mapping[str, Any] | None,
    inventory: LiveInventory | None,
) -> ProgramView:
    name, compiled = _compiled(record)
    runtime = _runtime_root(store, record)
    gate = _program_gate(gate, record)
    lanes = _lanes(members, gate)
    surface = _surface(record, inventory)
    if compiled is None:
        return _uncompiled_program(record, lanes, surface)
    steps, next_action = _steps(record, compiled, runtime, gate)
    definition = compiled.definition
    return ProgramView(
        record.spec.operation_id,
        record.spec.kind,
        record.state,
        record.revision,
        f"{name}@{definition.version}",
        compiled.definition_sha256,
        compiled.resolved_control_primitives,
        steps,
        lanes,
        next_action,
        max((view.visits for view in steps), default=0),
        _loop_limit(compiled),
        surface,
        _program_classification(
            record,
            surface=surface,
            next_action=next_action,
            pipeline_resolved=True,
        ),
    )


def _diagnostic_issues(store_root: Path | str, owner_id: str) -> list[IssueView]:
    try:
        packet = observe(store_root, owner_id)
    except (StoreError, OSError, ValueError):
        return [
            IssueView(
                "diagnostics-unreadable",
                owner_id,
                "durable diagnostics could not be observed",
                COORDINATOR,
            )
        ]
    return [
        IssueView(
            str(signal["code"]),
            str(signal["operation_id"]),
            str(signal["state"]),
            COORDINATOR if packet["model_required"] else ATTENTION,
        )
        for signal in packet["signals"]
    ]


def _program_issues(programs: tuple[ProgramView, ...]) -> list[IssueView]:
    issues: list[IssueView] = []
    for program in programs:
        if program.pipeline == "unresolved":
            issues.append(
                IssueView(
                    "pipeline-contract-unresolved",
                    program.operation_id,
                    "operation contract matches no compiled pipeline",
                    COORDINATOR,
                )
            )
        if program.surface in {"missing", "ambiguous"}:
            issues.append(
                IssueView(
                    f"surface-{program.surface}",
                    program.operation_id,
                    f"recorded surface is {program.surface} in the cmux tree",
                    ATTENTION if program.surface == "missing" else COORDINATOR,
                )
            )
        if program.next_action == "unknown" and program.pipeline not in {
            "unresolved",
            "none",
        }:
            issues.append(
                IssueView(
                    "pipeline-progress-unknown",
                    program.operation_id,
                    "durable step evidence does not form a compiled prefix",
                    COORDINATOR,
                )
            )
    return issues


def _invalid_records(store: OperationStore, owner_id: str) -> tuple[
    list[OperationRecord],
    list[IssueView],
]:
    directory = store.root / "owners" / owner_id / "operations"
    if not directory.is_dir():
        return [], []
    records: list[OperationRecord] = []
    issues: list[IssueView] = []
    for path in sorted(directory.glob("*.json")):
        try:
            records.append(store.read(owner_id, path.stem))
        except StoreError:
            issues.append(
                IssueView(
                    "operation-record-invalid",
                    path.stem,
                    "durable record cannot be parsed",
                    COORDINATOR,
                )
            )
    return records, issues


def _programs(
    store: OperationStore,
    records: list[OperationRecord],
    owner_id: str,
    inventory: LiveInventory | None,
) -> tuple[list[OperationRecord], tuple[ProgramView, ...]]:
    """Bind every record to at most one controller and project those programs."""

    gate = _gate(store, owner_id)
    controllers = [
        record for record in records if record.spec.kind in CONTROLLER_KINDS
    ]
    bindings = {
        record.spec.operation_id: record_controller(record, controllers)
        for record in records
    }
    programs = tuple(
        _program(
            store,
            controller,
            [
                record
                for record in records
                if (bound := bindings[record.spec.operation_id]) is not None
                and bound.spec.operation_id == controller.spec.operation_id
            ],
            gate=gate,
            inventory=inventory,
        )
        for controller in controllers[:MAX_PROGRAMS]
    )
    return controllers, programs


def _deduplicated(issues: list[IssueView]) -> list[IssueView]:
    seen: set[tuple[str, str]] = set()
    unique: list[IssueView] = []
    for issue in issues:
        identity = (issue.code, issue.operation_id)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(issue)
    return unique


def project(
    store_root: Path | str,
    owner_id: str,
    *,
    inventory: LiveInventory | None = None,
    surface_probe: str = "unavailable",
) -> DashboardProjection:
    """Project one owner's durable harness state as a read-only dashboard."""

    store = OperationStore(store_root)
    records, issues = _invalid_records(store, owner_id)
    controllers, programs = _programs(store, records, owner_id, inventory)
    issues.extend(_program_issues(programs))
    issues.extend(_diagnostic_issues(store.root, owner_id))
    bounded = _deduplicated(issues)
    classification = HEALTHY
    for program in programs:
        classification = escalate(classification, program.classification)
    # Classification reads every observed issue, not just the displayed
    # prefix: bounding the view must never bound the coordinator's signal.
    for issue in bounded:
        classification = escalate(classification, issue.classification)
    return DashboardProjection(
        owner_id,
        classification,
        surface_probe,
        programs,
        tuple(bounded[:MAX_ISSUES]),
        {
            "programs": max(len(controllers) - MAX_PROGRAMS, 0),
            "issues": max(len(bounded) - MAX_ISSUES, 0),
        },
    )
