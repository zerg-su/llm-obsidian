#!/usr/bin/env python3
"""Deterministic date-page mutations routed through vault-write.py."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from daily_contract import (
    DAILY_TASK_SECTIONS,
    DailyContractError,
    h2_section_span,
    parse_daily_task,
    replace_h2,
    task_done_line,
    task_input_open_line,
    update_frontmatter,
    validate_date,
)


ROOT = Path(os.environ.get("LLM_OBSIDIAN_ROOT") or Path(__file__).resolve().parents[1]).resolve()
WRITER = ROOT / "scripts" / "vault-write.py"
TEMPLATE = ROOT / "_templates" / "daily.md"
SECTIONS = {"plans": "## Планы", "reminders": "## Напоминания", "notes": "## Заметки"}
SESSION_LINE_RX = re.compile(r"^-\s+.+?\s+·\s+`[A-Za-z0-9][A-Za-z0-9._:-]{7,127}`\s*$")
SESSION_HEADING_RX = re.compile(r"^###\s+(?:Claude|Codex|Other)\s*$")


def session_id() -> str:
    helper = ROOT / "scripts" / "current-session-id.sh"
    result = subprocess.run([str(helper)], cwd=ROOT, text=True, capture_output=True) if helper.is_file() else None
    return result.stdout.strip() if result and result.returncode == 0 and result.stdout.strip() else "unknown"


def rel_path(date: str) -> str:
    validate_date(date)
    return f"wiki/daily/{date[:4]}/{date[5:7]}/{date}.md"


def skeleton(date: str, current_session: str) -> str:
    if not TEMPLATE.is_file():
        raise DailyContractError("canonical _templates/daily.md is missing")
    text = TEMPLATE.read_text(encoding="utf-8").replace("<DATE>", date)
    if current_session != "unknown":
        text = text.replace("sessions: []", f"sessions:\n  - {current_session}")
    return text


def load_page(date: str, current_session: str) -> tuple[str, str | None]:
    rel = rel_path(date)
    path = ROOT / rel
    current = path.read_text(encoding="utf-8") if path.is_file() else None
    return rel, current if current is not None else skeleton(date, current_session)


def section_lines(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == heading) + 1
    except StopIteration as exc:
        raise DailyContractError(f"required section missing: {heading}") from exc
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return [line for line in lines[start:end] if line.strip()]


def page_spec(rel: str, old: str | None, new: str) -> dict:
    if old is None:
        return {"op": "create", "path": rel, "content": new}
    digest = hashlib.sha256(old.encode("utf-8")).hexdigest()
    return {"op": "update", "path": rel, "content": new, "expected_sha256": digest}


def call_writer(specs: list[dict], current_session: str, *, dry_run: bool) -> dict:
    payload = {"schema_version": 1, "actor": "journal", "session": current_session, "pages": specs}
    command = [sys.executable, str(WRITER), "--output", "json"]
    if dry_run:
        command.append("--dry-run")
    result = subprocess.run(command, cwd=ROOT, input=json.dumps(payload, ensure_ascii=False), text=True, capture_output=True)
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DailyContractError(f"vault writer returned invalid JSON: {(result.stderr or result.stdout).strip()}") from exc
    if result.returncode:
        raise DailyContractError(response.get("error", {}).get("message") or "vault writer failed")
    return response


def mutate(date: str, transform, *, dry_run: bool) -> dict:
    current_session = session_id()
    rel = rel_path(date)
    path = ROOT / rel
    old = path.read_text(encoding="utf-8") if path.is_file() else None
    base = old if old is not None else skeleton(date, current_session)
    new = transform(base)
    new = update_frontmatter(new, datetime.now().astimezone().strftime("%Y-%m-%d"), current_session)
    return call_writer([page_spec(rel, old, new)], current_session, dry_run=dry_run)


def ensure(date: str, *, dry_run: bool) -> dict:
    validate_date(date)
    path = ROOT / rel_path(date)
    if path.is_file():
        return {"schema_version": 1, "status": "existing", "written_paths": []}
    return mutate(date, lambda text: text, dry_run=dry_run)


def append(date: str, section_name: str, text: str, *, dry_run: bool) -> dict:
    return mutate(
        date,
        lambda page: append_to_page(date, page, section_name, text),
        dry_run=dry_run,
    )


def append_to_page(date: str, page: str, section_name: str, text: str) -> str:
    heading = SECTIONS[section_name]
    if section_name != "notes" and ("\n" in text or not text.strip()):
        raise DailyContractError("plans/reminders require one non-empty line")
    if not text.strip():
        raise DailyContractError("notes text must not be empty")
    if section_name == "notes" and any(line.startswith(("## ", "---")) for line in text.splitlines()):
        raise DailyContractError("notes must not introduce headings or frontmatter delimiters")

    current = section_lines(page, heading)
    if section_name in DAILY_TASK_SECTIONS:
        candidate = task_input_open_line(date, section_name, text)
        candidate_task = parse_daily_task(
            candidate,
            date=date,
            section=section_name,
            line_no=1,
        )
        if candidate_task is None:
            raise DailyContractError("cannot normalize task input")
        identity = candidate_task.normalized_text
        parsed = [
            parse_daily_task(line, date=date, section=section_name, line_no=index + 1)
            for index, line in enumerate(current)
        ]
        if any(
            item is not None
            and item.normalized_text == identity
            for item in parsed
        ):
            return page
        current.append(candidate)
    else:
        current.extend(text.strip().splitlines())
    return replace_h2(page, heading, current)


def check(date: str, match: str, *, section_name: str = "plans", dry_run: bool) -> dict:
    return mutate(
        date,
        lambda page: check_in_page(date, page, match, section_name=section_name),
        dry_run=dry_run,
    )


def check_in_page(
    date: str, page: str, match: str, *, section_name: str = "plans"
) -> str:
    needle = match.strip().casefold()
    if not needle:
        raise DailyContractError("check match must not be empty")
    if section_name not in DAILY_TASK_SECTIONS:
        raise DailyContractError("check section must be plans or reminders")

    lines = page.splitlines()
    start, end = h2_section_span(page, DAILY_TASK_SECTIONS[section_name])
    matches = []
    for index in range(start, end):
        task = parse_daily_task(lines[index], date=date, section=section_name, line_no=index + 1)
        if task is not None and task.status in {"open", "in_progress"} and needle in task.raw.casefold():
            matches.append((index, task))
    if not matches:
        raise DailyContractError(f"no unfinished {section_name} item matches")
    if len(matches) > 1:
        choices = "; ".join(task.raw for _, task in matches[:5])
        raise DailyContractError(f"multiple unfinished {section_name} items match: {choices}")
    index, task = matches[0]
    lines[index] = task_done_line(task, datetime.now().astimezone().strftime("%Y-%m-%d"))
    return "\n".join(lines).rstrip() + "\n"


def batch(date: str, operations_json: str, *, dry_run: bool) -> dict:
    """Apply a bounded ordered operation list in one optimistic writer transaction."""
    try:
        operations = json.loads(operations_json)
    except json.JSONDecodeError as exc:
        raise DailyContractError("batch operations must be valid JSON") from exc
    if not isinstance(operations, list) or not 1 <= len(operations) <= 20:
        raise DailyContractError("batch operations must contain between 1 and 20 items")

    def transform(page: str) -> str:
        current = page
        for index, operation in enumerate(operations, start=1):
            if not isinstance(operation, dict):
                raise DailyContractError(f"batch operation {index} must be an object")
            kind = operation.get("op")
            section = operation.get("section")
            if kind == "append" and section in SECTIONS and set(operation) == {"op", "section", "text"}:
                text = operation.get("text")
                if not isinstance(text, str):
                    raise DailyContractError(f"batch operation {index} text must be a string")
                current = append_to_page(date, current, str(section), text)
            elif (
                kind == "check"
                and section in DAILY_TASK_SECTIONS
                and set(operation) == {"op", "section", "match"}
            ):
                match = operation.get("match")
                if not isinstance(match, str):
                    raise DailyContractError(f"batch operation {index} match must be a string")
                current = check_in_page(date, current, match, section_name=str(section))
            else:
                raise DailyContractError(f"batch operation {index} has an invalid contract")
        return current

    return mutate(date, transform, dry_run=dry_run)


def sessions(date: str, *, dry_run: bool) -> dict:
    helper = ROOT / "scripts" / "session-map.py"
    result = subprocess.run([sys.executable, str(helper), date], cwd=ROOT, text=True, capture_output=True)
    if result.returncode:
        raise DailyContractError(result.stderr.strip() or "session-map failed")
    lines = [line.rstrip() for line in result.stdout.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines == [f"# no sessions on {date}"]:
        lines = []
    meaningful = [line.strip() for line in lines if line.strip()]
    if any(SESSION_LINE_RX.fullmatch(line) is None and SESSION_HEADING_RX.fullmatch(line) is None for line in meaningful):
        raise DailyContractError("session-map returned an invalid line")
    return mutate(date, lambda page: replace_h2(page, "## Сессии", lines), dry_run=dry_run)


def carryover(source: str, target: str, *, dry_run: bool) -> dict:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "agenda.py"),
        "collect",
        "--source",
        source,
        "--date",
        target,
        "--section",
        "plans",
    ]
    if dry_run:
        command.append("--dry-run")
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DailyContractError(f"agenda returned invalid JSON: {(result.stderr or result.stdout).strip()}") from exc
    if result.returncode:
        raise DailyContractError(result.stderr.strip() or "agenda carryover failed")
    return response


def today(date: str, *, dry_run: bool) -> dict:
    ensured = ensure(date, dry_run=dry_run)
    command = [sys.executable, str(ROOT / "scripts" / "agenda.py"), "scan", "--date", date, "--json"]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if result.returncode:
        raise DailyContractError(result.stderr.strip() or "agenda scan failed")
    try:
        scan = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DailyContractError("agenda scan returned invalid JSON") from exc
    return {
        "schema_version": 1,
        "status": "ok",
        "date": date,
        "ensure": ensured,
        "unfinished_count": scan["count"],
        "warnings": scan["warnings"],
        "next": f"python3 scripts/agenda.py collect --date {date}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("ensure", "sessions", "today"):
        item = sub.add_parser(name)
        item.add_argument("--date", required=True)
        item.add_argument("--dry-run", action="store_true")
    add = sub.add_parser("append")
    add.add_argument("--date", required=True)
    add.add_argument("--section", choices=sorted(SECTIONS), required=True)
    add.add_argument("--text", required=True)
    add.add_argument("--dry-run", action="store_true")
    done = sub.add_parser("check")
    done.add_argument("--date", required=True)
    done.add_argument("--match", required=True)
    done.add_argument("--section", choices=sorted(DAILY_TASK_SECTIONS), default="plans")
    done.add_argument("--dry-run", action="store_true")
    grouped = sub.add_parser("batch")
    grouped.add_argument("--date", required=True)
    grouped.add_argument("--operations-json", required=True)
    grouped.add_argument("--dry-run", action="store_true")
    carry = sub.add_parser("carryover")
    carry.add_argument("--source", required=True)
    carry.add_argument("--target", required=True)
    carry.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "ensure":
            response = ensure(args.date, dry_run=args.dry_run)
        elif args.command == "today":
            response = today(args.date, dry_run=args.dry_run)
        elif args.command == "append":
            response = append(args.date, args.section, args.text, dry_run=args.dry_run)
        elif args.command == "check":
            response = check(args.date, args.match, section_name=args.section, dry_run=args.dry_run)
        elif args.command == "batch":
            response = batch(args.date, args.operations_json, dry_run=args.dry_run)
        elif args.command == "sessions":
            response = sessions(args.date, dry_run=args.dry_run)
        else:
            response = carryover(args.source, args.target, dry_run=args.dry_run)
        print(json.dumps(response, ensure_ascii=False))
        return 0
    except (DailyContractError, OSError) as exc:
        print(f"journal-write: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
