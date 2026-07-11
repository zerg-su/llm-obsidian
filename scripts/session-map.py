#!/usr/bin/env python3
"""Build a cheap runtime-aware daily session-to-task map.

Lists Claude Code and Codex CLI sessions active on a given date for this vault.
Labels come from wiki pages touched by each session, then fall back to the first
substantive user prompt in the transcript. No AI is used.

Human output is grouped for direct insertion under a daily page's `## Сессии`:

    ### Claude

    - <label> · `<full-session-id>`

    ### Codex

    - <label> · `<full-thread-id>`

Machine output (`--json`) includes an explicit `runtime` field; runtime is
derived from the transcript source, never guessed from the identifier.

Usage:
    ./scripts/session-map.py                 # today
    ./scripts/session-map.py 2026-06-26      # explicit date (YYYY-MM-DD)
    ./scripts/session-map.py --json          # machine form

Exit codes: 0 ok (even if no sessions), 2 no transcript roots found.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterator


VAULT = Path(__file__).resolve().parents[1]
INDEX = VAULT / ".vault-meta" / "index.jsonl"

# Claude Code encodes the project working dir into a slug by replacing every
# non-alphanumeric char with '-' (so '/Users/a.b/x' -> '-Users-a-b-x').
SLUG = re.sub(r"[^A-Za-z0-9]", "-", str(VAULT))
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
CLAUDE_PROJ = CLAUDE_HOME / "projects" / SLUG
CODEX_ROOT = CODEX_HOME / "sessions"

MAX_TITLES = 2
LOW_VALUE_TYPES = {"daily", "meta", "fold", "log"}
RUNTIME_ORDER = ("claude", "codex", "other")
RUNTIME_TITLES = {"claude": "Claude", "codex": "Codex", "other": "Other"}
SYSTEM_PROMPT_PREFIXES = (
    "# AGENTS.md instructions",
    "<environment_context>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<skills_instructions>",
    "<apps_instructions>",
    "<plugins_instructions>",
)


def parse_args(argv: list[str]) -> tuple[str, bool]:
    date = None
    as_json = False
    for arg in argv[1:]:
        if arg == "--json":
            as_json = True
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", arg):
            date = arg
        else:
            print(f"session-map: ignoring unknown arg {arg!r}", file=sys.stderr)
    return date or datetime.date.today().isoformat(), as_json


def iter_jsonl(path: Path) -> Iterator[dict]:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def active_on(path: Path, date: str) -> tuple[bool, float]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False, 0.0
    return datetime.date.fromtimestamp(mtime).isoformat() == date, mtime


def codex_session_id(path: Path) -> str | None:
    record = next(iter_jsonl(path), None)
    if not record or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or Path(cwd).expanduser().resolve() != VAULT.resolve():
        return None
    session_id = payload.get("id") or payload.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


def sessions_on(date: str) -> list[dict]:
    """Return session records sorted by last activity across both runtimes."""

    by_identity: dict[tuple[str, str], dict] = {}
    if CLAUDE_PROJ.is_dir():
        for path in CLAUDE_PROJ.glob("*.jsonl"):
            active, mtime = active_on(path, date)
            if not active:
                continue
            session_id = path.stem
            by_identity[("claude", session_id)] = {
                "mtime": mtime,
                "session": session_id,
                "runtime": "claude",
                "path": path,
            }

    if CODEX_ROOT.is_dir():
        for path in CODEX_ROOT.glob("*/*/*/*.jsonl"):
            active, mtime = active_on(path, date)
            if not active:
                continue
            session_id = codex_session_id(path)
            if not session_id:
                continue
            identity = ("codex", session_id)
            current = by_identity.get(identity)
            if current is None or mtime > current["mtime"]:
                by_identity[identity] = {
                    "mtime": mtime,
                    "session": session_id,
                    "runtime": "codex",
                    "path": path,
                }

    return sorted(by_identity.values(), key=lambda item: (item["mtime"], item["runtime"], item["session"]))


def labels_from_wiki() -> dict[str, list[tuple[str, str]]]:
    """Map session ID to `(type, title)` tuples from the retrieval index."""

    by_session: dict[str, list[tuple[str, str]]] = {}
    if not INDEX.is_file():
        return by_session
    for record in iter_jsonl(INDEX):
        title = record.get("title") or record.get("path", "")
        page_type = record.get("type") or ""
        for session_id in record.get("sessions") or []:
            entries = by_session.setdefault(session_id, [])
            if title not in [item_title for _, item_title in entries]:
                entries.append((page_type, title))
    return by_session


def content_texts(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        item.get("text", "")
        for item in content
        if isinstance(item, dict) and item.get("type") in {"text", "input_text"} and isinstance(item.get("text"), str)
    ]


def prompt_candidates(record: dict, runtime: str) -> list[str]:
    if runtime == "claude":
        if record.get("type") != "user" or record.get("isSidechain"):
            return []
        message = record.get("message")
        return content_texts(message.get("content")) if isinstance(message, dict) else []

    payload = record.get("payload")
    if (
        runtime == "codex"
        and record.get("type") == "response_item"
        and isinstance(payload, dict)
        and payload.get("type") == "message"
        and payload.get("role") == "user"
    ):
        return content_texts(payload.get("content"))
    return []


def clean_prompt(value: str) -> str:
    text = value.strip()
    if not text or text.startswith(SYSTEM_PROMPT_PREFIXES) or text[0] in "[</":
        return ""
    text = re.sub(r"^\*\*[^*]+\*\*\s*", "", text)
    text = re.sub(r"^\(\d{2}\.\d{2}\.\d{4}[^)]*\)\s*", "", text)
    text = " ".join(text.split())
    return text[:80] if len(text.split()) >= 4 else ""


def first_prompt(path: Path, runtime: str) -> str:
    """Return the first substantive typed prompt without tool/wrapper content."""

    for record in iter_jsonl(path):
        for candidate in prompt_candidates(record, runtime):
            text = clean_prompt(candidate)
            if text:
                return text
    return ""


def compose_label(titles: list[str]) -> str:
    if not titles:
        return ""
    head = titles[:MAX_TITLES]
    label = "; ".join(head)
    extra = len(titles) - len(head)
    return label + (f" (+{extra})" if extra > 0 else "")


def build(date: str) -> list[dict]:
    wiki = labels_from_wiki()
    rows: list[dict] = []
    for session in sessions_on(date):
        session_id = session["session"]
        entries = wiki.get(session_id, [])
        high = [title for page_type, title in entries if page_type not in LOW_VALUE_TYPES]
        low = [title for page_type, title in entries if page_type in LOW_VALUE_TYPES]

        label = compose_label(high)
        source = "wiki"
        if not label:
            label = first_prompt(session["path"], session["runtime"])
            source = "prompt" if label else ""
        if not label:
            label = compose_label(low)
            source = "wiki-structural" if label else "none"
        if source == "none":
            continue
        rows.append(
            {
                "session": session_id,
                "runtime": session["runtime"],
                "label": label,
                "source": source,
                "time": datetime.datetime.fromtimestamp(session["mtime"]).strftime("%H:%M"),
                "pages": [title for _, title in entries],
            }
        )
    return rows


def render_markdown(rows: list[dict]) -> list[str]:
    lines: list[str] = []
    for runtime in RUNTIME_ORDER:
        selected = [row for row in rows if row.get("runtime", "other") == runtime]
        if not selected:
            continue
        if lines:
            lines.append("")
        lines.extend([f"### {RUNTIME_TITLES[runtime]}", ""])
        lines.extend(f"- {row['label']} · `{row['session']}`" for row in selected)
    return lines


def main() -> int:
    date, as_json = parse_args(sys.argv)
    if not CLAUDE_PROJ.is_dir() and not CODEX_ROOT.is_dir():
        print("session-map: Claude and Codex transcript roots not found", file=sys.stderr)
        return 2
    rows = build(date)
    if as_json:
        print(json.dumps({"date": date, "sessions": rows}, ensure_ascii=False, indent=2))
    elif not rows:
        print(f"# no sessions on {date}")
    else:
        print("\n".join(render_markdown(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
