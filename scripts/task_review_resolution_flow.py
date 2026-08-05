"""Continue an exact review session after typed finding resolution."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from harness.runtime_session_contracts import continuation_effect_id
from harness.review_attempt import LEGACY_CROSS_HEAD_RESUME_DISABLED
from harness.workflows.review import (
    ReviewContext,
    ReviewRound,
    prepare_review_round,
)
from harness.workflows.review_gate import (
    ReviewGateController,
    ReviewGateRun,
    ReviewPreset,
)
from task_review_context import _callback_path, _context, _prompt
from task_review_delta_packet import (
    DeltaPacketError,
    validate_materialized_delta_packet,
)
from task_review_resolution_bundle import _resolution_bundle
from task_review_shared import ResolutionBundle, TaskReviewError, _git
from task_review_transport import _receipt, _write_round_meta


def legacy_cross_head_resume_disabled() -> dict[str, object]:
    """Return the only resume disposition exposed to the exact-HEAD path."""

    return LEGACY_CROSS_HEAD_RESUME_DISABLED.payload()


def _prompt_effect_id(
    runtime_root: Path,
    pointer: str,
    *,
    callback_target_path: Path | None = None,
    callback_operation_id: str = "",
    callback_run_id: str = "",
) -> str:
    """Bind a continuation receipt to the exact materialized prompt bytes."""

    prompt_path = (runtime_root / pointer).resolve()
    if (
        runtime_root.resolve() not in prompt_path.parents
        or not prompt_path.is_file()
        or prompt_path.is_symlink()
    ):
        raise TaskReviewError("review verification prompt is unavailable")
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise TaskReviewError(
            "review verification prompt is unavailable"
        ) from exc
    if callback_target_path is None:
        return continuation_effect_id(prompt)
    try:
        if callback_target_path.is_symlink() or not callback_target_path.is_file():
            raise TaskReviewError("review callback target is unavailable")
        target = json.loads(callback_target_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskReviewError("review callback target is unavailable") from exc
    generation = target.get("generation") if isinstance(target, dict) else None
    target_operation_id = (
        str(target.get("operation_id") or "")
        if isinstance(target, dict)
        else ""
    )
    target_run_id = (
        str(target.get("run_id") or "") if isinstance(target, dict) else ""
    )
    if (
        not isinstance(target, dict)
        or target.get("schema_version") != 1
        or type(generation) is not int
        or generation < 1
        or not callback_operation_id
        or not callback_run_id
    ):
        raise TaskReviewError("review callback target is invalid")
    if (
        target_operation_id != callback_operation_id
        or target_run_id != callback_run_id
    ):
        generation += 1
    bound_prompt = "\n".join(
        (
            prompt,
            str(generation),
            callback_operation_id,
            callback_run_id,
        )
    )
    return continuation_effect_id(bound_prompt)


def _resolution_packet_ready(
    gate: ReviewGateController,
    run: ReviewGateRun,
    context_manifest: Path,
    bundle: ResolutionBundle,
) -> bool:
    """Validate the complete exact-HEAD packet before any provider prompt."""

    try:
        delta = validate_materialized_delta_packet(
            context_manifest,
            expected_reviewed_head=(
                bundle.resolution.reviewed_head_sha
            ),
            expected_resolved_head=(
                bundle.resolution.resolved_head_sha
            ),
            expected_review_identity_sha256=(
                bundle.review_identity_sha256
            ),
        )
        if delta != bundle.fix_delta:
            raise DeltaPacketError(
                "materialized review delta differs from Git evidence"
            )
    except (DeltaPacketError, OSError):
        gate._mark_attention(run.execution.lanes)
        return False
    return True


def _preload_resolution_bundle(
    *,
    worktree: Path,
    gate_root: Path,
    task_id: str,
    state: Mapping[str, Any],
) -> ResolutionBundle | None:
    """Bind a moved current HEAD to its exact awaiting-resolution evidence."""

    status = str(state.get("status") or "")
    if status not in {"awaiting-resolution", "verifying"}:
        return None
    awaiting = state.get("awaiting_resolution")
    if not isinstance(awaiting, dict):
        return None
    persisted_pointers = (
        state.get("resolution_evidence")
        if isinstance(state.get("resolution_evidence"), dict)
        else {}
    )
    if status == "awaiting-resolution" and not awaiting:
        return None
    if status == "verifying" and not persisted_pointers:
        return None
    reviewed_heads = {
        str(value.get("reviewed_head_sha") or "")
        for value in awaiting.values()
        if isinstance(value, dict)
    }
    resolved_head = _git(worktree, "rev-parse", "HEAD")
    if status == "awaiting-resolution" and reviewed_heads == {resolved_head}:
        return None
    if status == "verifying":
        bound = state.get("context")
        if (
            not isinstance(bound, dict)
            or str(bound.get("head_sha") or "") != resolved_head
        ):
            raise TaskReviewError(
                "verifying review resolution context targets another HEAD"
            )
    return _resolution_bundle(
        worktree,
        gate_root,
        task_id,
        awaiting,
        resolved_head,
        persisted_identity_sha256=str(
            state.get("resolution_transport_identity_sha256") or ""
        ),
        persisted_resolution_pointers=persisted_pointers,
    )


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
    if not _resolution_packet_ready(
        gate, run, context_manifest, bundle
    ):
        return _receipt(
            status="attention-required",
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=gate.rehydrate(),
        )
    for lane in run.execution.lanes:
        if (
            lane.axis in awaiting
            or lane.verification_iteration < 1
            or lane.axis not in bundle.by_axis
            or lane.axis not in run.rounds
        ):
            continue
        gate.backfill_succeeded_continuation_receipt(
            lane,
            run.rounds[lane.axis],
            bundle.by_axis[lane.axis],
        )
    decision = None
    for lane in run.execution.lanes:
        if lane.axis not in awaiting:
            continue
        if not _resolution_packet_ready(
            gate, run, context_manifest, bundle
        ):
            return _receipt(
                status="attention-required",
                meta=meta,
                vault=vault,
                worktree=worktree,
                runtime_root=runtime_root,
                context_manifest=context_manifest,
                run=gate.rehydrate(),
            )
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

        next_round = prepare_review_round(
            gate.round_store,
            replace(
                lane,
                verification_iteration=lane.verification_iteration + 1,
            ),
        )
        callback_target_path = (
            vault
            / ".vault-meta"
            / "harness"
            / "owners"
            / lane.owner_id
            / "runtime"
            / lane.operation_id
            / "callback-target.json"
        )
        effect_id = (
            _prompt_effect_id(
                runtime_root,
                pointer,
                callback_target_path=callback_target_path,
                callback_operation_id=next_round.operation_id,
                callback_run_id=next_round.run_id,
            )
            if callback_target_path.is_file()
            and not callback_target_path.is_symlink()
            else _prompt_effect_id(runtime_root, pointer)
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
            continuation_effect_id=effect_id,
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
