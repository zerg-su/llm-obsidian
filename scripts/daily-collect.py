#!/usr/bin/env python3
"""Collect a compact, deterministic evidence bundle for one daily summary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from daily_contract import DailyContractError, atomic_private_json, validate_date, validate_evidence
from daily_timing import script_ms
from pipeline_events import emit_event


ROOT = Path(os.environ.get("LLM_OBSIDIAN_ROOT") or Path(__file__).resolve().parents[1]).resolve()
WIKI = ROOT / "wiki"
SESSION_DIR = WIKI / "meta" / "sessions"
LOG = WIKI / "log.md"
HOT = WIKI / "hot.md"
LOG_HEADING_RX = re.compile(r"^## \[(\d{4}-\d{2}-\d{2})(?: [^]]+)?\]\s+([^|]+)\|\s*(.+)$")


def compact(text: str, limit: int = 1800) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    block = text[4:end]
    values: dict[str, str] = {}
    for line in block.splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2).strip('"\'')
    return values, text[end + 5 :]


def first_paragraph(body: str) -> str:
    paragraphs: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        if line.startswith("#"):
            if current:
                break
            continue
        if not line.strip():
            if current:
                paragraphs.append(" ".join(current))
                break
            continue
        current.append(line.strip())
    if current and not paragraphs:
        paragraphs.append(" ".join(current))
    return paragraphs[0] if paragraphs else ""


def section(body: str, heading: str) -> str:
    lines = body.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == heading) + 1
    except StopIteration:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def collect_sessions(date: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not SESSION_DIR.is_dir():
        return rows
    for path in sorted(SESSION_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        fm, body = split_frontmatter(text)
        if date not in {fm.get("created"), fm.get("updated")}:
            continue
        title = fm.get("title") or path.stem
        parts = [first_paragraph(body), section(body, "## Outcome")]
        detail = compact("\n\n".join(part for part in parts if part))
        if detail:
            rows.append({"kind": "session", "title": title, "text": detail, "source": str(path.relative_to(ROOT))})
    return rows


def collect_log(date: str) -> list[dict[str, str]]:
    if not LOG.is_file():
        return []
    lines = LOG.read_text(encoding="utf-8").splitlines()
    rows: list[dict[str, str]] = []
    index = 0
    while index < len(lines):
        match = LOG_HEADING_RX.match(lines[index])
        if not match:
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not lines[end].startswith("## ["):
            end += 1
        if match.group(1) == date:
            title = f"{match.group(2).strip()}: {match.group(3).strip()}"
            body = compact("\n".join(lines[index + 1 : end]), 1200)
            rows.append({"kind": "log", "title": title, "text": body or title, "source": "wiki/log.md"})
        index = end
    return rows


def collect_git(date: str) -> list[dict[str, str]]:
    result = subprocess.run(
        ["git", "log", f"--since={date} 00:00:00", f"--until={date} 23:59:59", "--format=%s"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return []
    return [
        {"kind": "git", "title": subject.strip(), "text": subject.strip(), "source": "git"}
        for subject in result.stdout.splitlines()
        if subject.strip() and not subject.strip().casefold().startswith("wiki:")
    ]


def collect_hot() -> list[dict[str, str]]:
    if not HOT.is_file():
        return []
    active = section(HOT.read_text(encoding="utf-8"), "## Active Threads")
    return [
        {"kind": "hot", "title": "Active thread", "text": line[2:].strip(), "source": "wiki/hot.md"}
        for line in active.splitlines()
        if line.startswith("- ")
    ]


def collect_current(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        return []
    text = compact(path.read_text(encoding="utf-8"), 2000)
    return [{"kind": "current", "title": "Current session summary", "text": text, "source": "current-session"}] if text else []


def collect_session_map(date: str) -> list[dict[str, str]]:
    helper = ROOT / "scripts" / "session-map.py"
    if not helper.is_file():
        return []
    result = subprocess.run([sys.executable, str(helper), date, "--json"], cwd=ROOT, text=True, capture_output=True)
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, list):
        return []
    rows: list[dict[str, str]] = []
    for item in sessions:
        if not isinstance(item, dict):
            continue
        session_id = item.get("session")
        label = item.get("label")
        runtime = item.get("runtime", "other")
        if isinstance(session_id, str) and isinstance(label, str) and runtime in {"claude", "codex", "other"}:
            rows.append({"session_id": session_id, "label": label, "runtime": runtime})
    return rows


def assign_ids(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    counts: defaultdict[str, int] = defaultdict(int)
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for row in rows:
        identity = (re.sub(r"\W+", " ", row["title"]).casefold().strip(), re.sub(r"\W+", " ", row["text"]).casefold().strip())
        if identity in seen:
            continue
        seen.add(identity)
        counts[row["kind"]] += 1
        item = dict(row)
        item["id"] = f"{row['kind']}:{counts[row['kind']]:03d}"
        result.append(item)
    return result


def build(date: str, current_summary: Path | None = None) -> dict:
    validate_date(date)
    strong = collect_sessions(date) + collect_log(date) + collect_current(current_summary) + collect_git(date)
    if not strong:
        raise DailyContractError(f"found no completed-work evidence for {date}")
    items = assign_ids(strong + collect_hot())[:100]
    session_map = collect_session_map(date)
    canonical = json.dumps(
        {"date": date, "items": items, "session_map": session_map},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload = {
        "schema_version": 1,
        "date": date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bundle_id": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "items": items,
        "session_map": session_map,
    }
    return validate_evidence(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=datetime.now().astimezone().strftime("%Y-%m-%d"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--current-summary-file", type=Path)
    args = parser.parse_args()
    started = monotonic()
    try:
        payload = build(args.date, args.current_summary_file)
        if args.output:
            atomic_private_json(args.output, payload)
            print(args.output)
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        emit_event(
            "daily-collect",
            actor="daily-collect",
            counts={
                "duration_ms": script_ms(started),
                "items": len(payload["items"]),
                "sessions": len(payload["session_map"]),
            },
            root=ROOT,
        )
        return 0
    except (DailyContractError, OSError) as exc:
        status = (
            "noop"
            if isinstance(exc, DailyContractError) and str(exc).startswith("found no completed-work evidence")
            else "error"
        )
        emit_event(
            "daily-collect",
            actor="daily-collect",
            counts={"duration_ms": script_ms(started), "exit_code": 4},
            status=status,
            root=ROOT,
        )
        print(f"daily-collect: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
