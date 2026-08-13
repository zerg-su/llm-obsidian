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

from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from review_contract import REVIEW_PARENT_KINDS

from .contracts import ContractError, OperationRecord, _identifier
from .custom_pipelines import CustomPipelinePolicy, resolve_custom_executable
from .diagnostics import observe
from .pipeline_builtins import builtin_registry, compiled_executable_for_contract
from .pipelines import CompiledPipeline, reconcile_pipeline
from .state_machine import TERMINAL
from .status_segment import CONTROLLER_KINDS, LiveInventory
from .store import OperationStore, StoreError
from .dashboard_receipts import (
    absolute_path_is_safe,
    fix_receipt_visits,
    liveness_interval_start,
    read_gate,
    review_summary,
    root_task_name,
    repair_receipt_count,
    root_task_result,
    root_interval_start,
    root_timing,
    verification_receipt_interval,
    verification_receipt_timing,
    verification_receipt_visits,
)
from .dashboard_policy import (
    ACTIVE,
    ATTENTION,
    COORDINATOR,
    HEALTHY,
    MAX_CHILDREN,
    MAX_DEPTH,
    MAX_ISSUES,
    MAX_LANES,
    MAX_PROGRAMS,
    REVIEW_OBSERVATIONS,
    UNKNOWN,
    UNKNOWN_REVIEW,
    UNKNOWN_ROUTE,
    UNKNOWN_TIMING,
    WAITING,
    ChildView,
    DashboardProjection,
    IssueView,
    LaneView,
    ProgramView,
    RouteView,
    StepView,
    TimingView,
    aggregate as _aggregate,
    current_verification_ids as _current_verification_ids,
    escalate,
    executor_status as _executor_status,
    program_issues as _program_issues,
    program_classification as _program_classification,
    record_activity as _record_activity,
    route_view as _route_view,
)
from .dashboard_history import project_history
from .dashboard_tree import (
    child_tree as _child_tree,
    child_views as _child_views,
    current_review_ids as _current_review_ids,
    root_id as _root_id,
)


# Which durable operation kinds are executed by which compiled primitive.  A
# child whose kind appears in no set has no exact step lineage and is shown at
# the program level rather than guessed onto the nearest-looking step.
STEP_PRIMITIVE_KINDS = {
    "verify": frozenset({"pipeline-verify"}),
    "review": frozenset(REVIEW_PARENT_KINDS | {"review-round"}),
}

def _runtime_root(store: OperationStore, record: OperationRecord) -> Path:
    return (
        store.root
        / "owners"
        / record.spec.owner_id
        / "runtime"
        / record.spec.operation_id
    )


def _program_gate(
    gate: Mapping[str, Any] | None,
    record: OperationRecord,
) -> Mapping[str, Any] | None:
    """Bind the owner review gate to the one program it actually reviews."""

    if gate is None:
        return None
    subject = str(gate.get("dispatch_operation_id") or "")
    return gate if subject == record.spec.operation_id else None


def _compiled(
    store: OperationStore, record: OperationRecord
) -> tuple[str, CompiledPipeline | None]:
    """Resolve the exact compiled contract the operation was dispatched under."""

    if not record.spec.contract_sha256:
        return "", None
    try:
        name, compiled = compiled_executable_for_contract(
            record.spec.contract_sha256
        )
    except ValueError:
        try:
            _baseline, compiled, _commands, spec = resolve_custom_executable(
                store_root=_runtime_root(store, record).parent,
                operation_id=record.spec.operation_id,
                definition_sha256=record.spec.contract_sha256,
                registry=builtin_registry(),
                policy=CustomPipelinePolicy.default(),
                capabilities=("route:resolved",),
            )
        except (ContractError, OSError, ValueError):
            return "", None
        return f"custom/{spec.spec_id}", compiled
    return name, compiled


def _bind_children(
    children: tuple[ChildView, ...],
    compiled: CompiledPipeline | None,
) -> tuple[dict[str, list[ChildView]], tuple[ChildView, ...]]:
    """Bind each direct child to the compiled step whose primitive executes it.

    A durable child records the pipeline it belongs to, not which step of that
    pipeline ran it.  When a definition declares two steps of the same
    primitive — expressible in a custom pipeline — that identity is genuinely
    ambiguous, so the child stays at the program level instead of being
    collapsed onto whichever matching step happens to come first.
    """

    steps = compiled.definition.steps if compiled is not None else ()
    by_step: dict[str, list[ChildView]] = {}
    loose: list[ChildView] = []
    for child in children:
        matches = [
            step.step_id
            for step in steps
            if child.kind
            in STEP_PRIMITIVE_KINDS.get(step.primitive_id, frozenset())
        ]
        if len(matches) == 1:
            by_step.setdefault(matches[0], []).append(child)
        else:
            loose.append(child)
    return by_step, tuple(loose)


