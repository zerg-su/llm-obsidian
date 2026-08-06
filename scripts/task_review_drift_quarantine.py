"""Lifecycle orchestration for fail-closed stale review drift quarantine."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Callable

from harness.contracts import AttentionReason, OwnedResources
from harness.state_machine import TERMINAL
from harness.store import OperationStore
from harness.workflows.review import finish_review_lane
from harness.workflows.review_gate import ReviewGateController, ReviewGateRun
from task_review_drift_contract import DriftQuarantineAuthorization
from task_review_drift_evidence import (
    build_evidence,
    evidence_root,
    persist_evidence,
    validate_archived_evidence,
    write_progress,
)
from task_review_shared import TaskReviewError


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
) -> list[str]:
    cleaned_parents: list[str] = []
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
        if parent.state not in TERMINAL:
            if parent.state == "awaiting-callback":
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
            terminal_rounds=[],
        )
        _fault(fault_observer, f"drift-quarantine-parent-cleaned:{axis}")
    return cleaned_parents


def _terminalize_rounds(
    *,
    evidence: dict[str, object],
    gate: ReviewGateController,
    store: OperationStore,
    task_id: str,
    authorization: DriftQuarantineAuthorization,
    evidence_path: Path,
    cleaned_parents: list[str],
) -> list[str]:
    terminal_rounds: list[str] = []
    rows = evidence["lanes"]
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, dict)
        round_id = str(row["round_operation_id"])
        child = store.read(task_id, round_id)
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
        )
    return terminal_rounds


def quarantine_drifted_attempt(
    *,
    authorization: DriftQuarantineAuthorization,
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
    if (
        archived is not None
        and archived.get("replacement_head_sha") != current_head
    ):
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
    write_progress(
        gate,
        authorization,
        evidence_path=evidence_path,
        status="prepared",
        cleaned_parents=[],
        terminal_rounds=[],
    )
    _fault(fault_observer, "drift-quarantine-prepared")
    cleaned_parents = _clean_parents(
        evidence=evidence,
        gate=gate,
        run=run,
        store=store,
        runtime=runtime,
        task_id=task_id,
        authorization=authorization,
        evidence_path=evidence_path,
        fault_observer=fault_observer,
    )
    terminal_rounds = _terminalize_rounds(
        evidence=evidence,
        gate=gate,
        store=store,
        task_id=task_id,
        authorization=authorization,
        evidence_path=evidence_path,
        cleaned_parents=cleaned_parents,
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
    write_progress(
        gate,
        authorization,
        evidence_path=evidence_root(gate, authorization) / "evidence.json",
        status="fresh-review-started",
        cleaned_parents=[str(row["parent_operation_id"]) for row in rows],
        terminal_rounds=[str(row["round_operation_id"]) for row in rows],
    )
