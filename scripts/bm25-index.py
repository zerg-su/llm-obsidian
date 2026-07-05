#!/usr/bin/env python3
"""bm25-index.py — sparse BM25 inverted index over whole wiki pages.

Page-level adaptation of the upstream (AgriciDaniel/claude-obsidian v1.7.2)
chunk-level indexer: tokenizer, scoring and locking are ported as-is; the
document unit is a whole page (frontmatter title + tags + body), no chunk
store, no contextual-prefix. doc_id = vault-relative path ("wiki/....md") —
the same key space as the tiling embeddings cache, so the dense and sparse
channels fuse by path with no mapping (see semantic-search.py --hybrid).

Pure stdlib (no rank_bm25 dep). Standard Okapi BM25 with k1=1.5, b=0.75.
Rebuilt by the Stop hook on wiki changes — the index is always fresh to the
last turn. Cheap: ~500 pages tokenize in well under a second.

Skips: log.md (linear log), _templates/, any _index.md (AUTO-INDEX churn).
Everything else is indexed — including wiki/meta|plans|folds, which the
tiling cache excludes; BM25 is the channel that covers them.

Concurrency:
- Locks .vault-meta/.bm25.lock (fcntl exclusive) around any index write.
- Atomic .tmp + rename for the index file.

Index schema (.vault-meta/bm25/index.json):
{
  "schema_version": 2,            # 1 = upstream chunk schema; 2 = page-level
  "params": {"k1": 1.5, "b": 0.75},
  "doc_count": 475,
  "avg_dl": 487.5,
  "updated_at": "2026-07-04T...",
  "vocab": {"<term>": {"df": 17, "postings": [["wiki/a.md", 3], ...]}},
  "docs": {"wiki/a.md": {"dl": 487}}
}

Interfaces:
  bm25-index.py build [--quiet]     # full rebuild (cheap at this scale)
  bm25-index.py query "text" [--top 20]
  bm25-index.py stats

Exit codes:
  0 — success
  1 — lock acquisition failed
  2 — usage error
  3 — index file missing or corrupt (query/stats mode)
  4 — wiki directory missing
"""

import argparse
import fcntl
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

VAULT_ROOT = Path(__file__).resolve().parent.parent
WIKI_DIR = VAULT_ROOT / "wiki"
META_DIR = VAULT_ROOT / ".vault-meta"
BM25_DIR = META_DIR / "bm25"
INDEX_PATH = BM25_DIR / "index.json"
LOCK_PATH = META_DIR / ".bm25.lock"

K1 = 1.5
B = 0.75

# Small high-frequency stopword lists (English + Russian prose particles).
# Conservative — keep recall high. Single-char tokens are dropped by the
# len > 1 filter in tokenize(), so и/в/с/к/о need no entries.
STOPWORDS = frozenset("""
a an and are as at be by for from has have he her him his i if in is it its
of on or that the their them they this to was were will with you your
на не что как по для из это при или если его так же был была было были
чтобы только уже есть нет них ним она они оно мы вы но да ещё еще
""".split())

# Unicode-aware tokenizer (ported from upstream v1.7.2). \w under re.UNICODE
# matches letters and digits from any script (Cyrillic, CJK, accented Latin)
# plus underscore. Internal apostrophes and hyphens are preserved so "user's"
# and "well-formed" stay single tokens. Pure-symbol/emoji tokens fail the
# leading \w anchor and are skipped.
TOKEN_RE = re.compile(r"\w[\w'\-]*", re.UNICODE)

FM_RX = re.compile(r"\A---\n(.*?)\n---\n?", re.S)

EXIT_OK = 0
EXIT_LOCK = 1
EXIT_USAGE = 2
EXIT_INDEX_MISSING = 3
EXIT_NO_WIKI = 4


def log(msg):
    print(msg, file=sys.stderr)


def tokenize(text):
    """Lowercase, strip punctuation, drop stopwords. Returns a list of terms."""
    return [t.lower() for t in TOKEN_RE.findall(text)
            if t.lower() not in STOPWORDS and len(t) > 1]


def acquire_lock():
    META_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        log("ERR: could not acquire bm25 lock")
        sys.exit(EXIT_LOCK)
    return fd


def release_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def parse_page(text):
    """Light frontmatter parse: (title, tags, body). No pyyaml."""
    m = FM_RX.match(text)
    fm, body = (m.group(1), text[m.end():]) if m else ("", text)
    title = ""
    tm = re.search(r"^title:\s*[\"']?(.+?)[\"']?\s*$", fm, re.M)
    if tm:
        title = tm.group(1)
    tags = []
    flow = re.search(r"^tags:\s*\[(.*?)\]\s*$", fm, re.M)
    if flow:
        tags = [t.strip().strip("'\"") for t in flow.group(1).split(",") if t.strip()]
    else:
        block = re.search(r"^tags:\s*\n((?:[ \t]+-[ \t]+.*\n?)+)", fm, re.M)
        if block:
            tags = [ln.strip().lstrip("-").strip().strip("'\"")
                    for ln in block.group(1).splitlines() if ln.strip().startswith("-")]
    return title, tags, body


