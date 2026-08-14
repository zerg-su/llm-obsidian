"""Immutable display timing for one identity-bound reviewer callback."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .workflows.review import ReviewRound


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
FIELDS = frozenset(
    {
        "schema_version",
        "owner_id",
        "root_operation_id",
        "parent_operation_id",
        "round_operation_id",
        "run_id",
        "axis",
        "started_at",
        "completed_at",
        "callback_sha256",
    }
)


def reviewer_timing_path(store_root: Path, round_: ReviewRound) -> Path:
    """Return the attempt-scoped path owned by the exact reviewer round."""

    return (
        store_root
        / "review-data"
        / round_.owner_id
        / round_.owner_id
        / "review-timing"
        / round_.parent_operation_id
        / f"{round_.operation_id}.json"
    )


def _path_is_safe(path: Path, boundary: Path) -> bool:
    path = Path(os.path.abspath(path.expanduser()))
    boundary = Path(os.path.abspath(boundary.expanduser()))
    try:
        path.relative_to(boundary)
    except ValueError:
        return False
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current = current / part
            if current.is_symlink():
                return False
    except OSError:
        return False
    return True


def _read_object(path: Path, *, boundary: Path) -> tuple[dict[str, Any], bytes] | None:
    if not _path_is_safe(path, boundary) or not path.is_file() or path.is_symlink():
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        raw = os.read(descriptor, 65_537)
        if len(raw) > 65_536 or os.read(descriptor, 1):
            return None
    except OSError:
        return None
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return (value, raw) if isinstance(value, dict) else None


def _canonical(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _epoch(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _started_epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            return None
        result = parsed.timestamp()
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _identity(round_: ReviewRound, callback_sha256: str) -> dict[str, object] | None:
    root_operation_id = round_.spec.root_operation_id
    if not root_operation_id or SHA256.fullmatch(callback_sha256) is None:
        return None
    return {
        "schema_version": 1,
        "owner_id": round_.owner_id,
        "root_operation_id": root_operation_id,
        "parent_operation_id": round_.parent_operation_id,
        "round_operation_id": round_.operation_id,
        "run_id": round_.run_id,
        "axis": round_.axis,
        "callback_sha256": callback_sha256,
    }


def _existing_matches(
    path: Path,
    *,
    boundary: Path,
    identity: Mapping[str, object],
    started_at: float,
) -> bool:
    snapshot = _read_object(path, boundary=boundary)
    if snapshot is None:
        return False
    value, raw = snapshot
    existing_start = _epoch(value.get("started_at"))
    completed_at = _epoch(value.get("completed_at"))
    return (
        set(value) == FIELDS
        and all(value.get(key) == expected for key, expected in identity.items())
        and existing_start == started_at
        and completed_at is not None
        and completed_at >= started_at
        and raw == _canonical(value)
    )


def _publish_new(path: Path, payload: bytes) -> bool:
    descriptor, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_tmp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            return False
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def publish_reviewer_timing(
    store_root: Path,
    runtime_root: Path,
    round_: ReviewRound,
    callback_sha256: str,
    *,
    observed_at: float | None = None,
) -> Path | None:
    """Publish one immutable interval; invalid or conflicting evidence is absent."""

    identity = _identity(round_, callback_sha256)
    if identity is None:
        return None
    meta_path = (
        runtime_root
        / "callbacks"
        / round_.axis
        / ".review-meta.json"
    )
    meta_snapshot = _read_object(meta_path, boundary=runtime_root)
    if meta_snapshot is None:
        return None
    meta = meta_snapshot[0]
    if any(
        meta.get(key) != expected
        for key, expected in {
            "schema_version": 1,
            "transport": "review-round",
            "operation_id": round_.operation_id,
            "run_id": round_.run_id,
            "parent_session_operation_id": round_.parent_operation_id,
            "axis": round_.axis,
        }.items()
    ):
        return None
    started_at = _started_epoch(meta.get("started_at"))
    if started_at is None:
        return None
    path = reviewer_timing_path(store_root, round_)
    try:
        if not _path_is_safe(path, store_root):
            return None
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        if not _path_is_safe(path, store_root):
            return None
        lock_path = path.parent / ".lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            lock_path.chmod(0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            if path.exists() or path.is_symlink():
                return (
                    path
                    if _existing_matches(
                        path,
                        boundary=store_root,
                        identity=identity,
                        started_at=started_at,
                    )
                    else None
                )
            completed_at = _epoch(time.time() if observed_at is None else observed_at)
            if completed_at is None or completed_at < started_at:
                return None
            value = {
                **identity,
                "started_at": started_at,
                "completed_at": completed_at,
            }
            if not _publish_new(path, _canonical(value)):
                return (
                    path
                    if _existing_matches(
                        path,
                        boundary=store_root,
                        identity=identity,
                        started_at=started_at,
                    )
                    else None
                )
            return path
    except (OSError, TypeError, ValueError):
        return None