def _model_step_status(record: OperationRecord) -> str:
    if record.state == "attention-required":
        return "attention"
    if record.state == "complete":
        return "complete"
    if record.state in {"failed", "cancelled"}:
        return "stopped"
    if record.state in {"created", "preflight"}:
        return "pending"
    return "running"


def _review_status(gate: Mapping[str, Any] | None) -> str:
    if gate is None:
        return "pending"
    return REVIEW_OBSERVATIONS.get(str(gate.get("status") or ""), "unknown")


def _review_head(gate: Mapping[str, Any] | None) -> str:
    context = gate.get("context") if isinstance(gate, Mapping) else None
    return str(context.get("head_sha") or "") if isinstance(context, Mapping) else ""


def _step_route(
    step: Any,
    record: OperationRecord,
    children: tuple[ChildView, ...],
) -> RouteView:
    """Report the frozen route of whichever record executes this exact step."""

    if step.primitive_id == "model_step":
        return _route_view(record)
    if step.primitive_id == "verify":
        if children:
            return children[0].route
        return replace(
            UNKNOWN_ROUTE,
            preset=record.spec.verification_profile or UNKNOWN,
        )
    if step.primitive_id == "review":
        return next(
            (
                child.route
                for child in children
                if child.kind in REVIEW_PARENT_KINDS
            ),
            UNKNOWN_ROUTE,
        )
    return UNKNOWN_ROUTE


def _step_view(
    step: Any,
    identity: str,
    *,
    store: OperationStore,
    record: OperationRecord,
    runtime: Path,
    gate: Mapping[str, Any] | None,
    children: tuple[ChildView, ...],
    observed_at: float,
    review_limit: int,
    root_interval: object = UNKNOWN_TIMING,
    root_owned: bool = False,
) -> tuple[StepView, bool]:
    """Bind one compiled step to durable evidence, or derive it from the record.

    The second value reports whether the status came from durable step
    evidence. A derived status is only ever a statement about the operation, so
    the caller may not read it as proof that this particular step is running.
    A live child operation is durable evidence about the step it executes: it
    is what proves that verification, not the finished implementation, is the
    step currently running.
    """

    child_statuses = tuple(child.status for child in children)
    issue = ""
    if step.primitive_id == "review":
        visits: tuple[int, ...] = ()
        if gate is not None:
            evidence, status = True, _review_status(gate)
        else:
            evidence, status = bool(children), _aggregate(child_statuses)
    elif step.primitive_id == "verify":
        visits, issue = verification_receipt_visits(
            store,
            record,
            runtime,
            exact_head_sha=_review_head(gate),
        )
        current_ids = _current_verification_ids(record, step, gate)
        if issue == "verification-receipt-missing" and any(
            child.status == "running" and child.operation_id in current_ids
            for child in children
        ):
            evidence, status, issue = True, _aggregate(child_statuses), ""
        elif issue:
            evidence, status = True, "attention"
        elif visits:
            evidence, status = True, "complete"
        elif children:
            evidence, status = True, _aggregate(child_statuses)
        else:
            evidence, status = False, _model_step_status(record)
    else:
        visits, issue = fix_receipt_visits(store, record, runtime, step.step_id)
        evidence = bool(visits) or bool(issue)
        status = "attention" if issue else (
            "complete" if visits else _model_step_status(record)
        )
    timing = (
        root_interval
        if root_owned
        and step.primitive_id == "model_step"
        and step.session_mode == "worktree"
        else UNKNOWN_TIMING
    )
    review = UNKNOWN_REVIEW
    if step.primitive_id == "verify":
        timing = verification_receipt_timing(
            store,
            record,
            runtime,
            observed_at,
            exact_head_sha=_review_head(gate),
        )
    if timing.mode == UNKNOWN and status == "running":
        elapsed = [child.timing for child in children if child.timing.mode == "elapsed"]
        if elapsed:
            timing = max(elapsed, key=lambda item: item.seconds or 0)
    if step.primitive_id == "review":
        review = review_summary(store, record, gate, limit=review_limit)
    return (
        StepView(
            step_id=step.step_id,
            primitive=identity,
            session_mode=step.session_mode,
            status=status,
            visits=len(visits),
            route=_step_route(step, record, children),
            children=children,
            evidence_issue=issue,
            timing=timing,
            review=review,
        ),
        evidence,
    )


