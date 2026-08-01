#!/usr/bin/env python3
"""Content-free, best-effort telemetry for harness-owned review rounds."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from lifecycle_telemetry import elapsed_ms, emit_lifecycle_event
from review_contract import SEVERITIES


REVIEW_EVENT_OPS = frozenset(
    {"review-round-start", "review-callback", "review-round-complete"}
)


def severity_counts(severities: Iterable[str]) -> dict[str, int]:
    counts = {severity: 0 for severity in sorted(SEVERITIES)}
    total = 0
    for severity in severities:
        if severity not in counts:
            continue
        counts[severity] += 1
        total += 1
    return {
        "findings": total,
        **{
            f"{severity}_findings": counts[severity]
            for severity in sorted(SEVERITIES)
        },
    }


def emit_review_event(
    worktree: Path,
    vault_root: Path,
    *,
    event: str,
    axis: str,
    reviewer_runtime: str,
    iteration: int,
    terminal_status: str,
    started_at: str,
    severities: Iterable[str] = (),
) -> bool:
    """Emit only enums, counters, and timing; never review content."""

    try:
        if event not in REVIEW_EVENT_OPS:
            return False
        counts: dict[str, int] = {"iteration": max(0, int(iteration))}
        if event == "review-round-start":
            counts["rounds_started"] = 1
        else:
            duration = elapsed_ms(started_at)
            if duration is not None:
                counts["duration_ms"] = duration
        if event == "review-callback":
            key = (
                "accepted_callbacks"
                if terminal_status == "accepted"
                else "rejected_callbacks"
            )
            counts[key] = 1
        if event == "review-round-complete":
            counts.update(severity_counts(severities))
            counts["rounds_completed"] = 1
        return emit_lifecycle_event(
            worktree,
            event,
            actor="review",
            identifiers={
                "axis": axis,
                "reviewer_runtime": reviewer_runtime,
                "terminal_status": terminal_status,
            },
            counts=counts,
            status="error" if terminal_status == "rejected" else "ok",
            vault_root=vault_root,
        )
    except (OSError, OverflowError, RuntimeError, TypeError, ValueError):
        return False


__all__ = [
    "REVIEW_EVENT_OPS",
    "SEVERITIES",
    "emit_review_event",
    "severity_counts",
]
