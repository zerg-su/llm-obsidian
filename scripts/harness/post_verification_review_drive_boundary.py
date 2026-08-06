"""Exact read-only bindings for post-verification review-drive continuation."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Mapping

from .contracts import EffectOutcome, OperationRecord, OwnedResources
from .liveness import LivenessController, LivenessState
from .store import OperationStore, StoreError


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
HEAD = re.compile(r"[0-9a-f]{40,64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
RECEIPT_NAME = "post-verification-review-drive.json"
MARKER_NAME = "pipeline-review-post-verification-start.json"
RECEIPT_FIELDS = {
    "schema_version",
    "status",
    "operation_id",
    "run_id",
    "active_review_operation_id",
    "authorization_record_id",
    "authorization_record_sha256",
    "definition_sha256",
    "source_verification_operation_id",
    "source_verification_head_sha",
    "source_verification_receipt_sha256",
    "controller_receipt_sha256",
    "failed_drive_head_sha",
    "drive_sha256",
    "drive_marker_sha256",
    "callback_error_sha256",
    "session_sha256",
    "launch_sha256",
    "ready_sha256",
    "provider_events_sha256",
    "gate_source_sha256",
    "progress_source_sha256",
    "archive_bindings",
    "retained_operation_bindings",
    "target_head_sha",
    "target_tree_sha",
    "time_budget_seconds",
    "progress_at",
    "source_revision",
    "target_revision",
    "source_deadline_at",
    "target_deadline_at",
    "source_operation_sha256",
    "target_operation_sha256",
    "source_liveness_sha256",
    "target_liveness_sha256",
    "os_signals_sent",
    "cmux_signals_sent",
    "callback_effects_replayed",
    "provider_effects_replayed",
    "binding_sha256",
}


class PostVerificationReviewDriveError(RuntimeError):
    """The continuation is absent, live-ambiguous, or drifted."""


def canonical(value: object, *, newline: bool = False) -> bytes:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return raw + (b"\n" if newline else b"")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def regular_json(
    path: Path, label: str, *, maximum: int = 1_048_576
) -> tuple[dict[str, object], bytes]:
    if not path.is_file() or path.is_symlink():
        raise PostVerificationReviewDriveError(f"{label} is unavailable")
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PostVerificationReviewDriveError(f"{label} is invalid") from exc
    if not raw or len(raw) > maximum or not isinstance(value, dict):
        raise PostVerificationReviewDriveError(f"{label} is invalid")
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
        raise PostVerificationReviewDriveError("current Git identity is unavailable")
    return result.stdout.strip()


def _current_git(worktree: Path, branch: str) -> tuple[str, str]:
    root = Path(_git(worktree, "rev-parse", "--show-toplevel")).resolve()
    head = _git(worktree, "rev-parse", "HEAD")
    tree = _git(worktree, "rev-parse", "HEAD^{tree}")
    current_branch = _git(worktree, "symbolic-ref", "--short", "HEAD")
    status = _git(worktree, "status", "--porcelain=v1", "--untracked-files=all")
    if (
        root != worktree
        or not HEAD.fullmatch(head)
        or not HEAD.fullmatch(tree)
        or current_branch != branch
        or status
    ):
        raise PostVerificationReviewDriveError(
            "post-verification continuation requires the exact clean task HEAD"
        )
    return head, tree


def _tree_bindings(root: Path, *, exclude: frozenset[str]) -> list[dict[str, str]]:
    if not root.is_dir() or root.is_symlink():
        raise PostVerificationReviewDriveError("quarantine archive is unavailable")
    rows: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise PostVerificationReviewDriveError("quarantine archive is invalid")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative not in exclude:
            rows.append({"path": relative, "sha256": sha256(path.read_bytes())})
    if not rows:
        raise PostVerificationReviewDriveError("quarantine archive is empty")
    return rows


def _provider_identity(
    identity: object,
    *,
    operation_id: str,
    run_id: str,
    resources: OwnedResources,
) -> bool:
    return bool(
        isinstance(identity, dict)
        and identity.get("operation_id") == operation_id
        and identity.get("owner_id") == operation_id
        and identity.get("run_id") == run_id
        and identity.get("process_identity") == resources.process_identity
        and identity.get("surface_id") == resources.surface_id
    )


def _provider_event_binding(
    runtime_root: Path,
    *,
    operation_id: str,
    run_id: str,
    resources: OwnedResources,
) -> str:
    provider_root = runtime_root / "provider-events"
    generations = sorted(
        path for path in provider_root.glob("generation-*") if path.is_dir()
    )
    if len(generations) != 1 or generations[0].is_symlink():
        raise PostVerificationReviewDriveError(
            "dispatch provider event generation is ambiguous"
        )
    event_paths = sorted((generations[0] / "events").glob("*.json"))
    events = [regular_json(path, "dispatch provider event")[0] for path in event_paths]
    if len(events) != 2 or [row.get("kind") for row in events] != [
        "provider-started",
        "input-accepted",
    ]:
        raise PostVerificationReviewDriveError(
            "dispatch provider event boundary is not exact"
        )
    if any(
        not _provider_identity(
            row.get("identity"),
            operation_id=operation_id,
            run_id=run_id,
            resources=resources,
        )
        for row in events
    ):
        raise PostVerificationReviewDriveError(
            "dispatch provider event identity drifted"
        )
    delivery, _raw = regular_json(
        generations[0] / "delivery" / "delivery-state.json",
        "dispatch provider delivery state",
    )
    cursor = delivery.get("cursor")
    exact_cursor = bool(
        isinstance(cursor, dict)
        and cursor.get("last_sequence") == 2
        and cursor.get("provider_started") is True
        and cursor.get("input_accepted") is True
        and cursor.get("result_published") is False
        and cursor.get("process_exited") is False
        and cursor.get("resource_closed") is False
        and cursor.get("event_gap") is False
    )
    if (
        delivery.get("send_status") != "accepted"
        or delivery.get("send_attempts") != 1
        or delivery.get("callback_submits") != 0
        or not exact_cursor
    ):
        raise PostVerificationReviewDriveError(
            "dispatch provider delivery state drifted"
        )
    digest = hashlib.sha256()
    for path in sorted(item for item in provider_root.rglob("*") if item.is_file()):
        if path.is_symlink():
            raise PostVerificationReviewDriveError(
                "dispatch provider event evidence is invalid"
            )
        digest.update(path.relative_to(provider_root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def review_drive_sha256(runtime_root: Path, gate_raw: bytes, head_sha: str) -> str:
    """Hash one exact review drive without launching or replaying it."""

    digest = hashlib.sha256()
    digest.update(gate_raw)
    digest.update(head_sha.encode())
    callback_root = runtime_root.parents[3] / "review-runtime" / runtime_root.name / "callbacks"
    if callback_root.is_symlink():
        raise PostVerificationReviewDriveError("review callback root is invalid")
    if callback_root.is_dir():
        for callback in sorted(callback_root.rglob(".review-callback.json")):
            if callback.is_symlink() or not callback.is_file():
                raise PostVerificationReviewDriveError(
                    "review callback binding is invalid"
                )
            digest.update(callback.relative_to(callback_root).as_posix().encode())
            digest.update(callback.read_bytes())
    return digest.hexdigest()


def _verification_binding(
    *,
    store: OperationStore,
    operation_id: str,
    runtime_root: Path,
    definition_sha256: str,
    target_head: str,
    worktree: Path,
) -> dict[str, str]:
    controller, controller_raw = regular_json(
        runtime_root / "pipeline-step-verify.json",
        "completed scoped verification controller receipt",
    )
    verification_id = str(controller.get("operation_id") or "")
    child_path = (
        runtime_root / "pipeline-verification" / verification_id / "receipt.json"
    )
    child, child_raw = regular_json(
        child_path, "completed scoped verification receipt"
    )
    evidence = child.get("evidence")
    source_head = str(child.get("head_sha") or "")
    rows_valid = bool(
        isinstance(evidence, list)
        and evidence
        and all(
            isinstance(row, dict)
            and row.get("exit_code") == 0
            and row.get("head_sha") == source_head
            and row.get("profile") == "scoped"
            for row in evidence
        )
    )
    if (
        controller != child
        or child.get("schema_version") not in {1, 2}
        or child.get("status") != "complete"
        or child.get("parent_operation_id") != operation_id
        or child.get("definition_sha256") != definition_sha256
        or child.get("step_id") != "verify"
        or child.get("profile") != "scoped"
        or not SHA256.fullmatch(str(child.get("profile_sha256") or ""))
        or not SHA256.fullmatch(str(child.get("input_sha256") or ""))
        or not HEAD.fullmatch(source_head)
        or not rows_valid
    ):
        raise PostVerificationReviewDriveError(
            "completed scoped verification receipt drifted"
        )
    try:
        child_record = store.read(operation_id, verification_id)
    except StoreError as exc:
        raise PostVerificationReviewDriveError(
            "completed scoped verification operation is unavailable"
        ) from exc
    if (
        child_record.state != "complete"
        or child_record.resources != OwnedResources()
        or child_record.pending_effect
        or child_record.spec.parent_operation_id != operation_id
        or child_record.spec.kind != "pipeline-verify"
    ):
        raise PostVerificationReviewDriveError(
            "completed scoped verification operation drifted"
        )
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", source_head, target_head],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    if ancestor.returncode != 0:
        raise PostVerificationReviewDriveError(
            "completed scoped verification is not an ancestor of the repair"
        )
    assert isinstance(evidence, list)
    for row in evidence:
        assert isinstance(row, dict)
        pointer = Path(str(row.get("output_pointer") or ""))
        output = (runtime_root / pointer).resolve()
        evidence_root = (runtime_root / "pipeline-verification").resolve()
        if (
            pointer.is_absolute()
            or evidence_root not in output.parents
            or not output.is_file()
            or output.is_symlink()
        ):
            raise PostVerificationReviewDriveError(
                "completed scoped verification output drifted"
            )
    return {
        "source_verification_operation_id": verification_id,
        "source_verification_head_sha": source_head,
        "source_verification_receipt_sha256": sha256(child_raw),
        "controller_receipt_sha256": sha256(controller_raw),
    }


def _retained_bindings(
    *,
    store: OperationStore,
    operation_id: str,
    progress: Mapping[str, object],
) -> list[dict[str, object]]:
    parent_ids = progress.get("cleaned_parents")
    round_ids = progress.get("terminal_rounds")
    receipts = progress.get("retirement_receipts")
    valid_lists = bool(
        isinstance(parent_ids, list)
        and isinstance(round_ids, list)
        and len(parent_ids) == len(set(parent_ids)) == 2
        and len(round_ids) == len(set(round_ids)) == 2
    )
    valid_receipts = bool(
        isinstance(receipts, dict)
        and isinstance(parent_ids, list)
        and set(receipts) == set(parent_ids)
        and all(SHA256.fullmatch(str(value)) for value in receipts.values())
    )
    if progress.get("schema_version") != 2 or not valid_lists or not valid_receipts:
        raise PostVerificationReviewDriveError(
            "quarantine progress identity drifted"
        )
    assert isinstance(parent_ids, list) and isinstance(round_ids, list)
    rows: list[dict[str, object]] = []
    for expected_state, identifiers in (("cancelled", parent_ids), ("complete", round_ids)):
        for raw_id in identifiers:
            identifier = str(raw_id)
            if not IDENTIFIER.fullmatch(identifier):
                raise PostVerificationReviewDriveError(
                    "retained review identity drifted"
                )
            try:
                record = store.read(operation_id, identifier)
            except StoreError as exc:
                raise PostVerificationReviewDriveError(
                    "retained review operation is unavailable"
                ) from exc
            if (
                record.state != expected_state
                or record.resources != OwnedResources()
                or record.pending_effect
            ):
                raise PostVerificationReviewDriveError(
                    "retained review operation drifted"
                )
            path = store._operation_path(operation_id, identifier)
            rows.append(
                {
                    "operation_id": identifier,
                    "state": expected_state,
                    "record_sha256": sha256(path.read_bytes()),
                }
            )
    return sorted(rows, key=lambda row: str(row["operation_id"]))


def _task_binding(
    worktree: Path, store: OperationStore, operation_id: str
) -> tuple[dict[str, object], str, str]:
    meta, _raw = regular_json(worktree / ".task-meta.json", "task metadata")
    vault = Path(str(meta.get("vault_root") or "")).expanduser().resolve()
    if (
        meta.get("version") not in {3, 4}
        or meta.get("task_id") != operation_id
        or meta.get("interaction_policy") != "unattended"
        or Path(str(meta.get("worktree") or "")).expanduser().resolve() != worktree
        or vault / ".vault-meta" / "harness" != store.root.resolve()
    ):
        raise PostVerificationReviewDriveError(
            "task metadata does not bind the continuation"
        )
    head, tree = _current_git(worktree, str(meta.get("branch") or ""))
    return meta, head, tree


def _owned_dispatch(
    store: OperationStore,
    operation_id: str,
    process_adapter: object,
    cmux_adapter: object,
) -> OperationRecord:
    try:
        record = store.read(operation_id, operation_id)
    except StoreError as exc:
        raise PostVerificationReviewDriveError(
            "dispatch operation is unavailable"
        ) from exc
    resources = record.resources
    complete_resources = bool(
        resources.surface_id
        and resources.process_group > 1
        and resources.supervisor_pid > 1
        and SHA256.fullmatch(resources.process_identity)
        and SHA256.fullmatch(resources.supervisor_identity)
    )
    exact_record = bool(
        record.spec.owner_id == operation_id
        and record.spec.operation_id == operation_id
        and record.spec.kind == "dispatch"
        and record.spec.route.profile == "executor"
        and record.effect_id == "start-provider"
        and record.effect_outcome == EffectOutcome.SUCCEEDED
        and not record.pending_effect
        and not record.accepted_callback_id
        and not record.accepted_callback_kind
        and not record.accepted_callback_sha256
        and record.state in {"attention-required", "awaiting-callback"}
    )
    if not exact_record or not complete_resources:
        raise PostVerificationReviewDriveError(
            "dispatch provider ownership is not exact"
        )
    try:
        exact_statuses = getattr(process_adapter, "exact_statuses", None)
        if callable(exact_statuses):
            process_status, supervisor_status = exact_statuses(
                resources.process_group,
                resources.process_identity,
                resources.supervisor_pid,
                resources.supervisor_identity,
            )
        else:
            process_status = process_adapter.process_status(
                resources.process_group, resources.process_identity
            )
            supervisor_status = process_adapter.pid_status(
                resources.supervisor_pid, resources.supervisor_identity
            )
        statuses = {
            process_status,
            supervisor_status,
            cmux_adapter.status(resources.surface_id),
        }
    except Exception as exc:
        raise PostVerificationReviewDriveError(
            "dispatch provider ownership cannot be proven"
        ) from exc
    if statuses != {"alive"}:
        raise PostVerificationReviewDriveError(
            "dispatch provider ownership is not live and exact"
        )
    return record


def _session_binding(
    worktree: Path, runtime_root: Path, record: OperationRecord
) -> tuple[float, dict[str, str]]:
    operation_id = record.spec.operation_id
    session, session_raw = regular_json(runtime_root / "session.json", "runtime session")
    launch, launch_raw = regular_json(runtime_root / "launch.json", "runtime launch")
    time_budget = session.get("time_budget_seconds")
    valid_budget = bool(
        isinstance(time_budget, (int, float))
        and not isinstance(time_budget, bool)
        and math.isfinite(float(time_budget))
        and float(time_budget) > 0
    )
    common_session = bool(
        session.get("schema_version") == 1
        and session.get("operation_id") == operation_id
        and session.get("run_id") == record.run_id
        and session.get("callback_mode") == "task-summary"
        and Path(str(session.get("cwd") or "")).expanduser().resolve() == worktree
        and Path(str(session.get("product_root") or "")).expanduser().resolve()
        == worktree
    )
    common_launch = bool(
        launch.get("schema_version") == 1
        and launch.get("owner_id") == operation_id
        and launch.get("operation_id") == operation_id
        and launch.get("run_id") == record.run_id
        and launch.get("surface_id") == record.resources.surface_id
        and launch.get("callback_mode") == "task-summary"
        and Path(str(launch.get("cwd") or "")).expanduser().resolve() == worktree
        and Path(str(launch.get("product_root") or "")).expanduser().resolve()
        == worktree
    )
    if not valid_budget or not common_session or not common_launch:
        raise PostVerificationReviewDriveError(
            "dispatch provider runtime identity drifted"
        )
    return float(time_budget), {
        "session_sha256": sha256(session_raw),
        "launch_sha256": sha256(launch_raw),
    }


def _ready_liveness_binding(
    runtime_root: Path,
    record: OperationRecord,
    transition_receipt: Mapping[str, object] | None,
) -> tuple[LivenessState, dict[str, str]]:
    resources = record.resources
    ready, ready_raw = regular_json(runtime_root / "ready.json", "runtime ready")
    expected_ready = {
        "schema_version": 1,
        "status": "ready",
        "pid": resources.process_group,
        "process_group": resources.process_group,
        "process_identity": resources.process_identity,
        "supervisor_pid": resources.supervisor_pid,
        "supervisor_identity": resources.supervisor_identity,
    }
    if ready != expected_ready:
        raise PostVerificationReviewDriveError(
            "dispatch provider runtime identity drifted"
        )
    liveness_path = runtime_root / "liveness" / "state.json"
    liveness = LivenessController(runtime_root / "liveness").current_state()
    split_publication = bool(
        transition_receipt is not None
        and transition_receipt.get("status") == "transitioning"
        and liveness_path.is_file()
        and not liveness_path.is_symlink()
        and sha256(liveness_path.read_bytes())
        == transition_receipt.get("source_liveness_sha256")
    )
    if liveness is None or (
        not split_publication
        and (
            liveness.operation_revision != record.revision
            or liveness.operation_state != record.state
        )
    ):
        raise PostVerificationReviewDriveError(
            "dispatch provider liveness identity drifted"
        )
    return liveness, {"ready_sha256": sha256(ready_raw)}


def _drive_binding(
    runtime_root: Path,
    *,
    operation_id: str,
    definition_sha256: str,
    gate_raw: bytes,
    source_head: str,
    continued_drive_sha256: str | None = None,
) -> dict[str, str]:
    callback_error, error_raw = regular_json(
        runtime_root / "callback-error.json", "review drive error latch"
    )
    marker, marker_raw = regular_json(
        runtime_root / "pipeline-review-start.json", "pending review drive marker"
    )
    exact_marker = bool(
        set(marker)
        == {
            "schema_version",
            "operation_id",
            "definition_sha256",
            "drive_sha256",
            "status",
        }
        and marker.get("schema_version") == 1
        and marker.get("operation_id") == operation_id
        and marker.get("definition_sha256") == definition_sha256
        and marker.get("status") == "pending"
        and SHA256.fullmatch(str(marker.get("drive_sha256") or ""))
    )
    calculated_drive_sha256 = review_drive_sha256(
        runtime_root, gate_raw, source_head
    )
    expected_drive_sha256 = continued_drive_sha256 or calculated_drive_sha256
    if (
        callback_error != {"schema_version": 1, "status": "review-drive-failed"}
        or not exact_marker
        or marker.get("drive_sha256") != expected_drive_sha256
    ):
        raise PostVerificationReviewDriveError(
            "pending review drive boundary drifted"
        )
    return {
        "failed_drive_head_sha": source_head,
        "drive_sha256": str(marker["drive_sha256"]),
        "drive_marker_sha256": sha256(marker_raw),
        "callback_error_sha256": sha256(error_raw),
    }


def _gate_binding(
    *,
    store: OperationStore,
    operation_id: str,
    active_review_operation_id: str,
    target_head: str,
) -> tuple[dict[str, object], bytes, list[dict[str, object]], Path, bytes]:
    gate_root = store.root / "review-data" / operation_id / operation_id
    gate, gate_raw = regular_json(gate_root / "review-gate.json", "review gate")
    quarantine = gate.get("drift_quarantine")
    source_gate = gate.get("fresh_reevaluation_used") is not True
    source_valid = bool(
        source_gate
        and gate.get("status") == "attention-required"
        and gate.get("active_review_operation_id") == active_review_operation_id
    )
    fresh_valid = bool(
        not source_gate
        and gate.get("status") in {"fresh-reevaluation", "reviewing", "verifying"}
        and isinstance(gate.get("context"), dict)
        and gate["context"].get("head_sha") == target_head
    )
    if (
        gate.get("schema_version") != 1
        or gate.get("dispatch_operation_id") != operation_id
        or gate.get("owner_id") != operation_id
        or not isinstance(quarantine, dict)
        or quarantine.get("status") != "quarantined"
        or not (source_valid or fresh_valid)
    ):
        raise PostVerificationReviewDriveError("review gate identity drifted")
    pointer = Path(str(quarantine.get("evidence_pointer") or ""))
    evidence_path = (gate_root / pointer).resolve()
    try:
        evidence_path.relative_to(gate_root.resolve())
    except ValueError as exc:
        raise PostVerificationReviewDriveError(
            "quarantine archive escapes the review gate"
        ) from exc
    evidence, evidence_raw = regular_json(evidence_path, "quarantine evidence")
    exact_evidence = bool(
        evidence.get("status") == "quarantined-evidence"
        and evidence.get("operation_id") == operation_id
        and evidence.get("review_operation_id") == active_review_operation_id
        and quarantine.get("evidence_sha256") == sha256(evidence_raw)
        and evidence.get("callback_effects_replayed") == 0
        and evidence.get("provider_effects_replayed") == 0
    )
    if not exact_evidence:
        raise PostVerificationReviewDriveError(
            "quarantine evidence identity drifted"
        )
    archive_root = evidence_path.parent
    progress, progress_raw = regular_json(
        archive_root / "progress.json", "quarantine progress"
    )
    expected_progress = "quarantined" if source_gate else "fresh-review-started"
    if (
        progress.get("status") != expected_progress
        or progress.get("evidence_sha256") != sha256(evidence_raw)
    ):
        raise PostVerificationReviewDriveError(
            "quarantine progress identity drifted"
        )
    retained = _retained_bindings(
        store=store, operation_id=operation_id, progress=progress
    )
    return gate, gate_raw, retained, archive_root, progress_raw


def runtime_bindings(
    *,
    worktree: Path,
    store: OperationStore,
    operation_id: str,
    active_review_operation_id: str,
    authorization_record_id: str,
    authorization_record_sha256: str,
    process_adapter: object,
    cmux_adapter: object,
    transition_receipt: Mapping[str, object] | None = None,
) -> tuple[dict[str, object], OperationRecord, LivenessState, Path]:
    if (
        not IDENTIFIER.fullmatch(operation_id)
        or not IDENTIFIER.fullmatch(active_review_operation_id)
        or not IDENTIFIER.fullmatch(authorization_record_id)
        or not SHA256.fullmatch(authorization_record_sha256)
    ):
        raise PostVerificationReviewDriveError(
            "post-verification authorization identity is invalid"
        )
    meta, target_head, target_tree = _task_binding(worktree, store, operation_id)
    record = _owned_dispatch(store, operation_id, process_adapter, cmux_adapter)
    runtime_root = store.root / "owners" / operation_id / "runtime" / operation_id
    time_budget, session = _session_binding(worktree, runtime_root, record)
    liveness, ready = _ready_liveness_binding(
        runtime_root, record, transition_receipt
    )
    gate, gate_raw, retained, archive_root, progress_raw = _gate_binding(
        store=store,
        operation_id=operation_id,
        active_review_operation_id=active_review_operation_id,
        target_head=target_head,
    )
    source_gate = gate.get("fresh_reevaluation_used") is not True
    definition = str(meta.get("pipeline_policy", {}).get("definition_sha256") or "")
    verification = _verification_binding(
        store=store,
        operation_id=operation_id,
        runtime_root=runtime_root,
        definition_sha256=definition,
        target_head=target_head,
        worktree=worktree,
    )
    drive = _drive_binding(
        runtime_root,
        operation_id=operation_id,
        definition_sha256=definition,
        gate_raw=gate_raw,
        source_head=verification["source_verification_head_sha"],
        continued_drive_sha256=(
            str(transition_receipt.get("drive_sha256") or "")
            if not source_gate and transition_receipt is not None
            else None
        ),
    )
    static: dict[str, object] = {
        "schema_version": 1,
        "operation_id": operation_id,
        "run_id": record.run_id,
        "active_review_operation_id": active_review_operation_id,
        "authorization_record_id": authorization_record_id,
        "authorization_record_sha256": authorization_record_sha256,
        "definition_sha256": definition,
        **verification,
        **drive,
        **session,
        **ready,
        "provider_events_sha256": _provider_event_binding(
            runtime_root,
            operation_id=operation_id,
            run_id=record.run_id,
            resources=record.resources,
        ),
        "gate_source_sha256": sha256(gate_raw) if source_gate else "",
        "progress_source_sha256": sha256(progress_raw) if source_gate else "",
        "archive_bindings": _tree_bindings(
            archive_root, exclude=frozenset({"progress.json"})
        ),
        "retained_operation_bindings": retained,
        "target_head_sha": target_head,
        "target_tree_sha": target_tree,
        "time_budget_seconds": time_budget,
        "os_signals_sent": 0,
        "cmux_signals_sent": 0,
        "callback_effects_replayed": 0,
        "provider_effects_replayed": 0,
    }
    return static, record, liveness, runtime_root
