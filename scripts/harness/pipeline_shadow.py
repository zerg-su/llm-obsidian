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
