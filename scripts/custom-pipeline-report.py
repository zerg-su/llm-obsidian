#!/usr/bin/env python3
"""Report content-free custom pipeline promotion candidates."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path


SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def report(path: Path, *, minimum_runs: int = 3) -> dict[str, object]:
    completed: Counter[str] = Counter()
    failed: Counter[str] = Counter()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        identifiers = event.get("identifiers") if isinstance(event, dict) else None
        if (
            not isinstance(identifiers, dict)
            or event.get("op") != "compiled-pipeline"
            or identifiers.get("compiler_outcome") != "custom-resolved"
        ):
            continue
        fingerprint = str(identifiers.get("definition_sha") or "")
        if not SHA256.fullmatch(fingerprint):
            continue
        if identifiers.get("terminal_category") == "complete":
            completed[fingerprint] += 1
        elif identifiers.get("attention_category") != "none":
            failed[fingerprint] += 1
    fingerprints = sorted(set(completed) | set(failed))
    rows = [
        {
            "definition_sha256": fingerprint,
            "completed_runs": completed[fingerprint],
            "attention_runs": failed[fingerprint],
            "promotion_candidate": (
                completed[fingerprint] >= minimum_runs
                and failed[fingerprint] == 0
            ),
        }
        for fingerprint in fingerprints
    ]
    return {
        "schema_version": 1,
        "minimum_completed_runs": minimum_runs,
        "fingerprint_count": len(rows),
        "promotion_candidate_count": sum(
            bool(row["promotion_candidate"]) for row in rows
        ),
        "pipelines": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--events",
        type=Path,
        default=Path(".vault-meta/pipeline-events.jsonl"),
    )
    parser.add_argument("--minimum-runs", type=int, default=3)
    args = parser.parse_args()
    if args.minimum_runs < 1:
        parser.error("--minimum-runs must be positive")
    print(
        json.dumps(
            report(args.events, minimum_runs=args.minimum_runs),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
