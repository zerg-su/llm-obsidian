"""Preserved pre/post state bindings for post-fresh publication recovery."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from harness.contracts import EffectOutcome, OwnedResources
from harness.liveness import LivenessController
from harness.post_verification_review_drive import _validated_receipt
from harness.post_verification_review_drive_boundary import (
    MARKER_NAME,
    RECEIPT_NAME,
    PostVerificationReviewDriveError,
    _current_git,
    _provider_event_binding,
    _tree_bindings,
    regular_json,
    sha256,
)
from harness.store import OperationStore, StoreError
from task_review_drift_contract import PostFreshPublicationSyncAuthorization


def _same_file(path: Path, expected_sha256: object, label: str) -> None:
    if (
        not path.is_file()
        or path.is_symlink()
        or sha256(path.read_bytes()) != expected_sha256
    ):
        raise PostVerificationReviewDriveError(f"{label} drifted")


def preserved_continuation(
    *,
    worktree: Path,
    store: OperationStore,
    operation_id: str,
    authorization: PostFreshPublicationSyncAuthorization,
    receipt: Mapping[str, object],
) -> tuple[dict[str, object], Path]:
    """Validate immutable continuation inputs without probing live resources."""

    continuation = authorization.continuation
    runtime_root = store.root / "owners" / operation_id / "runtime" / operation_id
    if (
        receipt.get("status") not in {"prepared", "applied"}
        or receipt.get("operation_id") != operation_id
        or receipt.get("active_review_operation_id")
        != continuation.active_review_operation_id
        or receipt.get("authorization_record_id")
        != continuation.authorization_record_id
        or receipt.get("authorization_record_sha256")
        != continuation.authorization_record_sha256
    ):
        raise PostVerificationReviewDriveError(
            "prepared continuation authorization drifted"
        )
    meta, _meta_raw = regular_json(worktree / ".task-meta.json", "task metadata")
    if (
        meta.get("task_id") != operation_id
        or Path(str(meta.get("worktree") or "")).resolve() != worktree
        or Path(str(meta.get("vault_root") or "")).resolve()
        / ".vault-meta"
        / "harness"
        != store.root.resolve()
    ):
        raise PostVerificationReviewDriveError("task identity drifted")
    head, tree = _current_git(worktree, str(meta.get("branch") or ""))
    if head != receipt.get("target_head_sha") or tree != receipt.get("target_tree_sha"):
        raise PostVerificationReviewDriveError("repair HEAD drifted")
    try:
        root_record = store.read(operation_id, operation_id)
    except StoreError as exc:
        raise PostVerificationReviewDriveError(
            "dispatch operation is unavailable"
        ) from exc
    live = LivenessController(runtime_root / "liveness").current_state()
    expected_root_state = (
        "awaiting-callback"
        if receipt.get("status") == "applied"
        else "attention-required"
    )
    if (
        root_record.state != expected_root_state
        or (
            receipt.get("status") == "prepared"
            and root_record.resume_state != "awaiting-callback"
        )
        or root_record.pending_effect
        or root_record.effect_id != "start-provider"
        or root_record.effect_outcome != EffectOutcome.SUCCEEDED
        or root_record.accepted_callback_id
        or live is None
        or live.operation_state != root_record.state
        or live.operation_revision != root_record.revision
    ):
        raise PostVerificationReviewDriveError(
            "dispatch continuation state drifted"
        )
    for name, field in (
        ("session.json", "session_sha256"),
        ("launch.json", "launch_sha256"),
        ("ready.json", "ready_sha256"),
        ("pipeline-review-start.json", "drive_marker_sha256"),
        ("callback-error.json", "callback_error_sha256"),
        ("pipeline-step-verify.json", "controller_receipt_sha256"),
    ):
        _same_file(runtime_root / name, receipt.get(field), name)
    if (
        _provider_event_binding(
            runtime_root,
            operation_id=operation_id,
            run_id=root_record.run_id,
            resources=root_record.resources,
        )
        != receipt.get("provider_events_sha256")
    ):
        raise PostVerificationReviewDriveError("dispatch provider effects drifted")
    verification_path = (
        runtime_root
        / "pipeline-verification"
        / str(receipt.get("source_verification_operation_id") or "")
        / "receipt.json"
    )
    _same_file(
        verification_path,
        receipt.get("source_verification_receipt_sha256"),
        "verification receipt",
    )
    gate_root = store.root / "review-data" / operation_id / operation_id
    gate, _gate_raw = regular_json(gate_root / "review-gate.json", "fresh review gate")
    quarantine = gate.get("drift_quarantine")
    if not isinstance(quarantine, dict):
        raise PostVerificationReviewDriveError("quarantine binding drifted")
    archive = (
        gate_root / Path(str(quarantine.get("evidence_pointer") or ""))
    ).resolve().parent
    if _tree_bindings(archive, exclude=frozenset({"progress.json"})) != receipt.get(
        "archive_bindings"
    ):
        raise PostVerificationReviewDriveError("quarantine archive drifted")
    for binding in receipt.get("retained_operation_bindings", []):
        if not isinstance(binding, dict):
            raise PostVerificationReviewDriveError("retained binding drifted")
        retained_id = str(binding.get("operation_id") or "")
        try:
            retained = store.read(operation_id, retained_id)
        except StoreError as exc:
            raise PostVerificationReviewDriveError(
                "retained operation is unavailable"
            ) from exc
        if (
            retained.state != binding.get("state")
            or retained.resources != OwnedResources()
            or retained.pending_effect
            or sha256(store._operation_path(operation_id, retained_id).read_bytes())
            != binding.get("record_sha256")
        ):
            raise PostVerificationReviewDriveError("retained operation drifted")
    return gate, runtime_root


def applied_publication(
    *,
    runtime_root: Path,
    operation_id: str,
    authorization: PostFreshPublicationSyncAuthorization,
    sync: Mapping[str, object],
) -> bool:
    """Validate a completed transaction without constraining later callbacks."""

    if sync.get("status") != "applied":
        return False
    if (
        sync.get("operation_id") != operation_id
        or sync.get("authorization_record_id")
        != authorization.authorization_record_id
        or sync.get("authorization_record_sha256")
        != authorization.authorization_record_sha256
    ):
        raise PostVerificationReviewDriveError(
            "applied post-fresh publication authorization drifted"
        )
    continuation = _validated_receipt(runtime_root / RECEIPT_NAME)
    _same_file(
        runtime_root / MARKER_NAME,
        sync.get("fresh_marker_sha256"),
        "fresh review marker",
    )
    if continuation is None or continuation.get("status") != "applied":
        raise PostVerificationReviewDriveError(
            "applied continuation receipt drifted"
        )
    return True
