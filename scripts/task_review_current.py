"""Current-checkout review policy, identity, and scratch lifecycle."""

from __future__ import annotations

import os
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
from harness.workflows.review import ReviewContext
from harness.workflows.review_gate import ReviewPreset
from model_routing import load_config, routing_from_environment
from task_review_context import (
    _current_review_is_quiescent,
    _current_runtime_root,
    _gate_root,
    _request,
)
from task_review_flow import _run_review
from task_review_shared import (
    TaskReviewError,
    _atomic_json,
    _atomic_text,
    _git,
    _load_review_boundary_input,
    _read_json,
)

if TYPE_CHECKING:
    from task_review_plan import PlanReviewCompilation


def _validate_current_checkout(worktree: Path) -> Path:
    worktree = worktree.expanduser().resolve()
    required = (
        worktree / "wiki",
        worktree / "scripts",
        worktree / "skills/review/SKILL.md",
        worktree / "config/model-routing.toml",
        worktree / "config/verification-profiles.toml",
    )
    if (
        not worktree.is_dir()
        or any(not path.exists() for path in required)
        or _git(worktree, "rev-parse", "--show-toplevel") != str(worktree)
    ):
        raise TaskReviewError(
            "current review requires an exact llm-obsidian checkout root"
        )
    return worktree


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
    }


def _same_requested_policy(
    stored: Mapping[str, Any],
    requested: Mapping[str, Any],
) -> bool:
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
        and str(stored.get("boundary_input_sha256") or "")
        == str(requested.get("boundary_input_sha256") or "")
    )


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
) -> dict[str, Any] | None:
    if not active_path.is_file() or active_path.is_symlink():
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
        quiescent = _current_review_is_quiescent(vault, task_id)
        terminal_stale = approved_stale or skipped_stale or (
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
        ) or stale_resolution_boundary(
            status,
            bound_head,
            current_head,
            quiescent,
        )
    elif gate_state_path.exists():
        raise TaskReviewError("current review gate is not a regular file")
    else:
        # The active pointer is written before the gate is materialized.  If a
        # preflight exception stopped that launch, a later invocation must not
        # treat the zero-effect pointer as live ownership forever.  Supersede
        # it only when the exact owner has no operation rows; any persisted
        # row remains fail-closed and requires normal lifecycle recovery.
        terminal_stale = not OperationStore(
            vault / ".vault-meta" / "harness"
        ).list(task_id)
    if terminal_stale:
        return None
    if not same_policy:
        raise TaskReviewError(
            "an active current review uses another preset or override"
        )
    if plan_file is not None and (
        Path(str(candidate.get("plan_file") or "")).resolve()
        != plan_file.expanduser().resolve()
    ):
        raise TaskReviewError("an active current review uses another plan")
    if isinstance(candidate.get("plan_review"), Mapping):
        from task_review_plan import finalize_active_plan_rebind

        candidate = finalize_active_plan_rebind(candidate, active_path)
    return candidate


def run_current_review(
    worktree: Path,
    *,
    deep: bool = False,
    full: bool = False,
    cross_model: bool = False,
    runtime: str = "",
    model: str = "",
    effort: str = "",
    no_review: bool = False,
    purpose: str = "implementation",
    boundary_input_file: Path | None = None,
    plan_file: Path | None = None,
    origin_surface: str = "",
    scratch_root: Path | None = None,
    runtime_manager: object | None = None,
    apply_finalizing_recovery: Callable[..., dict[str, Any]],
    plan_compilation: PlanReviewCompilation | None = None,
    plan_base_sha: str = "",
    plan_head_sha: str = "",
) -> dict[str, Any]:
    worktree = _validate_current_checkout(worktree)
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
    vault = worktree
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
    )
    active_path = (
        vault
        / ".vault-meta"
        / "harness"
        / "current-review"
        / "active.json"
    )
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
    )

    if meta is None:
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
        task_id = str(uuid.uuid4())
        runtime_root = _current_runtime_root(
            worktree, task_id, scratch_root
        )
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
        if plan_file is None:
            plan = runtime_root / "inputs/current-review-scope.md"
            _atomic_text(
                plan,
                "\n".join(
                    (
                        "# Current checkout review scope",
                        "",
                        "Review the exact current checkout and HEAD against its "
                        "repository instructions, tests, and public contract.",
                        "",
                    )
                ),
            )
        else:
            plan = plan_file.expanduser().resolve()
            if not plan.is_file() or plan.is_symlink():
                raise TaskReviewError("current review plan is unavailable")
        meta = {
            "version": 4,
            "lifecycle": "current-checkout",
            "task_id": task_id,
            "task_name": "current checkout review",
            "task_surface": surface,
            "worktree": str(worktree),
            "vault_root": str(vault),
            "plan_file": str(plan),
            "routing": {"session": session},
            "review_policy": requested_policy,
            "runtime_root": str(runtime_root),
        }
        if boundary_input is not None:
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
            stored_boundary = (
                runtime_root / "inputs" / "review-boundary-input.json"
            )
            _atomic_json(stored_boundary, boundary_input.payload())
            meta["review_boundary_input_file"] = str(stored_boundary)
        _request(
            meta,
            vault,
            task_id,
            ReviewContext(
                "pending/manifest.json",
                _git(worktree, "rev-parse", "HEAD"),
                "scoped",
                profile.sha256,
                "",
                purpose,
                boundary_input.input_sha256 if boundary_input else "",
            ),
        )
        _atomic_json(runtime_root / "current-review.json", meta)
        _atomic_json(active_path, meta)
    else:
        task_id = str(meta["task_id"])
        runtime_root = Path(str(meta.get("runtime_root") or "")).resolve()
        expected_root = _current_runtime_root(
            worktree, task_id, scratch_root
        )
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

    return _run_review(
        meta,
        vault,
        worktree,
        task_id,
        runtime_root,
        runtime_manager=runtime_manager,
        apply_finalizing_recovery=apply_finalizing_recovery,
    )
