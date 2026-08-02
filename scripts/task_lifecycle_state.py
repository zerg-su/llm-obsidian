"""Shared durable state and coordinator identity for task lifecycle actions."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from task_contract import v3_session_is_bound


_STATE_DIR: Path | None = None


def set_state_dir(path: Path | None) -> None:
    global _STATE_DIR
    _STATE_DIR = path


def state_dir() -> Path | None:
    return _STATE_DIR


def lifecycle_file(worktree: Path, name: str, kind: str = "reviewer") -> Path:
    if kind == "reviewer" and _STATE_DIR is not None:
        return _STATE_DIR / name
    return worktree / name


def reviewer_uses_broker_state(worktree: Path) -> bool:
    """Return whether reviewer state belongs to a v3 broker operation."""

    return _STATE_DIR is not None and _STATE_DIR.resolve() != worktree.resolve()


def reviewer_captures_checkpoint(worktree: Path) -> bool:
    """Retain resume state only when a later reviewer round can consume it."""

    return _STATE_DIR is None or reviewer_uses_broker_state(worktree)


def root_coordinator_reviewer(worktree: Path, kind: str) -> bool:
    """Recognize an explicit primary-checkout review lifecycle."""

    if (
        kind != "reviewer"
        or _STATE_DIR is None
        or _STATE_DIR.resolve() != worktree.resolve()
    ):
        return False
    try:
        return (
            read_json(lifecycle_file(worktree, ".review-meta.json")).get(
                "archive_mode"
            )
            == "coordinator"
        )
    except SystemExit:
        return False


def die(message: str, code: int = 2) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        die(f"cannot read {path}: {exc}")
    if not isinstance(data, dict):
        die(f"{path} must contain an object")
    return data


def write_marker(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def current_session_id() -> str:
    acceptance = str(os.environ.get("LLM_OBSIDIAN_ACCEPTANCE") or "") == "1"
    acceptance_session = str(
        os.environ.get("LLM_OBSIDIAN_ACCEPTANCE_SESSION_ID") or ""
    ).strip()
    if acceptance and re.fullmatch(r"[A-Za-z0-9._:-]+", acceptance_session):
        return acceptance_session
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "unknown"


def require_origin_session(worktree: Path, supplied: str = "") -> None:
    meta = read_json(worktree / ".task-meta.json")
    origin = str(meta.get("origin_session") or "")
    actual = current_session_id()
    if meta.get("version") in {3, 4}:
        valid = actual != "unknown" and v3_session_is_bound(meta, actual)
    else:
        valid = actual != "unknown" and bool(origin) and actual == origin
    if not valid or (supplied and supplied != actual):
        die("only the originating coordinator session may finalize or close this task", 3)
