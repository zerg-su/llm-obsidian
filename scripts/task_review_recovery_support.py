"""Fail-closed compatibility and authorization helpers for review recovery."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

from harness.contracts import OperationRecord, OperationSpec, OwnedResources
from harness.state_machine import TERMINAL
from harness.store import OperationStore, StoreError
from harness.workflows.review_gate import ReviewGateController, ReviewScopeBoundary
from review_resolution import ResolutionError, validate_resolution_evidence
from task_review_context import _canonical_sha256
from task_review_shared import TaskReviewError, _atomic_json, _read_json


class _RecoveryRoundStore:
    """Read exact terminal rounds created before parent identity persisted."""

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


def _authorization_payload(
    *,
    task_id: str,
    boundary: ReviewScopeBoundary,
    attention: dict[str, Any],
    attention_path: Path,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "operation_id": task_id,
        "kind": boundary.kind,
        "previous_context_sha256": boundary.previous_context_sha256,
        "next_context_sha256": boundary.next_context_sha256,
        "reason": boundary.reason,
        "authorization_provenance": "coordinator-approved",
        "verification_operation_id": str(attention.get("id") or ""),
        "verification_receipt_sha256": hashlib.sha256(
            attention_path.read_bytes()
        ).hexdigest(),
        "status": "authorized",
    }


def _persist_authorization(
    gate: ReviewGateController,
    authorization: dict[str, Any],
    *,
    error_label: str,
) -> tuple[str, Path]:
    name = (
        "fresh-boundary-authorization-"
        f"{_canonical_sha256(authorization)[:16]}.json"
    )
    path = gate.root / name
    if path.exists():
        if (
            path.is_symlink()
            or _read_json(path, "fresh boundary authorization") != authorization
        ):
            raise TaskReviewError(f"{error_label} authorization changed")
    else:
        _atomic_json(path, authorization)
    return name, path


def _approved_summary_resolution(
    *,
    gate: ReviewGateController,
    state: dict[str, Any],
    task_id: str,
    simple_axis: str,
    current_head: str,
) -> Any:
    raw_evidence = state.get("resolution_evidence")
    if not isinstance(raw_evidence, dict) or len(raw_evidence) != 1:
        raise TaskReviewError(
            "approved summary recovery resolution boundary is invalid"
        )
    pointer = Path(str(next(iter(raw_evidence.values()))))
    path = (gate.root / pointer).resolve()
    if (
        pointer.is_absolute()
        or gate.root not in path.parents
        or not path.is_file()
        or path.is_symlink()
    ):
        raise TaskReviewError(
            "approved summary recovery resolution evidence is unavailable"
        )
    try:
        resolution = validate_resolution_evidence(
            _read_json(path, "persisted review resolution")
        )
    except ResolutionError as exc:
        raise TaskReviewError(
            "approved summary recovery resolution evidence is invalid"
        ) from exc
    if (
        resolution.operation_id != task_id
        or resolution.axis != simple_axis
        or resolution.resolved_head_sha != current_head
    ):
        raise TaskReviewError(
            "approved summary recovery resolution identity changed"
        )
    return resolution
