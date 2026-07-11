#!/usr/bin/env python3
"""Small numeric-only timing helpers for the deterministic daily pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from time import monotonic


def script_ms(started: float) -> float:
    """Return a stable non-negative process duration rounded for telemetry."""

    return round(max(0.0, (monotonic() - started) * 1000.0), 3)


def elapsed_since_iso_ms(value: object, *, fallback_ms: float) -> float:
    """Return wall elapsed time from an ISO timestamp, or a safe local fallback."""

    if not isinstance(value, str):
        return fallback_ms
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback_ms
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta_ms = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() * 1000.0
    return fallback_ms if delta_ms < 0 else round(delta_ms, 3)


__all__ = ["elapsed_since_iso_ms", "script_ms"]
