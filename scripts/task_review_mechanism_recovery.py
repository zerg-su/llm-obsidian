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
from review_resolution import (
    ResolutionError,
    validate_resolution_evidence,
)
from task_review_context import (
    _canonical_sha256,
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
    _atomic_json,
    _read_json,
)
from task_review_transport import _receipt
from review_contract import review_axis_responsibility


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
    current_context, context_manifest = _context(
        meta,
        vault,
        worktree,
        _runtime_root(vault, task_id),
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
        store,
    )
    state = gate.read()
    run = gate.rehydrate()
    stored_boundary = state.get("fresh_boundary")
    if (
        state.get("fresh_reevaluation_used") is True
        and state.get("status") in {"fresh-reevaluation", "reviewing", "verifying"}
        and isinstance(stored_boundary, dict)
        and str(attention.get("id") or "")
        in str(stored_boundary.get("reason") or "")
    ):
        return _receipt(
            status=(
                "verifying"
                if state.get("status") == "verifying"
                else "reviewing"
            ),
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=_runtime_root(vault, task_id),
            context_manifest=(
                _runtime_root(vault, task_id)
                / run.execution.request.context.manifest
            ),
            run=run,
        )
    previous_context = run.execution.request.context
    axes = run.execution.request.policy.axes
    simple_axis = axes[0] if len(axes) == 1 else ""
    if (
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
    ):
        raw_resolution_evidence = state.get("resolution_evidence")
        if (
            not isinstance(raw_resolution_evidence, dict)
            or len(raw_resolution_evidence) != 1
        ):
            raise TaskReviewError(
                "approved summary recovery resolution boundary is invalid"
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
                "approved summary recovery resolution evidence is unavailable"
            )
        try:
            persisted_resolution = validate_resolution_evidence(
                _read_json(
                    persisted_path, "persisted review resolution"
                )
            )
        except ResolutionError as exc:
            raise TaskReviewError(
                "approved summary recovery resolution evidence is invalid"
            ) from exc
        if (
            persisted_resolution.operation_id != task_id
            or persisted_resolution.axis != simple_axis
            or persisted_resolution.resolved_head_sha
            != current_context.head_sha
        ):
            raise TaskReviewError(
                "approved summary recovery resolution identity changed"
            )
        bundle = _recovery_resolution_bundle(
            worktree,
            task_id,
            persisted_resolution,
            current_context.head_sha,
            str(state.get("resolution_transport_identity_sha256") or ""),
        )
        current_context, context_manifest = _context(
            meta,
            vault,
            worktree,
            _runtime_root(vault, task_id),
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
        authorization = {
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
        authorization_name = (
            "fresh-boundary-authorization-"
            f"{_canonical_sha256(authorization)[:16]}.json"
        )
        authorization_path = gate.root / authorization_name
        if authorization_path.exists():
            if (
                authorization_path.is_symlink()
                or _read_json(
                    authorization_path,
                    "fresh summary boundary authorization",
                )
                != authorization
            ):
                raise TaskReviewError(
                    "approved summary recovery authorization changed"
                )
        else:
            _atomic_json(authorization_path, authorization)
        gate.authorize_fresh_summary_boundary(
            run,
            boundary=boundary,
            context=current_context,
            authorization_pointer=authorization_name,
            authorization_sha256=hashlib.sha256(
                authorization_path.read_bytes()
            ).hexdigest(),
        )
        return _launch_authorized_task_review(
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=_runtime_root(vault, task_id),
            task_id=task_id,
            gate=gate,
            run=run,
            context=current_context,
            context_manifest=context_manifest,
            boundary=boundary,
            max_verify_iterations=0,
        )
    if (
        state.get("status")
        not in {"verifying", "attention-required", "fresh-boundary-authorized"}
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
    boundary = ReviewScopeBoundary(
        "context",
        review_context_sha256(previous_context),
        review_context_sha256(current_context),
        (
            "resolved mechanism escalation "
            f"{attention.get('id')}: replace the dead verification runtime"
        ),
    )
    authorization = {
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
                "review mechanism recovery authorization changed"
            )
    else:
        _atomic_json(authorization_path, authorization)
    if state.get("status") == "verifying":
        gate._mark_attention(run.execution.lanes)
    if gate.read().get("status") != "fresh-boundary-authorized":
        gate.authorize_fresh_boundary(
            run,
            boundary=boundary,
            authorization_pointer=authorization_name,
            authorization_sha256=hashlib.sha256(
                authorization_path.read_bytes()
            ).hexdigest(),
        )
    return _launch_authorized_task_review(
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=_runtime_root(vault, task_id),
        task_id=task_id,
        gate=gate,
        run=run,
        context=current_context,
        context_manifest=context_manifest,
        boundary=boundary,
        max_verify_iterations=0,
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
