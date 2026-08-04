"""Coordinator-authorized recovery for dead or superseded review lanes."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from harness.contracts import OwnedResources
from harness.runtime_sessions import RuntimeSessionManager
from harness.state_machine import TERMINAL
from harness.store import OperationStore
from harness.workflows.review_gate import (
    ReviewGateController,
    ReviewScopeBoundary,
    review_context_sha256,
)
from task_review_context import (
    _context,
    _gate_root,
    _runtime_root,
    _validate_task,
)
from task_review_finalizing import (
    _dispatched_review_is_quiescent,
    _launch_authorized_task_review,
)
from task_review_resolution_bundle import _recovery_resolution_bundle
from task_review_shared import (
    TaskReviewError,
    _read_json,
)
from task_review_recovery_support import (
    _RecoveryRoundStore,
    _approved_summary_resolution,
    _authorization_payload,
    _persist_authorization,
)
from task_review_transport import _receipt
from review_contract import review_axis_responsibility


def _running_recovery_receipt(
    *,
    state: dict[str, Any],
    attention_id: str,
    meta: dict[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    run: Any,
) -> dict[str, Any] | None:
    stored_boundary = state.get("fresh_boundary")
    if not (
        state.get("fresh_reevaluation_used") is True
        and state.get("status") in {"fresh-reevaluation", "reviewing", "verifying"}
        and isinstance(stored_boundary, dict)
        and attention_id in str(stored_boundary.get("reason") or "")
    ):
        return None
    return _receipt(
        status="verifying" if state.get("status") == "verifying" else "reviewing",
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        context_manifest=runtime_root / run.execution.request.context.manifest,
        run=run,
    )


def _approved_summary_recovery(
    *,
    state: dict[str, Any],
    attention: dict[str, Any],
    attention_path: Path,
    meta: dict[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    task_id: str,
    gate: ReviewGateController,
    run: Any,
    current_context: Any,
) -> dict[str, Any] | None:
    previous_context = run.execution.request.context
    axes = run.execution.request.policy.axes
    simple_axis = axes[0] if len(axes) == 1 else ""
    is_summary_recovery = (
        state.get("status") == "approved"
        and state.get("fresh_reevaluation_used") is not True
        and state.get("final_results") not in ({}, None)
        and run.execution.request.policy.depth == "simple"
        and bool(simple_axis)
        and review_axis_responsibility(simple_axis) == "holistic"
        and previous_context.head_sha == current_context.head_sha
        and previous_context.verification_profile
        == current_context.verification_profile
        and previous_context.verification_profile_sha256
        == current_context.verification_profile_sha256
        and bool(previous_context.implementer_summary_sha256)
        and previous_context.implementer_summary_sha256
        != current_context.implementer_summary_sha256
    )
    if not is_summary_recovery:
        return None
    resolution = _approved_summary_resolution(
        gate=gate,
        state=state,
        task_id=task_id,
        simple_axis=simple_axis,
        current_head=current_context.head_sha,
    )
    bundle = _recovery_resolution_bundle(
        worktree,
        task_id,
        resolution,
        current_context.head_sha,
        str(state.get("resolution_transport_identity_sha256") or ""),
    )
    current_context, context_manifest = _context(
        meta,
        vault,
        worktree,
        runtime_root,
        task_id,
        resolution_bundle=bundle,
    )
    boundary = ReviewScopeBoundary(
        "context",
        review_context_sha256(previous_context),
        review_context_sha256(current_context),
        (
            "resolved mechanism escalation "
            f"{attention.get('id')}: review refreshed summary bytes only"
        ),
    )
    authorization = _authorization_payload(
        task_id=task_id,
        boundary=boundary,
        attention=attention,
        attention_path=attention_path,
    )
    name, path = _persist_authorization(
        gate,
        authorization,
        error_label="approved summary recovery",
    )
    gate.authorize_fresh_summary_boundary(
        run,
        boundary=boundary,
        context=current_context,
        authorization_pointer=name,
        authorization_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    return _launch_authorized_task_review(
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        task_id=task_id,
        gate=gate,
        run=run,
        context=current_context,
        context_manifest=context_manifest,
        boundary=boundary,
        max_verify_iterations=0,
    )


def _assert_quiescent_stale_boundary(
    *,
    state: dict[str, Any],
    run: Any,
    store: OperationStore,
    task_id: str,
) -> None:
    if (
        state.get("status")
        not in {
            "verifying",
            "awaiting-resolution",
            "attention-required",
            "fresh-boundary-authorized",
        }
        or state.get("fresh_reevaluation_used") is True
        or state.get("final_results") not in ({}, None)
        or not run.execution.lanes
    ):
        raise TaskReviewError(
            "review mechanism recovery is not at one stale verification boundary"
        )
    for lane in run.execution.lanes:
        parent = store.read(task_id, lane.operation_id)
        round_ = run.rounds.get(lane.axis)
        if round_ is None:
            raise TaskReviewError("review mechanism recovery round is unavailable")
        child = store.read(task_id, round_.operation_id)
        if (
            parent.state not in TERMINAL
            or child.state not in TERMINAL
            or parent.resources != OwnedResources()
            or child.resources != OwnedResources()
            or parent.pending_effect
            or child.pending_effect
        ):
            raise TaskReviewError(
                "review mechanism recovery still has live review ownership"
            )


def _recover_stale_boundary(
    *,
    state: dict[str, Any],
    attention: dict[str, Any],
    attention_path: Path,
    meta: dict[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    task_id: str,
    gate: ReviewGateController,
    run: Any,
    store: OperationStore,
    current_context: Any,
    context_manifest: Path,
) -> dict[str, Any]:
    _assert_quiescent_stale_boundary(
        state=state,
        run=run,
        store=store,
        task_id=task_id,
    )
    boundary = ReviewScopeBoundary(
        "context",
        review_context_sha256(run.execution.request.context),
        review_context_sha256(current_context),
        (
            "resolved mechanism escalation "
            f"{attention.get('id')}: replace the dead verification runtime"
        ),
    )
    authorization = _authorization_payload(
        task_id=task_id,
        boundary=boundary,
        attention=attention,
        attention_path=attention_path,
    )
    name, path = _persist_authorization(
        gate,
        authorization,
        error_label="review mechanism recovery",
    )
    if state.get("status") in {"verifying", "awaiting-resolution"}:
        gate._mark_attention(run.execution.lanes)
    if gate.read().get("status") != "fresh-boundary-authorized":
        gate.authorize_fresh_boundary(
            run,
            boundary=boundary,
            authorization_pointer=name,
            authorization_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return _launch_authorized_task_review(
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        task_id=task_id,
        gate=gate,
        run=run,
        context=current_context,
        context_manifest=context_manifest,
        boundary=boundary,
        max_verify_iterations=0,
    )


def recover_task_review_for_mechanism(
    worktree: Path,
    *,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    """Use one resolved mechanism escalation to replace a dead review lane."""

    worktree = worktree.expanduser().resolve()
    meta, vault, task_id = _validate_task(worktree)
    attention_path = worktree / ".task-needs-attention.json"
    attention = _read_json(attention_path, "task escalation")
    runtime_root = _runtime_root(vault, task_id)
    current_context, context_manifest = _context(
        meta,
        vault,
        worktree,
        runtime_root,
        task_id,
    )
    decision = str(attention.get("decision") or "")
    if (
        attention.get("status") != "resolved"
        or attention.get("category") != "mechanism-failure"
        or str(attention.get("worktree") or "") != str(worktree)
        or not decision.startswith(
            "authorize-one-bounded-fresh-context-review-boundary-for-"
        )
        or current_context.head_sha[:7] not in decision
    ):
        raise TaskReviewError(
            "review mechanism recovery lacks exact coordinator authorization"
        )
    store_root = vault / ".vault-meta" / "harness"
    store = OperationStore(store_root)
    runtime = runtime_manager or RuntimeSessionManager.for_root(
        vault, store_root=store_root
    )
    gate = ReviewGateController(
        _gate_root(vault, task_id),
        runtime,
        _RecoveryRoundStore(store),
    )
    state = gate.read()
    run = gate.rehydrate()
    running = _running_recovery_receipt(
        state=state,
        attention_id=str(attention.get("id") or ""),
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        run=run,
    )
    if running is not None:
        return running
    summary_recovery = _approved_summary_recovery(
        state=state,
        attention=attention,
        attention_path=attention_path,
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        task_id=task_id,
        gate=gate,
        run=run,
        current_context=current_context,
    )
    if summary_recovery is not None:
        return summary_recovery
    return _recover_stale_boundary(
        state=state,
        attention=attention,
        attention_path=attention_path,
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        task_id=task_id,
        gate=gate,
        run=run,
        store=store,
        current_context=current_context,
        context_manifest=context_manifest,
    )


def restart_task_review_for_boundary(
    worktree: Path,
    *,
    kind: str,
    reason: str,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    """Start the one persisted fresh review allowed for a dispatched task."""

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
        raise TaskReviewError("fresh review gate is unavailable")
    state = gate.read()
    run = gate.rehydrate()
    context, context_manifest = _context(
        meta,
        vault,
        worktree,
        runtime_root,
        task_id,
    )
    stored_boundary = state.get("fresh_boundary")
    if (
        state.get("status") in {"fresh-reevaluation", "reviewing", "verifying"}
        and state.get("fresh_reevaluation_used") is True
        and isinstance(stored_boundary, dict)
        and stored_boundary.get("kind") == kind
        and stored_boundary.get("reason") == reason
        and stored_boundary.get("next_context_sha256")
        == review_context_sha256(context)
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
    if (
        state.get("status") != "fresh-boundary-authorized"
        or state.get("fresh_reevaluation_used") is True
        or not _dispatched_review_is_quiescent(store, task_id)
    ):
        raise TaskReviewError(
            "fresh review requires one quiescent authorized boundary"
        )
    previous_context = run.execution.request.context
    boundary = ReviewScopeBoundary(
        kind,
        review_context_sha256(previous_context),
        review_context_sha256(context),
        reason,
    )
    return _launch_authorized_task_review(
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        task_id=task_id,
        gate=gate,
        run=run,
        context=context,
        context_manifest=context_manifest,
        boundary=boundary,
    )
