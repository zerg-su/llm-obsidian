"""Coordinator-authorized recovery for dead or superseded review lanes."""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from harness.contracts import OwnedResources
from harness.runtime_sessions import RuntimeSessionManager
from harness.runtime_worker_review_bridge import (
    publish_review_resolution_transport,
)
from harness.post_verification_review_drive import (
    synchronize_post_verification_review_drive,
)
from harness.state_machine import TERMINAL
from harness.store import OperationStore
from harness.workflows.review_gate import (
    ReviewGateController,
    ReviewScopeBoundary,
    review_context_sha256,
)
from task_review_context import (
    _callback_path,
    _context,
    _envelope,
    _gate_root,
    _runtime_root,
    _validate_task,
)
from task_review_finalization_attempt import finalization_ledger
from task_review_finalizing import (
    _dispatched_review_is_quiescent,
    _launch_authorized_task_review,
)
from task_review_resolution_bundle import _recovery_resolution_bundle
from task_review_shared import (
    TaskReviewError,
    _read_json,
)
from task_review_boundary_authorization import (
    authorization_payload,
    persist_authorization,
)
from task_review_legacy_rounds import RecoveryRoundStore
from task_review_resolution_evidence import approved_summary_resolution
from task_review_transport import _receipt
from review_contract import review_axis_responsibility
from task_escalation_records import (
    EscalationRecordError,
    load_chain,
    load_latest,
)
from task_review_drift_contract import (
    DriftQuarantineAuthorization,
    SignalFreeRetirementAuthorization,
    SupportedCloseRetirementAuthorization,
    authorized_drift_quarantine,
    authorized_post_verification_review_drive,
    authorized_signal_free_retirement,
    authorized_supported_close_retirement,
)
from task_review_drift_quarantine import (
    mark_drift_quarantine_fresh,
    quarantine_drifted_attempt,
)


_ACCEPTED_CALLBACK_RECOVERY_PREFIX = (
    "Classified as an eligible repository-owned callback-ingestion "
    "mechanism failure."
)
_ACCEPTED_CALLBACK_ORDERING_PREFIX = (
    "Classified as the same eligible repository-owned callback-ingestion "
    "mechanism failure."
)
_ACCEPTED_CALLBACK_CHAIN_PREFIX = (
    "Classified as the same eligible repository-owned callback-ingestion "
    "authorization-chain mechanism failure."
)


def _literal_accepted_callback_head(
    attention: dict[str, Any], worktree: Path
) -> str:
    decision = str(attention.get("decision") or "")
    match = re.search(r"clean HEAD ([0-9a-f]{40,64})", decision)
    if (
        attention.get("status") != "resolved"
        or attention.get("category") != "mechanism-failure"
        or str(attention.get("worktree") or "") != str(worktree)
        or not decision.startswith(_ACCEPTED_CALLBACK_RECOVERY_PREFIX)
        or "Preserve and ingest the existing callback identities and findings"
        not in decision
        or "do not relaunch reviewers" not in decision
        or match is None
    ):
        return ""
    return match.group(1)


