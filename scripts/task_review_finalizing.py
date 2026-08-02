"""Exact-identity recovery for accepted finalizing review rounds."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from harness.contracts import OwnedResources
from harness.runtime_sessions import RuntimeSessionManager
from harness.state_machine import TERMINAL
from harness.store import OperationStore
from harness.workflows.review import ReviewContext, ReviewRound
from harness.workflows.review_gate import (
    ReviewGateController,
    ReviewGateRun,
    ReviewScopeBoundary,
    review_context_sha256,
)
from task_review_context import (
    _callback_path,
    _canonical_sha256,
    _context,
    _gate_root,
    _prompt,
    _runtime_root,
    _validate_task,
)
from task_review_shared import (
    FinalizingRecovery,
    TaskReviewError,
    _atomic_json,
    _read_json,
)
from task_review_transport import (
    _emit_round_telemetry,
    _receipt,
    _write_round_meta,
)
from task_review_verification import (
    _durable_successful_verification,
    _finalizing_resubmit_recovery,
)


def _apply_finalizing_recovery(
    *,
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    task_id: str,
    gate: ReviewGateController,
    run: ReviewGateRun,
    recovery: FinalizingRecovery,
) -> dict[str, Any]:
    lane = run.execution.lanes[0]
    round_ = run.rounds[lane.axis]
    initial_status = gate.read().get("status")
    try:
        gate.stage_finalizing_reverification(
            run,
            lane,
            round_,
            recovery.result,
            recovery_pointer=recovery.marker_pointer,
            recovery_sha256=recovery.marker_sha256,
        )
    except (OSError, ValueError) as exc:
        raise TaskReviewError(
            f"finalizing review recovery failed: {exc}"
        ) from exc
    if initial_status == "verifying":
        _emit_round_telemetry(
            worktree,
            vault,
            runtime_root,
            round_,
            event="review-callback",
            terminal_status="accepted",
        )
        _emit_round_telemetry(
            worktree,
            vault,
            runtime_root,
            round_,
            event="review-round-complete",
            terminal_status=recovery.result.verdict,
            severities=tuple(
                finding.severity for finding in recovery.result.findings
            ),
        )
    successful = _durable_successful_verification(
        meta,
        vault,
        gate.round_store,
        task_id,
        recovery.context.head_sha,
    )
    if successful is None:
        return _receipt(
            status="verifying",
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=recovery.context_manifest,
            run=gate.rehydrate(),
        )
    verification_operation_id, verification_receipt_sha256 = successful
    previous_context = run.execution.request.context
    reason = "verified resubmission requires exact-HEAD reviewer inspection"
    boundary = ReviewScopeBoundary(
        "context",
        review_context_sha256(previous_context),
        review_context_sha256(recovery.context),
        reason,
    )
    authorization = {
        "schema_version": 1,
        "operation_id": task_id,
        "kind": boundary.kind,
        "previous_context_sha256": boundary.previous_context_sha256,
        "next_context_sha256": boundary.next_context_sha256,
        "reason": boundary.reason,
        "authorization_provenance": "pipeline-verification",
        "verification_operation_id": verification_operation_id,
        "verification_receipt_sha256": verification_receipt_sha256,
        "status": "authorized",
    }
    authorization_name = (
        "fresh-boundary-authorization-"
        f"{_canonical_sha256(authorization)[:16]}.json"
    )
    authorization_path = gate.root / authorization_name
    if authorization_path.exists():
        if (
            authorization_path.is_symlink()
            or _read_json(
                authorization_path, "fresh boundary authorization"
            )
            != authorization
        ):
            raise TaskReviewError(
                "fresh boundary authorization changed across replay"
            )
    else:
        _atomic_json(authorization_path, authorization)
    authorization_sha256 = hashlib.sha256(
        authorization_path.read_bytes()
    ).hexdigest()
    try:
        gate.authorize_fresh_boundary(
            run,
            boundary=boundary,
            authorization_pointer=authorization_name,
            authorization_sha256=authorization_sha256,
        )
    except (OSError, ValueError) as exc:
        raise TaskReviewError(
            f"finalizing review boundary authorization failed: {exc}"
        ) from exc
    if not _dispatched_review_is_quiescent(gate.round_store, task_id):
        raise TaskReviewError(
            "verified fresh review boundary is not quiescent"
        )
    return _launch_authorized_task_review(
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        task_id=task_id,
        gate=gate,
        run=run,
        context=recovery.context,
        context_manifest=recovery.context_manifest,
        boundary=boundary,
    )


def recover_finalizing_review(
    worktree: Path,
    *,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    """Recover only the exact accepted-approval verification crash window."""

    worktree = worktree.expanduser().resolve()
    meta, vault, task_id = _validate_task(worktree)
    runtime_root = _runtime_root(vault, task_id)
    store_root = vault / ".vault-meta" / "harness"
    store = OperationStore(store_root)
    runtime = runtime_manager or RuntimeSessionManager.for_root(
        vault, store_root=store_root
    )
    gate = ReviewGateController(
        _gate_root(vault, task_id),
        runtime,
        store,
    )
    if not gate.state_path.is_file() or gate.state_path.is_symlink():
        raise TaskReviewError(
            "finalizing review recovery gate is unavailable"
        )
    run = gate.rehydrate()
    context, context_manifest = _context(
        meta,
        vault,
        worktree,
        runtime_root,
        task_id,
    )
    state = gate.read()
    if (
        state.get("status") == "approved"
        and isinstance(state.get("context"), dict)
        and state["context"].get("head_sha") == context.head_sha
        and isinstance(state.get("finalizing_recovery"), dict)
    ):
        return _receipt(
            status="approved",
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=run,
        )
    if (
        state.get("status") in {"fresh-reevaluation", "reviewing", "verifying"}
        and isinstance(state.get("context"), dict)
        and state["context"].get("head_sha") == context.head_sha
        and isinstance(state.get("fresh_boundary_authorization"), dict)
    ):
        return _receipt(
            status="reviewing",
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=run,
        )
    if context.head_sha == run.execution.request.context.head_sha:
        raise TaskReviewError(
            "finalizing review recovery requires an exact repaired HEAD"
        )
    recovery = _finalizing_resubmit_recovery(
        meta,
        vault,
        worktree,
        runtime_root,
        task_id,
        store,
        gate,
        run,
        context,
    )
    if recovery is None:
        raise TaskReviewError(
            "finalizing review recovery boundary is unavailable"
        )
    return _apply_finalizing_recovery(
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        task_id=task_id,
        gate=gate,
        run=run,
        recovery=recovery,
    )


def _dispatched_review_is_quiescent(
    store: OperationStore,
    task_id: str,
) -> bool:
    rows = store.list(task_id)
    if not rows:
        return False
    for row in rows:
        if row.resources != OwnedResources() or row.pending_effect:
            return False
        if (
            row.spec.operation_id == task_id
            and row.spec.kind == "dispatch"
        ):
            if row.state != "attention-required":
                return False
        elif row.state not in TERMINAL:
            return False
    return True


def _launch_authorized_task_review(
    *,
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    task_id: str,
    gate: ReviewGateController,
    run: ReviewGateRun,
    context: ReviewContext,
    context_manifest: Path,
    boundary: ReviewScopeBoundary,
    max_verify_iterations: int | None = None,
) -> dict[str, Any]:
    """Launch one pre-authorized fresh run after complete scratch preflight."""

    prompt_pointers = {
        axis: _prompt(
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context=context,
            axis=axis,
            verification=False,
        )
        for axis in run.execution.request.policy.axes
    }

    def prepare_lane(
        axis: str,
        _session_request: object,
        _result: object,
        round_: ReviewRound,
    ) -> None:
        _write_round_meta(
            runtime_root=runtime_root,
            vault=vault,
            worktree=worktree,
            task_id=task_id,
            depth=run.execution.request.policy.depth,
            context=context,
            lane_operation_id=round_.parent_operation_id,
            round_=round_,
        )

    for axis in run.execution.request.policy.axes:
        callback = _callback_path(runtime_root, axis)
        if callback.is_symlink():
            raise TaskReviewError("fresh review callback is invalid")
        callback.unlink(missing_ok=True)
    fresh = gate.restart_for_boundary(
        run,
        boundary=boundary,
        context=context,
        origin_surface=str(meta.get("task_surface") or ""),
        cwd=runtime_root,
        product_root=worktree,
        prompt_pointer=prompt_pointers[
            run.execution.request.policy.axes[0]
        ],
        prompt_pointers=prompt_pointers,
        callback_root="callbacks",
        max_verify_iterations=max_verify_iterations,
        prepare_lane=prepare_lane,
    )
    if fresh is None:
        raise TaskReviewError("fresh review boundary is exhausted")
    return _receipt(
        status="reviewing",
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        context_manifest=context_manifest,
        run=fresh,
    )
