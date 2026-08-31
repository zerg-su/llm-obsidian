"""Linearizable current-review admission and fresh-owner materialization."""

from __future__ import annotations

import fcntl
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from current_review_lease import CurrentReviewLeaseError, create_review_lease
from harness.review_program import ReviewBoundaryInput
from harness.workflows.review import ReviewContext
from model_routing import load_config, routing_from_environment
from review_target import ReviewTarget
from task_review_context import _current_runtime_root, _request
from task_review_shared import TaskReviewError, _atomic_json, _git


@contextmanager
def current_admission_lock(scoped_active_path: Path) -> Iterator[None]:
    """Serialize one target from active-pointer read through provider admission."""

    parent = scoped_active_path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent.chmod(0o700)
    lock_path = parent / ".admission.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise TaskReviewError(
            "current review admission lock is unavailable"
        ) from exc
    with os.fdopen(descriptor, "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def plan_rebind_allowed(
    *,
    stored_plan: Path,
    requested_plan: Path | None,
    requested_purpose: str,
    boundary_input: ReviewBoundaryInput | None,
    status: str,
    bound_head: str,
    current_head: str,
    same_policy: bool,
) -> bool:
    """Admit only an explicit changed-HEAD implementation amendment."""

    return bool(
        requested_plan is not None
        and requested_plan != stored_plan
        and requested_purpose == "implementation"
        and boundary_input is None
        and status == "changes-requested"
        and bound_head
        and bound_head != current_head
        and same_policy
    )


def guard_active_plan(
    stored_plan: object,
    requested_plan: Path | None,
    *,
    allow_rebind: bool,
) -> None:
    """Reject a plan-path switch outside the explicit amendment boundary."""

    if (
        requested_plan is None
        or Path(str(stored_plan or "")).resolve() == requested_plan
        or allow_rebind
    ):
        return
    raise TaskReviewError("an active current review uses another plan")


def start_current_review(
    worktree: Path,
    target: ReviewTarget,
    vault: Path,
    active_path: Path,
    requested_policy: Mapping[str, Any],
    boundary_input: ReviewBoundaryInput | None,
    *,
    base_sha: str,
    purpose: str,
    boundary_input_file: Path | None,
    plan_file: Path | None,
    plan_identity: tuple[str, str] | None,
    artifact_root: Path | None,
    origin_surface: str,
    supersedes_task_id: str,
    scratch_root: Path | None,
    plan_compilation: Any | None,
    plan_base_sha: str,
    plan_head_sha: str,
) -> tuple[dict[str, Any], str, Path]:
    """Create one fresh owner while the caller holds its target lock."""

    if plan_file is None or plan_identity is None:
        raise TaskReviewError(
            "a new current review requires --plan with a behavior-specific "
            "Outcome Contract"
        )
    try:
        review_lease = create_review_lease(target, base_sha=base_sha)
    except CurrentReviewLeaseError as exc:
        raise TaskReviewError(str(exc)) from exc
    current_head = _git(worktree, "rev-parse", "HEAD")
    if boundary_input is not None and (
        (
            purpose == "implementation"
            and boundary_input.product_head_sha != current_head
        )
        or (
            purpose == "release"
            and boundary_input.integration_head_sha != current_head
        )
    ):
        raise TaskReviewError("review boundary input targets another HEAD")
    plan = plan_file.expanduser().resolve()
    approved_plan_sha256, outcome_contract_sha256 = plan_identity
    task_id = str(uuid.uuid4())
    runtime_root = _current_runtime_root(worktree, task_id, scratch_root)
    surface = (
        origin_surface.strip()
        or str(os.environ.get("CMUX_SURFACE_ID") or "").strip()
    )
    if not surface:
        raise TaskReviewError(
            "current review requires an exact cmux origin surface"
        )
    config = load_config(vault)
    session, source = routing_from_environment(config)
    if source == "tracked-default":
        raise TaskReviewError(
            "current review requires a host-confirmed current session route"
        )
    session = {**session, "source": source}
    meta: dict[str, Any] = {
        "version": 4,
        "lifecycle": "current-checkout",
        "task_id": task_id,
        "task_name": "current checkout review",
        "task_surface": surface,
        "worktree": str(worktree),
        "vault_root": str(vault),
        "target_key": target.target_key,
        "review_lease": review_lease,
        "plan_file": str(plan),
        "routing": {"session": session},
        "review_policy": requested_policy,
        "runtime_root": str(runtime_root),
        "approved_plan_sha256": (
            boundary_input.plan_sha256
            if boundary_input is not None
            else approved_plan_sha256
        ),
        "outcome_contract_sha256": (
            boundary_input.outcome_contract_sha256
            if boundary_input is not None
            else outcome_contract_sha256
        ),
        "finalization_policy": {
            "max_cycles": 5,
            "add_independent_model_after": 3,
            "primary_route_alias": "finalization-primary",
            "independent_route_alias": "finalization-independent",
            "execution": "ephemeral",
        },
    }
    if artifact_root is not None:
        meta["review_artifact_root"] = str(artifact_root)
    if supersedes_task_id:
        meta["supersedes_task_id"] = supersedes_task_id
    if boundary_input is not None:
        if purpose == "release" and boundary_input_file is not None:
            meta["review_boundary_input_source_file"] = str(
                boundary_input_file.expanduser().resolve()
            )
        if plan_compilation is not None:
            from task_review_plan import materialize_plan_review

            boundary_input = materialize_plan_review(
                runtime_root,
                plan_compilation,
                base_sha=plan_base_sha,
                head_sha=plan_head_sha,
            )
            meta["plan_review"] = {
                "schema_version": 1,
                "base_sha": plan_base_sha,
                "head_sha": plan_head_sha,
                "plan_relative_path": plan_compilation.plan_relative_path,
                "artifact_root": "runtime",
            }
        stored_boundary = runtime_root / "inputs" / "review-boundary-input.json"
        _atomic_json(stored_boundary, boundary_input.payload())
        meta["review_boundary_input_file"] = str(stored_boundary)
    _request(
        meta,
        vault,
        task_id,
        ReviewContext(
            "pending/manifest.json",
            current_head,
            "scoped",
            str(requested_policy["verification_profile_sha256"]),
            "",
            purpose,
            boundary_input.input_sha256 if boundary_input else "",
        ),
    )
    _atomic_json(runtime_root / "current-review.json", meta)
    _atomic_json(active_path, meta)
    return meta, task_id, runtime_root
