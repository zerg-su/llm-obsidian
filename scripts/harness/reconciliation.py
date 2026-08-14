"""Deterministic owned-resource reconciliation and cleanup ordering."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import AttentionReason, OperationRecord


@dataclass(frozen=True)
class ReconcileDecision:
    action: str
    reason: AttentionReason | None = None


@dataclass(frozen=True)
class AcceptedCallbackOwnership:
    applicable: bool
    process_status: str = "unknown"
    supervisor_status: str = "unknown"


def prove_accepted_callback_ownership(
    record: OperationRecord,
    process_adapter: object,
) -> AcceptedCallbackOwnership:
    """Prove signal-less cleanup ownership from one durable callback receipt."""

    if (
        record.state not in {"finalizing", "exiting"}
        or not record.accepted_callback_id
        or not record.accepted_callback_kind
        or not record.accepted_callback_sha256
    ):
        return AcceptedCallbackOwnership(False)
    resources = record.resources
    if (
        resources.process_group <= 1
        or resources.supervisor_pid <= 1
        or not resources.process_identity
        or not resources.supervisor_identity
    ):
        return AcceptedCallbackOwnership(True)
    try:
        process_status = str(
            process_adapter.process_status(
                resources.process_group,
                resources.process_identity,
            )
        )
        supervisor_probe = getattr(process_adapter, "pid_status", None)
        if not callable(supervisor_probe):
            return AcceptedCallbackOwnership(True)
        supervisor_status = str(
            supervisor_probe(
                resources.supervisor_pid,
                resources.supervisor_identity,
            )
        )
    except Exception:
        return AcceptedCallbackOwnership(True)
    if process_status not in {"alive", "dead", "unknown"}:
        process_status = "unknown"
    if supervisor_status not in {"alive", "dead", "unknown"}:
        supervisor_status = "unknown"
    if "unknown" not in {process_status, supervisor_status}:
        return AcceptedCallbackOwnership(
            True,
            process_status,
            supervisor_status,
        )
    capture = getattr(process_adapter, "capture_identity", None)
    if not callable(capture):
        return AcceptedCallbackOwnership(
            True,
            process_status,
            supervisor_status,
        )
    if process_status == "unknown":
        try:
            identity = capture(
                resources.process_group,
                process_group=resources.process_group,
            )
        except Exception:
            return AcceptedCallbackOwnership(
                True,
                process_status,
                supervisor_status,
            )
        if identity == resources.process_identity:
            process_status = "alive"
    if supervisor_status == "unknown":
        try:
            identity = capture(resources.supervisor_pid)
        except Exception:
            return AcceptedCallbackOwnership(
                True,
                process_status,
                supervisor_status,
            )
        if identity == resources.supervisor_identity:
            supervisor_status = "alive"
    return AcceptedCallbackOwnership(
        True,
        process_status,
        supervisor_status,
    )


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
) -> ReconcileDecision:
    resources = record.resources
    if resources.process_group <= 1 or not resources.surface_id:
        return ReconcileDecision("none", AttentionReason.ATTENTION_REQUIRED)
    process = process_adapter.process_status(
        resources.process_group,
        resources.process_identity,
    )
    try:
        surface = cmux_adapter.status(resources.surface_id)
    except Exception:
        surface = "unknown"
    decision = decide(process, surface)
    if decision.action == "close-exact":
        cmux_adapter.close_exact(resources.surface_id)
    return decision
