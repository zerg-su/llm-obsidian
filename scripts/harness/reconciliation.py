"""Deterministic owned-resource reconciliation and cleanup ordering."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import AttentionReason, OperationRecord


@dataclass(frozen=True)
class ReconcileDecision:
    action: str
    reason: AttentionReason | None = None


def decide(process: str, surface: str) -> ReconcileDecision:
    if process == "alive" and surface == "alive":
        return ReconcileDecision("continue")
    if process == "dead" and surface == "alive":
        return ReconcileDecision("close-exact")
    if process == "alive" and surface == "missing":
        return ReconcileDecision("none", AttentionReason.PROCESS_ORPHANED)
    if process == "dead" and surface == "missing":
        return ReconcileDecision("complete")
    return ReconcileDecision("none", AttentionReason.ATTENTION_REQUIRED)


def reconcile(
    record: OperationRecord,
    process_adapter: object,
    cmux_adapter: object,
    *,
    workspace: tuple[str, str] | None = None,
) -> ReconcileDecision:
    resources = record.resources
    if resources.process_group <= 1 or not resources.surface_id:
        return ReconcileDecision("none", AttentionReason.ATTENTION_REQUIRED)
    process = process_adapter.process_status(
        resources.process_group,
        resources.process_identity,
    )
    try:
        surface = (
            cmux_adapter.workspace_status(*workspace)
            if workspace is not None
            else cmux_adapter.status(resources.surface_id)
        )
    except Exception:
        surface = "unknown"
    decision = decide(process, surface)
    if decision.action == "close-exact":
        if workspace is not None:
            cmux_adapter.close_workspace_exact(*workspace)
        else:
            cmux_adapter.close_exact(resources.surface_id)
    return decision
