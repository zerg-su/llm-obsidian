#!/usr/bin/env python3
"""Quality gate for section-level retrieval.

Sparse metrics are always measured.  Hybrid metrics are added when the dense
chunk cache and Ollama query embedding are available; their absence never
hides or skips the required sparse gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import retrieve


ROOT = Path(__file__).resolve().parents[1]
GOLDSET = ROOT / ".vault-meta" / "retrieval-goldset.jsonl"
BASELINE = ROOT / ".vault-meta" / "retrieval-baseline.json"
MIN_QUERIES = 40
MAX_REGRESSION = 0.02
TOP_K = 10
RECALL_K = 20
QUALITY_PENDING = ROOT / ".vault-meta" / "retrieval-quality.pending.json"


def goldset_hash(path: Path = GOLDSET) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_goldset(*, allow_small: bool = False) -> list[dict]:
    if not GOLDSET.is_file():
        raise ValueError(f"goldset missing: {GOLDSET}")
    entries: list[dict] = []
    for line_number, line in enumerate(GOLDSET.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"goldset line {line_number}: {exc}") from exc
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("q"), str)
            or not entry["q"].strip()
            or not isinstance(entry.get("expect"), list)
            or not entry["expect"]
            or entry.get("split") not in {"tune", "heldout"}
        ):
            raise ValueError(
                f"goldset line {line_number}: require q, non-empty expect, split=tune|heldout"
            )
        if "REPLACE ME" in entry["q"]:
            raise ValueError(f"goldset line {line_number}: placeholder query is forbidden")
        relevance = entry.get("relevance", {})
        if not isinstance(relevance, dict) or any(
            path not in entry["expect"] or not isinstance(grade, int) or not 1 <= grade <= 3
            for path, grade in relevance.items()
        ):
            raise ValueError(f"goldset line {line_number}: relevance must grade expected paths 1..3")
        sections = entry.get("expect_sections", [])
        if not isinstance(sections, list) or any(
            not isinstance(item, dict)
            or item.get("path") not in entry["expect"]
            or not isinstance(item.get("heading"), str)
            or not item["heading"].strip()
            or not isinstance(item.get("relevance", 1), int)
            or not 1 <= item.get("relevance", 1) <= 3
            for item in sections
        ):
            raise ValueError(f"goldset line {line_number}: invalid expect_sections")
        entries.append(entry)
    heldout = sum(entry["split"] == "heldout" for entry in entries)
    if not allow_small and len(entries) < MIN_QUERIES:
        raise ValueError(f"goldset has {len(entries)} queries; minimum is {MIN_QUERIES}")
    if entries and heldout * 2 < len(entries):
        raise ValueError(f"heldout split is {heldout}/{len(entries)}; require at least 50%")
    return entries


def first_hit_rank(ranked: list[str], expected: list[str], k: int = TOP_K) -> int | None:
    wanted = set(expected)
    for rank, path in enumerate(ranked[:k], 1):
        if path in wanted:
            return rank
    return None


@dataclass
class Metrics:
    ranks: list[int | None]

    def __init__(self) -> None:
        self.ranks = []
        self.recalls: list[float] = []
        self.ndcgs: list[float] = []
        self.latencies_ms: list[float] = []

    def add(
        self,
        rank: int | None,
        *,
        recall: float | None = None,
        ndcg: float | None = None,
        latency_ms: float = 0.0,
    ) -> None:
        self.ranks.append(rank)
        self.recalls.append(float(rank is not None) if recall is None else recall)
        self.ndcgs.append((1.0 / rank if rank else 0.0) if ndcg is None else ndcg)
        self.latencies_ms.append(latency_ms)

    @property
    def n(self) -> int:
        return len(self.ranks)

    def hit_at(self, k: int) -> float:
        return (
            sum(rank is not None and rank <= k for rank in self.ranks) / self.n
            if self.n
            else 0.0
        )

    def mrr(self, k: int = TOP_K) -> float:
        return (
            sum(1.0 / rank for rank in self.ranks if rank is not None and rank <= k) / self.n
            if self.n
            else 0.0
        )

    def p95_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]

    def summary(self) -> dict[str, float | int]:
        return {
            "n": self.n,
            "hit_at_1": self.hit_at(1),
            "hit_at_5": self.hit_at(5),
            "mrr_at_10": self.mrr(),
            "recall_at_20": sum(self.recalls) / self.n if self.n else 0.0,
            "ndcg_at_10": sum(self.ndcgs) / self.n if self.n else 0.0,
            "p95_ms": self.p95_ms(),
        }


def live_expected(entry: dict) -> list[str]:
    return [path for path in entry["expect"] if (ROOT / path).is_file()]


def recall_at(ranked: list[str], expected: list[str], k: int = RECALL_K) -> float:
    wanted = set(expected)
    return len(wanted & set(ranked[:k])) / len(wanted) if wanted else 0.0


def result_gain(entry: dict, result: retrieve.Result) -> int:
    sections = entry.get("expect_sections") or []
    if sections:
        for item in sections:
            if item["path"] == result.path and item["heading"] == result.heading:
                return int(item.get("relevance", 1))
        return 0
    return int((entry.get("relevance") or {}).get(result.path, 1 if result.path in entry["expect"] else 0))


def ndcg_at(results: list[retrieve.Result], entry: dict, k: int = TOP_K) -> float:
    gains = [result_gain(entry, result) for result in results[:k]]
    if entry.get("expect_sections"):
        ideal = sorted((int(item.get("relevance", 1)) for item in entry["expect_sections"]), reverse=True)[:k]
    else:
        grades = entry.get("relevance") or {path: 1 for path in entry["expect"]}
        ideal = sorted((int(value) for value in grades.values()), reverse=True)[:k]
    dcg = sum((2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))
    idcg = sum((2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(ideal, 1))
    return dcg / idcg if idcg else 0.0


def benchmark(
    entries: list[dict], *, verbose: bool = False, sparse_only: bool = False
) -> tuple[dict, list[dict], str | None]:
    index, _ = retrieve.ensure_sparse()
    dense, dense_reason = (None, "disabled by --sparse-only") if sparse_only else retrieve.load_dense(index)
    dense_ok = False
    if dense is not None:
        try:
            retrieve.embed("retrieval benchmark probe", dense["model"])
            dense_ok = True
            dense_reason = None
        except Exception as exc:  # network/model errors are optional degradation
            dense_reason = str(exc)

    channels = ["sparse"] + (["hybrid"] if dense_ok else [])
    cohorts = {
        channel: {"all": Metrics(), "heldout": Metrics(), "tune": Metrics()}
        for channel in channels
    }
    rows: list[dict] = []
    for entry in entries:
        expected = live_expected(entry)
        if not expected:
            raise ValueError(f"goldset expect paths are all missing for query: {entry['q']}")
        started = time.perf_counter()
        sparse_results, _ = retrieve.retrieve(index, entry["q"], top=RECALL_K, dense_mode="off")
        latency = {"sparse": (time.perf_counter() - started) * 1000}
        result_sets = {"sparse": sparse_results}
        if dense_ok:
            started = time.perf_counter()
            hybrid_results, meta = retrieve.retrieve(
                index, entry["q"], top=RECALL_K, dense_mode="auto"
            )
            latency["hybrid"] = (time.perf_counter() - started) * 1000
            if meta["degraded"]:
                raise RuntimeError(f"dense degraded after successful probe: {meta['reason']}")
            result_sets["hybrid"] = hybrid_results
        row = {"q": entry["q"], "split": entry["split"]}
        for channel in channels:
            paths = [item.path for item in result_sets[channel]]
            rank = first_hit_rank(paths, expected)
            recall = recall_at(paths, expected)
            ndcg = ndcg_at(result_sets[channel], entry)
            cohorts[channel]["all"].add(rank, recall=recall, ndcg=ndcg, latency_ms=latency[channel])
            cohorts[channel][entry["split"]].add(rank, recall=recall, ndcg=ndcg, latency_ms=latency[channel])
            row[channel] = rank
            row[f"{channel}_ndcg"] = ndcg
        rows.append(row)
        if verbose:
            marks = ", ".join(f"{channel}={row[channel]}" for channel in channels)
            print(f"{entry['split']:<7} {marks} | {entry['q']}", file=sys.stderr)
    summary = {
        channel: {cohort: metrics.summary() for cohort, metrics in by_cohort.items()}
        for channel, by_cohort in cohorts.items()
    }
    return summary, rows, dense_reason


def baseline_payload(summary: dict, count: int) -> dict:
    sparse = {
        cohort: {key: value for key, value in metrics.items() if key != "p95_ms"}
        for cohort, metrics in summary["sparse"].items()
    }
    return {
        "schema_version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "goldset_sha256": goldset_hash(),
        "goldset_size": count,
        "max_regression": MAX_REGRESSION,
        "sparse": sparse,
    }


def gate(summary: dict, baseline_path: Path = BASELINE) -> list[str]:
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"baseline missing/corrupt: {baseline_path}: {exc}") from exc
    if baseline.get("goldset_sha256") != goldset_hash():
        raise ValueError("baseline goldset hash differs; review queries and write an explicit new baseline")
    tolerance = float(baseline.get("max_regression", MAX_REGRESSION))
    failures: list[str] = []
    for cohort in ("all", "heldout"):
        for metric in ("hit_at_5", "mrr_at_10", "recall_at_20", "ndcg_at_10"):
            if metric not in baseline["sparse"][cohort]:
                continue
            old = float(baseline["sparse"][cohort][metric])
            new = float(summary["sparse"][cohort][metric])
            if new < old - tolerance - 1e-12:
                failures.append(
                    f"sparse/{cohort}/{metric}: {new:.3f} < baseline {old:.3f} - {tolerance:.3f}"
                )
    return failures


def render(summary: dict, count: int, dense_reason: str | None) -> str:
    lines = [f"retrieval-bench: {count} queries", ""]
    lines.append(f"{'channel/split':<18} {'n':>4} {'hit@1':>7} {'hit@5':>7} {'MRR@10':>8} {'R@20':>7} {'NDCG@10':>9} {'p95ms':>8}")
    for channel, cohorts in summary.items():
        for cohort in ("all", "heldout", "tune"):
            metrics = cohorts[cohort]
            lines.append(
                f"{channel + '/' + cohort:<18} {metrics['n']:>4} "
                f"{metrics['hit_at_1']:>7.2f} {metrics['hit_at_5']:>7.2f} "
                f"{metrics['mrr_at_10']:>8.3f} {metrics['recall_at_20']:>7.3f} "
                f"{metrics['ndcg_at_10']:>9.3f} {metrics['p95_ms']:>8.1f}"
            )
    if "hybrid" not in summary:
        lines.append(f"hybrid: OPTIONAL/UNAVAILABLE ({dense_reason or 'dense cache missing'})")
    return "\n".join(lines)


def write_report(path: Path, output: str, rows: list[dict], summary: dict) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    channels = list(summary)
    content = [
        "---",
        "type: meta",
        f'title: "retrieval-bench-{today}"',
        f"created: {today}",
        f"updated: {today}",
        "status: solid",
        "tags: [meta, report, retrieval]",
        "sessions: []",
        "---",
        "",
        "# Retrieval Benchmark Report",
        "",
        "```text",
        output,
        "```",
        "",
        "| split | query | " + " | ".join(channels) + " |",
        "|---|---|" + "---|" * len(channels),
    ]
    for row in rows:
        content.append(
            f"| {row['split']} | {row['q'][:80]} | "
            + " | ".join(str(row[channel]) for channel in channels)
            + " |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(content) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--allow-small", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="emit machine-readable benchmark result")
    parser.add_argument("--sparse-only", action="store_true", help="skip optional dense probe/channel")
    args = parser.parse_args(argv)
    try:
        entries = load_goldset(allow_small=args.allow_small)
        summary, rows, dense_reason = benchmark(
            entries, verbose=args.verbose, sparse_only=args.sparse_only
        )
        output = render(summary, len(entries), dense_reason)
        if args.json:
            print(json.dumps({"schema_version": 1, "count": len(entries), "summary": summary, "rows": rows, "dense_reason": dense_reason}))
        else:
            print(output)
        if args.report:
            write_report(args.report, output, rows, summary)
        if args.write_baseline:
            args.baseline.write_text(
                json.dumps(baseline_payload(summary, len(entries)), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(f"baseline written: {args.baseline}", file=sys.stderr)
        if args.gate:
            failures = gate(summary, args.baseline)
            if failures:
                for failure in failures:
                    print(f"RETRIEVAL_REGRESSION: {failure}", file=sys.stderr)
                return 5
            print("retrieval gate: PASS", file=sys.stderr)
            QUALITY_PENDING.unlink(missing_ok=True)
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"retrieval-bench: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
