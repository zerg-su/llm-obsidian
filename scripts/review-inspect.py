#!/usr/bin/env python3
"""Bounded, read-only Git inspection for provider review sessions."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence


TIMEOUT_SECONDS = 8
STDERR_LIMIT = 8_192
OUTPUT_LIMITS = {
    "status": 32_768,
    "log": 16_384,
    "stat": 32_768,
    "names": 32_768,
    "check": 32_768,
    "patch": 65_536,
    "metadata": 16_384,
    "content": 65_536,
    "contains": 32_768,
}
SHA_RE = re.compile(r"[0-9a-f]{40,64}\Z")
TRUNCATED = b"\n[review-inspect output truncated]\n"


class InspectError(ValueError):
    def __init__(self, message: str, *, returncode: int = 2) -> None:
        super().__init__(message)
        self.returncode = returncode


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool


def _bounded(value: bytes, limit: int) -> tuple[bytes, bool]:
    if len(value) <= limit:
        return value, False
    keep = max(0, limit - len(TRUNCATED))
    return value[:keep] + TRUNCATED, True


def _git_binary() -> str:
    binary = shutil.which(
        "git", path="/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"
    )
    if not binary:
        raise InspectError("git executable is unavailable", returncode=3)
    return str(Path(binary).resolve())


def _run_git(worktree: Path, args: Sequence[str], *, limit: int) -> GitResult:
    command = (
        _git_binary(),
        "--no-optional-locks",
        "-C",
        str(worktree),
        "--no-pager",
        *args,
    )
    environment = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            env=environment,
            start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise InspectError(
                f"Git inspection exceeded {TIMEOUT_SECONDS}s", returncode=124
            )
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout, truncated = _bounded(stdout_file.read(limit + 1), limit)
        stderr, _ = _bounded(stderr_file.read(STDERR_LIMIT + 1), STDERR_LIMIT)
    return GitResult(returncode, stdout, stderr, truncated)


def _worktree(value: str) -> Path:
    supplied = Path(value).expanduser()
    if not supplied.is_absolute() or supplied.is_symlink() or not supplied.is_dir():
        raise InspectError("--worktree must be an exact absolute directory")
    resolved = supplied.resolve()
    probe = _run_git(
        resolved,
        ("rev-parse", "--show-toplevel"),
        limit=4_096,
    )
    if probe.returncode:
        raise InspectError("--worktree is not a Git worktree", returncode=3)
    try:
        top = Path(probe.stdout.decode("utf-8").strip()).resolve()
    except UnicodeDecodeError as exc:
        raise InspectError("Git returned an invalid worktree path", returncode=3) from exc
    if top != resolved:
        raise InspectError("--worktree must name the exact Git worktree root")
    return resolved


def _sha(value: str, field: str) -> str:
    if not SHA_RE.fullmatch(value):
        raise InspectError(f"{field} must be an exact lowercase Git object id")
    return value


def _commit(worktree: Path, value: str, field: str) -> str:
    sha = _sha(value, field)
    result = _run_git(worktree, ("cat-file", "-e", f"{sha}^{{commit}}"), limit=1_024)
    if result.returncode:
        raise InspectError(f"{field} is not a local commit", returncode=4)
    return sha


def _path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or any(token in value for token in ("*", "?", "[", "]", "|", ";", ">", "<", "`", "$"))
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() == "."
        or any(part in {"", "."} for part in path.parts)
    ):
        raise InspectError("--path must be repository-relative without traversal")
    return path.as_posix()


def _paths(values: list[str]) -> tuple[str, ...]:
    return tuple(_path(value) for value in values)


def _emit(result: GitResult) -> int:
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    return result.returncode


def _status(worktree: Path, expected: str) -> int:
    expected = _sha(expected, "--expect-head")
    head_result = _run_git(worktree, ("rev-parse", "HEAD"), limit=128)
    if head_result.returncode:
        return _emit(head_result)
    head = head_result.stdout.decode("ascii").strip()
    if head != expected:
        raise InspectError("worktree HEAD does not match --expect-head", returncode=4)
    status = _run_git(
        worktree,
        ("status", "--porcelain=v1", "--untracked-files=all"),
        limit=OUTPUT_LIMITS["status"],
    )
    if status.returncode:
        return _emit(status)
    value = {
        "schema_version": 1,
        "head_sha": head,
        "clean": not bool(status.stdout.strip()),
        "status": status.stdout.decode("utf-8", errors="replace"),
        "truncated": status.truncated,
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    sys.stdout.write(encoded)
    return 0


def _log(worktree: Path, ref: str) -> int:
    ref = _commit(worktree, ref, "--ref")
    return _emit(
        _run_git(
            worktree,
            ("log", "--max-count=20", "--format=%H %s", ref),
            limit=OUTPUT_LIMITS["log"],
        )
    )


def _diff(
    worktree: Path,
    base: str,
    head: str,
    format_: str,
    paths: list[str],
) -> int:
    base = _commit(worktree, base, "--base")
    head = _commit(worktree, head, "--head")
    scoped = _paths(paths)
    forms = {
        "stat": ("diff", "--stat", "--no-ext-diff", "--no-textconv"),
        "names": ("diff", "--name-only", "--no-ext-diff", "--no-textconv"),
        "check": ("diff", "--check", "--no-ext-diff"),
        "patch": (
            "diff",
            "--patch",
            "--no-ext-diff",
            "--no-textconv",
            "--unified=80",
        ),
    }
    args = (*forms[format_], base, head, "--", *scoped)
    return _emit(_run_git(worktree, args, limit=OUTPUT_LIMITS[format_]))


def _show(worktree: Path, ref: str, format_: str, paths: list[str]) -> int:
    ref = _commit(worktree, ref, "--ref")
    scoped = _paths(paths)
    if format_ == "metadata":
        if scoped:
            raise InspectError("commit metadata does not accept --path")
        args = ("show", "--no-patch", "--format=fuller", ref)
    else:
        args = (
            "show",
            "--format=fuller",
            "--no-ext-diff",
            "--no-textconv",
            "--unified=80",
            ref,
            "--",
            *scoped,
        )
    return _emit(_run_git(worktree, args, limit=OUTPUT_LIMITS[format_]))


def _contains(worktree: Path, sha: str) -> int:
    sha = _commit(worktree, sha, "--sha")
    return _emit(
        _run_git(
            worktree,
            (
                "for-each-ref",
                f"--contains={sha}",
                "--format=%(refname)",
                "refs/heads",
                "refs/tags",
            ),
            limit=OUTPUT_LIMITS["contains"],
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one bounded read-only Git inspection operation."
    )
    parser.add_argument("--worktree", required=True)
    sub = parser.add_subparsers(dest="operation", required=True)
    status = sub.add_parser("status")
    status.add_argument("--expect-head", required=True)
    log = sub.add_parser("log")
    log.add_argument("--ref", required=True)
    diff = sub.add_parser("diff")
    diff.add_argument("--base", required=True)
    diff.add_argument("--head", required=True)
    diff.add_argument("--format", choices=("stat", "names", "check", "patch"), required=True)
    diff.add_argument("--path", action="append", default=[])
    commit = sub.add_parser("commit")
    commit.add_argument("--ref", required=True)
    commit.add_argument("--format", choices=("metadata", "content"), required=True)
    commit.add_argument("--path", action="append", default=[])
    contains = sub.add_parser("contains")
    contains.add_argument("--sha", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        worktree = _worktree(args.worktree)
        if args.operation == "status":
            return _status(worktree, args.expect_head)
        if args.operation == "log":
            return _log(worktree, args.ref)
        if args.operation == "diff":
            return _diff(worktree, args.base, args.head, args.format, args.path)
        if args.operation == "commit":
            return _show(worktree, args.ref, args.format, args.path)
        if args.operation == "contains":
            return _contains(worktree, args.sha)
    except (InspectError, OSError, UnicodeError) as exc:
        code = exc.returncode if isinstance(exc, InspectError) else 3
        print(f"review-inspect: {exc}", file=sys.stderr)
        return code
    raise AssertionError("unreachable operation")


if __name__ == "__main__":
    raise SystemExit(main())
