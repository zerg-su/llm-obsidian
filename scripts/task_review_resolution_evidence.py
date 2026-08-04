"""Validate the one persisted resolution used by summary-only recovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.workflows.review_gate import ReviewGateController
from review_resolution import ResolutionError, validate_resolution_evidence
from task_review_shared import TaskReviewError, _read_json


def approved_summary_resolution(
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
