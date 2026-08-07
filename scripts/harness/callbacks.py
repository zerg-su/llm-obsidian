"""Store-backed, operation-scoped one-shot callback acceptance."""

from __future__ import annotations

from dataclasses import dataclass
from time import time

from .contracts import AttentionReason, CallbackEnvelope
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

    def accept(
        self,
        envelope: CallbackEnvelope,
        *,
        deadline_operation_id: str = "",
    ) -> CallbackAcceptance:
        try:
            record = self.store.read(self.owner_id, envelope.operation_id)
        except StoreError as exc:
            raise CallbackError("callback belongs to an unknown operation") from exc
        deadline_record = record
        if deadline_operation_id and envelope.kind == "review":
            try:
                deadline_record = self.store.read(
                    self.owner_id, deadline_operation_id
                )
            except StoreError as exc:
                raise CallbackError("callback deadline owner is unknown") from exc
        next_state = self._next_state(envelope)
        reason = (
            AttentionReason.ATTENTION_REQUIRED
            if next_state == "attention-required"
            else None
        )
        try:
            updated, accepted, timed_out = self.store.accept_callback(
                self.owner_id,
                envelope,
                expected_revision=record.revision,
                next_state=next_state,
                reason=reason,
                deadline_operation_id=deadline_operation_id,
                enforce_deadline=(
                    envelope.kind == "review"
                    and deadline_record.spec.route.profile in REVIEWER_PROFILES
                ),
                now=time(),
            )
        except StoreError as exc:
            raise CallbackError(str(exc)) from exc
        if timed_out:
            raise CallbackTimeoutError(AttentionReason.CALLBACK_TIMEOUT.value)
        return CallbackAcceptance(accepted, not accepted, updated.state)
