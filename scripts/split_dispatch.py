"""Bind activated Split waves to the existing dispatch result contract."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from dispatch_custom_contracts import compiled_pipeline_for_request
from harness.contracts import ContractError
from harness.split_activation import (
    SplitActivation,
    SplitDispatchBinding,
    SplitDriveResult,
    SplitLaunchReceipt,
    SplitTerminalReceipt,
    compile_activation,
    drive_split,
    parse_split_child_policy,
    split_child_policy,
)
from harness.split_contracts import SplitManifest


@dataclass(frozen=True)
class DispatchChildRequest:
    """One already-normalized existing dispatch request and its immutable bytes."""

    subplan_id: str
    request_sha256: str
    request: Mapping[str, Any]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request_sha256, str)
            or len(self.request_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.request_sha256)
        ):
            raise ContractError("dispatch child request_sha256 must be a lowercase sha256")
        if not isinstance(self.request, Mapping):
            raise ContractError("dispatch child request must be a mapping")
        object.__setattr__(self, "request", MappingProxyType(dict(self.request)))


@dataclass(frozen=True)
class PreparedSplitDispatch:
    activation: SplitActivation
    children: tuple[DispatchChildRequest, ...]

    def __post_init__(self) -> None:
        if self.activation.accepted != bool(self.children):
            raise ContractError("prepared Split dispatch capability and children disagree")


def prepare_split_dispatch(
    manifest: SplitManifest,
    *,
    current_plan_sha256: str,
    current_outcome_contract_sha256: str,
    registered_pipelines: Iterable[str],
    children: tuple[DispatchChildRequest, ...],
) -> PreparedSplitDispatch:
    """Compile all existing dispatch bindings before any child launch effect."""

    # Preserve the eight-class zero-effect precedence.  A rejected manifest
    # never causes child request parsing, config resolution, or dispatch.
    from harness.split_validation import validate_manifest

    checked = validate_manifest(
        manifest,
        current_plan_sha256=current_plan_sha256,
        current_outcome_contract_sha256=current_outcome_contract_sha256,
        registered_pipelines=registered_pipelines,
    )
    if not checked.accepted:
        return PreparedSplitDispatch(
            SplitActivation(checked.issue_codes, None, None, ()),
            (),
        )
    if not isinstance(children, tuple) or any(
        not isinstance(item, DispatchChildRequest) for item in children
    ):
        raise ContractError("Split dispatch children must be a typed tuple")
    expected_ids = tuple(item.subplan_id for item in manifest.subplans)
    if tuple(item.subplan_id for item in children) != expected_ids:
        raise ContractError("Split dispatch requests changed manifest order or coverage")

    bindings: list[SplitDispatchBinding] = []
    for candidate, child in zip(manifest.subplans, children, strict=True):
        request = child.request
        policy = parse_split_child_policy(request.get("split"))
        if policy != split_child_policy(manifest, candidate):
            raise ContractError(f"{candidate.subplan_id} task Split policy drifted")
        worktree = request.get("worktree")
        worktree_path = str(worktree) if isinstance(worktree, Path) else ""
        binding = SplitDispatchBinding(
            manifest_sha256=manifest.manifest_sha256,
            base_sha=manifest.parent.base_sha,
            subplan_id=candidate.subplan_id,
            request_id=str(request.get("request_id") or ""),
            pipeline=str(request.get("pipeline") or ""),
            route_alias=policy.route_alias,
            worktree_path=worktree_path,
            placement=str(request.get("placement") or ""),
            budget=policy.budget,
        )
        compiled_budget = compiled_pipeline_for_request(dict(request)).worst_case_budget
        if (
            compiled_budget.token_limit > policy.budget.token_limit
            or compiled_budget.time_budget_seconds > policy.budget.time_budget_seconds
        ):
            raise ContractError(
                f"{candidate.subplan_id} pipeline exceeds its frozen child budget"
            )
        bindings.append(binding)

    activation = compile_activation(
        manifest,
        current_plan_sha256=current_plan_sha256,
        current_outcome_contract_sha256=current_outcome_contract_sha256,
        registered_pipelines=registered_pipelines,
        bindings=tuple(bindings),
    )
    return PreparedSplitDispatch(activation, children)


def _launch_receipt(
    binding: SplitDispatchBinding,
    result: Mapping[str, object],
) -> SplitLaunchReceipt:
    if not isinstance(result, Mapping):
        raise ContractError("existing dispatch adapter returned no result mapping")
    return SplitLaunchReceipt(
        manifest_sha256=binding.manifest_sha256,
        base_sha=binding.base_sha,
        subplan_id=binding.subplan_id,
        request_id=str(result.get("request_id") or ""),
        workspace_id=str(result.get("task_workspace") or ""),
        worktree_path=str(result.get("worktree") or ""),
        surface_id=str(result.get("task_surface") or ""),
        placement=str(result.get("placement") or ""),
    )


def drive_split_dispatch(
    prepared: PreparedSplitDispatch,
    *,
    terminal_receipts: tuple[SplitTerminalReceipt, ...],
    launch_receipts: tuple[SplitLaunchReceipt, ...],
    start_dispatch: Callable[[Mapping[str, Any], str], Mapping[str, object]],
    persist_launch: Callable[
        [SplitLaunchReceipt, str], SplitLaunchReceipt
    ] | None = None,
) -> SplitDriveResult:
    """Drive one wave through the existing dispatch execution facade."""

    by_id = {item.subplan_id: item for item in prepared.children}

    def launch(binding: SplitDispatchBinding) -> SplitLaunchReceipt:
        child = by_id[binding.subplan_id]
        result = start_dispatch(child.request, child.request_sha256)
        receipt = _launch_receipt(binding, result)
        return (
            persist_launch(receipt, child.request_sha256)
            if persist_launch is not None
            else receipt
        )

    return drive_split(
        prepared.activation,
        terminal_receipts=terminal_receipts,
        launch_receipts=launch_receipts,
        launch=launch,
    )
