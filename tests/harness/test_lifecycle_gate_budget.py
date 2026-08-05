#!/usr/bin/env python3
"""Measured lifecycle gate telemetry and hard wall-clock budgets."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "harness"))

from lifecycle_gate import run_with_wall_budget  # noqa: E402


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


logical = {"scenarios": 7, "schedules": 7, "actions": 33, "invariants": 7}
ticks = iter((100.0, 101.25))
measured = run_with_wall_budget("fast", lambda: logical, monotonic=lambda: next(ticks))
check(
    "gate telemetry contains measured wall time outside the stable logical summary",
    measured == {**logical, "wall_seconds": 1.25} and measured is not logical,
    measured,
)

ticks = iter((100.0, 160.000001))
try:
    run_with_wall_budget("fast", lambda: logical, monotonic=lambda: next(ticks))
except RuntimeError as exc:
    check(
        "an injected fast-gate budget overrun makes the gate red",
        "60.0" in str(exc),
        exc,
    )
else:
    raise AssertionError("fast lifecycle wall budget mutation unexpectedly passed")

ticks = iter((100.0, 400.000001))
try:
    run_with_wall_budget("extended", lambda: logical, monotonic=lambda: next(ticks))
except RuntimeError as exc:
    check(
        "an injected extended-gate budget overrun makes the gate red",
        "300.0" in str(exc),
        exc,
    )
else:
    raise AssertionError("extended lifecycle wall budget mutation unexpectedly passed")

print("\nAll lifecycle gate-budget tests passed.")
