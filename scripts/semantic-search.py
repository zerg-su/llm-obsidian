#!/usr/bin/env python3
"""Semantic search over the vault using the tiling embeddings cache.

Reuses .vault-meta/tiling-cache.json (built by scripts/tiling-check.py) as a
retrieval index: embeds the query via the local ollama /api/embed endpoint and
ranks cached pages by cosine similarity. Read-only; never touches the cache.

--hybrid adds a sparse lexical channel: BM25 over whole pages (imported from
scripts/bm25-index.py, index rebuilt by the Stop hook). Fusion is scope-aware
RRF (k=60): dense ranks in-scope pages, bm25 only INJECTS pages the dense
tiling cache cannot see (wiki/meta/, plans/, folds/, too_large) — see the
comment at hybrid_fuse. Degradations are automatic: ollama down -> bm25-only,
bm25 index missing -> dense-only, both down -> the dense failure's exit code.
Both channels key by vault-relative path.

Scope caveat (dense channel): the tiling cache covers the tiling scope only —
wiki/meta/, wiki/plans/, wiki/folds/ and index-like files are NOT in it (see
tiling-check.py EXCLUDE rules). The BM25 channel DOES cover them (all of
wiki/ except log.md, _templates/, _index.md), so --hybrid closes that gap;
for log.md fall back to Grep.

Usage:
    ./scripts/semantic-search.py "<query>" [--top N] [--hybrid]

Exit codes (mirror tiling-check.py): 0 ok, 2 usage, 3 cache missing/corrupt,
10 ollama unreachable. In --hybrid mode 3/10 fire only when BOTH channels are
unavailable.
"""

from __future__ import annotations

import json
import math
import sys
import urllib.error
import urllib.request
from pathlib import Path

VAULT_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = VAULT_ROOT / ".vault-meta" / "tiling-cache.json"
OLLAMA_URL = "http://127.0.0.1:11434"
EMBED_TIMEOUT_SEC = 30
EMBED_NUM_CTX = 8192
EMBED_MAX_CHARS = 12000


