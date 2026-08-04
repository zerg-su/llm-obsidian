"""Persist exact coordinator authorization for a fresh review boundary."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from harness.workflows.review_gate import ReviewGateController, ReviewScopeBoundary
from task_review_context import _canonical_sha256
from task_review_shared import TaskReviewError, _atomic_json, _read_json


def authorization_payload(
    *,
    task_id: str,
    boundary: ReviewScopeBoundary,
    attention: dict[str, Any],
    attention_record_sha256: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", attention_record_sha256):
        raise TaskReviewError("coordinator authorization record digest is invalid")
    return {
        "schema_version": 1,
        "operation_id": task_id,
        "kind": boundary.kind,
        "previous_context_sha256": boundary.previous_context_sha256,
        "next_context_sha256": boundary.next_context_sha256,
        "reason": boundary.reason,
        "authorization_provenance": "coordinator-approved",
        "verification_operation_id": str(attention.get("id") or ""),
        "verification_receipt_sha256": attention_record_sha256,
        "status": "authorized",
    }


def persist_authorization(
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
