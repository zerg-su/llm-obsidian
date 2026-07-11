#!/usr/bin/env python3
"""Deterministic, idempotent rollup of hashed operation-log entries."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pipeline_events import emit_event
from vault_schema import LOG_ENTRY_RX, iter_wikilinks, parse_frontmatter, split_frontmatter


ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "wiki" / "log.md"
FOLDS = ROOT / "wiki" / "folds"
WRITER = ROOT / "scripts" / "vault-write.py"
ENTRY_RX = LOG_ENTRY_RX


@dataclass(frozen=True)
class LogEntry:
    id: str
    date: str
    operation: str
    title: str
    text: str
    summary: str
    links: list[str]


def canonical_entry(text: str) -> str:
    return text.strip().replace("\r\n", "\n") + "\n"


def summary_of(body: str) -> str:
    for line in body.splitlines():
        clean = re.sub(r"^\s*(?:[-*+] |\d+[.)]\s+|>\s*)", "", line).strip()
        if clean:
            clean = re.sub(r"\s+", " ", clean).replace("|", r"\|")
            return clean[:217].rstrip() + ("…" if len(clean) > 217 else "")
    return "—"


def parse_entries(text: str) -> list[LogEntry]:
    matches = list(ENTRY_RX.finditer(text))
    entries: list[LogEntry] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        raw = canonical_entry(text[match.start() : end])
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        body = text[match.end() : end].strip()
        entries.append(
            LogEntry(
                id=digest,
                date=match.group(1).strip()[:10],
                operation=match.group(2).strip(),
                title=match.group(3).strip().replace("|", r"\|"),
                text=raw,
                summary=summary_of(body),
                links=sorted(set(iter_wikilinks(body))),
            )
        )
    return entries


def processed_ids() -> set[str]:
    processed: set[str] = set()
    if not FOLDS.is_dir():
        return processed
    for path in sorted(FOLDS.glob("*.md")):
        block = split_frontmatter(path.read_text(encoding="utf-8"))
        if block is None:
            continue
        try:
            values = parse_frontmatter(block).get("entry_ids", [])
        except ValueError as exc:
            print(f"fold-log: skip malformed {path}: {exc}", file=sys.stderr)
            continue
        if isinstance(values, list):
            processed.update(value for value in values if isinstance(value, str))
    return processed


def fold_status(k: int = 6) -> dict:
    entries = parse_entries(LOG.read_text(encoding="utf-8")) if LOG.is_file() else []
    eligible = [entry for entry in entries if entry.operation.casefold() != "fold"]
    processed = processed_ids()
    unprocessed_newest = [entry for entry in eligible if entry.id not in processed]
    size = 2**k
    oldest = list(reversed(unprocessed_newest))
    selected = oldest[:size] if len(oldest) >= size else []
    return {
        "k": k,
        "batch_size": size,
        "total_entries": len(entries),
        "eligible_entries": len(eligible),
        "processed_entries": len({entry.id for entry in eligible} & processed),
        "unprocessed_entries": len(unprocessed_newest),
        "excluded_fold_entries": len(entries) - len(eligible),
        "ready": bool(selected),
        "selected": selected,
    }


def fold_id(k: int, entries: list[LogEntry]) -> str:
    return f"fold-k{k}-{entries[0].id[:12]}-{entries[-1].id[:12]}-n{len(entries)}"


def current_session() -> str:
    helper = ROOT / "scripts" / "current-session-id.sh"
    if helper.is_file():
        result = subprocess.run([str(helper)], cwd=ROOT, text=True, capture_output=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "unknown"


def render_page(k: int, entries: list[LogEntry], session: str) -> tuple[str, str]:
    identifier = fold_id(k, entries)
    created = entries[-1].date
    operations = Counter(entry.operation for entry in entries)
    operation_summary = ", ".join(f"{name}={count}" for name, count in sorted(operations.items()))
    lines = [
        "---",
        "type: fold",
        f'title: "Log Fold {entries[0].date} to {entries[-1].date}"',
        f"fold_id: {identifier}",
        f"batch_exponent: {k}",
        f"created: {created}",
        f"updated: {created}",
        "status: stable",
        "tags: [fold, log-rollup]",
        "sessions:",
        f"  - id: {session}",
        f"    date: {created}",
        "entry_ids:",
    ]
    lines.extend(f"  - {entry.id}" for entry in entries)
    lines.extend(
        [
            "---",
            "",
            f"# Log Fold {entries[0].date} to {entries[-1].date}",
            "",
            f"Deterministic rollup of {len(entries)} oldest unprocessed [[log]] entries. Operations: {operation_summary}.",
            "",
            "## Child Entries",
            "",
            "| Entry ID | Date | Operation | Title | Extract | Links |",
            "|---|---|---|---|---|---|",
        ]
    )
    for entry in entries:
        links = ", ".join(f"[[{link}]]" for link in entry.links) or "—"
        lines.append(
            f"| `{entry.id[:12]}` | {entry.date} | {entry.operation} | "
            f"{entry.title} | {entry.summary} | {links} |"
        )
    lines.extend(
        [
            "",
            "## Integrity",
            "",
            f"- Boundary IDs: `{entries[0].id}` → `{entries[-1].id}`.",
            f"- Batch: `2^{k} = {len(entries)}` eligible entries.",
            "- Fold log entries are excluded from future input batches.",
            "- Child entries remain unchanged in [[log]].",
            "",
        ]
    )
    return identifier, "\n".join(lines)


def status_json(status: dict) -> dict:
    return {
        key: value
        for key, value in status.items()
        if key != "selected"
    } | {"selected_ids": [entry.id for entry in status["selected"]]}


def commit_fold(k: int, identifier: str, content: str, entries: list[LogEntry], session: str) -> int:
    payload = {
        "actor": "wiki-fold",
        "session": session,
        "pages": [
            {
                "op": "create",
                "path": f"wiki/folds/{identifier}.md",
                "content": content,
            }
        ],
        "log_entry": (
            f"## [{entries[-1].date}] fold | {identifier}\n\n"
            f"[[{identifier}]] rolls up {len(entries)} oldest unprocessed entries "
            f"from {entries[0].date} to {entries[-1].date}; boundary IDs "
            f"`{entries[0].id[:12]}` → `{entries[-1].id[:12]}`."
        ),
    }
    result = subprocess.run(
        [sys.executable, str(WRITER)],
        cwd=ROOT,
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode == 0:
        emit_event(
            "fold",
            actor="wiki-fold",
            session=session,
            paths=[f"wiki/folds/{identifier}.md"],
            counts={"entries": len(entries), "batch_exponent": k},
            root=ROOT,
        )
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=("fold", "status"), default="fold")
    parser.add_argument("--k", type=int, default=6, help="batch exponent; default 6 = 64 entries")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.k <= 10:
        parser.error("--k must be between 1 and 10")
    status = fold_status(args.k)
    if args.command == "status":
        print(json.dumps(status_json(status), ensure_ascii=False, indent=2))
        return 0
    if not status["ready"]:
        message = (
            f"fold-log: {status['unprocessed_entries']} unprocessed eligible entries; "
            f"need {status['batch_size']} (fold entries excluded: {status['excluded_fold_entries']})"
        )
        if args.json:
            print(json.dumps(status_json(status), ensure_ascii=False, indent=2))
        else:
            print(message)
        return 0
    session = current_session()
    identifier, content = render_page(args.k, status["selected"], session)
    if args.json:
        data = status_json(status) | {"fold_id": identifier, "path": f"wiki/folds/{identifier}.md"}
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if not args.commit:
        print(content)
        print(f"fold-log: DRY-RUN {identifier}; rerun with --commit", file=sys.stderr)
        return 0
    return commit_fold(args.k, identifier, content, status["selected"], session)


if __name__ == "__main__":
    raise SystemExit(main())
