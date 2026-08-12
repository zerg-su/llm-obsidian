#!/usr/bin/env python3
"""Immutable coordinator-decision records with one pointer-only latest marker."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


MARKER_NAME = ".task-needs-attention.json"
RECORDS_NAME = ".task-escalation-records"
POINTER_FIELDS = {"schema_version", "record_id", "record_sha256"}
RECORD_FIELDS = {
    "schema_version",
    "record_id",
    "record_type",
    "origin",
    "previous",
    "occurred_at",
    "payload",
}
RECORD_TYPES = {"raise", "resolution", "amendment", "delivery-failure"}
RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_JSON_BYTES = 64 * 1024
MAX_CHAIN_RECORDS = 4096
_UNSET = object()


class EscalationRecordError(ValueError):
    """Raised when durable coordinator-decision evidence is not exact."""


@dataclass(frozen=True)
class DecisionRecord:
    record_id: str
    record_type: str
    payload: dict[str, Any]
    sha256: str
    path: Path
    previous_record_id: str = ""
    previous_record_sha256: str = ""


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink():
        raise EscalationRecordError(f"{label} cannot be a symlink")
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise EscalationRecordError(f"missing {label}") from exc
    except OSError as exc:
        raise EscalationRecordError(f"cannot read {label}") from exc
    if len(raw) > MAX_JSON_BYTES:
        raise EscalationRecordError(f"{label} exceeds its size bound")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EscalationRecordError(f"invalid JSON in {label}") from exc
    if not isinstance(value, dict):
        raise EscalationRecordError(f"{label} must contain an object")
    return value, raw


def _task_origin(worktree: Path) -> dict[str, str]:
    meta, _ = _read_object(worktree / ".task-meta.json", "task metadata")
    recorded_worktree = str(meta.get("worktree") or "").strip()
    if recorded_worktree and Path(recorded_worktree).expanduser().resolve() != worktree:
        raise EscalationRecordError("task metadata worktree origin is stale")
    return {
        "worktree": str(worktree),
        "project_id": str(meta.get("project_id") or ""),
        "task_id": str(meta.get("task_id") or ""),
        "origin_session": str(meta.get("origin_session") or ""),
    }


def _valid_record_id(value: object) -> str:
    record_id = str(value or "")
    if not RECORD_ID.fullmatch(record_id):
        raise EscalationRecordError("record identity is invalid")
    return record_id


def _valid_sha256(value: object, label: str) -> str:
    digest = str(value or "")
    if not SHA256.fullmatch(digest):
        raise EscalationRecordError(f"{label} digest is invalid")
    return digest


def record_path(worktree: Path, record_id: str) -> Path:
    root = worktree.expanduser().resolve()
    return _records_root(root) / f"{_valid_record_id(record_id)}.json"


def _records_root(worktree: Path) -> Path:
    records = worktree / RECORDS_NAME
    if records.is_symlink():
        raise EscalationRecordError("decision records directory cannot be a symlink")
    if records.exists() and not records.is_dir():
        raise EscalationRecordError("decision records directory is invalid")
    return records


def _marker_path(worktree: Path) -> Path:
    return worktree / MARKER_NAME


def _require_current_marker_schema(worktree: Path) -> None:
    marker = _marker_path(worktree)
    if not marker.exists() and not marker.is_symlink():
        return
    value, _ = _read_object(marker, "task attention marker")
    if value.get("schema_version") != 2:
        raise EscalationRecordError(
            "task attention marker requires current pointer schema"
        )


def _fsync_directory(path: Path) -> None:
    """Make directory-entry mutations durable before publishing dependants."""

    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _record_from_value(
    worktree: Path,
    path: Path,
    value: dict[str, Any],
    raw: bytes,
    *,
    expected_sha256: str | None = None,
) -> DecisionRecord:
    if set(value) != RECORD_FIELDS or value.get("schema_version") != 1:
        raise EscalationRecordError("decision record schema is invalid")
    record_id = _valid_record_id(value.get("record_id"))
    if path.name != f"{record_id}.json":
        raise EscalationRecordError("record identity does not match its path")
    record_type = str(value.get("record_type") or "")
    if record_type not in RECORD_TYPES:
        raise EscalationRecordError("decision record type is invalid")
    origin = value.get("origin")
    if origin != _task_origin(worktree):
        raise EscalationRecordError("decision record origin does not match this task")
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise EscalationRecordError("decision record payload is invalid")
    occurred_at = value.get("occurred_at")
    if not isinstance(occurred_at, str) or not occurred_at.strip():
        raise EscalationRecordError("decision record time is invalid")
    previous = value.get("previous")
    previous_id = ""
    previous_sha256 = ""
    if previous is not None:
        if not isinstance(previous, dict) or set(previous) != {
            "record_id",
            "record_sha256",
        }:
            raise EscalationRecordError("decision record predecessor is invalid")
        previous_id = _valid_record_id(previous.get("record_id"))
        previous_sha256 = _valid_sha256(
            previous.get("record_sha256"), "predecessor"
        )
    digest = _sha256(raw)
    if expected_sha256 is not None and digest != expected_sha256:
        raise EscalationRecordError("decision record digest does not match its pointer")
    return DecisionRecord(
        record_id,
        record_type,
        payload,
        digest,
        path,
        previous_id,
        previous_sha256,
    )


def _read_record(
    worktree: Path, record_id: str, expected_sha256: str | None = None
) -> DecisionRecord:
    path = record_path(worktree, record_id)
    value, raw = _read_object(path, f"decision record {record_id}")
    return _record_from_value(
        worktree, path, value, raw, expected_sha256=expected_sha256
    )


def _load_marker_latest(worktree: Path) -> DecisionRecord | None:
    """Load the pointer target; callers validate its complete chain."""

    root = worktree.expanduser().resolve()
    marker_path = _marker_path(root)
    if not marker_path.exists() and not marker_path.is_symlink():
        return None
    marker, raw = _read_object(marker_path, "task attention marker")
    if marker.get("schema_version") != 2:
        raise EscalationRecordError(
            "task attention marker requires current pointer schema"
        )
    if set(marker) != POINTER_FIELDS:
        raise EscalationRecordError("latest attention marker is not pointer-only")
    record_id = _valid_record_id(marker.get("record_id"))
    digest = _valid_sha256(marker.get("record_sha256"), "latest record")
    return _read_record(root, record_id, digest)


def load_chain(worktree: Path) -> tuple[DecisionRecord, ...]:
    """Load the complete authoritative chain, oldest record first."""

    root = worktree.expanduser().resolve()
    latest = _load_marker_latest(root)
    if latest is None:
        return ()
    newest_first: list[DecisionRecord] = []
    seen: set[str] = set()
    current = latest
    while True:
        if current.record_id in seen:
            raise EscalationRecordError("decision record chain contains a cycle")
        seen.add(current.record_id)
        newest_first.append(current)
        if len(newest_first) > MAX_CHAIN_RECORDS:
            raise EscalationRecordError("decision record chain exceeds its bound")
        if not current.previous_record_id:
            break
        current = _read_record(
            root,
            current.previous_record_id,
            current.previous_record_sha256,
        )
    newest_first.reverse()
    chain = tuple(newest_first)
    _validate_chain_semantics(root, chain)
    return chain


def load_amendments(worktree: Path) -> tuple[DecisionRecord, ...]:
    """Return every authoritative amendment, rejecting any orphan branch."""

    root = worktree.expanduser().resolve()
    chain = load_chain(root)
    authoritative = tuple(
        record for record in chain if record.record_type == "amendment"
    )
    records = _records_root(root)
    persisted: list[DecisionRecord] = []
    if records.exists():
        for path in sorted(records.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise EscalationRecordError(
                    "immutable decision record inventory is invalid"
                )
            record = _read_record(root, path.stem)
            if record.record_type == "amendment":
                persisted.append(record)
    if {
        (record.record_id, record.sha256) for record in persisted
    } != {
        (record.record_id, record.sha256) for record in authoritative
    }:
        raise EscalationRecordError(
            "authoritative amendment evidence has an immutable sibling fork"
        )
    return authoritative


def load_latest(worktree: Path) -> DecisionRecord | None:
    """Load and validate the complete chain, returning its authoritative head."""

    chain = load_chain(worktree)
    return None if not chain else chain[-1]


def _validate_payload_worktree(worktree: Path, payload: dict[str, Any]) -> None:
    raw = str(payload.get("worktree") or "")
    if not raw or Path(raw).expanduser().resolve() != worktree:
        raise EscalationRecordError("decision payload worktree origin is stale")


def _validate_chain_semantics(
    worktree: Path, chain: tuple[DecisionRecord, ...]
) -> None:
    previous: DecisionRecord | None = None
    for record in chain:
        payload = record.payload
        _validate_payload_worktree(worktree, payload)
        status = str(payload.get("status") or "")
        if record.record_type == "raise":
            if (
                status != "pending"
                or payload.get("id") != record.record_id
                or (
                    previous is not None
                    and previous.payload.get("status")
                    in {"pending", "delivery-failed"}
                )
            ):
                raise EscalationRecordError("raise transition identity is invalid")
        elif record.record_type == "resolution":
            decision = " ".join(str(payload.get("decision") or "").split())
            resolved_at = str(payload.get("resolved_at") or "")
            if (
                previous is None
                or previous.record_type not in {"raise", "delivery-failure"}
                or previous.payload.get("status") not in {
                    "pending",
                    "delivery-failed",
                }
                or status != "resolved"
                or payload.get("id") != previous.payload.get("id")
                or payload.get("resolved_from") != previous.payload.get("status")
                or not decision
                or not resolved_at
                or record.record_id
                != _derived_record_id(
                    "resolution",
                    {
                        "previous_record_sha256": previous.sha256,
                        "decision": decision,
                    },
                )
            ):
                raise EscalationRecordError("resolution transition identity is invalid")
            expected = dict(previous.payload)
            expected.update(
                {
                    "status": "resolved",
                    "resolved_from": previous.payload.get("status"),
                    "decision": decision,
                    "resolved_at": resolved_at,
                }
            )
            if payload != expected:
                raise EscalationRecordError("resolution transition payload is invalid")
        elif record.record_type == "delivery-failure":
            failed_at = str(payload.get("delivery_failed_at") or "")
            if (
                previous is None
                or previous.record_type != "raise"
                or previous.payload.get("status") != "pending"
                or status != "delivery-failed"
                or payload.get("id") != previous.payload.get("id")
                or not failed_at
                or record.record_id
                != _derived_record_id(
                    "delivery-failure",
                    {"previous_record_sha256": previous.sha256},
                )
            ):
                raise EscalationRecordError(
                    "delivery-failure transition identity is invalid"
                )
            expected = dict(previous.payload)
            expected.update(
                {"status": "delivery-failed", "delivery_failed_at": failed_at}
            )
            if payload != expected:
                raise EscalationRecordError(
                    "delivery-failure transition payload is invalid"
                )
        else:
            decision = " ".join(str(payload.get("decision") or "").split())
            stable = {
                "plan_sha256": _valid_sha256(payload.get("plan_sha256"), "plan"),
                "outcome_sha256": _valid_sha256(
                    payload.get("outcome_sha256"), "Outcome"
                ),
                "decision": decision,
            }
            if (
                status != "resolved"
                or payload.get("category") != "amendment"
                or not decision
                or record.record_id != _derived_record_id("amendment", stable)
                or (
                    previous is not None
                    and previous.payload.get("status")
                    in {"pending", "delivery-failed"}
                )
            ):
                raise EscalationRecordError("amendment transition identity is invalid")
        previous = record


def load_attention(worktree: Path) -> dict[str, Any] | None:
    """Return the latest validated full payload for migrated readers."""

    latest = load_latest(worktree)
    return None if latest is None else dict(latest.payload)


def attention_record_sha256(worktree: Path) -> str:
    latest = load_latest(worktree)
    if latest is None:
        raise EscalationRecordError("task has no coordinator decision record")
    return latest.sha256


@contextmanager
def _writer_lock(worktree: Path) -> Iterator[None]:
    records = _records_root(worktree)
    records_existed = records.exists()
    records.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not records_existed:
        _fsync_directory(worktree)
    lock_path = records / ".lock"
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise EscalationRecordError("decision records lock is invalid")
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_immutable(path: Path, value: dict[str, Any]) -> bytes:
    encoded = _canonical_bytes(value)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise EscalationRecordError("immutable decision record cannot be a symlink")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        current = path.read_bytes()
        if current != encoded:
            raise EscalationRecordError("duplicate record identity has changed bytes")
        return current
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return encoded


def _write_pointer(worktree: Path, record: DecisionRecord) -> None:
    path = _marker_path(worktree)
    value = {
        "schema_version": 2,
        "record_id": record.record_id,
        "record_sha256": record.sha256,
    }
    encoded = _canonical_bytes(value)
    if path.is_symlink():
        raise EscalationRecordError("latest attention marker cannot be a symlink")
    if path.exists() and not path.is_file():
        raise EscalationRecordError("latest attention marker is not a regular file")
    if path.is_file() and path.read_bytes() == encoded:
        return
    descriptor, raw_tmp = tempfile.mkstemp(
        prefix=".task-needs-attention.", suffix=".tmp", dir=worktree
    )
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory = os.open(worktree, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        tmp.unlink(missing_ok=True)


def _record_value(
    worktree: Path,
    *,
    record_id: str,
    record_type: str,
    payload: dict[str, Any],
    occurred_at: str,
    previous: DecisionRecord | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "record_id": _valid_record_id(record_id),
        "record_type": record_type,
        "origin": _task_origin(worktree),
        "previous": (
            None
            if previous is None
            else {
                "record_id": previous.record_id,
                "record_sha256": previous.sha256,
            }
        ),
        "occurred_at": occurred_at,
        "payload": payload,
    }


def _persist_record(worktree: Path, value: dict[str, Any]) -> DecisionRecord:
    record_id = _valid_record_id(value.get("record_id"))
    path = record_path(worktree, record_id)
    raw = _write_immutable(path, value)
    _fsync_directory(path.parent)
    return _record_from_value(worktree, path, value, raw)


def _existing_semantic_record(
    worktree: Path,
    record_id: str,
    *,
    record_type: str,
    payload: dict[str, Any],
    ignored_payload_fields: set[str] | None = None,
) -> DecisionRecord | None:
    path = record_path(worktree, record_id)
    if not path.exists():
        return None
    existing = _read_record(worktree, record_id)
    ignored = ignored_payload_fields or set()
    expected_payload = {key: value for key, value in payload.items() if key not in ignored}
    actual_payload = {
        key: value for key, value in existing.payload.items() if key not in ignored
    }
    if existing.record_type != record_type or actual_payload != expected_payload:
        raise EscalationRecordError("duplicate record identity has changed payload")
    return existing


def _require_expected_latest(
    latest: DecisionRecord | None, expected_record_sha256: str | None | object
) -> None:
    if expected_record_sha256 is _UNSET:
        return
    actual = "" if latest is None else latest.sha256
    expected = str(expected_record_sha256 or "")
    if actual != expected:
        raise EscalationRecordError("latest record changed before append")


def _resume_existing_append(
    worktree: Path,
    existing: DecisionRecord,
    latest: DecisionRecord | None,
) -> tuple[DecisionRecord, bool]:
    """Recover one interrupted pointer write without rewinding a live chain."""

    if latest is not None and latest.sha256 == existing.sha256:
        return existing, True
    if latest is not None and any(
        item.sha256 == existing.sha256 for item in load_chain(worktree)
    ):
        return existing, False
    expected_previous = "" if latest is None else latest.sha256
    if existing.previous_record_sha256 != expected_previous:
        raise EscalationRecordError(
            "replayed record predecessor does not match the authoritative chain"
        )
    _write_pointer(worktree, existing)
    return existing, True


def append_raise(
    worktree: Path,
    payload: dict[str, Any],
    *,
    expected_record_sha256: str | None | object = _UNSET,
) -> DecisionRecord:
    """Append one pending escalation before any delivery effect."""

    root = worktree.expanduser().resolve()
    if not isinstance(payload, dict):
        raise EscalationRecordError("raise payload must be an object")
    record_id = _valid_record_id(payload.get("id"))
    if payload.get("status") != "pending":
        raise EscalationRecordError("raise payload must be pending")
    payload_worktree = str(payload.get("worktree") or "")
    if (
        not payload_worktree
        or Path(payload_worktree).expanduser().resolve() != root
    ):
        raise EscalationRecordError("raise payload worktree origin is stale")
    normalized_payload = {**payload, "worktree": str(root)}
    _require_current_marker_schema(root)
    with _writer_lock(root):
        latest = load_latest(root)
        _require_expected_latest(latest, expected_record_sha256)
        existing = _existing_semantic_record(
            root,
            record_id,
            record_type="raise",
            payload=normalized_payload,
            ignored_payload_fields={"raised_at"},
        )
        if existing is not None:
            existing, authoritative = _resume_existing_append(
                root, existing, latest
            )
            if authoritative:
                return existing
            assert latest is not None
            return latest
        if latest is not None and latest.payload.get("status") in {
            "pending",
            "delivery-failed",
        }:
            raise EscalationRecordError("another coordinator escalation is unresolved")
        value = _record_value(
            root,
            record_id=record_id,
            record_type="raise",
            payload=normalized_payload,
            occurred_at=str(normalized_payload.get("raised_at") or _utc_now()),
            previous=latest,
        )
        record = _persist_record(root, value)
        _write_pointer(root, record)
        return record


def _derived_record_id(prefix: str, value: object) -> str:
    return f"{prefix}-{_sha256(_canonical_bytes(value))[:32]}"


def append_resolution(
    worktree: Path,
    decision: str,
    *,
    resolved_at: str | None = None,
    expected_record_sha256: str | None | object = _UNSET,
) -> DecisionRecord:
    """Append one current-schema resolution."""

    root = worktree.expanduser().resolve()
    answer = " ".join(str(decision).split()).strip()
    if not answer:
        raise EscalationRecordError("resolution decision is empty")
    _require_current_marker_schema(root)
    with _writer_lock(root):
        latest = load_latest(root)
        _require_expected_latest(latest, expected_record_sha256)
        if latest is None:
            raise EscalationRecordError("there is no unresolved coordinator escalation")
        if latest.payload.get("status") == "resolved":
            if latest.record_type == "resolution" and latest.payload.get("decision") == answer:
                return latest
            raise EscalationRecordError("there is no unresolved coordinator escalation")
        if latest.payload.get("status") not in {"pending", "delivery-failed"}:
            raise EscalationRecordError("latest coordinator record cannot be resolved")
        payload_worktree = str(latest.payload.get("worktree") or "")
        if (
            not payload_worktree
            or Path(payload_worktree).expanduser().resolve() != root
        ):
            raise EscalationRecordError("attention marker origin is stale")
        record_id = _derived_record_id(
            "resolution",
            {"previous_record_sha256": latest.sha256, "decision": answer},
        )
        payload = dict(latest.payload)
        payload.update(
            {
                "status": "resolved",
                "resolved_from": str(latest.payload.get("status") or ""),
                "decision": answer,
                "resolved_at": resolved_at or _utc_now(),
            }
        )
        existing = _existing_semantic_record(
            root,
            record_id,
            record_type="resolution",
            payload=payload,
            ignored_payload_fields={"resolved_at"},
        )
        if existing is not None:
            existing, authoritative = _resume_existing_append(
                root, existing, latest
            )
            if not authoritative:
                raise EscalationRecordError(
                    "resolution replay is no longer the latest record"
                )
            return existing
        value = _record_value(
            root,
            record_id=record_id,
            record_type="resolution",
            payload=payload,
            occurred_at=str(payload["resolved_at"]),
            previous=latest,
        )
        record = _persist_record(root, value)
        _write_pointer(root, record)
        return record


def append_delivery_failure(
    worktree: Path,
    *,
    failed_at: str | None = None,
    expected_record_sha256: str | None | object = _UNSET,
) -> DecisionRecord:
    """Append the failed delivery transition without mutating its raise record."""

    root = worktree.expanduser().resolve()
    _require_current_marker_schema(root)
    with _writer_lock(root):
        latest = load_latest(root)
        _require_expected_latest(latest, expected_record_sha256)
        if latest is None or latest.payload.get("status") != "pending":
            if latest is not None and latest.payload.get("status") == "delivery-failed":
                return latest
            raise EscalationRecordError("latest escalation is not pending delivery")
        record_id = _derived_record_id(
            "delivery-failure", {"previous_record_sha256": latest.sha256}
        )
        payload = dict(latest.payload)
        payload.update(
            {
                "status": "delivery-failed",
                "delivery_failed_at": failed_at or _utc_now(),
            }
        )
        existing = _existing_semantic_record(
            root,
            record_id,
            record_type="delivery-failure",
            payload=payload,
            ignored_payload_fields={"delivery_failed_at"},
        )
        if existing is not None:
            existing, authoritative = _resume_existing_append(
                root, existing, latest
            )
            if not authoritative:
                raise EscalationRecordError(
                    "delivery-failure replay is no longer the latest record"
                )
            return existing
        value = _record_value(
            root,
            record_id=record_id,
            record_type="delivery-failure",
            payload=payload,
            occurred_at=str(payload["delivery_failed_at"]),
            previous=latest,
        )
        record = _persist_record(root, value)
        _write_pointer(root, record)
        return record


def append_amendment(
    worktree: Path,
    *,
    plan_sha256: str,
    outcome_sha256: str,
    decision: str,
    recorded_at: str | None = None,
    expected_record_sha256: str | None | object = _UNSET,
) -> DecisionRecord:
    """Append one coordinator amendment bound to frozen plan and Outcome bytes."""

    root = worktree.expanduser().resolve()
    plan_digest = _valid_sha256(plan_sha256, "plan")
    outcome_digest = _valid_sha256(outcome_sha256, "Outcome")
    answer = " ".join(str(decision).split()).strip()
    if not answer:
        raise EscalationRecordError("amendment decision is empty")
    stable = {
        "plan_sha256": plan_digest,
        "outcome_sha256": outcome_digest,
        "decision": answer,
    }
    record_id = _derived_record_id("amendment", stable)
    _require_current_marker_schema(root)
    with _writer_lock(root):
        latest = load_latest(root)
        _require_expected_latest(latest, expected_record_sha256)
        payload = {
            "version": 1,
            "id": record_id,
            "status": "resolved",
            "task_name": str(_read_object(root / ".task-meta.json", "task metadata")[0].get("task_name") or "task amendment"),
            "category": "amendment",
            "worktree": str(root),
            **stable,
            "recorded_at": recorded_at or _utc_now(),
        }
        existing = _existing_semantic_record(
            root,
            record_id,
            record_type="amendment",
            payload=payload,
            ignored_payload_fields={"recorded_at"},
        )
        if existing is not None:
            _resume_existing_append(root, existing, latest)
            return existing
        if latest is not None and latest.payload.get("status") in {
            "pending",
            "delivery-failed",
        }:
            raise EscalationRecordError(
                "cannot append an amendment while an escalation is unresolved"
            )
        value = _record_value(
            root,
            record_id=record_id,
            record_type="amendment",
            payload=payload,
            occurred_at=str(payload["recorded_at"]),
            previous=latest,
        )
        record = _persist_record(root, value)
        _write_pointer(root, record)
        return record
