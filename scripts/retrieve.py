#!/usr/bin/env python3
"""Section-level sparse-first retrieval with optional local dense fusion.

Pages are split on H2/H3 headings, then into bounded 800-word windows with
100-word overlap.  Ranking happens on chunks; output is deduplicated to the
best chunk per page and always includes its heading and snippet.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline_events import emit_event
from vault_schema import extract_tags, parse_frontmatter, split_document


ROOT = Path(__file__).resolve().parents[1]
WIKI = ROOT / "wiki"
META = ROOT / ".vault-meta" / "retrieval"
SPARSE_INDEX = META / "index.json"
DENSE_INDEX = META / "dense.json"
LOCK = ROOT / ".vault-meta" / ".retrieval.lock"
DENSE_LOCK = ROOT / ".vault-meta" / ".dense-refresh.lock"

SCHEMA_VERSION = 3
CHUNK_WORDS = 800
CHUNK_OVERLAP = 100
HEADING_LEVELS = (2, 3)
CONTEXTUAL_HEADERS = os.environ.get("RETRIEVAL_CONTEXTUAL_HEADERS") == "1"
RERANKER = os.environ.get("RETRIEVAL_RERANKER", "off")
CHUNK_CONFIG = {
    "words": CHUNK_WORDS,
    "overlap": CHUNK_OVERLAP,
    "heading_levels": list(HEADING_LEVELS),
    "contextual_headers": CONTEXTUAL_HEADERS,
}
MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "bge-m3")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
EMBED_TIMEOUT = 30
EMBED_BATCH = 16
DENSE_CHECKPOINT_SECONDS = 10.0
RRF_K = 60
SPARSE_WEIGHT = 1.2
DENSE_WEIGHT = 1.0

TOKEN_RX = re.compile(r"\w[\w'\-]*", re.UNICODE)
STOPWORDS = frozenset(
    """
