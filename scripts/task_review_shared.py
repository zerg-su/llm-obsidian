"""Shared typed values and owner-only IO for current review orchestration."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping, NamedTuple

from harness.review_program import ReviewBoundaryInput
from harness.workflows.review import ReviewContext, ReviewResult, ReviewRound
from harness.workflows.review_gate import ReviewGateRun


class TaskReviewError(ValueError):
    pass


class StaleRoundCallbackError(TaskReviewError):
    """A callback belonging to another round or verification iteration.

    This is a transport rejection, not a claim that the versioned payload
    schema itself was invalid.
    """


class ActiveReviewRound(NamedTuple):
    run: ReviewGateRun
    lane: object
    round: ReviewRound


class ResolutionBundle(NamedTuple):
    resolution: ReviewResolution
    fix_delta: bytes
    by_axis: Mapping[str, ReviewResolutionEvidence]
    review_identity_sha256: str
    origin_reviewed_head_sha: str = ""


class FinalizingRecovery(NamedTuple):
    context: ReviewContext
    context_manifest: Path
    marker_pointer: str
    marker_sha256: str
    response_receipt_path: Path
    response_receipt: Mapping[str, object]
    result: ReviewResult


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(value, encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_bytes(value)
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_json(path: Path, value: object) -> None:
    _atomic_text(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskReviewError(f"{label} is unavailable") from exc
    if not isinstance(value, dict):
        raise TaskReviewError(f"{label} must be an object")
    return value


def _load_review_boundary_input(
    path: Path, *, purpose: str
) -> ReviewBoundaryInput:
    path = path.expanduser().resolve()
    if not path.is_file() or path.is_symlink():
        raise TaskReviewError("review boundary input is unavailable")
    boundary = ReviewBoundaryInput.from_mapping(
        _read_json(path, "review boundary input")
    )
    if boundary.purpose != purpose:
        raise TaskReviewError("review boundary purpose does not match the request")
    return boundary


def _git(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise TaskReviewError("cannot resolve the exact product revision")
    return result.stdout.strip()


def _git_bytes(worktree: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise TaskReviewError("cannot build the exact review fix delta")
    return result.stdout
