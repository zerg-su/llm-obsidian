#!/usr/bin/env python3
"""Two-pass historical lifecycle corpus over frozen RED and real-core GREEN paths."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCENARIO_ROOT = ROOT / "tests" / "harness" / "lifecycle_scenarios"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "harness"))

from lifecycle_scheduler import compile_schedules, replay_trace  # noqa: E402
from lifecycle_historical import run_historical_schedule  # noqa: E402
from lifecycle_simulator_oracle import (  # noqa: E402
    InvariantViolation,
    assert_snapshot,
    load_scenario,
)


EXPECTED_SCENARIOS = {
    "d264-73-mixed-head-terminal-boundary",
    "d265-stale-exiting-resource-gone",
    "d265-duplicate-late-callback",
    "d265-callback-timeout-completion-collision",
    "d265-reap-complete-pending-effect",
    "d265-partial-summary-publication",
    "v2-6-5-rearm-liveness-latch",
}


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def expect_declared_red(scenario: dict[str, object]) -> str:
    try:
        assert_snapshot(scenario["initial_snapshot"])
    except InvariantViolation as exc:
        if exc.invariant_id != scenario["expected_initial_invariant"]:
            raise AssertionError(
                f"{scenario['scenario_id']} expected {scenario['expected_initial_invariant']} "
                f"but failed by {exc.invariant_id}"
            ) from exc
        return exc.invariant_id
    raise AssertionError(f"{scenario['scenario_id']} historical fixture is not RED")


def run_pass() -> dict[str, object]:
    paths = sorted(SCENARIO_ROOT.glob("*.json"))
    scenarios = [load_scenario(path) for path in paths]
    identities = {str(item["scenario_id"]) for item in scenarios}
    if identities != EXPECTED_SCENARIOS:
        raise AssertionError(f"historical lifecycle corpus drifted: {sorted(identities)}")

    digests: dict[str, str] = {}
    traces: list[str] = []
    red_invariants: set[str] = set()
    action_count = 0
    schedule_count = 0
    for scenario in scenarios:
        scenario_id = str(scenario["scenario_id"])
        digests[scenario_id] = canonical_sha256(scenario)
        red_invariants.add(expect_declared_red(scenario))
        schedules = compile_schedules(
            scenario,
            seed=int(scenario["seed"]),
            max_schedules=int(scenario.get("max_schedules", 64)),
        )
        schedule_count += len(schedules)
        action_count += sum(len(item.actions) for item in schedules)
        for index, schedule in enumerate(schedules):
            with tempfile.TemporaryDirectory(
                prefix=f"lifecycle-corpus-{scenario_id}.{index}."
            ) as raw:
                execution = run_historical_schedule(
                    scenario, schedule, Path(raw)
                )
                if execution.state not in scenario["expected_terminal_states"]:
                    raise AssertionError(
                        f"{scenario_id} ended in undeclared state {execution.state}"
                    )
                forbidden = set(scenario["forbidden_effects"])
                if forbidden & execution.effect_ids:
                    raise AssertionError(f"{scenario_id} emitted a forbidden effect")
                if execution.real_effects != {
                    "provider": 0,
                    "model": 0,
                    "cmux": 0,
                    "network": 0,
                }:
                    raise AssertionError(f"{scenario_id} crossed a real effect boundary")
                replayed = replay_trace(
                    scenario,
                    seed=schedule.seed,
                    trace_sha256=schedule.trace_sha256,
                )
                if replayed.canonical_bytes() != schedule.canonical_bytes():
                    raise AssertionError(f"{scenario_id} exact trace replay drifted")
                traces.append(schedule.trace_sha256)

    return {
        "scenarios": len(scenarios),
        "schedules": schedule_count,
        "actions": action_count,
        "invariants": len(red_invariants),
        "fixture_sha256": digests,
        "trace_sha256": traces,
    }


first = run_pass()
second = run_pass()
check("historical corpus contains exactly seven declared scenarios", first["scenarios"] == 7)
check("two-pass corpus replay is byte-identical", first == second, (first, second))
check(
    "every historical class is RED by its named invariant and GREEN afterward",
    first["invariants"] == 7 and first["schedules"] == 7,
    first,
)

ledger = (ROOT / "docs" / "acceptance" / "v2.6.5-causal-ledger.md").read_text(
    encoding="utf-8"
)
check(
    "causal ledger binds every machine-readable scenario identity",
    all(identity in ledger for identity in EXPECTED_SCENARIOS),
)

source = SCENARIO_ROOT / "d265_partial_summary_publication.json"
mutated_raw = source.read_text(encoding="utf-8").replace(
    "SIM-INV-ATOMIC-PUBLICATION", "SIM-INV-ATOMIC-PUBLICATIOX", 1
)
with tempfile.TemporaryDirectory(prefix="lifecycle-fixture-mutation.") as raw:
    mutated_path = Path(raw) / source.name
    mutated_path.write_text(mutated_raw, encoding="utf-8")
    mutated = load_scenario(mutated_path)
    mutation_detected = canonical_sha256(mutated) != first["fixture_sha256"][
        "d265-partial-summary-publication"
    ]
    try:
        expect_declared_red(mutated)
    except AssertionError:
        mutation_detected = mutation_detected and True
    else:
        mutation_detected = False
check("one-byte fixture mutation is detected by digest plus oracle identity", mutation_detected)

print(json.dumps({key: first[key] for key in ("scenarios", "schedules", "actions", "invariants")}, sort_keys=True, separators=(",", ":")))
print("\nAll lifecycle regression-corpus tests passed.")
