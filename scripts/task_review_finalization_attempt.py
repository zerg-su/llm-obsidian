"""Compile and reserve one schema-valid exact-HEAD finalization attempt."""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from harness.contracts import RuntimeRoute
from harness.finalization_ledger import (
    FinalizationLedger,
    predecessor_bound_attempt_id,
)
from harness.review_finalization import (
    reserve_task_finalization_cycle,
    task_finalization_policy,
)
from harness.workflows.review import ReviewOperationRequest
from model_routing import load_config
from review_contract import (
    compile_effective_review_topology,
    review_axis_provider,
    review_runtime_provider,
)
from task_review_shared import TaskReviewError
from task_plan_authority import PlanAuthorityError, resolve_plan_authority


class FinalizationAttemptError(TaskReviewError):
    """The normalized task cannot reserve its exact finalization attempt."""


def exact_head_attempt_enabled(meta: Mapping[str, Any]) -> bool:
    """Derive protocol authority only from the normalized additive v4 policy."""

    try:
        return meta.get("version") == 4 and task_finalization_policy(meta) is not None
    except ValueError as exc:
        raise FinalizationAttemptError(str(exc)) from exc


def _plan_authority(meta: Mapping[str, Any], worktree: Path):
    try:
        return resolve_plan_authority(meta, worktree)
    except PlanAuthorityError as exc:
        raise FinalizationAttemptError(str(exc)) from exc


def attempt_binding(meta: Mapping[str, Any], task_id: str, worktree: Path, *, cycle: int) -> tuple[str, int, str, str]:
    policy = task_finalization_policy(meta)
    if (
        type(cycle) is not int
        or cycle < 1
        or policy is None
        or cycle > policy.max_cycles
    ):
        raise FinalizationAttemptError("exact-HEAD review cycle is invalid")
    authority = _plan_authority(meta, worktree)
    return task_id, cycle, authority.plan_sha256, authority.outcome_sha256


def finalization_ledger(meta: Mapping[str, Any], vault: Path, task_id: str, worktree: Path) -> FinalizationLedger:
    policy = task_finalization_policy(meta)
    if policy is None:
        raise FinalizationAttemptError(
            "exact-HEAD finalization policy is unavailable"
        )
    authority = _plan_authority(meta, worktree)
    return FinalizationLedger(
        vault / ".vault-meta" / "harness" / "finalization-ledger",
        lineage_id=task_id,
        origin_task_id=task_id,
        plan_sha256=authority.plan_sha256,
        outcome_contract_sha256=authority.outcome_sha256,
        max_cycles=policy.max_cycles,
    )


def _attempt_id(
    task_id: str,
    exact_head: str,
    cycle: int,
    *,
    predecessor_attempt_id: str = "",
) -> str:
    try:
        namespace = uuid.UUID(task_id)
    except ValueError as exc:
        raise FinalizationAttemptError(
            "exact-HEAD finalization lineage is invalid"
        ) from exc
    if predecessor_attempt_id:
        try:
            return predecessor_bound_attempt_id(
                lineage_id=task_id,
                predecessor_attempt_id=predecessor_attempt_id,
                exact_head=exact_head,
                cycle_number=cycle,
            )
        except ValueError as exc:
            raise FinalizationAttemptError(str(exc)) from exc
    return str(uuid.uuid5(namespace, f"{exact_head}:cycle:{cycle}"))


def _bind_routes(
    request: ReviewOperationRequest,
    *,
    attempt_id: str,
    routes: object,
    routing_sha256: str,
) -> ReviewOperationRequest:
    selected = tuple(getattr(routes, "routes", ()))
    if len(selected) not in {1, 2}:
        raise FinalizationAttemptError("finalization route selection is invalid")
    compiled = tuple(
        RuntimeRoute(
            route.runtime,
            route.model,
            route.effort,
            "reviewer-callback",
            routing_sha256,
        )
        for route in selected
    )
    by_provider = {
        review_runtime_provider(route.runtime): route for route in compiled
    }
    if len(compiled) == 2 and set(by_provider) != {"anthropic", "openai"}:
        raise FinalizationAttemptError(
            "independent finalization routes are not independent"
        )
    requested_mode = request.requested_mode or request.policy.depth
    topology = compile_effective_review_topology(
        mode=requested_mode,
        cross_model=request.policy.cross_model,
        max_verify_iterations=request.policy.max_verify_iterations,
        verification_profile=request.context.verification_profile,
        verification_profile_sha256=(
            request.context.verification_profile_sha256
        ),
        routes=by_provider,
    )
    selected_provider = (
        topology.lanes[0].provider if len(by_provider) == 1 else ""
    )
    selected_route = by_provider[topology.lanes[0].provider]
    policy = replace(
        request.policy,
        operation_id=attempt_id,
        depth=topology.mode,
        runtime=selected_route.runtime if selected_provider else "",
        model=selected_route.model if selected_provider else "",
        effort=selected_route.effort if selected_provider else "",
        selected_provider=selected_provider,
    )
    return replace(
        request,
        policy=policy,
        route=selected_route,
        axis_routes={
            axis: by_provider[review_axis_provider(axis)]
            for axis in policy.axes
        },
        requested_mode=requested_mode,
        topology_sha256=topology.sha256,
    )


def reserve_exact_head_attempt(
    meta: Mapping[str, Any],
    *,
    vault: Path,
    worktree: Path,
    task_id: str,
    request: ReviewOperationRequest,
    cycle: int,
    predecessor_attempt_id: str = "",
    reserved_attempt_id: str = "",
) -> tuple[ReviewOperationRequest, FinalizationLedger, int]:
    config = load_config(vault)
    ledger = finalization_ledger(meta, vault, task_id, worktree)
    attempt_id = reserved_attempt_id or _attempt_id(
        task_id,
        request.context.head_sha,
        cycle,
        predecessor_attempt_id=predecessor_attempt_id,
    )
    if reserved_attempt_id and predecessor_attempt_id:
        raise FinalizationAttemptError(
            "reserved attempt cannot also declare a predecessor"
        )
    reservation = reserve_task_finalization_cycle(
        meta,
        ledger=ledger,
        config=config,
        attempt_id=attempt_id,
        exact_head=request.context.head_sha,
        task_id=task_id,
        worktree=str(worktree),
        independent_permitted=True,
        availability=None,
        now_epoch=int(time.time()),
        predecessor_attempt_id=predecessor_attempt_id,
    )
    if reservation is None or reservation.routes is None:
        raise FinalizationAttemptError(
            "exact-HEAD finalization reservation is unavailable"
        )
    if not reservation.cycle.allowed and reservation.cycle.reason not in {
        "already-reserved",
        "attempt-terminal",
    }:
        raise FinalizationAttemptError(
            f"exact-HEAD finalization stopped: {reservation.cycle.reason}"
        )
    if reservation.cycle.cycle_number != cycle:
        raise FinalizationAttemptError("exact-HEAD finalization cycle changed")
    return (
        _bind_routes(
            request,
            attempt_id=attempt_id,
            routes=reservation.routes,
            routing_sha256=config.fingerprint,
        ),
        ledger,
        cycle,
    )