def _steps(
    store: OperationStore,
    record: OperationRecord,
    compiled: CompiledPipeline,
    runtime: Path,
    gate: Mapping[str, Any] | None,
    children: Mapping[str, list[ChildView]],
    observed_at: float,
    root_interval: object = UNKNOWN_TIMING,
) -> tuple[tuple[StepView, ...], str]:
    """Project every compiled step, then ask the compiler for the next action."""

    root_step_interval = _root_step_interval(
        store,
        record,
        compiled,
        children,
        observed_at,
        root_interval,
    )

    raw = [
        _step_view(
            step,
            identity,
            store=store,
            record=record,
            runtime=runtime,
            gate=gate,
            children=tuple(children.get(step.step_id, ())),
            observed_at=observed_at,
            review_limit=max(
                item.total_pass_limit
                for item in compiled.definition.completion_policies
            ),
            root_interval=root_step_interval,
            root_owned=index == 0,
        )
        for index, (step, identity) in enumerate(zip(
            compiled.definition.steps,
            compiled.resolved_primitives,
            strict=True,
        ))
    ]
    if record.state in {"failed", "cancelled"}:
        accepted_frontier = max(
            (
                index
                for index, (view, evidence) in enumerate(raw)
                if evidence and view.status == "complete"
            ),
            default=-1,
        )
        interrupted = accepted_frontier + 1
        return (
            tuple(
                replace(
                    view,
                    status=(
                        "complete"
                        if index <= accepted_frontier
                        else "stopped" if index == interrupted else "pending"
                    ),
                )
                for index, (view, _evidence) in enumerate(raw)
            ),
            "attention",
        )
    # Durable evidence for a later step proves that every earlier step already
    # finished, and nothing after the frontier can have started. Only derived
    # statuses are corrected this way, so a genuine evidence conflict still
    # reaches the compiler and still becomes an unknown.
    frontier_index = max(
        (
            index
            for index, (view, evidence) in enumerate(raw)
            if evidence and view.status != "pending"
        ),
        default=-1,
    )
    collected: list[StepView] = []
    frontier = False
    for index, (view, evidence) in enumerate(raw):
        if not evidence and index < frontier_index:
            view = replace(view, status="complete")
        elif not evidence and frontier:
            view = replace(view, status="pending")
        if view.status != "complete":
            frontier = True
        collected.append(view)
    views = tuple(collected)
    verify_index = next(
        (
            index
            for index, view in enumerate(views)
            if view.primitive.split("@", 1)[0] == "verify"
        ),
        -1,
    )
    if verify_index >= 0:
        verify = views[verify_index]
        gate_status = str(
            gate.get("status") if isinstance(gate, Mapping) else ""
        )
        if (
            verify.evidence_issue == "verification-receipt-missing"
            and gate_status == "verifying"
        ):
            normalized = tuple(
                replace(view, status="complete")
                if index < verify_index and not raw[index][1]
                else replace(view, status="pending")
                if index > verify_index
                else view
                for index, view in enumerate(views)
            )
            return normalized, "attention"
        if verify.status == "running" and gate_status == "verifying":
            views = tuple(
                replace(view, status="pending") if index > verify_index else view
                for index, view in enumerate(views)
            )
    observations = {view.step_id: view.status for view in views}
    if any(view.status == "unknown" for view in views):
        return views, "unknown"
    try:
        progress = reconcile_pipeline(compiled, observations)
    except Exception:
        return views, "unknown"
    return views, progress.action


