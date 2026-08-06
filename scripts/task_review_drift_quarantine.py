"""Lifecycle orchestration for fail-closed stale review drift quarantine."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Callable

from harness.contracts import AttentionReason, OperationRecord, OwnedResources, to_dict
from harness.runtime_session_contracts import RuntimeSessionError
from harness.state_machine import TERMINAL
from harness.store import OperationStore
from harness.supervisor import OperationSupervisor, SupervisorError
from harness.workflows.review import finish_review_lane
from harness.workflows.review_gate import ReviewGateController, ReviewGateRun
from task_review_drift_contract import (
    DriftQuarantineAuthorization,
    SignalFreeRetirementAuthorization,
)
from task_review_drift_evidence import (
    build_evidence,
    evidence_root,
    persist_evidence,
    validate_archived_evidence,
    validate_progress,
    validate_unrelated_ownership_from_evidence,
    write_progress,
)
from task_review_shared import TaskReviewError, _atomic_json, _read_json


def _clean_descendant_head(worktree: Path, base_head: str) -> str:
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
        ["git", "merge-base", "--is-ancestor", base_head, "HEAD"],
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
            "drift quarantine requires a clean authorized descendant HEAD"
        )
    return current


def _fault(observer: Callable[[str], None] | None, event: str) -> None:
    if observer is not None:
        observer(event)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _persist_exact_json(path: Path, payload: dict[str, object], label: str) -> None:
    if path.exists():
        if path.is_symlink() or _read_json(path, label) != payload:
            raise TaskReviewError(f"{label} changed")
        return
    _atomic_json(path, payload)


def _signal_free_root(
    gate: ReviewGateController,
    authorization: DriftQuarantineAuthorization,
    signal_authorization: SignalFreeRetirementAuthorization,
) -> Path:
    return (
        evidence_root(gate, authorization)
        / "signal-free"
        / signal_authorization.authorization_record_id
    )


def _bind_signal_free_evidence(
    *,
    gate: ReviewGateController,
    authorization: DriftQuarantineAuthorization,
    signal_authorization: SignalFreeRetirementAuthorization,
    evidence_path: Path,
    evidence: dict[str, object],
    current_head: str,
) -> None:
    payload = {
        "schema_version": 1,
        "status": "authorized-signal-free-retirement",
        "operation_id": evidence["operation_id"],
        "reviewed_head_sha": evidence["reviewed_head_sha"],
        "archived_replacement_head_sha": evidence["replacement_head_sha"],
        "replacement_head_sha": current_head,
        "drift_authorization_record_id": authorization.authorization_record_id,
        "drift_authorization_record_sha256": authorization.authorization_record_sha256,
        "authorization_record_id": signal_authorization.authorization_record_id,
        "authorization_record_sha256": signal_authorization.authorization_record_sha256,
        "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        "os_signals_sent": 0,
        "cmux_signals_sent": 0,
        "provider_effects_replayed": 0,
        "callback_effects_replayed": 0,
    }
    _persist_exact_json(
        _signal_free_root(gate, authorization, signal_authorization)
        / "evidence.json",
        payload,
        "signal-free retirement evidence",
    )


def _absent_ownership(runtime: object, task_id: str, operation_id: str) -> object:
    prove = getattr(runtime, "prove_durable_cleanup_ownership", None)
    if not callable(prove):
        raise TaskReviewError(
            "signal-free retirement ownership inventory is unavailable"
        )
    try:
        ownership = prove(task_id, operation_id)
    except RuntimeSessionError as exc:
        raise TaskReviewError(
            "signal-free retirement ownership inventory is ambiguous"
        ) from exc
    statuses = {
        "process": str(getattr(ownership, "process_status", "")),
        "supervisor": str(getattr(ownership, "supervisor_status", "")),
        "surface": str(getattr(ownership, "surface_status", "")),
        "workspace": str(getattr(ownership, "workspace_status", "")),
    }
    if any(value == "alive" for value in statuses.values()):
        raise TaskReviewError(
            "signal-free retirement found an exact live resource"
        )
    if statuses != {
        "process": "dead",
        "supervisor": "dead",
        "surface": "missing",
        "workspace": "missing",
    }:
        raise TaskReviewError(
            "signal-free retirement ownership inventory is ambiguous"
        )
    if not str(getattr(ownership, "workspace_id", "")) or not str(
        getattr(ownership, "window_id", "")
    ):
        raise TaskReviewError(
            "signal-free retirement ownership inventory is partial"
        )
    return ownership


def _retirement_payload(
    *,
    parent: OperationRecord,
    row: dict[str, object],
    ownership: object,
    evidence_path: Path,
    current_head: str,
    authorization: DriftQuarantineAuthorization,
    signal_authorization: SignalFreeRetirementAuthorization,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "proven-absent",
        "axis": row["axis"],
        "parent_operation_id": parent.spec.operation_id,
        "parent_run_id": parent.run_id,
        "parent_lane_id": parent.lane_id,
        "original_operation_sha256": _canonical_sha256(to_dict(parent)),
        "original_resources": to_dict(parent.resources),
        "process_status": getattr(ownership, "process_status", ""),
        "supervisor_status": getattr(ownership, "supervisor_status", ""),
        "surface_status": getattr(ownership, "surface_status", ""),
        "workspace_status": getattr(ownership, "workspace_status", ""),
        "workspace_id": getattr(ownership, "workspace_id", ""),
        "window_id": getattr(ownership, "window_id", ""),
        "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        "replacement_head_sha": current_head,
        "drift_authorization_record_id": authorization.authorization_record_id,
        "drift_authorization_record_sha256": authorization.authorization_record_sha256,
        "authorization_record_id": signal_authorization.authorization_record_id,
        "authorization_record_sha256": signal_authorization.authorization_record_sha256,
        "os_signals_sent": 0,
        "cmux_signals_sent": 0,
        "provider_effects_replayed": 0,
        "callback_effects_replayed": 0,
    }


def _retirement_receipt_path(
    gate: ReviewGateController,
    authorization: DriftQuarantineAuthorization,
    signal_authorization: SignalFreeRetirementAuthorization,
    operation_id: str,
) -> Path:
    return (
        _signal_free_root(gate, authorization, signal_authorization)
        / "parents"
        / f"{operation_id}.json"
    )


def _validate_completed_retirement_receipt(
    *,
    path: Path,
    digest: str,
    row: dict[str, object],
    evidence_path: Path,
    current_head: str,
    authorization: DriftQuarantineAuthorization,
    signal_authorization: SignalFreeRetirementAuthorization,
) -> None:
    if path.is_symlink() or not path.is_file():
        raise TaskReviewError("signal-free retirement receipt is unavailable")
    raw = path.read_bytes()
    receipt = _read_json(path, "signal-free retirement receipt")
    resources = receipt.get("original_resources")
    required_zero = (
        "os_signals_sent",
        "cmux_signals_sent",
        "provider_effects_replayed",
        "callback_effects_replayed",
    )
    if (
        hashlib.sha256(raw).hexdigest() != digest
        or receipt.get("schema_version") != 1
        or receipt.get("status") != "proven-absent"
        or receipt.get("axis") != row["axis"]
        or receipt.get("parent_operation_id") != row["parent_operation_id"]
        or receipt.get("parent_run_id") != row["parent_run_id"]
        or not receipt.get("parent_lane_id")
        or re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("original_operation_sha256") or "")) is None
        or not isinstance(resources, dict)
        or not resources.get("surface_id")
        or type(resources.get("process_group")) is not int
        or resources["process_group"] <= 1
        or type(resources.get("supervisor_pid")) is not int
        or resources["supervisor_pid"] <= 1
        or re.fullmatch(r"[0-9a-f]{64}", str(resources.get("process_identity") or "")) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(resources.get("supervisor_identity") or "")) is None
        or receipt.get("process_status") != "dead"
        or receipt.get("supervisor_status") != "dead"
        or receipt.get("surface_status") != "missing"
        or receipt.get("workspace_status") != "missing"
        or not receipt.get("workspace_id")
        or not receipt.get("window_id")
        or receipt.get("evidence_sha256")
        != hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        or receipt.get("replacement_head_sha") != current_head
        or receipt.get("drift_authorization_record_id")
        != authorization.authorization_record_id
        or receipt.get("drift_authorization_record_sha256")
        != authorization.authorization_record_sha256
        or receipt.get("authorization_record_id")
        != signal_authorization.authorization_record_id
        or receipt.get("authorization_record_sha256")
        != signal_authorization.authorization_record_sha256
        or any(receipt.get(field) != 0 for field in required_zero)
    ):
        raise TaskReviewError("signal-free retirement receipt changed")


def _retire_signal_free_parent(
    *,
    row: dict[str, object],
    parent: OperationRecord,
    gate: ReviewGateController,
    store: OperationStore,
    runtime: object,
    task_id: str,
    authorization: DriftQuarantineAuthorization,
    signal_authorization: SignalFreeRetirementAuthorization,
    evidence_path: Path,
    current_head: str,
    cleaned_parents: list[str],
    terminal_rounds: list[str],
    retirement_receipts: dict[str, str],
    fault_observer: Callable[[str], None] | None,
) -> OperationRecord:
    operation_id = str(row["parent_operation_id"])
    receipt_path = _retirement_receipt_path(
        gate, authorization, signal_authorization, operation_id
    )
    if parent.state == "complete" and parent.resources == OwnedResources():
        digest = retirement_receipts.get(operation_id, "")
        _validate_completed_retirement_receipt(
            path=receipt_path,
            digest=digest,
            row=row,
            evidence_path=evidence_path,
            current_head=current_head,
            authorization=authorization,
            signal_authorization=signal_authorization,
        )
        return parent
    if parent.resources == OwnedResources():
        raise TaskReviewError("signal-free retirement resource projection is partial")
    ownership = _absent_ownership(runtime, task_id, operation_id)
    payload = _retirement_payload(
        parent=parent,
        row=row,
        ownership=ownership,
        evidence_path=evidence_path,
        current_head=current_head,
        authorization=authorization,
        signal_authorization=signal_authorization,
    )
    _persist_exact_json(receipt_path, payload, "signal-free retirement receipt")
    receipt_digest = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    retirement_receipts[operation_id] = receipt_digest
    write_progress(
        gate,
        authorization,
        evidence_path=evidence_path,
        status="cleaning",
        cleaned_parents=cleaned_parents,
        terminal_rounds=terminal_rounds,
        retirement_receipts=retirement_receipts,
    )
    _fault(fault_observer, f"signal-free-retirement-receipt:{row['axis']}")
    second = _absent_ownership(runtime, task_id, operation_id)
    if _retirement_payload(
        parent=parent,
        row=row,
        ownership=second,
        evidence_path=evidence_path,
        current_head=current_head,
        authorization=authorization,
        signal_authorization=signal_authorization,
    ) != payload:
        raise TaskReviewError("signal-free retirement ownership inventory changed")
    try:
        retired = OperationSupervisor(
            store, task_id, operation_id
        ).retire_proven_absent_resources(parent.resources)
    except SupervisorError as exc:
        raise TaskReviewError(str(exc)) from exc
    _fault(fault_observer, f"signal-free-parent-retired:{row['axis']}")
    return retired


def _clean_parents(
    *,
    evidence: dict[str, object],
    gate: ReviewGateController,
    run: ReviewGateRun,
    store: OperationStore,
    runtime: object,
    task_id: str,
    authorization: DriftQuarantineAuthorization,
    evidence_path: Path,
    fault_observer: Callable[[str], None] | None,
    progress: dict[str, object],
    signal_authorization: SignalFreeRetirementAuthorization | None,
    current_head: str,
) -> tuple[list[str], dict[str, str]]:
    cleaned_parents = list(progress.get("cleaned_parents") or [])
    terminal_rounds = list(progress.get("terminal_rounds") or [])
    retirement_receipts = dict(progress.get("retirement_receipts") or {})
    lanes_by_axis = {lane.axis: lane for lane in run.execution.lanes}
    rows = evidence["lanes"]
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, dict)
        axis = str(row["axis"])
        lane = lanes_by_axis.get(axis)
        if lane is None or lane.operation_id != row["parent_operation_id"]:
            raise TaskReviewError("drift quarantine lane changed")
        parent = store.read(task_id, lane.operation_id)
        if lane.operation_id in cleaned_parents:
            if parent.state != "complete" or parent.resources != OwnedResources():
                raise TaskReviewError("drift quarantine progress identity changed")
            continue
        if (
            signal_authorization is not None
            and parent.state == "complete"
            and parent.resources == OwnedResources()
        ):
            parent = _retire_signal_free_parent(
                row=row,
                parent=parent,
                gate=gate,
                store=store,
                runtime=runtime,
                task_id=task_id,
                authorization=authorization,
                signal_authorization=signal_authorization,
                evidence_path=evidence_path,
                current_head=current_head,
                cleaned_parents=cleaned_parents,
                terminal_rounds=terminal_rounds,
                retirement_receipts=retirement_receipts,
                fault_observer=fault_observer,
            )
        if parent.state not in TERMINAL:
            if signal_authorization is not None:
                parent = _retire_signal_free_parent(
                    row=row,
                    parent=parent,
                    gate=gate,
                    store=store,
                    runtime=runtime,
                    task_id=task_id,
                    authorization=authorization,
                    signal_authorization=signal_authorization,
                    evidence_path=evidence_path,
                    current_head=current_head,
                    cleaned_parents=cleaned_parents,
                    terminal_rounds=terminal_rounds,
                    retirement_receipts=retirement_receipts,
                    fault_observer=fault_observer,
                )
            elif parent.state == "awaiting-callback":
                store.transition(
                    task_id,
                    lane.operation_id,
                    "attention-required",
                    reason=AttentionReason.RETRY_EXHAUSTED,
                )
            elif parent.state not in {
                "attention-required",
                "cancelling",
                "finalizing",
                "exiting",
            }:
                raise TaskReviewError("drift quarantine parent state changed")
            if signal_authorization is None:
                cleaned = finish_review_lane(
                    runtime, lane, timeout_seconds=30.0, poll_seconds=0.1
                )
                if cleaned.state != "complete" or cleaned.resources != OwnedResources():
                    raise TaskReviewError(
                        "drift quarantine resource cleanup is incomplete"
                    )
        parent = store.read(task_id, lane.operation_id)
        if parent.state != "complete" or parent.resources != OwnedResources():
            raise TaskReviewError("drift quarantine resource cleanup is incomplete")
        cleaned_parents.append(lane.operation_id)
        write_progress(
            gate,
            authorization,
            evidence_path=evidence_path,
            status="cleaning",
            cleaned_parents=cleaned_parents,
            terminal_rounds=terminal_rounds,
            retirement_receipts=(
                retirement_receipts if signal_authorization is not None else None
            ),
        )
        _fault(fault_observer, f"drift-quarantine-parent-cleaned:{axis}")
    return cleaned_parents, retirement_receipts


def _terminalize_rounds(
    *,
    evidence: dict[str, object],
    gate: ReviewGateController,
    store: OperationStore,
    task_id: str,
    authorization: DriftQuarantineAuthorization,
    evidence_path: Path,
    cleaned_parents: list[str],
    progress: dict[str, object],
    signal_authorization: SignalFreeRetirementAuthorization | None,
    retirement_receipts: dict[str, str],
) -> list[str]:
    terminal_rounds = list(progress.get("terminal_rounds") or [])
    rows = evidence["lanes"]
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, dict)
        round_id = str(row["round_operation_id"])
        child = store.read(task_id, round_id)
        if round_id in terminal_rounds:
            if child.state != "complete" or child.resources != OwnedResources():
                raise TaskReviewError("drift quarantine progress identity changed")
            continue
        if (
            child.resources != OwnedResources()
            or child.pending_effect
            or child.accepted_callback_id != row["accepted_callback_id"]
            or child.accepted_callback_sha256
            != row["accepted_callback_sha256"]
            or child.accepted_callback_kind != "review"
        ):
            raise TaskReviewError("drift quarantine round identity changed")
        for current, following in (
            ("verifying", "finalizing"),
            ("finalizing", "exiting"),
            ("exiting", "complete"),
        ):
            if child.state == current:
                store.transition(task_id, round_id, following)
                child = store.read(task_id, round_id)
        if child.state != "complete":
            raise TaskReviewError("drift quarantine round cleanup is incomplete")
        terminal_rounds.append(round_id)
        write_progress(
            gate,
            authorization,
            evidence_path=evidence_path,
            status="terminalizing-rounds",
            cleaned_parents=cleaned_parents,
            terminal_rounds=terminal_rounds,
            retirement_receipts=(
                retirement_receipts if signal_authorization is not None else None
            ),
        )
    return terminal_rounds


def quarantine_drifted_attempt(
    *,
    authorization: DriftQuarantineAuthorization,
    signal_authorization: SignalFreeRetirementAuthorization | None = None,
    gate: ReviewGateController,
    store: OperationStore,
    runtime: object,
    runtime_root: Path,
    task_id: str,
    worktree: Path,
    fault_observer: Callable[[str], None] | None = None,
) -> ReviewGateRun | None:
    """Quarantine exact stale ownership without consuming either callback."""

    current_head = _clean_descendant_head(
        worktree, authorization.descendant_base_head
    )
    archived = validate_archived_evidence(gate, authorization)
    if archived is not None and signal_authorization is None and archived.get(
        "replacement_head_sha"
    ) != current_head:
        raise TaskReviewError("drift quarantine replacement HEAD changed")
    state = gate.read()
    if archived is not None and state.get("fresh_reevaluation_used") is True:
        context = state.get("context")
        if (
            state.get("status") not in {"fresh-reevaluation", "reviewing", "verifying"}
            or not isinstance(context, dict)
            or context.get("head_sha") != current_head
        ):
            raise TaskReviewError("drift quarantine fresh boundary changed")
        return None
    if signal_authorization is not None and archived is None:
        raise TaskReviewError("signal-free retirement archive is unavailable")
    run = gate.rehydrate_attempt()
    evidence = archived or build_evidence(
        authorization=authorization,
        gate=gate,
        run=run,
        store=store,
        runtime_root=runtime_root,
        task_id=task_id,
        current_head=current_head,
    )
    evidence_path = persist_evidence(gate, authorization, evidence)
    if archived is None:
        write_progress(
            gate,
            authorization,
            evidence_path=evidence_path,
            status="prepared",
            cleaned_parents=[],
            terminal_rounds=[],
        )
        _fault(fault_observer, "drift-quarantine-prepared")
    progress = validate_progress(gate, authorization, evidence)
    if signal_authorization is not None:
        state = gate.read()
        attempt = state.get("attempt")
        identity = attempt.get("identity") if isinstance(attempt, dict) else None
        if (
            state.get("status") != "reviewing"
            or state.get("fresh_reevaluation_used") is True
            or state.get("drift_quarantine") not in (None, {})
            or not isinstance(attempt, dict)
            or attempt.get("status") != "awaiting-callback"
            or not isinstance(identity, dict)
            or identity.get("exact_head_sha") != evidence.get("reviewed_head_sha")
        ):
            raise TaskReviewError("signal-free retirement gate drifted")
        validate_unrelated_ownership_from_evidence(store, task_id, evidence)
        _bind_signal_free_evidence(
            gate=gate,
            authorization=authorization,
            signal_authorization=signal_authorization,
            evidence_path=evidence_path,
            evidence=evidence,
            current_head=current_head,
        )
    cleaned_parents, retirement_receipts = _clean_parents(
        evidence=evidence,
        gate=gate,
        run=run,
        store=store,
        runtime=runtime,
        task_id=task_id,
        authorization=authorization,
        evidence_path=evidence_path,
        fault_observer=fault_observer,
        progress=progress,
        signal_authorization=signal_authorization,
        current_head=current_head,
    )
    terminal_rounds = _terminalize_rounds(
        evidence=evidence,
        gate=gate,
        store=store,
        task_id=task_id,
        authorization=authorization,
        evidence_path=evidence_path,
        cleaned_parents=cleaned_parents,
        progress=progress,
        signal_authorization=signal_authorization,
        retirement_receipts=retirement_receipts,
    )
    attempt = gate._attempt()
    if attempt.status == "awaiting-callback":
        attempt = attempt.fail_attention(attempt.identity)
    elif (
        attempt.status != "terminal"
        or attempt.terminal is None
        or attempt.terminal.result.value != "attention-required"
    ):
        raise TaskReviewError("drift quarantine attempt state changed")
    gate._replace(
        status="attention-required",
        attempt=attempt.payload(),
        drift_quarantine={
            "status": "quarantined",
            "evidence_pointer": evidence_path.relative_to(gate.root).as_posix(),
            "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            "authorization_record_id": authorization.authorization_record_id,
            "authorization_record_sha256": authorization.authorization_record_sha256,
        },
    )
    write_progress(
        gate,
        authorization,
        evidence_path=evidence_path,
        status="quarantined",
        cleaned_parents=cleaned_parents,
        terminal_rounds=terminal_rounds,
        retirement_receipts=(
            retirement_receipts
            if signal_authorization is not None
            else None
        ),
    )
    _fault(fault_observer, "drift-quarantine-complete")
    return run


def mark_drift_quarantine_fresh(
    gate: ReviewGateController,
    authorization: DriftQuarantineAuthorization,
) -> None:
    evidence = validate_archived_evidence(gate, authorization)
    if evidence is None:
        raise TaskReviewError("drift quarantine evidence is unavailable")
    if gate.read().get("fresh_reevaluation_used") is not True:
        raise TaskReviewError("drift quarantine fresh review did not start")
    rows = evidence["lanes"]
    assert isinstance(rows, list)
    progress = validate_progress(gate, authorization, evidence)
    retirement_receipts = (
        dict(progress.get("retirement_receipts") or {})
        if progress.get("schema_version") == 2
        else None
    )
    write_progress(
        gate,
        authorization,
        evidence_path=evidence_root(gate, authorization) / "evidence.json",
        status="fresh-review-started",
        cleaned_parents=[str(row["parent_operation_id"]) for row in rows],
        terminal_rounds=[str(row["round_operation_id"]) for row in rows],
        retirement_receipts=retirement_receipts,
    )
