"""Fail-closed atomic rearm for one bound review-drive attention latch."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from time import time
from typing import Callable, Mapping

from review_resolution import (
    ResolutionError,
    review_transport_identity_sha256,
    validate_resolution,
)
from task_contract import ContractError as TaskContractError
from task_contract import normalize as normalize_task
from wiki_summary_contract import WikiSummaryError, validate_summary_for_task

from .contracts import (
    AttentionReason,
    EffectOutcome,
    OperationRecord,
    operation_record_from_dict,
    to_dict,
)
from .liveness import LivenessController
from .state_machine import transition
from .store import OperationStore, StoreError


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
HEAD = re.compile(r"[0-9a-f]{40,64}\Z")
MAX_FIRST_PARENT_HEADS = 256
RECEIPT_FIELDS = {
    "schema_version",
    "status",
    "operation_id",
    "run_id",
    "attention_revision",
    "target_revision",
    "resume_state",
    "progress_at",
    "time_budget_seconds",
    "source_deadline_at",
    "target_deadline_at",
    "callback_error_sha256",
    "drive_marker_sha256",
    "drive_sha256",
    "failed_drive_head_sha",
    "notification_marker_sha256",
    "notification_packet_sha256",
    "review_packet_sha256",
    "resolution_sha256",
    "summary_sha256",
    "review_gate_sha256",
    "review_identity_sha256",
    "reviewed_head_sha",
    "resolved_head_sha",
    "resolved_tree_sha",
    "source_operation_sha256",
    "target_operation_sha256",
    "source_liveness_sha256",
    "target_liveness_sha256",
    "parent_bindings",
    "binding_sha256",
}


class ReviewDriveRearmError(RuntimeError):
    """The stale review-drive latch is absent, ambiguous, or changed."""


def _canonical(value: object, *, newline: bool = False) -> bytes:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return encoded + (b"\n" if newline else b"")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _regular_json(
    path: Path, label: str, *, maximum: int = 1_048_576
) -> tuple[dict[str, object], bytes]:
    if not path.is_file() or path.is_symlink():
        raise ReviewDriveRearmError(f"{label} is unavailable")
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > maximum:
            raise ValueError
        value = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ReviewDriveRearmError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise ReviewDriveRearmError(f"{label} is invalid")
    return value, raw


def _git(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ReviewDriveRearmError("current Git identity is unavailable")
    return result.stdout.strip()


def _current_git(worktree: Path, branch: str) -> tuple[str, str]:
    resolved_root = Path(_git(worktree, "rev-parse", "--show-toplevel")).resolve()
    head = _git(worktree, "rev-parse", "HEAD")
    tree = _git(worktree, "rev-parse", "HEAD^{tree}")
    current_branch = _git(worktree, "symbolic-ref", "--short", "HEAD")
    status = _git(worktree, "status", "--porcelain=v1", "--untracked-files=all")
    if (
        resolved_root != worktree
        or not HEAD.fullmatch(head)
        or not HEAD.fullmatch(tree)
        or current_branch != branch
        or status
    ):
        raise ReviewDriveRearmError("rearm requires the exact clean task HEAD")
    return head, tree


def _review_callbacks(
    operation_id: str, gate: Mapping[str, object]
) -> tuple[list[dict[str, object]], list[str], str]:
    awaiting = gate.get("awaiting_resolution")
    if not isinstance(awaiting, dict) or len(awaiting) != 2:
        raise ReviewDriveRearmError("review gate retained parents are incomplete")
    callbacks: list[dict[str, object]] = []
    finding_ids: list[str] = []
    reviewed_heads: set[str] = set()
    for axis in sorted(awaiting):
        boundary = awaiting[axis]
        if not isinstance(axis, str) or not isinstance(boundary, dict):
            raise ReviewDriveRearmError("review gate boundary is invalid")
        callback = {
            "axis": axis,
            "round_operation_id": str(boundary.get("round_operation_id") or ""),
            "round_run_id": str(boundary.get("round_run_id") or ""),
            "callback_id": str(boundary.get("callback_id") or ""),
            "callback_sha256": str(boundary.get("callback_sha256") or ""),
        }
        material = boundary.get("material_finding_ids")
        reviewed = str(boundary.get("reviewed_head_sha") or "")
        if (
            boundary.get("review_operation_id") != operation_id
            or any(not value for value in callback.values())
            or not SHA256.fullmatch(str(callback["callback_sha256"]))
            or not isinstance(material, list)
            or not material
            or any(not isinstance(item, str) or not item for item in material)
            or not HEAD.fullmatch(reviewed)
        ):
            raise ReviewDriveRearmError("review gate boundary identity is invalid")
        callbacks.append(callback)
        finding_ids.extend(material)
        reviewed_heads.add(reviewed)
    if (
        len(reviewed_heads) != 1
        or len(finding_ids) != len(set(finding_ids))
        or gate.get("active_review_operation_id") != operation_id
    ):
        raise ReviewDriveRearmError("active review identity is ambiguous")
    try:
        identity = review_transport_identity_sha256(operation_id, callbacks)
    except ResolutionError as exc:
        raise ReviewDriveRearmError("active review identity is invalid") from exc
    return callbacks, finding_ids, next(iter(reviewed_heads))


def _parent_bindings(
    store: OperationStore,
    owner_id: str,
    gate: Mapping[str, object],
) -> list[dict[str, object]]:
    lanes = gate.get("lanes")
    awaiting = gate.get("awaiting_resolution")
    if (
        not isinstance(lanes, list)
        or len(lanes) != 2
        or not isinstance(awaiting, dict)
    ):
        raise ReviewDriveRearmError("review gate retained parents are incomplete")
    result: list[dict[str, object]] = []
    axes: set[str] = set()
    for raw_lane in lanes:
        if not isinstance(raw_lane, dict):
            raise ReviewDriveRearmError("review parent binding is invalid")
        axis = str(raw_lane.get("axis") or "")
        operation_id = str(raw_lane.get("operation_id") or "")
        run_id = str(raw_lane.get("run_id") or "")
        lane_id = str(raw_lane.get("lane_id") or "")
        surface_id = str(raw_lane.get("surface_id") or "")
        if (
            axis in axes
            or axis not in awaiting
            or raw_lane.get("state") != "awaiting-callback"
            or raw_lane.get("verification_iteration") != 0
            or not all((operation_id, run_id, lane_id, surface_id))
            or not str(raw_lane.get("checkpoint") or "")
            or not SHA256.fullmatch(str(raw_lane.get("checkpoint_sha256") or ""))
        ):
            raise ReviewDriveRearmError("review parent binding is invalid")
        try:
            parent = store.read(owner_id, operation_id)
        except StoreError as exc:
            raise ReviewDriveRearmError("retained review parent is unavailable") from exc
        parent_path = store._operation_path(owner_id, operation_id)
        if (
            parent.spec.owner_id != owner_id
            or parent.spec.operation_id != operation_id
            or parent.run_id != run_id
            or parent.lane_id != lane_id
            or parent.state != "awaiting-callback"
            or parent.resources.surface_id != surface_id
            or not parent.resources.process_identity
            or not parent.resources.supervisor_identity
            or parent.pending_effect
            or parent.effect_id != "start-provider"
            or parent.effect_outcome != EffectOutcome.SUCCEEDED
            or parent.accepted_callback_id
            or parent.accepted_callback_kind
            or parent.accepted_callback_sha256
        ):
            raise ReviewDriveRearmError("retained review parent identity drifted")
        raw = parent_path.read_bytes()
        result.append(
            {
                "axis": axis,
                "operation_id": operation_id,
                "run_id": run_id,
                "lane_id": lane_id,
                "surface_id": surface_id,
                "revision": parent.revision,
                "record_sha256": _sha256(raw),
            }
        )
        axes.add(axis)
    if axes != set(awaiting):
        raise ReviewDriveRearmError("review parent axes changed")
    return sorted(result, key=lambda item: str(item["axis"]))


def _drive_sha256(vault: Path, operation_id: str, gate_raw: bytes, head: str) -> str:
    digest = hashlib.sha256()
    digest.update(gate_raw)
    digest.update(head.encode())
    callback_root = (
        vault
        / ".vault-meta"
        / "harness"
        / "review-runtime"
        / operation_id
        / "callbacks"
    )
    if callback_root.is_symlink():
        raise ReviewDriveRearmError("review callback root cannot be a symlink")
    if callback_root.is_dir():
        for callback in sorted(callback_root.rglob(".review-callback.json")):
            if callback.is_symlink() or not callback.is_file():
                raise ReviewDriveRearmError("review callback binding is invalid")
            digest.update(callback.relative_to(callback_root).as_posix().encode())
            digest.update(callback.read_bytes())
    return digest.hexdigest()


def _first_parent_heads(
    worktree: Path, reviewed_head: str, current_head: str
) -> tuple[str, ...]:
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", reviewed_head, current_head],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    if ancestor.returncode != 0:
        raise ReviewDriveRearmError(
            "reviewed HEAD is not an ancestor of the current resolution"
        )
    descendants = _git(
        worktree,
        "rev-list",
        "--first-parent",
        f"--max-count={MAX_FIRST_PARENT_HEADS}",
        f"{reviewed_head}..{current_head}",
    ).splitlines()
    if (
        not descendants
        or len(descendants) >= MAX_FIRST_PARENT_HEADS
        or descendants[0] != current_head
        or any(not HEAD.fullmatch(item) for item in descendants)
    ):
        raise ReviewDriveRearmError(
            "reviewed-to-current first-parent ancestry is invalid or unbounded"
        )
    oldest_parent = _git(worktree, "rev-parse", f"{descendants[-1]}^1")
    heads = (reviewed_head, *reversed(descendants))
    if (
        oldest_parent != reviewed_head
        or heads[-1] != current_head
        or len(heads) != len(set(heads))
    ):
        raise ReviewDriveRearmError(
            "reviewed HEAD is not on the exact current first-parent ancestry"
        )
    return heads


def _failed_drive_head(
    worktree: Path,
    reviewed_head: str,
    current_head: str,
    marker_sha256: str,
    drive_digest: Callable[[str], str],
) -> str:
    matches: list[str] = []
    for candidate in _first_parent_heads(worktree, reviewed_head, current_head):
        digest = drive_digest(candidate)
        if not SHA256.fullmatch(digest):
            raise ReviewDriveRearmError("candidate review drive digest is invalid")
        if digest == marker_sha256:
            matches.append(candidate)
    if len(matches) != 1:
        raise ReviewDriveRearmError(
            "failed review drive must match exactly one first-parent HEAD"
        )
    return matches[0]


def _static_bindings(
    *,
    worktree: Path,
    vault: Path,
    store: OperationStore,
    owner_id: str,
    operation_id: str,
    meta: Mapping[str, object],
    runtime_root: Path,
    head: str,
    tree: str,
    drive_digest: Callable[[str], str] | None,
) -> dict[str, object]:
    gate_path = store.root / "review-data" / operation_id / operation_id / "review-gate.json"
    gate, gate_raw = _regular_json(gate_path, "review gate")
    context = gate.get("context")
    policy = gate.get("policy")
    review_policy = meta.get("review_policy")
    if (
        gate.get("schema_version") != 1
        or gate.get("dispatch_operation_id") != operation_id
        or gate.get("owner_id") != owner_id
        or gate.get("status") != "awaiting-resolution"
        or gate.get("product_root") != str(worktree)
        or gate.get("continuation_effects") != {}
        or gate.get("resolution_evidence") != {}
        or not isinstance(context, dict)
        or not isinstance(policy, dict)
        or not isinstance(review_policy, Mapping)
        or context.get("verification_profile")
        != review_policy.get("verification_profile")
        or context.get("verification_profile_sha256")
        != review_policy.get("verification_profile_sha256")
        or policy.get("depth") != review_policy.get("mode")
        or policy.get("cross_model") != review_policy.get("cross_model")
        or policy.get("runtime") != review_policy.get("runtime")
        or policy.get("model") != review_policy.get("model")
        or policy.get("effort") != review_policy.get("effort")
        or policy.get("max_verify_iterations")
        != review_policy.get("max_verify_iterations")
    ):
        raise ReviewDriveRearmError("active review gate identity drifted")
    callbacks, finding_ids, reviewed_head = _review_callbacks(operation_id, gate)
    if context.get("head_sha") != reviewed_head or reviewed_head == head:
        raise ReviewDriveRearmError("reviewed HEAD binding is stale")
    try:
        review_identity = review_transport_identity_sha256(operation_id, callbacks)
    except ResolutionError as exc:
        raise ReviewDriveRearmError("active review identity is invalid") from exc
    parents = _parent_bindings(store, owner_id, gate)

    review_packet, review_raw = _regular_json(
        worktree / ".task-review.json", "review packet"
    )
    if (
        review_packet.get("schema_version") != 1
        or review_packet.get("operation_id") != operation_id
        or review_packet.get("review_operation_id") != operation_id
        or review_packet.get("reviewed_head_sha") != reviewed_head
        or review_packet.get("review_identity_sha256") != review_identity
        or review_packet.get("review_callbacks") != callbacks
        or review_packet.get("material_finding_ids") != finding_ids
    ):
        raise ReviewDriveRearmError("review packet identity drifted")

    resolution, resolution_raw = _regular_json(
        worktree / ".task-review-resolution.json", "review resolution"
    )
    try:
        validate_resolution(
            resolution,
            expected_operation_id=operation_id,
            expected_reviewed_head_sha=reviewed_head,
            expected_resolved_head_sha=head,
            expected_finding_ids=finding_ids,
            expected_review_identity_sha256=review_identity,
        )
    except ResolutionError as exc:
        raise ReviewDriveRearmError("review resolution identity drifted") from exc

    summary, summary_raw = _regular_json(
        worktree / ".task-summary.json", "task summary"
    )
    try:
        validated_summary = validate_summary_for_task(
            summary, meta, allow_missing_session=True, require_schema=True
        )
    except (WikiSummaryError, OSError) as exc:
        raise ReviewDriveRearmError("task summary contract is invalid") from exc
    reap_policy = meta.get("reap_policy")
    if (
        not isinstance(reap_policy, Mapping)
        or validated_summary.get("type") not in reap_policy.get("allowed_types", [])
        or validated_summary.get("title") != reap_policy.get("title")
    ):
        raise ReviewDriveRearmError("task summary handoff identity drifted")

    callback_error, callback_error_raw = _regular_json(
        runtime_root / "callback-error.json", "review drive error"
    )
    if callback_error != {"schema_version": 1, "status": "review-drive-failed"}:
        raise ReviewDriveRearmError("review drive error latch is not exact")
    drive_marker, drive_raw = _regular_json(
        runtime_root / "pipeline-review-start.json", "review drive marker"
    )
    definition = str(meta.get("pipeline_policy", {}).get("definition_sha256") or "")
    marker_sha256 = str(drive_marker.get("drive_sha256") or "")
    if (
        set(drive_marker)
        != {
            "schema_version",
            "operation_id",
            "definition_sha256",
            "drive_sha256",
            "status",
        }
        or drive_marker.get("schema_version") != 1
        or drive_marker.get("operation_id") != operation_id
        or drive_marker.get("definition_sha256") != definition
        or not SHA256.fullmatch(marker_sha256)
        or drive_marker.get("status") != "pending"
    ):
        raise ReviewDriveRearmError("failed review drive binding drifted")
    digest_for_head = drive_digest or (
        lambda candidate: _drive_sha256(
            vault, operation_id, gate_raw, candidate
        )
    )
    failed_drive_head = _failed_drive_head(
        worktree,
        reviewed_head,
        head,
        marker_sha256,
        digest_for_head,
    )
    notification, notification_raw = _regular_json(
        runtime_root / "pipeline-review-resolution-notify.json",
        "review resolution notification",
    )
    packet_sha256 = _sha256(_canonical(review_packet))
    if (
        set(notification)
        != {
            "schema_version",
            "operation_id",
            "packet_sha256",
            "reviewed_head_sha",
            "summary_sha256",
            "status",
        }
        or notification.get("schema_version") != 1
        or notification.get("operation_id") != operation_id
        or notification.get("packet_sha256") != packet_sha256
        or notification.get("reviewed_head_sha") != reviewed_head
        or not SHA256.fullmatch(str(notification.get("summary_sha256") or ""))
        or notification.get("status") != "sent"
    ):
        raise ReviewDriveRearmError("review resolution notification drifted")
    return {
        "callback_error_sha256": _sha256(callback_error_raw),
        "drive_marker_sha256": _sha256(drive_raw),
        "drive_sha256": marker_sha256,
        "failed_drive_head_sha": failed_drive_head,
        "notification_marker_sha256": _sha256(notification_raw),
        "notification_packet_sha256": packet_sha256,
        "review_packet_sha256": _sha256(review_raw),
        "resolution_sha256": _sha256(resolution_raw),
        "summary_sha256": _sha256(summary_raw),
        "review_gate_sha256": _sha256(gate_raw),
        "review_identity_sha256": review_identity,
        "reviewed_head_sha": reviewed_head,
        "resolved_head_sha": head,
        "resolved_tree_sha": tree,
        "parent_bindings": parents,
    }


def _receipt_binding(receipt: Mapping[str, object]) -> str:
    payload = {
        key: value
        for key, value in receipt.items()
        if key not in {"status", "binding_sha256"}
    }
    return _sha256(_canonical(payload))


def _validated_receipt(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    receipt, _raw = _regular_json(path, "review drive rearm receipt")
    if (
        set(receipt) != RECEIPT_FIELDS
        or receipt.get("schema_version") != 1
        or receipt.get("status") not in {"prepared", "applied"}
        or receipt.get("resume_state") != "awaiting-callback"
        or receipt.get("binding_sha256") != _receipt_binding(receipt)
    ):
        raise ReviewDriveRearmError("review drive rearm receipt is invalid")
    for field in (
        "callback_error_sha256",
        "drive_marker_sha256",
        "drive_sha256",
        "notification_marker_sha256",
        "notification_packet_sha256",
        "review_packet_sha256",
        "resolution_sha256",
        "summary_sha256",
        "review_gate_sha256",
        "review_identity_sha256",
        "source_operation_sha256",
        "target_operation_sha256",
        "source_liveness_sha256",
        "target_liveness_sha256",
        "binding_sha256",
    ):
        if not SHA256.fullmatch(str(receipt.get(field) or "")):
            raise ReviewDriveRearmError("review drive rearm receipt digest is invalid")
    for field in (
        "failed_drive_head_sha",
        "reviewed_head_sha",
        "resolved_head_sha",
        "resolved_tree_sha",
    ):
        if not HEAD.fullmatch(str(receipt.get(field) or "")):
            raise ReviewDriveRearmError("review drive rearm HEAD binding is invalid")
    return receipt


def review_marker_path_after_rearm(
    runtime_root: Path, operation_id: str
) -> Path:
    """Select a fresh drive marker while retaining the failed marker bytes."""

    original = runtime_root / "pipeline-review-start.json"
    receipt = _validated_receipt(runtime_root / "review-drive-rearm.json")
    if receipt is None:
        return original
    if (
        receipt.get("status") != "applied"
        or receipt.get("operation_id") != operation_id
        or not original.is_file()
        or original.is_symlink()
        or _sha256(original.read_bytes()) != receipt.get("drive_marker_sha256")
    ):
        raise ReviewDriveRearmError(
            "applied review drive rearm no longer binds its failed marker"
        )
    return runtime_root / "pipeline-review-rearm-start.json"


def _source_operation(record: OperationRecord) -> bool:
    return bool(
        record.state == "attention-required"
        and record.resume_state == "awaiting-callback"
        and record.attention_reason == AttentionReason.ATTENTION_REQUIRED
        and not record.pending_effect
    )


def _target_operation(record: OperationRecord, receipt: Mapping[str, object]) -> bool:
    return bool(
        record.state == "awaiting-callback"
        and record.revision == receipt.get("target_revision")
        and record.run_id == receipt.get("run_id")
        and record.deadline_at == receipt.get("target_deadline_at")
        and record.attention_reason is None
        and not record.resume_state
        and not record.pending_effect
    )


def _validate_static_receipt(
    receipt: Mapping[str, object], static: Mapping[str, object]
) -> None:
    for key, value in static.items():
        if receipt.get(key) != value:
            raise ReviewDriveRearmError(f"review drive rearm {key} drifted")


def _locked_rearm(
    *,
    root: Path,
    vault: Path,
    store: OperationStore,
    operation_id: str,
    run_id: str,
    runtime_root: Path,
    meta: Mapping[str, object],
    head: str,
    tree: str,
    time_budget: float,
    observed: float,
    fault: Callable[[str], None],
    drive_digest: Callable[[str], str] | None,
) -> dict[str, object]:
    liveness = LivenessController(runtime_root / "liveness")
    receipt_path = runtime_root / "review-drive-rearm.json"
    with store.locked(operation_id):
        with liveness._locked():
            try:
                record = store.read(operation_id, operation_id)
            except StoreError as exc:
                raise ReviewDriveRearmError("root operation is unavailable") from exc
            if (
                record.spec.owner_id != operation_id
                or record.spec.operation_id != operation_id
                or record.run_id != run_id
                or record.spec.contract_sha256
                != meta.get("pipeline_policy", {}).get("definition_sha256")
                or record.accepted_callback_id
            ):
                raise ReviewDriveRearmError("root operation identity drifted")
            current_liveness = liveness._state()
            if current_liveness is None:
                raise ReviewDriveRearmError("root liveness state is unavailable")
            static = _static_bindings(
                worktree=root,
                vault=vault,
                store=store,
                owner_id=operation_id,
                operation_id=operation_id,
                meta=meta,
                runtime_root=runtime_root,
                head=head,
                tree=tree,
                drive_digest=drive_digest,
            )
            receipt = _validated_receipt(receipt_path)
            operation_raw = store._operation_path(operation_id, operation_id).read_bytes()
            liveness_raw = (runtime_root / "liveness" / "state.json").read_bytes()
            operation_sha256 = _sha256(operation_raw)
            liveness_sha256 = _sha256(liveness_raw)

            if receipt is None:
                if (
                    not _source_operation(record)
                    or current_liveness.operation_revision != record.revision
                    or current_liveness.operation_state != record.state
                ):
                    raise ReviewDriveRearmError("root attention latch is not exact")
                target, changed = transition(record, "awaiting-callback")
                if not changed.changed:
                    raise ReviewDriveRearmError("root attention latch did not transition")
                target = replace(target, deadline_at=observed + time_budget)
                target_liveness = replace(
                    current_liveness,
                    last_progress_at=observed,
                    operation_revision=target.revision,
                    operation_state=target.state,
                    nudge_count=0,
                    restart_count=0,
                )
                receipt = {
                    "schema_version": 1,
                    "status": "prepared",
                    "operation_id": operation_id,
                    "run_id": run_id,
                    "attention_revision": record.revision,
                    "target_revision": target.revision,
                    "resume_state": "awaiting-callback",
                    "progress_at": observed,
                    "time_budget_seconds": time_budget,
                    "source_deadline_at": record.deadline_at,
                    "target_deadline_at": target.deadline_at,
                    **static,
                    "source_operation_sha256": operation_sha256,
                    "target_operation_sha256": _sha256(
                        _canonical(to_dict(target), newline=True)
                    ),
                    "source_liveness_sha256": liveness_sha256,
                    "target_liveness_sha256": _sha256(
                        _canonical(to_dict(target_liveness), newline=True)
                    ),
                }
                receipt["binding_sha256"] = _receipt_binding(receipt)
                OperationStore._write(receipt_path, receipt)
                fault("prepared")
            else:
                _validate_static_receipt(receipt, static)
                if (
                    receipt.get("operation_id") != operation_id
                    or receipt.get("run_id") != run_id
                    or receipt.get("time_budget_seconds") != time_budget
                    or receipt.get("resolved_head_sha") != head
                    or receipt.get("resolved_tree_sha") != tree
                ):
                    raise ReviewDriveRearmError("review drive rearm identity drifted")

            source_operation = operation_sha256 == receipt["source_operation_sha256"]
            target_operation = operation_sha256 == receipt["target_operation_sha256"]
            source_live = liveness_sha256 == receipt["source_liveness_sha256"]
            target_live = liveness_sha256 == receipt["target_liveness_sha256"]
            if (
                receipt["status"] == "applied"
                and target_operation
                and _target_operation(record, receipt)
                and current_liveness.operation_revision == receipt["target_revision"]
                and current_liveness.operation_state == "awaiting-callback"
                and current_liveness.last_progress_at >= receipt["progress_at"]
            ):
                return dict(receipt)
            if not (source_operation or target_operation) or not (
                source_live or target_live
            ):
                raise ReviewDriveRearmError("review drive rearm state drifted")
            if target_live and source_operation:
                raise ReviewDriveRearmError("review drive rearm publication order drifted")
            if source_operation:
                if (
                    not _source_operation(record)
                    or record.revision != receipt["attention_revision"]
                    or record.deadline_at != receipt["source_deadline_at"]
                ):
                    raise ReviewDriveRearmError("source attention latch drifted")
                target, _changed = transition(record, "awaiting-callback")
                target = replace(target, deadline_at=float(receipt["target_deadline_at"]))
                if _sha256(_canonical(to_dict(target), newline=True)) != receipt[
                    "target_operation_sha256"
                ]:
                    raise ReviewDriveRearmError("target operation binding drifted")
                store._write(
                    store._operation_path(operation_id, operation_id), to_dict(target)
                )
                record = target
                fault("operation-written")
            elif not _target_operation(record, receipt):
                raise ReviewDriveRearmError("target operation identity drifted")

            if source_live:
                if (
                    current_liveness.operation_revision
                    != receipt["attention_revision"]
                    or current_liveness.operation_state != "attention-required"
                ):
                    raise ReviewDriveRearmError("source liveness latch drifted")
                target_liveness = replace(
                    current_liveness,
                    last_progress_at=float(receipt["progress_at"]),
                    operation_revision=int(receipt["target_revision"]),
                    operation_state="awaiting-callback",
                    nudge_count=0,
                    restart_count=0,
                )
                if _sha256(_canonical(to_dict(target_liveness), newline=True)) != receipt[
                    "target_liveness_sha256"
                ]:
                    raise ReviewDriveRearmError("target liveness binding drifted")
                LivenessController._write(
                    runtime_root / "liveness" / "state.json",
                    to_dict(target_liveness),
                )
                fault("liveness-written")
            elif (
                current_liveness.operation_revision != receipt["target_revision"]
                or current_liveness.operation_state != "awaiting-callback"
                or current_liveness.last_progress_at != receipt["progress_at"]
            ):
                raise ReviewDriveRearmError("target liveness identity drifted")

            if receipt["status"] == "prepared":
                receipt = {**receipt, "status": "applied"}
                OperationStore._write(receipt_path, receipt)
                fault("applied")
            return dict(receipt)


def rearm_review_drive(
    worktree: Path | str,
    *,
    now: float | None = None,
    _fault_hook: Callable[[str], None] | None = None,
    _drive_digest: Callable[[str], str] | None = None,
) -> dict[str, object]:
    """Consume one exact failed-drive latch without any provider-facing effect."""

    root = Path(worktree).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ReviewDriveRearmError("task worktree is unavailable")
    meta, _meta_raw = _regular_json(root / ".task-meta.json", "task metadata")
    try:
        policy = normalize_task(meta)
    except (TaskContractError, OSError) as exc:
        raise ReviewDriveRearmError("task metadata contract is invalid") from exc
    operation_id = str(meta.get("task_id") or "")
    vault = Path(str(meta.get("vault_root") or "")).expanduser().resolve()
    if (
        policy.get("version") != 4
        or meta.get("interaction_policy") != "unattended"
        or Path(str(meta.get("worktree") or "")).expanduser().resolve() != root
        or not vault.is_absolute()
        or not (vault / "wiki").is_dir()
    ):
        raise ReviewDriveRearmError("task metadata does not bind this worktree")
    head, tree = _current_git(root, str(meta.get("branch") or ""))
    store = OperationStore(vault / ".vault-meta" / "harness")
    runtime_root = store.root / "owners" / operation_id / "runtime" / operation_id
    session, _session_raw = _regular_json(runtime_root / "session.json", "runtime session")
    run_id = str(session.get("run_id") or "")
    time_budget = session.get("time_budget_seconds")
    if (
        session.get("schema_version") != 1
        or session.get("operation_id") != operation_id
        or Path(str(session.get("cwd") or "")).expanduser().resolve() != root
        or Path(str(session.get("product_root") or "")).expanduser().resolve()
        != root
        or not run_id
        or not isinstance(time_budget, (int, float))
        or isinstance(time_budget, bool)
        or not math.isfinite(float(time_budget))
        or time_budget <= 0
    ):
        raise ReviewDriveRearmError("runtime session binding is invalid")
    observed = time() if now is None else float(now)
    if not math.isfinite(observed) or observed <= 0:
        raise ReviewDriveRearmError("rearm progress time is invalid")

    fault = _fault_hook or (lambda _stage: None)
    return _locked_rearm(
        root=root,
        vault=vault,
        store=store,
        operation_id=operation_id,
        run_id=run_id,
        runtime_root=runtime_root,
        meta=meta,
        head=head,
        tree=tree,
        time_budget=float(time_budget),
        observed=observed,
        fault=fault,
        drive_digest=_drive_digest,
    )
