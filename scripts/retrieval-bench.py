#!/usr/bin/env python3
"""retrieval-bench.py — measure retrieval quality against a curated goldset.

Runs every goldset query through the four retrieval channels and reports
hit@1 / hit@5 / MRR@10 per channel. This is the measuring stick for any
future retrieval tuning (embedding model swap, chunking, fusion weights):
no change ships without moving these numbers.

Channels (imported from sibling scripts via importlib, no code duplication):
  tag     — scripts/tag-search.py     (frontmatter tag prefilter)
  bm25    — scripts/bm25-index.py     (sparse lexical, whole pages)
  dense   — scripts/semantic-search.py (tiling-cache cosine via local ollama)
  hybrid  — RRF fusion of dense + bm25 (same as semantic-search --hybrid)

Goldset: .vault-meta/retrieval-goldset.jsonl — one JSON per line:
  {"q": "<query as the user would ask>", "expect": ["wiki/....md", ...], "note": "..."}
A query counts as a hit when ANY of its expect paths appears in the top-K.
Queries whose expect paths no longer exist are SKIPPED with a warning
(fix the goldset, do not let dead pages poison the metric).

Degradation: ollama unreachable -> dense/hybrid marked SKIPPED, tag/bm25
still measured (exit 0). Missing goldset or bm25 index -> exit 3.

Usage:
    ./scripts/retrieval-bench.py [--verbose] [--report <path>]

Exit codes: 0 ok, 2 usage, 3 goldset/index missing or corrupt.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

VAULT_ROOT = Path(__file__).resolve().parent.parent
GOLDSET = VAULT_ROOT / ".vault-meta" / "retrieval-goldset.jsonl"
CACHE_PATH = VAULT_ROOT / ".vault-meta" / "tiling-cache.json"

TOP_K = 10


def load_module(filename: str, name: str):
    path = VAULT_ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_goldset() -> list[dict]:
    if not GOLDSET.is_file():
        print(f"goldset missing at {GOLDSET} — seed it first", file=sys.stderr)
        sys.exit(3)
    entries = []
    for n, line in enumerate(GOLDSET.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            assert isinstance(d["q"], str) and isinstance(d["expect"], list) and d["expect"]
        except Exception as exc:
            print(f"goldset line {n} unreadable: {exc}", file=sys.stderr)
            sys.exit(3)
        entries.append(d)
    if not entries:
        print("goldset is empty", file=sys.stderr)
        sys.exit(3)
    return entries


def first_hit_rank(ranked: list[str], expect: list[str], k: int = TOP_K) -> int | None:
    """1-based rank of the first expected path in top-k, else None."""
    exp = set(expect)
    for i, path in enumerate(ranked[:k], start=1):
        if path in exp:
            return i
    return None


class Metrics:
    def __init__(self) -> None:
        self.ranks: list[int | None] = []

    def add(self, rank: int | None) -> None:
        self.ranks.append(rank)

    @property
    def n(self) -> int:
        return len(self.ranks)

    def hit_at(self, k: int) -> float:
        if not self.ranks:
            return 0.0
        return sum(1 for r in self.ranks if r is not None and r <= k) / len(self.ranks)

    def mrr(self) -> float:
        if not self.ranks:
            return 0.0
        return sum(1.0 / r for r in self.ranks if r is not None) / len(self.ranks)


def main() -> int:
    argv = sys.argv[1:]
    verbose = "--verbose" in argv
    report_path: Path | None = None
    if "--report" in argv:
        try:
            report_path = Path(argv[argv.index("--report") + 1])
        except IndexError:
            print("usage: retrieval-bench.py [--verbose] [--report <path>]", file=sys.stderr)
            return 2
    for a in argv:
        if a not in ("--verbose", "--report") and (report_path is None or a != str(report_path)):
            print("usage: retrieval-bench.py [--verbose] [--report <path>]", file=sys.stderr)
            return 2

    goldset = load_goldset()

    ts = load_module("tag-search.py", "bench_tag_search")
    bm = load_module("bm25-index.py", "bench_bm25_index")
    sem = load_module("semantic-search.py", "bench_semantic_search")

    # tag channel state
    tag_index = None
    if ts.TAG_INDEX.is_file():
        try:
            tag_index = json.loads(ts.TAG_INDEX.read_text(encoding="utf-8"))
        except Exception:
            tag_index = None

    # bm25 channel state
    bm_idx = bm.load_index_or_none()
    if bm_idx is None:
        print("bm25 index missing — run ./scripts/bm25-index.py build", file=sys.stderr)
        return 3

    # dense channel state: cache + one probe embed decides availability
    dense_ok = False
    model = ""
    entries: dict = {}
    if CACHE_PATH.is_file():
        try:
            cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            model = cache["model"]
            entries = cache["embeddings"]
            sem.embed("ping", model)
            dense_ok = True
        except Exception as exc:
            print(f"dense channel unavailable ({exc}) — dense/hybrid SKIPPED", file=sys.stderr)
    else:
        print("tiling cache missing — dense/hybrid SKIPPED", file=sys.stderr)

    channels = ["tag", "bm25"] + (["dense", "hybrid"] if dense_ok else [])
    metrics = {c: Metrics() for c in channels}
    per_query: list[dict] = []
    skipped_queries = 0

    for entry in goldset:
        q, expect = entry["q"], entry["expect"]
        live_expect = [p for p in expect if (VAULT_ROOT / p).is_file()]
        if not live_expect:
            print(f"WARN goldset query skipped (expect pages gone): {q[:60]}", file=sys.stderr)
            skipped_queries += 1
            continue

        ranked: dict[str, list[str]] = {}

        words = ts.words_of(q)
        if tag_index is not None and words:
            matched = ts.match_tags(words, list(tag_index.keys()))
            ranked["tag"] = [p for p, _ in ts.rank_pages(matched, tag_index)][:TOP_K]
        else:
            ranked["tag"] = []

        ranked["bm25"] = [p for p, _ in bm.score_query(bm_idx, q, top_k=TOP_K * 2)][:TOP_K]

        if dense_ok:
            qvec = sem.embed(q, model)
            dense_sorted = sorted(
                ((sem.cosine(qvec, e["embedding"]), p) for p, e in entries.items()),
                reverse=True,
            )
            ranked["dense"] = [p for _, p in dense_sorted[:TOP_K * 2]][:TOP_K]
            fused = sem.hybrid_fuse(
                [p for _, p in dense_sorted[:20]],
                [p for p, _ in bm.score_query(bm_idx, q, top_k=20)],
                set(entries),
            )
            ranked["hybrid"] = [p for p, _ in fused[:TOP_K]]

        row = {"q": q, "note": entry.get("note", "")}
        for c in channels:
            r = first_hit_rank(ranked[c], live_expect)
            metrics[c].add(r)
            row[c] = r
        per_query.append(row)

    # ---- output ----
    lines: list[str] = []
    lines.append(f"retrieval-bench: {len(per_query)} queries"
                 + (f" ({skipped_queries} skipped)" if skipped_queries else ""))
    lines.append("")
    lines.append(f"{'channel':<8} {'hit@1':>7} {'hit@5':>7} {'MRR@10':>8}")
    for c in channels:
        m = metrics[c]
        lines.append(f"{c:<8} {m.hit_at(1):>7.2f} {m.hit_at(5):>7.2f} {m.mrr():>8.3f}")
    if not dense_ok:
        lines.append("dense/hybrid: SKIPPED (ollama or tiling cache unavailable)")
    if verbose:
        lines.append("")
        header = "rank per query (None = miss beyond top-10):"
        lines.append(header)
        for row in per_query:
            marks = "  ".join(f"{c}={row[c]}" for c in channels)
            lines.append(f"  {marks}  | {row['q'][:60]}")
    out = "\n".join(lines)
    print(out)

    if report_path is not None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rep: list[str] = []
        rep.append("---")
        rep.append("type: meta")
        rep.append(f'title: "retrieval-bench-{date}"')
        rep.append(f"created: {date}")
        rep.append(f"updated: {date}")
        rep.append("status: solid")
        rep.append("tags:")
        rep.append("  - meta")
        rep.append("  - report")
        rep.append("  - retrieval")
        rep.append("---")
        rep.append("")
        rep.append("# Retrieval Benchmark Report")
        rep.append("")
        rep.append(f"- generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
        rep.append(f"- goldset: {len(per_query)} queries"
                   + (f" ({skipped_queries} skipped)" if skipped_queries else ""))
        rep.append(f"- dense model: {model or 'n/a'}; dense pages: {len(entries)}; "
                   f"bm25 pages: {bm_idx['doc_count']}")
        rep.append("")
        rep.append("| channel | hit@1 | hit@5 | MRR@10 |")
        rep.append("|---|---|---|---|")
        for c in channels:
            m = metrics[c]
            rep.append(f"| {c} | {m.hit_at(1):.2f} | {m.hit_at(5):.2f} | {m.mrr():.3f} |")
        if not dense_ok:
            rep.append("")
            rep.append("> dense/hybrid SKIPPED — ollama or tiling cache unavailable at run time.")
        rep.append("")
        rep.append("## Per-query ranks (None = miss beyond top-10)")
        rep.append("")
        rep.append("| query | " + " | ".join(channels) + " |")
        rep.append("|---|" + "---|" * len(channels))
        for row in per_query:
            cells = " | ".join(str(row[c]) for c in channels)
            rep.append(f"| {row['q'][:70]} | {cells} |")
        rep.append("")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(rep) + "\n", encoding="utf-8")
        print(f"report written: {report_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
