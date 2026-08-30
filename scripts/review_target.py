"""Generic Git review targets and read-only change observations."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from harness.git_ops import GitAdapter, GitError


class ReviewTargetError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewTarget:
    root: Path
    common_dir: Path
    branch: str
    detached: bool
    target_key: str


@dataclass(frozen=True)
class LightSnapshot:
    target_key: str
    root: Path
    head: str
    base: str
    base_source: str
    paths: tuple[str, ...]
    changed_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    committed_sha256: str
    index_sha256: str
    worktree_sha256: str
    untracked_sha256: str
    snapshot_sha256: str
    coverage_gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class CleanHeadSnapshot:
    target_key: str
    root: Path
    head: str
    snapshot_sha256: str


@dataclass(frozen=True)
class DriftResult:
    changed: bool
    reasons: tuple[str, ...]


def _sha256(value: str | bytes) -> str:
    raw = value.encode("utf-8", errors="surrogateescape") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _canonical_sha256(value: object) -> str:
    return _sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )


def _adapter(target: ReviewTarget) -> GitAdapter:
    return GitAdapter(target.root)


def resolve_target(path: Path | str) -> ReviewTarget:
    try:
        adapter = GitAdapter.resolve(path)
        isolation = adapter.isolation()
    except (GitError, OSError) as exc:
        raise ReviewTargetError(str(exc)) from exc
    identity = _canonical_sha256(
        {"common_dir": str(isolation.common_dir), "root": str(isolation.root)}
    )[:32]
    return ReviewTarget(
        root=isolation.root,
        common_dir=isolation.common_dir,
        branch=isolation.branch,
        detached=isolation.detached,
        target_key=identity,
    )


def _normalize_paths(paths: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw in paths:
        value = str(raw).strip().replace(os.sep, "/")
        candidate = PurePosixPath(value)
        if (
            not value
            or "\x00" in value
            or candidate.is_absolute()
            or ".." in candidate.parts
            or value.startswith(":")
        ):
            raise ReviewTargetError(f"review path scope is invalid: {raw!r}")
        normalized.append(candidate.as_posix().rstrip("/"))
    return tuple(sorted(set(normalized)))


def _nul_paths(value: str) -> tuple[str, ...]:
    return tuple(sorted(path for path in value.split("\x00") if path))


def _base(
    adapter: GitAdapter, head: str, explicit: str
) -> tuple[str, str, bool, tuple[str, ...]]:
    if explicit:
        try:
            requested = adapter.revision(explicit)
            return adapter.merge_base(head, requested), "explicit", False, ()
        except GitError as exc:
            raise ReviewTargetError(f"explicit review base is invalid: {exc}") from exc

    for ref, source in (
        ("@{upstream}", "upstream"),
        ("refs/remotes/origin/HEAD", "origin-head"),
        ("main", "local-main"),
        ("master", "local-master"),
    ):
        candidate = adapter.optional_revision(ref)
        if not candidate or candidate == head:
            continue
        try:
            return adapter.merge_base(head, candidate), source, False, ()
        except GitError:
            continue

    parent = adapter.optional_revision(f"{head}^")
    if parent:
        return (
            parent,
            "latest-commit-fallback",
            False,
            ("No branch base was resolved; coverage starts at the latest commit.",),
        )
    return (
        head,
        "root-commit-fallback",
        True,
        ("No branch base was resolved; coverage starts at the root commit.",),
    )


def _untracked_digest(root: Path, paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        candidate = root / relative
        try:
            mode = candidate.lstat().st_mode
        except OSError as exc:
            raise ReviewTargetError(
                f"included untracked path is unavailable: {relative}"
            ) from exc
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\x00")
        if stat.S_ISLNK(mode):
            digest.update(b"symlink\x00")
            digest.update(os.readlink(candidate).encode("utf-8", errors="surrogateescape"))
        elif stat.S_ISREG(mode):
            digest.update(b"file\x00")
            try:
                with candidate.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                raise ReviewTargetError(
                    f"included untracked path is unreadable: {relative}"
                ) from exc
        else:
            raise ReviewTargetError(
                f"included untracked path is not a regular file: {relative}"
            )
        digest.update(b"\x00")
    return digest.hexdigest()


def snapshot_light(
    target: ReviewTarget,
    *,
    base: str = "",
    paths: Iterable[str] = (),
    include_untracked: bool = False,
) -> LightSnapshot:
    adapter = _adapter(target)
    path_scope = _normalize_paths(paths)
    try:
        head = adapter.revision("HEAD")
        resolved_base, base_source, root_commit, gaps = _base(adapter, head, base)
        if root_commit:
            committed = adapter.root_commit_text(head, paths=path_scope)
            committed_paths = _nul_paths(
                adapter.root_commit_text(head, name_only=True, paths=path_scope)
            )
        else:
            committed = adapter.diff_text(resolved_base, head, paths=path_scope)
            committed_paths = _nul_paths(
                adapter.diff_text(
                    resolved_base, head, name_only=True, paths=path_scope
                )
            )
        staged = adapter.diff_text(cached=True, paths=path_scope)
        staged_paths = _nul_paths(
            adapter.diff_text(cached=True, name_only=True, paths=path_scope)
        )
        worktree = adapter.diff_text(paths=path_scope)
        worktree_paths = _nul_paths(
            adapter.diff_text(name_only=True, paths=path_scope)
        )
        untracked = (
            adapter.untracked_paths(path_scope) if include_untracked else ()
        )
    except GitError as exc:
        raise ReviewTargetError(f"review target snapshot failed: {exc}") from exc

    changed = tuple(sorted(set((*committed_paths, *staged_paths, *worktree_paths))))
    components = {
        "target_key": target.target_key,
        "head": head,
        "base": resolved_base,
        "base_source": base_source,
        "paths": path_scope,
        "changed_paths": changed,
        "untracked_paths": untracked,
        "committed_sha256": _sha256(committed),
        "index_sha256": _sha256(staged),
        "worktree_sha256": _sha256(worktree),
        "untracked_sha256": _untracked_digest(target.root, untracked),
        "coverage_gaps": gaps,
    }
    return LightSnapshot(
        root=target.root,
        snapshot_sha256=_canonical_sha256(components),
        **components,
    )


def snapshot_clean(target: ReviewTarget) -> CleanHeadSnapshot:
    adapter = _adapter(target)
    try:
        snapshot = adapter.inspect("HEAD")
        status = adapter.status_porcelain()
    except GitError as exc:
        raise ReviewTargetError(f"clean review snapshot failed: {exc}") from exc
    if status or snapshot.dirty_paths or snapshot.conflicts or snapshot.operation:
        raise ReviewTargetError(
            "Lifecycle review requires a clean target worktree with no Git operation"
        )
    identity = {
        "target_key": target.target_key,
        "root": str(target.root),
        "head": snapshot.head,
        "clean": True,
    }
    return CleanHeadSnapshot(
        target_key=target.target_key,
        root=target.root,
        head=snapshot.head,
        snapshot_sha256=_canonical_sha256(identity),
    )


def compare_snapshot(
    before: LightSnapshot | CleanHeadSnapshot,
    after: LightSnapshot | CleanHeadSnapshot,
) -> DriftResult:
    if type(before) is not type(after):
        return DriftResult(True, ("snapshot-kind",))
    if before.snapshot_sha256 == after.snapshot_sha256:
        return DriftResult(False, ())
    reasons: list[str] = []
    if before.target_key != after.target_key:
        reasons.append("target")
    if before.head != after.head:
        reasons.append("head")
    if isinstance(before, LightSnapshot) and isinstance(after, LightSnapshot):
        for field, reason in (
            ("base", "base"),
            ("paths", "scope"),
            ("committed_sha256", "committed"),
            ("index_sha256", "index"),
            ("worktree_sha256", "worktree"),
            ("untracked_sha256", "untracked"),
        ):
            if getattr(before, field) != getattr(after, field):
                reasons.append(reason)
    if not reasons:
        reasons.append("snapshot")
    return DriftResult(True, tuple(reasons))


def snapshot_payload(snapshot: LightSnapshot | CleanHeadSnapshot) -> dict[str, object]:
    payload = asdict(snapshot)
    payload["root"] = str(snapshot.root)
    payload["schema_version"] = 1
    return payload
