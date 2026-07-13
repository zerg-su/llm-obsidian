#!/usr/bin/env python3
"""Deterministic parser for the '## Wiki Summary' handoff block.

/reap (Phase 1.3-1.4) feeds it the raw fetch (.task-summary.md content or
cmux read-screen scrollback); the script isolates the LAST '## Wiki Summary'
block, validates the header fields and emits JSON. Replaces model-side
parsing where a typo'd type silently misrouted the page.

Rules:
  - block   = from the last '## Wiki Summary' to the next '^## ' or EOF
  - type    must be one of: session decision runbook incident
             service-update repo-touch                     -> exit 2 otherwise
  - title   non-empty, no unresolved '<placeholder>'       -> exit 2 otherwise
  - session optional (old splits) -> stderr warning, still exit 0
  - body    everything after the header lines

Usage:
  cat .task-summary.md | ./scripts/parse-wiki-summary.py
  ./scripts/parse-wiki-summary.py --file <path>
  ./scripts/parse-wiki-summary.py --json-file .task-summary.json
  ./scripts/parse-wiki-summary.py --json-file .task-summary.json --render-markdown

stdout: {"schema_version": 1, "type": ..., "title": ..., "session": ..., "body": ...}
Exit codes: 0 ok, 2 invalid block, 3 usage/io error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from wiki_summary_contract import TYPES, WikiSummaryError, render_markdown, validate_summary

HEADER_RX = re.compile(r"^(type|title|session):\s*(.*)$")
MARKER = "## Wiki Summary"
REVIEW_ARCHIVE_PREFIX = "wiki/meta/reviews/"


def attach_review_archive(summary: dict[str, object], marker_path: Path | None) -> dict[str, object]:
    if marker_path is None or not marker_path.is_file():
        return summary
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WikiSummaryError(f"invalid review archive marker: {exc}") from exc
    if not isinstance(marker, dict) or marker.get("schema_version") != 1:
        raise WikiSummaryError("review archive marker must be a schema_version 1 object")
    title = str(marker.get("title") or "").strip()
    review_id = str(marker.get("review_id") or "").strip()
    path = str(marker.get("path") or "").strip()
    status = str(marker.get("status") or "").strip()
    if not title or len(title) > 500 or "\n" in title or "[[" in title or "]]" in title:
        raise WikiSummaryError("review archive marker has an invalid title")
    if not review_id or len(review_id) > 100:
        raise WikiSummaryError("review archive marker has an invalid review_id")
    if not path.startswith(REVIEW_ARCHIVE_PREFIX) or not path.endswith(".md") or ".." in Path(path).parts:
        raise WikiSummaryError("review archive marker path is outside wiki/meta/reviews")
    if Path(path).stem != title or marker.get("wikilink") != f"[[{title}]]":
        raise WikiSummaryError("review archive marker title/path/wikilink do not match")
    if status not in {"archived", "already-current"}:
        raise WikiSummaryError("review archive marker is not a completed archive")
    link_line = f"Review archive: [[{title}]]"
    body = str(summary.get("body") or "").rstrip()
    if link_line not in body.splitlines():
        body = f"{body}\n\n{link_line}" if body else link_line
    enriched = dict(summary)
    enriched["body"] = body
    return enriched


def fail(code: int, msg: str) -> int:
    print(f"parse-wiki-summary: {msg}", file=sys.stderr)
    return code


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, help="legacy Markdown summary input")
    parser.add_argument("--json-file", type=Path, help="canonical v1 JSON summary input")
    parser.add_argument("--render-markdown", action="store_true", help="render validated canonical JSON")
    parser.add_argument(
        "--review-archive-marker",
        type=Path,
        help="optional validated .review-archive.json whose wikilink is appended to the body",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    if args.json_file:
        try:
            raw_json = json.loads(args.json_file.read_text(encoding="utf-8"))
            summary = validate_summary(raw_json, require_schema=True)
            summary = attach_review_archive(summary, args.review_archive_marker)
        except (OSError, json.JSONDecodeError, WikiSummaryError) as exc:
            return fail(2, f"invalid canonical JSON: {exc}")
        if not summary["session"]:
            print("parse-wiki-summary: WARN no session in canonical summary", file=sys.stderr)
        print(render_markdown(summary) if args.render_markdown else json.dumps(summary, ensure_ascii=False))
        return 0

    if args.file:
        try:
            raw = args.file.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError as e:
            return fail(3, f"cannot read --file: {e}")
    else:
        raw = sys.stdin.read()

    idx = raw.rfind(MARKER)
    if idx == -1:
        return fail(2, f"no '{MARKER}' block found — ask the task-split to emit one "
                       "(/reap-send or print it in chat)")
    block = raw[idx + len(MARKER):]
    m = re.search(r"^## ", block, flags=re.M)
    if m:
        block = block[: m.start()]

    fields: dict[str, str] = {}
    body_lines: list[str] = []
    in_body = False
    for line in block.split("\n"):
        if not in_body:
            if not line.strip():
                # blank lines between headers are fine; body starts at the
                # first non-header content line
                if fields:
                    body_lines.append(line)
                continue
            hm = HEADER_RX.match(line.strip())
            if hm:
                fields[hm.group(1)] = hm.group(2).strip()
                continue
            in_body = True
        body_lines.append(line)

    try:
        summary = validate_summary(
            {
                "schema_version": 1,
                "type": fields.get("type", ""),
                "title": fields.get("title", ""),
                "session": fields.get("session") or None,
                "body": "\n".join(body_lines).strip("\n"),
            }
        )
        summary = attach_review_archive(summary, args.review_archive_marker)
    except WikiSummaryError as exc:
        return fail(2, str(exc))
    if not summary["session"]:
        print(
            "parse-wiki-summary: WARN no session: field (old split?) — "
            "executor provenance will be missing",
            file=sys.stderr,
        )

    print(render_markdown(summary) if args.render_markdown else json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
