"""Exact bounded OperationStore lineage projection for the dashboard."""

from __future__ import annotations

from typing import Mapping

from review_contract import REVIEW_PARENT_KINDS

from .contracts import OperationRecord
from .dashboard_policy import (
    MAX_CHILDREN,
    MAX_DEPTH,
    ChildView,
    record_activity,
    route_view,
)
from .dashboard_receipts import liveness_timing
from .state_machine import TERMINAL
from .store import OperationStore


def parent_id(record: OperationRecord) -> str:
    """Return the exact durable parent identity, or empty for a root."""

    if record.spec.parent_operation_id:
        return record.spec.parent_operation_id
    if (
        record.spec.kind in REVIEW_PARENT_KINDS
        and record.spec.owner_id != record.spec.operation_id
    ):
        return record.spec.owner_id
    return ""


def root_id(
    record: OperationRecord,
    by_id: Mapping[str, OperationRecord],
) -> str:
    """Walk one exact lineage without crossing a missing or foreign root."""

    declared = record.spec.root_operation_id
    current = record
    seen = {current.spec.operation_id}
    while True:
        if declared and current.spec.operation_id == declared:
            return declared
        parent = by_id.get(parent_id(current))
        if declared and (
            parent is None
            or parent.spec.root_operation_id not in {"", declared}
        ):
            return f"invalid-lineage:{record.spec.operation_id}"
        if parent is None or parent.spec.operation_id in seen:
            return current.spec.operation_id
        seen.add(parent.spec.operation_id)
        current = parent


def child_views(
    parent: str,
    tree: Mapping[str, list[OperationRecord]],
    depth: int = 0,
    *,
    store: OperationStore,
    observed_at: float,
    current_ids: frozenset[str] = frozenset(),
    dropped: dict[str, int] | None = None,
) -> tuple[ChildView, ...]:
    if depth >= MAX_DEPTH:
        if dropped is not None:
            dropped["children"] += len(tree.get(parent, ()))
        return ()
    records = sorted(
        tree.get(parent, ()),
        key=lambda record: (
            record.spec.operation_id not in current_ids,
            record.state in TERMINAL,
            record.spec.operation_id,
        ),
    )
    selected = records[:MAX_CHILDREN]
    if dropped is not None:
        dropped["children"] += len(records) - len(selected)
    return tuple(
        ChildView(
            record.spec.operation_id,
            record.spec.kind,
            record.state,
            record_activity(record),
            route_view(record),
            child_views(
                record.spec.operation_id,
                tree,
                depth + 1,
                store=store,
                observed_at=observed_at,
                current_ids=current_ids,
                dropped=dropped,
            ),
            liveness_timing(store, record, observed_at),
        )
        for record in selected
    )


def child_tree(
    records: list[OperationRecord],
) -> dict[str, list[OperationRecord]]:
    by_id = {record.spec.operation_id: record for record in records}
    tree: dict[str, list[OperationRecord]] = {}
    for record in sorted(records, key=lambda item: item.spec.operation_id):
        parent = parent_id(record)
        if parent in by_id and parent != record.spec.operation_id:
            tree.setdefault(parent, []).append(record)
    return tree


def current_review_ids(
    gate: Mapping[str, object] | None,
    tree: Mapping[str, list[OperationRecord]],
) -> frozenset[str]:
    """Return the selected review parent and every live descendant."""

    active = str(gate.get("active_review_operation_id") or "") if gate else ""
    if not active:
        return frozenset()
    current = {active}
    pending = [active]
    while pending:
        parent = pending.pop()
        for child in tree.get(parent, ()):
            if child.state in TERMINAL:
                continue
            operation_id = child.spec.operation_id
            if operation_id not in current:
                current.add(operation_id)
                pending.append(operation_id)
    return frozenset(current)
