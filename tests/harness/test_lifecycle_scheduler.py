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

coverage_scenario = {
    **scenario,
    "scenario_id": "scheduler-three-way-pair-coverage",
    "max_steps": 3,
    "actions": [
        {"action_id": "a", "action": "advance-clock", "delta": 1},
        {"action_id": "b", "action": "advance-clock", "delta": 2},
        {"action_id": "c", "action": "advance-clock", "delta": 3},
    ],
}
coverage = compile_schedules(
    coverage_scenario,
    seed=71,
    max_schedules=2,
)
pair_orientations = {
    pair: {
        order.index(pair[0]) < order.index(pair[1])
        for order in (item.action_ids for item in coverage)
    }
    for pair in (("a", "b"), ("a", "c"), ("b", "c"))
}
check(
    "the schedule ceiling cannot starve an independent pair orientation",
    len(coverage) == 2
    and all(orientations == {False, True} for orientations in pair_orientations.values()),
    {
        "orders": [item.action_ids for item in coverage],
        "pair_orientations": pair_orientations,
    },
)

for bounded_scenario, bound, value in (
    (coverage_scenario, "max_depth", 2),
    (scenario, "max_waves", 1),
):
    try:
        compile_schedules(
            {**bounded_scenario, bound: value},
            seed=71,
            max_schedules=2,
        )
    except ValueError:
        check(f"scheduler enforces the declared {bound} ceiling", True)
    else:
        raise AssertionError(f"scheduler ignored the declared {bound} ceiling")

different = compile_schedules(
    coverage_scenario,
    seed=72,
    max_schedules=2,
)
check(
    "different seed changes selection before truncation without losing coverage",
    [item.action_ids for item in different]
    != [item.action_ids for item in coverage]
    and all(
        {
            order.index(pair[0]) < order.index(pair[1])
            for order in (item.action_ids for item in different)
        }
        == {False, True}
        for pair in (("a", "b"), ("a", "c"), ("b", "c"))
    ),
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
    "deterministic logical summary is stable and excludes wall telemetry",
    summary_a == summary_b
    and summary_a
    == {
        "scenarios": 1,
        "schedules": 2,
        "actions": 6,
        "invariants": 7,
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
