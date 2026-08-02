"""Shared value validation for persistent task sessions and cmux adapters."""

from __future__ import annotations

import re


SCHEMA_VERSION = 1
TASK_STATES = {"active", "archiving", "archived", "degraded"}
LANE_STATES = {"idle", "starting", "running", "callback-ready", "failed", "archived"}
OPERATION_STATES = {"queued", "starting", "running", "callback-ready", "complete", "failed"}
DOMAINS = {"normal", "review", "secure-fetch", "secure-synth"}
RUNTIMES = {"claude", "codex"}
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


class TaskSessionError(ValueError):
    """A task-session value or state violates its durable contract."""


def require_uuid(value: str, field: str) -> str:
    normalized = value.strip().lower()
    if UUID_RE.fullmatch(normalized) is None:
        raise TaskSessionError(f"{field} must be a UUID")
    return normalized


def require_token(value: str, field: str) -> str:
    normalized = value.strip()
    if SAFE_TOKEN_RE.fullmatch(normalized) is None:
        raise TaskSessionError(f"{field} is invalid")
    return normalized
