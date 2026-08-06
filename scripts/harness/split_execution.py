"""Pure deterministic wave scheduling and explicit child-local placement data."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from typing import Mapping

from .contracts import ContractError, ID_RE
from .split_contracts import ChildBudget
from .split_validation import ValidatedSplit


CHILD_PLACEMENT = "child-workspace"


@dataclass(frozen=True)
class WorkspaceLocality:
    subplan_id: str
    workspace_id: str
    worktree_path: str
    executor_placement: str
    review_placement: str
    verification_placement: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.subplan_id, "locality subplan_id"),
            (self.workspace_id, "locality workspace_id"),
        ):
            if not isinstance(value, str) or not ID_RE.fullmatch(value):
                raise ContractError(f"{label} must be a bounded identifier")
        if (
            not isinstance(self.worktree_path, str)
            or not self.worktree_path
            or not PurePath(self.worktree_path).is_absolute()
        ):
            raise ContractError("locality worktree_path must be explicit and absolute")
        placements = (
            self.executor_placement,
            self.review_placement,
            self.verification_placement,
        )
        if placements != (CHILD_PLACEMENT,) * 3:
            raise ContractError("executor, review, and verification must stay child-local")


@dataclass(frozen=True)
class ScheduledChild:
    subplan_id: str
    pipeline: str
    route_alias: str
    budget: ChildBudget
    locality: WorkspaceLocality


@dataclass(frozen=True)
class ExecutionWave:
    wave: int
    children: tuple[ScheduledChild, ...]

    def __post_init__(self) -> None:
        if type(self.wave) is not int or self.wave < 1 or not self.children:
            raise ContractError("execution wave must be positive and non-empty")


@dataclass(frozen=True)
class SplitExecutionPlan:
    manifest_sha256: str
    subplan_count: int
    max_parallel: int
    waves: tuple[ExecutionWave, ...]


def schedule_waves(
    validated: ValidatedSplit,
    locality_by_subplan: Mapping[str, WorkspaceLocality],
) -> SplitExecutionPlan:
    """Schedule ready children by manifest order, never coordinator focus."""

    if not isinstance(validated, ValidatedSplit):
        raise ContractError("wave scheduling requires a validated Split capability")
    manifest = validated.manifest
    ordered_ids = tuple(item.subplan_id for item in manifest.subplans)
    if set(locality_by_subplan) != set(ordered_ids):
        raise ContractError("locality data must cover the exact manifest children")
    for subplan_id, locality in locality_by_subplan.items():
        if not isinstance(locality, WorkspaceLocality) or locality.subplan_id != subplan_id:
            raise ContractError("locality key and typed child identity must agree")

    by_id = {item.subplan_id: item for item in manifest.subplans}
    remaining = set(ordered_ids)
    completed: set[str] = set()
    waves: list[ExecutionWave] = []
    while remaining:
        ready = [
            subplan_id
            for subplan_id in ordered_ids
            if subplan_id in remaining
            and set(by_id[subplan_id].dependencies).issubset(completed)
        ][: manifest.max_parallel]
        if not ready:
            raise ContractError("validated dependency graph has no ready child")
        children = tuple(
            ScheduledChild(
                subplan_id=subplan_id,
                pipeline=by_id[subplan_id].pipeline,
                route_alias=by_id[subplan_id].route_alias,
                budget=by_id[subplan_id].budget,
                locality=locality_by_subplan[subplan_id],
            )
            for subplan_id in ready
        )
        waves.append(ExecutionWave(len(waves) + 1, children))
        completed.update(ready)
        remaining.difference_update(ready)

    return SplitExecutionPlan(
        manifest_sha256=manifest.manifest_sha256,
        subplan_count=manifest.subplan_count,
        max_parallel=manifest.max_parallel,
        waves=tuple(waves),
    )
