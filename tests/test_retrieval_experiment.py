#!/usr/bin/env python3
"""Promotion-gate tests for retrieval experiments."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "retrieval_experiment", ROOT / "scripts" / "retrieval-experiment.py"
)
assert SPEC and SPEC.loader
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


def payload(*, ndcg: float, hit5: float, p95: float) -> dict:
    return {
        "summary": {
            "sparse": {
                "heldout": {
                    "ndcg_at_10": ndcg,
                    "hit_at_5": hit5,
                    "p95_ms": p95,
                }
            }
        }
    }


def main() -> None:
    baseline = payload(ndcg=0.50, hit5=0.90, p95=10.0)
    assert experiment.verdict(
        baseline, payload(ndcg=0.53, hit5=0.90, p95=14.0)
    )["promoted"]
    assert not experiment.verdict(
        baseline, payload(ndcg=0.51, hit5=0.90, p95=10.0)
    )["promoted"]
    assert not experiment.verdict(
        baseline, payload(ndcg=0.53, hit5=0.88, p95=10.0)
    )["promoted"]
    assert not experiment.verdict(
        baseline, payload(ndcg=0.53, hit5=0.90, p95=16.0)
    )["promoted"]
    print("All retrieval experiment tests passed.")


if __name__ == "__main__":
    main()
