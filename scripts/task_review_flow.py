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
    EXACT_HEAD_REVIEW_PROTOCOL,
    LEGACY_CROSS_HEAD_RESUME_DISABLED,
    ReviewAttempt,
    ReviewAttemptError,
    ReviewAttemptTerminalResult,
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
from task_review_finalization_attempt import (
    FinalizationAttemptError,
    attempt_binding,
    exact_head_attempt_enabled,
    finalization_ledger,
    reserve_exact_head_attempt,
)
from task_review_resolution_bundle import _resolution_bundle
from task_review_shared import (
    ActiveReviewRound,
    StaleRoundCallbackError,
    TaskReviewError,
    _atomic_json,
    _git,
    _read_json,
)
from task_review_verification import _finalizing_resubmit_recovery
from task_review_replay import _pending_replay_is_safe
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


def _resolution_source_state(
    gate_root: Path,
    state: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Recover the exact prior finding boundary across zero-lane restarts."""

    attempt = ReviewAttempt.from_mapping(state["attempt"])
    if attempt.status == "terminal":
        if attempt.terminal is None:
            return None
        if attempt.terminal.result == ReviewAttemptTerminalResult.CHANGES_REQUESTED:
            return state
        if (
            attempt.terminal.result
            != ReviewAttemptTerminalResult.ATTENTION_REQUIRED
            or attempt.terminal.lane_results
        ):
            return None
    elif attempt.identity.cycle <= 1:
        return None
    identity = attempt.identity
    for cycle in range(identity.cycle - 1, 0, -1):
        pointer = gate_root / "attempts" / f"cycle-{cycle}.json"
        if not pointer.is_file() or pointer.is_symlink():
            return None
        candidate = _read_json(pointer, "review attempt archive")
        archived = ReviewAttempt.from_mapping(candidate.get("attempt"))
        archived_identity = archived.identity
        if (
            archived_identity.cycle != cycle
            or archived_identity.finalization_lineage_id
            != identity.finalization_lineage_id
            or archived_identity.plan_sha256 != identity.plan_sha256
            or archived_identity.outcome_sha256 != identity.outcome_sha256
            or archived_identity.policy != identity.policy
        ):
            raise ReviewAttemptError("review attempt archive identity drifted")
        if archived.status != "terminal" or archived.terminal is None:
            raise ReviewAttemptError("review attempt archive is not terminal")
        if (
            archived.terminal.result
            == ReviewAttemptTerminalResult.CHANGES_REQUESTED
        ):
            if archived_identity.exact_head_sha == identity.exact_head_sha:
                raise ReviewAttemptError(
                    "review resolution source did not move the exact HEAD"
                )
            return candidate
        if (
            archived.terminal.result
            != ReviewAttemptTerminalResult.ATTENTION_REQUIRED
            or archived.terminal.lane_results
        ):
            return None
    return None


def _archive_resolution_callbacks(
    runtime_root: Path,
    state: Mapping[str, Any],
) -> None:
    """Preserve accepted callback bytes and free only their exact outboxes."""

    boundaries = state.get("review_notification_evidence")
    if not isinstance(boundaries, Mapping) or not boundaries:
        raise ReviewAttemptError("review resolution callbacks are unavailable")
    for axis, raw_boundary in sorted(boundaries.items()):
        if not isinstance(axis, str) or not isinstance(raw_boundary, Mapping):
            raise ReviewAttemptError("review resolution callback is invalid")
        callback_id = str(raw_boundary.get("callback_id") or "")
        callback_sha256 = str(raw_boundary.get("callback_sha256") or "")
        round_operation_id = str(
            raw_boundary.get("round_operation_id") or ""
        )
        round_run_id = str(raw_boundary.get("round_run_id") or "")
        if (
            not callback_id
            or len(callback_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in callback_sha256)
            or not round_operation_id
            or not round_run_id
        ):
            raise ReviewAttemptError("review resolution callback identity is invalid")
        callback = _callback_path(runtime_root, axis)
        archive_dir = callback.parent / "accepted"
        archive = archive_dir / f"{callback_sha256}.review-callback.json"
        if (
            archive.is_symlink()
            or callback.is_symlink()
            or archive_dir.is_symlink()
            or (archive_dir.exists() and not archive_dir.is_dir())
        ):
            raise ReviewAttemptError("review resolution callback path is invalid")
        def matches_boundary(raw: bytes) -> bool:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return False
            return (
                isinstance(payload, dict)
                and payload.get("callback_id") == callback_id
                and payload.get("payload_sha256") == callback_sha256
                and payload.get("operation_id") == round_operation_id
                and payload.get("run_id") == round_run_id
                and payload.get("kind") == "review"
                and isinstance(payload.get("payload"), dict)
                and payload["payload"].get("axis") == axis
            )

        archived_raw = archive.read_bytes() if archive.is_file() else None
        if archived_raw is not None and not matches_boundary(archived_raw):
            raise ReviewAttemptError("review resolution callback archive changed")
        if callback.is_file():
            raw = callback.read_bytes()
            if not matches_boundary(raw):
                if archived_raw is not None:
                    continue
                raise ReviewAttemptError(
                    "review resolution callback identity drifted"
                )
            archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            archive_dir.chmod(0o700)
            if archive.exists():
                if archived_raw != raw:
                    raise ReviewAttemptError(
                        "review resolution callback archive changed"
                    )
                callback.unlink()
            else:
                callback.replace(archive)
        elif not archive.is_file():
            raise ReviewAttemptError(
                "review resolution callback bytes are unavailable"
            )


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


def _exact_head_attempt_enabled(meta: Mapping[str, Any]) -> bool:
    """Select the protocol only from the normalized additive v4 policy.

    ``review_policy`` deliberately remains the exact public v4 shape.  The
    already-versioned ``finalization_policy`` is the code-owned capability for
    exact-HEAD attempts; caller-injected selector fields have no authority.
    """

    try:
        return exact_head_attempt_enabled(meta)
    except FinalizationAttemptError as exc:
        raise TaskReviewError(str(exc)) from exc


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
    meta: Mapping[str, Any], task_id: str, *, cycle: int
) -> tuple[str, int, str, str]:
    try:
        return attempt_binding(meta, task_id, cycle=cycle)
    except FinalizationAttemptError as exc:
        raise TaskReviewError(str(exc)) from exc


def _review_origin_surface(
    meta: Mapping[str, Any], runtime: object, *, allow_fallback: bool
) -> str:
    """Use the live coordinator surface only after an effect-free task loss."""

    task_surface = str(meta.get("task_surface") or "")
    wiki_surface = str(meta.get("wiki_surface") or "")
    status = getattr(getattr(runtime, "cmux", None), "status", None)
    if not allow_fallback or not callable(status) or not wiki_surface:
        return task_surface
    try:
        return (
            wiki_surface
            if status(task_surface) != "alive" and status(wiki_surface) == "alive"
            else task_surface
        )
    except Exception:
        return task_surface


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
    cycle: int,
    origin_surface: str,
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
        meta, task_id, cycle=cycle
    )
    run = gate.begin_attempt(
        dispatch_operation_id=task_id,
        finalization_lineage_id=lineage,
        cycle=cycle,
        plan_sha256=plan_sha256,
        outcome_sha256=outcome_sha256,
        request=request,
        origin_surface=origin_surface,
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
    already_awaiting: bool = False, exact_attempt: bool = False,
) -> tuple[str, ...]:
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
        if exact_attempt or defer_resolution or context.purpose != "release"
        else sorted(ready, key=lambda item: item[2].verdict != "approve")
    )
    actions: list[str] = []
    for lane, round_, result in ordered:
        if exact_attempt and _ready_result_is_recorded(gate, gate.read(), result):
            continue
        decision = (
            gate.complete_attempt_round(run, lane, round_, result)
            if exact_attempt
            else gate.defer_round_for_resolution(run, lane, round_, result)
            if defer_resolution
            else gate.complete_round(run, lane, round_, result)
        )
        _record_accepted_result(worktree, vault, runtime_root, round_, result)
        actions.append(decision.action)
        if (decision.action != "awaiting-axes" if exact_attempt
                else decision.action == "attention-required"):
            break
    return tuple(actions)


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
    resolution_bundle = None
    current_head = _git(worktree, "rev-parse", "HEAD")
    if gate_exists:
        source_state = _resolution_source_state(gate_root, gate.read())
        if source_state is not None:
            source_attempt = ReviewAttempt.from_mapping(
                source_state["attempt"]
            )
            if current_head != source_attempt.identity.exact_head_sha:
                boundaries = source_state.get("review_notification_evidence")
                if not isinstance(boundaries, Mapping):
                    raise ReviewAttemptError(
                        "review resolution boundary is unavailable"
                    )
                resolution_bundle = _resolution_bundle(
                    worktree,
                    gate_root,
                    task_id,
                    boundaries,
                    current_head,
                )
                _archive_resolution_callbacks(runtime_root, source_state)
    context, context_manifest = _context(
        meta,
        vault,
        worktree,
        runtime_root,
        task_id,
        resolution_bundle=resolution_bundle,
    )
    preset, request = _request(meta, vault, task_id, context)
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

    cycle = 1
    zero_lane_preflight = False
    if gate_exists:
        prior_state = gate.read()
        prior_attempt = ReviewAttempt.from_mapping(prior_state["attempt"])
        cycle = prior_attempt.identity.cycle
        if prior_attempt.status == "terminal":
            assert prior_attempt.terminal is not None
            ledger = finalization_ledger(meta, vault, task_id)
            ledger.record_terminal(
                attempt_id=prior_attempt.identity.attempt_id,
                terminal_result=prior_attempt.terminal.result.value,
            )
            zero_lane_preflight = (
                prior_attempt.terminal.result
                == ReviewAttemptTerminalResult.ATTENTION_REQUIRED
                and not prior_attempt.terminal.lane_results
                and prior_state.get("status") == "attention-required"
                and prior_state.get("lanes") == []
                and prior_state.get("round_results") in ({}, None)
                and prior_state.get("final_results") in ({}, None)
                and prior_state.get("evidence") in ({}, None)
            )
            if (
                context.head_sha == prior_attempt.identity.exact_head_sha
                and not zero_lane_preflight
            ):
                return _receipt(
                    status=prior_attempt.terminal.result.value,
                    meta=meta,
                    vault=vault,
                    worktree=worktree,
                    runtime_root=runtime_root,
                    context_manifest=context_manifest,
                    run=None,
                )
            if zero_lane_preflight and any(
                path.exists() or path.is_symlink()
                for path in (_callback_path(runtime_root, axis)
                             for axis in request.policy.axes)
            ):
                raise ReviewAttemptError(
                    "zero-lane review preflight found a callback artifact"
                )
            cycle += 1

    request, ledger = reserve_exact_head_attempt(
        meta,
        vault=vault,
        worktree=worktree,
        task_id=task_id,
        request=request,
        cycle=cycle,
    )
    if not gate_exists or ReviewAttempt.from_mapping(
        gate.read()["attempt"]
    ).status == "terminal":
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
            cycle=cycle,
            origin_surface=_review_origin_surface(
                meta, runtime, allow_fallback=zero_lane_preflight
            ),
        )

    state = gate.read()
    attempt = ReviewAttempt.from_mapping(state["attempt"])
    lineage, cycle, plan_sha256, outcome_sha256 = _attempt_binding(
        meta, task_id, cycle=attempt.identity.cycle
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
    _complete_ready_results(
        gate=gate,
        run=run,
        ready=ready,
        preset=preset,
        context=context,
        worktree=worktree,
        vault=vault,
        runtime_root=runtime_root,
        exact_attempt=True,
    )
    next_state = gate.read()
    next_status = str(next_state.get("status") or "")
    next_attempt = ReviewAttempt.from_mapping(next_state["attempt"])
    if next_attempt.status == "terminal":
        assert next_attempt.terminal is not None
        ledger.record_terminal(
            attempt_id=next_attempt.identity.attempt_id,
            terminal_result=next_attempt.terminal.result.value,
        )
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
    gate_path = _gate_root(vault, task_id) / "review-gate.json"
    exact_enabled = _exact_head_attempt_enabled(meta)
    if exact_enabled and gate_path.exists():
        try:
            existing_gate = json.loads(gate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskReviewError("review gate state is unavailable") from exc
        # A gate created before the v4 exact-attempt activation completes on
        # its original path. It cannot be converted or replayed in place.
        exact_enabled = isinstance(existing_gate, dict) and isinstance(
            existing_gate.get("attempt"), Mapping
        )
    if exact_enabled:
        return _run_exact_head_review(
            meta,
            vault,
            worktree,
            task_id,
            runtime_root,
            runtime_manager=runtime_manager,
        )
    return _legacy_resume_disabled_receipt(
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
    )
