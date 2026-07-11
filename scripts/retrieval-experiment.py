#!/usr/bin/env python3
"""Evaluate contextual headers and lexical reranking without enabling them."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "scripts" / "retrieval-bench.py"
RETRIEVE = ROOT / "scripts" / "retrieve.py"
DEFAULT_REPORT = ROOT / ".vault-meta" / "retrieval-experiment.json"
NDCG_GAIN = 0.02
MAX_HIT5_DROP = 0.01
MAX_LATENCY_RATIO = 1.5
VARIANTS = {
    "baseline": {"RETRIEVAL_CONTEXTUAL_HEADERS": "0", "RETRIEVAL_RERANKER": "off"},
    "lexical": {"RETRIEVAL_CONTEXTUAL_HEADERS": "0", "RETRIEVAL_RERANKER": "lexical"},
    "contextual": {"RETRIEVAL_CONTEXTUAL_HEADERS": "1", "RETRIEVAL_RERANKER": "off"},
    "combined": {"RETRIEVAL_CONTEXTUAL_HEADERS": "1", "RETRIEVAL_RERANKER": "lexical"},
}


def run_variant(name: str) -> dict:
    env = dict(os.environ)
    env.update(VARIANTS[name])
    result = subprocess.run(
        [sys.executable, str(BENCH), "--json", "--sparse-only"],
        cwd=ROOT, env=env, text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{name}: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout)


def restore_production_index() -> None:
    env = dict(os.environ)
    env.update(VARIANTS["baseline"])
    subprocess.run(
        [sys.executable, str(RETRIEVE), "build", "--quiet"],
        cwd=ROOT, env=env, text=True, capture_output=True, check=False,
    )


def verdict(baseline: dict, candidate: dict) -> dict:
    before = baseline["summary"]["sparse"]["heldout"]
    after = candidate["summary"]["sparse"]["heldout"]
    gain = after["ndcg_at_10"] - before["ndcg_at_10"]
    hit_drop = before["hit_at_5"] - after["hit_at_5"]
    latency_ratio = after["p95_ms"] / before["p95_ms"] if before["p95_ms"] else 1.0
    promoted = gain >= NDCG_GAIN and hit_drop <= MAX_HIT5_DROP and latency_ratio <= MAX_LATENCY_RATIO
    return {
        "promoted": promoted,
        "heldout_ndcg_gain": gain,
        "heldout_hit5_drop": hit_drop,
        "p95_latency_ratio": latency_ratio,
        "thresholds": {
            "min_ndcg_gain": NDCG_GAIN,
            "max_hit5_drop": MAX_HIT5_DROP,
            "max_latency_ratio": MAX_LATENCY_RATIO,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    results = {}
    try:
        for name in VARIANTS:
            results[name] = run_variant(name)
        decisions = {name: verdict(results["baseline"], results[name]) for name in VARIANTS if name != "baseline"}
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"retrieval-experiment: {exc}", file=sys.stderr)
        return 3
    finally:
        restore_production_index()
    promoted = [name for name, value in decisions.items() if value["promoted"]]
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "production_default_changed": False,
        "recommendation": promoted[0] if promoted else "keep-current-rrf",
        "decisions": decisions,
        "metrics": {name: value["summary"]["sparse"]["heldout"] for name, value in results.items()},
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
