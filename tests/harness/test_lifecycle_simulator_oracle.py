#!/usr/bin/env python3
"""Independent mutation-sensitive lifecycle simulator oracle."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "harness"))

from lifecycle_simulator_oracle import (  # noqa: E402
    InvariantViolation,
    assert_snapshot,
    load_scenario,
)


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def violation_id(snapshot: dict[str, object]) -> str:
    try:
        assert_snapshot(snapshot)
    except InvariantViolation as exc:
        return exc.invariant_id
    raise AssertionError("mutated snapshot unexpectedly passed")


scenario_path = (
    ROOT
    / "tests"
    / "harness"
    / "lifecycle_scenarios"
    / "v2_6_5_rearm_liveness_latch.json"
)
scenario = load_scenario(scenario_path)
historical = scenario["initial_snapshot"]
check(
    "frozen stale-rearm snapshot fails by its declared first invariant",
    violation_id(historical) == scenario["expected_initial_invariant"],
)

aligned = copy.deepcopy(historical)
aligned["liveness"].update(
    {"operation_revision": 15, "operation_state": "awaiting-callback"}
)
aligned["recovery"]["status"] = "applied"
aligned["error_latch"]["active"] = False
assert_snapshot(aligned)
check("applied rearm aligns operation, liveness, and latch", True)

duplicate_effect = copy.deepcopy(aligned)
duplicate_effect["effects"][0]["deliveries"] = 2
check(
    "oracle catches duplicate irreversible delivery",
    violation_id(duplicate_effect) == "SIM-INV-EFFECT-ONCE",
)

duplicate_callback = copy.deepcopy(aligned)
duplicate_callback["callbacks"] = [
    {
        "callback_id": "review-callback",
        "identity_sha256": "c" * 64,
        "accepted": True,
    },
    {
        "callback_id": "late-copy",
        "identity_sha256": "c" * 64,
        "accepted": True,
    },
]
check(
    "oracle catches duplicate callback identity acceptance",
    violation_id(duplicate_callback) == "SIM-INV-CALLBACK-ONCE",
)

stale_identity = copy.deepcopy(aligned)
stale_identity["callbacks"] = [
    {
        "callback_id": "stale-callback",
        "identity_sha256": "d" * 64,
        "expected_identity_sha256": "e" * 64,
        "accepted": True,
    }
]
check(
    "oracle catches accepted wrong-identity artifacts",
    violation_id(stale_identity) == "SIM-INV-IDENTITY",
)

resurrected = copy.deepcopy(aligned)
resurrected["terminal_history"] = ["complete"]
resurrected["operation"]["state"] = "running"
resurrected["liveness"].update(
    {"operation_revision": 15, "operation_state": "running"}
)
check(
    "oracle catches terminal resurrection",
    violation_id(resurrected) == "SIM-INV-TERMINAL-MONOTONIC",
)

leaked = copy.deepcopy(aligned)
leaked["operation"].update(
    {
        "state": "complete",
        "resources": {
            "surface_id": "leaked-surface",
            "process_identity": "a" * 64,
            "supervisor_identity": "b" * 64,
        },
    }
)
leaked["liveness"].update(
    {"operation_revision": 15, "operation_state": "complete"}
)
leaked["terminal_history"] = ["complete"]
check(
    "oracle catches terminal resource leaks",
    violation_id(leaked) == "SIM-INV-RESOURCE-FREE",
)

encoded = json.dumps(scenario, sort_keys=True, separators=(",", ":"))
check(
    "scenario is content-free and bounded",
    scenario["max_steps"] <= 8
    and len(scenario["actions"]) <= scenario["max_steps"]
    and "prompt" not in encoded
    and "output" not in encoded,
)

print("\nAll lifecycle simulator oracle tests passed.")
