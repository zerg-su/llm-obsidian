"""Read-only projection of one root's durable review correction history."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from .contracts import OperationRecord
from .dashboard_policy import (
    MAX_CHILDREN,
    MAX_DEPTH,
    UNKNOWN,
    UNKNOWN_ROUTE,
    UNKNOWN_TIMING,
    ChildView,
    LifecyclePhaseView,
    current_verification_ids,
    record_activity,
    route_view,
)
from .dashboard_receipts import (
    liveness_timing,
    review_attempt_history,
    review_summary,
    verification_receipt_timing,
    verification_receipt_visits,
)
from .pipelines import CompiledPipeline
from .store import OperationStore


def _child_view(
    record: OperationRecord,
    tree: Mapping[str, list[OperationRecord]],
    *,
    store: OperationStore,
    observed_at: float,
    depth: int = 0,
) -> ChildView:
    children = ()
    if depth < MAX_DEPTH:
        children = tuple(
            _child_view(
                child,
                tree,
                store=store,
                observed_at=observed_at,
                depth=depth + 1,
            )
            for child in sorted(
                tree.get(record.spec.operation_id, ()),
                key=lambda item: item.spec.operation_id,
            )[:MAX_CHILDREN]
        )
    return ChildView(
        record.spec.operation_id,
        record.spec.kind,
        record.state,
        record_activity(record),
        route_view(record),
        children,
        liveness_timing(store, record, observed_at),
    )


def _phase_timing(children: tuple[ChildView, ...]) -> object:
    elapsed = tuple(
        child.timing for child in children if child.timing.mode == "elapsed"
    )
    if elapsed:
        return max(elapsed, key=lambda item: item.seconds or 0)
    durations = tuple(
        child.timing for child in children if child.timing.mode == "duration"
    )
    return (
        max(durations, key=lambda item: item.seconds or 0)
        if durations
        else UNKNOWN_TIMING
    )


def _review_status(attempt: object) -> str:
    status = getattr(attempt, "status", "")
    if status != "terminal":
        return "pending" if status == "pending" else "running"
    terminal = getattr(attempt, "terminal", None)
    result = str(getattr(getattr(terminal, "result", None), "value", ""))
    return (
        "complete"
        if result in {"approved", "changes-requested"}
        else "attention"
    )


def _reviewers(
    identity: object,
    by_id: Mapping[str, OperationRecord],
    tree: Mapping[str, list[OperationRecord]],
    *,
    store: OperationStore,
    observed_at: float,
) -> tuple[ChildView, ...]:
    return tuple(
        _child_view(
            by_id[lane.operation_id],
            tree,
            store=store,
            observed_at=observed_at,
        )
        for lane in identity.lanes
        if lane.operation_id in by_id
    )


def _verification_phase(
    *,
    cycle: int,
    store: OperationStore,
    record: OperationRecord,
    runtime: Path,
    verification_gate: Mapping[str, Any],
    verification_head: str,
    verify_step: object | None,
    by_id: Mapping[str, OperationRecord],
    tree: Mapping[str, list[OperationRecord]],
    observed_at: float,
    active: bool,
) -> LifecyclePhaseView:
    expected_ids = (
        current_verification_ids(record, verify_step, verification_gate)
        if verify_step is not None
        else frozenset()
    )
    children = tuple(
        _child_view(
            by_id[operation_id],
            tree,
            store=store,
            observed_at=observed_at,
        )
        for operation_id in sorted(expected_ids)
        if operation_id in by_id
    )
    _visits, issue = verification_receipt_visits(
        store, record, runtime, exact_head_sha=verification_head
    )
    timing = verification_receipt_timing(
        store,
        record,
        runtime,
        observed_at,
        exact_head_sha=verification_head,
    )
    if len(children) == 1 and timing.mode != UNKNOWN:
        children = (replace(children[0], timing=timing),)
    active = active and any(
        child.state not in {"complete", "failed", "cancelled"}
        for child in children
    )
    if active:
        status = "running"
    elif issue:
        status = "attention"
    elif children and all(
        child.status in {"complete", "stopped"} for child in children
    ):
        status = "complete"
    else:
        status = "unknown"
    return LifecyclePhaseView(
        "reverify",
        cycle,
        status,
        operation_id=children[0].operation_id if len(children) == 1 else "",
        route=children[0].route if len(children) == 1 else UNKNOWN_ROUTE,
        children=children,
        timing=timing,
    )


def project_history(
    store: OperationStore,
    record: OperationRecord,
    compiled: CompiledPipeline,
    runtime: Path,
    gate: Mapping[str, Any] | None,
    members: list[OperationRecord],
    tree: Mapping[str, list[OperationRecord]],
    observed_at: float,
) -> tuple[LifecyclePhaseView, ...]:
    """Project the immutable exact-HEAD correction sequence for one root."""

    snapshots = review_attempt_history(store, record, gate)
    if not snapshots:
        return ()
    by_id = {member.spec.operation_id: member for member in members}
    verify_step = next(
        (step for step in compiled.definition.steps if step.primitive_id == "verify"),
        None,
    )
    review_limit = max(
        item.total_pass_limit for item in compiled.definition.completion_policies
    )
    phases: list[LifecyclePhaseView] = []
    for index, (cycle_gate, attempt) in enumerate(snapshots):
        identity = attempt.identity
        reviewers = _reviewers(
            identity,
            by_id,
            tree,
            store=store,
            observed_at=observed_at,
        )
        phases.append(
            LifecyclePhaseView(
                "review",
                identity.cycle,
                _review_status(attempt),
                operation_id=identity.attempt_id,
                route=reviewers[0].route if len(reviewers) == 1 else UNKNOWN_ROUTE,
                children=reviewers,
                timing=_phase_timing(reviewers),
                review=review_summary(
                    store, record, cycle_gate, limit=review_limit
                ),
            )
        )
        result = str(
            getattr(getattr(attempt.terminal, "result", None), "value", "")
        )
        if result != "changes-requested":
            continue
        following = snapshots[index + 1] if index + 1 < len(snapshots) else None
        gate_status = str(cycle_gate.get("status") or "")
        fixing = following is None and gate_status in {
            "changes-requested",
            "awaiting-resolution",
        }
        reverifying = following is None and gate_status in {
            "verifying",
            "recovery-verification-required",
            "fresh-boundary-authorized",
        }
        if following is None and not (fixing or reverifying):
            continue
        phases.append(
            LifecyclePhaseView(
                "fix",
                identity.cycle,
                "running" if fixing else "complete",
                operation_id=record.spec.operation_id,
                route=route_view(record),
            )
        )
        if fixing:
            continue
        verification_gate = following[0] if following is not None else cycle_gate
        verification_head = (
            following[1].identity.exact_head_sha
            if following is not None
            else str(
                (cycle_gate.get("context") or {}).get("head_sha") or ""
            )
        )
        phases.append(
            _verification_phase(
                cycle=identity.cycle,
                store=store,
                record=record,
                runtime=runtime,
                verification_gate=verification_gate,
                verification_head=verification_head,
                verify_step=verify_step,
                by_id=by_id,
                tree=tree,
                observed_at=observed_at,
                active=reverifying,
            )
        )
    return tuple(phases)
