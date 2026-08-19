"""Per-operation supervision policy, retry budgets, and callback liveness."""

from __future__ import annotations

from dataclasses import dataclass, replace
from time import monotonic, time
from typing import Callable, Generic, TypeVar

from .contracts import (
    AttentionReason,
    EffectOutcome,
    OperationRecord,
    OwnedResources,
)
from .store import OperationStore
from .state_machine import resolve_effect as apply_effect_resolution
from .state_machine import transition as apply_transition


T = TypeVar("T")


class SupervisorError(RuntimeError):
    """A durable lifecycle boundary cannot be advanced safely."""


@dataclass(frozen=True)
class EffectResult(Generic[T]):
    record: OperationRecord
    value: T | None
    replayed: bool = False


class OperationSupervisor:
    """Drive one stored operation without hiding uncertain external effects."""

    def __init__(self, store: OperationStore, owner_id: str, operation_id: str):
        self.store = store
        self.owner_id = owner_id
        self.operation_id = operation_id

    def read(self) -> OperationRecord:
        return self.store.read(self.owner_id, self.operation_id)

    def transition(self, state: str) -> OperationRecord:
        self.store.transition(self.owner_id, self.operation_id, state)
        return self.read()

    def bind_resources(self, resources: OwnedResources) -> OperationRecord:
        current = self.read()
        if current.resources == resources:
            return current
        updated = replace(
            current,
            resources=resources,
            revision=current.revision + 1,
        )
        self.store.save(updated, expected_revision=current.revision)
        return updated

    def retire_authorized_pending_effect(
        self,
        expected: OperationRecord,
        outcome: EffectOutcome,
    ) -> OperationRecord:
        """Atomically absorb an authorized stale effect and cancel its owner."""

        current = self.read()
        if (
            current != expected
            or current.spec.owner_id != self.owner_id
            or current.spec.operation_id != self.operation_id
            or not current.pending_effect
            or current.effect_outcome != EffectOutcome.PENDING
            or current.accepted_callback_id
            or outcome not in {EffectOutcome.SUCCEEDED, EffectOutcome.FAILED}
        ):
            raise SupervisorError(
                "pending-effect retirement identity changed"
            )
        updated = apply_effect_resolution(current, outcome)
        if updated.state == "attention-required":
            if not updated.resume_state:
                raise SupervisorError(
                    "pending-effect retirement has no exact resume state"
                )
            updated, _ = apply_transition(updated, updated.resume_state)
        updated, _ = apply_transition(updated, "cancelling")
        updated, _ = apply_transition(updated, "exiting")
        updated, _ = apply_transition(updated, "cancelled")
        updated = replace(
            updated,
            resources=OwnedResources(),
            revision=updated.revision + 1,
        )
        self.store.save(updated, expected_revision=current.revision)
        return updated

    def retire_proven_absent_resources(
        self, expected_resources: OwnedResources
    ) -> OperationRecord:
        """Atomically terminalize one exact reviewer after absence proof."""

        current = self.read()
        if (
            current.spec.route.profile != "reviewer-callback"
            or expected_resources == OwnedResources()
            or current.resources != expected_resources
            or current.pending_effect
            or current.state
            not in {
                "awaiting-callback",
                "attention-required",
                "finalizing",
                "cancelling",
                "exiting",
            }
        ):
            raise SupervisorError(
                "signal-free retirement parent identity changed"
            )
        updated = current
        if updated.state not in {"finalizing", "cancelling", "exiting"}:
            updated, _ = apply_transition(updated, "cancelling")
        if updated.state in {"finalizing", "cancelling"}:
            updated, _ = apply_transition(updated, "exiting")
        updated, _ = apply_transition(updated, "complete")
        updated = replace(
            updated,
            resources=OwnedResources(),
            revision=updated.revision + 1,
        )
        self.store.save(updated, expected_revision=current.revision)
        return updated

    def complete_round_after_supported_close(
        self,
        parent: OperationRecord,
        *,
        parent_run_id: str,
        round_run_id: str,
        accepted_callback_id: str,
        accepted_callback_sha256: str,
    ) -> OperationRecord:
        """Atomically terminalize one retained round after an exact close receipt."""

        current = self.read()
        parent_callback = (
            parent.accepted_callback_id,
            parent.accepted_callback_kind,
            parent.accepted_callback_sha256,
        )
        if (
            parent.spec.owner_id != self.owner_id
            or parent.spec.operation_id != current.spec.parent_operation_id
            or parent.spec.route.profile != "reviewer-callback"
            or parent.run_id != parent_run_id
            or parent.lane_id != current.lane_id
            or parent.state != "cancelled"
            or parent.resources != OwnedResources()
            or parent.pending_effect
            or parent.effect_id != "request-exit"
            or parent.effect_outcome != EffectOutcome.SUCCEEDED
            or any(parent_callback)
            or current.spec.owner_id != self.owner_id
            or current.spec.operation_id != self.operation_id
            or current.spec.kind != "review-round"
            or current.spec.route.profile != "reviewer-callback"
            or current.run_id != round_run_id
            or current.resources != OwnedResources()
            or current.pending_effect
            or current.effect_outcome != EffectOutcome.NONE
            or current.effect_id
            or current.accepted_callback_kind != "review"
            or current.accepted_callback_id != accepted_callback_id
            or current.accepted_callback_sha256 != accepted_callback_sha256
            or current.state not in {"verifying", "complete"}
        ):
            raise SupervisorError(
                "supported-close retained round identity changed"
            )
        if current.state == "complete":
            return current
        updated = current
        for state in ("finalizing", "exiting", "complete"):
            updated, _ = apply_transition(updated, state)
        self.store.save(updated, expected_revision=current.revision)
        return updated

    def configure_budget(
        self,
        *,
        attempt_limit: int,
        model_restart_limit: int,
        time_budget_seconds: float,
        token_limit: int,
        now: float | None = None,
    ) -> OperationRecord:
        """Persist one immutable budget envelope before runtime effects."""

        if (
            type(attempt_limit) is not int
            or attempt_limit < 1
            or type(model_restart_limit) is not int
            or model_restart_limit < 0
            or not isinstance(time_budget_seconds, (int, float))
            or isinstance(time_budget_seconds, bool)
            or time_budget_seconds <= 0
            or type(token_limit) is not int
            or token_limit < 1
        ):
            raise SupervisorError("operation budget is invalid")
        current = self.read()
        if current.deadline_at:
            if (
                current.attempt_limit != attempt_limit
                or current.model_restart_limit != model_restart_limit
                or current.token_limit != token_limit
            ):
                raise SupervisorError("persisted operation budget is immutable")
            return current
        started = time() if now is None else float(now)
        updated = replace(
            current,
            attempt_limit=attempt_limit,
            model_restart_limit=model_restart_limit,
            deadline_at=started + float(time_budget_seconds),
            token_limit=token_limit,
            revision=current.revision + 1,
        )
        self.store.save(updated, expected_revision=current.revision)
        return updated

    def _exhaust(
        self,
        current: OperationRecord,
        reason: AttentionReason = AttentionReason.RETRY_EXHAUSTED,
    ) -> None:
        if current.state != "attention-required":
            self.store.transition(
                self.owner_id,
                self.operation_id,
                "attention-required",
                reason=reason,
            )
        raise SupervisorError(reason.value)

    def _check_time(
        self,
        current: OperationRecord,
        now: float | None,
        reason: AttentionReason = AttentionReason.RETRY_EXHAUSTED,
    ) -> None:
        observed = time() if now is None else float(now)
        if current.deadline_at and observed >= current.deadline_at:
            self._exhaust(current, reason)

    def consume_attempt(
        self, *, tokens: int = 0, now: float | None = None
    ) -> OperationRecord:
        """Atomically consume one persisted attempt and its observed tokens."""

        if type(tokens) is not int or tokens < 0:
            raise SupervisorError("token usage must be a non-negative integer")
        current = self.read()
        self._check_time(current, now)
        if (
            current.attempt >= current.attempt_limit
            or current.tokens_used + tokens > current.token_limit
        ):
            self._exhaust(current)
        updated = replace(
            current,
            attempt=current.attempt + 1,
            tokens_used=current.tokens_used + tokens,
            revision=current.revision + 1,
        )
        self.store.save(updated, expected_revision=current.revision)
        return updated

    def begin_continuation(
        self,
        *,
        time_budget_seconds: float,
        now: float | None = None,
    ) -> OperationRecord:
        """Start the next bounded attempt in one already-owned live session."""

        if (
            not isinstance(time_budget_seconds, (int, float))
            or isinstance(time_budget_seconds, bool)
            or time_budget_seconds <= 0
        ):
            raise SupervisorError("continuation time budget is invalid")
        current = self.read()
        if current.state not in {"running", "awaiting-callback", "verifying"}:
            raise SupervisorError(
                "operation cannot begin a continuation from its current state"
            )
        if current.attempt >= current.attempt_limit:
            self._exhaust(current)
        started = time() if now is None else float(now)
        updated = replace(
            current,
            attempt=current.attempt + 1,
            deadline_at=started + float(time_budget_seconds),
            revision=current.revision + 1,
        )
        self.store.save(updated, expected_revision=current.revision)
        return updated

    def consume_tokens(
        self, tokens: int, *, now: float | None = None
    ) -> OperationRecord:
        """Persist provider token accounting without consuming an attempt."""

        if type(tokens) is not int or tokens < 0:
            raise SupervisorError("token usage must be a non-negative integer")
        current = self.read()
        self._check_time(current, now)
        if current.tokens_used + tokens > current.token_limit:
            self._exhaust(current)
        updated = replace(
            current,
            tokens_used=current.tokens_used + tokens,
            revision=current.revision + 1,
        )
        self.store.save(updated, expected_revision=current.revision)
        return updated

    def consume_model_restart(
        self,
        *,
        explicitly_permitted: bool,
        now: float | None = None,
    ) -> OperationRecord:
        """Persist the exceptional model-restart budget across coordinators."""

        current = self.read()
        self._check_time(current, now)
        if (
            not explicitly_permitted
            or current.model_restarts >= current.model_restart_limit
        ):
            self._exhaust(current)
        updated = replace(
            current,
            model_restarts=current.model_restarts + 1,
            revision=current.revision + 1,
        )
        self.store.save(updated, expected_revision=current.revision)
        return updated

    def check_budget(
        self,
        *,
        now: float | None = None,
        timeout_reason: AttentionReason = AttentionReason.RETRY_EXHAUSTED,
    ) -> OperationRecord:
        if not isinstance(timeout_reason, AttentionReason):
            raise SupervisorError("operation timeout reason is invalid")
        current = self.read()
        self._check_time(current, now, timeout_reason)
        return current

    def effect(
        self,
        effect_id: str,
        action: Callable[[OperationRecord], T],
        *,
        persist_result: Callable[[OperationRecord, T], None] | None = None,
        resume_pending: bool = False,
    ) -> EffectResult[T]:
        """Execute one write-ahead effect, refusing blind uncertain retries."""

        current = self.read()
        if current.pending_effect:
            if current.pending_effect != effect_id or not resume_pending:
                raise SupervisorError(
                    f"effect {current.pending_effect} requires reconciliation"
                )
        elif (
            current.effect_id == effect_id
            and current.effect_outcome == EffectOutcome.SUCCEEDED
        ):
            return EffectResult(current, None, True)
        else:
            current = self.store.begin_effect(
                self.owner_id, self.operation_id, effect_id
            )

        value = action(current)
        if persist_result is not None:
            persist_result(current, value)
        resolved = self.store.resolve_effect(
            self.owner_id,
            self.operation_id,
            EffectOutcome.SUCCEEDED,
        )
        return EffectResult(resolved, value)


