"""Owner-only dispatch request persistence and path validation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DispatchError(ValueError):
    """The dispatch request violates its pre-effect contract."""


def _approval_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(
        path.with_suffix(".lock"),
        flags,
        0o600,
    )
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(descriptor)
        raise DispatchError("custom approval lock is not owner-only")
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


def custom_authoring_enabled(vault_root: Path) -> bool:
    path = vault_root / "config" / "harness.toml"
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DispatchError("custom pipeline policy is unavailable") from exc
    features = value.get("features")
    enabled = (
        features.get("custom_pipeline_authoring")
        if isinstance(features, dict)
        else None
    )
    if not isinstance(enabled, bool):
        raise DispatchError("custom pipeline authoring switch is invalid")
    return enabled


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DispatchError(f"missing JSON file: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DispatchError(f"JSON root must be an object: {path}")
    return value


def ensure_owned_dir(path: Path) -> None:
    if path.exists():
        info = path.stat()
        if path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid():
            raise DispatchError(f"runtime directory is not owned by the current user: {path}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            path.chmod(0o700)
    else:
        path.mkdir(parents=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise DispatchError(f"runtime directory is not owned by the current user: {path}")


def atomic_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        path.chmod(mode)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_text(
        path,
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def exclusive_json(path: Path, value: dict[str, Any]) -> None:
    """Create one durable claim without a check-then-create race."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def absolute_dir(value: Any, field: str, *, must_exist: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise DispatchError(f"{field} must be a non-empty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise DispatchError(f"{field} must be a non-empty absolute path")
    path = path.resolve()
    if must_exist and not path.is_dir():
        raise DispatchError(f"{field} directory is missing: {path}")
    return path


def absolute_file(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise DispatchError(f"{field} must be a non-empty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise DispatchError(f"{field} must be a non-empty absolute path")
    path = path.resolve()
    if not path.is_file():
        raise DispatchError(f"{field} file is missing: {path}")
    return path


def require_string(value: Any, field: str, *, maximum: int = 10_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DispatchError(f"{field} must be a non-empty string")
    value = value.strip()
    if "\0" in value or len(value) > maximum:
        raise DispatchError(f"{field} is invalid")
    return value
