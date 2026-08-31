#!/usr/bin/env python3
"""Behavior tests for generic review targets and immutable observations."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from review_target import (  # noqa: E402
    ReviewTargetError,
    compare_snapshot,
    resolve_target,
    snapshot_clean,
    snapshot_light,
)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


with tempfile.TemporaryDirectory(prefix="review-target-test.") as raw:
    repo = Path(raw) / "external-product"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "review@example.invalid")
    git(repo, "config", "user.name", "Review Target Test")
    (repo / "src").mkdir()
    (repo / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "README.md").write_text("# Product\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    initial = git(repo, "rev-parse", "HEAD")

    git(repo, "switch", "-c", "feature")
    (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(repo, "add", "src/app.py")
    git(repo, "commit", "-m", "feature commit")
    feature_head = git(repo, "rev-parse", "HEAD")

    (repo / "staged.py").write_text("STAGED = True\n", encoding="utf-8")
    git(repo, "add", "staged.py")
    (repo / "README.md").write_text("# Product\n\nDirty overlay.\n", encoding="utf-8")
    (repo / "notes.txt").write_text("explicitly untracked\n", encoding="utf-8")

    nested = repo / "src"
    target = resolve_target(nested)
    check("nested path resolves the exact Git root", target.root == repo.resolve())
    check("target identity binds common dir and worktree", len(target.target_key) == 32)

    without_untracked = snapshot_light(target, base=initial)
    check("explicit base resolves to the requested merge base", without_untracked.base == initial)
    check("snapshot binds exact HEAD", without_untracked.head == feature_head)
    check(
        "committed, staged, and unstaged changes share one scope",
        set(without_untracked.changed_paths) == {"README.md", "src/app.py", "staged.py"},
        repr(without_untracked.changed_paths),
    )
    check("untracked files are excluded by default", without_untracked.untracked_paths == ())
    check("snapshot records the excluded-untracked request", not without_untracked.include_untracked)

    with_untracked = snapshot_light(target, base=initial, include_untracked=True)
    check("untracked files require explicit inclusion", with_untracked.untracked_paths == ("notes.txt",))
    check("snapshot records the included-untracked request", with_untracked.include_untracked)
    check("untracked bytes affect the snapshot identity", with_untracked.snapshot_sha256 != without_untracked.snapshot_sha256)

    scoped = snapshot_light(
        target,
        base=initial,
        paths=("src",),
        include_untracked=True,
    )
    check("path scope constrains every overlay", scoped.changed_paths == ("src/app.py",))
    check("path scope also constrains untracked files", scoped.untracked_paths == ())

    before = with_untracked
    (repo / "README.md").write_text("# Product\n\nChanged during review.\n", encoding="utf-8")
    after = snapshot_light(target, base=initial, include_untracked=True)
    drift = compare_snapshot(before, after)
    check("tracked drift is observable", drift.changed and "worktree" in drift.reasons, repr(drift.reasons))

    try:
        snapshot_clean(target)
    except ReviewTargetError as exc:
        check("Lifecycle admission rejects a dirty target", "clean" in str(exc))
    else:
        check("Lifecycle admission rejects a dirty target", False)

    git(repo, "reset", "--hard", "HEAD")
    (repo / "notes.txt").unlink()
    clean = snapshot_clean(target)
    check("clean snapshot binds exact committed HEAD", clean.head == feature_head)
    check("clean snapshot carries the same target key", clean.target_key == target.target_key)

    (repo / "new-untracked.txt").write_text("drift\n", encoding="utf-8")
    try:
        snapshot_clean(target)
    except ReviewTargetError:
        check("untracked drift makes a target non-clean", True)
    else:
        check("untracked drift makes a target non-clean", False)

    try:
        snapshot_light(target, base="missing-review-base")
    except ReviewTargetError as exc:
        check("invalid explicit base fails closed", "base" in str(exc))
    else:
        check("invalid explicit base fails closed", False)

    byte_repo = Path(raw) / "byte-product"
    byte_repo.mkdir()
    git(byte_repo, "init", "-b", "main")
    git(byte_repo, "config", "user.email", "review@example.invalid")
    git(byte_repo, "config", "user.name", "Review Target Byte Test")
    (byte_repo / "payload.txt").write_bytes(b"before-\xff\n")
    git(byte_repo, "add", "payload.txt")
    git(byte_repo, "commit", "-m", "byte baseline")
    byte_base = git(byte_repo, "rev-parse", "HEAD")
    (byte_repo / "payload.txt").write_bytes(b"after-\xfe\n")
    non_utf_name = b"untracked-\xff.txt".decode("utf-8", "surrogateescape")
    try:
        (byte_repo / non_utf_name).write_bytes(b"untracked\n")
    except OSError:
        non_utf_name = ""
    byte_snapshot = snapshot_light(
        resolve_target(byte_repo),
        base=byte_base,
        include_untracked=bool(non_utf_name),
    )
    check(
        "Light snapshot preserves arbitrary Git bytes without a decode crash",
        byte_snapshot.changed_paths == ("payload.txt",)
        and byte_snapshot.untracked_paths
        == ((non_utf_name,) if non_utf_name else ()),
        repr(byte_snapshot),
    )

    whitespace_repo = Path(raw) / "whitespace-product"
    whitespace_repo.mkdir()
    git(whitespace_repo, "init", "-b", "main")
    git(whitespace_repo, "config", "user.email", "review@example.invalid")
    git(whitespace_repo, "config", "user.name", "Review Target Path Test")
    (whitespace_repo / " committed.txt").write_text("before\n", encoding="utf-8")
    (whitespace_repo / " worktree.txt").write_text("before\n", encoding="utf-8")
    git(whitespace_repo, "add", " committed.txt", " worktree.txt")
    git(whitespace_repo, "commit", "-m", "whitespace baseline")
    whitespace_base = git(whitespace_repo, "rev-parse", "HEAD")
    (whitespace_repo / " committed.txt").write_text("after\n", encoding="utf-8")
    git(whitespace_repo, "add", " committed.txt")
    git(whitespace_repo, "commit", "-m", "whitespace committed")
    (whitespace_repo / " staged.txt").write_text("staged\n", encoding="utf-8")
    git(whitespace_repo, "add", " staged.txt")
    (whitespace_repo / " worktree.txt").write_text("worktree\n", encoding="utf-8")
    (whitespace_repo / " untracked.txt").write_text("untracked\n", encoding="utf-8")
    whitespace_target = resolve_target(whitespace_repo)
    whitespace_snapshot = snapshot_light(
        whitespace_target,
        base=whitespace_base,
        include_untracked=True,
    )
    check(
        "Light overlays preserve leading-whitespace Git paths",
        whitespace_snapshot.changed_paths
        == (" committed.txt", " staged.txt", " worktree.txt")
        and whitespace_snapshot.untracked_paths == (" untracked.txt",),
        repr(whitespace_snapshot),
    )
    whitespace_scoped = snapshot_light(
        whitespace_target,
        base=whitespace_base,
        paths=(" committed.txt",),
        include_untracked=True,
    )
    check(
        "Light explicit scope preserves leading-whitespace path text",
        whitespace_scoped.paths == (" committed.txt",)
        and whitespace_scoped.changed_paths == (" committed.txt",),
        repr(whitespace_scoped),
    )

print("\nAll review target tests passed.")