def should_skip(md, wiki_rel_parts):
    if md.name == "_index.md":
        return True
    if "_templates" in wiki_rel_parts:
        return True
    if md.name == "log.md" and len(wiki_rel_parts) == 1:
        return True
    return False


def discover_docs():
    """Yield (rel_path "wiki/....md", doc_text) for every indexable page."""
    if not WIKI_DIR.is_dir():
        log(f"ERR: no wiki directory at {WIKI_DIR}")
        sys.exit(EXIT_NO_WIKI)
    for md in sorted(WIKI_DIR.rglob("*.md")):
        parts = md.relative_to(WIKI_DIR).parts
        if should_skip(md, parts):
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            log(f"  skip (unreadable): {md} — {e}")
            continue
        title, tags, body = parse_page(text)
        if not title:
            title = md.stem
        doc_text = title + " " + " ".join(tags) + "\n" + body
        yield str(md.relative_to(VAULT_ROOT)), doc_text


def build_index():
    docs = {}
    df = Counter()
    postings = defaultdict(list)

    for rel_path, text in discover_docs():
        tokens = tokenize(text)
        tf = Counter(tokens)
        docs[rel_path] = {"dl": len(tokens)}
        for term, count in tf.items():
            df[term] += 1
            postings[term].append([rel_path, count])

    if not docs:
        log("WARN: no pages indexed")
        return None

    avg_dl = sum(d["dl"] for d in docs.values()) / len(docs)
    vocab = {term: {"df": df[term], "postings": postings[term]}
             for term in sorted(df.keys())}

    return {
        "schema_version": 2,
        "params": {"k1": K1, "b": B},
        "doc_count": len(docs),
        "avg_dl": avg_dl,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "vocab": vocab,
        "docs": docs,
    }


def write_index(index):
    BM25_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BM25_DIR / f"index.json.tmp.{os.getpid()}"
    try:
        tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, INDEX_PATH)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def load_index_or_none():
    """None on missing/corrupt index — importable by semantic-search.py
    (--hybrid) without sys.exit side effects."""
    if not INDEX_PATH.is_file():
        return None
    try:
        idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        assert isinstance(idx.get("vocab"), dict) and isinstance(idx.get("docs"), dict)
        return idx
    except Exception:
        return None


def score_query(idx, text, top_k=20):
    """[(path, score)] ranked desc. Pure function over a loaded index."""
    vocab = idx["vocab"]
    docs = idx["docs"]
    k1 = idx["params"]["k1"]
    b = idx["params"]["b"]
    N = idx["doc_count"]
    # Defensive guard (upstream v1.7.2 audit L7): keep the divide safe even if
    # a future refactor lets avg_dl reach 0 with a non-empty vocab.
    avg_dl_safe = idx["avg_dl"] or 1.0

    qterms = tokenize(text)
    if not qterms:
        return []

    scores = defaultdict(float)
    for term in qterms:
        v = vocab.get(term)
        if not v:
            continue
        df = v["df"]
        idf = math.log(1 + (N - df + 0.5) / (df + 0.5))
        for path, cnt in v["postings"]:
            dl = docs[path]["dl"]
            denom = cnt + k1 * (1 - b + b * dl / avg_dl_safe)
            scores[path] += idf * (cnt * (k1 + 1)) / denom

    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]


def load_index_or_exit():
    idx = load_index_or_none()
    if idx is None:
        log(f"ERR: no readable index at {INDEX_PATH}. Run `bm25-index.py build` first.")
        sys.exit(EXIT_INDEX_MISSING)
    return idx


def main():
    parser = argparse.ArgumentParser(description="BM25 inverted index over wiki pages.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp_build = sub.add_parser("build", help="Full rebuild (cheap at vault scale).")
    sp_build.add_argument("--quiet", action="store_true", help="Suppress the summary log line")

    sp_query = sub.add_parser("query", help="Query the index.")
    sp_query.add_argument("text", help="Query text")
    sp_query.add_argument("--top", type=int, default=20, help="Top-K results")

    sub.add_parser("stats", help="Print index stats.")

    args = parser.parse_args()

    if args.cmd == "build":
        fd = acquire_lock()
        try:
            index = build_index()
            if index is None:
                log("Nothing to index.")
                return EXIT_OK
            write_index(index)
            if not args.quiet:
                log(f"Wrote {INDEX_PATH}  docs={index['doc_count']}  "
                    f"vocab={len(index['vocab'])}  avg_dl={index['avg_dl']:.1f}")
        finally:
            release_lock(fd)
        return EXIT_OK

    if args.cmd == "query":
        idx = load_index_or_exit()
        results = score_query(idx, args.text, top_k=args.top)
        print(f"# bm25 top-{args.top} for: {args.text}  (index: {idx['doc_count']} pages)")
        for path, score in results:
            print(f"{score:.4f}\t{path}")
        return EXIT_OK

    if args.cmd == "stats":
        idx = load_index_or_exit()
        print(json.dumps({
            "doc_count": idx["doc_count"],
            "avg_dl": round(idx["avg_dl"], 2),
            "vocab_size": len(idx["vocab"]),
            "updated_at": idx["updated_at"],
            "params": idx["params"],
            "schema_version": idx["schema_version"],
        }, indent=2))
        return EXIT_OK

    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
