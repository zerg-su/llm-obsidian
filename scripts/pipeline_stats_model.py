#!/usr/bin/env python3
"""Typed snapshots and aggregation for pipeline usage statistics."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field


KNOWN_RUNTIMES = frozenset({"claude", "codex", "unknown"})
LIFECYCLE_OPS = frozenset({
    "agent-run",
    "review-round-start",
    "review-callback",
    "review-round-complete",
    "review-round",
    "task-escalation",
    "surface-lifecycle",
    "task-complete",
})
AGENT_DRIVEN_OPS = LIFECYCLE_OPS | {"model-turn", "model-turn-incomplete"}
SKILL_CAPABLE_RUNTIMES = frozenset({"claude", "codex"})
SKILL_OBSERVABLE_RUNTIMES = frozenset({"claude"})


@dataclass
class StatsSnapshot:
    """Content-free counters collected from local runtime evidence."""

    days: int
    total_prompts: int = 0
    skills: set[str] = field(default_factory=set)
    runtime_activity: dict[str, int] = field(default_factory=dict)
    hint_runtimes: dict[str, set[str]] = field(default_factory=dict)
    typed_count: dict[str, int] = field(default_factory=dict)
    auto_count: dict[str, int] = field(default_factory=dict)
    last_used: dict[str, dt.datetime] = field(default_factory=dict)
    agent_count: dict[str, int] = field(default_factory=dict)
    hint_count: dict[str, int] = field(default_factory=dict)
    hint_followed: dict[str, int] = field(default_factory=dict)
    assist_count: dict[str, int] = field(default_factory=dict)
    assist_last: dict[str, dt.datetime] = field(default_factory=dict)
    operation_count: dict[tuple[str, str, str], int] = field(default_factory=dict)
    operation_last: dict[tuple[str, str, str], dt.datetime] = field(default_factory=dict)
    operation_durations: dict[tuple[str, str, str], list[float]] = field(default_factory=dict)
    turn_count: dict[tuple[str, str, str], int] = field(default_factory=dict)
    turn_durations: dict[tuple[str, str], list[float]] = field(default_factory=dict)
    lifecycle_events: list[dict] = field(default_factory=list)
    unattributed_agent_activity: int = 0


@dataclass(frozen=True)
class AggregatedStats:
    snapshot: StatsSnapshot
    totals: dict[str, int]
    used: list[tuple[str, int]]
    dead: list[str]
    bounds: dict[str, list[str]]


def event_count(record: dict, key: str) -> float:
    counts = record.get("counts")
    value = counts.get(key) if isinstance(counts, dict) else None
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
        and math.isfinite(value)
    ):
        return float(value)
    return 0.0


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def seconds(value: float | None) -> str:
    return "-" if value is None else f"{value / 1000:.1f}"


def normalize_runtime(value: object) -> str:
    """Clamp a seam's runtime tag to the known vocabulary."""
    runtime = str(value or "unknown")
    return runtime if runtime in KNOWN_RUNTIMES else "unknown"


def classify_zero_usage(
    zero_skills: list[str],
    hint_runtimes: dict[str, set[str]],
    runtime_activity: dict[str, int],
    unattributed_agent_activity: int = 0,
) -> dict[str, list[str]]:
    """Split zero-invocation skills by how far the evidence reaches."""
    uncovered = sorted(
        runtime
        for runtime, count in runtime_activity.items()
        if count > 0
        and runtime in SKILL_CAPABLE_RUNTIMES
        and runtime not in SKILL_OBSERVABLE_RUNTIMES
    )
    if unattributed_agent_activity > 0:
        uncovered.append("unattributed")
    uncovered_hint_runtimes = set(
        SKILL_CAPABLE_RUNTIMES - SKILL_OBSERVABLE_RUNTIMES
    )
    if unattributed_agent_activity > 0:
        uncovered_hint_runtimes.add("unknown")
    hinted = [
        name
        for name in zero_skills
        if (hint_runtimes.get(name) or set()) & uncovered_hint_runtimes
    ]
    hinted_set = set(hinted)
    return {
        "uncovered_runtimes": uncovered,
        "hinted_elsewhere": hinted,
        "dead": [name for name in zero_skills if name not in hinted_set],
    }


def aggregate_snapshot(snapshot: StatsSnapshot) -> AggregatedStats:
    totals = {
        name: snapshot.typed_count.get(name, 0) + snapshot.auto_count.get(name, 0)
        for name in set(snapshot.typed_count) | set(snapshot.auto_count)
    }
    used = sorted(totals.items(), key=lambda item: -item[1])
    dead = sorted(snapshot.skills - set(totals))
    bounds = classify_zero_usage(
        dead,
        snapshot.hint_runtimes,
        snapshot.runtime_activity,
        snapshot.unattributed_agent_activity,
    )
    return AggregatedStats(snapshot, totals, used, dead, bounds)