def embed(text: str, model: str) -> list[float]:
    # No "search_query:" prefix on purpose: the tiling cache embeds documents
    # WITHOUT "search_document:" (shared with tiling-check.py), and symmetric
    # unprefixed query/doc ranks fine empirically (tested 2026-06-10);
    # asymmetric prefixing showed no measurable gain on this vault.
    payload = json.dumps({
        "model": model,
        "input": text[:EMBED_MAX_CHARS],
        "options": {"num_ctx": EMBED_NUM_CTX},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed", data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT_SEC) as resp:
        data = json.loads(resp.read())
    embs = data.get("embeddings")
    if not isinstance(embs, list) or not embs or not embs[0]:
        raise RuntimeError(f"no embedding in response: {str(data)[:160]}")
    return embs[0]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


RRF_K = 60
# Scope-aware hybrid fusion (2026-07-05, swept on the 52-query goldset with a
# 26-query held-out half; see wiki/meta/reports/retrieval-bench-2026-07-05.md):
#
#   * dense votes normally (DENSE_WEIGHT);
#   * bm25 votes ONLY for pages OUTSIDE the dense tiling scope (wiki/meta/,
#     wiki/plans/, wiki/folds/, too_large) — injected at BM25_OOS_WEIGHT;
#   * bm25 votes for in-scope pages are dropped entirely.
#
# Why: at any dense:bm25 weighting that protects a correct dense#1, an in-scope
# bm25#1 scores below dense#10 — it can never rescue a dense miss, only demote
# a correct dense#1 via correlated errors (held-out hit@1: 0.38 with [3,1] vs
# 0.62 with in-scope votes off). Meanwhile the flat low bm25 weight buried
# out-of-scope pages even at bm25#1 (guaranteed top-10 miss), silently killing
# the coverage that justified BM25 in the mix. OOS weight is 2.9, NOT 3.0: at a
# 3.0 tie the (path, score) tie-break can put an OOS page above a correct
# dense#1; 2.9 slots bm25#1-OOS between dense#3 and dense#4. All-52:
# 0.73/0.88/0.786 vs dense-solo 0.73/0.83/0.771; tuned-26 dense ranking
# preserved exactly.
DENSE_WEIGHT = 3.0
BM25_OOS_WEIGHT = 2.9


def hybrid_fuse(dense_ranked: list[str], bm25_ranked: list[str],
                dense_scope: set[str]) -> list[tuple[str, float]]:
    """Fuse dense + bm25 rankings; bm25 only injects out-of-dense-scope pages.

    dense_scope = set of paths present in the tiling cache (what dense CAN see).
    """
    scores: dict[str, float] = {}
    for rank, path in enumerate(dense_ranked, start=1):
        scores[path] = scores.get(path, 0.0) + DENSE_WEIGHT / (RRF_K + rank)
    for rank, path in enumerate(bm25_ranked, start=1):
        if path in dense_scope:
            continue
        scores[path] = scores.get(path, 0.0) + BM25_OOS_WEIGHT / (RRF_K + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def rrf_fuse(ranked_lists: list[list[str]],
             weights: list[float] | None = None) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion: score(p) = sum_i weights[i] / (K + rank_p).

    weights defaults to 1.0 per list (classic equal-weight RRF); pass a
    per-list weight vector (e.g. HYBRID_WEIGHTS) to favour a channel.
    """
    scores: dict[str, float] = {}
    for i, lst in enumerate(ranked_lists):
        w = 1.0 if weights is None else weights[i]
        for rank, path in enumerate(lst, start=1):
            scores[path] = scores.get(path, 0.0) + w / (RRF_K + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def load_bm25_module():
    """Import scripts/bm25-index.py (hyphenated name -> importlib). None if absent."""
    import importlib.util
    mod_path = VAULT_ROOT / "scripts" / "bm25-index.py"
    if not mod_path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("bm25_index", mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as exc:
        print(f"bm25 module load failed: {exc}", file=sys.stderr)
        return None


def main() -> int:
    argv = sys.argv[1:]
    top = 5
    hybrid = False
    args: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--top":
            try:
                top = int(argv[i + 1])
                i += 2
                continue
            except (IndexError, ValueError):
                print("usage: semantic-search.py '<query>' [--top N] [--hybrid]", file=sys.stderr)
                return 2
        if argv[i] == "--hybrid":
            hybrid = True
            i += 1
            continue
        args.append(argv[i])
        i += 1
    if not args:
        print("usage: semantic-search.py '<query>' [--top N] [--hybrid]", file=sys.stderr)
        return 2
    query = " ".join(args)

    # Dense channel: tiling-cache cosine. On failure remember (exit_code, msg)
    # instead of bailing — in hybrid mode BM25 may still carry the query.
    dense_fail: tuple[int, str] | None = None
    scored: list[tuple[float, str]] = []
    model = ""
    entries: dict = {}
    if not CACHE_PATH.is_file():
        dense_fail = (3, "tiling cache missing — run a lint/tiling pass first "
                         "(./scripts/tiling-check.py --report ...)")
    else:
        try:
            cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            model = cache["model"]
            entries = cache["embeddings"]
            assert isinstance(entries, dict) and entries
        except Exception as exc:
            dense_fail = (3, f"tiling cache unreadable: {exc}")
    if dense_fail is None:
        try:
            qvec = embed(query, model)
            scored = sorted(
                ((cosine(qvec, e["embedding"]), path) for path, e in entries.items()),
                reverse=True,
            )
        except (urllib.error.URLError, OSError) as exc:
            dense_fail = (10, f"ollama unreachable at {OLLAMA_URL}: {exc}")
        except Exception as exc:
            dense_fail = (10, f"embed failed: {exc}")

    if not hybrid:
        if dense_fail is not None:
            print(dense_fail[1], file=sys.stderr)
            return dense_fail[0]
        print(f"# semantic top-{top} for: {query}  (cache: {len(entries)} pages, model {model})")
        for score, path in scored[:top]:
            print(f"{score:.4f}  {path}")
        return 0

    # Sparse channel: BM25 over whole pages (same path key space).
    fuse_k = top * 4
    bm25_ranked: list[str] | None = None
    bm25_docs = 0
    mod = load_bm25_module()
    if mod is not None:
        idx = mod.load_index_or_none()
        if idx is not None:
            bm25_ranked = [p for p, _ in mod.score_query(idx, query, top_k=fuse_k)]
            bm25_docs = idx["doc_count"]

    if dense_fail is not None and bm25_ranked is None:
        print(dense_fail[1], file=sys.stderr)
        print("bm25 index also unavailable — no channel left "
              "(./scripts/bm25-index.py build)", file=sys.stderr)
        return dense_fail[0]

    if dense_fail is not None:
        print(f"{dense_fail[1]} — bm25-only", file=sys.stderr)
        print(f"# bm25-only top-{top} for: {query}  (index: {bm25_docs} pages)")
        for rank, path in enumerate(bm25_ranked[:top], start=1):
            print(f"bm25#{rank}  {path}")
        return 0

    dense_ranked = [path for _, path in scored[:fuse_k]]
    if bm25_ranked is None:
        print("bm25 index missing — dense-only (./scripts/bm25-index.py build)",
              file=sys.stderr)
        print(f"# semantic top-{top} for: {query}  (cache: {len(entries)} pages, model {model})")
        for score, path in scored[:top]:
            print(f"{score:.4f}  {path}")
        return 0

    fused = hybrid_fuse(dense_ranked, bm25_ranked, set(entries))
    d_rank = {p: r for r, p in enumerate(dense_ranked, start=1)}
    b_rank = {p: r for r, p in enumerate(bm25_ranked, start=1)}
    print(f"# hybrid top-{top} for: {query}  "
          f"(dense: {len(entries)} pages, model {model} | bm25: {bm25_docs} pages | RRF k={RRF_K})")
    for path, score in fused[:top]:
        d = f"dense#{d_rank[path]}" if path in d_rank else "dense#-"
        b = f"bm25#{b_rank[path]}" if path in b_rank else "bm25#-"
        print(f"{score:.4f}  {path}  ({d}, {b})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
