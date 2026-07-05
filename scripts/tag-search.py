#!/usr/bin/env python3
"""Tag prefilter over the vault using the reverse tag index.

Reuses .vault-meta/tag-index.json (built by scripts/reindex.py, refreshed by
the Stop hook every turn) to narrow query candidates by frontmatter tags —
the cheapest, most precise relevance signal — before any page is opened.
Read-only; pure stdlib; no ollama required.

Matching (query word vs tag, both normalized to lowercase):
  - whole-tag match:     "argocd"      ~ tag "argocd"
  - tag-token match:     "access"      ~ tag "access-api" (split on -_/. )
  - adjacent bigram:     "access api"  ~ tag "access-api"

Pages are ranked by the number of DISTINCT matched tags (tie-break: total
matched words desc, then path asc).

Usage:
    ./scripts/tag-search.py "<query>" [--top N]

Exit codes (mirror semantic-search.py): 0 ok (incl. zero hits), 2 usage,
3 index missing/corrupt.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

VAULT_ROOT = Path(__file__).resolve().parent.parent
TAG_INDEX = VAULT_ROOT / ".vault-meta" / "tag-index.json"

WORD_RX = re.compile(r"\w+", re.UNICODE)
TOKEN_SPLIT_RX = re.compile(r"[-_/.\s]+")


def normalize(tag: str) -> str:
    return tag.strip().lower()


def tag_tokens(tag: str) -> set[str]:
    return {t for t in TOKEN_SPLIT_RX.split(normalize(tag)) if t}


def words_of(query: str) -> list[str]:
    return [w.lower() for w in WORD_RX.findall(query) if len(w) >= 2]


def match_tags(words: list[str], tags: list[str]) -> dict[str, set[str]]:
    """tag -> set of query words that matched it."""
    bigrams = {f"{a}-{b}": (a, b) for a, b in zip(words, words[1:])}
    matched: dict[str, set[str]] = defaultdict(set)
    for tag in tags:
        norm = normalize(tag)
        tokens = tag_tokens(tag)
        for w in words:
            if w == norm or w in tokens:
                matched[tag].add(w)
        if norm in bigrams:
            matched[tag].update(bigrams[norm])
    return dict(matched)


def rank_pages(matched: dict[str, set[str]], index: dict) -> list[tuple[str, set[str]]]:
    """[(path, matched_tags)] ranked by distinct matched tags desc, then total
    matched words desc, then path asc. Reused by retrieval-bench.py."""
    page_tags: dict[str, set[str]] = defaultdict(set)
    page_words: dict[str, set[str]] = defaultdict(set)
    for tag, tag_words in matched.items():
        for path in index.get(tag, []):
            page_tags[path].add(tag)
            page_words[path].update(tag_words)
    ranked = sorted(
        page_tags,
        key=lambda p: (-len(page_tags[p]), -len(page_words[p]), p),
    )
    return [(p, page_tags[p]) for p in ranked]


def main() -> int:
    argv = sys.argv[1:]
    top = 10
    args: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--top":
            try:
                top = int(argv[i + 1])
                i += 2
                continue
            except (IndexError, ValueError):
                print("usage: tag-search.py '<query>' [--top N]", file=sys.stderr)
                return 2
        args.append(argv[i])
        i += 1
    if not args:
        print("usage: tag-search.py '<query>' [--top N]", file=sys.stderr)
        return 2
    words = words_of(" ".join(args))
    if not words:
        print("usage: tag-search.py '<query>' [--top N] — query has no usable words",
              file=sys.stderr)
        return 2

    if not TAG_INDEX.is_file():
        print("tag index missing — run ./scripts/reindex.py first", file=sys.stderr)
        return 3
    try:
        index = json.loads(TAG_INDEX.read_text(encoding="utf-8"))
        assert isinstance(index, dict)
    except Exception as exc:
        print(f"tag index unreadable: {exc}", file=sys.stderr)
        return 3

    matched = match_tags(words, list(index.keys()))
    if not matched:
        print(f"# tag top-{top} for: {' '.join(words)}  (0 matching tags of {len(index)})")
        return 0

    ranked = rank_pages(matched, index)
    print(f"# tag top-{top} for: {' '.join(words)}  "
          f"(matched tags: {', '.join(sorted(matched))})")
    for path, ptags in ranked[:top]:
        print(f"{len(ptags)}\t{path}\t{','.join(sorted(ptags))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
