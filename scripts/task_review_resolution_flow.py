"""Continue an exact review session after typed finding resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from harness.workflows.review import ReviewContext, ReviewRound
from harness.workflows.review_gate import (
    ReviewGateController,
    ReviewGateRun,
    ReviewPreset,
)
from task_review_context import _callback_path, _context, _prompt
from task_review_resolution_bundle import _resolution_bundle
from task_review_shared import TaskReviewError
from task_review_transport import _receipt, _write_round_meta


def _continue_resolution(
    *,
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    task_id: str,
    gate_root: Path,
    gate: ReviewGateController,
    state: Mapping[str, Any],
    run: ReviewGateRun,
    context: ReviewContext,
    context_manifest: Path,
    preset: ReviewPreset,
) -> dict[str, Any]:
    awaiting = state.get("awaiting_resolution")
    if not isinstance(awaiting, dict) or not awaiting:
        raise TaskReviewError("awaiting review has no finding evidence")
    if any(
        not isinstance(value, dict)
        or not str(value.get("reviewed_head_sha") or "")
        for value in awaiting.values()
    ):
        raise TaskReviewError("review resolution boundary is invalid")
    reviewed_heads = {
        str(value["reviewed_head_sha"]) for value in awaiting.values()
    }
    if reviewed_heads == {context.head_sha}:
        return _receipt(
            status="awaiting-resolution",
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=run,
        )
    bundle = _resolution_bundle(
        worktree,
        gate_root,
        task_id,
        awaiting,
        context.head_sha,
        persisted_identity_sha256=str(
            state.get("resolution_transport_identity_sha256") or ""
        ),
        persisted_resolution_pointers=(
            state.get("resolution_evidence")
            if isinstance(state.get("resolution_evidence"), dict)
            else {}
        ),
    )
    context, context_manifest = _context(
        meta,
        vault,
        worktree,
        runtime_root,
        task_id,
        resolution_bundle=bundle,
    )
    decision = None
    for lane in run.execution.lanes:
        if lane.axis not in awaiting:
            continue
        pointer = _prompt(
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context=context,
            axis=lane.axis,
            verification=True,
        )

        def prepare_round(
            next_lane: object, round_: ReviewRound
        ) -> None:
            _write_round_meta(
                runtime_root=runtime_root,
                vault=vault,
                worktree=worktree,
                task_id=task_id,
                depth=preset.depth,
                context=context,
                lane_operation_id=round_.parent_operation_id,
                round_=round_,
            )

        decision = gate.continue_after_resolution(
            run,
            lane,
            context=context,
            resolution=bundle.by_axis[lane.axis],
            review_identity_sha256=bundle.review_identity_sha256,
            verification_prompt_pointer=pointer,
            callback_pointer=(
                _callback_path(runtime_root, lane.axis)
                .relative_to(runtime_root)
                .as_posix()
            ),
            prepare_round=prepare_round,
        )
        if decision.action == "attention-required":
            break
    next_status = (
        decision.action
        if decision is not None and decision.action == "attention-required"
        else str(gate.read().get("status") or "")
    )
    return _receipt(
        status=next_status,
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        context_manifest=context_manifest,
        run=gate.rehydrate(),
    )
