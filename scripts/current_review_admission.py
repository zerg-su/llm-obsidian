"""Linearizable current-review admission and fresh-owner materialization."""

from __future__ import annotations

import fcntl
import hashlib
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
from task_review_shared import (
    TaskReviewError,
    _atomic_bytes,
    _atomic_json,
    _git,
    _read_json,
)


class _RetainCurrentReviewScratch(TaskReviewError):
    """Pointer durability is uncertain, so deleting its scratch is unsafe."""


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


def freeze_current_plan_bytes(plan: Path, expected_sha256: str) -> bytes:
    """Capture exact plan bytes before allocating or publishing an owner."""

    try:
        content = plan.read_bytes()
    except OSError as exc:
        raise TaskReviewError("current review plan snapshot is unavailable") from exc
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise TaskReviewError("current review plan changed before owner publication")
    return content


def publish_current_plan_snapshot(
    runtime_root: Path, content: bytes, expected_sha256: str
) -> Path:
    """Publish captured plan bytes below their exact owner scratch."""

    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise TaskReviewError("current review plan snapshot digest changed")
    path = (
        runtime_root
        / "inputs"
        / "approved-plans"
        / f"{expected_sha256}.md"
    )
    _atomic_bytes(path, content)
    return path


def _active_pointer_snapshot(active_path: Path) -> bytes | None:
    """Capture the predecessor pointer before any owner scratch exists."""

    if active_path.is_symlink():
        raise TaskReviewError("current review active pointer is not a regular file")
    if active_path.is_file():
        try:
            return active_path.read_bytes()
        except OSError as exc:
            raise TaskReviewError(
                "current review active pointer is unavailable"
            ) from exc
    if active_path.exists():
        raise TaskReviewError("current review active pointer is not a regular file")
    return None


def _durable_unlink(path: Path) -> None:
    path.unlink()
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _rollback_active_pointer(
    active_path: Path,
    predecessor: bytes | None,
    *,
    published: bytes,
) -> bool:
    """Restore the predecessor only while the pointer still names this owner."""

    if _active_pointer_snapshot(active_path) != published:
        return False
    if predecessor is None:
        _durable_unlink(active_path)
    else:
        _atomic_bytes(active_path, predecessor)
    return _active_pointer_snapshot(active_path) == predecessor


def _publish_active_pointer(
    active_path: Path,
    meta: Mapping[str, Any],
    predecessor: bytes | None,
    *,
    published: bytes,
) -> None:
    """Publish the commit point or restore its exact predecessor durably."""

    try:
        _atomic_json(active_path, meta)
    except BaseException as publication_error:
        try:
            current = _active_pointer_snapshot(active_path)
        except TaskReviewError as inspection_error:
            raise _RetainCurrentReviewScratch(
                "current review active pointer publication is uncertain; "
                "scratch retained for recovery"
            ) from inspection_error
        if current == published:
            try:
                restored = _rollback_active_pointer(
                    active_path,
                    predecessor,
                    published=published,
                )
            except BaseException as rollback_error:
                raise _RetainCurrentReviewScratch(
                    "current review active pointer rollback failed; "
                    "scratch retained for recovery"
                ) from rollback_error
            if not restored:
                raise _RetainCurrentReviewScratch(
                    "current review active pointer rollback is uncertain; "
                    "scratch retained for recovery"
                ) from publication_error
        elif current != predecessor:
            raise _RetainCurrentReviewScratch(
                "current review active pointer publication is uncertain; "
                "scratch retained for recovery"
            ) from publication_error
        raise TaskReviewError(
            "current review active pointer publication failed"
        ) from publication_error


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
    bound_plan_sha256 = (
        boundary_input.plan_sha256
        if boundary_input is not None
        else approved_plan_sha256
    )
    frozen_plan_bytes = freeze_current_plan_bytes(plan, bound_plan_sha256)
    task_id = str(uuid.uuid4())
    runtime_path = _current_runtime_path(worktree, task_id, scratch_root)
    frozen_plan = (
        runtime_path
        / "inputs"
        / "approved-plans"
        / f"{bound_plan_sha256}.md"
    )
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
        "plan_snapshot_file": str(frozen_plan),
        "routing": {"session": session},
        "review_policy": requested_policy,
        "runtime_root": str(runtime_path),
        "approved_plan_sha256": bound_plan_sha256,
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
    predecessor_pointer = _active_pointer_snapshot(active_path)
    runtime_root = runtime_path
    try:
        runtime_root = _current_runtime_root(worktree, task_id, scratch_root)
        published_plan = publish_current_plan_snapshot(
            runtime_root, frozen_plan_bytes, bound_plan_sha256
        )
        if published_plan != frozen_plan:
            raise TaskReviewError("current review plan snapshot path changed")
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
        owner_meta_path = runtime_root / "current-review.json"
        _atomic_json(owner_meta_path, meta)
        _publish_active_pointer(
            active_path,
            meta,
            predecessor_pointer,
            published=owner_meta_path.read_bytes(),
        )
    except _RetainCurrentReviewScratch:
        raise
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
