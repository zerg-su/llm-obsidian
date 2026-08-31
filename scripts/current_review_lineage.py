"""Fresh-lineage eligibility and late-winner convergence for current review."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Mapping

from task_review_context import (
    _current_review_is_quiescent,
    _current_runtime_root,
    _gate_root,
)
from task_review_shared import TaskReviewError, _git, _read_json


ACTIVE_WINNER_STATUSES = frozenset(
    {
        "pending",
        "reviewing",
        "verifying",
        "fresh-reevaluation",
        "fresh-boundary-authorized",
        "recovery-verification-required",
        "awaiting-resolution",
    }
)


def same_requested_policy(
    stored: Mapping[str, Any],
    requested: Mapping[str, Any],
    *,
    allow_boundary_rebind: bool = False,
) -> bool:
    """Compare the exact public current-review policy identity."""

    base_matches = all(
        stored.get(name) == requested.get(name)
        for name in (
            "mode",
            "cross_model",
            "runtime",
            "model",
            "effort",
            "max_verify_iterations",
            "verification_profile",
            "verification_profile_sha256",
        )
    )
    return (
        base_matches
        and str(stored.get("purpose") or "implementation")
        == str(requested.get("purpose") or "implementation")
        and str(stored.get("base_sha") or "")
        == str(requested.get("base_sha") or "")
        and (
            allow_boundary_rebind
            or str(stored.get("boundary_input_sha256") or "")
            == str(requested.get("boundary_input_sha256") or "")
        )
    )


def new_lineage_supersedes_active(
    candidate: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    task_id: str,
    *,
    new_lineage: bool,
    same_policy: bool,
    status: str,
    bound_head: str,
    current_head: str,
    operation_quiescent: bool,
) -> bool:
    """Authorize one replacement only from an exhausted material lineage."""

    if not new_lineage:
        return False
    if not same_policy:
        raise TaskReviewError(
            "an exhausted current review uses another preset or override"
        )
    if status != "changes-requested":
        raise TaskReviewError(
            "--new-lineage requires an exhausted changes-requested review"
        )
    if not bound_head or bound_head == current_head:
        raise TaskReviewError(
            "--new-lineage requires a committed resolution HEAD"
        )
    if not operation_quiescent:
        raise TaskReviewError(
            "--new-lineage requires quiescent review operations"
        )
    from task_review_finalization_attempt import finalization_ledger

    exhausted = finalization_ledger(
        candidate, vault, task_id, worktree
    ).snapshot()
    if (
        exhausted.get("terminal_disposition")
        != "finalization-budget-exhausted"
    ):
        raise TaskReviewError(
            "--new-lineage requires an exhausted finalization ledger"
        )
    return True


def _request_matches(
    value: Mapping[str, Any],
    *,
    task_id: str,
    worktree: Path,
    target_key: str,
    requested_policy: Mapping[str, Any],
    plan_identity: tuple[str, str],
) -> bool:
    policy = value.get("review_policy")
    return bool(
        value.get("task_id") == task_id
        and value.get("lifecycle") == "current-checkout"
        and value.get("worktree") == str(worktree)
        and value.get("target_key") == target_key
        and isinstance(policy, Mapping)
        and same_requested_policy(policy, requested_policy)
        and (
            str(value.get("approved_plan_sha256") or ""),
            str(value.get("outcome_contract_sha256") or ""),
        )
        == plan_identity
    )


def published_new_lineage_matches(
    candidate: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    target_key: str,
    requested_policy: Mapping[str, Any],
    plan_identity: tuple[str, str] | None,
    scratch_root: Path | None,
) -> bool:
    """Recognize the winner observed by a late overlapping fresh caller."""

    if plan_identity is None:
        return False
    task_id = str(candidate.get("task_id") or "")
    predecessor_id = str(candidate.get("supersedes_task_id") or "")
    try:
        if (
            str(uuid.UUID(task_id)) != task_id
            or str(uuid.UUID(predecessor_id)) != predecessor_id
            or task_id == predecessor_id
        ):
            return False
    except (ValueError, TypeError, AttributeError):
        return False
    if not _request_matches(
        candidate,
        task_id=task_id,
        worktree=worktree,
        target_key=target_key,
        requested_policy=requested_policy,
        plan_identity=plan_identity,
    ):
        return False
    winner_gate_path = _gate_root(vault, task_id) / "review-gate.json"
    if not winner_gate_path.is_file() or winner_gate_path.is_symlink():
        return False
    winner_gate = _read_json(winner_gate_path, "current review gate")
    if str(winner_gate.get("status") or "") not in ACTIVE_WINNER_STATUSES:
        return False
    predecessor_path = (
        _current_runtime_root(worktree, predecessor_id, scratch_root)
        / "current-review.json"
    )
    if not predecessor_path.is_file() or predecessor_path.is_symlink():
        return False
    predecessor = _read_json(predecessor_path, "superseded current review")
    if not _request_matches(
        predecessor,
        task_id=predecessor_id,
        worktree=worktree,
        target_key=target_key,
        requested_policy=requested_policy,
        plan_identity=plan_identity,
    ):
        return False
    predecessor_gate_path = _gate_root(vault, predecessor_id) / "review-gate.json"
    if not predecessor_gate_path.is_file() or predecessor_gate_path.is_symlink():
        return False
    predecessor_gate = _read_json(
        predecessor_gate_path, "superseded current review gate"
    )
    bound = predecessor_gate.get("context")
    bound_head = (
        str(bound.get("head_sha") or "") if isinstance(bound, Mapping) else ""
    )
    return new_lineage_supersedes_active(
        predecessor,
        vault,
        worktree,
        predecessor_id,
        new_lineage=True,
        same_policy=True,
        status=str(predecessor_gate.get("status") or ""),
        bound_head=bound_head,
        current_head=_git(worktree, "rev-parse", "HEAD"),
        operation_quiescent=_current_review_is_quiescent(vault, predecessor_id),
    )
