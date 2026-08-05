"""Deterministic, zero-effect validation for the eight Split rejection classes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .contracts import ContractError, SHA256_RE
from .split_contracts import SplitManifest, ownership_conflicts, seal_manifest


VALIDATION_CODE_ORDER = (
    "stale-digest",
    "uncovered-evidence",
    "overlapping-ownership",
    "dependency-cycle",
    "missing-join",
    "weakened-non-goal",
    "unregistered-pipeline",
    "budget-exceeded",
)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str

    def __post_init__(self) -> None:
        if self.code not in VALIDATION_CODE_ORDER or not self.message:
            raise ContractError("split validation issue is invalid")


@dataclass(frozen=True)
class ValidatedSplit:
    """Capability proving the manifest passed every pre-effect gate."""

    manifest: SplitManifest


@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    issues: tuple[ValidationIssue, ...]
    validated: ValidatedSplit | None

    def __post_init__(self) -> None:
        if self.accepted != (not self.issues) or self.accepted != (
            self.validated is not None
        ):
            raise ContractError("validation result capability and issues disagree")

    @property
    def issue_codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    @property
    def effects(self) -> tuple[()]:
        return ()


def _dependency_graph_invalid(manifest: SplitManifest) -> bool:
    ids = tuple(item.subplan_id for item in manifest.subplans)
    known = set(ids)
    dependencies = {
        item.subplan_id: set(item.dependencies) for item in manifest.subplans
    }
    if any(
        dependency not in known or dependency == subplan_id
        for subplan_id, values in dependencies.items()
        for dependency in values
    ):
        return True
    remaining = set(ids)
    completed: set[str] = set()
    while remaining:
        ready = [
            subplan_id
            for subplan_id in ids
            if subplan_id in remaining
            and dependencies[subplan_id].issubset(completed)
        ]
        if not ready:
            return True
        completed.update(ready)
        remaining.difference_update(ready)
    return False


def _budget_exceeded(manifest: SplitManifest) -> bool:
    frozen = manifest.frozen_budget
    return (
        manifest.subplan_count != len(manifest.subplans)
        or manifest.subplan_count > frozen.subplan_limit
        or manifest.max_parallel > manifest.subplan_count
        or manifest.max_parallel > frozen.max_parallel
        or sum(item.budget.token_limit for item in manifest.subplans)
        > frozen.total_token_limit
        or sum(item.budget.time_budget_seconds for item in manifest.subplans)
        > frozen.total_time_budget_seconds
    )


def validate_manifest(
    manifest: SplitManifest,
    *,
    current_plan_sha256: str,
    current_outcome_contract_sha256: str,
    registered_pipelines: Iterable[str],
) -> ValidationResult:
    """Evaluate exactly eight pre-effect classes in a stable public order."""

    if not isinstance(manifest, SplitManifest):
        raise ContractError("split validation requires a typed manifest")
    if (
        not isinstance(current_plan_sha256, str)
        or not SHA256_RE.fullmatch(current_plan_sha256)
        or not isinstance(current_outcome_contract_sha256, str)
        or not SHA256_RE.fullmatch(current_outcome_contract_sha256)
    ):
        raise ContractError("current parent digests must be lowercase sha256")
    if (
        not manifest.manifest_sha256
        or seal_manifest(manifest).manifest_sha256 != manifest.manifest_sha256
    ):
        raise ContractError("validation requires an exact sealed manifest")
    pipelines = frozenset(registered_pipelines)
    if not pipelines or any(not isinstance(item, str) or not item for item in pipelines):
        raise ContractError("registered pipelines must be a non-empty string set")

    parent = manifest.parent
    assigned_evidence = {
        evidence_id
        for subplan in manifest.subplans
        for evidence_id in subplan.evidence_ids
    }
    weakened_non_goal = any(
        subplan.inherited_non_goals != parent.non_goals
        for subplan in manifest.subplans
    )
    unknown_pipelines = sorted(
        {item.pipeline for item in manifest.subplans} - pipelines
    )
    conditions = {
        "stale-digest": (
            parent.plan_sha256 != current_plan_sha256
            or parent.outcome_contract_sha256 != current_outcome_contract_sha256
        ),
        "uncovered-evidence": assigned_evidence != set(parent.evidence_ids),
        "overlapping-ownership": bool(ownership_conflicts(manifest.subplans)),
        "dependency-cycle": _dependency_graph_invalid(manifest),
        "missing-join": (
            manifest.join is None
            or manifest.join.strategy != "manifest-order"
            or manifest.join.required_status != "approved"
        ),
        "weakened-non-goal": weakened_non_goal,
        "unregistered-pipeline": bool(unknown_pipelines),
        "budget-exceeded": _budget_exceeded(manifest),
    }
    messages = {
        "stale-digest": "parent plan or Outcome Contract digest is stale",
        "uncovered-evidence": "child evidence assignment does not exactly cover the parent",
        "overlapping-ownership": "two subplans own the same exact path",
        "dependency-cycle": "dependency graph is cyclic or references an unknown child",
        "missing-join": "manifest-order approved-only join is not declared",
        "weakened-non-goal": "a child changed the ordered inherited non-goal set",
        "unregistered-pipeline": "a child pipeline is not registered",
        "budget-exceeded": "manifest count, parallelism, token, or time budget is exceeded",
    }
    issues = tuple(
        ValidationIssue(code, messages[code])
        for code in VALIDATION_CODE_ORDER
        if conditions[code]
    )
    return ValidationResult(
        accepted=not issues,
        issues=issues,
        validated=ValidatedSplit(manifest) if not issues else None,
    )
