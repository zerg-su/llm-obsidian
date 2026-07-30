"""Store-backed, operation-scoped one-shot callback acceptance."""

from __future__ import annotations

from dataclasses import dataclass, replace
from time import time

from .contracts import AttentionReason, CallbackEnvelope, OperationRecord
from .state_machine import TERMINAL, transition
from .store import OperationStore, StoreError


REVIEWER_PROFILES = frozenset(
    {"reviewer-readonly", "reviewer-callback"}
)


class CallbackError(RuntimeError):
    pass


class CallbackTimeoutError(CallbackError):
    pass


@dataclass(frozen=True)
class CallbackAcceptance:
    accepted: bool
    duplicate: bool
    next_state: str


class CallbackBroker:
    def __init__(self, store: OperationStore, owner_id: str):
        self.store = store
        self.owner_id = owner_id

    @staticmethod
    def _next_state(envelope: CallbackEnvelope) -> str:
        if envelope.kind == "review":
            verdict = envelope.payload.get("verdict")
            if verdict == "approve":
                return "finalizing"
            if verdict == "changes-requested":
                return "verifying"
            if verdict == "blocked":
                return "attention-required"
            raise CallbackError("unknown review verdict")
        if envelope.kind in {"result", "wiki-summary", "research"}:
            return "finalizing"
        raise CallbackError("unknown callback kind")

    @staticmethod
    def _matches_receipt(envelope: CallbackEnvelope, record: OperationRecord) -> bool:
        return (
            record.accepted_callback_id == envelope.callback_id
            and record.accepted_callback_kind == envelope.kind
            and record.accepted_callback_sha256 == envelope.payload_sha256
        )

    def accept(
        self,
        envelope: CallbackEnvelope,
        *,
        deadline_operation_id: str = "",
    ) -> CallbackAcceptance:
        with self.store.locked(self.owner_id):
            try:
                record = self.store.read(self.owner_id, envelope.operation_id)
            except StoreError as exc:
                raise CallbackError("callback belongs to an unknown operation") from exc

            if envelope.operation_id != record.spec.operation_id:
                raise CallbackError("callback belongs to a different operation")
            if envelope.run_id != record.run_id:
                raise CallbackError("callback belongs to a different run")

            if record.accepted_callback_id:
                if not self._matches_receipt(envelope, record):
                    raise CallbackError("operation already accepted a different callback")
                return CallbackAcceptance(False, True, record.state)

            if record.state in TERMINAL:
                raise CallbackError("terminal operation cannot accept a callback")
            deadline_record = record
            if deadline_operation_id and envelope.kind == "review":
                try:
                    deadline_record = self.store.read(
                        self.owner_id, deadline_operation_id
                    )
                except StoreError as exc:
                    raise CallbackError(
                        "callback deadline owner is unknown"
                    ) from exc
                if (
                    deadline_record.lane_id != record.lane_id
                    or (
                        deadline_operation_id != envelope.operation_id
                        and envelope.payload.get(
                            "parent_session_operation_id"
                        )
                        != deadline_operation_id
                    )
                ):
                    raise CallbackError(
                        "callback deadline owner mismatches its parent lane"
                    )
            if (
                envelope.kind == "review"
                and deadline_record.spec.route.profile
                in REVIEWER_PROFILES
                and deadline_record.state not in TERMINAL
            ):
                timed_out = (
                    deadline_record.state == "attention-required"
                    and deadline_record.attention_reason
                    == AttentionReason.CALLBACK_TIMEOUT
                )
                if (
                    not timed_out
                    and deadline_record.deadline_at
                    and time() >= deadline_record.deadline_at
                ):
                    updated, _ = transition(
                        deadline_record,
                        "attention-required",
                        reason=AttentionReason.CALLBACK_TIMEOUT,
                    )
                    self.store._save_locked(
                        updated,
                        expected_revision=deadline_record.revision,
                    )
                    timed_out = True
                if timed_out:
                    raise CallbackTimeoutError(
                        AttentionReason.CALLBACK_TIMEOUT.value
                    )
            if record.state != "awaiting-callback":
                raise CallbackError("operation is not awaiting a callback")

            next_state = self._next_state(envelope)
            reason = (
                AttentionReason.ATTENTION_REQUIRED
                if next_state == "attention-required"
                else None
            )
            updated, result = transition(record, next_state, reason=reason)
            updated = replace(
                updated,
                accepted_callback_id=envelope.callback_id,
                accepted_callback_kind=envelope.kind,
                accepted_callback_sha256=envelope.payload_sha256,
            )
            self.store._save_locked(updated, expected_revision=record.revision)
            return CallbackAcceptance(True, False, result.state)
