"""Owner-only persistence primitives for task session state."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from task_session_contracts import (
    DOMAINS,
    RUNTIMES,
    TaskSessionError,
    require_uuid,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_object(path: Path, *, required: bool = True) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not required:
            return {}
        raise TaskSessionError(f"missing state file: {path}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskSessionError(f"invalid state file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TaskSessionError(f"state file must contain an object: {path}")
    return value


def ensure_owner_only_dir(path: Path) -> Path:
    try:
        info = path.stat()
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        info = path.stat()
    else:
        if path.is_symlink() or not path.is_dir():
            raise TaskSessionError(f"state directory is not an owned directory: {path}")
        if info.st_uid != os.getuid():
            raise TaskSessionError(f"state directory is not owner-only: {path}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            path.chmod(0o700)
            info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise TaskSessionError(f"state directory is not owner-only: {path}")
    return path


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    ensure_owner_only_dir(path.parent)
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_write_file_only(path: Path, value: dict[str, Any]) -> None:
    """Atomically write an owner-only file without changing its parent mode."""
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_uid != os.getuid():
        raise TaskSessionError(f"state file parent is missing or not owned by the current user: {parent}")
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        path.chmod(0o600)
    finally:
        temp.unlink(missing_ok=True)


def remove_owned_research_scratch(path: Path, vault_root: Path) -> bool:
    """Remove only a coordinator-created fetch/synth temp directory."""
    try:
        resolved = path.expanduser().resolve()
        resolved.relative_to(vault_root.expanduser().resolve())
    except ValueError:
        pass
    else:
        return False
    if (
        path.is_symlink()
        or not resolved.is_dir()
        or resolved.stat().st_uid != os.getuid()
        or not resolved.name.startswith(("llm-obsidian-fetch-", "llm-obsidian-synth-"))
    ):
        return False
    shutil.rmtree(resolved)
    return True


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    ensure_owner_only_dir(path.parent)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with os.fdopen(descriptor, "r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        pass


def git_common_dir(worktree: Path) -> Path:
    root = worktree.expanduser().resolve()
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
        text=True,
        capture_output=True,
        check=False,
    )
    raw = result.stdout.strip()
    if result.returncode != 0 or not raw or "\n" in raw or "\0" in raw:
        raise TaskSessionError("persistent task sessions require a Git worktree")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    common = candidate.resolve()
    if not common.is_dir() or common.stat().st_uid != os.getuid():
        raise TaskSessionError("Git common directory is missing or not owned by the current user")
    return common


def project_id_for(worktree: Path, *, create: bool) -> str:
    common = git_common_dir(worktree)
    marker_dir = common / "llm-obsidian"
    marker = marker_dir / "project-id"
    lock = marker_dir / "project-id.lock"
    with file_lock(lock):
        if marker.exists():
            try:
                value = marker.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise TaskSessionError(f"cannot read project identity: {exc}") from exc
            if marker.stat().st_uid != os.getuid() or stat.S_IMODE(marker.stat().st_mode) & 0o077:
                raise TaskSessionError("project identity marker is not owner-only")
            return require_uuid(value, "project_id")
        if not create:
            raise TaskSessionError("project identity marker is missing")
        value = str(uuid.uuid4())
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return value


def lane_id_for(project_id: str, task_id: str, domain: str, runtime: str, model: str) -> str:
    project_id = require_uuid(project_id, "project_id")
    task_id = require_uuid(task_id, "task_id")
    if domain not in DOMAINS:
        raise TaskSessionError(f"domain must be one of {sorted(DOMAINS)}")
    if runtime not in RUNTIMES:
        raise TaskSessionError(f"runtime must be one of {sorted(RUNTIMES)}")
    if not model.strip() or len(model) > 200 or "\0" in model:
        raise TaskSessionError("model is invalid")
    payload = "\0".join((project_id, task_id, domain, runtime, model)).encode()
    return hashlib.sha256(payload).hexdigest()[:32]
