#!/usr/bin/env python3
"""Deterministic policy checks for the Stop-hook latency SLO gate."""

from __future__ import annotations

import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from stop_hook_latency_gate import (  # noqa: E402
    JITTER_OUTLIERS,
    MEASURED_SAMPLES,
    SLO_SECONDS,
    WARMUP_SAMPLES,
    assess_samples,
    measure_monotonic,
)


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


check(
    "Stop-hook benchmark keeps one exact warm-up and seven samples",
    WARMUP_SAMPLES == 1
    and MEASURED_SAMPLES == 7
    and JITTER_OUTLIERS == 1,
)
check("Stop-hook product SLO remains exactly one second", SLO_SECONDS == 1.0)

warmup_spike = assess_samples([4.0, *([0.2] * MEASURED_SAMPLES)])
check(
    "deterministic warm-up is excluded from the measured window",
    warmup_spike.passed and warmup_spike.sustained_upper_s == 0.2,
    warmup_spike,
)

one_jitter_spike = assess_samples(
    [0.2, *([0.2] * (MEASURED_SAMPLES - 1)), 1.4]
)
check(
    "one scheduler-jitter outlier does not masquerade as sustained regression",
    one_jitter_spike.passed and one_jitter_spike.sustained_upper_s == 0.2,
    one_jitter_spike,
)

at_slo = assess_samples(
    [0.2, *([0.2] * (MEASURED_SAMPLES - 2)), 1.0, 1.0]
)
check(
    "two samples at the strict SLO boundary fail closed",
    not at_slo.passed and at_slo.sustained_upper_s == SLO_SECONDS,
    at_slo,
)

real_delay = measure_monotonic(lambda: time.sleep(SLO_SECONDS + 0.01))
injected_delay = assess_samples(
    [
        0.2,
        *([0.2] * (MEASURED_SAMPLES - 2)),
        real_delay,
        real_delay,
    ]
)
check(
    "an injected real sustained delay still fails the unchanged SLO",
    real_delay >= SLO_SECONDS and not injected_delay.passed,
    injected_delay,
)

for label, values in (
    ("missing sample", [0.2] * (WARMUP_SAMPLES + MEASURED_SAMPLES - 1)),
    ("non-finite sample", [0.2] * 7 + [float("nan")]),
    ("negative sample", [0.2] * 7 + [-0.1]),
):
    try:
        assess_samples(values)
    except ValueError:
        check(f"latency gate rejects {label}", True)
    else:
        raise AssertionError(f"latency gate accepted {label}")

print("\nAll Stop-hook latency gate tests passed.")
