"""Automatic review gate driving and replay-safe telemetry."""

from __future__ import annotations

import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from harness.contracts import AttentionReason
from harness.review_attempt import (
    LEGACY_CROSS_HEAD_RESUME_DISABLED,
    ReviewAttempt,
    ReviewAttemptError,
)
from harness.runtime_sessions import RuntimeSessionManager
from harness.state_machine import TERMINAL
from harness.store import OperationStore, StoreError
from harness.workflows.review import (
    ReviewContext,
    ReviewOperationRequest,
    ReviewResult,
    ReviewRound,
    review_session_specs,
    runtime_status_is_live,
)
from harness.workflows.review_gate import (
    ReviewGateController,
    ReviewGateRun,
    ReviewPreset,
)
from harness.workflows.review_gate_attempt import (
    compile_review_attempt_identity,
)
from review_resolution import MATERIAL_SEVERITIES
from review_telemetry import emit_review_event
from task_review_context import (
    _callback_path,
    _context,
    _envelope,
    _gate_root,
    _prompt,
    _request,
)
from task_review_resolution_bundle import _resolution_bundle
from task_review_shared import (
    ActiveReviewRound,
    StaleRoundCallbackError,
    TaskReviewError,
    _atomic_json,
)
from task_review_verification import _finalizing_resubmit_recovery
from task_review_replay import _pending_replay_is_safe
from task_review_resolution_flow import (
    _continue_resolution,
    _preload_resolution_bundle,
)
from task_review_transport import (
    _callback_wake,
    _collect_ready_results,
    _emit_round_telemetry,
    _receipt,
    _record_accepted_result,
    _write_round_meta,
    load_active_round,
)
def _pending_gate_replay(
    gate: ReviewGateController, store: OperationStore, task_id: str
) -> bool:
    if not gate.state_path.exists():
        return False
    initial_state = gate.read()
    if (
        initial_state.get("status") == "attention-required"
        and initial_state.get("lanes") == []
    ):
        try:
            dispatch_record = store.read(task_id, task_id)
        except StoreError:
            dispatch_record = None
        if (
            dispatch_record is not None
            and dispatch_record.state
            not in {"attention-required", *TERMINAL}
        ):
            gate.resume_unbound_attention()
            initial_state = gate.read()
    pending = initial_state.get("status") == "pending"
    if pending and initial_state.get("lanes") != []:
        raise TaskReviewError("pending review gate already owns lanes")
    return pending