def _root_step_interval(
    store: OperationStore,
    record: OperationRecord,
    compiled: CompiledPipeline,
    children: Mapping[str, list[ChildView]],
    observed_at: float,
    root_interval: object,
) -> object:
    """Freeze the first root-owned model step at exact later-step liveness."""
    steps = compiled.definition.steps
    if (
        not isinstance(root_interval, TimingView)
        or not steps
        or steps[0].primitive_id != "model_step"
        or steps[0].session_mode != "worktree"
    ):
        return root_interval
    later = tuple(
        child
        for step in steps[1:]
        for child in children.get(step.step_id, ())
    )
    if not later:
        return root_interval
    root_start = root_interval_start(store, record, observed_at)
    if root_start is None:
        return UNKNOWN_TIMING
    starts: list[float] = []
    for child in later:
        try:
            child_record = store.read(record.spec.owner_id, child.operation_id)
        except StoreError:
            return UNKNOWN_TIMING
        if (
            child_record.spec.parent_operation_id != record.spec.operation_id
            or child_record.spec.root_operation_id
            not in {"", record.spec.operation_id}
        ):
            return UNKNOWN_TIMING
        start = liveness_interval_start(store, child_record, observed_at)
        if start is None and child_record.spec.kind == "pipeline-verify":
            liveness_path = store.root / "owners" / child_record.spec.owner_id
            liveness_path /= f"runtime/{child.operation_id}/liveness/state.json"
            if liveness_path.exists():
                return UNKNOWN_TIMING
            runtime = _runtime_root(store, record)
            interval = verification_receipt_interval(
                store, record, runtime, observed_at,
                operation_id=child.operation_id,
            )
            if interval is None:
                receipt = runtime / f"pipeline-verification/{child.operation_id}/receipt.json"
                if receipt.exists():
                    return UNKNOWN_TIMING
                continue
            start = interval[0]
        if start is None or start < root_start:
            return UNKNOWN_TIMING
        starts.append(start)
    if not starts:
        return root_interval
    return TimingView("duration", int(min(starts) - root_start))


def _loop_limit(compiled: CompiledPipeline) -> int:
    if "bounded_loop" not in {
        identity.split("@", 1)[0]
        for identity in compiled.resolved_control_primitives
    }:
        return 0
    return max(
        item.total_pass_limit
        for item in compiled.definition.completion_policies
    )


def _lane_status(records: list[OperationRecord]) -> str:
    """A lane is only active while some member is really doing live work."""

    statuses = tuple(_record_activity(record) for record in records)
    if any(status == "attention" for status in statuses):
        return "attention"
    return "active" if any(status == "running" for status in statuses) else "idle"


def _lanes(
    members: list[OperationRecord],
    gate: Mapping[str, Any] | None,
) -> tuple[tuple[LaneView, ...], int]:
    """Project the durable operation lanes plus any declared review axes."""

    grouped: dict[str, list[OperationRecord]] = {}
    for member in members:
        grouped.setdefault(member.lane_id, []).append(member)
    operation_lanes = [
        LaneView(
            lane_id,
            "operation",
            tuple(item.spec.operation_id for item in records),
            _lane_status(records),
        )
        for lane_id, records in sorted(grouped.items())
    ]
    axis_lanes: list[LaneView] = []
    axes = gate.get("lanes") if isinstance(gate, Mapping) else None
    review_active = _review_status(gate) == "running"
    if isinstance(axes, list):
        status = "active" if review_active else "idle"
        for axis in axes:
            name = axis.get("axis") if isinstance(axis, Mapping) else None
            if isinstance(name, str) and name:
                axis_lanes.append(LaneView(name, "review-axis", (), status))
    operation_lanes.sort(
        key=lambda lane: (lane.status != "active", lane.lane_id)
    )
    lanes = (
        axis_lanes + operation_lanes
        if review_active
        else operation_lanes + axis_lanes
    )
    return tuple(lanes[:MAX_LANES]), max(len(lanes) - MAX_LANES, 0)


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