def _authorized_accepted_callback_head(
    attention: dict[str, Any], worktree: Path
) -> str:
    literal_head = _literal_accepted_callback_head(attention, worktree)
    if literal_head:
        return literal_head
    decision = str(attention.get("decision") or "")
    match = re.search(r"reviewed HEAD ([0-9a-f]{40,64})", decision)
    if (
        attention.get("status") != "resolved"
        or attention.get("category") != "mechanism-failure"
        or str(attention.get("worktree") or "") != str(worktree)
        or not decision.startswith(_ACCEPTED_CALLBACK_CHAIN_PREFIX)
        or "latest resolved same-failure escalation" not in decision
        or "exact previous chain reaches" not in decision
        or "two accepted callback identities are unchanged" not in decision
        or "every intervening record digest and previous pointer validates"
        not in decision
        or "clean descendant" not in decision
        or match is None
    ):
        return ""
    reviewed_head = match.group(1)
    try:
        chain = load_chain(worktree)
    except EscalationRecordError:
        return ""
    if not chain or chain[-1].payload != attention:
        return ""
    chain_paths = {record.path.resolve() for record in chain}
    records_root = worktree / ".task-escalation-records"
    try:
        stored_paths = {
            path.resolve()
            for path in records_root.iterdir()
            if path.name.endswith(".json")
        }
    except OSError:
        return ""
    if stored_paths != chain_paths:
        return ""
    anchors = [
        (index, record, _literal_accepted_callback_head(record.payload, worktree))
        for index, record in enumerate(chain[:-1])
        if _literal_accepted_callback_head(record.payload, worktree)
    ]
    if len(anchors) != 1:
        return ""
    anchor_index, _anchor, anchor_head = anchors[0]
    if anchor_head != reviewed_head:
        return ""
    scope = {
        key: attention.get(key)
        for key in ("category", "worktree", "task_name", "task_surface")
    }
    for record in chain[anchor_index:]:
        if any(record.payload.get(key) != value for key, value in scope.items()):
            return ""
        if record.record_type not in {"raise", "resolution"}:
            return ""
        if record.record_type == "resolution" and record is not chain[-1]:
            record_decision = str(record.payload.get("decision") or "")
            if record is not chain[anchor_index] and not record_decision.startswith(
                _ACCEPTED_CALLBACK_ORDERING_PREFIX
            ):
                return ""
    return reviewed_head


