#!/usr/bin/env python3
"""Public behavior for the bounded code-owned reviewer Git facade."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INSPECT = ROOT / "scripts" / "review-inspect.py"


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def inspect(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(INSPECT), "--worktree", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "REVIEW_INSPECT_INJECTION": "must-not-affect-git",
        },
    )


def tree_evidence(root: Path) -> tuple[str, str, str]:
    status = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    head = git(root, "rev-parse", "HEAD")
    git_dir = Path(git(root, "rev-parse", "--absolute-git-dir"))
    digest = hashlib.sha256()
    for path in sorted(git_dir.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(git_dir).as_posix().encode())
            digest.update(path.read_bytes())
    return status, head, digest.hexdigest()


with tempfile.TemporaryDirectory(prefix="review-inspect.") as raw:
    repo = Path(raw) / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "review-inspect@example.invalid")
    git(repo, "config", "user.name", "Review Inspect Test")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "first")
    first = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "v-test", first)
    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    (repo / "notes.txt").write_text("note\n", encoding="utf-8")
    git(repo, "add", "tracked.txt", "notes.txt")
    git(repo, "commit", "-m", "second")
    head = git(repo, "rev-parse", "HEAD")
    (repo / "tracked.txt").write_text("one\ntwo\nworking\n", encoding="utf-8")
    before = tree_evidence(repo)

    status = inspect(repo, "status", "--expect-head", head)
    check(
        "status reports exact HEAD and porcelain state",
        status.returncode == 0
        and json.loads(status.stdout)["head_sha"] == head
        and json.loads(status.stdout)["clean"] is False
        and "tracked.txt" in json.loads(status.stdout)["status"],
        (status.stdout, status.stderr),
    )
    wrong = inspect(repo, "status", "--expect-head", first)
    check("status rejects a stale exact HEAD", wrong.returncode == 4, wrong.stderr)

    log = inspect(repo, "log", "--ref", head)
    check(
        "recent log is bounded and rooted at the validated ref",
        log.returncode == 0
        and "second" in log.stdout
        and len(log.stdout.encode()) <= 16_384,
        (log.stdout, log.stderr),
    )
    stat = inspect(repo, "diff", "--base", first, "--head", head, "--format", "stat")
    names = inspect(repo, "diff", "--base", first, "--head", head, "--format", "names")
    patch = inspect(
        repo,
        "diff",
        "--base",
        first,
        "--head",
        head,
        "--format",
        "patch",
        "--path",
        "tracked.txt",
    )
    check(
        "diff exposes stat names and a path-scoped bounded patch",
        stat.returncode == names.returncode == patch.returncode == 0
        and "tracked.txt" in stat.stdout
        and names.stdout.splitlines() == ["notes.txt", "tracked.txt"]
        and "+two" in patch.stdout
        and "notes.txt" not in patch.stdout
        and len(patch.stdout.encode()) <= 65_536,
        (stat.stdout, names.stdout, patch.stderr),
    )
    check_result = inspect(
        repo, "diff", "--base", first, "--head", head, "--format", "check"
    )
    check("diff --check has a dedicated bounded form", check_result.returncode == 0)

    metadata = inspect(repo, "commit", "--ref", head, "--format", "metadata")
    content = inspect(
        repo,
        "commit",
        "--ref",
        head,
        "--format",
        "content",
        "--path",
        "tracked.txt",
    )
    check(
        "one commit exposes bounded metadata or path-scoped content",
        metadata.returncode == content.returncode == 0
        and head in metadata.stdout
        and "second" in metadata.stdout
        and "+two" in content.stdout
        and "notes.txt" not in content.stdout,
        (metadata.stdout, content.stdout),
    )
    contains = inspect(repo, "contains", "--sha", first)
    check(
        "validated SHA containment reports local branches and tags only",
        contains.returncode == 0
        and "refs/heads/main" in contains.stdout
        and "refs/tags/v-test" in contains.stdout,
        contains.stdout,
    )

    for label, args in (
        ("symbolic refs", ("log", "--ref", "HEAD")),
        (
            "path traversal",
            (
                "diff",
                "--base",
                first,
                "--head",
                head,
                "--format",
                "patch",
                "--path",
                "../outside",
            ),
        ),
        ("unknown operations", ("remote", "-v")),
    ):
        rejected = inspect(repo, *args)
        check(f"review-inspect rejects {label}", rejected.returncode == 2, rejected.stderr)

    after = tree_evidence(repo)
    check(
        "all review inspection leaves worktree and Git metadata byte-stable",
        after == before,
        (before, after),
    )

print("\nAll review-inspect tests passed.")