def _uncompiled_program(
    store: OperationStore,
    record: OperationRecord,
    lanes: tuple[LaneView, ...],
    surface: str,
    children: tuple[ChildView, ...],
    dropped_children: int,
    dropped_lanes: int,
    observed_at: float,
) -> ProgramView:
    """Project an operation that no compiled pipeline currently explains.

    An operation with no contract is not pipeline-bound at all — review and
    research programs are the ordinary case — so only a contract that exists
    and resolves to nothing is a real projection failure.
    """

    bound = bool(record.spec.contract_sha256)
    return ProgramView(
        operation_id=record.spec.operation_id,
        kind=record.spec.kind,
        state=record.state,
        revision=record.revision,
        pipeline="unresolved" if bound else "none",
        definition_sha256=record.spec.contract_sha256,
        controls=(),
        steps=(),
        lanes=lanes,
        next_action="unknown" if bound else "none",
        loop_passes=0,
        loop_limit=0,
        surface=surface,
        classification=_program_classification(
            record,
            surface=surface,
            next_action="none",
            pipeline_resolved=not bound,
        ),
        executor=_route_view(record),
        executor_status=_record_activity(record),
        children=children,
        dropped_children=dropped_children,
        dropped_lanes=dropped_lanes,
        timing=root_timing(store, record, observed_at),
        task_name=root_task_name(store, record) or UNKNOWN,
        self_healed_count=repair_receipt_count(store, record),
        current_stage=(
            "complete" if record.state == "complete" else record.state
        ),
        task_result=root_task_result(store, record),
    )


