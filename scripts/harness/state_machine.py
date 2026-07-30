"""Legal operation transitions and write-ahead effect semantics."""

from __future__ import annotations

from dataclasses import replace

from .contracts import (
    AttentionReason,
    ContractError,
    EffectOutcome,
    OperationRecord,
    TransitionResult,
)


TERMINAL = frozenset({"complete", "failed", "cancelled"})
TRANSITIONS = {
    "created": {"preflight", "cancelling", "attention-required", "failed"},
    "preflight": {"starting", "cancelling", "attention-required", "failed"},
    "starting": {"running", "cancelling", "attention-required", "failed"},
    "running": {"awaiting-callback", "verifying", "finalizing", "cancelling", "attention-required", "failed"},
    "awaiting-callback": {"running", "verifying", "finalizing", "cancelling", "attention-required", "failed"},
    "verifying": {"running", "finalizing", "cancelling", "attention-required", "failed"},
    "finalizing": {"exiting", "cancelling", "attention-required", "failed"},
    "cancelling": {"exiting", "attention-required", "failed"},
    "exiting": {"complete", "cancelled", "attention-required", "failed"},
    "attention-required": {
        "created",
        "preflight",
        "starting",
        "running",
        "awaiting-callback",
        "verifying",
        "finalizing",
        "cancelling",
        "exiting",
        "failed",
    },
    "complete": set(),
    "failed": set(),
    "cancelled": set(),
}


def transition(
    record: OperationRecord,
    state: str,
    *,
    reason: AttentionReason | None = None,
) -> tuple[OperationRecord, TransitionResult]:
    if state == record.state:
        return record, TransitionResult(
            record.spec.operation_id, record.state, state, record.revision, False, record.attention_reason
        )
    if record.pending_effect and state != "attention-required":
        raise ContractError("unresolved external effect blocks state transition")
    if state not in TRANSITIONS.get(record.state, set()):
        raise ContractError(f"illegal transition: {record.state} -> {state}")
    if state == "attention-required" and reason is None:
        raise ContractError("attention-required transition needs a reason")
    if state != "attention-required" and reason is not None:
        raise ContractError("attention reason is only valid for attention-required")
    updated = replace(
        record,
        state=state,
        revision=record.revision + 1,
        attention_reason=reason,
        resume_state=(
            record.state
            if state == "attention-required"
            else ""
        ),
    )
    return updated, TransitionResult(
        record.spec.operation_id, record.state, state, updated.revision, True, reason
    )


def begin_effect(record: OperationRecord, effect: str) -> OperationRecord:
    if record.state in TERMINAL:
        raise ContractError("terminal operation cannot begin an effect")
    if record.pending_effect:
        if record.pending_effect == effect:
            return record
        raise ContractError("an external effect is already pending reconciliation")
    if not effect:
        raise ContractError("effect identifier is required")
    if (
        record.effect_id == effect
        and record.effect_outcome in {EffectOutcome.SUCCEEDED, EffectOutcome.FAILED}
    ):
        return record
    return replace(
        record,
        pending_effect=effect,
        effect_id=effect,
        effect_outcome=EffectOutcome.PENDING,
        revision=record.revision + 1,
    )


def resolve_effect(record: OperationRecord, outcome: EffectOutcome) -> OperationRecord:
    if outcome not in {EffectOutcome.SUCCEEDED, EffectOutcome.FAILED}:
        raise ContractError("effect resolution must be succeeded or failed")
    if not record.pending_effect:
        if record.effect_outcome == outcome:
            return record
        raise ContractError("no external effect is awaiting resolution")
    return replace(
        record,
        pending_effect="",
        effect_outcome=outcome,
        revision=record.revision + 1,
    )