@dataclass(frozen=True)
class RetryBudget:
    read_probe_limit: int = 3
    model_restart_limit: int = 1
    attempt: int = 0
    model_restarts: int = 0

    def next_probe(self) -> "RetryBudget":
        if self.attempt >= self.read_probe_limit:
            raise RuntimeError(AttentionReason.RETRY_EXHAUSTED.value)
        return replace(self, attempt=self.attempt + 1)

    def next_model_restart(self, *, explicitly_permitted: bool) -> "RetryBudget":
        if not explicitly_permitted or self.model_restarts >= self.model_restart_limit:
            raise RuntimeError(AttentionReason.RETRY_EXHAUSTED.value)
        return replace(self, model_restarts=self.model_restarts + 1)


@dataclass(frozen=True)
class WatchState:
    started_at: float
    last_progress_at: float
    callback_deadline: float

    @classmethod
    def start(cls, timeout_seconds: float) -> "WatchState":
        now = monotonic()
        return cls(now, now, now + timeout_seconds)

    def progress(self) -> "WatchState":
        return replace(self, last_progress_at=monotonic())

    def callback_timed_out(self, now: float | None = None) -> bool:
        return (monotonic() if now is None else now) >= self.callback_deadline


def next_action_after_uncertain_effect(record: OperationRecord) -> str:
    return "reconcile" if record.pending_effect else "continue"
