"""Exact task review authorization shared by summary delivery and reap."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

from model_routing_config import RoutingConfig
from review_contract import VERIFY_BUDGETS

from .finalization_policy import (
    AvailabilityEvidence,
    FinalizationPolicy,
    FinalizationPolicyError,
    FinalizationRouteDecision,
    compile_finalization_routes,
    parse_finalization_policy,
    require_registered_finalization_routes,
)
from .finalization_ledger import CycleDecision, FinalizationLedger
from .finalization_pivot import (
    FinalizationPivotError,
    load_accepted_pivot_receipt,
    pivot_required,
)
from .verification import VerificationError, load_profiles
from .workflows.review_gate import (
    ReviewGateAuthorization,
    authorize_task_finalization,
)


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
FINALIZATION_STATUSES = {
    "missing",
    "reviewing",
    "approved",
    "skipped",
    "attention",
    "stale",
}
ACTIVE_GATE_STATUSES = {
    "pending",
    "reviewing",
    "verifying",
    "fresh-reevaluation",
    "fresh-boundary-authorized",
    "recovery-verification-required",
    "awaiting-resolution",
}


@dataclass(frozen=True)
class TaskReviewStatus:
    """Typed, read-only view of the review gate at the reap boundary."""

    status: Literal[
        "missing",
        "reviewing",
        "approved",
        "skipped",
        "attention",
        "stale",
    ]
    gate_root: Path
    authorization: ReviewGateAuthorization | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in FINALIZATION_STATUSES:
            raise ValueError("review finalization status is invalid")
        if self.status in {"approved", "skipped"}:
            if self.authorization is None:
                raise ValueError("terminal review approval requires authorization")
        elif self.authorization is not None:
            raise ValueError("non-authorized review status cannot carry evidence")


def task_finalization_policy(
    meta: Mapping[str, object],
) -> FinalizationPolicy | None:
    """Read the optional additive v4 policy; historical tasks remain valid."""

    raw = meta.get("finalization_policy")
    if raw is None:
        return None
    if meta.get("version") != 4 or not isinstance(raw, Mapping):
        raise ValueError("finalization_policy requires v4 task metadata")
    try:
        policy = parse_finalization_policy(raw)
        require_registered_finalization_routes(policy)
    except FinalizationPolicyError as exc:
        raise ValueError(str(exc)) from exc
    return policy


def compile_task_finalization_routes(
    meta: Mapping[str, object],
    *,
    config: RoutingConfig,
    cycle_number: int,
    independent_permitted: bool,
    availability: AvailabilityEvidence | None,
    structural_pivot_accepted: bool = False,
    now_epoch: int,
) -> FinalizationRouteDecision | None:
    """Compile task policy without changing standalone review topology."""

    policy = task_finalization_policy(meta)
    if policy is None:
        return None
    review = meta.get("review_policy")
    if not isinstance(review, Mapping):
        raise ValueError("task finalization requires review_policy")
    runtime = review.get("runtime", "")
    model = review.get("model", "")
    effort = review.get("effort", "")
    if not all(isinstance(value, str) for value in (runtime, model, effort)):
        raise ValueError("task finalization review override is invalid")
    return compile_finalization_routes(
        config=config,
        policy=policy,
        cycle_number=cycle_number,
        independent_permitted=independent_permitted,
        availability=availability,
        explicit_runtime=runtime,
        explicit_model=model,
        explicit_effort=effort,
        required_mode=str(review.get("mode") or ""),
        structural_pivot_accepted=structural_pivot_accepted,
        now_epoch=now_epoch,
    )


@dataclass(frozen=True)
class TaskFinalizationReservation:
    cycle: CycleDecision
    routes: FinalizationRouteDecision | None


def reserve_task_finalization_cycle(
    meta: Mapping[str, object],
    *,
    ledger: FinalizationLedger,
    config: RoutingConfig,
    attempt_id: str,
    exact_head: str,
    task_id: str,
    worktree: str,
    independent_permitted: bool,
    availability: AvailabilityEvidence | None,
    now_epoch: int,
    predecessor_attempt_id: str = "",
) -> TaskFinalizationReservation | None:
    """Compile all bounded choices, then atomically select the reserved cycle."""

    policy = task_finalization_policy(meta)
    if policy is None:
        return None
    if ledger.max_cycles != policy.max_cycles:
        raise ValueError("finalization ledger and task policy ceilings differ")
    snapshot = ledger.snapshot(missing_ok=True)
    pivot_receipt = None
    if pivot_required(snapshot):
        try:
            pivot_receipt = load_accepted_pivot_receipt(
                ledger.root, snapshot=snapshot
            )
        except FinalizationPivotError as exc:
            raise ValueError(str(exc)) from exc
        already_reserved = any(
            cycle.get("attempt_id") == attempt_id
            for cycle in snapshot.get("cycles", [])
        )
        if pivot_receipt is None and not already_reserved:
            raise ValueError(
                "the third material failure requires an accepted structural "
                "pivot receipt before another product cycle"
            )
    decisions: dict[int, FinalizationRouteDecision] = {}
    for cycle in range(1, policy.max_cycles + 1):
        decision = compile_task_finalization_routes(
            meta,
            config=config,
            cycle_number=cycle,
            independent_permitted=independent_permitted,
            availability=availability,
            structural_pivot_accepted=pivot_receipt is not None,
            now_epoch=now_epoch,
        )
        assert decision is not None
        decisions[cycle] = decision
    reserved = ledger.reserve_from_policy_matrix(
        attempt_id=attempt_id,
        exact_head=exact_head,
        task_id=task_id,
        worktree=worktree,
        provider_policies={
            cycle: decision.ledger_policy()
            for cycle, decision in decisions.items()
        },
        predecessor_attempt_id=predecessor_attempt_id,
    )
    selected = (
        decisions[reserved.cycle_number]
        if reserved.cycle_number is not None
        else None
    )
    return TaskFinalizationReservation(reserved, selected)


def _task_identity(
    meta: Mapping[str, object],
    *,
    expected_operation_id: str = "",
) -> str:
    raw = str(meta.get("task_id") or "")
    try:
        task_id = str(uuid.UUID(raw))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("review finalization requires a canonical task UUID") from exc
    if task_id != raw:
        raise ValueError("review finalization requires a canonical task UUID")
    if expected_operation_id and task_id != expected_operation_id:
        raise ValueError("review gate task identity mismatches the runtime operation")
    return task_id


def _vault_root(
    meta: Mapping[str, object],
    *,
    expected_vault: Path | None = None,
) -> Path:
    raw = meta.get("vault_root")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("review finalization requires an absolute vault root")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("review finalization requires an absolute vault root")
    vault = path.resolve()
    if expected_vault is not None and vault != expected_vault.expanduser().resolve():
        raise ValueError("review gate vault identity mismatches the trusted runtime")
    store = vault / ".vault-meta" / "harness"
    if not store.is_dir():
        raise ValueError("review gate harness store is unavailable")
    return vault


def review_gate_root(
    meta: Mapping[str, object],
    worktree: Path,
    *,
    expected_vault: Path | None = None,
    expected_operation_id: str = "",
) -> Path:
    """Derive the sole gate root; task metadata cannot supply a path."""

    worktree = worktree.expanduser().resolve()
    declared = meta.get("worktree")
    if declared is not None:
        raw = Path(str(declared)).expanduser()
        if not raw.is_absolute() or raw.resolve() != worktree:
            raise ValueError("review gate worktree identity mismatches the task")
    task_id = _task_identity(
        meta, expected_operation_id=expected_operation_id
    )
    vault = _vault_root(meta, expected_vault=expected_vault)
    return (
        vault
        / ".vault-meta"
        / "harness"
        / "review-data"
        / task_id
        / task_id
    ).resolve()


def _review_binding(
    meta: Mapping[str, object],
    vault: Path,
) -> tuple[str, str, str]:
    policy = meta.get("review_policy")
    if not isinstance(policy, Mapping):
        raise ValueError("review finalization policy is unavailable")
    mode = str(policy.get("mode") or "")
    if mode not in {"simple", "deep", "full", "skip"}:
        raise ValueError("review finalization mode is invalid")
    cross_model = policy.get("cross_model")
    runtime = policy.get("runtime")
    model = policy.get("model")
    effort = policy.get("effort")
    budget = policy.get("max_verify_iterations")
    expected_budget = 0 if mode == "skip" else VERIFY_BUDGETS[mode]
    if (
        type(cross_model) is not bool
        or not isinstance(runtime, str)
        or not isinstance(model, str)
        or not isinstance(effort, str)
        or type(budget) is not int
        or budget != expected_budget
    ):
        raise ValueError("review finalization policy binding is incomplete")
    if mode == "skip" and any((cross_model, runtime, model, effort)):
        raise ValueError("typed no-review skip cannot carry provider overrides")
    profile = str(policy.get("verification_profile") or "")
    digest = str(policy.get("verification_profile_sha256") or "")
    if not PROFILE.fullmatch(profile) or not SHA256.fullmatch(digest):
        raise ValueError("review finalization profile binding is unavailable")
    try:
        configured = load_profiles(vault / "config" / "verification-profiles.toml")
    except (OSError, VerificationError) as exc:
        raise ValueError(
            "coordinator verification profile registry is unavailable"
        ) from exc
    current = configured.get(profile)
    if current is None or current.sha256 != digest:
        raise ValueError("review finalization profile binding is stale")
    return mode, profile, digest


def _head(worktree: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode or re.fullmatch(r"[0-9a-f]{40,64}", value) is None:
        raise ValueError("review finalization cannot resolve the exact task HEAD")
    return value


def _implementer_summary_sha256(
    meta: Mapping[str, object], worktree: Path
) -> str:
    """Bind new v4 finalization to the exact summary reviewed by the lane."""

    if meta.get("version") != 4:
        return ""
    path = worktree / ".task-summary.json"
    if not path.is_file() or path.is_symlink():
        raise ValueError(
            "v4 review finalization requires the exact implementer summary"
        )
    raw = path.read_bytes()
    if not raw or len(raw) > 250_000:
        raise ValueError("v4 implementer summary is empty or oversized")
    return hashlib.sha256(raw).hexdigest()


def require_task_review(
    meta: Mapping[str, object],
    worktree: Path,
    *,
    expected_vault: Path | None = None,
    expected_operation_id: str = "",
) -> ReviewGateAuthorization:
    """Fail closed unless the exact task HEAD has approval or an explicit skip."""

    finalization = task_review_status(
        meta,
        worktree,
        expected_vault=expected_vault,
        expected_operation_id=expected_operation_id,
    )
    if finalization.status not in {"approved", "skipped"}:
        raise ValueError(
            "review finalization is "
            f"{finalization.status}: {finalization.reason or 'not authorized'}"
        )
    assert finalization.authorization is not None
    return finalization.authorization


def task_review_status(
    meta: Mapping[str, object],
    worktree: Path,
    *,
    expected_vault: Path | None = None,
    expected_operation_id: str = "",
) -> TaskReviewStatus:
    """Classify the exact gate without turning normal review wait into attention."""

    worktree = worktree.expanduser().resolve()
    fallback_root = worktree / ".invalid-review-gate"
    try:
        task_id = _task_identity(
            meta, expected_operation_id=expected_operation_id
        )
        vault = _vault_root(meta, expected_vault=expected_vault)
        mode, profile, digest = _review_binding(meta, vault)
        root = review_gate_root(
            meta,
            worktree,
            expected_vault=expected_vault,
            expected_operation_id=expected_operation_id,
        )
        head = _head(worktree)
        summary_sha256 = (
            _implementer_summary_sha256(meta, worktree)
            if mode != "skip"
            else ""
        )
    except (OSError, ValueError) as exc:
        return TaskReviewStatus("attention", fallback_root, reason=str(exc))
    state_path = root / "review-gate.json"
    if not state_path.exists():
        return TaskReviewStatus("missing", root)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return TaskReviewStatus("attention", root, reason=str(exc))
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        return TaskReviewStatus(
            "attention", root, reason="review gate state is invalid"
        )
    raw_product_root = state.get("product_root")
    product_root = (
        Path(raw_product_root).expanduser()
        if isinstance(raw_product_root, str)
        else Path()
    )
    if (
        state.get("dispatch_operation_id") != task_id
        or not product_root.is_absolute()
        or product_root.resolve() != worktree
    ):
        return TaskReviewStatus(
            "attention", root, reason="review gate identity is invalid"
        )
    context = state.get("context")
    if not isinstance(context, dict) or (
        context.get("head_sha") != head
        or context.get("verification_profile") != profile
        or context.get("verification_profile_sha256") != digest
        or (
            summary_sha256
            and context.get("implementer_summary_sha256")
            != summary_sha256
        )
    ):
        return TaskReviewStatus(
            "stale", root, reason="review gate evidence is stale"
        )
    status = str(state.get("status") or "")
    if status in ACTIVE_GATE_STATUSES:
        return TaskReviewStatus("reviewing", root)
    if status == "attention-required":
        return TaskReviewStatus(
            "attention", root, reason="review gate requires attention"
        )
    if status not in {"approved", "skipped"}:
        return TaskReviewStatus(
            "attention", root, reason="review gate status is invalid"
        )
    try:
        authorization = authorize_task_finalization(
            root,
            dispatch_operation_id=task_id,
            expected_head_sha=head,
            expected_profile=profile,
            expected_profile_sha256=digest,
            expected_summary_sha256=summary_sha256,
        )
    except (OSError, TypeError, ValueError) as exc:
        rendered = str(exc)
        terminal = "stale" if "stale" in rendered.lower() else "attention"
        return TaskReviewStatus(terminal, root, reason=rendered)
    if mode == "skip" and authorization.skipped:
        return TaskReviewStatus("skipped", root, authorization)
    if mode != "skip" and authorization.approved:
        return TaskReviewStatus("approved", root, authorization)
    return TaskReviewStatus(
        "attention",
        root,
        reason="review gate authorization contradicts the task policy",
    )
