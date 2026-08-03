"""Rebuild exact review context after a verified finalizing resubmit."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from harness.contracts import to_dict
from harness.store import OperationStore
from harness.workflows.review import ReviewContext, review_round_envelope
from harness.workflows.review_gate import ReviewGateController, ReviewGateRun
from review_resolution import ResolutionError, validate_resolution_evidence
from task_review_context import _context
from task_review_request import _callback_path, _canonical_sha256, _envelope
from task_review_resolution_bundle import _recovery_resolution_bundle
from task_review_shared import (
    FinalizingRecovery,
    TaskReviewError,
    _atomic_json,
    _read_json,
)
from task_review_verification_resubmit import _durable_verification_resubmit
from review_contract import review_axis_responsibility


def _finalizing_resubmit_recovery(
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    task_id: str,
    store: OperationStore,
    gate: ReviewGateController,
    run: ReviewGateRun,
    current_context: ReviewContext,
) -> FinalizingRecovery | None:
    """Validate one accepted approval stranded after verification repair."""

    state = gate.read()
    axes = run.execution.request.policy.axes
    simple_axis = axes[0] if len(axes) == 1 else ""
    if (
        state.get("status")
        not in {
            "verifying",
            "recovery-verification-required",
            "fresh-boundary-authorized",
        }
        or run.execution.request.policy.depth != "simple"
        or not simple_axis
        or review_axis_responsibility(simple_axis) != "holistic"
        or len(run.execution.lanes) != 1
    ):
        return None
    lane = run.execution.lanes[0]
    round_ = run.rounds.get(simple_axis)
    if round_ is None:
        return None
    child = gate.round_store.read(round_.owner_id, round_.operation_id)
    if (
        child.state not in {"finalizing", "complete"}
        or child.accepted_callback_kind != "review"
    ):
        return None
    previous_context = run.execution.request.context
    if previous_context.head_sha == current_context.head_sha:
        return None
    summary_path = worktree / ".task-summary.json"
    resolution_path = worktree / ".task-review-resolution.json"
    callback_path = _callback_path(runtime_root, simple_axis)
    for path, label in (
        (summary_path, "task summary"),
        (resolution_path, "review resolution"),
        (callback_path, "accepted review callback"),
    ):
        if not path.is_file() or path.is_symlink():
            raise TaskReviewError(
                f"finalizing review recovery {label} is unavailable"
            )
    callback_raw = _read_json(callback_path, "accepted review callback")
    envelope, result = _envelope(callback_path, round_)
    expected_envelope = to_dict(review_round_envelope(round_, result))
    if (
        result.verdict != "approve"
        or callback_raw != expected_envelope
        or envelope.callback_id != child.accepted_callback_id
        or envelope.kind != child.accepted_callback_kind
        or envelope.payload_sha256 != child.accepted_callback_sha256
    ):
        raise TaskReviewError(
            "finalizing review recovery callback identity changed"
        )
    (
        response_receipt_path,
        response_receipt,
        verification_receipt_sha256,
    ) = _durable_verification_resubmit(
        meta,
        worktree,
        store,
        task_id,
        previous_context.head_sha,
        current_context.head_sha,
    )
    raw_resolution_evidence = state.get("resolution_evidence")
    if (
        not isinstance(raw_resolution_evidence, dict)
        or len(raw_resolution_evidence) != 1
    ):
        raise TaskReviewError(
            "finalizing review recovery resolution boundary is invalid"
        )
    persisted_pointer = Path(
        str(next(iter(raw_resolution_evidence.values())))
    )
    persisted_path = (gate.root / persisted_pointer).resolve()
    if (
        persisted_pointer.is_absolute()
        or gate.root not in persisted_path.parents
        or not persisted_path.is_file()
        or persisted_path.is_symlink()
    ):
        raise TaskReviewError(
            "finalizing review recovery resolution evidence is unavailable"
        )
    try:
        persisted_resolution = validate_resolution_evidence(
            _read_json(persisted_path, "persisted review resolution")
        )
    except ResolutionError as exc:
        raise TaskReviewError(
            f"finalizing review recovery resolution evidence is invalid: {exc}"
        ) from exc
    if (
        persisted_resolution.operation_id != task_id
        or persisted_resolution.axis != simple_axis
        or persisted_resolution.resolved_head_sha
        != previous_context.head_sha
    ):
        raise TaskReviewError(
            "finalizing review recovery resolution identity changed"
        )
    bundle = _recovery_resolution_bundle(
        worktree,
        task_id,
        persisted_resolution,
        current_context.head_sha,
        str(state.get("resolution_transport_identity_sha256") or ""),
    )
    rebuilt_resolution = bundle.by_axis.get(simple_axis)
    if (
        rebuilt_resolution is None
        or rebuilt_resolution.operation_id
        != persisted_resolution.operation_id
        or rebuilt_resolution.axis != persisted_resolution.axis
        or rebuilt_resolution.reviewed_head_sha
        != persisted_resolution.reviewed_head_sha
        or rebuilt_resolution.previous_finding_ids
        != persisted_resolution.previous_finding_ids
        or dict(rebuilt_resolution.resolutions)
        != dict(persisted_resolution.resolutions)
    ):
        raise TaskReviewError(
            "finalizing review recovery finding rulings changed"
        )
    summary_bytes = summary_path.read_bytes()
    if not summary_bytes or len(summary_bytes) > 250_000:
        raise TaskReviewError(
            "finalizing review recovery task summary is invalid"
        )
    recovery_context, context_manifest = _context(
        meta,
        vault,
        worktree,
        runtime_root,
        task_id,
        resolution_bundle=bundle,
    )
    marker = {
        "schema_version": 1,
        "operation_id": task_id,
        "round_operation_id": round_.operation_id,
        "round_run_id": round_.run_id,
        "accepted_callback_sha256": envelope.payload_sha256,
        "failed_head_sha": previous_context.head_sha,
        "resubmitted_head_sha": recovery_context.head_sha,
        "verification_receipt_sha256": verification_receipt_sha256,
        "response_receipt_sha256": _canonical_sha256(response_receipt),
        "persisted_resolution_sha256": hashlib.sha256(
            persisted_path.read_bytes()
        ).hexdigest(),
        "resolution_sha256": hashlib.sha256(
            resolution_path.read_bytes()
        ).hexdigest(),
        "summary_sha256": hashlib.sha256(summary_bytes).hexdigest(),
        "status": "validated",
    }
    marker_name = (
        "finalizing-review-recovery-"
        f"{_canonical_sha256(marker)[:16]}.json"
    )
    marker_path = gate.root / marker_name
    if marker_path.exists():
        if (
            marker_path.is_symlink()
            or _read_json(marker_path, "finalizing review recovery marker")
            != marker
        ):
            raise TaskReviewError(
                "finalizing review recovery marker changed"
            )
    else:
        _atomic_json(marker_path, marker)
    if response_receipt_path.exists():
        if _read_json(
            response_receipt_path, "verification response receipt"
        ) != response_receipt:
            raise TaskReviewError(
                "finalizing review recovery response receipt changed"
            )
    else:
        _atomic_json(response_receipt_path, response_receipt)
    verification_child = store.read(
        task_id,
        str(response_receipt["verification_operation_id"]),
    )
    if verification_child.state == "attention-required":
        store.transition(
            task_id,
            verification_child.spec.operation_id,
            "failed",
        )
    elif verification_child.state != "failed":
        raise TaskReviewError(
            "finalizing review recovery verification response is not terminal"
        )
    return FinalizingRecovery(
        recovery_context,
        context_manifest,
        marker_name,
        hashlib.sha256(marker_path.read_bytes()).hexdigest(),
        response_receipt_path,
        response_receipt,
        result,
    )
