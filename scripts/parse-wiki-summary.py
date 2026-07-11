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


def fail(code: int, msg: str) -> int:
    print(f"parse-wiki-summary: {msg}", file=sys.stderr)
    return code


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, help="legacy Markdown summary input")
    parser.add_argument("--json-file", type=Path, help="canonical v1 JSON summary input")
    parser.add_argument("--render-markdown", action="store_true", help="render validated canonical JSON")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    if args.json_file:
        try:
            raw_json = json.loads(args.json_file.read_text(encoding="utf-8"))
            summary = validate_summary(raw_json, require_schema=True)
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
