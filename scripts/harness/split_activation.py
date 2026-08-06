"""Bounded Split activation over existing dispatch and lifecycle adapters.

The module owns no provider, cmux, worktree, or Git effect.  A caller supplies
the existing dispatch adapter only after a sealed manifest has produced the
``ValidatedSplit`` capability and every child binding has been checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from typing import Callable, Iterable, Mapping

from .contracts import ContractError, ID_RE
from .split_contracts import ChildBudget, SplitCandidate, SplitManifest
from .split_execution import SplitExecutionPlan, WorkspaceLocality, schedule_waves
from .split_join import ChildReceipt, JoinDecision, evaluate_join
from .split_validation import ValidatedSplit, validate_manifest


CHILD_PLACEMENT = "child-workspace"
DRIVE_DISPOSITIONS = frozenset(
    {
        "rejected",
        "awaiting-children",
        "ready-to-join",
        "attention-required",
        "failed",
        "cancelled",
        "conflict",
    }
)


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ContractError(f"{label} must be a bounded identifier")
    return value


def _absolute_path(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not PurePath(value).is_absolute()
    ):
        raise ContractError(f"{label} must be an explicit absolute path")
    return value


@dataclass(frozen=True)
class SplitDispatchBinding:
    """Exact child policy handed to the existing workspace dispatch facade."""

    manifest_sha256: str
    subplan_id: str
    request_id: str
    pipeline: str
    route_alias: str
    worktree_path: str
    placement: str
    budget: ChildBudget

    def __post_init__(self) -> None:
        if (
            not isinstance(self.manifest_sha256, str)
            or len(self.manifest_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.manifest_sha256)
        ):
            raise ContractError("binding manifest_sha256 must be a lowercase sha256")
        for value, label in (
            (self.subplan_id, "binding subplan_id"),
            (self.request_id, "binding request_id"),
            (self.route_alias, "binding route_alias"),
        ):
            _identifier(value, label)
        if not isinstance(self.pipeline, str) or not self.pipeline:
            raise ContractError("binding pipeline must be non-empty")
        _absolute_path(self.worktree_path, "binding worktree_path")
        if self.placement != "workspace":
            raise ContractError("Split child dispatch must use workspace placement")
        if not isinstance(self.budget, ChildBudget):
            raise ContractError("Split child dispatch budget must be typed")


@dataclass(frozen=True)
class SplitChildPolicy:
    """Exact manifest slice persisted in an existing task contract."""

    manifest_sha256: str
    parent_plan_sha256: str
    parent_outcome_contract_sha256: str
    subplan_id: str
    route_alias: str
    owned_paths: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    dependencies: tuple[str, ...]
    budget: ChildBudget

    def __post_init__(self) -> None:
        for value, label in (
            (self.manifest_sha256, "split policy manifest_sha256"),
            (self.parent_plan_sha256, "split policy parent_plan_sha256"),
            (
                self.parent_outcome_contract_sha256,
                "split policy parent_outcome_contract_sha256",
            ),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ContractError(f"{label} must be a lowercase sha256")
        _identifier(self.subplan_id, "split policy subplan_id")
        _identifier(self.route_alias, "split policy route_alias")
        if (
            not isinstance(self.owned_paths, tuple)
            or not self.owned_paths
            or len(set(self.owned_paths)) != len(self.owned_paths)
            or any(
                not isinstance(item, str)
                or not item
                or PurePath(item).is_absolute()
                or PurePath(item).as_posix() != item
                or "." in PurePath(item).parts
                or ".." in PurePath(item).parts
                for item in self.owned_paths
            )
        ):
            raise ContractError("split policy owned_paths must be exact relative files")
        for values, label, allow_empty in (
            (self.evidence_ids, "split policy evidence_ids", False),
            (self.dependencies, "split policy dependencies", True),
        ):
            if (
                not isinstance(values, tuple)
                or (not allow_empty and not values)
                or len(set(values)) != len(values)
                or any(not isinstance(item, str) or not ID_RE.fullmatch(item) for item in values)
            ):
                raise ContractError(f"{label} must be unique bounded identifiers")
        if not isinstance(self.budget, ChildBudget):
            raise ContractError("split policy budget must be typed")


def split_child_policy(
    manifest: SplitManifest,
    candidate: SplitCandidate,
) -> SplitChildPolicy:
    """Project one exact child from a sealed manifest."""

    if candidate not in manifest.subplans or not manifest.manifest_sha256:
        raise ContractError("split child policy requires an exact sealed manifest child")
    return SplitChildPolicy(
        manifest_sha256=manifest.manifest_sha256,
        parent_plan_sha256=manifest.parent.plan_sha256,
        parent_outcome_contract_sha256=manifest.parent.outcome_contract_sha256,
        subplan_id=candidate.subplan_id,
        route_alias=candidate.route_alias,
        owned_paths=candidate.owned_paths,
        evidence_ids=candidate.evidence_ids,
        dependencies=candidate.dependencies,
        budget=candidate.budget,
    )


def split_child_policy_payload(value: SplitChildPolicy) -> dict[str, object]:
    if not isinstance(value, SplitChildPolicy):
        raise ContractError("split child policy must be typed")
    return {
        "manifest_sha256": value.manifest_sha256,
        "parent_plan_sha256": value.parent_plan_sha256,
        "parent_outcome_contract_sha256": value.parent_outcome_contract_sha256,
        "subplan_id": value.subplan_id,
        "route_alias": value.route_alias,
        "owned_paths": list(value.owned_paths),
        "evidence_ids": list(value.evidence_ids),
        "dependencies": list(value.dependencies),
        "budget": {
            "token_limit": value.budget.token_limit,
            "time_budget_seconds": value.budget.time_budget_seconds,
        },
    }


def parse_split_child_policy(value: object) -> SplitChildPolicy:
    expected = {
        "manifest_sha256",
        "parent_plan_sha256",
        "parent_outcome_contract_sha256",
        "subplan_id",
        "route_alias",
        "owned_paths",
        "evidence_ids",
        "dependencies",
        "budget",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ContractError("split child policy fields changed")
    raw_budget = value.get("budget")
    if not isinstance(raw_budget, Mapping) or set(raw_budget) != {
        "token_limit",
        "time_budget_seconds",
    }:
        raise ContractError("split child policy budget fields changed")
    return SplitChildPolicy(
        manifest_sha256=value.get("manifest_sha256"),  # type: ignore[arg-type]
        parent_plan_sha256=value.get("parent_plan_sha256"),  # type: ignore[arg-type]
        parent_outcome_contract_sha256=value.get(  # type: ignore[arg-type]
            "parent_outcome_contract_sha256"
        ),
        subplan_id=value.get("subplan_id"),  # type: ignore[arg-type]
        route_alias=value.get("route_alias"),  # type: ignore[arg-type]
        owned_paths=tuple(value.get("owned_paths", ())),
        evidence_ids=tuple(value.get("evidence_ids", ())),
        dependencies=tuple(value.get("dependencies", ())),
        budget=ChildBudget(
            token_limit=raw_budget.get("token_limit"),  # type: ignore[arg-type]
            time_budget_seconds=raw_budget.get("time_budget_seconds"),  # type: ignore[arg-type]
        ),
    )


@dataclass(frozen=True)
class SplitLaunchReceipt:
    """Immutable identity returned by the existing dispatch/cmux adapter."""

    manifest_sha256: str
    subplan_id: str
    request_id: str
    workspace_id: str
    worktree_path: str
    surface_id: str
    placement: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.manifest_sha256, str)
            or len(self.manifest_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.manifest_sha256)
        ):
            raise ContractError("launch manifest_sha256 must be a lowercase sha256")
        for value, label in (
            (self.subplan_id, "launch subplan_id"),
            (self.request_id, "launch request_id"),
            (self.workspace_id, "launch workspace_id"),
            (self.surface_id, "launch surface_id"),
        ):
            _identifier(value, label)
        _absolute_path(self.worktree_path, "launch worktree_path")
        if self.placement != "workspace":
            raise ContractError("Split launch left child workspace placement")


@dataclass(frozen=True)
class SplitTerminalReceipt:
    """Terminal child proof plus child-local resource cleanup evidence."""

    child: ChildReceipt
    request_id: str
    workspace_id: str
    worktree_path: str
    executor_placement: str
    review_placement: str
    verification_placement: str
    resources_closed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.child, ChildReceipt):
            raise ContractError("terminal receipt child must be typed")
        for value, label in (
            (self.request_id, "terminal request_id"),
            (self.workspace_id, "terminal workspace_id"),
        ):
            _identifier(value, label)
        _absolute_path(self.worktree_path, "terminal worktree_path")
        if (
            self.executor_placement,
            self.review_placement,
            self.verification_placement,
        ) != (CHILD_PLACEMENT,) * 3:
            raise ContractError("Split terminal work must remain child-local")
        if type(self.resources_closed) is not bool:
            raise ContractError("terminal resources_closed must be a boolean")


@dataclass(frozen=True)
class SplitActivation:
    validation_issues: tuple[str, ...]
    validated: ValidatedSplit | None
    execution: SplitExecutionPlan | None
    bindings: tuple[SplitDispatchBinding, ...]

    def __post_init__(self) -> None:
        accepted = self.validated is not None
        if accepted != (self.execution is not None) or accepted == bool(
            self.validation_issues
        ):
            raise ContractError("Split activation capability and issues disagree")
        if not accepted and self.bindings:
            raise ContractError("rejected Split activation cannot expose bindings")

    @property
    def accepted(self) -> bool:
        return self.validated is not None

    @property
    def issue_codes(self) -> tuple[str, ...]:
        return self.validation_issues


@dataclass(frozen=True)
class SplitDriveResult:
    disposition: str
    reason: str
    launch_receipts: tuple[SplitLaunchReceipt, ...]

    def __post_init__(self) -> None:
        if self.disposition not in DRIVE_DISPOSITIONS or not self.reason:
            raise ContractError("Split drive result is invalid")


def _binding_locality(binding: SplitDispatchBinding) -> WorkspaceLocality:
    return WorkspaceLocality(
        subplan_id=binding.subplan_id,
        workspace_id=binding.request_id,
        worktree_path=binding.worktree_path,
        executor_placement=CHILD_PLACEMENT,
        review_placement=CHILD_PLACEMENT,
        verification_placement=CHILD_PLACEMENT,
    )


def compile_activation(
    manifest: SplitManifest,
    *,
    current_plan_sha256: str,
    current_outcome_contract_sha256: str,
    registered_pipelines: Iterable[str],
    bindings: tuple[SplitDispatchBinding, ...],
) -> SplitActivation:
    """Validate every zero-effect class before binding any dispatch effect."""

    validation = validate_manifest(
        manifest,
        current_plan_sha256=current_plan_sha256,
        current_outcome_contract_sha256=current_outcome_contract_sha256,
        registered_pipelines=registered_pipelines,
    )
    if not validation.accepted:
        return SplitActivation(validation.issue_codes, None, None, ())
    if not isinstance(bindings, tuple) or any(
        not isinstance(item, SplitDispatchBinding) for item in bindings
    ):
        raise ContractError("Split bindings must be a typed tuple")
    expected_ids = tuple(item.subplan_id for item in manifest.subplans)
    observed_ids = tuple(item.subplan_id for item in bindings)
    if observed_ids != expected_ids:
        raise ContractError("Split bindings changed manifest order or coverage")
    if len({item.request_id for item in bindings}) != len(bindings):
        raise ContractError("Split child request ids must be unique")
    if len({item.worktree_path for item in bindings}) != len(bindings):
        raise ContractError("Split child worktrees must be unique")
    for child, binding in zip(manifest.subplans, bindings, strict=True):
        if (
            binding.manifest_sha256 != manifest.manifest_sha256
            or binding.pipeline != child.pipeline
            or binding.route_alias != child.route_alias
            or binding.budget != child.budget
        ):
            raise ContractError(f"{child.subplan_id} dispatch binding drifted")
    locality = {
        binding.subplan_id: _binding_locality(binding) for binding in bindings
    }
    assert validation.validated is not None
    execution = schedule_waves(validation.validated, locality)
    return SplitActivation((), validation.validated, execution, bindings)


def _binding_map(activation: SplitActivation) -> dict[str, SplitDispatchBinding]:
    return {item.subplan_id: item for item in activation.bindings}


def _manifest_order(activation: SplitActivation) -> tuple[str, ...]:
    assert activation.validated is not None
    return tuple(item.subplan_id for item in activation.validated.manifest.subplans)


def _ordered_subset(observed: tuple[str, ...], expected: tuple[str, ...], label: str) -> None:
    if len(set(observed)) != len(observed) or observed != tuple(
        item for item in expected if item in set(observed)
    ):
        raise ContractError(f"{label} changed manifest order or contains duplicates")


def _validate_launches(
    activation: SplitActivation,
    receipts: tuple[SplitLaunchReceipt, ...],
) -> dict[str, SplitLaunchReceipt]:
    if not isinstance(receipts, tuple) or any(
        not isinstance(item, SplitLaunchReceipt) for item in receipts
    ):
        raise ContractError("Split launch receipts must be a typed tuple")
    expected = _manifest_order(activation)
    _ordered_subset(tuple(item.subplan_id for item in receipts), expected, "launch receipts")
    bindings = _binding_map(activation)
    manifest_sha = activation.validated.manifest.manifest_sha256  # type: ignore[union-attr]
    for receipt in receipts:
        binding = bindings[receipt.subplan_id]
        if (
            receipt.manifest_sha256 != manifest_sha
            or receipt.request_id != binding.request_id
            or receipt.worktree_path != binding.worktree_path
            or receipt.placement != "workspace"
        ):
            raise ContractError(f"{receipt.subplan_id} launch receipt drifted")
    return {item.subplan_id: item for item in receipts}


def _validate_terminals(
    activation: SplitActivation,
    receipts: tuple[SplitTerminalReceipt, ...],
    launches: Mapping[str, SplitLaunchReceipt],
) -> dict[str, SplitTerminalReceipt]:
    if not isinstance(receipts, tuple) or any(
        not isinstance(item, SplitTerminalReceipt) for item in receipts
    ):
        raise ContractError("Split terminal receipts must be a typed tuple")
    expected = _manifest_order(activation)
    observed = tuple(item.child.subplan_id for item in receipts)
    _ordered_subset(observed, expected, "terminal receipts")
    bindings = _binding_map(activation)
    candidates = {
        item.subplan_id: item for item in activation.validated.manifest.subplans  # type: ignore[union-attr]
    }
    manifest_sha = activation.validated.manifest.manifest_sha256  # type: ignore[union-attr]
    for receipt in receipts:
        subplan_id = receipt.child.subplan_id
        binding = bindings[subplan_id]
        launch = launches.get(subplan_id)
        candidate = candidates[subplan_id]
        if launch is None:
            raise ContractError(f"{subplan_id} terminal receipt has no launch receipt")
        if (
            receipt.child.manifest_sha256 != manifest_sha
            or receipt.child.evidence_ids != candidate.evidence_ids
            or receipt.request_id != binding.request_id
            or receipt.request_id != launch.request_id
            or receipt.workspace_id != launch.workspace_id
            or receipt.worktree_path != binding.worktree_path
            or receipt.worktree_path != launch.worktree_path
        ):
            raise ContractError(f"{subplan_id} terminal receipt identity drifted")
    return {item.child.subplan_id: item for item in receipts}


def drive_split(
    activation: SplitActivation,
    *,
    terminal_receipts: tuple[SplitTerminalReceipt, ...],
    launch_receipts: tuple[SplitLaunchReceipt, ...],
    launch: Callable[[SplitDispatchBinding], SplitLaunchReceipt],
) -> SplitDriveResult:
    """Launch at most one deterministic ready wave through an injected adapter."""

    if not activation.accepted:
        return SplitDriveResult(
            "rejected",
            "Split manifest did not produce a validation capability",
            (),
        )
    try:
        launches = _validate_launches(activation, launch_receipts)
        terminals = _validate_terminals(activation, terminal_receipts, launches)
    except ContractError as exc:
        return SplitDriveResult("attention-required", str(exc), launch_receipts)

    for subplan_id in _manifest_order(activation):
        terminal = terminals.get(subplan_id)
        if terminal is None:
            continue
        if not terminal.resources_closed:
            return SplitDriveResult(
                "attention-required",
                f"{subplan_id} terminal resources are not closed",
                launch_receipts,
            )
        if terminal.child.status != "approved":
            return SplitDriveResult(
                terminal.child.status,
                f"{subplan_id} ended {terminal.child.status}",
                launch_receipts,
            )

    order = _manifest_order(activation)
    if len(terminals) == len(order):
        return SplitDriveResult(
            "ready-to-join",
            "all exact children are approved and resource-free",
            launch_receipts,
        )

    assert activation.execution is not None
    completed = set(terminals)
    active = set(launches) - completed
    wave = next(
        wave
        for wave in activation.execution.waves
        if any(child.subplan_id not in completed for child in wave.children)
    )
    candidates = {
        item.subplan_id: item for item in activation.validated.manifest.subplans  # type: ignore[union-attr]
    }
    capacity = max(0, activation.execution.max_parallel - len(active))
    ready = [
        child.subplan_id
        for child in wave.children
        if child.subplan_id not in launches
        and set(candidates[child.subplan_id].dependencies).issubset(completed)
    ][:capacity]
    if not ready:
        return SplitDriveResult(
            "awaiting-children",
            "the current wave is awaiting exact terminal child receipts",
            launch_receipts,
        )

    emitted = list(launch_receipts)
    bindings = _binding_map(activation)
    for subplan_id in ready:
        binding = bindings[subplan_id]
        try:
            receipt = launch(binding)
            candidate_launches = _validate_launches(
                activation, (*tuple(emitted), receipt)
            )
        except Exception as exc:
            return SplitDriveResult(
                "attention-required",
                f"{subplan_id} dispatch effect did not return an exact receipt: {exc}",
                tuple(emitted),
            )
        emitted.append(candidate_launches[subplan_id])
    return SplitDriveResult(
        "awaiting-children",
        f"launched bounded wave {wave.wave}",
        tuple(emitted),
    )


def join_split(
    activation: SplitActivation,
    *,
    launch_receipts: tuple[SplitLaunchReceipt, ...],
    terminal_receipts: tuple[SplitTerminalReceipt, ...],
    current_heads: Mapping[str, str],
) -> JoinDecision:
    """Join only exact manifest-ordered, approved, resource-free children."""

    if not activation.accepted:
        return JoinDecision("receipt-invalid", "Split activation was rejected")
    try:
        launch_map = _validate_launches(activation, launch_receipts)
        terminals = _validate_terminals(activation, terminal_receipts, launch_map)
    except ContractError as exc:
        return JoinDecision("receipt-invalid", str(exc))
    if (
        len(launch_map) != len(_manifest_order(activation))
        or len(terminals) != len(_manifest_order(activation))
    ):
        return JoinDecision("receipt-invalid", "terminal receipt coverage is incomplete")
    for subplan_id in _manifest_order(activation):
        terminal = terminals[subplan_id]
        if not terminal.resources_closed:
            return JoinDecision(
                "attention-required",
                f"{subplan_id} terminal resources are not closed",
            )
    assert activation.validated is not None
    return evaluate_join(
        activation.validated,
        tuple(item.child for item in terminal_receipts),
        current_heads=current_heads,
    )
