"""Bounded orchestration for one exact provider-session cancellation."""

from __future__ import annotations

from time import sleep

from .contracts import OwnedResources
from .runtime_session_contracts import RuntimeSessionError, RuntimeSessionResult
from .state_machine import TERMINAL


CANCEL_CLEANUP_OBSERVATIONS = 40
CANCEL_CLEANUP_INTERVAL_SECONDS = 0.05
RETRYABLE_CANCEL_ACTIONS = frozenset(
    {"terminate-orphan", "wait-for-exit", "wait-for-supervisor"}
)


class RuntimeSessionCancellationMixin:
    """Request exit once, then finish exact cleanup within a fixed probe budget."""

    def cancel(self, owner_id: str, operation_id: str) -> RuntimeSessionResult:
        requested = self.request_exit(owner_id, operation_id)
        if requested.record.state in TERMINAL:
            self._require_released_terminal(requested)
            return requested
        if requested.action != "exit-requested":
            return requested

        result = requested
        for observation in range(CANCEL_CLEANUP_OBSERVATIONS):
            result = self.cleanup(
                owner_id,
                operation_id,
                terminal_state="cancelled",
            )
            if result.record.state in TERMINAL:
                self._require_released_terminal(result)
                return result
            if result.action not in RETRYABLE_CANCEL_ACTIONS:
                return result
            if observation + 1 < CANCEL_CLEANUP_OBSERVATIONS:
                sleep(CANCEL_CLEANUP_INTERVAL_SECONDS)
        return result

    @staticmethod
    def _require_released_terminal(result: RuntimeSessionResult) -> None:
        if result.record.resources != OwnedResources():
            raise RuntimeSessionError(
                "cancelled provider retained owned resources"
            )
