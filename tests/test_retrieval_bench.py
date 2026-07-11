#!/usr/bin/env python3
"""Hermetic tests for retrieval metrics, split policy, and regression gate."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "scripts/retrieval-bench.py"


class Fail(SystemExit):
    pass


def assert_eq(label, expected, actual):
    if expected != actual:
        raise Fail(f"FAIL {label}: expected {expected!r}, got {actual!r}")
    print(f"OK   {label}")


def assert_true(label, condition, extra=""):
    if not condition:
        raise Fail(f"FAIL {label}{': ' + extra if extra else ''}")
    print(f"OK   {label}")


sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("retrieval_bench_test", BENCH)
rb = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rb
spec.loader.exec_module(rb)


def test_metrics():
    assert_eq("rank first", 1, rb.first_hit_rank(["a", "b"], ["a"]))
    assert_eq("rank any expected", 2, rb.first_hit_rank(["x", "b"], ["a", "b"]))
    assert_eq("rank miss", None, rb.first_hit_rank(["x"], ["a"]))
    metrics = rb.Metrics()
    for rank in (1, 3, None, 2):
        metrics.add(rank)
    assert_eq("metrics n", 4, metrics.n)
    assert_eq("hit@1", 0.25, metrics.hit_at(1))
    assert_eq("hit@5", 0.75, metrics.hit_at(5))
    assert_eq("mrr", round((1 + 1 / 3 + 1 / 2) / 4, 6), round(metrics.mrr(), 6))
    metrics.add(12)
    assert_eq("mrr@10 excludes deeper ranks", round((1 + 1 / 3 + 1 / 2) / 5, 6), round(metrics.mrr(), 6))
    assert_eq("recall multi", 0.5, rb.recall_at(["a", "x"], ["a", "b"], 2))
    ranked = [SimpleNamespace(path="a", heading="Right"), SimpleNamespace(path="b", heading="Other")]
    entry = {
        "expect": ["a", "b"],
        "expect_sections": [
            {"path": "a", "heading": "Right", "relevance": 3},
            {"path": "b", "heading": "Wanted", "relevance": 1},
        ],
    }
    value = rb.ndcg_at(ranked, entry)
    assert_true("section NDCG partial", 0.8 < value < 1.0, str(value))


def make_sandbox():
    root = Path(tempfile.mkdtemp(prefix="retrieval-bench-test."))
    (root / "scripts").mkdir()
    (root / "wiki").mkdir()
    (root / ".vault-meta").mkdir()
    for filename in ("retrieval-bench.py", "retrieve.py", "vault_schema.py", "pipeline_events.py"):
        shutil.copy2(ROOT / "scripts" / filename, root / "scripts" / filename)
    return root


def run_bench(root, *args):
    env = dict(os.environ)
    env["OLLAMA_URL"] = "http://127.0.0.1:9"
    return subprocess.run(
        [sys.executable, str(root / "scripts/retrieval-bench.py"), *args],
        text=True,
        capture_output=True,
        env=env,
    )


def test_cli():
    root = make_sandbox()
    try:
        result = run_bench(root, "--allow-small")
        assert_eq("missing goldset exit 3", 3, result.returncode)

        goldset = root / ".vault-meta/retrieval-goldset.jsonl"
        goldset.write_text("bad-json{\n", encoding="utf-8")
        result = run_bench(root, "--allow-small")
        assert_eq("corrupt goldset exit 3", 3, result.returncode)

        page = root / "wiki/Target.md"
        page.write_text(
            """---
type: concept
title: "Buildkit Cleanup"
address: c-000001
status: developing
created: 2026-07-09
updated: 2026-07-09
tags: [buildkit, disk]
sessions: []
---

# Buildkit Cleanup

## Disk Leak

Remove orphan buildkit containers to reclaim disk space.
""",
            encoding="utf-8",
        )
        goldset.write_text(
            json.dumps(
                {
                    "q": "buildkit disk leak cleanup",
                    "expect": ["wiki/Target.md"],
                    "split": "heldout",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = run_bench(root)
        assert_eq("small goldset rejected", 3, result.returncode)
        assert_true("minimum named", "minimum" in result.stderr)

        result = run_bench(root, "--allow-small")
        assert_eq("sparse bench exit 0", 0, result.returncode)
        assert_true("sparse measured", "sparse/all" in result.stdout)
        assert_true("new metrics measured", "NDCG@10" in result.stdout and "R@20" in result.stdout)
        assert_true("dense optional noted", "OPTIONAL/UNAVAILABLE" in result.stdout)

        report = root / "report.md"
        result = run_bench(root, "--allow-small", "--report", str(report))
        assert_eq("report exit 0", 0, result.returncode)
        text = report.read_text(encoding="utf-8")
        assert_true("report strict frontmatter", text.startswith("---\ntype: meta") and "sessions: []" in text)

        baseline = root / ".vault-meta/baseline.json"
        result = run_bench(
            root,
            "--allow-small",
            "--write-baseline",
            "--baseline",
            str(baseline),
        )
        assert_eq("write baseline exit 0", 0, result.returncode)
        pending = root / ".vault-meta/retrieval-quality.pending.json"
        pending.write_text("{}\n", encoding="utf-8")
        result = run_bench(
            root, "--allow-small", "--gate", "--baseline", str(baseline)
        )
        assert_eq("matching baseline gate pass", 0, result.returncode)
        assert_true("gate pass message", "gate: PASS" in result.stderr)
        assert_true("passing gate clears quality marker", not pending.exists())

        data = json.loads(baseline.read_text(encoding="utf-8"))
        data["sparse"]["all"]["hit_at_5"] = 1.0
        data["sparse"]["all"]["mrr_at_10"] = 1.0
        data["sparse"]["heldout"]["hit_at_5"] = 1.0
        data["sparse"]["heldout"]["mrr_at_10"] = 1.0
        # Make the current ranking miss while preserving the same goldset hash
        # in the baseline, so the metric regression path—not hash drift—fires.
        page.rename(root / "wiki/Irrelevant.md")
        goldset.write_text(
            json.dumps(
                {
                    "q": "totally unmatched phrase",
                    "expect": ["wiki/Irrelevant.md"],
                    "split": "heldout",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        data["goldset_sha256"] = rb.hashlib.sha256(goldset.read_bytes()).hexdigest()
        baseline.write_text(json.dumps(data), encoding="utf-8")
        pending.write_text("{}\n", encoding="utf-8")
        result = run_bench(
            root, "--allow-small", "--gate", "--baseline", str(baseline)
        )
        assert_eq("regression gate exit 5", 5, result.returncode)
        assert_true("regression named", "RETRIEVAL_REGRESSION" in result.stderr)
        assert_true("failing gate preserves quality marker", pending.exists())
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    try:
        test_metrics()
        test_cli()
    except Fail as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
    print("\nAll retrieval benchmark tests passed.")