def _clean_descendant_head(worktree: Path, reviewed_head: str) -> str:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    descendant = subprocess.run(
        ["git", "merge-base", "--is-ancestor", reviewed_head, "HEAD"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    current = head.stdout.strip()
    if (
        head.returncode
        or re.fullmatch(r"[0-9a-f]{40,64}", current) is None
        or status.returncode
        or status.stdout
        or descendant.returncode
    ):
        raise TaskReviewError(
            "accepted callback recovery requires a clean descendant HEAD"
        )
    return current


def _recover_accepted_exact_callbacks(
    *,
    attention: dict[str, Any],
    meta: dict[str, Any],
    vault: Path,
    worktree: Path,
    task_id: str,
    runtime_manager: object | None,
) -> dict[str, Any]:
    """Ingest an exact accepted callback set without any reviewer relaunch."""

    reviewed_head = _authorized_accepted_callback_head(attention, worktree)
    if not reviewed_head:
        raise TaskReviewError(
            "accepted callback recovery lacks exact coordinator authorization"
        )
    _clean_descendant_head(worktree, reviewed_head)
    runtime_root = _runtime_root(vault, task_id)
    store_root = vault / ".vault-meta" / "harness"
    store = OperationStore(store_root)
    runtime = runtime_manager or RuntimeSessionManager.for_root(
        vault, store_root=store_root
    )
    gate = ReviewGateController(_gate_root(vault, task_id), runtime, store)
    state = gate.read()
    attempt = state.get("attempt")
    identity = attempt.get("identity") if isinstance(attempt, dict) else None
    terminal = attempt.get("terminal") if isinstance(attempt, dict) else None
    if (
        state.get("execution_protocol") != "exact-head-attempt-v1"
        or not isinstance(attempt, dict)
        or not isinstance(identity, dict)
        or identity.get("exact_head_sha") != reviewed_head
    ):
        raise TaskReviewError(
            "accepted callback recovery gate identity is invalid"
        )
    if (
        attempt.get("status") == "terminal"
        and isinstance(terminal, dict)
        and terminal.get("result") == "changes-requested"
        and state.get("status") == "changes-requested"
    ):
        completed_state = state
    else:
        if (
            attempt.get("status") != "awaiting-callback"
            or state.get("status") != "reviewing"
        ):
            raise TaskReviewError(
                "accepted callback recovery is not at its callback boundary"
            )
        run = gate.rehydrate_attempt()
        ready = []
        callback_identities: set[tuple[str, str]] = set()
        for lane in run.execution.lanes:
            round_ = run.rounds[lane.axis]
            callback = _callback_path(runtime_root, lane.axis)
            if not callback.is_file() or callback.is_symlink():
                raise TaskReviewError(
                    "accepted callback recovery artifact is unavailable"
                )
            envelope, result = _envelope(callback, round_)
            child = store.read(round_.owner_id, round_.operation_id)
            if (
                child.spec != round_.spec
                or child.lane_id != round_.lane_id
                or child.run_id != round_.run_id
                or child.state
                not in {"verifying", "finalizing", "exiting", "complete"}
                or child.pending_effect
                or child.accepted_callback_id != envelope.callback_id
                or child.accepted_callback_kind != envelope.kind
                or child.accepted_callback_sha256 != envelope.payload_sha256
            ):
                raise TaskReviewError(
                    "accepted callback recovery identity changed"
                )
            ready.append((lane, round_, result))
            callback_identities.add(
                (
                    child.accepted_callback_id,
                    child.accepted_callback_sha256,
                )
            )
        if (
            len(run.execution.lanes) != 2
            or len(ready) != 2
            or len(callback_identities) != 2
        ):
            raise TaskReviewError(
                "accepted callback recovery identity set is not the exact pair"
            )
        for lane, round_, result in ready:
            gate.complete_attempt_round(run, lane, round_, result)
        completed_state = gate.read()
        completed_attempt = completed_state.get("attempt")
        completed_terminal = (
            completed_attempt.get("terminal")
            if isinstance(completed_attempt, dict)
            else None
        )
        if (
            completed_state.get("status") != "changes-requested"
            or not isinstance(completed_attempt, dict)
            or completed_attempt.get("status") != "terminal"
            or not isinstance(completed_terminal, dict)
            or completed_terminal.get("result") != "changes-requested"
            or not isinstance(
                completed_state.get("awaiting_resolution"), dict
            )
        ):
            raise TaskReviewError(
                "accepted callback recovery did not reach typed resolution"
            )
        finalization_ledger(meta, vault, task_id).record_terminal(
            attempt_id=str(identity.get("attempt_id") or ""),
            terminal_result="changes-requested",
        )

    dispatch = store.read(task_id, task_id)
    surface_id = dispatch.resources.surface_id
    if not surface_id or surface_id != str(meta.get("task_surface") or ""):
        raise TaskReviewError(
            "accepted callback recovery task surface identity changed"
        )
    summary_path = worktree / ".task-summary.json"
    if not summary_path.is_file() or summary_path.is_symlink():
        raise TaskReviewError(
            "accepted callback recovery summary is unavailable"
        )
    cmux = getattr(runtime, "cmux", None)
    if cmux is None:
        raise TaskReviewError(
            "accepted callback recovery notification adapter is unavailable"
        )
    publish_review_resolution_transport(
        gate_state=completed_state,
        gate_root=gate.root,
        worktree=worktree,
        operation_id=task_id,
        surface_id=surface_id,
        summary_sha256=hashlib.sha256(summary_path.read_bytes()).hexdigest(),
        runtime_spec_path=(
            store_root
            / "owners"
            / task_id
            / "runtime"
            / task_id
            / "launch.json"
        ),
        cmux_adapter=cmux,
    )
    context = completed_state.get("context")
    manifest = str(context.get("manifest") or "") if isinstance(context, dict) else ""
    return _receipt(
        status="changes-requested",
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        context_manifest=runtime_root / manifest,
        run=None,
    )


def _recover_drift_quarantine(
    *,
    authorization: DriftQuarantineAuthorization,
    attention: dict[str, Any],
    attention_record_sha256: str,
    meta: dict[str, Any],
    vault: Path,
    worktree: Path,
    task_id: str,
    runtime_manager: object | None,
    fault_observer: Callable[[str], None] | None,
    signal_authorization: SignalFreeRetirementAuthorization | None = None,
    supported_close: SupportedCloseRetirementAuthorization | None = None,
    resume_completed_quarantine: bool = False,
) -> dict[str, Any]:
    runtime_root = _runtime_root(vault, task_id)
    store_root = vault / ".vault-meta" / "harness"
    store = OperationStore(store_root)
    runtime = runtime_manager or RuntimeSessionManager.for_root(
        vault, store_root=store_root
    )
    gate = ReviewGateController(_gate_root(vault, task_id), runtime, store)
    current_context, context_manifest = _context(
        meta, vault, worktree, runtime_root, task_id
    )
    run = quarantine_drifted_attempt(
        authorization=authorization,
        signal_authorization=signal_authorization,
        supported_close=supported_close,
        gate=gate,
        store=store,
        runtime=runtime,
        runtime_root=runtime_root,
        task_id=task_id,
        worktree=worktree,
        fault_observer=fault_observer,
        resume_completed_quarantine=resume_completed_quarantine,
    )
    if run is None:
        mark_drift_quarantine_fresh(gate, authorization)
        state = gate.read()
        return _receipt(
            status=(
                "verifying" if state.get("status") == "verifying" else "reviewing"
            ),
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=gate.rehydrate(),
        )
    recovery = _RecoveryContext(
        state=gate.read(),
        attention=attention,
        attention_record_sha256=attention_record_sha256,
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        task_id=task_id,
        gate=gate,
        run=run,
        store=store,
        current_context=current_context,
        context_manifest=context_manifest,
    )
    result = _recover_stale_boundary(recovery)
    mark_drift_quarantine_fresh(gate, authorization)
    return result


@dataclass(frozen=True)
class _RecoveryContext:
    state: dict[str, Any]
    attention: dict[str, Any]
    attention_record_sha256: str
    meta: dict[str, Any]
    vault: Path
    worktree: Path
    runtime_root: Path
    task_id: str
    gate: ReviewGateController
    run: Any
    store: OperationStore
    current_context: Any
    context_manifest: Path


def _running_recovery_receipt(
    recovery: _RecoveryContext,
) -> dict[str, Any] | None:
    stored_boundary = recovery.state.get("fresh_boundary")
    if not (
        recovery.state.get("fresh_reevaluation_used") is True
        and recovery.state.get("status")
        in {"fresh-reevaluation", "reviewing", "verifying"}
        and isinstance(stored_boundary, dict)
        and str(recovery.attention.get("id") or "")
        in str(stored_boundary.get("reason") or "")
    ):
        return None
    return _receipt(
        status=(
            "verifying"
            if recovery.state.get("status") == "verifying"
            else "reviewing"
        ),
        meta=recovery.meta,
        vault=recovery.vault,
        worktree=recovery.worktree,
        runtime_root=recovery.runtime_root,
        context_manifest=(
            recovery.runtime_root
            / recovery.run.execution.request.context.manifest
        ),
        run=recovery.run,
    )


def _approved_summary_recovery(
    recovery: _RecoveryContext,
) -> dict[str, Any] | None:
    previous_context = recovery.run.execution.request.context
    axes = recovery.run.execution.request.policy.axes
    simple_axis = axes[0] if len(axes) == 1 else ""
    is_summary_recovery = (
        recovery.state.get("status") == "approved"
        and recovery.state.get("fresh_reevaluation_used") is not True
        and recovery.state.get("final_results") not in ({}, None)
        and recovery.run.execution.request.policy.depth == "simple"
        and bool(simple_axis)
        and review_axis_responsibility(simple_axis) == "holistic"
        and previous_context.head_sha == recovery.current_context.head_sha
        and previous_context.verification_profile
        == recovery.current_context.verification_profile
        and previous_context.verification_profile_sha256
        == recovery.current_context.verification_profile_sha256
        and bool(previous_context.implementer_summary_sha256)
        and previous_context.implementer_summary_sha256
        != recovery.current_context.implementer_summary_sha256
    )
    if not is_summary_recovery:
        return None
    resolution = approved_summary_resolution(
        gate=recovery.gate,
        state=recovery.state,
        task_id=recovery.task_id,
        simple_axis=simple_axis,
        current_head=recovery.current_context.head_sha,
    )
    bundle = _recovery_resolution_bundle(
        recovery.worktree,
        recovery.task_id,
        resolution,
        recovery.current_context.head_sha,
        str(
            recovery.state.get("resolution_transport_identity_sha256") or ""
        ),
    )
    current_context, context_manifest = _context(
        recovery.meta,
        recovery.vault,
        recovery.worktree,
        recovery.runtime_root,
        recovery.task_id,
        resolution_bundle=bundle,
    )
    boundary = ReviewScopeBoundary(
        "context",
        review_context_sha256(previous_context),
        review_context_sha256(current_context),
        (
            "resolved mechanism escalation "
            f"{recovery.attention.get('id')}: review refreshed summary bytes only"
        ),
    )
    authorization = authorization_payload(
        review_operation_id=(
            recovery.run.execution.request.policy.operation_id
        ),
        dispatch_operation_id=recovery.task_id,
        boundary=boundary,
        attention=recovery.attention,
        attention_record_sha256=recovery.attention_record_sha256,
    )
    name, path = persist_authorization(
        recovery.gate,
        authorization,
        error_label="approved summary recovery",
    )
    recovery.gate.authorize_fresh_summary_boundary(
        recovery.run,
        boundary=boundary,
        context=current_context,
        authorization_pointer=name,
        authorization_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    return _launch_authorized_task_review(
        meta=recovery.meta,
        vault=recovery.vault,
        worktree=recovery.worktree,
        runtime_root=recovery.runtime_root,
        task_id=recovery.task_id,
        gate=recovery.gate,
        run=recovery.run,
        context=current_context,
        context_manifest=context_manifest,
        boundary=boundary,
        max_verify_iterations=0,
    )


def _assert_quiescent_stale_boundary(
    *,
    state: dict[str, Any],
    run: Any,
    store: OperationStore,
    task_id: str,
) -> None:
    if (
        state.get("status")
        not in {
            "verifying",
            "awaiting-resolution",
            "attention-required",
            "fresh-boundary-authorized",
        }
        or state.get("fresh_reevaluation_used") is True
        or state.get("final_results") not in ({}, None)
        or not run.execution.lanes
    ):
        raise TaskReviewError(
            "review mechanism recovery is not at one stale verification boundary"
        )
    for lane in run.execution.lanes:
        parent = store.read(task_id, lane.operation_id)
        round_ = run.rounds.get(lane.axis)
        if round_ is None:
            raise TaskReviewError("review mechanism recovery round is unavailable")
        child = store.read(task_id, round_.operation_id)
        if (
            parent.state not in TERMINAL
            or child.state not in TERMINAL
            or parent.resources != OwnedResources()
            or child.resources != OwnedResources()
            or parent.pending_effect
            or child.pending_effect
        ):
            raise TaskReviewError(
                "review mechanism recovery still has live review ownership"
            )


def _recover_stale_boundary(
    recovery: _RecoveryContext,
) -> dict[str, Any]:
    _assert_quiescent_stale_boundary(
        state=recovery.state,
        run=recovery.run,
        store=recovery.store,
        task_id=recovery.task_id,
    )
    boundary = ReviewScopeBoundary(
        "context",
        review_context_sha256(recovery.run.execution.request.context),
        review_context_sha256(recovery.current_context),
        (
            "resolved mechanism escalation "
            f"{recovery.attention.get('id')}: replace the dead verification runtime"
        ),
    )
    authorization = authorization_payload(
        review_operation_id=(
            recovery.run.execution.request.policy.operation_id
        ),
        dispatch_operation_id=recovery.task_id,
        boundary=boundary,
        attention=recovery.attention,
        attention_record_sha256=recovery.attention_record_sha256,
    )
    name, path = persist_authorization(
        recovery.gate,
        authorization,
        error_label="review mechanism recovery",
    )
    if recovery.state.get("status") in {"verifying", "awaiting-resolution"}:
        recovery.gate._mark_attention(recovery.run.execution.lanes)
    if recovery.gate.read().get("status") != "fresh-boundary-authorized":
        recovery.gate.authorize_fresh_boundary(
            recovery.run,
            boundary=boundary,
            authorization_pointer=name,
            authorization_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return _launch_authorized_task_review(
        meta=recovery.meta,
        vault=recovery.vault,
        worktree=recovery.worktree,
        runtime_root=recovery.runtime_root,
        task_id=recovery.task_id,
        gate=recovery.gate,
        run=recovery.run,
        context=recovery.current_context,
        context_manifest=recovery.context_manifest,
        boundary=boundary,
        max_verify_iterations=0,
    )


def recover_task_review_for_mechanism(
    worktree: Path,
    *,
    runtime_manager: object | None = None,
    quarantine_fault_observer: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Use one resolved mechanism escalation to replace a dead review lane."""

    worktree = worktree.expanduser().resolve()
    meta, vault, task_id = _validate_task(worktree)
    try:
        attention_record = load_latest(worktree)
    except EscalationRecordError as exc:
        raise TaskReviewError(f"task escalation record is invalid: {exc}") from exc
    if attention_record is None:
        raise TaskReviewError("task escalation record is unavailable")
    attention = attention_record.payload
    post_verification = authorized_post_verification_review_drive(
        attention_record, worktree
    )
    if post_verification is not None:
        supported = post_verification.supported_close
        return _recover_drift_quarantine(
            authorization=supported.signal_free.drift,
            signal_authorization=supported.signal_free,
            supported_close=supported,
            attention=attention,
            attention_record_sha256=attention_record.sha256,
            meta=meta,
            vault=vault,
            worktree=worktree,
            task_id=task_id,
            runtime_manager=runtime_manager,
            fault_observer=quarantine_fault_observer,
            resume_completed_quarantine=True,
        )
    supported_close = authorized_supported_close_retirement(
        attention_record, worktree
    )
    if supported_close is not None:
        return _recover_drift_quarantine(
            authorization=supported_close.signal_free.drift,
            signal_authorization=supported_close.signal_free,
            supported_close=supported_close,
            attention=attention,
            attention_record_sha256=attention_record.sha256,
            meta=meta,
            vault=vault,
            worktree=worktree,
            task_id=task_id,
            runtime_manager=runtime_manager,
            fault_observer=quarantine_fault_observer,
        )
    signal_authorization = authorized_signal_free_retirement(
        attention_record, worktree
    )
    if signal_authorization is not None:
        return _recover_drift_quarantine(
            authorization=signal_authorization.drift,
            signal_authorization=signal_authorization,
            attention=attention,
            attention_record_sha256=attention_record.sha256,
            meta=meta,
            vault=vault,
            worktree=worktree,
            task_id=task_id,
            runtime_manager=runtime_manager,
            fault_observer=quarantine_fault_observer,
        )
    drift_authorization = authorized_drift_quarantine(
        attention_record, worktree
    )
    if drift_authorization is not None:
        return _recover_drift_quarantine(
            authorization=drift_authorization,
            attention=attention,
            attention_record_sha256=attention_record.sha256,
            meta=meta,
            vault=vault,
            worktree=worktree,
            task_id=task_id,
            runtime_manager=runtime_manager,
            fault_observer=quarantine_fault_observer,
        )
    if _authorized_accepted_callback_head(attention, worktree):
        return _recover_accepted_exact_callbacks(
            attention=attention,
            meta=meta,
            vault=vault,
            worktree=worktree,
            task_id=task_id,
            runtime_manager=runtime_manager,
        )
    runtime_root = _runtime_root(vault, task_id)
    current_context, context_manifest = _context(
        meta,
        vault,
        worktree,
        runtime_root,
        task_id,
    )
    decision = str(attention.get("decision") or "")
    if (
        attention.get("status") != "resolved"
        or attention.get("category") != "mechanism-failure"
        or str(attention.get("worktree") or "") != str(worktree)
        or not decision.startswith(
            "authorize-one-bounded-fresh-context-review-boundary-for-"
        )
        or current_context.head_sha[:7] not in decision
    ):
        raise TaskReviewError(
            "review mechanism recovery lacks exact coordinator authorization"
        )
    store_root = vault / ".vault-meta" / "harness"
    store = OperationStore(store_root)
    runtime = runtime_manager or RuntimeSessionManager.for_root(
        vault, store_root=store_root
    )
    gate = ReviewGateController(
        _gate_root(vault, task_id),
        runtime,
        RecoveryRoundStore(store),
    )
    state = gate.read()
    run = gate.rehydrate()
    recovery = _RecoveryContext(
        state=state,
        attention=attention,
        attention_record_sha256=attention_record.sha256,
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        task_id=task_id,
        gate=gate,
        run=run,
        store=store,
        current_context=current_context,
        context_manifest=context_manifest,
    )
    running = _running_recovery_receipt(recovery)
    if running is not None:
        return running
    summary_recovery = _approved_summary_recovery(recovery)
    if summary_recovery is not None:
        return summary_recovery
    return _recover_stale_boundary(recovery)


def recover_post_verification_review_drive(
    worktree: Path,
    *,
    process_adapter: object,
    cmux_adapter: object,
    runtime_manager: object | None = None,
    fault_observer: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    """Synchronize one live dispatch before its authorized fresh review."""

    worktree = worktree.expanduser().resolve()
    meta, vault, task_id = _validate_task(worktree)
    try:
        attention_record = load_latest(worktree)
    except EscalationRecordError as exc:
        raise TaskReviewError(f"task escalation record is invalid: {exc}") from exc
    if attention_record is None:
        return None
    authorization = authorized_post_verification_review_drive(
        attention_record, worktree
    )
    if authorization is None:
        return None
    if authorization.dispatch_operation_id != task_id:
        raise TaskReviewError(
            "post-verification dispatch authorization identity drifted"
        )
    store_root = vault / ".vault-meta" / "harness"
    store = OperationStore(store_root)

    def recover_review() -> dict[str, Any]:
        supported = authorization.supported_close
        return _recover_drift_quarantine(
            authorization=supported.signal_free.drift,
            signal_authorization=supported.signal_free,
            supported_close=supported,
            attention=attention_record.payload,
            attention_record_sha256=attention_record.sha256,
            meta=meta,
            vault=vault,
            worktree=worktree,
            task_id=task_id,
            runtime_manager=runtime_manager,
            fault_observer=None,
            resume_completed_quarantine=True,
        )

    return synchronize_post_verification_review_drive(
        worktree,
        store=store,
        operation_id=task_id,
        active_review_operation_id=authorization.active_review_operation_id,
        authorization_record_id=authorization.authorization_record_id,
        authorization_record_sha256=authorization.authorization_record_sha256,
        process_adapter=process_adapter,
        cmux_adapter=cmux_adapter,
        recover_review=recover_review,
        _fault_hook=fault_observer,
    )


def restart_task_review_for_boundary(
    worktree: Path,
    *,
    kind: str,
    reason: str,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    """Start the one persisted fresh review allowed for a dispatched task."""

    worktree = worktree.expanduser().resolve()
    meta, vault, task_id = _validate_task(worktree)
    runtime_root = _runtime_root(vault, task_id)
    store_root = vault / ".vault-meta" / "harness"
    store = OperationStore(store_root)
    runtime = runtime_manager or RuntimeSessionManager.for_root(
        vault, store_root=store_root
    )
    gate = ReviewGateController(
        _gate_root(vault, task_id),
        runtime,
        store,
    )
    if not gate.state_path.is_file() or gate.state_path.is_symlink():
        raise TaskReviewError("fresh review gate is unavailable")
    state = gate.read()
    run = gate.rehydrate()
    context, context_manifest = _context(
        meta,
        vault,
        worktree,
        runtime_root,
        task_id,
    )
    stored_boundary = state.get("fresh_boundary")
    if (
        state.get("status") in {"fresh-reevaluation", "reviewing", "verifying"}
        and state.get("fresh_reevaluation_used") is True
        and isinstance(stored_boundary, dict)
        and stored_boundary.get("kind") == kind
        and stored_boundary.get("reason") == reason
        and stored_boundary.get("next_context_sha256")
        == review_context_sha256(context)
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
    if (
        state.get("status") != "fresh-boundary-authorized"
        or state.get("fresh_reevaluation_used") is True
        or not _dispatched_review_is_quiescent(store, task_id)
    ):
        raise TaskReviewError(
            "fresh review requires one quiescent authorized boundary"
        )
    previous_context = run.execution.request.context
    boundary = ReviewScopeBoundary(
        kind,
        review_context_sha256(previous_context),
        review_context_sha256(context),
        reason,
    )
    return _launch_authorized_task_review(
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        task_id=task_id,
        gate=gate,
        run=run,
        context=context,
        context_manifest=context_manifest,
        boundary=boundary,
    )
