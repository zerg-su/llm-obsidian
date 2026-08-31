"""Current-checkout review policy, identity, and scratch lifecycle."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from harness.review_program import (
    PURPOSES as REVIEW_PURPOSES,
    ReviewBoundaryInput,
    ReviewProgramError,
)
from harness.review_program_authority import (
    stale_resolution_boundary,
    trusted_review_receipt,
)
from harness.store import OperationStore
from harness.verification import load_profiles
from harness.workflows.review_gate import ReviewPreset
from current_review_admission import (
    current_admission_lock,
    freeze_current_plan_bytes,
    guard_active_plan,
    plan_rebind_allowed,
    publish_current_plan_snapshot,
    start_current_review,
    zero_effect_replacement_plan,
)
from current_review_lease import (
    CurrentReviewLeaseError,
    create_review_lease,
    evaluate_review_lease,
    lease_attention_payload,
    resolve_review_base,
)
from current_review_lineage import (
    new_lineage_supersedes_active as _new_lineage_supersedes_active,
    published_new_lineage_matches as _published_new_lineage_matches,
    same_requested_policy as _same_requested_policy,
)
from current_review_scope import (
    current_plan_identity,
)
from review_coordinator import CoordinatorError, resolve_coordinator
from review_target import ReviewTarget, ReviewTargetError, resolve_target
from task_review_context import (
    _current_review_is_quiescent,
    _zero_effect_attention_is_quiescent,
    _zero_effect_attention_shape,
    _current_runtime_root,
    _gate_root,
)
from task_review_identity import (
    _current_review_active_path,
    _legacy_current_review_active_path,
)
from task_review_flow import _run_review
from task_review_shared import (
    TaskReviewError,
    _atomic_json,
    _git,
    _load_review_boundary_input,
    _read_json,
)

if TYPE_CHECKING:
    from task_review_plan import PlanReviewCompilation


def _validate_current_checkout(worktree: Path) -> Path:
    try:
        return resolve_target(worktree).root
    except ReviewTargetError as exc:
        raise TaskReviewError(
            f"current review requires a Git worktree target: {exc}"
        ) from exc


def _active_path(vault: Path, worktree: Path, target_key: str) -> Path:
    scoped = _current_review_active_path(vault, target_key)
    if scoped.exists() or scoped.is_symlink():
        return scoped
    legacy = _legacy_current_review_active_path(vault)
    if legacy.is_file() and not legacy.is_symlink():
        candidate = _read_json(legacy, "legacy current review state")
        if (
            candidate.get("lifecycle") == "current-checkout"
            and candidate.get("worktree") == str(worktree)
        ):
            return legacy
    return scoped


def _current_policy(
    *,
    deep: bool,
    full: bool,
    cross_model: bool,
    runtime: str,
    model: str,
    effort: str,
    no_review: bool,
    profile_sha256: str,
    purpose: str = "implementation",
    boundary_input_sha256: str = "",
    base_sha: str = "",
) -> dict[str, Any]:
    if purpose not in REVIEW_PURPOSES:
        raise TaskReviewError("current review purpose is invalid")
    if no_review and (purpose != "implementation" or boundary_input_sha256):
        raise TaskReviewError("a purpose-bound review cannot be skipped")
    preset = ReviewPreset.from_flags(
        deep=deep,
        full=full,
        cross_model=cross_model,
        runtime=runtime,
        model=model,
        effort=effort,
        no_review=no_review,
    )
    mode = preset.depth if preset.enabled else "skip"
    max_verify_iterations = (
        0
        if mode == "skip" or purpose == "release"
        else min(preset.max_verify_iterations, 1)
        if purpose == "intent"
        else preset.max_verify_iterations
    )
    return {
        "mode": mode,
        "cross_model": cross_model,
        "runtime": runtime,
        "model": model,
        "effort": effort,
        "max_verify_iterations": max_verify_iterations,
        "verification_profile": "scoped",
        "verification_profile_sha256": profile_sha256,
        "purpose": purpose,
        "boundary_input_sha256": boundary_input_sha256,
        "base_sha": base_sha,
    }


def _current_review_artifact_root(
    worktree: Path,
    *,
    purpose: str,
    boundary_input_file: Path | None,
    plan_file: Path | None,
    artifact_root: Path | None,
) -> Path | None:
    """Bind release inputs to one explicit owner-controlled external root."""

    worktree = worktree.expanduser().resolve()
    if purpose != "release":
        if artifact_root is not None:
            raise TaskReviewError(
                "--artifact-root only applies to current release review"
            )
        return None
    if artifact_root is None:
        raise TaskReviewError(
            "current release review requires --artifact-root"
        )
    raw_root = artifact_root.expanduser()
    if not raw_root.is_absolute() or raw_root.is_symlink():
        raise TaskReviewError("current release artifact root is invalid")
    try:
        resolved_root = raw_root.resolve(strict=True)
    except OSError as exc:
        raise TaskReviewError(
            "current release artifact root is unavailable"
        ) from exc
    if (
        not resolved_root.is_dir()
        or resolved_root == worktree
        or worktree in resolved_root.parents
        or resolved_root in worktree.parents
    ):
        raise TaskReviewError("current release artifact root is invalid")
    for label, candidate in (
        ("boundary input", boundary_input_file),
        ("plan", plan_file),
    ):
        if candidate is None:
            raise TaskReviewError(
                f"current release review requires an exact {label}"
            )
        raw_candidate = candidate.expanduser()
        if raw_candidate.is_symlink():
            raise TaskReviewError(
                f"current release {label} is outside its artifact root"
            )
        try:
            resolved_candidate = raw_candidate.resolve(strict=True)
        except OSError as exc:
            raise TaskReviewError(
                f"current release {label} is unavailable"
            ) from exc
        if (
            not resolved_candidate.is_file()
            or resolved_root not in resolved_candidate.parents
        ):
            raise TaskReviewError(
                f"current release {label} is outside its artifact root"
            )
    return resolved_root


def _stopped_release_enters_implementation(
    stored: Mapping[str, Any] | object,
    requested: Mapping[str, Any],
    boundary: ReviewBoundaryInput | None,
    *,
    bound_head: str,
    current_head: str,
) -> bool:
    if (
        not isinstance(stored, Mapping)
        or boundary is None
        or not bound_head
        or bound_head == current_head
        or boundary.purpose != "implementation"
        or boundary.product_head_sha != current_head
    ):
        return False
    if (
        str(stored.get("purpose") or "implementation") != "release"
        or str(requested.get("purpose") or "implementation")
        != "implementation"
    ):
        return False
    stored_boundary = str(stored.get("boundary_input_sha256") or "")
    requested_boundary = str(requested.get("boundary_input_sha256") or "")
    return bool(
        stored_boundary
        and requested_boundary
        and stored_boundary != requested_boundary
    )


def _same_review_purpose(
    stored: Mapping[str, Any] | object,
    requested: Mapping[str, Any],
) -> bool:
    return isinstance(stored, Mapping) and str(
        stored.get("purpose") or "implementation"
    ) == str(requested.get("purpose") or "implementation")


def _approved_implementation_enters_release(
    worktree: Path,
    candidate: Mapping[str, Any],
    stored: Mapping[str, Any] | object,
    requested: Mapping[str, Any],
    boundary: ReviewBoundaryInput | None,
    *,
    operation_id: str,
    bound_head: str,
    current_head: str,
) -> bool:
    """Trust only the exact approved implementation checkpoint for release."""

    if (
        not isinstance(stored, Mapping)
        or boundary is None
        or boundary.purpose != "release"
        or str(stored.get("purpose") or "implementation") != "implementation"
        or str(requested.get("purpose") or "implementation") != "release"
        or not bound_head
        or bound_head != current_head
        or boundary.integration_head_sha != current_head
    ):
        return False
    source = Path(str(candidate.get("review_boundary_input_file") or ""))
    try:
        implementation = _load_review_boundary_input(
            source, purpose="implementation"
        )
        receipt = trusted_review_receipt(
            worktree, implementation, operation_id
        )
    except (OSError, ReviewProgramError, TaskReviewError):
        return False
    return bool(
        receipt.verdict == "approved"
        and receipt.boundary_input_sha256
        == str(stored.get("boundary_input_sha256") or "")
        and implementation.plan_sha256 == boundary.plan_sha256
        and implementation.outcome_contract_sha256
        == boundary.outcome_contract_sha256
    )


def _require_new_lineage_source(new_lineage: bool, message: str) -> None:
    if new_lineage:
        raise TaskReviewError(message)


def _active_current_review(
    worktree: Path,
    vault: Path,
    active_path: Path,
    requested_policy: Mapping[str, Any],
    boundary_input: ReviewBoundaryInput | None,
    *,
    plan_file: Path | None,
    plan_compilation: PlanReviewCompilation | None,
    plan_base_sha: str,
    plan_head_sha: str,
    scratch_root: Path | None,
    artifact_root: Path | None,
    new_lineage: bool,
) -> dict[str, Any] | None:
    if not active_path.is_file() or active_path.is_symlink():
        _require_new_lineage_source(
            new_lineage,
            "--new-lineage requires an exhausted active current review",
        )
        return None
    candidate = _read_json(active_path, "current review state")
    if (
        candidate.get("lifecycle") != "current-checkout"
        or candidate.get("worktree") != str(worktree)
    ):
        raise TaskReviewError("current review state belongs to another checkout")
    try:
        task_id = str(uuid.UUID(str(candidate.get("task_id") or "")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise TaskReviewError("current review identity is invalid") from exc
    gate_state_path = _gate_root(vault, task_id) / "review-gate.json"
    stored_policy = candidate.get("review_policy")
    same_policy = isinstance(stored_policy, dict) and _same_requested_policy(
        stored_policy, requested_policy
    )
    terminal_stale = False
    may_rebind_plan = False
    if gate_state_path.is_file() and not gate_state_path.is_symlink():
        gate_state = _read_json(gate_state_path, "current review gate")
        status = str(gate_state.get("status") or "")
        bound = gate_state.get("context")
        bound_head = str(bound.get("head_sha") or "") if isinstance(bound, dict) else ""
        current_head = _git(worktree, "rev-parse", "HEAD")
        requested_purpose = str(
            requested_policy.get("purpose") or "implementation"
        )
        if (
            plan_compilation is not None
            and not same_policy
            and isinstance(candidate.get("plan_review"), Mapping)
        ):
            from task_review_plan import guard_active_protected_artifacts

            candidate_runtime_root = Path(
                str(candidate.get("runtime_root") or "")
            ).resolve()
            expected_runtime_root = _current_runtime_root(
                worktree, task_id, scratch_root
            )
            if candidate_runtime_root != expected_runtime_root:
                raise TaskReviewError(
                    "current review scratch root changed during plan rebind"
                )
            guard_active_protected_artifacts(
                candidate_runtime_root,
                candidate,
                plan_compilation,
            )
        if (
            plan_compilation is not None
            and not same_policy
            and status == "awaiting-resolution"
        ):
            from task_review_plan import rebind_active_plan_review

            candidate = rebind_active_plan_review(
                worktree,
                active_path,
                candidate,
                gate_state,
                requested_policy,
                plan_compilation,
                requested_base_sha=plan_base_sha,
                requested_head_sha=plan_head_sha,
            )
            stored_policy = candidate.get("review_policy")
            same_policy = isinstance(
                stored_policy, dict
            ) and _same_requested_policy(stored_policy, requested_policy)
        approved_stale = status == "approved" and (
            _approved_implementation_enters_release(
                worktree,
                candidate,
                stored_policy,
                requested_policy,
                boundary_input,
                operation_id=task_id,
                bound_head=bound_head,
                current_head=current_head,
            )
            if requested_purpose == "release"
            else bound_head != current_head or not same_policy
        )
        skipped_stale = (
            status == "skipped"
            and requested_purpose != "release"
            and (bound_head != current_head or not same_policy)
        )
        operation_quiescent = _current_review_is_quiescent(vault, task_id)
        zero_effect_quiescent = _zero_effect_attention_is_quiescent(
            vault, task_id, gate_state
        )
        quiescent = operation_quiescent or zero_effect_quiescent
        terminal_stale = _new_lineage_supersedes_active(
            candidate,
            vault,
            worktree,
            task_id,
            new_lineage=new_lineage,
            same_policy=same_policy,
            status=status,
            bound_head=bound_head,
            current_head=current_head,
            operation_quiescent=operation_quiescent,
        )
        if (
            _zero_effect_attention_shape(gate_state)
            and not quiescent
        ):
            raise TaskReviewError(
                "zero-effect current review retains operation ownership"
            )
        bounded_current_resolution = (
            status == "changes-requested"
            and isinstance(candidate.get("finalization_policy"), Mapping)
        )
        if bounded_current_resolution and not same_policy:
            same_policy = isinstance(
                stored_policy, Mapping
            ) and _same_requested_policy(
                stored_policy,
                requested_policy,
                allow_boundary_rebind=True,
            )
        stored_plan = Path(str(candidate.get("plan_file") or "")).resolve()
        requested_plan = (
            plan_file.expanduser().resolve() if plan_file is not None else None
        )
        may_rebind_plan = plan_rebind_allowed(
            stored_plan=stored_plan,
            requested_plan=requested_plan,
            requested_purpose=requested_purpose,
            boundary_input=boundary_input,
            status=status,
            bound_head=bound_head,
            current_head=current_head,
            same_policy=same_policy,
        )
        terminal_stale = terminal_stale or approved_stale or skipped_stale or (
            status == "stopped"
            and _stopped_release_enters_implementation(
                stored_policy,
                requested_policy,
                boundary_input,
                bound_head=bound_head,
                current_head=current_head,
            )
        ) or (status == "attention-required" and quiescent) or (
            status in {"pending", "reviewing", "verifying"}
            and not gate_state.get("round_results")
            and not gate_state.get("final_results")
            and _same_review_purpose(stored_policy, requested_policy)
            and quiescent
        ) or (
            not bounded_current_resolution
            and stale_resolution_boundary(
                status,
                bound_head,
                current_head,
                quiescent,
            )
        )
    elif gate_state_path.exists():
        raise TaskReviewError("current review gate is not a regular file")
    else:
        # The active pointer is written before the gate is materialized.  If a
        # preflight exception stopped that launch, a later invocation must not
        # treat the zero-effect pointer as live ownership forever.  Supersede
        # it only when the exact owner has no operation rows; any persisted
        # row remains fail-closed and requires normal lifecycle recovery.
        if OperationStore(vault / ".vault-meta" / "harness").list(task_id):
            raise TaskReviewError(
                "current review ownership exists before gate materialization"
            )
        _require_new_lineage_source(
            new_lineage,
            "--new-lineage requires an exhausted material review",
        )
        terminal_stale = True
    if terminal_stale:
        return None
    if not same_policy:
        raise TaskReviewError(
            "an active current review uses another preset or override"
        )
    stored_artifact_root = str(candidate.get("review_artifact_root") or "")
    requested_artifact_root = str(artifact_root) if artifact_root else ""
    if stored_artifact_root != requested_artifact_root:
        raise TaskReviewError(
            "an active current review uses another artifact root"
        )
    guard_active_plan(
        candidate.get("plan_file"),
        plan_file.expanduser().resolve() if plan_file is not None else None,
        allow_rebind=may_rebind_plan,
    )
    if isinstance(candidate.get("plan_review"), Mapping):
        from task_review_plan import finalize_active_plan_rebind

        candidate = finalize_active_plan_rebind(candidate, active_path)
    return candidate


def _resume_current_review(
    meta: Mapping[str, Any],
    target: ReviewTarget,
    vault: Path,
    worktree: Path,
    active_path: Path,
    scratch_root: Path | None,
    boundary_input: ReviewBoundaryInput | None,
    requested_policy: Mapping[str, Any],
    plan_file: Path | None,
) -> tuple[dict[str, Any], str, Path]:
    task_id = str(meta["task_id"])
    runtime_root = Path(str(meta.get("runtime_root") or "")).resolve()
    expected_root = _current_runtime_root(worktree, task_id, scratch_root)
    if runtime_root != expected_root:
        raise TaskReviewError(
            "current review scratch root changed during an active gate"
        )
    if (
        runtime_root == worktree
        or worktree in runtime_root.parents
        or not (runtime_root / "current-review.json").is_file()
    ):
        raise TaskReviewError("current review scratch is unavailable")
    gate_path = _gate_root(vault, task_id) / "review-gate.json"
    gate_status = ""
    gate_context: Mapping[str, Any] | None = None
    if gate_path.is_file() and not gate_path.is_symlink():
        gate_state = _read_json(gate_path, "current review gate")
        gate_status = str(gate_state.get("status") or "")
        raw_context = gate_state.get("context")
        gate_context = raw_context if isinstance(raw_context, Mapping) else None
    stored_plan = Path(str(meta.get("plan_file") or "")).resolve()
    effective_plan = (
        plan_file.expanduser().resolve() if plan_file is not None else stored_plan
    )
    plan_sha256, outcome_sha256 = current_plan_identity(effective_plan)
    raw_lease = meta.get("review_lease")
    if not isinstance(raw_lease, Mapping):
        try:
            review_lease = create_review_lease(
                target,
                base_sha=str(requested_policy.get("base_sha") or ""),
            )
        except CurrentReviewLeaseError as exc:
            raise TaskReviewError(str(exc)) from exc
        bound_head = str(gate_context.get("head_sha") or "") if gate_context else ""
        if (
            bound_head
            and bound_head != review_lease["head"]
            and gate_status != "changes-requested"
        ):
            raise TaskReviewError(
                "legacy current review target drifted from its bound HEAD"
            )
        lease_changed = True
    else:
        try:
            review_lease, lease_changed = evaluate_review_lease(
                raw_lease, target, gate_status=gate_status
            )
        except CurrentReviewLeaseError as exc:
            if gate_status != "changes-requested" and gate_path.parent.exists():
                _atomic_json(
                    gate_path.parent / "review-lease-attention.json",
                    lease_attention_payload(
                        task_id=task_id,
                        lease=raw_lease,
                        reasons=exc.reasons,
                        observed_head=exc.observed_head,
                    ),
                )
            raise TaskReviewError(str(exc)) from exc
    explicit_amendment = bool(
        plan_file is not None
        and gate_status == "changes-requested"
        and lease_changed
        and boundary_input is None
    )
    if not explicit_amendment and (
        plan_sha256 != str(meta.get("approved_plan_sha256") or "")
        or outcome_sha256 != str(meta.get("outcome_contract_sha256") or "")
    ):
        raise TaskReviewError("current review plan changed without amendment")
    if lease_changed:
        updates: dict[str, Any] = {"review_lease": review_lease}
        if boundary_input is not None and gate_status == "changes-requested":
            frozen_plan = publish_current_plan_snapshot(
                runtime_root,
                freeze_current_plan_bytes(
                    effective_plan, boundary_input.plan_sha256
                ),
                boundary_input.plan_sha256,
            )
            stored_boundary = runtime_root / "inputs/review-boundary-input.json"
            _atomic_json(stored_boundary, boundary_input.payload())
            updates.update(
                {
                    "review_policy": requested_policy,
                    "review_boundary_input_file": str(stored_boundary),
                    "plan_snapshot_file": str(frozen_plan),
                    "approved_plan_sha256": boundary_input.plan_sha256,
                    "outcome_contract_sha256": (
                        boundary_input.outcome_contract_sha256
                    ),
                }
            )
        elif boundary_input is None and gate_status == "changes-requested":
            frozen_plan = publish_current_plan_snapshot(
                runtime_root,
                freeze_current_plan_bytes(effective_plan, plan_sha256),
                plan_sha256,
            )
            updates.update(
                {
                    "plan_file": str(effective_plan),
                    "plan_snapshot_file": str(frozen_plan),
                    "approved_plan_sha256": plan_sha256,
                    "outcome_contract_sha256": outcome_sha256,
                }
            )
        meta = {**meta, **updates}
        _atomic_json(runtime_root / "current-review.json", meta)
        _atomic_json(active_path, meta)
    return dict(meta), task_id, runtime_root


def _admit_and_run_current_review(
    worktree: Path,
    target: ReviewTarget,
    vault: Path,
    scoped_active_path: Path,
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
    new_lineage: bool,
    scratch_root: Path | None,
    runtime_manager: object | None,
    apply_finalizing_recovery: Callable[..., dict[str, Any]],
    plan_compilation: PlanReviewCompilation | None,
    plan_base_sha: str,
    plan_head_sha: str,
) -> dict[str, Any]:
    observed_path = _active_path(vault, worktree, target.target_key)
    observed_task_id = ""
    if observed_path.is_file() and not observed_path.is_symlink():
        observed = _read_json(observed_path, "current review state")
        observed_task_id = str(observed.get("task_id") or "")
    with current_admission_lock(scoped_active_path):
        active_path = _active_path(vault, worktree, target.target_key)
        predecessor: dict[str, Any] | None = None
        if active_path.is_file() and not active_path.is_symlink():
            predecessor = _read_json(active_path, "current review state")
        effective_new_lineage = new_lineage
        if new_lineage and observed_task_id and active_path.is_file():
            current = _read_json(active_path, "current review state")
            current_task_id = str(current.get("task_id") or "")
            if (
                (
                    current_task_id != observed_task_id
                    and str(current.get("supersedes_task_id") or "")
                    == observed_task_id
                )
                or _published_new_lineage_matches(
                    current,
                    vault,
                    worktree,
                    target.target_key,
                    requested_policy,
                    plan_identity,
                    scratch_root,
                )
            ):
                effective_new_lineage = False
        meta = _active_current_review(
            worktree,
            vault,
            active_path,
            requested_policy,
            boundary_input,
            plan_file=plan_file,
            plan_compilation=plan_compilation,
            plan_base_sha=plan_base_sha,
            plan_head_sha=plan_head_sha,
            scratch_root=scratch_root,
            artifact_root=artifact_root,
            new_lineage=effective_new_lineage,
        )
        if meta is None:
            effective_plan_file = plan_file
            effective_plan_identity = plan_identity
            if effective_plan_file is None and predecessor is not None:
                stored_policy = predecessor.get("review_policy")
                inherited = zero_effect_replacement_plan(
                    predecessor,
                    vault,
                    current_head=_git(worktree, "rev-parse", "HEAD"),
                    same_policy=(
                        isinstance(stored_policy, Mapping)
                        and _same_requested_policy(
                            stored_policy,
                            requested_policy,
                        )
                    ),
                )
                if inherited is not None:
                    effective_plan_file, effective_plan_identity = inherited
            meta, task_id, runtime_root = start_current_review(
                worktree,
                target,
                vault,
                active_path,
                requested_policy,
                boundary_input,
                base_sha=base_sha,
                purpose=purpose,
                boundary_input_file=boundary_input_file,
                plan_file=effective_plan_file,
                plan_identity=effective_plan_identity,
                artifact_root=artifact_root,
                origin_surface=origin_surface,
                supersedes_task_id=(
                    observed_task_id if new_lineage else ""
                ),
                scratch_root=scratch_root,
                plan_compilation=plan_compilation,
                plan_base_sha=plan_base_sha,
                plan_head_sha=plan_head_sha,
            )
        else:
            meta, task_id, runtime_root = _resume_current_review(
                meta,
                target,
                vault,
                worktree,
                active_path,
                scratch_root,
                boundary_input,
                requested_policy,
                plan_file,
            )
        return _run_review(
            meta,
            vault,
            worktree,
            task_id,
            runtime_root,
            runtime_manager=runtime_manager,
            apply_finalizing_recovery=apply_finalizing_recovery,
        )


def run_current_review(
    worktree: Path,
    *,
    vault_root: Path | None = None,
    base: str = "",
    deep: bool = False,
    full: bool = False,
    cross_model: bool = False,
    runtime: str = "",
    model: str = "",
    effort: str = "",
    no_review: bool = False,
    purpose: str = "implementation",
    boundary_input_file: Path | None = None,
    artifact_root: Path | None = None,
    plan_file: Path | None = None,
    origin_surface: str = "",
    new_lineage: bool = False,
    scratch_root: Path | None = None,
    runtime_manager: object | None = None,
    apply_finalizing_recovery: Callable[..., dict[str, Any]],
    plan_compilation: PlanReviewCompilation | None = None,
    plan_base_sha: str = "",
    plan_head_sha: str = "",
) -> dict[str, Any]:
    worktree = _validate_current_checkout(worktree)
    target = resolve_target(worktree)
    if base and purpose != "implementation":
        raise TaskReviewError(
            "--base applies only to current implementation review"
        )
    try:
        base_sha = resolve_review_base(target, base)
    except CurrentReviewLeaseError as exc:
        raise TaskReviewError(str(exc)) from exc
    try:
        vault = resolve_coordinator(
            explicit=vault_root,
            cwd=worktree,
        ).root
    except CoordinatorError as exc:
        raise TaskReviewError(str(exc)) from exc
    artifact_root = _current_review_artifact_root(
        worktree,
        purpose=purpose,
        boundary_input_file=boundary_input_file,
        plan_file=plan_file,
        artifact_root=artifact_root,
    )
    if plan_compilation is not None:
        from task_review_plan import GIT_OID

        if (
            purpose != "intent"
            or boundary_input_file is not None
            or plan_file is None
            or plan_compilation.worktree != worktree
            or plan_compilation.plan_path != plan_file.expanduser().resolve()
            or not GIT_OID.fullmatch(plan_base_sha)
            or not GIT_OID.fullmatch(plan_head_sha)
            or _git(worktree, "rev-parse", "HEAD") != plan_head_sha
        ):
            raise TaskReviewError("plan facade boundary is invalid")
    profiles = load_profiles(vault / "config/verification-profiles.toml")
    profile = profiles.get("scoped")
    if profile is None:
        raise TaskReviewError("scoped verification profile is unavailable")
    boundary_input = (
        plan_compilation.boundary()
        if plan_compilation is not None
        else _load_review_boundary_input(boundary_input_file, purpose=purpose)
        if boundary_input_file is not None
        else None
    )
    if boundary_input is None and purpose != "implementation":
        raise TaskReviewError(
            "intent and release review require --boundary-input"
        )
    requested_policy = _current_policy(
        deep=deep,
        full=full,
        cross_model=cross_model,
        runtime=runtime,
        model=model,
        effort=effort,
        no_review=no_review,
        profile_sha256=profile.sha256,
        purpose=purpose,
        boundary_input_sha256=(
            boundary_input.input_sha256 if boundary_input else ""
        ),
        base_sha=base_sha,
    )
    resolved_plan: Path | None = None
    plan_identity: tuple[str, str] | None = None
    if plan_file is not None:
        resolved_plan = plan_file.expanduser().resolve()
        if not resolved_plan.is_file() or plan_file.expanduser().is_symlink():
            raise TaskReviewError("current review plan is unavailable")
        plan_identity = current_plan_identity(resolved_plan)
    scoped_active_path = _current_review_active_path(vault, target.target_key)
    return _admit_and_run_current_review(
        worktree,
        target,
        vault,
        scoped_active_path,
        requested_policy,
        boundary_input,
        base_sha=base_sha,
        purpose=purpose,
        boundary_input_file=boundary_input_file,
        plan_file=resolved_plan,
        plan_identity=plan_identity,
        artifact_root=artifact_root,
        origin_surface=origin_surface,
        new_lineage=new_lineage,
        scratch_root=scratch_root,
        runtime_manager=runtime_manager,
        apply_finalizing_recovery=apply_finalizing_recovery,
        plan_compilation=plan_compilation,
        plan_base_sha=plan_base_sha,
        plan_head_sha=plan_head_sha,
    )
