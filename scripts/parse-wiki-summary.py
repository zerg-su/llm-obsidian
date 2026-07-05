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

stdout: {"type": ..., "title": ..., "session": ..., "body": ...}
Exit codes: 0 ok, 2 invalid block, 3 usage/io error.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TYPES = {"session", "decision", "runbook", "incident", "service-update", "repo-touch"}
HEADER_RX = re.compile(r"^(type|title|session):\s*(.*)$")
MARKER = "## Wiki Summary"


def fail(code: int, msg: str) -> int:
    print(f"parse-wiki-summary: {msg}", file=sys.stderr)
    return code


def main(argv: list[str]) -> int:
    if "--file" in argv:
        try:
            raw = Path(argv[argv.index("--file") + 1]).read_text(
                encoding="utf-8", errors="replace"
            )
        except (IndexError, OSError) as e:
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

    typ = fields.get("type", "")
    title = fields.get("title", "")
    session = fields.get("session", "")

    if typ not in TYPES:
        return fail(2, f"type {typ!r} is not one of {sorted(TYPES)}")
    if not title or title.startswith("<"):
        return fail(2, f"title is empty or an unresolved placeholder: {title!r}")
    if session.startswith("<"):
        return fail(2, f"session is an unresolved placeholder: {session!r}")
    if not session:
        print(
            "parse-wiki-summary: WARN no session: field (old split?) — "
            "executor provenance will be missing",
            file=sys.stderr,
        )

    body = "\n".join(body_lines).strip("\n")
    print(json.dumps(
        {"type": typ, "title": title, "session": session or None, "body": body},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
