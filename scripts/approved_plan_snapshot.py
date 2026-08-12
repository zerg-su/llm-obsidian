"""Content-addressed approved-plan snapshots for active dispatch authority."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SNAPSHOT_DIRECTORY = "approved-plan-snapshots"


class PlanSnapshotError(ValueError):
    """The approved snapshot identity or its immutable bytes are invalid."""


@dataclass(frozen=True)
class ApprovedPlanSnapshot:
    path: Path
    sha256: str
    content: bytes


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_regular(path: Path, label: str, *, owner_only: bool = False) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PlanSnapshotError(f"{label} is unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PlanSnapshotError(f"{label} is not a regular file")
        if owner_only and (
            before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) & 0o077
        ):
            raise PlanSnapshotError(f"{label} is not owner-only")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise PlanSnapshotError(f"{label} changed during capture")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _snapshot_root(vault_root: Path, *, create: bool) -> Path:
    vault = vault_root.expanduser().resolve()
    runtime = vault / ".vault-meta"
    if runtime.is_symlink() or not runtime.is_dir():
        raise PlanSnapshotError("plan snapshot runtime root is invalid or symlinked")
    root = runtime / SNAPSHOT_DIRECTORY
    if root.is_symlink():
        raise PlanSnapshotError("plan snapshot directory cannot be a symlink")
    if create:
        try:
            root.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise PlanSnapshotError("plan snapshot directory is unavailable") from exc
    if not root.is_dir():
        raise PlanSnapshotError("plan snapshot directory is unavailable")
    info = root.stat()
    if info.st_uid != os.getuid():
        raise PlanSnapshotError("plan snapshot directory is not owner-owned")
    if stat.S_IMODE(info.st_mode) & 0o077:
        root.chmod(0o700)
    return root


def _publish_immutable(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise PlanSnapshotError("approved plan snapshot cannot be published") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            current = _read_regular(
                path, "approved plan snapshot", owner_only=True
            )
            if current != content:
                raise PlanSnapshotError(
                    "approved plan snapshot digest collision or tampering"
                )
        except OSError as exc:
            raise PlanSnapshotError(
                "approved plan snapshot cannot be published"
            ) from exc
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def bind_approved_plan_snapshot(request: Mapping[str, Any]) -> dict[str, Any]:
    """Capture the exact approved bytes and return their private request binding."""

    vault_value = request.get("vault_root")
    source_value = request.get("_approved_plan_file", request.get("plan_file"))
    if not isinstance(vault_value, Path) or not isinstance(source_value, Path):
        raise PlanSnapshotError("approved plan capture requires resolved paths")
    source = source_value.expanduser().resolve()
    content = _read_regular(source, "approved plan source")
    digest = _sha256(content)
    expected = request.get("_approved_plan_sha256")
    if expected is not None and expected != digest:
        raise PlanSnapshotError("approved plan source digest changed")
    root = _snapshot_root(vault_value, create=True)
    target = root / f"{digest}.md"
    _publish_immutable(target, content)
    result = dict(request)
    result["_approved_plan_file"] = target
    result["_approved_plan_sha256"] = digest
    prompt = request.get("_approved_prompt")
    if isinstance(prompt, str):
        source_text = str(source)
        if source_text not in prompt:
            raise PlanSnapshotError(
                "approved task prompt does not name its plan snapshot"
            )
        result["_approved_prompt"] = prompt.replace(source_text, str(target))
    return result


def validate_approved_plan_snapshot(
    meta: Mapping[str, Any],
) -> ApprovedPlanSnapshot:
    """Resolve only the canonical snapshot named by the frozen metadata."""

    vault_raw = str(meta.get("vault_root") or "").strip()
    path_raw = str(meta.get("plan_snapshot_file") or "").strip()
    digest = str(meta.get("approved_plan_sha256") or "").strip()
    if not vault_raw or not path_raw or not SHA256.fullmatch(digest):
        raise PlanSnapshotError("approved plan snapshot identity is incomplete")
    vault = Path(vault_raw).expanduser()
    path = Path(path_raw).expanduser()
    if not vault.is_absolute() or not path.is_absolute():
        raise PlanSnapshotError("approved plan snapshot identity must be absolute")
    root = _snapshot_root(vault.resolve(), create=False)
    canonical = root / f"{digest}.md"
    if path != canonical or path.resolve() != canonical:
        raise PlanSnapshotError("approved plan snapshot path is not canonical")
    worktree_raw = str(meta.get("worktree") or "").strip()
    if worktree_raw:
        worktree = Path(worktree_raw).expanduser().resolve()
        if path == worktree or worktree in path.parents:
            raise PlanSnapshotError("approved plan snapshot is inside the task worktree")
    content = _read_regular(path, "approved plan snapshot", owner_only=True)
    if _sha256(content) != digest:
        raise PlanSnapshotError("approved plan snapshot digest changed")
    return ApprovedPlanSnapshot(path, digest, content)
