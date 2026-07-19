#!/usr/bin/env python3
"""Privacy-bounded, runtime-neutral operation telemetry.

Events contain no prompt, query, command, page body, snippet, error text, or
arbitrary string metadata. Only safe identifiers, relative paths, and numeric
counters are accepted.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Optional, Union

ROOT = Path(__file__).resolve().parents[1]
EVENT_NAME = "pipeline-events.jsonl"
LOCK_NAME = ".pipeline-events.lock"
ROTATE_BYTES = 1_048_576
TOKEN_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
COUNT_KEY_RX = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
STATUSES = {"ok", "degraded", "error", "noop", "conflict"}


def runtime_name(environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    if env.get("CODEX_THREAD_ID") or env.get("CODEX_CI") or env.get("CODEX_MANAGED_BY_NPM"):
        return "codex"
    if env.get("CLAUDE_CODE_SESSION_ID") or env.get("CLAUDE_PROJECT_DIR"):
        return "claude"
    return "unknown"


def safe_token(value: object, fallback: str) -> str:
    raw = str(value or "").strip()
    if TOKEN_RX.fullmatch(raw):
        return raw
    if not raw:
        return fallback
    return "sha256:" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def session_name(explicit: object = None, environ: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    raw = explicit or env.get("CODEX_THREAD_ID") or env.get("CLAUDE_CODE_SESSION_ID") or "unknown"
    return safe_token(raw, "unknown")


def safe_path(value: object) -> Optional[str]:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or len(raw) > 240 or any(ord(char) < 32 for char in raw):
        return None
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or raw.startswith("~"):
        return None
    if not (raw.startswith("wiki/") or raw == ".raw/.manifest.json"):
        return None
    return path.as_posix()


def safe_counts(counts: Optional[Mapping[str, object]]) -> dict[str, Union[float, int]]:
    clean: dict[str, Union[float, int]] = {}
    for index, (key, value) in enumerate((counts or {}).items()):
        if index >= 32:
            break
        if not COUNT_KEY_RX.fullmatch(str(key)) or isinstance(value, bool):
            raise ValueError("telemetry counters need safe keys and numeric values")
        try:
            valid_number = isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
        except OverflowError:
            valid_number = False
        if not valid_number:
            raise ValueError("telemetry counters need safe keys and numeric values")
        clean[str(key)] = value
    return clean


def build_event(
    op: object,
    *,
    actor: object = None,
    session: object = None,
    paths: Optional[Iterable[object]] = None,
    counts: Optional[Mapping[str, object]] = None,
    status: str = "ok",
    environ: Optional[Mapping[str, str]] = None,
) -> dict:
    if status not in STATUSES:
        raise ValueError(f"unsupported telemetry status: {status}")
    clean_paths: list[str] = []
    for value in paths or []:
        path = safe_path(value)
        if path and path not in clean_paths:
            clean_paths.append(path)
        if len(clean_paths) >= 20:
            break
    return {
        "schema": 1,
        "ts": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime_name(environ),
        "session": session_name(session, environ),
        "actor": safe_token(actor, "unknown"),
        "op": safe_token(op, "unknown"),
        "status": status,
        "paths": clean_paths,
        "counts": safe_counts(counts),
    }


def emit_event(
    op: object,
    *,
    actor: object = None,
    session: object = None,
    paths: Optional[Iterable[object]] = None,
    counts: Optional[Mapping[str, object]] = None,
    status: str = "ok",
    root: Path = ROOT,
    environ: Optional[Mapping[str, str]] = None,
) -> bool:
    """Append one event; telemetry failure never changes the primary operation."""

    try:
        event = build_event(
            op,
            actor=actor,
            session=session,
            paths=paths,
            counts=counts,
            status=status,
            environ=environ,
        )
        meta = root / ".vault-meta"
        meta.mkdir(parents=True, exist_ok=True)
        log = meta / EVENT_NAME
        rotated = meta / (EVENT_NAME + ".1")
        lock = meta / LOCK_NAME
        with lock.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            if log.is_file() and log.stat().st_size >= ROTATE_BYTES:
                os.replace(log, rotated)
            with log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except (OSError, OverflowError, TypeError, ValueError):
        return False


__all__ = ["build_event", "emit_event", "runtime_name", "safe_path", "safe_token"]
