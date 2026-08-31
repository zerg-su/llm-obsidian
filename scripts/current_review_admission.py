"""Linearizable current-review admission and fresh-owner materialization."""

from __future__ import annotations

import fcntl
import os
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from current_review_lease import CurrentReviewLeaseError, create_review_lease
from current_review_scope import current_plan_identity
from harness.review_program import ReviewBoundaryInput
from harness.store import OperationStore
from harness.workflows.review import ReviewContext
from model_routing import load_config, routing_from_environment
from review_target import ReviewTarget
from task_review_context import (
    _current_review_is_quiescent,
    _current_runtime_root,
    _gate_root,
    _request,
    _zero_effect_attention_is_quiescent,
    _zero_effect_attention_shape,
)
from task_review_identity import _current_runtime_path
from task_review_shared import TaskReviewError, _atomic_json, _git, _read_json


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


def zero_effect_replacement_plan(
    predecessor: Mapping[str, Any],
    vault: Path,
    *,
    current_head: str,
    same_policy: bool,
) -> tuple[Path, tuple[str, str]] | None:
    """Reuse the exact stored plan only for a same-HEAD zero-effect retry."""

    lease = predecessor.get("review_lease")
    if (
        not same_policy
        or not isinstance(lease, Mapping)
        or str(lease.get("head") or "") != current_head
    ):
        return None
    try:
        task_id = str(uuid.UUID(str(predecessor.get("task_id") or "")))
    except (ValueError, TypeError, AttributeError):
        return None
    gate_path = _gate_root(vault, task_id) / "review-gate.json"
    if gate_path.is_file() and not gate_path.is_symlink():
        gate = _read_json(gate_path, "current review gate")
        if (
            str(gate.get("status") or "") != "attention-required"
            or not _zero_effect_attention_shape(gate)
            or not (
                _current_review_is_quiescent(vault, task_id)
                or _zero_effect_attention_is_quiescent(vault, task_id, gate)
            )
        ):
            return None
    elif gate_path.exists() or gate_path.is_symlink():
        raise TaskReviewError("current review gate is not a regular file")
    elif OperationStore(vault / ".vault-meta" / "harness").list(task_id):
        return None
    stored_plan = Path(str(predecessor.get("plan_file") or "")).expanduser()
    if not stored_plan.is_file() or stored_plan.is_symlink():
        raise TaskReviewError("stored current review plan is unavailable")
    resolved_plan = stored_plan.resolve()
    identity = current_plan_identity(resolved_plan)
    if (
        str(predecessor.get("approved_plan_sha256") or "") != identity[0]
        or str(predecessor.get("outcome_contract_sha256") or "")
        != identity[1]
    ):
        raise TaskReviewError("stored current review plan identity changed")
    return resolved_plan, identity


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
            "a new current review requires --plan with a behavior-bound "
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
    plan = plan_file.expanduser().resolve()
    approved_plan_sha256, outcome_contract_sha256 = plan_identity
    task_id = str(uuid.uuid4())
    runtime_path = _current_runtime_path(worktree, task_id, scratch_root)
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
        "runtime_root": str(runtime_path),
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
    runtime_root = _current_runtime_root(worktree, task_id, scratch_root)
    try:
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
        _atomic_json(runtime_root / "current-review.json", meta)
        _atomic_json(active_path, meta)
    except BaseException:
        try:
            if runtime_root.is_dir() and not runtime_root.is_symlink():
                shutil.rmtree(runtime_root)
        except OSError as cleanup_error:
            raise TaskReviewError(
                "unpublished current review scratch cleanup failed"
            ) from cleanup_error
        raise
    return meta, task_id, runtime_root
