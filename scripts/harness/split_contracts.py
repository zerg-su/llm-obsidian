"""Typed, effect-free contracts for governed SplitManifest previews."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .contracts import ContractError, ID_RE, SHA256_RE


SPLIT_SCHEMA_VERSION = 1
PIPELINE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
SELECTION_MODES = frozenset({"fan-out", "one-child-fallback"})
SELECTION_REASONS = frozenset(
    {
        "fan-out",
        "single-candidate",
        "independence-unproven",
        "coordination-cost",
        "ownership-overlap",
    }
)
ZERO_EFFECT_COUNTS = MappingProxyType(
    {
        "dispatches": 0,
        "provider_calls": 0,
        "surfaces_created": 0,
        "worktrees_created": 0,
    }
)


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ContractError(f"{label} must be a bounded identifier")
    return value


def _sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ContractError(f"{label} must be a lowercase sha256")
    return value


def _pipeline(value: str, label: str) -> str:
    if not isinstance(value, str) or not PIPELINE_RE.fullmatch(value):
        raise ContractError(f"{label} must be a bounded pipeline identifier")
    return value


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} must be non-empty text")
    return value


def _string_tuple(
    values: tuple[str, ...],
    label: str,
    *,
    identifiers: bool = False,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(values, tuple) or (not values and not allow_empty):
        raise ContractError(f"{label} must be a{' non-empty' if not allow_empty else ''} tuple")
    normalized: list[str] = []
    for value in values:
        normalized.append(
            _identifier(value, label) if identifiers else _text(value, label)
        )
    if len(set(normalized)) != len(normalized):
        raise ContractError(f"{label} must be unique")
    return tuple(normalized)


def _owned_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ContractError("owned path must be a non-empty POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts:
        raise ContractError("owned path must remain repository-relative")
    normalized = path.as_posix()
    if normalized != value or value.endswith("/"):
        raise ContractError("owned path must be an exact normalized file path")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ContractError(f"{label} fields changed")


@dataclass(frozen=True)
class ChildBudget:
    token_limit: int
    time_budget_seconds: int

    def __post_init__(self) -> None:
        if (
            type(self.token_limit) is not int
            or type(self.time_budget_seconds) is not int
            or self.token_limit < 1
            or self.time_budget_seconds < 1
        ):
            raise ContractError("child budget must contain positive integers")


@dataclass(frozen=True)
class FrozenSplitBudget:
    subplan_limit: int
    max_parallel: int
    total_token_limit: int
    total_time_budget_seconds: int

    def __post_init__(self) -> None:
        values = (
            self.subplan_limit,
            self.max_parallel,
            self.total_token_limit,
            self.total_time_budget_seconds,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise ContractError("frozen split budget must contain positive integers")
        if self.max_parallel > self.subplan_limit:
            raise ContractError("frozen max_parallel cannot exceed subplan_limit")


@dataclass(frozen=True)
class ParentContract:
    plan_sha256: str
    outcome_contract_sha256: str
    evidence_ids: tuple[str, ...]
    non_goals: tuple[str, ...]

    def __post_init__(self) -> None:
        _sha256(self.plan_sha256, "parent plan_sha256")
        _sha256(self.outcome_contract_sha256, "parent outcome_contract_sha256")
        _string_tuple(self.evidence_ids, "parent evidence id", identifiers=True)
        _string_tuple(self.non_goals, "parent non-goal")


@dataclass(frozen=True)
class SplitCandidate:
    subplan_id: str
    title: str
    pipeline: str
    route_alias: str
    owned_paths: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    dependencies: tuple[str, ...]
    inherited_non_goals: tuple[str, ...]
    budget: ChildBudget
    independence_proven: bool

    def __post_init__(self) -> None:
        _identifier(self.subplan_id, "subplan_id")
        _text(self.title, "subplan title")
        _pipeline(self.pipeline, "child pipeline")
        _identifier(self.route_alias, "child route_alias")
        if self.route_alias in {"claude", "codex"}:
            raise ContractError("child route_alias must remain transport-neutral")
        if not isinstance(self.owned_paths, tuple) or not self.owned_paths:
            raise ContractError("owned_paths must be a non-empty tuple")
        paths = tuple(_owned_path(value) for value in self.owned_paths)
        if len(set(paths)) != len(paths):
            raise ContractError("owned_paths must be unique")
        _string_tuple(self.evidence_ids, "child evidence id", identifiers=True)
        _string_tuple(
            self.dependencies,
            "child dependency",
            identifiers=True,
            allow_empty=True,
        )
        _string_tuple(self.inherited_non_goals, "inherited non-goal")
        if not isinstance(self.budget, ChildBudget):
            raise ContractError("child budget must be typed")
        if type(self.independence_proven) is not bool:
            raise ContractError("independence_proven must be a boolean")


@dataclass(frozen=True)
class JoinSpec:
    strategy: str = "manifest-order"
    required_status: str = "approved"

    def __post_init__(self) -> None:
        _identifier(self.strategy, "join strategy")
        _identifier(self.required_status, "join required_status")


@dataclass(frozen=True)
class SplitSelection:
    mode: str
    reason: str

    def __post_init__(self) -> None:
        if self.mode not in SELECTION_MODES or self.reason not in SELECTION_REASONS:
            raise ContractError("split selection is invalid")
        if (self.mode == "fan-out") != (self.reason == "fan-out"):
            raise ContractError("split selection mode and reason disagree")


@dataclass(frozen=True)
class SplitManifest:
    parent: ParentContract
    selection: SplitSelection
    subplan_count: int
    max_parallel: int
    frozen_budget: FrozenSplitBudget
    subplans: tuple[SplitCandidate, ...]
    join: JoinSpec | None
    manifest_sha256: str = ""
    schema_version: int = SPLIT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SPLIT_SCHEMA_VERSION:
            raise ContractError("unsupported SplitManifest schema")
        if not isinstance(self.parent, ParentContract):
            raise ContractError("manifest parent contract must be typed")
        if not isinstance(self.selection, SplitSelection):
            raise ContractError("manifest selection must be typed")
        if (
            type(self.subplan_count) is not int
            or self.subplan_count < 1
            or type(self.max_parallel) is not int
            or self.max_parallel < 1
        ):
            raise ContractError("manifest counts must be positive integers")
        if not isinstance(self.frozen_budget, FrozenSplitBudget):
            raise ContractError("manifest frozen budget must be typed")
        if (
            not isinstance(self.subplans, tuple)
            or not self.subplans
            or any(not isinstance(item, SplitCandidate) for item in self.subplans)
        ):
            raise ContractError("manifest subplans must be a non-empty typed tuple")
        ids = tuple(item.subplan_id for item in self.subplans)
        if len(set(ids)) != len(ids):
            raise ContractError("manifest subplan ids must be unique")
        if self.selection.mode == "fan-out" and (
            len(self.subplans) < 2
            or any(not item.independence_proven for item in self.subplans)
        ):
            raise ContractError("fan-out requires at least two proven-independent children")
        if self.selection.mode == "one-child-fallback" and (
            len(self.subplans) != 1
            or self.subplan_count != 1
            or self.max_parallel != 1
        ):
            raise ContractError("one-child fallback must remain one serial child")
        if self.join is not None and not isinstance(self.join, JoinSpec):
            raise ContractError("manifest join must be typed or absent")
        if self.manifest_sha256:
            _sha256(self.manifest_sha256, "manifest_sha256")


@dataclass(frozen=True)
class SplitPreview:
    manifest: SplitManifest

    @property
    def effect_counts(self) -> dict[str, int]:
        return dict(ZERO_EFFECT_COUNTS)


def _candidate_dict(value: SplitCandidate) -> dict[str, Any]:
    return {
        "subplan_id": value.subplan_id,
        "title": value.title,
        "pipeline": value.pipeline,
        "route_alias": value.route_alias,
        "owned_paths": list(value.owned_paths),
        "evidence_ids": list(value.evidence_ids),
        "dependencies": list(value.dependencies),
        "inherited_non_goals": list(value.inherited_non_goals),
        "budget": {
            "token_limit": value.budget.token_limit,
            "time_budget_seconds": value.budget.time_budget_seconds,
        },
        "independence_proven": value.independence_proven,
    }


def manifest_to_dict(value: SplitManifest) -> dict[str, Any]:
    """Return the exact published JSON envelope in canonical field shape."""

    return {
        "schema_version": value.schema_version,
        "manifest_sha256": value.manifest_sha256,
        "parent": {
            "plan_sha256": value.parent.plan_sha256,
            "outcome_contract_sha256": value.parent.outcome_contract_sha256,
            "evidence_ids": list(value.parent.evidence_ids),
            "non_goals": list(value.parent.non_goals),
        },
        "selection": {
            "mode": value.selection.mode,
            "reason": value.selection.reason,
        },
        "subplan_count": value.subplan_count,
        "max_parallel": value.max_parallel,
        "frozen_budget": {
            "subplan_limit": value.frozen_budget.subplan_limit,
            "max_parallel": value.frozen_budget.max_parallel,
            "total_token_limit": value.frozen_budget.total_token_limit,
            "total_time_budget_seconds": value.frozen_budget.total_time_budget_seconds,
        },
        "subplans": [_candidate_dict(item) for item in value.subplans],
        "join": (
            {
                "strategy": value.join.strategy,
                "required_status": value.join.required_status,
            }
            if value.join is not None
            else None
        ),
    }


def _canonical_manifest_bytes(value: SplitManifest) -> bytes:
    payload = manifest_to_dict(dataclasses.replace(value, manifest_sha256=""))
    payload.pop("manifest_sha256")
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def seal_manifest(value: SplitManifest) -> SplitManifest:
    """Bind the exact manifest content to its deterministic SHA-256."""

    digest = hashlib.sha256(_canonical_manifest_bytes(value)).hexdigest()
    return dataclasses.replace(value, manifest_sha256=digest)


def _budget_from_dict(value: Mapping[str, Any]) -> ChildBudget:
    _exact_keys(value, {"token_limit", "time_budget_seconds"}, "child budget")
    return ChildBudget(
        token_limit=value.get("token_limit"),
        time_budget_seconds=value.get("time_budget_seconds"),
    )


def _candidate_from_dict(value: Mapping[str, Any]) -> SplitCandidate:
    _exact_keys(
        value,
        {
            "subplan_id",
            "title",
            "pipeline",
            "route_alias",
            "owned_paths",
            "evidence_ids",
            "dependencies",
            "inherited_non_goals",
            "budget",
            "independence_proven",
        },
        "split candidate",
    )
    return SplitCandidate(
        subplan_id=value.get("subplan_id"),
        title=value.get("title"),
        pipeline=value.get("pipeline"),
        route_alias=value.get("route_alias"),
        owned_paths=tuple(value.get("owned_paths", ())),
        evidence_ids=tuple(value.get("evidence_ids", ())),
        dependencies=tuple(value.get("dependencies", ())),
        inherited_non_goals=tuple(value.get("inherited_non_goals", ())),
        budget=_budget_from_dict(value.get("budget", {})),
        independence_proven=value.get("independence_proven"),
    )


def manifest_from_dict(value: Mapping[str, Any]) -> SplitManifest:
    """Parse and digest-check one exact manifest; unknown fields fail closed."""

    _exact_keys(
        value,
        {
            "schema_version",
            "manifest_sha256",
            "parent",
            "selection",
            "subplan_count",
            "max_parallel",
            "frozen_budget",
            "subplans",
            "join",
        },
        "SplitManifest",
    )
    parent = value.get("parent", {})
    _exact_keys(
        parent,
        {"plan_sha256", "outcome_contract_sha256", "evidence_ids", "non_goals"},
        "manifest parent",
    )
    selection = value.get("selection", {})
    _exact_keys(selection, {"mode", "reason"}, "manifest selection")
    frozen = value.get("frozen_budget", {})
    _exact_keys(
        frozen,
        {
            "subplan_limit",
            "max_parallel",
            "total_token_limit",
            "total_time_budget_seconds",
        },
        "manifest frozen budget",
    )
    raw_join = value.get("join")
    join = None
    if raw_join is not None:
        _exact_keys(raw_join, {"strategy", "required_status"}, "manifest join")
        join = JoinSpec(
            strategy=raw_join.get("strategy"),
            required_status=raw_join.get("required_status"),
        )
    manifest = SplitManifest(
        parent=ParentContract(
            plan_sha256=parent.get("plan_sha256"),
            outcome_contract_sha256=parent.get("outcome_contract_sha256"),
            evidence_ids=tuple(parent.get("evidence_ids", ())),
            non_goals=tuple(parent.get("non_goals", ())),
        ),
        selection=SplitSelection(
            mode=selection.get("mode"),
            reason=selection.get("reason"),
        ),
        subplan_count=value.get("subplan_count"),
        max_parallel=value.get("max_parallel"),
        frozen_budget=FrozenSplitBudget(
            subplan_limit=frozen.get("subplan_limit"),
            max_parallel=frozen.get("max_parallel"),
            total_token_limit=frozen.get("total_token_limit"),
            total_time_budget_seconds=frozen.get("total_time_budget_seconds"),
        ),
        subplans=tuple(_candidate_from_dict(item) for item in value.get("subplans", ())),
        join=join,
        manifest_sha256=value.get("manifest_sha256"),
        schema_version=value.get("schema_version"),
    )
    if seal_manifest(manifest).manifest_sha256 != manifest.manifest_sha256:
        raise ContractError("manifest_sha256 does not bind the exact manifest")
    return manifest


def ownership_conflicts(
    candidates: Sequence[SplitCandidate],
) -> tuple[tuple[str, str, str], ...]:
    """Return deterministic exact-file ownership conflicts."""

    owners: dict[str, str] = {}
    conflicts: list[tuple[str, str, str]] = []
    for candidate in candidates:
        for path in candidate.owned_paths:
            prior = owners.get(path)
            if prior is not None and prior != candidate.subplan_id:
                conflicts.append((prior, candidate.subplan_id, path))
            else:
                owners[path] = candidate.subplan_id
    return tuple(conflicts)


def _fallback_candidate(
    parent: ParentContract,
    candidates: Sequence[SplitCandidate],
    *,
    pipeline: str,
    route_alias: str,
) -> SplitCandidate:
    return SplitCandidate(
        subplan_id="whole-plan",
        title="Whole plan implementation",
        pipeline=pipeline,
        route_alias=route_alias,
        owned_paths=tuple(sorted({path for item in candidates for path in item.owned_paths})),
        evidence_ids=parent.evidence_ids,
        dependencies=(),
        inherited_non_goals=parent.non_goals,
        budget=ChildBudget(
            token_limit=sum(item.budget.token_limit for item in candidates),
            time_budget_seconds=sum(
                item.budget.time_budget_seconds for item in candidates
            ),
        ),
        independence_proven=True,
    )


def build_split_preview(
    *,
    parent: ParentContract,
    candidates: Sequence[SplitCandidate],
    frozen_budget: FrozenSplitBudget,
    requested_max_parallel: int,
    coordination_cost: int,
    parallel_benefit: int,
    fallback_pipeline: str,
    fallback_route_alias: str,
    join: JoinSpec | None,
) -> SplitPreview:
    """Choose a natural fan-out or one-child fallback without effect adapters."""

    if not isinstance(parent, ParentContract) or not isinstance(
        frozen_budget, FrozenSplitBudget
    ):
        raise ContractError("preview requires typed parent and frozen budget")
    if not isinstance(candidates, Sequence) or not candidates:
        raise ContractError("preview requires at least one candidate")
    if any(not isinstance(item, SplitCandidate) for item in candidates):
        raise ContractError("preview candidates must be typed")
    for value, label in (
        (requested_max_parallel, "requested_max_parallel"),
        (coordination_cost, "coordination_cost"),
        (parallel_benefit, "parallel_benefit"),
    ):
        if type(value) is not int or value < (1 if label == "requested_max_parallel" else 0):
            raise ContractError(f"{label} is invalid")
    _pipeline(fallback_pipeline, "fallback pipeline")
    _identifier(fallback_route_alias, "fallback route_alias")

    reason = "fan-out"
    if len(candidates) == 1:
        reason = "single-candidate"
    elif any(not item.independence_proven for item in candidates):
        reason = "independence-unproven"
    elif ownership_conflicts(candidates):
        reason = "ownership-overlap"
    elif coordination_cost >= parallel_benefit:
        reason = "coordination-cost"

    if reason == "fan-out":
        subplans = tuple(
            dataclasses.replace(item, inherited_non_goals=parent.non_goals)
            for item in candidates
        )
        selection = SplitSelection("fan-out", "fan-out")
        max_parallel = min(
            requested_max_parallel,
            frozen_budget.max_parallel,
            len(subplans),
        )
    else:
        subplans = (
            _fallback_candidate(
                parent,
                candidates,
                pipeline=fallback_pipeline,
                route_alias=fallback_route_alias,
            ),
        )
        selection = SplitSelection("one-child-fallback", reason)
        max_parallel = 1

    return SplitPreview(
        manifest=seal_manifest(
            SplitManifest(
                parent=parent,
                selection=selection,
                subplan_count=len(subplans),
                max_parallel=max_parallel,
                frozen_budget=frozen_budget,
                subplans=subplans,
                join=join,
            )
        )
    )


def preview_from_dict(value: Mapping[str, Any]) -> SplitPreview:
    """Parse one exact preview request used by the read/stdin-only facade."""

    _exact_keys(
        value,
        {
            "schema_version",
            "parent",
            "candidates",
            "frozen_budget",
            "requested_max_parallel",
            "coordination_cost",
            "parallel_benefit",
            "fallback_pipeline",
            "fallback_route_alias",
            "join",
            "current_parent",
            "registered_pipelines",
        },
        "split preview request",
    )
    if value.get("schema_version") != 1:
        raise ContractError("unsupported split preview request schema")
    parent = value.get("parent", {})
    _exact_keys(
        parent,
        {"plan_sha256", "outcome_contract_sha256", "evidence_ids", "non_goals"},
        "preview parent",
    )
    frozen = value.get("frozen_budget", {})
    _exact_keys(
        frozen,
        {
            "subplan_limit",
            "max_parallel",
            "total_token_limit",
            "total_time_budget_seconds",
        },
        "preview frozen budget",
    )
    raw_join = value.get("join")
    join = None
    if raw_join is not None:
        _exact_keys(raw_join, {"strategy", "required_status"}, "preview join")
        join = JoinSpec(
            strategy=raw_join.get("strategy"),
            required_status=raw_join.get("required_status"),
        )
    return build_split_preview(
        parent=ParentContract(
            plan_sha256=parent.get("plan_sha256"),
            outcome_contract_sha256=parent.get("outcome_contract_sha256"),
            evidence_ids=tuple(parent.get("evidence_ids", ())),
            non_goals=tuple(parent.get("non_goals", ())),
        ),
        candidates=tuple(
            _candidate_from_dict(item) for item in value.get("candidates", ())
        ),
        frozen_budget=FrozenSplitBudget(
            subplan_limit=frozen.get("subplan_limit"),
            max_parallel=frozen.get("max_parallel"),
            total_token_limit=frozen.get("total_token_limit"),
            total_time_budget_seconds=frozen.get("total_time_budget_seconds"),
        ),
        requested_max_parallel=value.get("requested_max_parallel"),
        coordination_cost=value.get("coordination_cost"),
        parallel_benefit=value.get("parallel_benefit"),
        fallback_pipeline=value.get("fallback_pipeline"),
        fallback_route_alias=value.get("fallback_route_alias"),
        join=join,
    )
