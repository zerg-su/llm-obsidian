"""Fail-closed replay proof for a pending review gate."""

from __future__ import annotations

from harness.contracts import AttentionReason
from harness.state_machine import TERMINAL
from harness.store import OperationStore, StoreError
from harness.workflows.review import (
    ReviewOperationRequest,
    review_session_specs,
    runtime_status_is_live,
)
from harness.workflows.review_gate import ReviewGateController
from task_review_shared import TaskReviewError


def _pending_gate_replay(
    gate: ReviewGateController, store: OperationStore, task_id: str
) -> bool:
    """Resume only an unbound zero-lane gate owned by its live dispatch."""
    if not gate.state_path.exists():
        return False
    initial_state = gate.read()
    if (
        initial_state.get("status") == "attention-required"
        and initial_state.get("lanes") == []
    ):
        try:
            dispatch_record = store.read(task_id, task_id)
        except StoreError:
            dispatch_record = None
        if dispatch_record is not None and dispatch_record.state not in {
            "attention-required",
            *TERMINAL,
        }:
            gate.resume_unbound_attention()
            initial_state = gate.read()
    pending = initial_state.get("status") == "pending"
    if pending and initial_state.get("lanes") != []:
        raise TaskReviewError("pending review gate already owns lanes")
    return pending


def _pending_replay_is_safe(
    request: ReviewOperationRequest,
    store: OperationStore,
    gate: ReviewGateController,
    runtime: object,
) -> bool:
    for identity in review_session_specs(request):
        record = None
        safe = False
        for _attempt in range(2):
            try:
                record = store.read(
                    request.owner_id, identity.spec.operation_id
                )
            except StoreError:
                safe = True
                break
            resources = record.resources
            clean_created = (
                record.state == "created"
                and not record.pending_effect
                and not any(
                    (
                        resources.surface_id,
                        resources.process_group,
                        resources.supervisor_pid,
                        resources.process_identity,
                        resources.supervisor_identity,
                    )
                )
            )
            if clean_created:
                safe = True
                break
            if record.state not in {
                "running",
                "awaiting-callback",
                "verifying",
            }:
                break
            try:
                observed = runtime.status(
                    request.owner_id, identity.spec.operation_id
                )
                latest = store.read(
                    request.owner_id, identity.spec.operation_id
                )
            except Exception:
                continue
            observed_record = getattr(observed, "record", None)
            observed_resources = getattr(
                observed_record, "resources", None
            )
            if (
                observed_record == latest
                and observed_resources is not None
                and bool(observed_resources.surface_id)
                and observed_resources.process_group > 1
                and observed_resources.supervisor_pid > 1
                and bool(observed_resources.process_identity)
                and bool(observed_resources.supervisor_identity)
                and runtime_status_is_live(observed)
            ):
                safe = True
                break
        if safe:
            continue
        try:
            record = store.read(
                request.owner_id, identity.spec.operation_id
            )
        except StoreError:
            continue
        if (
            record.state not in TERMINAL
            and record.state != "attention-required"
        ):
            store.transition(
                request.owner_id,
                record.spec.operation_id,
                "attention-required",
                reason=AttentionReason.ATTENTION_REQUIRED,
            )
        gate.mark_pending_attention()
        return False
    return True
