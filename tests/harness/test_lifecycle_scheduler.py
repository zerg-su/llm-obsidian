#!/usr/bin/env python3
"""Bounded deterministic lifecycle schedule compilation and exact replay."""

from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "harness"))

from lifecycle_scheduler import (  # noqa: E402
    ScheduleFailure,
    compile_schedules,
    replay_trace,
    run_schedule,
    schedule_summary,
)
from lifecycle_simulator import LifecycleWorld  # noqa: E402


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


scenario = {
    "schema_version": 1,
    "scenario_id": "scheduler-pairwise",
    "seed": 71,
    "max_steps": 4,
    "expected_initial_invariant": "SIM-INV-SCHEMA",
    "expected_terminal_states": ["created"],
    "forbidden_effects": [],
    "initial_snapshot": {},
    "actions": [
        {"action_id": "clock", "action": "advance-clock", "delta": 1},
        {"action_id": "tick", "action": "worker-tick"},
        {
            "action_id": "liveness",
            "action": "publish-liveness",
            "after": ["clock", "tick"],
        },
    ],
}

first = compile_schedules(scenario, seed=71, max_schedules=16)
second = compile_schedules(copy.deepcopy(scenario), seed=71, max_schedules=16)
check(
    "same scenario and seed compile byte-identical schedules",
    [item.canonical_bytes() for item in first]
    == [item.canonical_bytes() for item in second],
)
check(
    "bounded enumeration covers both orders of an independent pair",
    {tuple(item.action_ids[:2]) for item in first}
    == {("clock", "tick"), ("tick", "clock")},
    [item.action_ids for item in first],
)
check(
    "every schedule preserves declared dependencies",
    all(item.action_ids[-1] == "liveness" for item in first),
)

different = compile_schedules(scenario, seed=72, max_schedules=16)
check(
    "different seed changes deterministic traversal without changing coverage",
    [item.action_ids for item in different] != [item.action_ids for item in first]
    and {item.action_ids for item in different} == {item.action_ids for item in first},
)

selected = first[-1]
replayed = replay_trace(scenario, seed=71, trace_sha256=selected.trace_sha256)
check(
    "exact trace identity replays the same ordered actions",
    replayed.canonical_bytes() == selected.canonical_bytes(),
)

summary_a = schedule_summary(first, invariants=7, actions=6)
summary_b = schedule_summary(second, invariants=7, actions=6)
check(
    "deterministic summary is stable and reports virtual wall time",
    summary_a == summary_b
    and summary_a
    == {
        "scenarios": 1,
        "schedules": 2,
        "actions": 6,
        "invariants": 7,
        "wall_seconds": 0.0,
    },
    summary_a,
)

failure_scenario = {
    **scenario,
    "scenario_id": "scheduler-first-failure",
    "max_steps": 2,
    "actions": [
        {"action_id": "start", "action": "start-worker"},
        {
            "action_id": "stale-live",
            "action": "publish-liveness",
            "revision": 0,
            "state": "created",
            "after": ["start"],
        },
    ],
}
failure_schedule = compile_schedules(failure_scenario, seed=19)[0]
with tempfile.TemporaryDirectory(prefix="scheduler-failure.") as raw:
    try:
        run_schedule(
            failure_scenario,
            failure_schedule,
            lambda: LifecycleWorld.fresh(Path(raw)),
        )
    except ScheduleFailure as exc:
        check(
            "first violated invariant binds scenario, trace, and bad action",
            exc.scenario_id == "scheduler-first-failure"
            and exc.trace_sha256 == failure_schedule.trace_sha256
            and exc.action_id == "stale-live"
            and exc.action_index == 1
            and exc.invariant_id == "SIM-INV-OP-LIVENESS",
            exc,
        )
    else:
        raise AssertionError("injected invariant failure unexpectedly passed")

print("\nAll lifecycle scheduler tests passed.")