def _start_review(
    *,
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    task_id: str,
    runtime_root: Path,
    context: ReviewContext,
    context_manifest: Path,
    preset: ReviewPreset,
    request: ReviewOperationRequest | None,
    gate_root: Path,
    gate: ReviewGateController,
    store: OperationStore,
    runtime: object,
    pending_replay: bool,
) -> dict[str, Any]:
    if not preset.enabled:
        ReviewGateController.skip(
            gate_root,
            dispatch_operation_id=task_id,
            owner_id=task_id,
            preset=preset,
            context=context,
            product_root=worktree,
        )
        return _receipt(
            status="skipped",
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
        )
    if request is None:
        raise TaskReviewError("enabled review has no request")
    if pending_replay and not _pending_replay_is_safe(
        request, store, gate, runtime
    ):
        return _receipt(
            status="attention-required",
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
        )
    prompt_pointers = {
        axis: _prompt(
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context=context,
            axis=axis,
            verification=False,
        )
        for axis in request.policy.axes
    }

    def prepare_lane(
        axis: str,
        _session_request: object,
        _result: object,
        round_: ReviewRound,
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

    try:
        run = gate.begin(
            dispatch_operation_id=task_id,
            request=request,
            origin_surface=str(meta.get("task_surface") or ""),
            cwd=runtime_root,
            product_root=worktree,
            prompt_pointer=prompt_pointers[request.policy.axes[0]],
            prompt_pointers=prompt_pointers,
            callback_root="callbacks",
            callback_wake=_callback_wake(meta, vault, worktree),
            prepare_lane=prepare_lane,
        )
    except ValueError:
        if pending_replay and not _pending_replay_is_safe(
            request, store, gate, runtime
        ):
            return _receipt(
                status="attention-required",
                meta=meta,
                vault=vault,
                worktree=worktree,
                runtime_root=runtime_root,
                context_manifest=context_manifest,
            )
        raise
    return _receipt(
        status="reviewing",
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        context_manifest=context_manifest,
        run=run,
    )


EXACT_HEAD_REVIEW_PROTOCOL = "exact-head-attempt-v1"


def _exact_head_attempt_enabled(meta: Mapping[str, Any]) -> bool:
    policy = meta.get("review_policy")
    return isinstance(policy, Mapping) and (
        policy.get("execution_protocol") == EXACT_HEAD_REVIEW_PROTOCOL
    )


def _legacy_resume_disabled_receipt(
    *,
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
) -> dict[str, Any]:
    disposition = LEGACY_CROSS_HEAD_RESUME_DISABLED.payload()
    return {
        **disposition,
        "review_purpose": str(
            meta.get("review_policy", {}).get("purpose")
            if isinstance(meta.get("review_policy"), Mapping)
            else "implementation"
        )
        or "implementation",
        "task_id": meta["task_id"],
        "worktree": str(worktree),
        "vault_root": str(vault),
        "context_manifest": "",
        "lanes": [],
    }


def _attempt_binding(
    meta: Mapping[str, Any], task_id: str
) -> tuple[str, int, str, str]:
    policy = meta.get("review_policy")
    if not isinstance(policy, Mapping):
        raise TaskReviewError("exact-HEAD review policy is unavailable")
    lineage = str(policy.get("finalization_lineage_id") or task_id)
    cycle = policy.get("finalization_cycle", 1)
    plan_sha256 = str(meta.get("approved_plan_sha256") or "")
    outcome_sha256 = str(meta.get("outcome_contract_sha256") or "")
    if type(cycle) is not int:
        raise TaskReviewError("exact-HEAD review cycle is invalid")
    return lineage, cycle, plan_sha256, outcome_sha256


def _start_exact_head_review(
    *,
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    task_id: str,
    runtime_root: Path,
    context: ReviewContext,
    context_manifest: Path,
    preset: ReviewPreset,
    request: ReviewOperationRequest | None,
    gate: ReviewGateController,
) -> dict[str, Any]:
    if not preset.enabled:
        raise TaskReviewError(
            "exact-HEAD attempt protocol requires an enabled review"
        )
    if request is None:
        raise TaskReviewError("enabled review has no request")
    prompt_pointers = {
        axis: _prompt(
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context=context,
            axis=axis,
            verification=False,
        )
        for axis in request.policy.axes
    }

    def prepare_lane(
        axis: str,
        _session_request: object,
        _result: object,
        round_: ReviewRound,
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

    lineage, cycle, plan_sha256, outcome_sha256 = _attempt_binding(
        meta, task_id
    )
    run = gate.begin_attempt(
        dispatch_operation_id=task_id,
        finalization_lineage_id=lineage,
        cycle=cycle,
        plan_sha256=plan_sha256,
        outcome_sha256=outcome_sha256,
        request=request,
        origin_surface=str(meta.get("task_surface") or ""),
        cwd=runtime_root,
        product_root=worktree,
        prompt_pointer=prompt_pointers[request.policy.axes[0]],
        prompt_pointers=prompt_pointers,
        callback_root="callbacks",
        callback_wake=_callback_wake(meta, vault, worktree),
        prepare_lane=prepare_lane,
    )
    return _receipt(
        status="reviewing",
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        context_manifest=context_manifest,
        run=run,
    )








def _requires_lane_barrier(preset: ReviewPreset) -> bool:
    return preset.depth in {"deep", "full"}


def _ready_result_is_recorded(
    gate: ReviewGateController,
    state: Mapping[str, Any],
    result: ReviewResult,
) -> bool:
    """Match the exact axis iteration, not merely an axis seen before."""

    raw_results = state.get("round_results")
    if not isinstance(raw_results, dict):
        return False
    pointer = raw_results.get(result.axis)
    if not isinstance(pointer, str) or not pointer:
        return False
    path = (gate.root / pointer).resolve()
    if gate.root not in path.parents or not path.is_file() or path.is_symlink():
        raise TaskReviewError("recorded review result pointer is invalid")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskReviewError("recorded review result is invalid") from exc
    if not isinstance(payload, dict):
        raise TaskReviewError("recorded review result is invalid")
    return (
        payload.get("axis") == result.axis
        and payload.get("verification_iteration")
        == result.verification_iteration
    )


def _should_defer_ready_results(
    preset: ReviewPreset,
    *,
    purpose: str,
    has_material: bool,
    already_awaiting: bool = False,
) -> bool:
    return _requires_lane_barrier(preset) and (
        already_awaiting or (purpose != "release" and has_material)
    )


def _complete_ready_results(
    *,
    gate: ReviewGateController,
    run: ReviewGateRun,
    ready: list[tuple[object, ReviewRound, ReviewResult]],
    preset: ReviewPreset,
    context: ReviewContext,
    worktree: Path,
    vault: Path,
    runtime_root: Path,
    already_awaiting: bool = False,
) -> None:
    has_material = any(
        result.verdict == "changes-requested"
        and any(
            finding.severity in MATERIAL_SEVERITIES
            for finding in result.findings
        )
        for _lane, _round, result in ready
    )
    defer_resolution = _should_defer_ready_results(
        preset,
        purpose=context.purpose,
        has_material=has_material,
        already_awaiting=already_awaiting,
    )
    ordered = (
        ready
        if defer_resolution or context.purpose != "release"
        else sorted(ready, key=lambda item: item[2].verdict != "approve")
    )
    for lane, round_, result in ordered:
        decision = (
            gate.defer_round_for_resolution(run, lane, round_, result)
            if defer_resolution
            else gate.complete_round(run, lane, round_, result)
        )
        _record_accepted_result(
            worktree, vault, runtime_root, round_, result
        )
        if decision.action == "attention-required":
            break


def _resume_bound_attention(
    gate: ReviewGateController,
    store: OperationStore,
    runtime_root: Path,
    state: Mapping[str, Any],
) -> tuple[Mapping[str, Any], str]:
    status = str(state.get("status") or "")
    stored_lanes = state.get("lanes")
    if not (
        status == "attention-required"
        and isinstance(stored_lanes, list)
        and stored_lanes
    ):
        return state, status
    owner_id = str(state.get("owner_id") or "")
    awaiting_resolution = state.get("awaiting_resolution")
    allowed_states = {"awaiting-callback", "verifying"}
    if isinstance(awaiting_resolution, dict) and awaiting_resolution:
        allowed_states.add("running")
    recoverable = bool(owner_id)
    for lane in stored_lanes:
        if not isinstance(lane, dict):
            recoverable = False
            break
        axis = str(lane.get("axis") or "")
        operation_id = str(lane.get("operation_id") or "")
        callback = _callback_path(runtime_root, axis)
        if (
            not axis
            or not operation_id
            or not callback.is_file()
            or callback.is_symlink()
        ):
            recoverable = False
            break
        try:
            record = store.read(owner_id, operation_id)
        except StoreError:
            recoverable = False
            break
        if record.state not in allowed_states:
            recoverable = False
            break
    if recoverable:
        gate.resume_bound_attention()
        state = gate.read()
        status = str(state.get("status") or "")
    return state, status


def _terminal_receipt(
    *,
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    context: ReviewContext,
    context_manifest: Path,
    gate: ReviewGateController,
    state: Mapping[str, Any],
    status: str,
) -> dict[str, Any] | None:
    if status not in {
        "approved",
        "skipped",
        "stopped",
        "attention-required",
    }:
        return None
    bound = state.get("context")
    if (
        status in {"approved", "skipped", "stopped"}
        and (
            not isinstance(bound, dict)
            or bound.get("head_sha") != context.head_sha
        )
    ):
        raise TaskReviewError(
            "terminal review evidence is stale for the product HEAD"
        )
    stored_lanes = state.get("lanes")
    receipt_run = (
        None
        if status == "skipped"
        or (status == "attention-required" and stored_lanes == [])
        else gate.rehydrate()
    )
    return _receipt(
        status=status,
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        context_manifest=context_manifest,
        run=receipt_run,
    )


def _run_exact_head_review(
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    task_id: str,
    runtime_root: Path,
    *,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    """Drive the iteration-zero attempt path through its frozen gate only."""

    store_root = vault / ".vault-meta" / "harness"
    store = OperationStore(store_root)
    runtime = runtime_manager or RuntimeSessionManager.for_root(
        vault, store_root=store_root
    )
    gate_root = _gate_root(vault, task_id)
    gate = ReviewGateController(gate_root, runtime, store)
    gate_exists = gate.state_path.exists()
    if gate_exists:
        state = gate.read()
        if not isinstance(state.get("attempt"), Mapping):
            return _legacy_resume_disabled_receipt(
                meta=meta,
                vault=vault,
                worktree=worktree,
                runtime_root=runtime_root,
            )
    context, context_manifest = _context(
        meta, vault, worktree, runtime_root, task_id
    )
    preset, request = _request(meta, vault, task_id, context)
    if not gate_exists:
        return _start_exact_head_review(
            meta=meta,
            vault=vault,
            worktree=worktree,
            task_id=task_id,
            runtime_root=runtime_root,
            context=context,
            context_manifest=context_manifest,
            preset=preset,
            request=request,
            gate=gate,
        )

    state = gate.read()
    attempt = ReviewAttempt.from_mapping(state["attempt"])
    if request is None:
        raise TaskReviewError("enabled review has no request")
    lineage, cycle, plan_sha256, outcome_sha256 = _attempt_binding(
        meta, task_id
    )
    candidate = compile_review_attempt_identity(
        request=request,
        finalization_lineage_id=lineage,
        cycle=cycle,
        plan_sha256=plan_sha256,
        outcome_sha256=outcome_sha256,
    )
    attempt.assert_identity(candidate)
    if context.head_sha != attempt.identity.exact_head_sha:
        raise ReviewAttemptError("review attempt cannot bind a changed HEAD")
    status = str(state.get("status") or "")
    if attempt.status == "terminal":
        if status != attempt.terminal.result.value:
            raise ReviewAttemptError("review attempt terminal projection changed")
        return _receipt(
            status=status,
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=None,
        )
    if attempt.status != "awaiting-callback" or status != "reviewing":
        raise ReviewAttemptError("review attempt is not at a callback boundary")
    run = gate.rehydrate_attempt()
    ready = _collect_ready_results(run, runtime_root, worktree, vault)
    if _requires_lane_barrier(preset) and len(ready) != len(
        run.execution.lanes
    ):
        return _receipt(
            status="reviewing",
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=run,
        )
    for lane, round_, result in ready:
        decision = gate.complete_attempt_round(run, lane, round_, result)
        if decision.action != "awaiting-axes":
            break
    next_state = gate.read()
    next_status = str(next_state.get("status") or "")
    next_attempt = ReviewAttempt.from_mapping(next_state["attempt"])
    return _receipt(
        status=next_status,
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        context_manifest=context_manifest,
        run=(
            gate.rehydrate_attempt()
            if next_attempt.status != "terminal"
            else None
        ),
    )


def _run_review(
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    task_id: str,
    runtime_root: Path,
    *,
    runtime_manager: object | None = None,
    apply_finalizing_recovery: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    if _exact_head_attempt_enabled(meta):
        return _run_exact_head_review(
            meta,
            vault,
            worktree,
            task_id,
            runtime_root,
            runtime_manager=runtime_manager,
        )
    store_root = vault / ".vault-meta" / "harness"
    store = OperationStore(store_root)
    runtime = runtime_manager or RuntimeSessionManager.for_root(
        vault, store_root=store_root
    )
    gate_root = _gate_root(vault, task_id)
    gate = ReviewGateController(gate_root, runtime, store)
    gate.reconcile_superseded_review_cleanup()
    gate_exists = gate.state_path.exists()
    initial_state = gate.read() if gate_exists else {}
    # A code-owned runtime resume can clear the lane attention before this
    # facade is replayed. Restore the gate phase before building the context:
    # an awaiting-resolution boundary needs its resolution bundle to rebind
    # the original boundary input to the resolved exact HEAD.
    if gate_exists:
        initial_state, _initial_status = _resume_bound_attention(
            gate, store, runtime_root, initial_state
        )
    resolution_bundle = _preload_resolution_bundle(
        worktree=worktree,
        gate_root=gate_root,
        task_id=task_id,
        state=initial_state,
    )
    context, context_manifest = _context(
        meta,
        vault,
        worktree,
        runtime_root,
        task_id,
        resolution_bundle=resolution_bundle,
    )
    preset, request = _request(meta, vault, task_id, context)
    pending_replay = _pending_gate_replay(gate, store, task_id)
    if not gate_exists or pending_replay:
        return _start_review(
            meta=meta,
            vault=vault,
            worktree=worktree,
            task_id=task_id,
            runtime_root=runtime_root,
            context=context,
            context_manifest=context_manifest,
            preset=preset,
            request=request,
            gate_root=gate_root,
            gate=gate,
            store=store,
            runtime=runtime,
            pending_replay=pending_replay,
        )

    state, status = _resume_bound_attention(
        gate, store, runtime_root, gate.read()
    )
    terminal = _terminal_receipt(
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        context=context,
        context_manifest=context_manifest,
        gate=gate,
        state=state,
        status=status,
    )
    if terminal is not None:
        return terminal

    run = gate.rehydrate()
    if status == "awaiting-resolution":
        if _requires_lane_barrier(preset):
            ready = [
                item
                for item in _collect_ready_results(
                    run, runtime_root, worktree, vault
                )
                if not _ready_result_is_recorded(gate, state, item[2])
            ]
            if ready:
                _complete_ready_results(
                    gate=gate,
                    run=run,
                    ready=ready,
                    preset=preset,
                    context=context,
                    worktree=worktree,
                    vault=vault,
                    runtime_root=runtime_root,
                    already_awaiting=True,
                )
                state = gate.read()
        return _continue_resolution(
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            task_id=task_id,
            gate_root=gate_root,
            gate=gate,
            state=state,
            run=run,
            context=context,
            context_manifest=context_manifest,
            preset=preset,
        )
    if status not in {
        "reviewing",
        "verifying",
        "recovery-verification-required",
        "fresh-boundary-authorized",
    }:
        raise TaskReviewError("review gate has an unsupported state")
    if context.head_sha != run.execution.request.context.head_sha:
        recovery = _finalizing_resubmit_recovery(
            meta,
            vault,
            worktree,
            runtime_root,
            task_id,
            store,
            gate,
            run,
            context,
        )
        if recovery is None:
            raise TaskReviewError(
                "product HEAD changed outside an awaiting-resolution boundary"
            )
        return apply_finalizing_recovery(
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            task_id=task_id,
            gate=gate,
            run=run,
            recovery=recovery,
        )

    ready = _collect_ready_results(run, runtime_root, worktree, vault)
    if _requires_lane_barrier(preset) and len(ready) != len(run.execution.lanes):
        return _receipt(
            status=status,
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=run,
        )
    _complete_ready_results(
        gate=gate,
        run=run,
        ready=ready,
        preset=preset,
        context=context,
        worktree=worktree,
        vault=vault,
        runtime_root=runtime_root,
    )
    next_status = str(gate.read().get("status") or "")
    return _receipt(
        status=next_status,
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        context_manifest=context_manifest,
        run=None if next_status == "skipped" else gate.rehydrate(),
    )
