#!/usr/bin/env python3
"""Validate model-produced daily-summary-v1 JSON before storing it privately."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from time import monotonic

from daily_contract import DailyContractError, atomic_private_json, load_json, validate_evidence, validate_summary
from daily_timing import elapsed_since_iso_ms, script_ms
from pipeline_events import emit_event


ROOT = Path(os.environ.get("LLM_OBSIDIAN_ROOT") or Path(__file__).resolve().parents[1]).resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = monotonic()
    evidence: dict | None = None
    try:
        evidence = validate_evidence(load_json(args.evidence))
        raw = json.load(sys.stdin)
        summary = validate_summary(raw, evidence)
        atomic_private_json(args.output, summary)
        local_ms = script_ms(started)
        emit_event(
            "daily-synthesis",
            actor="daily-summary-save",
            counts={
                "duration_ms": elapsed_since_iso_ms(evidence["generated_at"], fallback_ms=local_ms),
                "script_ms": local_ms,
                "bullets": len(summary["bullets"]),
            },
            root=ROOT,
        )
        print(args.output)
        return 0
    except (DailyContractError, OSError, json.JSONDecodeError) as exc:
        local_ms = script_ms(started)
        generated_at = evidence.get("generated_at") if evidence else None
        emit_event(
            "daily-synthesis",
            actor="daily-summary-save",
            counts={
                "duration_ms": elapsed_since_iso_ms(generated_at, fallback_ms=local_ms),
                "script_ms": local_ms,
                "exit_code": 3,
            },
            status="error",
            root=ROOT,
        )
        print(f"daily-summary-save: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
