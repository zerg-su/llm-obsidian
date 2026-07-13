#!/usr/bin/env python3
"""Best-effort, content-free telemetry helpers for dispatched task lifecycle."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from pipeline_events import emit_event


def nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def origin_vault(worktree: Path) -> Path | None:
    """Resolve the coordinator vault without recording the private absolute path."""

    root = worktree.expanduser().resolve()
    for marker_name in (".task-reap-complete.json", ".task-reap-prepared.json"):
        marker = read_object(root / marker_name)
        raw_vault = str(marker.get("vault_root") or "").strip()
        if raw_vault:
            candidate = Path(raw_vault).expanduser().resolve()
            if (candidate / "wiki").is_dir():
                return candidate

    meta = read_object(root / ".task-meta.json")
    raw_plan = str(meta.get("plan_file") or "").strip()
    if not raw_plan:
        return None
    plan = Path(raw_plan).expanduser().resolve()
    if plan.parent.name != "plans" or plan.parent.parent.name != "wiki":
        return None
    candidate = plan.parents[2]
    return candidate if (candidate / "wiki").is_dir() else None


def parse_utc(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def elapsed_ms(started_at: object, ended_at: object = None) -> int | None:
    started = parse_utc(started_at)
    ended = parse_utc(ended_at) if ended_at is not None else datetime.now(timezone.utc)
    if started is None or ended is None:
        return None
    elapsed = (ended - started).total_seconds() * 1000
    return max(0, round(elapsed))


def numeric_counts(values: Mapping[str, object] | None) -> dict[str, int | float]:
    clean: dict[str, int | float] = {}
    for key, value in (values or {}).items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            valid = math.isfinite(value) and value >= 0
        except OverflowError:
            valid = False
        if valid:
            clean[str(key)] = value
    return clean


def emit_lifecycle_event(
    worktree: Path,
    op: str,
    *,
    actor: str,
    counts: Mapping[str, object] | None = None,
    status: str = "ok",
    vault_root: Path | None = None,
) -> bool:
    """Emit to the origin vault; missing telemetry context is always non-fatal."""

    try:
        root = vault_root.expanduser().resolve() if vault_root is not None else origin_vault(worktree)
        if root is None:
            return False
        meta = read_object(worktree / ".task-meta.json")
        return emit_event(
            op,
            actor=actor,
            session=meta.get("origin_session"),
            counts=numeric_counts(counts),
            status=status,
            root=root,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


__all__ = [
    "elapsed_ms",
    "emit_lifecycle_event",
    "nonnegative_int",
    "numeric_counts",
    "origin_vault",
    "parse_utc",
    "read_object",
]
