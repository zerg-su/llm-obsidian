#!/usr/bin/env python3
"""Deterministic contract and comparison tests for the 2.6 paired evals."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "paired-evals.py"
MANIFEST = ROOT / "evals" / "paired-v2.6.0" / "manifest.json"
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location("paired_evals", SCRIPT)
assert spec is not None and spec.loader is not None
paired_evals = importlib.util.module_from_spec(spec)
spec.loader.exec_module(paired_evals)


def check(label: str, condition: bool) -> None:
    if not condition:
        raise SystemExit(f"FAIL {label}")
    print(f"OK   {label}")


manifest = paired_evals.load_manifest(MANIFEST, root=ROOT)
check("fixture set has two cases", tuple(manifest["case_ids"]) == ("fix", "design"))
check("fixture digest is stable", len(manifest["fixture_set_sha256"]) == 64)
check(
    "baseline and post plans keep exact contract identity",
    all(case["baseline_plan_sha256"] != case["post_plan_sha256"] for case in manifest["cases"])
    and all(case["contract_sha256"] for case in manifest["cases"]),
)
check(
    "goal-misaligned sentinel is frozen",
    sum(bool(case["goal_misaligned_sentinel"]) for case in manifest["cases"]) == 1,
)


def report(phase: str, *, fix_rounds: int = 1, callback_failures: int = 0) -> dict:
    return {
        "schema_version": 1,
        "phase": phase,
        "fixture_set_sha256": manifest["fixture_set_sha256"],
        "cases": [
            {
                "case_id": "fix",
                "contract_sha256": manifest["cases"][0]["contract_sha256"],
                "route": manifest["cases"][0]["route"],
                "verification_profile": "scoped",
                "outcome_disposition": "achieved",
                "outcome_evidence": {"established": 4, "missing": 0, "contradicted": 0},
                "user_interventions": 0,
                "model_rounds": fix_rounds,
                "review_rounds": 1,
                "callback_failures": callback_failures,
                "duplicate_effects": 0,
                "duration_seconds": 30,
            },
            {
                "case_id": "design",
                "contract_sha256": manifest["cases"][1]["contract_sha256"],
                "route": manifest["cases"][1]["route"],
                "verification_profile": "scoped",
                "outcome_disposition": "achieved",
                "outcome_evidence": {"established": 4, "missing": 0, "contradicted": 0},
                "user_interventions": 0,
                "model_rounds": 1,
                "review_rounds": 1,
                "callback_failures": 0,
                "duplicate_effects": 0,
                "duration_seconds": 30,
            },
        ],
    }


with tempfile.TemporaryDirectory(prefix="paired-evals.") as raw:
    tmp = Path(raw)
    baseline_path = tmp / "baseline.json"
    post_path = tmp / "post.json"
    baseline_path.write_text(json.dumps(report("baseline")) + "\n", encoding="utf-8")
    post_path.write_text(json.dumps(report("post")) + "\n", encoding="utf-8")
    comparison = paired_evals.compare_reports(
        manifest,
        baseline_path=baseline_path,
        post_path=post_path,
    )
    check("equal paired evidence has no regressions", comparison["regressions"] == [])

    post_path.write_text(
        json.dumps(report("post", fix_rounds=2, callback_failures=1)) + "\n",
        encoding="utf-8",
    )
    comparison = paired_evals.compare_reports(
        manifest,
        baseline_path=baseline_path,
        post_path=post_path,
    )
    check(
        "comparison reports bounded regressions",
        comparison["regressions"]
        == ["fix: callback_failures increased", "fix: model_rounds increased"],
    )

print("\nAll paired eval tests passed.")
