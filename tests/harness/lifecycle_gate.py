#!/usr/bin/env python3
"""Fast/extended lifecycle gate summaries and exact trace replay."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[2]
HISTORICAL = ROOT / "tests" / "harness" / "lifecycle_scenarios"
SWEEPS = ROOT / "tests" / "harness" / "lifecycle_sweeps"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "harness"))

from lifecycle_scheduler import compile_schedules, replay_trace, run_schedule  # noqa: E402
from lifecycle_historical import run_historical_schedule  # noqa: E402
from lifecycle_simulator import LifecycleWorld  # noqa: E402
from lifecycle_simulator_oracle import load_scenario  # noqa: E402


GATE_WALL_BUDGETS = {"fast": 60.0, "extended": 300.0}


def run_with_wall_budget(
    mode: str,
    callback: Callable[[], dict[str, int]],
    *,
    monotonic: Callable[[], float] = time.perf_counter,
) -> dict[str, int | float]:
    budget = GATE_WALL_BUDGETS[mode]
    started = monotonic()
    logical = callback()
    elapsed = monotonic() - started
    if elapsed < 0:
        raise RuntimeError("lifecycle gate monotonic clock moved backwards")
    if elapsed > budget:
        raise RuntimeError(
            f"{mode} lifecycle gate exceeded {budget:.1f}s wall budget: {elapsed:.6f}s"
        )
    return {**logical, "wall_seconds": round(elapsed, 6)}


def _scenario_paths() -> list[Path]:
    return sorted(HISTORICAL.glob("*.json")) + sorted(SWEEPS.glob("*.json"))


def _scenario(identity: str) -> dict[str, object]:
    loaded = [load_scenario(path) for path in _scenario_paths()]
    matches = [item for item in loaded if item["scenario_id"] == identity]
    if len(matches) != 1:
        raise ValueError("scenario identity is unknown or ambiguous")
    return matches[0]


def _historical_summary() -> dict[str, int]:
    scenarios = [load_scenario(path) for path in sorted(HISTORICAL.glob("*.json"))]
    if len(scenarios) != 7:
        raise RuntimeError("fast lifecycle corpus must contain exactly seven scenarios")
    schedules = 0
    actions = 0
    invariants: set[str] = set()
    for scenario in scenarios:
        compiled = compile_schedules(
            scenario,
            seed=int(scenario["seed"]),
            max_schedules=int(scenario.get("max_schedules", 64)),
        )
        for index, schedule in enumerate(compiled):
            with tempfile.TemporaryDirectory(
                prefix=f"lifecycle-fast-{scenario['scenario_id']}.{index}."
            ) as raw:
                execution = run_historical_schedule(
                    scenario, schedule, Path(raw)
                )
                if execution.state not in scenario["expected_terminal_states"]:
                    raise RuntimeError("historical lifecycle schedule did not converge")
                if execution.real_effects != {
                    "provider": 0,
                    "model": 0,
                    "cmux": 0,
                    "network": 0,
                }:
                    raise RuntimeError("historical schedule crossed a real effect boundary")
        schedules += len(compiled)
        actions += sum(len(item.actions) for item in compiled)
        invariants.add(str(scenario["expected_initial_invariant"]))
    return {
        "scenarios": len(scenarios),
        "schedules": schedules,
        "actions": actions,
        "invariants": len(invariants),
    }


def _extended_summary() -> dict[str, int]:
    summary = _historical_summary()
    initial_threads = threading.active_count()
    for path in sorted(SWEEPS.glob("*.json")):
        scenario = load_scenario(path)
        schedules = compile_schedules(
            scenario,
            seed=int(scenario["seed"]),
            max_schedules=int(scenario.get("max_schedules", 64)),
        )
        for schedule in schedules:
            with tempfile.TemporaryDirectory(prefix="lifecycle-extended.") as raw:
                world = run_schedule(
                    scenario,
                    schedule,
                    lambda raw=raw: LifecycleWorld.fresh(Path(raw)),
                )
                if world.record().state not in scenario["expected_terminal_states"]:
                    raise RuntimeError("extended schedule did not converge")
                if world.real_effect_counts() != {
                    "provider": 0,
                    "model": 0,
                    "cmux": 0,
                    "network": 0,
                }:
                    raise RuntimeError("extended schedule crossed a real effect boundary")
        summary["scenarios"] += 1
        summary["schedules"] += len(schedules)
        summary["actions"] += sum(len(item.actions) for item in schedules)
    if threading.active_count() != initial_threads:
        raise RuntimeError("lifecycle gate leaked a thread")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fast", "extended"), default="fast")
    parser.add_argument("--scenario")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--trace")
    args = parser.parse_args()
    replay_args = (args.scenario, args.seed, args.trace)
    if any(item is not None for item in replay_args):
        if any(item is None for item in replay_args):
            parser.error("--scenario, --seed, and --trace are required together")
        scenario = _scenario(str(args.scenario))
        schedule = replay_trace(
            scenario,
            seed=int(args.seed),
            trace_sha256=str(args.trace),
        )
        with tempfile.TemporaryDirectory(prefix="lifecycle-replay.") as raw:
            world = run_schedule(
                scenario,
                schedule,
                lambda: LifecycleWorld.fresh(Path(raw)),
            )
            payload = {
                "scenario": schedule.scenario_id,
                "seed": schedule.seed,
                "trace": schedule.trace_sha256,
                "actions": list(schedule.action_ids),
                "state": world.record().state,
                "real_effects": world.real_effect_counts(),
            }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0
    summary = run_with_wall_budget(
        args.mode,
        _historical_summary if args.mode == "fast" else _extended_summary,
    )
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
