"""Crash-safe continuation from one completed verify into one fresh review."""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from time import time
from typing import Callable, Mapping

from .contracts import AttentionReason, OperationRecord, to_dict
from .liveness import LivenessController, LivenessState
from .post_verification_review_drive_boundary import (
    HEAD,
    MARKER_NAME,
    RECEIPT_FIELDS,
    RECEIPT_NAME,
    SHA256,
    PostVerificationReviewDriveError,
    canonical as _canonical,
    regular_json as _regular_json,
    review_drive_sha256 as _review_drive_sha256,
    runtime_bindings as _runtime_bindings,
    sha256 as _sha256,
)
from .state_machine import transition
from .store import OperationStore


def _binding_sha256(receipt: Mapping[str, object]) -> str:
    return _sha256(
        _canonical(
            {
                key: value
                for key, value in receipt.items()
                if key not in {"status", "binding_sha256"}
            }
        )
    )


def _empty_transition() -> dict[str, object]:
    return {
        "progress_at": 0.0,
        "source_revision": 0,
        "target_revision": 0,
        "source_deadline_at": 0.0,
        "target_deadline_at": 0.0,
        "source_operation_sha256": "",
        "target_operation_sha256": "",
        "source_liveness_sha256": "",
        "target_liveness_sha256": "",
    }


def _write_receipt(path: Path, value: Mapping[str, object]) -> None:
    OperationStore._write(path, dict(value))


def _fresh_marker(
    runtime_root: Path, receipt: Mapping[str, object]
) -> dict[str, object]:
    gate_path = (
        runtime_root.parents[3]
        / "review-data"
        / str(receipt["operation_id"])
        / str(receipt["operation_id"])
        / "review-gate.json"
    )
    gate, gate_raw = _regular_json(gate_path, "fresh review gate")
    context = gate.get("context")
    if (
        gate.get("fresh_reevaluation_used") is not True
        or gate.get("status") not in {"fresh-reevaluation", "reviewing", "verifying"}
        or not isinstance(context, dict)
        or context.get("head_sha") != receipt.get("target_head_sha")
    ):
        raise PostVerificationReviewDriveError("fresh review gate drifted")
    return {
        "schema_version": 1,
        "operation_id": receipt["operation_id"],
        "definition_sha256": receipt["definition_sha256"],
        "drive_sha256": _review_drive_sha256(
            runtime_root, gate_raw, str(receipt["target_head_sha"])
        ),
        "status": "started",
    }


def _publish_fresh_marker(
    runtime_root: Path, receipt: Mapping[str, object]
) -> dict[str, object]:
    expected = _fresh_marker(runtime_root, receipt)
    path = runtime_root / MARKER_NAME
    if path.exists():
        current, _raw = _regular_json(path, "fresh review marker")
        if current != expected:
            raise PostVerificationReviewDriveError("fresh review marker drifted")
    else:
        _write_receipt(path, expected)
    return expected


