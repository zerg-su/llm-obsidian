#!/usr/bin/env python3
"""
Rebuild vault retrieval indexes from wiki/ frontmatter.

Outputs (in .vault-meta/, auto-committed by Stop-hook):
  - index.jsonl              one JSON per page (path, address, type, status,
                             tags, sessions, created, updated, related, title)
  - address-map.tsv          c-NNNNNN<TAB>relative-path  (O(1) address lookup)
  - session-to-pages.jsonl   one JSON per session id: {sid, pages: [paths]}
  - tag-index.json           {tag: [paths]} reverse index for multi-tag queries
  - recent.jsonl             top 50 pages by `updated:` desc (source for
                             auto-regen hot.md)

Reads simple frontmatter (key/value, flow-lists, block-lists, sessions sub-keys).
Pure stdlib — no pyyaml dependency.

Skips: log.md (linear, not indexed), _templates/, files without frontmatter,
session ids that are literal environment templates (e.g. "${CLAUDE_CODE_SESSION_ID}").

Run from repo root: ./scripts/reindex.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from vault_schema import DATE_RX, parse_frontmatter, split_frontmatter


REPO_ROOT = Path(__file__).resolve().parents[1]
WIKI = REPO_ROOT / "wiki"
META = REPO_ROOT / ".vault-meta"

SKIP_PATHS = {"log.md", "_templates"}

ADDRESS_RX = re.compile(r"^c-\d{6}$")
SID_TEMPLATES = {"${CLAUDE_CODE_SESSION_ID}", "${CODEX_THREAD_ID}"}


def atomic_write(path: Path, text: str) -> None:
    """Write via same-dir tmp + os.replace: a crash mid-write never leaves a
    half-written index (a parallel reader sees either the old or the new file).
    The ".tmp." infix keeps strays covered by the repo's *.tmp.* gitignore."""
    tmp = path.parent / f"{path.name}.tmp.{os.getpid()}"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def extract_sessions(fm: dict[str, Any]) -> list[str]:
    """Return list of session IDs, skipping template literals."""
    raw = fm.get("sessions")
    if not raw:
        return []
    out: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            sid = item.get("id") if isinstance(item, dict) else item
            if isinstance(sid, str) and sid and sid not in SID_TEMPLATES:
                out.append(sid)
    elif isinstance(raw, str) and raw not in SID_TEMPLATES:
        out.append(raw)
    return out


def extract_tags(fm: dict[str, Any]) -> list[str]:
    raw = fm.get("tags")
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, str)]
    return []


def extract_related(fm: dict[str, Any]) -> list[str]:
    raw = fm.get("related")
    if not raw:
        return []
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, str)]
    return []


def should_skip(path: Path) -> bool:
    rel = path.relative_to(WIKI)
    parts = set(rel.parts)
    if "_templates" in parts:
        return True
    if rel.name == "log.md":
        return True
    return False


