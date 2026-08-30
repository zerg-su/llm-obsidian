"""Observational exact-HEAD lease for standalone current review."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from harness.git_ops import GitAdapter, GitError
from review_target import (
    CleanHeadSnapshot,
    ReviewTarget,
    ReviewTargetError,
    resolve_target,
    snapshot_clean,
)
from task_review_shared import TaskReviewError, _atomic_json, _read_json


class CurrentReviewLeaseError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        reasons: tuple[str, ...],
        observed_head: str = "",
    ) -> None:
        super().__init__(message)
        self.reasons = reasons
        self.observed_head = observed_head


def _payload(snapshot: CleanHeadSnapshot) -> dict[str, object]:
    return {
        "schema_version": 1,
        "target_key": snapshot.target_key,
        "head": snapshot.head,
        "snapshot_sha256": snapshot.snapshot_sha256,
    }


def create_review_lease(target: ReviewTarget) -> dict[str, object]:
    try:
        return _payload(snapshot_clean(target))
    except ReviewTargetError as exc:
        reasons, head = _dirty_reasons(target)
        raise CurrentReviewLeaseError(
            str(exc), reasons=reasons, observed_head=head
        ) from exc


def _dirty_reasons(target: ReviewTarget) -> tuple[tuple[str, ...], str]:
    adapter = GitAdapter(target.root)
    reasons: set[str] = set()
    head = ""
    try:
        head = adapter.optional_revision("HEAD")
        status = adapter.status_porcelain()
        for entry in status.split("\x00"):
            if len(entry) < 2:
                continue
            code = entry[:2]
            if code == "??":
                reasons.add("untracked")
                continue
            if "U" in code or code in {"AA", "DD"}:
                reasons.add("conflict")
            if code[0] != " ":
                reasons.add("index")
            if code[1] != " ":
                reasons.add("worktree")
        inspected = adapter.inspect("HEAD")
        if inspected.operation:
            reasons.add("operation")
    except GitError:
        reasons.add("git-state")
    return tuple(sorted(reasons or {"dirty"})), head


def _validated_lease(lease: Mapping[str, object], target: ReviewTarget) -> None:
    if (
        set(lease)
        != {"schema_version", "target_key", "head", "snapshot_sha256"}
        or lease.get("schema_version") != 1
        or lease.get("target_key") != target.target_key
        or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", str(lease.get("head") or ""))
        is None
        or re.fullmatch(r"[0-9a-f]{64}", str(lease.get("snapshot_sha256") or ""))
        is None
    ):
        raise CurrentReviewLeaseError(
            "current review lease identity is invalid",
            reasons=("lease-identity",),
        )


def evaluate_review_lease(
    lease: Mapping[str, object],
    target: ReviewTarget,
    *,
    gate_status: str,
) -> tuple[dict[str, object], bool]:
    _validated_lease(lease, target)
    try:
        current = snapshot_clean(target)
    except ReviewTargetError as exc:
        reasons, head = _dirty_reasons(target)
        raise CurrentReviewLeaseError(
            "Lifecycle review resume requires a clean target; finish and commit "
            "the resolution before retrying",
            reasons=reasons,
            observed_head=head,
        ) from exc
    current_payload = _payload(current)
    if current_payload == dict(lease):
        return current_payload, False
    if gate_status == "changes-requested" and current.head != lease["head"]:
        return current_payload, True
    reasons = ("head",) if current.head != lease["head"] else ("snapshot",)
    raise CurrentReviewLeaseError(
        "current review target drifted while its exact-HEAD lease was active",
        reasons=reasons,
        observed_head=current.head,
    )


def assert_current_review_lease(
    meta: Mapping[str, Any], worktree: Path | str, *, gate_status: str
) -> None:
    if meta.get("lifecycle") != "current-checkout":
        return
    lease = meta.get("review_lease")
    if not isinstance(lease, Mapping):
        raise CurrentReviewLeaseError(
            "current review lease is unavailable",
            reasons=("lease-identity",),
        )
    target = resolve_target(worktree)
    _current, rebound = evaluate_review_lease(
        lease, target, gate_status=gate_status
    )
    if rebound:
        raise CurrentReviewLeaseError(
            "current review lease rebind was not persisted before review",
            reasons=("lease-rebind",),
        )


def lease_attention_payload(
    *,
    task_id: str,
    lease: Mapping[str, object],
    reasons: tuple[str, ...],
    observed_head: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "attention-required",
        "reason": "current-review-target-drift",
        "task_id": task_id,
        "target_key": str(lease.get("target_key") or ""),
        "expected_head": str(lease.get("head") or ""),
        "observed_head": observed_head,
        "drift": list(reasons),
    }


def enforce_current_review_lease(
    meta: Mapping[str, Any],
    worktree: Path,
    gate_path: Path,
    task_id: str,
) -> None:
    gate_status = ""
    if gate_path.is_file() and not gate_path.is_symlink():
        gate_status = str(
            _read_json(gate_path, "current review gate").get("status") or ""
        )
    try:
        assert_current_review_lease(
            meta, worktree, gate_status=gate_status
        )
    except CurrentReviewLeaseError as exc:
        lease = meta.get("review_lease")
        if isinstance(lease, Mapping):
            _atomic_json(
                gate_path.parent / "review-lease-attention.json",
                lease_attention_payload(
                    task_id=task_id,
                    lease=lease,
                    reasons=exc.reasons,
                    observed_head=exc.observed_head,
                ),
            )
        raise TaskReviewError(str(exc)) from exc