a an and are as at be by for from has have in is it of on or that the this to was were with
и в на не что как по для из это при или если так же но да уже есть нет
""".split()
)
HEADING_RX = re.compile(r"^(#{2,3})\s+(.+?)\s*$")


@dataclass(frozen=True)
class Chunk:
    id: str
    path: str
    title: str
    heading: str
    start_line: int
    segment: int
    tags: list[str]
    text: str
    content_hash: str


@dataclass
class Result:
    path: str
    title: str
    heading: str
    start_line: int
    snippet: str
    score: float
    sparse_rank: int | None
    dense_rank: int | None


def tokenize(text: str) -> list[str]:
    return [
        token.lower()
        for token in TOKEN_RX.findall(text)
        if len(token) > 1 and token.lower() not in STOPWORDS
    ]


def normalize(text: str) -> str:
    return " ".join(tokenize(text))


def should_skip(path: Path) -> bool:
    rel = path.relative_to(WIKI)
    return (
        "_templates" in rel.parts
        or path.name == "_index.md"
        or (path.name == "log.md" and len(rel.parts) == 1)
    )


def source_files() -> list[Path]:
    if not WIKI.is_dir():
        raise FileNotFoundError(f"wiki directory missing: {WIKI}")
    return [path for path in sorted(WIKI.rglob("*.md")) if not should_skip(path)]


def source_fingerprint() -> str:
    digest = hashlib.sha256(json.dumps(CHUNK_CONFIG, sort_keys=True).encode())
    for path in source_files():
        digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def body_sections(body: str, title: str) -> list[tuple[str, int, str]]:
    lines = body.splitlines()
    boundaries: list[tuple[int, str]] = []
    fence: str | None = None
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence is not None:
            continue
        match = HEADING_RX.match(line)
        if match and len(match.group(1)) in HEADING_LEVELS:
            boundaries.append((index, match.group(2).strip()))
    sections: list[tuple[str, int, str]] = []
    first = boundaries[0][0] if boundaries else len(lines)
    preamble = "\n".join(lines[:first]).strip()
    if preamble:
        sections.append((title, 1, preamble))
    for number, (start, heading) in enumerate(boundaries):
        end = boundaries[number + 1][0] if number + 1 < len(boundaries) else len(lines)
        text = "\n".join(lines[start + 1 : end]).strip()
        if text:
            sections.append((heading, start + 2, text))
    if not sections and body.strip():
        sections.append((title, 1, body.strip()))
    return sections


def window_text(text: str) -> list[str]:
    words = text.split()
    if len(words) <= CHUNK_WORDS:
        return [text.strip()]
    step = CHUNK_WORDS - CHUNK_OVERLAP
    return [" ".join(words[start : start + CHUNK_WORDS]) for start in range(0, len(words), step)]


def chunks_for_page(path: Path) -> list[Chunk]:
    raw = path.read_text(encoding="utf-8")
    document = split_document(raw)
    if document is None:
        fm: dict[str, Any] = {}
        body = raw
    else:
        block, body = document
        fm = parse_frontmatter(block)
    title = str(fm.get("title") or path.stem)
    tags = extract_tags(fm)
    rel = str(path.relative_to(ROOT))
    chunks: list[Chunk] = []
    for heading, line, section in body_sections(body, title):
        for segment, text in enumerate(window_text(section)):
            material = json.dumps(
                [CHUNK_CONFIG, rel, title, heading, segment, tags, text],
                ensure_ascii=False,
                sort_keys=True,
            )
            content_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
            chunks.append(
                Chunk(
                    id=content_hash[:24],
                    path=rel,
                    title=title,
                    heading=heading,
                    start_line=line,
                    segment=segment,
                    tags=tags,
                    text=text,
                    content_hash=content_hash,
                )
            )
    return chunks


def discover_chunks() -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in source_files():
        chunks.extend(chunks_for_page(path))
    return chunks


def weighted_tokens(chunk: Chunk) -> list[str]:
    terms = (
        tokenize(chunk.title) * 4
        + tokenize(" ".join(chunk.tags)) * 3
        + tokenize(chunk.heading) * 3
        + tokenize(chunk.text)
    )
    if CONTEXTUAL_HEADERS:
        terms += tokenize(contextual_header(asdict(chunk))) * 2
    return terms


def contextual_header(doc: dict[str, Any]) -> str:
    tags = ", ".join(doc.get("tags") or [])
    return (
        f"Document {doc.get('title', '')}. Section {doc.get('heading', '')}. "
        f"Vault path {doc.get('path', '')}. Tags {tags}."
    )


def build_sparse() -> dict[str, Any]:
    chunks = discover_chunks()
    if not chunks:
        raise RuntimeError("no chunks to index")
    docs: dict[str, dict[str, Any]] = {}
    df: Counter[str] = Counter()
    postings: dict[str, list[list[Any]]] = defaultdict(list)
    for chunk in chunks:
        terms = weighted_tokens(chunk)
        counts = Counter(terms)
        docs[chunk.id] = {**asdict(chunk), "dl": len(terms)}
        for term, count in counts.items():
            df[term] += 1
            postings[term].append([chunk.id, count])
    average = sum(doc["dl"] for doc in docs.values()) / len(docs)
    return {
        "schema_version": SCHEMA_VERSION,
        "chunk_config": CHUNK_CONFIG,
        "source_fingerprint": source_fingerprint(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "page_count": len({chunk.path for chunk in chunks}),
        "chunk_count": len(chunks),
        "avg_dl": average,
        "params": {"k1": 1.5, "b": 0.75},
        "docs": docs,
        "vocab": {
            term: {"df": df[term], "postings": postings[term]}
            for term in sorted(postings)
        },
    }


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_sparse(index: dict[str, Any]) -> None:
    atomic_write(SPARSE_INDEX, json.dumps(index, ensure_ascii=False, separators=(",", ":")))


def load_sparse() -> dict[str, Any] | None:
    try:
        index = json.loads(SPARSE_INDEX.read_text(encoding="utf-8"))
        if (
            index.get("schema_version") != SCHEMA_VERSION
            or index.get("chunk_config") != CHUNK_CONFIG
            or not isinstance(index.get("docs"), dict)
            or not isinstance(index.get("vocab"), dict)
        ):
            return None
        return index
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def acquire_lock(timeout: float = 5.0):
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK.open("a+", encoding="utf-8")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise TimeoutError("retrieval index lock busy")
            time.sleep(0.05)


def ensure_sparse(force: bool = False) -> tuple[dict[str, Any], bool]:
    existing = load_sparse()
    fingerprint = source_fingerprint()
    if not force and existing is not None and existing.get("source_fingerprint") == fingerprint:
        return existing, False
    handle = acquire_lock()
    try:
        existing = load_sparse()
        if not force and existing is not None and existing.get("source_fingerprint") == fingerprint:
            return existing, False
        index = build_sparse()
        write_sparse(index)
        return index, True
    finally:
        handle.close()


def exact_boost(doc: dict[str, Any], query: str, qterms: list[str]) -> float:
    qnorm = normalize(query)
    title = normalize(str(doc["title"]))
    heading = normalize(str(doc["heading"]))
    tags = {normalize(str(tag)) for tag in doc.get("tags", [])}
    boost = 0.0
    if qnorm and qnorm == title:
        boost += 12.0
    elif qnorm and qnorm in title:
        boost += 6.0
    if qnorm and qnorm == heading:
        boost += 8.0
    elif qnorm and qnorm in heading:
        boost += 4.0
    title_terms = set(tokenize(str(doc["title"])))
    if qterms and set(qterms).issubset(title_terms):
        boost += 4.0
    boost += sum(1.5 for term in set(qterms) if term in tags)
    return boost


def sparse_chunks(index: dict[str, Any], query: str, limit: int = 100) -> list[tuple[str, float]]:
    docs = index["docs"]
    vocab = index["vocab"]
    qterms = tokenize(query)
    if not qterms:
        return []
    k1, b = index["params"]["k1"], index["params"]["b"]
    count = index["chunk_count"]
    average = index["avg_dl"] or 1.0
    scores: dict[str, float] = defaultdict(float)
    for term in qterms:
        item = vocab.get(term)
        if not item:
            continue
        df = item["df"]
        inverse = math.log(1 + (count - df + 0.5) / (df + 0.5))
        for chunk_id, frequency in item["postings"]:
            length = docs[chunk_id]["dl"]
            denominator = frequency + k1 * (1 - b + b * length / average)
            scores[chunk_id] += inverse * (frequency * (k1 + 1)) / denominator
    for chunk_id in list(scores):
        scores[chunk_id] += exact_boost(docs[chunk_id], query, qterms)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(value * value for value in left))
    norm_right = math.sqrt(sum(value * value for value in right))
    return dot / (norm_left * norm_right) if norm_left and norm_right else 0.0


def embed_batch(texts: list[str], model: str = MODEL) -> list[list[float]]:
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=json.dumps(
            {"model": model, "input": texts, "options": {"num_ctx": 8192}}
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=EMBED_TIMEOUT) as response:
        data = json.loads(response.read())
    embeddings = data.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        raise RuntimeError("ollama returned an invalid embedding batch")
    return embeddings


def embed(text: str, model: str = MODEL) -> list[float]:
    return embed_batch([text], model)[0]


def dense_document(doc: dict[str, Any]) -> str:
    prefix = contextual_header(doc) + "\n" if CONTEXTUAL_HEADERS else ""
    return f"{prefix}{doc['title']}\n{doc['heading']}\n{doc['text']}"[:16000]


def dense_snapshot(
    index: dict[str, Any], embeddings: dict[str, list[float]], *, complete: bool
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "model": MODEL,
        "chunk_config": CHUNK_CONFIG,
        "source_fingerprint": index["source_fingerprint"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "complete": complete,
        "embeddings": embeddings,
    }


def write_dense_snapshot(
    index: dict[str, Any], embeddings: dict[str, list[float]], *, complete: bool
) -> dict[str, Any]:
    dense = dense_snapshot(index, embeddings, complete=complete)
    atomic_write(DENSE_INDEX, json.dumps(dense, separators=(",", ":")))
    return dense


def _refresh_dense_unlocked(index: dict[str, Any], *, quiet: bool = False) -> dict[str, Any]:
    try:
        current = json.loads(DENSE_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        current = {}
    reusable = (
        current.get("model") == MODEL
        and current.get("chunk_config") == CHUNK_CONFIG
        and isinstance(current.get("embeddings"), dict)
    )
    old = current.get("embeddings", {}) if reusable else {}
    embeddings = {chunk_id: old[chunk_id] for chunk_id in index["docs"] if chunk_id in old}
    missing = [chunk_id for chunk_id in index["docs"] if chunk_id not in embeddings]
    initial_count = len(embeddings)
    checkpoint_at = time.monotonic() + DENSE_CHECKPOINT_SECONDS
    try:
        for start in range(0, len(missing), EMBED_BATCH):
            batch = missing[start : start + EMBED_BATCH]
            vectors = embed_batch([dense_document(index["docs"][chunk_id]) for chunk_id in batch])
            if len(vectors) != len(batch):
                raise RuntimeError("embedding batch size mismatch")
            embeddings.update(dict(zip(batch, vectors)))
            if time.monotonic() >= checkpoint_at:
                write_dense_snapshot(index, embeddings, complete=False)
                checkpoint_at = time.monotonic() + DENSE_CHECKPOINT_SECONDS
    except Exception:
        if len(embeddings) > initial_count:
            write_dense_snapshot(index, embeddings, complete=False)
        raise
    dense = write_dense_snapshot(index, embeddings, complete=True)
    if not quiet:
        print(f"dense refresh: {len(missing)} new, {len(embeddings)} total", file=sys.stderr)
    return dense


def refresh_dense(index: dict[str, Any], *, quiet: bool = False) -> dict[str, Any]:
    """Refresh dense embeddings under one cross-process non-blocking lock."""
    DENSE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = DENSE_LOCK.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("dense refresh already in progress") from exc
        return _refresh_dense_unlocked(index, quiet=quiet)
    finally:
        handle.close()


def load_dense(index: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    try:
        dense = json.loads(DENSE_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None, "dense cache missing"
    embeddings = dense.get("embeddings")
    if (
        dense.get("schema_version") != SCHEMA_VERSION
        or dense.get("model") != MODEL
        or dense.get("chunk_config") != CHUNK_CONFIG
        or dense.get("source_fingerprint") != index.get("source_fingerprint")
        or dense.get("complete") is False
        or not isinstance(embeddings, dict)
        or set(embeddings) != set(index["docs"])
    ):
        return None, "dense cache stale"
    return dense, None


def rrf_fuse(
    ranked_lists: list[list[str]], weights: list[float] | None = None
) -> list[tuple[str, float]]:
    scores: dict[str, float] = defaultdict(float)
    for channel, ranked in enumerate(ranked_lists):
        weight = weights[channel] if weights else 1.0
        for rank, chunk_id in enumerate(ranked, 1):
            scores[chunk_id] += weight / (RRF_K + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def lexical_relevance(doc: dict[str, Any], query: str) -> float:
    qterms = set(tokenize(query))
    if not qterms:
        return 0.0
    title = set(tokenize(doc.get("title", "")))
    heading = set(tokenize(doc.get("heading", "")))
    tags = set(tokenize(" ".join(doc.get("tags") or [])))
    body = set(tokenize(doc.get("text", "")))
    coverage = len(qterms & (title | heading | tags | body)) / len(qterms)
    weighted = (
        4 * len(qterms & title)
        + 3 * len(qterms & heading)
        + 2 * len(qterms & tags)
        + len(qterms & body)
    ) / len(qterms)
    phrase = 1.0 if normalize(query) and normalize(query) in normalize(
        f"{doc.get('title', '')} {doc.get('heading', '')} {doc.get('text', '')}"
    ) else 0.0
    return coverage + 0.15 * weighted + 0.5 * phrase


def lexical_rerank(
    index: dict[str, Any], query: str, fused: list[tuple[str, float]], limit: int = 60
) -> list[tuple[str, float]]:
    head = fused[:limit]
    reranked = sorted(
        head,
        key=lambda item: (
            -lexical_relevance(index["docs"][item[0]], query),
            -item[1],
            item[0],
        ),
    )
    return reranked + fused[limit:]


def snippet(text: str, query: str, words: int = 55) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    pieces = clean.split()
    if len(pieces) <= words:
        return clean
    qterms = set(tokenize(query))
    hit = next(
        (index for index, value in enumerate(pieces) if qterms & set(tokenize(value))),
        0,
    )
    start = max(0, hit - words // 3)
    end = min(len(pieces), start + words)
    return ("…" if start else "") + " ".join(pieces[start:end]) + ("…" if end < len(pieces) else "")


def retrieve(
    index: dict[str, Any], query: str, *, top: int = 5, dense_mode: str = "auto"
) -> tuple[list[Result], dict[str, Any]]:
    limit = max(top * 12, 60)
    sparse = sparse_chunks(index, query, limit)
    sparse_rank = {chunk_id: rank for rank, (chunk_id, _) in enumerate(sparse, 1)}
    dense_rank: dict[str, int] = {}
    degraded = False
    reason: str | None = None

    fused: list[tuple[str, float]]
    if dense_mode == "off":
        fused = sparse
    else:
        dense, reason = load_dense(index)
        if dense is None:
            degraded = True
            fused = sparse
        else:
            try:
                query_vector = embed(query, dense["model"])
                dense_scored = sorted(
                    (
                        (chunk_id, cosine(query_vector, vector))
                        for chunk_id, vector in dense["embeddings"].items()
                    ),
                    key=lambda item: (-item[1], item[0]),
                )[:limit]
                dense_rank = {
                    chunk_id: rank for rank, (chunk_id, _) in enumerate(dense_scored, 1)
                }
                fused = rrf_fuse(
                    [[chunk_id for chunk_id, _ in sparse], [chunk_id for chunk_id, _ in dense_scored]],
                    [SPARSE_WEIGHT, DENSE_WEIGHT],
                )
                reason = None
            except (OSError, RuntimeError, urllib.error.URLError) as exc:
                degraded = True
                reason = f"dense query unavailable: {exc}"
                fused = sparse

    if RERANKER == "lexical":
        fused = lexical_rerank(index, query, fused)

    results: list[Result] = []
    seen: set[str] = set()
    for chunk_id, score in fused:
        doc = index["docs"].get(chunk_id)
        if not doc or doc["path"] in seen:
            continue
        seen.add(doc["path"])
        results.append(
            Result(
                path=doc["path"],
                title=doc["title"],
                heading=doc["heading"],
                start_line=doc["start_line"],
                snippet=snippet(doc["text"], query),
                score=float(score),
                sparse_rank=sparse_rank.get(chunk_id),
                dense_rank=dense_rank.get(chunk_id),
            )
        )
        if len(results) >= top:
            break
    meta = {
        "mode": "hybrid" if dense_rank else "sparse",
        "degraded": degraded,
        "reason": reason,
        "pages": index["page_count"],
        "chunks": index["chunk_count"],
        "model": MODEL if dense_rank else None,
        "contextual_headers": CONTEXTUAL_HEADERS,
        "reranker": RERANKER,
    }
    return results, meta


def query_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Section-level vault retrieval")
    parser.add_argument("query", nargs="+", help="Query text")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dense", choices=("auto", "off", "on"), default="auto")
    parser.add_argument(
        "--read-only", action="store_true",
        help="build stale sparse state in memory and emit no telemetry/index writes",
    )
    args = parser.parse_args(argv)
    if args.top < 1:
        parser.error("--top must be positive")
    query = " ".join(args.query)
    try:
        if args.read_only:
            index = load_sparse()
            if index is None or index.get("source_fingerprint") != source_fingerprint():
                index = build_sparse()
        else:
            index, _ = ensure_sparse()
        results, meta = retrieve(index, query, top=args.top, dense_mode=args.dense)
    except (FileNotFoundError, RuntimeError, TimeoutError) as exc:
        if not args.read_only:
            emit_event(
                "retrieve",
                actor="retrieve",
                counts={"requested": args.top, "results": 0},
                status="error",
                root=ROOT,
            )
        print(f"retrieve: {exc}", file=sys.stderr)
        return 3
    if not args.read_only:
        emit_event(
            "retrieve",
            actor="retrieve",
            paths=[item.path for item in results],
            counts={
                "requested": args.top,
                "results": len(results),
                "pages": meta["pages"],
                "chunks": meta["chunks"],
                "dense_used": int(meta["mode"] == "hybrid"),
                "degraded": int(bool(meta["degraded"])),
            },
            status="degraded" if meta["degraded"] else "ok",
            root=ROOT,
        )
    if args.json:
        print(
            json.dumps(
                {"query": query, "meta": meta, "results": [asdict(item) for item in results]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    reason = f" reason={json.dumps(meta['reason'], ensure_ascii=False)}" if meta["reason"] else ""
    print(
        f"# retrieval top-{args.top} mode={meta['mode']} degraded={str(meta['degraded']).lower()}"
        f" pages={meta['pages']} chunks={meta['chunks']}{reason}"
    )
    for rank, item in enumerate(results, 1):
        channels = f"sparse#{item.sparse_rank or '-'},dense#{item.dense_rank or '-'}"
        anchor = f"#{item.heading}" if item.heading and item.heading != item.title else ""
        print(f"{rank}. {item.path}{anchor}  score={item.score:.4f}  ({channels})")
        print(f"   {item.snippet}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"build", "ensure", "refresh-dense"}:
        command = argv.pop(0)
        quiet = "--quiet" in argv
        if any(arg != "--quiet" for arg in argv):
            print(f"usage: retrieve.py {command} [--quiet]", file=sys.stderr)
            return 2
        try:
            index, rebuilt = ensure_sparse(force=command == "build")
            dense_total = 0
            if command == "refresh-dense":
                dense = refresh_dense(index, quiet=quiet)
                dense_total = len(dense["embeddings"])
            elif not quiet:
                print(
                    f"retrieval index {'rebuilt' if rebuilt else 'fresh'}: "
                    f"{index['page_count']} pages, {index['chunk_count']} chunks",
                    file=sys.stderr,
                )
            emit_event(
                "dense-refresh" if command == "refresh-dense" else "retrieval-index",
                actor="retrieve",
                counts={
                    "pages": index["page_count"],
                    "chunks": index["chunk_count"],
                    "rebuilt": int(rebuilt),
                    "dense_total": dense_total,
                },
                root=ROOT,
            )
            return 0
        except urllib.error.HTTPError as exc:
            emit_event(
                "dense-refresh" if command == "refresh-dense" else "retrieval-index",
                actor="retrieve",
                counts={"exit_code": 11},
                status="degraded" if command == "refresh-dense" else "error",
                root=ROOT,
            )
            print(f"retrieve: ollama/model error: {exc}", file=sys.stderr)
            return 11
        except (urllib.error.URLError, OSError, RuntimeError, TimeoutError) as exc:
            exit_code = 10 if command == "refresh-dense" else 3
            emit_event(
                "dense-refresh" if command == "refresh-dense" else "retrieval-index",
                actor="retrieve",
                counts={"exit_code": exit_code},
                status="degraded" if command == "refresh-dense" else "error",
                root=ROOT,
            )
            print(f"retrieve: {exc}", file=sys.stderr)
            return exit_code
    return query_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
