"""Automatic review gate driving and replay-safe telemetry."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from harness.contracts import AttentionReason
from harness.dashboard_facade import DashboardBinding
from harness.finalization_ledger import predecessor_bound_attempt_id
from harness.review_attempt import (
    EXACT_HEAD_REVIEW_PROTOCOL,
    LEGACY_CROSS_HEAD_RESUME_DISABLED,
    ReviewAttempt,
    ReviewAttemptError,
    ReviewAttemptIdentity,
)
from harness.pre_model_reviewer_retirement import (
    review_attempt_records_are_quiescent,
)
from harness.review_finalization import StructuralPivotPending
from harness.review_cleanup_recovery import accepted_callback_cleanup_is_complete, recover_interrupted_review_attempt
from harness.runtime_worker_verification import (
    _verification_candidate_is_current,
)
from harness.runtime_sessions import RuntimeSessionManager
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
from review_telemetry import emit_review_event
from review_zero_effect import zero_effect_terminal_attempt
from task_review_context import (
    _assert_frozen_topology,
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
from task_review_resolution_bundle import (
    _approved_summary_predecessor_state,
    _archive_prior_terminal_callbacks,
    _archive_resolution_callbacks,
    _resolution_bundle,
    _resolution_source_state,
)
from task_review_shared import (
    ActiveReviewRound,
    StaleRoundCallbackError,
    TaskReviewError,
    _atomic_json,
    _git,
    _read_json,
)
from task_review_verification import _finalizing_resubmit_recovery
from task_review_replay import _pending_gate_replay, _pending_replay_is_safe
from task_review_transport import (
    _callback_wake,
    _collect_ready_results,
    _emit_round_telemetry,
    _receipt,
    _record_accepted_result,
    _write_round_meta,
    load_active_round,
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
            dashboard_binding=DashboardBinding(
                vault=vault,
                store=vault / ".vault-meta" / "harness",
                caller_surface=str(meta.get("task_surface") or ""),
                request_id=task_id,
            ),
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

def _admitted_review_launch(
    runtime_root: Path,
    worktree: Path,
    task_id: str,
    context: ReviewContext,
) -> None:
    """Verify the pipeline's launch admission against the actual context.

    The pipeline drive publishes the exact verification receipt/HEAD pair it
    admitted; the exact-HEAD flow reserves and launches only while the actual
    review context binds exactly the admitted HEAD and that HEAD is still the
    clean current candidate.  Any identity, HEAD, or clean-state mismatch
    fails closed before reservation or provider effect, and a malformed or
    symlinked admission is never treated as absent.  Absence keeps launches
    without a pipeline verification owner on their existing gates.
    """

    admission_path = runtime_root / "review-launch-admission.json"
    if admission_path.is_symlink():
        raise TaskReviewError("review launch admission is invalid")
    if not admission_path.is_file():
        return
    admission = _read_json(admission_path, "review launch admission")
    expected_keys = {
        "schema_version",
        "operation_id",
        "verification_operation_id",
        "verification_lane_id",
        "verification_run_id",
        "receipt_sha256",
        "head_sha",
        "status",
    }
    if (
        not isinstance(admission, dict)
        or set(admission) != expected_keys
        or admission.get("schema_version") != 1
        or admission.get("status") != "admitted"
        or admission.get("operation_id") != task_id
        or not all(
            isinstance(admission[key], str) and admission[key]
            for key in (
                "verification_operation_id",
                "verification_lane_id",
                "verification_run_id",
            )
        )
        or not re.fullmatch(
            "[0-9a-f]{64}", str(admission.get("receipt_sha256") or "")
        )
        or not re.fullmatch(
            "[0-9a-f]{40,64}", str(admission.get("head_sha") or "")
        )
    ):
        raise TaskReviewError("review launch admission is invalid")
    if admission["head_sha"] != context.head_sha:
        raise TaskReviewError("review launch admission targets another HEAD")
    if not _verification_candidate_is_current(
        worktree, str(admission["head_sha"])
    ):
        raise TaskReviewError(
            "review launch admission is stale for the current candidate"
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
    meta: Mapping[str, Any], task_id: str, worktree: Path, *, cycle: int
) -> tuple[str, int, str, str]:
    try:
        return attempt_binding(meta, task_id, worktree, cycle=cycle)
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


def _assert_active_attempt_authority(
    attempt: ReviewAttempt,
    candidate: ReviewAttemptIdentity,
    *,
    plan_sha256: str,
    outcome_sha256: str,
) -> None:
    amended = (
        attempt.identity.plan_sha256 != plan_sha256
        or attempt.identity.outcome_sha256 != outcome_sha256
    )
    if not amended:
        attempt.assert_identity(candidate)
        return
    stable_candidate = (
        candidate.attempt_id,
        candidate.finalization_lineage_id,
        candidate.cycle,
        candidate.exact_head_sha,
        candidate.policy,
        tuple(
            (
                lane.axis,
                lane.owner_id,
                lane.runtime,
                lane.model,
                lane.effort,
                lane.profile,
                lane.routing_sha256,
            )
            for lane in candidate.lanes
        ),
    )
    stable_attempt = (
        attempt.identity.attempt_id,
        attempt.identity.finalization_lineage_id,
        attempt.identity.cycle,
        attempt.identity.exact_head_sha,
        attempt.identity.policy,
        tuple(
            (
                lane.axis,
                lane.owner_id,
                lane.runtime,
                lane.model,
                lane.effort,
                lane.profile,
                lane.routing_sha256,
            )
            for lane in attempt.identity.lanes
        ),
    )
    if stable_candidate != stable_attempt:
        raise ReviewAttemptError("review attempt authority changed")


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
    approved_plan_amendment: bool = False,
    approved_summary_refresh: bool = False,
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
        meta, task_id, worktree, cycle=cycle
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
        dashboard_binding=DashboardBinding(
            vault=vault,
            store=vault / ".vault-meta" / "harness",
            caller_surface=origin_surface,
            request_id=task_id,
        ),
        callback_wake=_callback_wake(meta, vault, worktree),
        approved_plan_amendment=approved_plan_amendment,
        approved_summary_refresh=approved_summary_refresh,
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


def _complete_ready_results(
    *,
    gate: ReviewGateController,
    run: ReviewGateRun,
    ready: list[tuple[object, ReviewRound, ReviewResult]],
    worktree: Path,
    vault: Path,
    runtime_root: Path,
) -> tuple[str, ...]:
    """Ingest each ready exact-HEAD callback exactly once, in arrival order.

    The exact-HEAD attempt is the only production completion path, so there is
    one branch here rather than a mode flag.  Results already recorded in the
    gate are skipped, but their telemetry is still completed: the durable gate
    transition happens before the events are emitted, so a crash in between
    would otherwise drop them permanently.  ``_record_accepted_result`` is
    idempotent — ``_emit_round_telemetry`` keeps a durable marker per event key
    and returns early once an event has been emitted — so re-running it after a
    resumed attempt closes that window without duplicating events.
    """

    actions: list[str] = []
    for lane, round_, result in ready:
        if _ready_result_is_recorded(gate, gate.read(), result):
            _record_accepted_result(worktree, vault, runtime_root, round_, result)
            continue
        decision = gate.complete_attempt_round(run, lane, round_, result)
        _record_accepted_result(worktree, vault, runtime_root, round_, result)
        actions.append(decision.action)
        if decision.action != "awaiting-axes":
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


def _reviewing_receipt(
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    context_manifest: Path,
) -> dict[str, Any]:
    return _receipt(
        status="reviewing",
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        context_manifest=context_manifest,
    )


def _reserve_or_reviewing(
    reserve: Callable[[], Any], reviewing_receipt: Callable[[], dict[str, Any]]
) -> Any:
    """Translate a pending structural pivot into the normal reviewing state."""

    try:
        return reserve()
    except StructuralPivotPending:
        return reviewing_receipt()


def _summary_only_manifest_change(
    runtime_root: Path,
    previous_context: Mapping[str, object],
    context: ReviewContext,
    context_manifest: Path,
) -> bool:
    """Prove that two same-HEAD packets differ only by summary bytes."""

    previous_pointer = str(previous_context.get("manifest") or "")
    if not previous_pointer or Path(previous_pointer).is_absolute():
        return False
    root = runtime_root.expanduser().resolve()
    previous_path = (root / previous_pointer).resolve()
    current_path = context_manifest.expanduser().resolve()
    try:
        previous_path.relative_to(root)
        current_path.relative_to(root)
    except ValueError:
        return False
    if (
        not previous_path.is_file()
        or previous_path.is_symlink()
        or not current_path.is_file()
        or current_path.is_symlink()
    ):
        return False
    try:
        previous = _read_json(previous_path, "previous review context")
        current = _read_json(current_path, "current review context")
    except TaskReviewError:
        return False
    if (
        previous.get("schema_version") != 1
        or current.get("schema_version") != 1
        or previous.get("operation_id") != current.get("operation_id")
        or previous.get("metadata") != current.get("metadata")
        or not isinstance(previous.get("inputs"), list)
        or not isinstance(current.get("inputs"), list)
    ):
        return False
    previous_inputs = {
        str(item.get("name") or ""): item
        for item in previous["inputs"]
        if isinstance(item, Mapping) and item.get("name")
    }
    current_inputs = {
        str(item.get("name") or ""): item
        for item in current["inputs"]
        if isinstance(item, Mapping) and item.get("name")
    }
    if (
        len(previous_inputs) != len(previous["inputs"])
        or len(current_inputs) != len(current["inputs"])
        or set(previous_inputs) != set(current_inputs)
        or "implementer-summary.json" not in previous_inputs
    ):
        return False
    summary_name = "implementer-summary.json"
    if any(
        previous_inputs[name] != current_inputs[name]
        for name in previous_inputs
        if name != summary_name
    ):
        return False
    previous_summary = previous_inputs[summary_name]
    current_summary = current_inputs[summary_name]
    changing_summary_fields = {"sha256", "bytes"}
    return (
        previous_summary.get("sha256")
        == previous_context.get("implementer_summary_sha256")
        and current_summary.get("sha256")
        == context.implementer_summary_sha256
        and previous_summary.get("sha256")
        != current_summary.get("sha256")
        and {
            key: value
            for key, value in previous_summary.items()
            if key not in changing_summary_fields
        }
        == {
            key: value
            for key, value in current_summary.items()
            if key not in changing_summary_fields
        }
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
        current_state = gate.read()
        current_attempt = ReviewAttempt.from_mapping(
            current_state["attempt"]
        )
        current_context = current_state.get("context")
        approved_summary_drift = False
        active_summary_follow_up = (
            current_attempt.identity.exact_head_sha == current_head
            and _approved_summary_predecessor_state(
                gate_root, current_state
            )
            is not None
        )
        if (
            current_attempt.status == "terminal"
            and current_attempt.terminal is not None
            and current_attempt.terminal.result.value == "approved"
            and current_attempt.identity.exact_head_sha == current_head
            and isinstance(current_context, Mapping)
        ):
            summary_path = worktree / ".task-summary.json"
            prior_summary_sha256 = str(
                current_context.get("implementer_summary_sha256") or ""
            )
            if summary_path.is_file() and not summary_path.is_symlink():
                current_summary_sha256 = hashlib.sha256(
                    summary_path.read_bytes()
                ).hexdigest()
                approved_summary_drift = (
                    bool(prior_summary_sha256)
                    and prior_summary_sha256 != current_summary_sha256
                )
        if (
            not active_summary_follow_up
            and current_attempt.status != "terminal"
            and current_attempt.identity.cycle > 1
        ):
            previous_pointer = (
                gate_root
                / "attempts"
                / f"cycle-{current_attempt.identity.cycle - 1}.json"
            )
            if previous_pointer.is_file() and not previous_pointer.is_symlink():
                previous_state = _read_json(
                    previous_pointer, "previous review attempt"
                )
                previous_attempt = ReviewAttempt.from_mapping(
                    previous_state.get("attempt")
                )
                previous_context = previous_state.get("context")
                active_summary_follow_up = (
                    previous_attempt.status == "terminal"
                    and previous_attempt.terminal is not None
                    and previous_attempt.terminal.result.value == "approved"
                    and previous_attempt.identity.finalization_lineage_id
                    == current_attempt.identity.finalization_lineage_id
                    and previous_attempt.identity.exact_head_sha
                    == current_attempt.identity.exact_head_sha
                    and previous_attempt.identity.plan_sha256
                    == current_attempt.identity.plan_sha256
                    and previous_attempt.identity.outcome_sha256
                    == current_attempt.identity.outcome_sha256
                    and current_attempt.identity.attempt_id
                    == predecessor_bound_attempt_id(
                        lineage_id=(
                            current_attempt.identity.finalization_lineage_id
                        ),
                        predecessor_attempt_id=(
                            previous_attempt.identity.attempt_id
                        ),
                        exact_head=current_attempt.identity.exact_head_sha,
                        cycle_number=current_attempt.identity.cycle,
                    )
                    and isinstance(previous_context, Mapping)
                    and isinstance(current_context, Mapping)
                    and previous_context.get("implementer_summary_sha256")
                    != current_context.get("implementer_summary_sha256")
                )
        source_state = _resolution_source_state(
            gate_root,
            current_state,
            include_approved_history=(
                approved_summary_drift or active_summary_follow_up
            ),
        )
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
    predecessor_attempt_id = ""
    reserved_attempt_id = ""
    supersedes_approved_attempt_id = ""
    approved_summary_predecessor_attempt_id = ""
    amended_boundary = False
    recovered_attention_attempt = gate_exists and accepted_callback_cleanup_is_complete(gate)
    if gate_exists:
        prior_state = gate.read()
        prior_attempt = ReviewAttempt.from_mapping(prior_state["attempt"])
        if (
            prior_attempt.status == "terminal"
            and recover_interrupted_review_attempt(gate)
        ):
            recovered_attention_attempt = True
            prior_state = gate.read()
            prior_attempt = ReviewAttempt.from_mapping(
                prior_state["attempt"]
            )
        cycle = prior_attempt.identity.cycle
        if prior_attempt.status == "terminal":
            assert prior_attempt.terminal is not None
            (
                _,
                _,
                active_plan_sha256,
                active_outcome_sha256,
            ) = _attempt_binding(
                meta, task_id, worktree, cycle=cycle
            )
            amended_boundary = (
                prior_attempt.identity.plan_sha256 != active_plan_sha256
                or prior_attempt.identity.outcome_sha256
                != active_outcome_sha256
            )
            summary_only_drift = False
            if (
                prior_attempt.terminal.result.value == "approved"
                and context.head_sha
                == prior_attempt.identity.exact_head_sha
                and not amended_boundary
            ):
                prior_context = prior_state.get("context")
                prior_summary_sha256 = (
                    str(
                        prior_context.get(
                            "implementer_summary_sha256"
                        )
                        or ""
                    )
                    if isinstance(prior_context, Mapping)
                    else ""
                )
                current_summary_sha256 = (
                    context.implementer_summary_sha256
                )
                if (
                    re.fullmatch(
                        r"[0-9a-f]{64}", prior_summary_sha256
                    )
                    is None
                    or re.fullmatch(
                        r"[0-9a-f]{64}", current_summary_sha256
                    )
                    is None
                ):
                    raise ReviewAttemptError(
                        "approved review summary identity is unavailable"
                    )
                summary_only_drift = (
                    prior_summary_sha256 != current_summary_sha256
                )
                if summary_only_drift and not _summary_only_manifest_change(
                    runtime_root,
                    prior_context,
                    context,
                    context_manifest,
                ):
                    raise ReviewAttemptError(
                        "approved review context drift is not summary-only"
                    )
            ledger = finalization_ledger(meta, vault, task_id, worktree)
            # The one narrow same-HEAD relaxation: an attempt that terminated
            # before the provider launched owns no durable effect, so it may be
            # superseded at an unchanged HEAD instead of replayed as a receipt.
            # The predicate lives in review_zero_effect so no weaker same-HEAD
            # admission path can be introduced beside it.
            zero_lane_preflight = zero_effect_terminal_attempt(
                prior_state,
                prior_attempt.terminal.result.value,
                prior_attempt.terminal.lane_results,
            )
            if (
                zero_lane_preflight
                and not review_attempt_records_are_quiescent(
                    store, prior_attempt
                )
            ):
                raise ReviewAttemptError(
                    "zero-lane review predecessor may own a provider effect"
                )
            if zero_lane_preflight:
                approved_summary_predecessor = (
                    _approved_summary_predecessor_state(
                        gate_root, prior_state
                    )
                )
                _archive_prior_terminal_callbacks(
                    runtime_root,
                    gate_root,
                    prior_state,
                    store,
                    approved_summary_predecessor_only=(
                        approved_summary_predecessor is not None
                    ),
                )
            terminal_decision = ledger.record_terminal(
                attempt_id=prior_attempt.identity.attempt_id,
                terminal_result=prior_attempt.terminal.result.value,
            )
            if (
                context.head_sha == prior_attempt.identity.exact_head_sha
                and not zero_lane_preflight
                and not amended_boundary
                and not summary_only_drift
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
            if summary_only_drift:
                _archive_prior_terminal_callbacks(
                    runtime_root,
                    gate_root,
                    prior_state,
                    store,
                    current_attempt_only=summary_only_drift,
                )
            if terminal_decision.cycle_number is None:
                raise ReviewAttemptError(
                    "terminal review attempt lacks a product cycle"
                )
            if prior_attempt.terminal.result.value in {
                "attention-required",
                "blocked",
            }:
                cycle = terminal_decision.cycle_number
                predecessor_attempt_id = prior_attempt.identity.attempt_id
            else:
                cycle = terminal_decision.cycle_number + 1
                if summary_only_drift:
                    approved_summary_predecessor_attempt_id = (
                        prior_attempt.identity.attempt_id
                    )
                elif (
                    amended_boundary
                    and prior_attempt.terminal.result.value == "approved"
                ):
                    supersedes_approved_attempt_id = (
                        prior_attempt.identity.attempt_id
                    )
        else:
            reserved_attempt_id = prior_attempt.identity.attempt_id
    _admitted_review_launch(runtime_root, worktree, task_id, context)
    reservation = _reserve_or_reviewing(
        lambda: reserve_exact_head_attempt(
            meta,
            vault=vault,
            worktree=worktree,
            task_id=task_id,
            request=request,
            cycle=cycle,
            predecessor_attempt_id=predecessor_attempt_id,
            reserved_attempt_id=reserved_attempt_id,
            supersedes_approved_attempt_id=(
                supersedes_approved_attempt_id
            ),
            approved_summary_predecessor_attempt_id=(
                approved_summary_predecessor_attempt_id
            ),
            recover_attention_attempt=recovered_attention_attempt,
        ),
        lambda: _reviewing_receipt(
            meta, vault, worktree, runtime_root, context_manifest
        ),
    )
    if isinstance(reservation, dict):
        return reservation
    request, ledger, cycle = reservation
    _assert_frozen_topology(meta, request)
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
            approved_plan_amendment=amended_boundary,
            approved_summary_refresh=bool(
                approved_summary_predecessor_attempt_id
            ),
        )
    state = gate.read()
    attempt = ReviewAttempt.from_mapping(state["attempt"])
    lineage, cycle, plan_sha256, outcome_sha256 = _attempt_binding(
        meta, task_id, worktree, cycle=attempt.identity.cycle
    )
    candidate = compile_review_attempt_identity(
        request=request,
        finalization_lineage_id=lineage,
        cycle=cycle,
        plan_sha256=plan_sha256,
        outcome_sha256=outcome_sha256,
    )
    _assert_active_attempt_authority(
        attempt,
        candidate,
        plan_sha256=plan_sha256,
        outcome_sha256=outcome_sha256,
    )
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
        worktree=worktree,
        vault=vault,
        runtime_root=runtime_root,
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
