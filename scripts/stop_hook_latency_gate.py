#!/usr/bin/env python3
"""Deterministic, jitter-resistant Stop-hook latency SLO measurement."""

from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


SLO_SECONDS = 1.0
WARMUP_SAMPLES = 1
MEASURED_SAMPLES = 7
JITTER_OUTLIERS = 1


@dataclass(frozen=True)
class LatencyAssessment:
    schema_version: int
    status: str
    slo_s: float
    warmup_samples: int
    measured_samples: int
    jitter_outliers: int
    sustained_upper_s: float

    @property
    def passed(self) -> bool:
        return self.status == "passed"


def measure_monotonic(
    callback: Callable[[], object],
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> float:
    """Measure one sample with a monotonic clock and reject clock regression."""

    started = monotonic()
    callback()
    elapsed = monotonic() - started
    if not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("Stop-hook benchmark monotonic clock regressed")
    return elapsed


def assess_samples(samples: list[float]) -> LatencyAssessment:
    """Discard one warm-up and classify sustained latency under the fixed SLO."""

    expected = WARMUP_SAMPLES + MEASURED_SAMPLES
    if len(samples) != expected:
        raise ValueError(f"Stop-hook benchmark requires exactly {expected} samples")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        for value in samples
    ):
        raise ValueError("Stop-hook benchmark samples must be finite non-negative numbers")
    measured = sorted(float(value) for value in samples[WARMUP_SAMPLES:])
    sustained_upper = measured[-(JITTER_OUTLIERS + 1)]
    return LatencyAssessment(
        1,
        "passed" if sustained_upper < SLO_SECONDS else "failed",
        SLO_SECONDS,
        WARMUP_SAMPLES,
        MEASURED_SAMPLES,
        JITTER_OUTLIERS,
        round(sustained_upper, 6),
    )


def _recent_dirty_samples(path: Path) -> list[float]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Stop-hook latency telemetry must be a regular file")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    expected = WARMUP_SAMPLES + MEASURED_SAMPLES
    recent = rows[-expected:]
    if len(recent) != expected or any(
        not isinstance(row, dict)
        or row.get("wiki_dirty") != 1
        or row.get("commit_blocked") != 0
        for row in recent
    ):
        raise ValueError("Stop-hook benchmark window is incomplete or not comparable")
    return [row.get("total_s") for row in recent]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: stop_hook_latency_gate.py LATENCY_JSONL", file=sys.stderr)
        return 2
    try:
        result = assess_samples(_recent_dirty_samples(Path(argv[0])))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Stop-hook latency gate: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(result), sort_keys=True, separators=(",", ":")))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
