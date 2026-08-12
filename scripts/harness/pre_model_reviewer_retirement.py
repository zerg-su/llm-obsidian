"""Evidence-bound retirement for reviewers that failed before model startup."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .contracts import (
    AttentionReason,
    EffectOutcome,
    OperationRecord,
    OwnedResources,
    TransitionResult,
)
from .review_attempt import ReviewAttempt
from .state_machine import TERMINAL
from .store import OperationStore, StoreError
from .supervisor import OperationSupervisor, SupervisorError


EVIDENCE_NAMES = (
    "session.json",
    "launch.json",
    "ready.json",
    "exit.json",
    "callback-target.json",
)


def review_attempt_records_are_quiescent(
    store: object,
    attempt: ReviewAttempt,
) -> bool:
    """Prove a retry predecessor has no live or accepted provider effect."""

    rows_by_owner: dict[str, list[object]] = {}
    try:
        for lane in attempt.identity.lanes:
            rows = rows_by_owner.setdefault(
                lane.owner_id, list(store.list(lane.owner_id))
            )
            parents = [
                row
                for row in rows
                if row.spec.operation_id == lane.operation_id
            ]
            if len(parents) > 1 or (
                parents and not _quiescent_parent(parents[0], lane)
            ):
                return False
            children = [
                row
                for row in rows
                if row.spec.parent_operation_id == lane.operation_id
            ]
            if len(children) > 1 or any(
                not _quiescent_child(child, lane) for child in children
            ):
                return False
    except (AttributeError, StoreError, TypeError, ValueError):
        return False
    return True


def _quiescent_parent(parent: object, lane: object) -> bool:
    route = parent.spec.route
    return not any(
        (
            parent.spec.owner_id != lane.owner_id,
            parent.lane_id != lane.lane_id,
            parent.run_id != lane.run_id,
            route.runtime != lane.runtime,
            route.model != lane.model,
            route.effort != lane.effort,
            route.profile != lane.profile,
            route.routing_sha256 != lane.routing_sha256,
            parent.state not in TERMINAL,
            parent.resources != OwnedResources(),
            bool(parent.pending_effect),
            parent.effect_outcome
            not in {EffectOutcome.NONE, EffectOutcome.FAILED},
            parent.effect_id not in {"", "start-provider"},
            bool(parent.accepted_callback_id),
            bool(parent.accepted_callback_sha256),
            bool(parent.accepted_callback_kind),
        )
    )


def _quiescent_child(child: object, lane: object) -> bool:
    return not any(
        (
            child.spec.kind != "review-round",
            child.lane_id != lane.lane_id,
            child.state not in TERMINAL,
            child.resources != OwnedResources(),
            bool(child.pending_effect),
            child.effect_outcome
            not in {EffectOutcome.NONE, EffectOutcome.FAILED},
            bool(child.accepted_callback_id),
            bool(child.accepted_callback_sha256),
            bool(child.accepted_callback_kind),
        )
    )


def _regular_json_object(path: Path) -> dict[str, object]:
    """Read one stable regular-file object without following a file symlink."""

    if path.is_symlink() or not path.is_file():
        raise ValueError("runtime evidence is not a regular file")
    first = path.read_bytes()
    second = path.read_bytes()
    if first != second or not first or len(first) > 65_536:
        raise ValueError("runtime evidence changed while reading")
    value = json.loads(first)
    if not isinstance(value, dict):
        raise ValueError("runtime evidence is not an object")
    return value


def _failed_start_shape(
    record: OperationRecord, owner: str, operation_id: str
) -> bool:
    surface_id = record.resources.surface_id
    return all(
        (
            record.spec.owner_id == owner,
            record.spec.operation_id == operation_id,
            record.spec.route.profile == "reviewer-callback",
            record.state == "attention-required",
            record.attention_reason == AttentionReason.PROCESS_START_FAILED,
            record.resume_state == "starting",
            not record.pending_effect,
            record.effect_id == "start-provider",
            record.effect_outcome == EffectOutcome.FAILED,
            bool(surface_id),
            record.resources == OwnedResources(surface_id=surface_id),
            not record.accepted_callback_id,
            not record.accepted_callback_kind,
            not record.accepted_callback_sha256,
        )
    )


def _load_evidence(state_root: Path) -> dict[str, dict[str, object]] | None:
    if state_root.is_symlink() or not state_root.is_dir():
        return None
    try:
        return {
            name: _regular_json_object(state_root / name)
            for name in EVIDENCE_NAMES
        }
    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _absolute_regular_roots(
    session: Mapping[str, object],
) -> tuple[Path, Path] | None:
    raw_cwd = Path(str(session.get("cwd") or "")).expanduser()
    raw_product = Path(str(session.get("product_root") or "")).expanduser()
    if not all(
        (
            raw_cwd.is_absolute(),
            raw_product.is_absolute(),
            not raw_cwd.is_symlink(),
            not raw_product.is_symlink(),
            raw_cwd.is_dir(),
            raw_product.is_dir(),
        )
    ):
        return None
    cwd = raw_cwd.resolve()
    product = raw_product.resolve()
    if str(raw_cwd) != str(cwd) or str(raw_product) != str(product):
        return None
    return cwd, product


def _bound_absent_callback(cwd: Path, pointer: object) -> Path | None:
    if not isinstance(pointer, str) or not pointer or "\\" in pointer:
        return None
    relative = Path(pointer)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        return None
    current = cwd
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink() or not current.is_dir():
            return None
    callback_path = current / relative.parts[-1]
    try:
        callback_path.resolve().relative_to(cwd)
    except (OSError, ValueError):
        return None
    if (
        callback_path.exists()
        or callback_path.is_symlink()
    ):
        return None
    return callback_path


def _evidence_bindings_match(
    *,
    store: OperationStore,
    record: OperationRecord,
    state_root: Path,
    evidence: Mapping[str, Mapping[str, object]],
    cwd: Path,
    product: Path,
    callback_path: Path,
) -> bool:
    session = evidence["session.json"]
    launch = evidence["launch.json"]
    target = evidence["callback-target.json"]
    pointer = target.get("callback_pointer")
    generation = target.get("generation")
    return all(
        (
            session.get("schema_version") == 1,
            session.get("operation_id") == record.spec.operation_id,
            session.get("run_id") == record.run_id,
            session.get("placement") == "workspace",
            session.get("callback_mode", "envelope") == "envelope",
            session.get("callback_pointer") == pointer,
            all(
                isinstance(session.get(field), str) and bool(session.get(field))
                for field in (
                    "workspace_id",
                    "window_id",
                    "workspace_ref",
                    "window_ref",
                    "surface_ref",
                )
            ),
            launch.get("schema_version") == 1,
            launch.get("owner_id") == record.spec.owner_id,
            launch.get("operation_id") == record.spec.operation_id,
            launch.get("run_id") == record.run_id,
            launch.get("runtime") == record.spec.route.runtime,
            launch.get("callback_mode", "envelope") == "envelope",
            launch.get("cwd") == str(cwd),
            launch.get("product_root") == str(product),
            launch.get("surface_id") == record.resources.surface_id,
            launch.get("store_root") == str(store.root.resolve()),
            launch.get("ready_path") == str((state_root / "ready.json").resolve()),
            launch.get("exit_path") == str((state_root / "exit.json").resolve()),
            launch.get("callback_registration")
            == str((state_root / "callback-target.json").resolve()),
            launch.get("callback_pointer") == str(callback_path.resolve()),
            target.get("schema_version") == 1,
            type(generation) is int and generation > 0,
            isinstance(target.get("operation_id"), str)
            and bool(target.get("operation_id")),
            isinstance(target.get("run_id"), str) and bool(target.get("run_id")),
        )
    )


def _failure_receipts_match(
    evidence: Mapping[str, Mapping[str, object]],
) -> bool:
    return (
        evidence["ready.json"] == {"schema_version": 1, "status": "failed"}
        and evidence["exit.json"]
        == {
            "schema_version": 1,
            "status": "review-input-template-invalid",
            "exit_code": 2,
        }
    )


def _failed_child_matches(
    store: OperationStore,
    parent: OperationRecord,
    target: Mapping[str, object],
) -> bool:
    child_id = target.get("operation_id")
    if not isinstance(child_id, str) or not child_id:
        return False
    try:
        child = store.read(parent.spec.owner_id, child_id)
    except StoreError:
        return False
    return all(
        (
            child.spec.owner_id == parent.spec.owner_id,
            child.spec.operation_id == child_id,
            child.spec.kind == "review-round",
            child.spec.parent_operation_id == parent.spec.operation_id,
            child.spec.route.profile == "reviewer-callback",
            child.spec.route == parent.spec.route,
            child.run_id == target.get("run_id"),
            child.lane_id == parent.lane_id,
            child.state == "failed",
            child.resources == OwnedResources(),
            not child.pending_effect,
            not child.effect_id,
            child.effect_outcome == EffectOutcome.NONE,
            not child.accepted_callback_id,
            not child.accepted_callback_kind,
            not child.accepted_callback_sha256,
        )
    )


def _resources_are_missing(
    cmux_adapter: object,
    record: OperationRecord,
    session: Mapping[str, object],
) -> bool:
    try:
        surface_status = str(cmux_adapter.status(record.resources.surface_id))
        workspace_status = str(
            cmux_adapter.workspace_status(
                session["workspace_id"], session["window_id"]
            )
        )
    except Exception:
        return False
    return surface_status == "missing" and workspace_status == "missing"


def retire_failed_reviewer_start(
    store: OperationStore,
    owner: str,
    operation_id: str,
    *,
    cmux_adapter: object,
) -> TransitionResult | None:
    """Retire one exact reviewer with a failed start and no provider effect."""

    record = store.read(owner, operation_id)
    if not _failed_start_shape(record, owner, operation_id):
        return None
    state_root = store.root / "owners" / owner / "runtime" / operation_id
    evidence = _load_evidence(state_root)
    if evidence is None:
        return None
    roots = _absolute_regular_roots(evidence["session.json"])
    if roots is None:
        return None
    cwd, product = roots
    target = evidence["callback-target.json"]
    callback_path = _bound_absent_callback(cwd, target.get("callback_pointer"))
    if callback_path is None:
        return None
    if not all(
        (
            _evidence_bindings_match(
                store=store,
                record=record,
                state_root=state_root,
                evidence=evidence,
                cwd=cwd,
                product=product,
                callback_path=callback_path,
            ),
            _failure_receipts_match(evidence),
            _failed_child_matches(store, record, target),
            _resources_are_missing(cmux_adapter, record, evidence["session.json"]),
        )
    ):
        return None
    try:
        retired = OperationSupervisor(
            store, owner, operation_id
        ).retire_proven_absent_resources(record.resources)
    except SupervisorError:
        return None
    return TransitionResult(
        operation_id,
        record.state,
        retired.state,
        retired.revision,
        retired.revision != record.revision,
        retired.attention_reason,
    )
