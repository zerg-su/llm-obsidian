"""Content-free shadow parity for compiled and existing workflow seams."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import ContractError, OperationSpec
from .pipelines import CompiledPipeline, _identifier, _sha256


@dataclass(frozen=True)
class PipelineShadowReport:
    pipeline_id: str
    profile: str
    definition_sha256: str
    owner_id: str
    expected_steps: tuple[str, ...]
    observed_steps: tuple[str, ...]
    mismatches: tuple[str, ...]
    parity: bool
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported PipelineShadowReport schema")
        _identifier(self.pipeline_id, "shadow pipeline_id")
        _identifier(self.profile, "shadow profile")
        _sha256(self.definition_sha256, "shadow definition")
        _identifier(self.owner_id, "shadow owner_id")
        if self.parity != (not self.mismatches):
            raise ContractError("shadow parity must agree with its mismatches")


@dataclass(frozen=True)
class StagedOperationShadowReport:
    flow_id: str
    parent_kind: str
    stage_kinds: tuple[str, ...]
    distinct_operations: int
    mismatches: tuple[str, ...]
    parity: bool
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.distinct_operations < 0:
            raise ContractError("invalid staged shadow metadata")
        _identifier(self.flow_id, "staged shadow flow_id")
        _identifier(self.parent_kind, "staged shadow parent_kind")
        if self.parity != (not self.mismatches):
            raise ContractError("staged shadow parity disagrees with mismatches")


def shadow_lifecycle(
    compiled: CompiledPipeline,
    *,
    dispatch: OperationSpec,
    review: OperationSpec,
) -> PipelineShadowReport:
    """Compare the existing lifecycle specs without launching either path."""

    definition = compiled.definition
    if (definition.pipeline_id, definition.profile) != ("lifecycle", "default"):
        raise ContractError("lifecycle shadow requires lifecycle/default")
    expected = tuple(
        step.step_id
        for step in definition.steps
        if step.session_mode != "controller"
    )
    mismatches: list[str] = []
    if expected != ("dispatch", "review"):
        mismatches.append("compiled lifecycle step order drifted")
    expected_modes = tuple(
        step.session_mode
        for step in definition.steps
        if step.session_mode != "controller"
    )
    if expected_modes != ("worktree", "review"):
        mismatches.append("compiled lifecycle session mapping drifted")
    if dispatch.owner_id != review.owner_id:
        mismatches.append("dispatch and review owners differ")
    if dispatch.kind != "dispatch":
        mismatches.append(
            f"dispatch: expected worktree session, observed {dispatch.kind}"
        )
    if review.kind not in {"simple-review", "deep-review-spec"}:
        mismatches.append(
            f"review: expected review session, observed {review.kind}"
        )
    observed = (
        "dispatch" if dispatch.kind == "dispatch" else dispatch.kind,
        (
            "review"
            if review.kind in {"simple-review", "deep-review-spec"}
            else review.kind
        ),
    )
    return PipelineShadowReport(
        pipeline_id=definition.pipeline_id,
        profile=definition.profile,
        definition_sha256=compiled.definition_sha256,
        owner_id=dispatch.owner_id,
        expected_steps=expected,
        observed_steps=observed,
        mismatches=tuple(mismatches),
        parity=not mismatches,
    )


def shadow_staged_operations(
    *,
    flow_id: str,
    parent: OperationSpec,
    stages: tuple[OperationSpec, ...],
    expected_kinds: tuple[str, ...],
) -> StagedOperationShadowReport:
    """Prove an existing workflow uses distinct owner-scoped stage operations."""

    mismatches: list[str] = []
    stage_kinds = tuple(stage.kind for stage in stages)
    if stage_kinds != expected_kinds:
        mismatches.append("stage operation kinds differ from the frozen pattern")
    if any(stage.owner_id != parent.owner_id for stage in stages):
        mismatches.append("stage operation owner differs from its parent")
    operation_ids = (parent.operation_id,) + tuple(
        stage.operation_id for stage in stages
    )
    idempotency_keys = (parent.idempotency_key,) + tuple(
        stage.idempotency_key for stage in stages
    )
    distinct_operations = len(set(operation_ids))
    if distinct_operations != len(operation_ids):
        mismatches.append("stage operation identities are not unique")
    if len(set(idempotency_keys)) != len(idempotency_keys):
        mismatches.append("stage replay identities are not unique")
    return StagedOperationShadowReport(
        flow_id=flow_id,
        parent_kind=parent.kind,
        stage_kinds=stage_kinds,
        distinct_operations=distinct_operations,
        mismatches=tuple(mismatches),
        parity=not mismatches,
    )