def _validated_receipt(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    value, _raw = _regular_json(path, "post-verification continuation receipt")
    if (
        set(value) != RECEIPT_FIELDS
        or value.get("schema_version") != 1
        or value.get("status") not in {"prepared", "transitioning", "applied"}
        or value.get("binding_sha256") != _binding_sha256(value)
        or any(
            value.get(key) != 0
            for key in (
                "os_signals_sent",
                "cmux_signals_sent",
                "callback_effects_replayed",
                "provider_effects_replayed",
            )
        )
    ):
        raise PostVerificationReviewDriveError(
            "post-verification continuation receipt is invalid"
        )
    for key in (
        "authorization_record_sha256",
        "definition_sha256",
        "source_verification_receipt_sha256",
        "controller_receipt_sha256",
        "drive_sha256",
        "drive_marker_sha256",
        "callback_error_sha256",
        "session_sha256",
        "launch_sha256",
        "ready_sha256",
        "provider_events_sha256",
        "binding_sha256",
    ):
        if not SHA256.fullmatch(str(value.get(key) or "")):
            raise PostVerificationReviewDriveError(
                "post-verification continuation digest is invalid"
            )
    for key in (
        "source_verification_head_sha",
        "failed_drive_head_sha",
        "target_head_sha",
        "target_tree_sha",
    ):
        if not HEAD.fullmatch(str(value.get(key) or "")):
            raise PostVerificationReviewDriveError(
                "post-verification continuation HEAD is invalid"
            )
    if value["status"] != "prepared":
        for key in (
            "source_operation_sha256",
            "target_operation_sha256",
            "source_liveness_sha256",
            "target_liveness_sha256",
        ):
            if not SHA256.fullmatch(str(value.get(key) or "")):
                raise PostVerificationReviewDriveError(
                    "post-verification continuation transition is invalid"
                )
    return value


def _static_matches(receipt: Mapping[str, object], static: Mapping[str, object]) -> bool:
    for key, value in static.items():
        if key in {"gate_source_sha256", "progress_source_sha256"} and not value:
            continue
        if receipt.get(key) != value:
            return False
    return True


def _target_records(
    record: OperationRecord,
    liveness: LivenessState,
    *,
    observed: float,
    time_budget: float,
) -> tuple[OperationRecord, LivenessState]:
    if (
        record.state != "attention-required"
        or record.resume_state != "awaiting-callback"
        or record.attention_reason != AttentionReason.ATTENTION_REQUIRED
        or liveness.operation_revision != record.revision
        or liveness.operation_state != record.state
    ):
        raise PostVerificationReviewDriveError(
            "post-verification attention latch is not exact"
        )
    target, changed = transition(record, "awaiting-callback")
    if not changed.changed:
        raise PostVerificationReviewDriveError(
            "post-verification attention latch did not transition"
        )
    target = replace(target, deadline_at=observed + time_budget)
    target_liveness = replace(
        liveness,
        last_progress_at=observed,
        operation_revision=target.revision,
        operation_state=target.state,
        nudge_count=0,
        restart_count=0,
    )
    return target, target_liveness


def _apply_transition(
    *,
    store: OperationStore,
    operation_id: str,
    runtime_root: Path,
    receipt_path: Path,
    receipt: dict[str, object],
    observed: float,
    fault: Callable[[str], None],
) -> dict[str, object]:
    liveness = LivenessController(runtime_root / "liveness")
    with store.locked(operation_id):
        with liveness._locked():
            record = store.read(operation_id, operation_id)
            live = liveness._state()
            if live is None:
                raise PostVerificationReviewDriveError(
                    "dispatch provider liveness is unavailable"
                )
            operation_raw = store._operation_path(operation_id, operation_id).read_bytes()
            liveness_raw = (runtime_root / "liveness" / "state.json").read_bytes()
            operation_sha = _sha256(operation_raw)
            liveness_sha = _sha256(liveness_raw)
            if receipt["status"] == "applied":
                if (
                    record.state == "awaiting-callback"
                    and record.revision == receipt["target_revision"]
                    and live.operation_state == "awaiting-callback"
                    and live.operation_revision == record.revision
                    and live.last_progress_at >= receipt["progress_at"]
                ):
                    return receipt
                raise PostVerificationReviewDriveError(
                    "applied post-verification continuation drifted"
                )
            if receipt["status"] == "prepared":
                target, target_live = _target_records(
                    record,
                    live,
                    observed=observed,
                    time_budget=float(receipt["time_budget_seconds"]),
                )
                receipt = {
                    **receipt,
                    "status": "transitioning",
                    "progress_at": observed,
                    "source_revision": record.revision,
                    "target_revision": target.revision,
                    "source_deadline_at": record.deadline_at,
                    "target_deadline_at": target.deadline_at,
                    "source_operation_sha256": operation_sha,
                    "target_operation_sha256": _sha256(
                        _canonical(to_dict(target), newline=True)
                    ),
                    "source_liveness_sha256": liveness_sha,
                    "target_liveness_sha256": _sha256(
                        _canonical(to_dict(target_live), newline=True)
                    ),
                }
                receipt["binding_sha256"] = _binding_sha256(receipt)
                _write_receipt(receipt_path, receipt)
                fault("transition-prepared")
            source_operation = operation_sha == receipt["source_operation_sha256"]
            target_operation = operation_sha == receipt["target_operation_sha256"]
            source_live = liveness_sha == receipt["source_liveness_sha256"]
            target_live = liveness_sha == receipt["target_liveness_sha256"]
            if not (source_operation or target_operation) or not (source_live or target_live):
                raise PostVerificationReviewDriveError(
                    "post-verification continuation publication drifted"
                )
            if target_live and source_operation:
                raise PostVerificationReviewDriveError(
                    "post-verification continuation publication order drifted"
                )
            if source_operation:
                target, _changed = transition(record, "awaiting-callback")
                target = replace(target, deadline_at=float(receipt["target_deadline_at"]))
                if _sha256(_canonical(to_dict(target), newline=True)) != receipt[
                    "target_operation_sha256"
                ]:
                    raise PostVerificationReviewDriveError(
                        "post-verification target operation drifted"
                    )
                store._write(store._operation_path(operation_id, operation_id), to_dict(target))
                record = target
                fault("operation-written")
            elif (
                record.state != "awaiting-callback"
                or record.revision != receipt["target_revision"]
            ):
                raise PostVerificationReviewDriveError(
                    "post-verification target operation is invalid"
                )
            if source_live:
                target_live_state = replace(
                    live,
                    last_progress_at=float(receipt["progress_at"]),
                    operation_revision=int(receipt["target_revision"]),
                    operation_state="awaiting-callback",
                    nudge_count=0,
                    restart_count=0,
                )
                if _sha256(_canonical(to_dict(target_live_state), newline=True)) != receipt[
                    "target_liveness_sha256"
                ]:
                    raise PostVerificationReviewDriveError(
                        "post-verification target liveness drifted"
                    )
                LivenessController._write(
                    runtime_root / "liveness" / "state.json", to_dict(target_live_state)
                )
                fault("liveness-written")
            elif (
                live.operation_state != "awaiting-callback"
                or live.operation_revision != receipt["target_revision"]
            ):
                raise PostVerificationReviewDriveError(
                    "post-verification target liveness is invalid"
                )
            receipt = {**receipt, "status": "applied"}
            receipt["binding_sha256"] = _binding_sha256(receipt)
            _write_receipt(receipt_path, receipt)
            fault("applied")
            return receipt


def synchronize_post_verification_review_drive(
    worktree: Path | str,
    *,
    store: OperationStore,
    operation_id: str,
    active_review_operation_id: str,
    authorization_record_id: str,
    authorization_record_sha256: str,
    process_adapter: object,
    cmux_adapter: object,
    recover_review: Callable[[], Mapping[str, object]],
    now: float | None = None,
    _fault_hook: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Continue one exact live provider without verification or provider replay."""

    root = Path(worktree).expanduser().resolve(strict=True)
    observed = time() if now is None else float(now)
    if not root.is_dir() or not math.isfinite(observed) or observed <= 0:
        raise PostVerificationReviewDriveError(
            "post-verification continuation input is invalid"
        )
    fault = _fault_hook or (lambda _stage: None)
    preliminary_runtime = (
        store.root / "owners" / operation_id / "runtime" / operation_id
    )
    receipt_path = preliminary_runtime / RECEIPT_NAME
    receipt = _validated_receipt(receipt_path)
    static, record, live, runtime_root = _runtime_bindings(
        worktree=root,
        store=store,
        operation_id=operation_id,
        active_review_operation_id=active_review_operation_id,
        authorization_record_id=authorization_record_id,
        authorization_record_sha256=authorization_record_sha256,
        process_adapter=process_adapter,
        cmux_adapter=cmux_adapter,
        transition_receipt=receipt,
    )
    if runtime_root != preliminary_runtime:
        raise PostVerificationReviewDriveError(
            "post-verification continuation runtime drifted"
        )
    if receipt is None:
        if (
            record.state != "attention-required"
            or live.operation_state != "attention-required"
        ):
            raise PostVerificationReviewDriveError(
                "post-verification continuation did not start at attention"
            )
        receipt = {
            **static,
            "status": "prepared",
            **_empty_transition(),
            "binding_sha256": "",
        }
        receipt["binding_sha256"] = _binding_sha256(receipt)
        _write_receipt(receipt_path, receipt)
        fault("prepared")
    elif not _static_matches(receipt, static):
        raise PostVerificationReviewDriveError(
            "post-verification continuation binding drifted"
        )
    gate_path = store.root / "review-data" / operation_id / operation_id / "review-gate.json"
    gate, _gate_raw = _regular_json(gate_path, "review gate")
    if gate.get("fresh_reevaluation_used") is not True:
        outcome = recover_review()
        if not isinstance(outcome, Mapping) or outcome.get("status") not in {
            "reviewing",
            "verifying",
        }:
            raise PostVerificationReviewDriveError(
                "fresh review recovery made no bounded progress"
            )
        fault("review-started")
    refreshed_static, _record, _live, refreshed_runtime = _runtime_bindings(
        worktree=root,
        store=store,
        operation_id=operation_id,
        active_review_operation_id=active_review_operation_id,
        authorization_record_id=authorization_record_id,
        authorization_record_sha256=authorization_record_sha256,
        process_adapter=process_adapter,
        cmux_adapter=cmux_adapter,
        transition_receipt=receipt,
    )
    if refreshed_runtime != runtime_root or not _static_matches(receipt, refreshed_static):
        raise PostVerificationReviewDriveError(
            "fresh review recovery changed the continuation binding"
        )
    _publish_fresh_marker(runtime_root, receipt)
    fault("fresh-marker-written")
    return _apply_transition(
        store=store,
        operation_id=operation_id,
        runtime_root=runtime_root,
        receipt_path=receipt_path,
        receipt=receipt,
        observed=observed,
        fault=fault,
    )


def _applied_receipt(runtime_root: Path, operation_id: str) -> dict[str, object] | None:
    receipt = _validated_receipt(runtime_root / RECEIPT_NAME)
    if receipt is None:
        return None
    marker_path = runtime_root / "pipeline-review-start.json"
    callback_error_path = runtime_root / "callback-error.json"
    fresh_marker_path = runtime_root / MARKER_NAME
    fresh_marker = _fresh_marker(runtime_root, receipt)
    stored_fresh_marker, _fresh_raw = _regular_json(
        fresh_marker_path, "fresh review marker"
    )
    if (
        receipt.get("status") != "applied"
        or receipt.get("operation_id") != operation_id
        or not marker_path.is_file()
        or marker_path.is_symlink()
        or _sha256(marker_path.read_bytes()) != receipt.get("drive_marker_sha256")
        or not callback_error_path.is_file()
        or callback_error_path.is_symlink()
        or _sha256(callback_error_path.read_bytes())
        != receipt.get("callback_error_sha256")
        or stored_fresh_marker != fresh_marker
    ):
        raise PostVerificationReviewDriveError(
            "applied post-verification continuation is invalid"
        )
    return receipt


def continued_verification_receipt(
    runtime_root: Path,
    *,
    operation_id: str,
    current_head: str,
    controller_receipt: dict[str, object] | None,
) -> dict[str, object] | None:
    """Return the preserved completion only for its authorized repair descendant."""

    receipt = _applied_receipt(runtime_root, operation_id)
    if receipt is None:
        return None
    controller_path = runtime_root / "pipeline-step-verify.json"
    stored, raw = _regular_json(
        controller_path, "completed scoped verification controller receipt"
    )
    if (
        controller_receipt != stored
        or receipt.get("target_head_sha") != current_head
        or stored.get("status") != "complete"
        or stored.get("operation_id")
        != receipt.get("source_verification_operation_id")
        or stored.get("head_sha") != receipt.get("source_verification_head_sha")
        or _sha256(raw) != receipt.get("controller_receipt_sha256")
    ):
        raise PostVerificationReviewDriveError(
            "continued scoped verification binding drifted"
        )
    return stored


def post_verification_review_marker(
    runtime_root: Path,
    *,
    operation_id: str,
    definition_sha256: str,
) -> dict[str, object] | None:
    """Select a fresh marker while preserving the failed marker bytes."""

    receipt = _applied_receipt(runtime_root, operation_id)
    if receipt is None:
        return None
    if receipt.get("definition_sha256") != definition_sha256:
        raise PostVerificationReviewDriveError(
            "post-verification review definition drifted"
        )
    marker, _raw = _regular_json(runtime_root / MARKER_NAME, "fresh review marker")
    return {**marker, "path": runtime_root / MARKER_NAME}