def build_indexes() -> tuple[list[dict], dict[str, list[str]]]:
    pages: list[dict[str, Any]] = []
    session_pages: dict[str, list[str]] = defaultdict(list)
    for path in sorted(WIKI.rglob("*.md")):
        if should_skip(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        block = split_frontmatter(text)
        if block is None:
            continue
        fm = parse_frontmatter(block)
        rel = str(path.relative_to(REPO_ROOT))
        sessions = extract_sessions(fm)
        page = {
            "path": rel,
            "address": fm.get("address") if isinstance(fm.get("address"), str) else None,
            "type": fm.get("type"),
            "status": fm.get("status"),
            "title": fm.get("title"),
            "created": fm.get("created"),
            "updated": fm.get("updated"),
            "tags": extract_tags(fm),
            "sessions": sessions,
            "related": extract_related(fm),
        }
        pages.append(page)
        for sid in sessions:
            session_pages[sid].append(rel)
    return pages, dict(session_pages)


def write_outputs(pages: list[dict], session_pages: dict[str, list[str]]) -> None:
    META.mkdir(parents=True, exist_ok=True)

    # index.jsonl
    atomic_write(
        META / "index.jsonl",
        "".join(json.dumps(p, ensure_ascii=False, sort_keys=True) + "\n" for p in pages),
    )

    # address-map.tsv (sorted by address for stable diffs)
    addr_pairs = [
        (p["address"], p["path"]) for p in pages if p["address"] and ADDRESS_RX.match(p["address"])
    ]
    addr_pairs.sort()
    atomic_write(
        META / "address-map.tsv",
        "".join(f"{addr}\t{path}\n" for addr, path in addr_pairs),
    )

    # session-to-pages.jsonl
    atomic_write(
        META / "session-to-pages.jsonl",
        "".join(
            json.dumps(
                {"sid": sid, "pages": sorted(set(session_pages[sid]))},
                ensure_ascii=False,
            )
            + "\n"
            for sid in sorted(session_pages.keys())
        ),
    )

    # tag-index.json — reverse: tag -> [paths]
    tag_idx: dict[str, list[str]] = defaultdict(list)
    for p in pages:
        for tag in p.get("tags", []) or []:
            if isinstance(tag, str) and tag.strip():
                tag_idx[tag].append(p["path"])
    # Sort paths within each tag for stable diffs
    tag_idx_sorted = {tag: sorted(set(paths)) for tag, paths in sorted(tag_idx.items())}
    atomic_write(
        META / "tag-index.json",
        json.dumps(tag_idx_sorted, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

    # recent.jsonl — top 50 by updated desc (fallback to created if no updated)
    def page_sort_key(p: dict) -> str:
        return (p.get("updated") or p.get("created") or "0000-00-00")
    recent = sorted(pages, key=page_sort_key, reverse=True)[:50]
    atomic_write(
        META / "recent.jsonl",
        "".join(
            json.dumps({
                "path": p["path"],
                "title": p.get("title"),
                "type": p.get("type"),
                "status": p.get("status"),
                "updated": p.get("updated"),
                "created": p.get("created"),
                "address": p.get("address"),
                "tags": p.get("tags", []),
            }, ensure_ascii=False) + "\n"
            for p in recent
        ),
    )


AUTO_START = "<!-- AUTO-INDEX START -->"
AUTO_END = "<!-- AUTO-INDEX END -->"
AUTO_DATE = "<!-- AUTO-DATE -->"
FOLDER_INDEX_SKIP = {"daily", "_templates"}

_INDEX_TEMPLATE = """---
type: meta
title: "{folder} Index"
created: {today}
updated: {today}
tags:
  - index
status: evergreen
sessions: []
---

# {folder}/

Автогенерируемый листинг (reindex.py --folder-indexes). Ручной текст вне маркеров сохраняется.

{block}
"""


def write_folder_indexes(pages: list[dict]) -> int:
    """Regenerate the AUTO-INDEX block in every top-level folder's _index.md.

    Manual prose outside the markers is preserved; missing _index.md files are
    created from a minimal template. Files are written only when the content
    actually changed (the Stop hook runs this every turn — avoid commit churn).
    """
    by_folder: dict[str, list[dict]] = defaultdict(list)
    for p in pages:
        rel = Path(p["path"]).relative_to("wiki")
        if len(rel.parts) < 2 or rel.name == "_index.md":
            continue
        folder = rel.parts[0]
        if folder in FOLDER_INDEX_SKIP:
            continue
        by_folder[folder].append(p)

    def latest_content_date(items: list[dict]) -> str:
        dates = [
            value
            for item in items
            for value in (item.get("updated"), item.get("created"))
            if isinstance(value, str) and DATE_RX.fullmatch(value)
        ]
        return max(dates) if dates else time.strftime("%Y-%m-%d")

    today = time.strftime("%Y-%m-%d")
    written = 0
    for folder, items in sorted(by_folder.items()):
        listing_date = latest_content_date(items)
        items.sort(key=lambda p: (p.get("updated") or p.get("created") or ""), reverse=True)
        listing = []
        for p in items:
            stem = Path(p["path"]).stem
            bits = [b for b in (p.get("status"), p.get("updated")) if b]
            addr = f" `{p['address']}`" if p.get("address") else ""
            listing.append(f"- [[{stem}]] — {', '.join(bits)}{addr}")
        block = "\n".join(
            [AUTO_START, f"_{len(items)} pages, updated {listing_date}_", ""]
            + listing
            + [AUTO_END]
        )

        idx_file = WIKI / folder / "_index.md"
        if idx_file.exists():
            text = idx_file.read_text(encoding="utf-8")
            if AUTO_START in text and AUTO_END in text:
                pre, _, rest = text.partition(AUTO_START)
                _, _, post = rest.partition(AUTO_END)
                new_text = pre + block + post
            else:
                new_text = text.rstrip("\n") + "\n\n" + block + "\n"
        else:
            new_text = _INDEX_TEMPLATE.format(folder=folder, today=listing_date, block=block)
        if not idx_file.exists() or idx_file.read_text(encoding="utf-8") != new_text:
            atomic_write(idx_file, new_text)
            written += 1

    # index.md freshness stamp
    index_file = WIKI / "index.md"
    if index_file.exists():
        text = index_file.read_text(encoding="utf-8")
        new_text, n = re.subn(
            rf"{re.escape(AUTO_DATE)}\s*\d{{4}}-\d{{2}}-\d{{2}}",
            f"{AUTO_DATE} {today}",
            text,
        )
        if n and new_text != text:
            atomic_write(index_file, new_text)
    return written


def main(argv: list[str]) -> int:
    pages, session_pages = build_indexes()
    write_outputs(pages, session_pages)
    if "--folder-indexes" in argv:
        n = write_folder_indexes(pages)
        if "--quiet" not in argv and n:
            print(f"reindex: {n} folder _index.md regenerated", file=sys.stderr)
    # No-frontmatter files are invisible to every retrieval index — always surface them.
    indexed = {p["path"] for p in pages}
    invisible = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted(WIKI.rglob("*.md"))
        if not should_skip(path) and str(path.relative_to(REPO_ROOT)) not in indexed
    ]
    if invisible:
        print(
            f"reindex: {len(invisible)} file(s) without frontmatter are INVISIBLE to indexes: "
            + ", ".join(invisible),
            file=sys.stderr,
        )
    if "--quiet" not in argv:
        tag_count = sum(1 for p in pages if p.get("tags"))
        print(
            f"reindex: {len(pages)} pages, "
            f"{sum(1 for p in pages if p['address'])} with address, "
            f"{len(session_pages)} unique sessions, "
            f"{tag_count} tagged pages",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
