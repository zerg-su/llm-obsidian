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

    broken_target = Path(raw) / "broken-review-target"
    broken_target.symlink_to(Path(raw) / "missing-review-target")
    try:
        resolve_target(broken_target)
    except ReviewTargetError as exc:
        check(
            "broken target symlinks fail before Git root discovery",
            "does not exist" in str(exc),
            str(exc),
        )
    else:
        check("broken target symlinks fail before Git root discovery", False)

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

    fallback_repo = Path(raw) / "fallback-product"
    fallback_repo.mkdir()
    git(fallback_repo, "init", "-b", "main")
    git(fallback_repo, "config", "user.email", "review@example.invalid")
    git(fallback_repo, "config", "user.name", "Review Fallback Test")
    (fallback_repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(fallback_repo, "add", "app.py")
    git(fallback_repo, "commit", "-m", "root")
    fallback_target = resolve_target(fallback_repo)
    root_fallback = snapshot_light(fallback_target)
    check(
        "root commit fallback records its mandatory coverage gap",
        root_fallback.base_source == "root-commit-fallback"
        and root_fallback.coverage_gaps
        == ("No branch base was resolved; coverage starts at the root commit.",),
    )
    (fallback_repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(fallback_repo, "add", "app.py")
    git(fallback_repo, "commit", "-m", "second")
    latest_fallback = snapshot_light(fallback_target)
    check(
        "latest commit fallback records its mandatory coverage gap",
        latest_fallback.base_source == "latest-commit-fallback"
        and latest_fallback.coverage_gaps
        == ("No branch base was resolved; coverage starts at the latest commit.",),
    )

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

    outside = Path(raw) / "outside-secret.txt"
    outside.write_text("must stay outside provider context\n", encoding="utf-8")

    tracked_link_repo = Path(raw) / "tracked-link-product"
    tracked_link_repo.mkdir()
    git(tracked_link_repo, "init", "-b", "main")
    git(tracked_link_repo, "config", "user.email", "review@example.invalid")
    git(tracked_link_repo, "config", "user.name", "Review Target Symlink Test")
    (tracked_link_repo / "review-me").write_text("safe baseline\n", encoding="utf-8")
    git(tracked_link_repo, "add", "review-me")
    git(tracked_link_repo, "commit", "-m", "tracked link baseline")
    tracked_link_base = git(tracked_link_repo, "rev-parse", "HEAD")
    (tracked_link_repo / "review-me").unlink()
    (tracked_link_repo / "review-me").symlink_to(outside)
    git(tracked_link_repo, "add", "review-me")
    git(tracked_link_repo, "commit", "-m", "replace file with absolute link")
    try:
        snapshot_light(resolve_target(tracked_link_repo), base=tracked_link_base)
    except ReviewTargetError as exc:
        check(
            "tracked changed symlinks cannot escape the review target",
            "symlink escapes" in str(exc),
            str(exc),
        )
    else:
        check("tracked changed symlinks cannot escape the review target", False)

    untracked_link_repo = Path(raw) / "untracked-link-product"
    untracked_link_repo.mkdir()
    git(untracked_link_repo, "init", "-b", "main")
    git(untracked_link_repo, "config", "user.email", "review@example.invalid")
    git(untracked_link_repo, "config", "user.name", "Review Target Symlink Test")
    (untracked_link_repo / "safe.txt").write_text("baseline\n", encoding="utf-8")
    git(untracked_link_repo, "add", "safe.txt")
    git(untracked_link_repo, "commit", "-m", "untracked link baseline")
    (untracked_link_repo / "review-me").symlink_to("../outside-secret.txt")
    try:
        snapshot_light(resolve_target(untracked_link_repo), include_untracked=True)
    except ReviewTargetError as exc:
        check(
            "explicit untracked symlinks cannot escape the review target",
            "symlink escapes" in str(exc),
            str(exc),
        )
    else:
        check("explicit untracked symlinks cannot escape the review target", False)

print("\nAll review target tests passed.")
