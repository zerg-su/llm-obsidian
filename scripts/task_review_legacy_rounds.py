"""Recovery-only adapter for terminal rounds created before parent identity."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from harness.contracts import OperationRecord, OperationSpec, OwnedResources
from harness.state_machine import TERMINAL
from harness.store import OperationStore, StoreError


class RecoveryRoundStore:
    """Rehydrate one exact terminal legacy round without rewriting its record."""

    def __init__(self, store: OperationStore) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def create(
        self,
        spec: OperationSpec,
        *,
        lane_id: str,
        run_id: str,
    ) -> OperationRecord:
        try:
            return self._store.create(spec, lane_id=lane_id, run_id=run_id)
        except StoreError as original:
            try:
                existing = self._store.read(spec.owner_id, spec.operation_id)
            except StoreError:
                raise original
            legacy_spec = replace(spec, parent_operation_id="")
            if (
                spec.kind != "review-round"
                or not spec.parent_operation_id
                or existing.spec != legacy_spec
                or existing.lane_id != lane_id
                or existing.run_id != run_id
                or existing.state not in TERMINAL
                or existing.resources != OwnedResources()
                or existing.pending_effect
            ):
                raise original
            return replace(existing, spec=spec)
