#!/usr/bin/env python3
"""Content-free model-turn timing shared by Claude and Codex hooks."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

from pipeline_events import emit_event, safe_token


MARKER_DIR = "turn-markers"


def exact_session(data: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    raw = str(
        data.get("session_id")
        or env.get("CODEX_THREAD_ID")
        or env.get("CLAUDE_CODE_SESSION_ID")
        or ""
    ).strip()
    return raw


def runtime_environ(data: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    runtime = str(data.get("runtime") or "").strip().lower()
    if runtime == "codex":
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.pop("CLAUDE_PROJECT_DIR", None)
        if not env.get("CODEX_THREAD_ID"):
            env["CODEX_CI"] = "1"
    elif runtime == "claude":
        env.pop("CODEX_THREAD_ID", None)
        env.pop("CODEX_CI", None)
        env.pop("CODEX_MANAGED_BY_NPM", None)
        if not env.get("CLAUDE_CODE_SESSION_ID"):
            env["CLAUDE_CODE_SESSION_ID"] = exact_session(data, env) or "hook"
    return env


def marker_path(root: Path, session: str) -> Path:
    token = safe_token(session, "missing")
    return root / ".vault-meta" / MARKER_DIR / f"{token}.json"


def read_marker(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def role_for(data: Mapping[str, Any], *, context_root: Path | None = None) -> str:
    explicit = str(os.environ.get("LLM_OBSIDIAN_SESSION_ROLE") or data.get("session_role") or "").strip()
    if explicit in {"coordinator", "task", "reviewer"}:
        return explicit
    if context_root is not None:
        for candidate in (context_root, *context_root.parents):
            if (candidate / ".task-meta.json").is_file():
                return "task"
    return "coordinator"


def emit_incomplete(root: Path, session: str, marker: Mapping[str, Any], data: Mapping[str, Any]) -> None:
    counts: dict[str, int] = {}
    started_ms = marker.get("started_ms")
    if isinstance(started_ms, int) and not isinstance(started_ms, bool):
        counts["duration_ms"] = max(0, round(time.time() * 1000) - started_ms)
    emit_event(
        "model-turn-incomplete",
        actor=marker.get("actor") or "coordinator",
        session=session,
        counts=counts,
        status="degraded",
        root=root,
        environ=runtime_environ(data),
    )


def clear_stale(root: Path, data: Mapping[str, Any]) -> bool:
    """Close one exact session's stale marker; missing identity is a no-op."""

    session = exact_session(data)
    if not session:
        return False
    path = marker_path(root, session)
    if not path.exists():
        return False
    marker = read_marker(path)
    emit_incomplete(root, session, marker, data)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def start_turn(root: Path, data: Mapping[str, Any], *, context_root: Path | None = None) -> bool:
    """Start one turn, degrading a prior unmatched marker for this session."""

    session = exact_session(data)
    if not session:
        return False
    path = marker_path(root, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        marker = read_marker(path)
        emit_incomplete(root, session, marker, data)
    marker = {
        "schema": 1,
        "session": safe_token(session, "missing"),
        "actor": role_for(data, context_root=context_root),
        "started_ms": round(time.time() * 1000),
    }
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def finish_turn(root: Path, data: Mapping[str, Any]) -> bool:
    """Emit duration before Stop work; absent session/marker is a no-op."""

    session = exact_session(data)
    if not session:
        return False
    path = marker_path(root, session)
    marker = read_marker(path)
    started_ms = marker.get("started_ms")
    if not marker or not isinstance(started_ms, int) or isinstance(started_ms, bool):
        return False
    duration_ms = max(0, round(time.time() * 1000) - started_ms)
    emitted = emit_event(
        "model-turn",
        actor=marker.get("actor") or "coordinator",
        session=session,
        counts={"duration_ms": duration_ms},
        root=root,
        environ=runtime_environ(data),
    )
    if emitted:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False
    return emitted


__all__ = ["clear_stale", "exact_session", "finish_turn", "marker_path", "role_for", "start_turn"]