def _program(
    store: OperationStore,
    record: OperationRecord,
    members: list[OperationRecord],
    *,
    gate: Mapping[str, Any] | None,
    inventory: LiveInventory | None,
    tree: Mapping[str, list[OperationRecord]],
    observed_at: float,
) -> ProgramView:
    name, compiled = _compiled(store, record)
    runtime = _runtime_root(store, record)
    gate = _program_gate(gate, record)
    lanes, dropped_lanes = _lanes(members, gate)
    surface = _surface(record, inventory)
    dropped = {"children": 0}
    direct = _child_views(
        record.spec.operation_id,
        tree,
        store=store,
        observed_at=observed_at,
        current_ids=_current_review_ids(gate, tree),
        dropped=dropped,
    )
    by_step, loose = _bind_children(direct, compiled)
    if compiled is None:
        return _uncompiled_program(
            store,
            record,
            lanes,
            surface,
            loose,
            dropped["children"],
            dropped_lanes,
            observed_at,
        )
    program_timing = root_timing(store, record, observed_at)
    steps, next_action = _steps(
        store,
        record,
        compiled,
        runtime,
        gate,
        by_step,
        observed_at,
        root_interval=program_timing,
    )
    history = project_history(
        store,
        record,
        compiled,
        runtime,
        gate,
        members,
        tree,
        observed_at,
    )
    executor_status = _executor_status(record, steps)
    definition = compiled.definition
    self_healed = repair_receipt_count(store, record)
    active_phase = next(
        (phase for phase in history if phase.status in {"running", "attention"}),
        None,
    )
    phase_stage = (
        "Fixing review findings"
        if active_phase is not None and active_phase.kind == "fix"
        else "Re-verifying"
        if active_phase is not None and active_phase.kind == "reverify"
        else f"Review {active_phase.cycle}"
        if active_phase is not None
        else ""
    )
    current_stage = (
        "complete"
        if record.state == "complete"
        else phase_stage or next(
            (step.step_id for step in steps if step.status == "running"),
            next(
                (step.step_id for step in steps if step.status == "pending"),
                next_action or UNKNOWN,
            ),
        )
    )
    return ProgramView(
        operation_id=record.spec.operation_id,
        kind=record.spec.kind,
        state=record.state,
        revision=record.revision,
        pipeline=f"{name}@{definition.version}",
        definition_sha256=compiled.definition_sha256,
        controls=compiled.resolved_control_primitives,
        steps=steps,
        lanes=lanes,
        next_action=next_action,
        loop_passes=(
            max(
                (
                    view.visits
                    for view in steps
                    if view.primitive.split("@", 1)[0] == "model_step"
                ),
                default=0,
            )
            if _loop_limit(compiled)
            else 0
        ),
        loop_limit=_loop_limit(compiled),
        surface=surface,
        classification=_program_classification(
            record,
            surface=surface,
            next_action=next_action,
            pipeline_resolved=True,
            executor_status=executor_status,
        ),
        executor=_route_view(record),
        executor_status=executor_status,
        children=loose,
        dropped_children=dropped["children"],
        dropped_lanes=dropped_lanes,
        timing=program_timing,
        task_name=root_task_name(store, record) or UNKNOWN,
        self_healed_count=self_healed,
        current_stage=current_stage,
        task_result=root_task_result(store, record),
        history=history,
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
    observed_at: float,
) -> tuple[list[OperationRecord], tuple[ProgramView, ...]]:
    """Resolve every record to its one root program and project those trees.

    One dispatch is one program.  Verification children, review parents and
    review rounds are nested under the step that runs them, so an operation is
    only top-level when its durable lineage really has no owning root.
    """

    gate = read_gate(store, owner_id)
    by_id = {record.spec.operation_id: record for record in records}
    roots = {
        record.spec.operation_id: _root_id(record, by_id) for record in records
    }
    tree = _child_tree(records)
    controllers = [
        record
        for record in records
        if record.spec.kind in CONTROLLER_KINDS
        and roots[record.spec.operation_id] == record.spec.operation_id
    ]
    programs = tuple(
        _program(
            store,
            controller,
            [
                record
                for record in records
                if roots[record.spec.operation_id]
                == controller.spec.operation_id
            ],
            gate=gate,
            inventory=inventory,
            tree=tree,
            observed_at=observed_at,
        )
        for controller in controllers
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


def _bounded_projection(
    subject_id: str,
    programs: tuple[ProgramView, ...],
    issues: list[IssueView],
    surface_probe: str,
    observed_at: float | None,
    *,
    empty_classification: str = HEALTHY,
) -> DashboardProjection:
    """Deduplicate, bound, and classify one already-selected projection.

    Classification reads every observed issue, not just the displayed prefix:
    bounding the view must never bound the coordinator's signal.
    """

    bounded = _deduplicated(issues)
    classification = (
        empty_classification if not programs and not bounded else HEALTHY
    )
    for program in programs:
        classification = escalate(classification, program.classification)
    for issue in bounded:
        classification = escalate(classification, issue.classification)
    return DashboardProjection(
        owner_id=subject_id,
        classification=classification,
        surface_probe=surface_probe,
        programs=programs,
        issues=tuple(bounded[:MAX_ISSUES]),
        truncated={
            "programs": 0,
            "issues": max(len(bounded) - MAX_ISSUES, 0),
            "children": sum(program.dropped_children for program in programs),
            "lanes": sum(program.dropped_lanes for program in programs),
        },
        observed_at=observed_at,
    )


def project_root(
    store_root: Path | str,
    root_id: str,
    *,
    inventory: LiveInventory | None = None,
    surface_probe: str = "unavailable",
    observed_at: float | None = None,
) -> DashboardProjection:
    """Project one exact root and only its recorded descendants."""

    _identifier(root_id, "root operation id")
    if not absolute_path_is_safe(store_root):
        raise ValueError("dashboard store path contains a symlink")
    store = OperationStore(store_root)
    records, issues = _invalid_records(store, root_id)
    by_id = {record.spec.operation_id: record for record in records}
    roots = {
        record.spec.operation_id: _root_id(record, by_id) for record in records
    }
    members = [
        record for record in records if roots[record.spec.operation_id] == root_id
    ]
    _controllers, programs = _programs(
        store,
        members,
        root_id,
        inventory,
        float("nan") if observed_at is None else observed_at,
    )
    if root_id in by_id and not programs:
        issues.append(
            IssueView(
                "root-scope-not-a-root",
                root_id,
                "recorded identity does not own a root program",
                COORDINATOR,
            )
        )
    member_ids = {member.spec.operation_id for member in members} | {root_id}
    issues.extend(_program_issues(programs))
    issues.extend(
        issue
        for issue in _diagnostic_issues(store.root, root_id)
        if issue.operation_id in member_ids
    )
    # An empty scope is the observer opened before dispatch start: it is a
    # statement that nothing has happened yet, not that everything is healthy.
    return _bounded_projection(
        root_id,
        programs,
        issues,
        surface_probe,
        observed_at,
        empty_classification=WAITING,
    )


def project(
    store_root: Path | str,
    owner_id: str,
    *,
    inventory: LiveInventory | None = None,
    surface_probe: str = "unavailable",
    observed_at: float | None = None,
) -> DashboardProjection:
    """Project one owner's durable harness state as a read-only dashboard."""

    if not absolute_path_is_safe(store_root):
        raise ValueError("dashboard store path contains a symlink")
    store = OperationStore(store_root)
    records, issues = _invalid_records(store, owner_id)
    controllers, programs = _programs(
        store,
        records,
        owner_id,
        inventory,
        float("nan") if observed_at is None else observed_at,
    )
    issues.extend(_program_issues(programs))
    issues.extend(_diagnostic_issues(store.root, owner_id))
    return _bounded_projection(
        owner_id, programs, issues, surface_probe, observed_at
    )
