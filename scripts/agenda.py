#!/usr/bin/env python3
"""Deterministically scan, collect, and report unfinished daily agenda items."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from daily_contract import (
    DAILY_TASK_SECTIONS,
    DailyContractError,
    DailyTask,
    agenda_block_id,
    h2_section_span,
    parse_daily_task,
    section_has_indented_children,
    task_migrated_line,
    task_open_line,
    update_frontmatter,
    validate_date,
)


ROOT = Path(os.environ.get("LLM_OBSIDIAN_ROOT") or Path(__file__).resolve().parents[1]).resolve()
WRITER = ROOT / "scripts" / "vault-write.py"
TEMPLATE = ROOT / "_templates" / "daily.md"
DAILY_NAME_RX = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})\.md$")
MONTH_RX = re.compile(r"\d{4}-\d{2}")
ACTIVE_STATUSES = {"open", "in_progress"}
TARGET_SECTION_SUCCESSORS = {
    "plans": (
        "## Напоминания",
        "## Инциденты",
        "## Сделано",
        "## Сессии",
        "## Заметки",
    ),
    "reminders": (
        "## Инциденты",
        "## Сделано",
        "## Сессии",
        "## Заметки",
    ),
}


def current_session_id() -> str:
    helper = ROOT / "scripts" / "current-session-id.sh"
    result = subprocess.run([str(helper)], cwd=ROOT, text=True, capture_output=True) if helper.is_file() else None
    return result.stdout.strip() if result and result.returncode == 0 and result.stdout.strip() else "unknown"


def daily_rel_path(date: str) -> str:
    validate_date(date)
    return f"wiki/daily/{date[:4]}/{date[5:7]}/{date}.md"


def month_rel_path(month: str) -> str:
    validate_month(month)
    return f"wiki/daily/{month[:4]}/{month[5:7]}/{month} — Незавершённое.md"


def validate_month(month: str) -> str:
    if MONTH_RX.fullmatch(month) is None:
        raise DailyContractError("month must be YYYY-MM")
    try:
        datetime.strptime(month + "-01", "%Y-%m-%d")
    except ValueError as exc:
        raise DailyContractError("month must be a real calendar month") from exc
    return month


def daily_skeleton(date: str, session: str) -> str:
    if not TEMPLATE.is_file():
        raise DailyContractError("canonical _templates/daily.md is missing")
    text = TEMPLATE.read_text(encoding="utf-8").replace("<DATE>", date)
    if session != "unknown":
        text = text.replace("sessions: []", f"sessions:\n  - {session}")
    return text


def ensure_target_task_section(text: str, section: str) -> tuple[str, bool]:
    """Add one missing canonical target H2 without rewriting other sections."""

    heading = DAILY_TASK_SECTIONS[section]
    try:
        h2_section_span(text, heading)
        return text, False
    except DailyContractError:
        pass
    lines = text.splitlines()
    insertion = len(lines)
    for successor in TARGET_SECTION_SUCCESSORS[section]:
        found = next(
            (index for index, line in enumerate(lines) if line.strip() == successor),
            None,
        )
        if found is not None:
            insertion = found
            break
    block = [heading, ""]
    if insertion > 0 and lines[insertion - 1].strip():
        block.insert(0, "")
    lines[insertion:insertion] = block
    return "\n".join(lines).rstrip() + "\n", True


def page_spec(rel: str, old: str | None, new: str) -> dict[str, str]:
    if old is None:
        return {"op": "create", "path": rel, "content": new}
    return {
        "op": "update",
        "path": rel,
        "content": new,
        "expected_sha256": hashlib.sha256(old.encode("utf-8")).hexdigest(),
    }


def call_writer(specs: list[dict[str, str]], session: str, *, dry_run: bool) -> dict[str, Any]:
    payload = {"schema_version": 1, "actor": "agenda", "session": session, "pages": specs}
    command = [sys.executable, str(WRITER), "--output", "json"]
    if dry_run:
        command.append("--dry-run")
    result = subprocess.run(
        command,
        cwd=ROOT,
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
    )
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        detail = (result.stderr or result.stdout).strip()
        raise DailyContractError(f"vault writer returned invalid JSON: {detail}") from exc
    if result.returncode:
        message = response.get("error", {}).get("message") if isinstance(response, dict) else None
        raise DailyContractError(message or "vault writer failed")
    return response


def iter_daily_pages(target_date: str, *, since: str | None, source: str | None) -> Iterable[tuple[str, Path]]:
    validate_date(target_date)
    if since:
        validate_date(since, "since")
        if since >= target_date:
            raise DailyContractError("since must be earlier than date")
    if source:
        validate_date(source, "source")
        if source >= target_date:
            raise DailyContractError("source must be earlier than date")
    daily_root = ROOT / "wiki" / "daily"
    if not daily_root.is_dir():
        return
    for path in sorted(daily_root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/*.md")):
        match = DAILY_NAME_RX.fullmatch(path.name)
        if match is None:
            continue
        date = match.group("date")
        try:
            validate_date(date)
        except DailyContractError:
            continue
        if date >= target_date or (since and date < since) or (source and date != source):
            continue
        yield date, path


def parse_page_tasks(date: str, text: str, sections: set[str]) -> tuple[list[DailyTask], list[dict[str, Any]]]:
    lines = text.splitlines()
    tasks: list[DailyTask] = []
    warnings: list[dict[str, Any]] = []
    for section in sorted(sections):
        heading = DAILY_TASK_SECTIONS[section]
        try:
            start, end = h2_section_span(text, heading)
        except DailyContractError:
            warnings.append(
                {
                    "code": "section_missing",
                    "date": date,
                    "section": section,
                }
            )
            continue
        for index in range(start, end):
            task = parse_daily_task(lines[index], date=date, section=section, line_no=index + 1)
            if task is None or task.status not in ACTIVE_STATUSES:
                continue
            if task.indent or section_has_indented_children(lines[:end], index, task.indent):
                warnings.append(
                    {
                        "code": "nested_subtree_skipped",
                        "date": date,
                        "section": section,
                        "line": index + 1,
                        "identity": task.identity,
                    }
                )
                continue
            tasks.append(task)
    return tasks, warnings


def scan_state(
    target_date: str,
    *,
    since: str | None = None,
    source: str | None = None,
    sections: set[str] | None = None,
) -> dict[str, Any]:
    if since and source:
        raise DailyContractError("since and source are mutually exclusive")
    if source and not (ROOT / daily_rel_path(source)).is_file():
        raise DailyContractError(f"source date page does not exist: {source}")
    sections = sections or set(DAILY_TASK_SECTIONS)
    unknown = sections - set(DAILY_TASK_SECTIONS)
    if unknown:
        raise DailyContractError(f"unknown sections: {', '.join(sorted(unknown))}")
    occurrences: list[DailyTask] = []
    warnings: list[dict[str, Any]] = []
    page_texts: dict[str, str] = {}
    for date, path in iter_daily_pages(target_date, since=since, source=source):
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(ROOT))
        page_texts[rel] = text
        found, page_warnings = parse_page_tasks(date, text, sections)
        occurrences.extend(found)
        warnings.extend(page_warnings)

    grouped: dict[str, list[DailyTask]] = defaultdict(list)
    for task in occurrences:
        grouped[task.identity].append(task)
    items: list[dict[str, Any]] = []
    for identity, group in sorted(grouped.items(), key=lambda item: (item[1][0].date, item[1][0].line_no)):
        group.sort(key=lambda task: (task.date, task.line_no))
        descriptions = {task.normalized_text for task in group}
        sections_in_group = {task.section for task in group}
        if len(descriptions) != 1 or len(sections_in_group) != 1:
            warnings.append({"code": "identity_conflict", "identity": identity, "occurrences": len(group)})
            continue
        legacy = identity.startswith("legacy:")
        if legacy and len(group) > 1:
            warnings.append(
                {
                    "code": "legacy_identity_merged",
                    "identity": identity,
                    "occurrences": len(group),
                    "ambiguous": True,
                }
            )
        exemplar = group[0]
        block_id = exemplar.block_id or agenda_block_id(exemplar.date, exemplar.section, exemplar.description)
        items.append(
            {
                "identity": identity,
                "block_id": block_id,
                "section": exemplar.section,
                "text": exemplar.description,
                "legacy": legacy,
                "ambiguous": legacy and len(group) > 1,
                "occurrences": group,
            }
        )
    return {
        "date": target_date,
        "since": since,
        "source": source,
        "sections": sorted(sections),
        "items": items,
        "warnings": warnings,
        "page_texts": page_texts,
    }


def public_scan(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "date": state["date"],
        "since": state["since"],
        "source": state["source"],
        "sections": state["sections"],
        "count": len(state["items"]),
        "items": [
            {
                "identity": item["identity"],
                "block_id": item["block_id"],
                "section": item["section"],
                "text": item["text"],
                "legacy": item["legacy"],
                "ambiguous": item["ambiguous"],
                "sources": [
                    {"date": task.date, "line": task.line_no, "status": task.status}
                    for task in item["occurrences"]
                ],
            }
            for item in state["items"]
        ],
        "warnings": state["warnings"],
    }


def month_counts(month: str, replacements: dict[str, str] | None = None) -> dict[str, int]:
    replacements = replacements or {}
    counts = {"open": 0, "in_progress": 0, "migrated": 0, "done": 0, "cancelled": 0}
    folder = ROOT / "wiki" / "daily" / month[:4] / month[5:7]
    paths = set(folder.glob("*.md")) if folder.is_dir() else set()
    prefix = f"wiki/daily/{month[:4]}/{month[5:7]}/"
    paths.update(ROOT / rel for rel in replacements if rel.startswith(prefix))
    for path in sorted(paths):
        match = DAILY_NAME_RX.fullmatch(path.name)
        if match is None:
            continue
        date = match.group("date")
        rel = str(path.relative_to(ROOT))
        text = replacements[rel] if rel in replacements else path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for section, heading in DAILY_TASK_SECTIONS.items():
            try:
                start, end = h2_section_span(text, heading)
            except DailyContractError:
                continue
            for index in range(start, end):
                task = parse_daily_task(lines[index], date=date, section=section, line_no=index + 1)
                if task is not None:
                    counts[task.status] += 1
    return counts


def existing_sessions(text: str | None) -> list[str]:
    if not text:
        return []
    match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.S)
    if match is None:
        return []
    lines = match.group(1).splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == "sessions:"), None)
    if start is None:
        return []
    values: list[str] = []
    for line in lines[start + 1 :]:
        found = re.match(r"^\s+-\s+(\S+)\s*$", line)
        if found:
            values.append(found.group(1))
            continue
        if line and not line[0].isspace():
            break
    return values


def existing_frontmatter_scalar(text: str | None, key: str) -> str | None:
    if not text:
        return None
    match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.S)
    if match is None:
        return None
    found = re.search(rf"(?m)^{re.escape(key)}:\s*['\"]?([^'\"\n]+)['\"]?\s*$", match.group(1))
    return found.group(1).strip() if found else None


def render_month_report(month: str, counts: dict[str, int], session: str, old: str | None) -> str:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    created = existing_frontmatter_scalar(old, "created") or today
    sessions = existing_sessions(old)
    if session != "unknown" and session not in sessions:
        sessions.append(session)
    session_yaml = "sessions: []" if not sessions else "sessions:\n" + "\n".join(f"  - {value}" for value in sessions)
    folder = f"wiki/daily/{month[:4]}/{month[5:7]}"
    return (
        "---\n"
        "type: meta\n"
        f'title: "{month} — Незавершённое"\n'
        f"created: {created}\n"
        f"updated: {today}\n"
        "tags:\n  - meta\n  - agenda\n"
        "status: evergreen\n"
        f"{session_yaml}\n"
        "---\n\n"
        f"# {month} — незавершённое\n\n"
        "> [!info] Автоматическая сводка\n"
        "> Пункты остаются в исходных daily-страницах. Этот файл хранит только счётчики и живой запрос Tasks.\n\n"
        "## Состояние\n\n"
        f"- Открыто: {counts['open']}\n"
        f"- В работе: {counts['in_progress']}\n"
        f"- Перенесено: {counts['migrated']}\n"
        f"- Выполнено: {counts['done']}\n"
        f"- Отменено: {counts['cancelled']}\n\n"
        "## Живой список\n\n"
        "```tasks\n"
        f"path includes {folder}\n"
        "not done\n"
        "tags do not include #agenda/migrated\n"
        "group by heading\n"
        "group by path\n"
        "```\n"
    )


def report(month: str, *, dry_run: bool) -> dict[str, Any]:
    validate_month(month)
    session = current_session_id()
    rel = month_rel_path(month)
    path = ROOT / rel
    old = path.read_text(encoding="utf-8") if path.is_file() else None
    new = render_month_report(month, month_counts(month), session, old)
    if old == new:
        return {
            "schema_version": 1,
            "status": "existing",
            "written_paths": [],
            "month": month,
            "report": rel,
        }
    response = call_writer([page_spec(rel, old, new)], session, dry_run=dry_run)
    return {**response, "month": month, "report": rel}


def collect(
    target_date: str,
    *,
    since: str | None = None,
    source: str | None = None,
    sections: set[str] | None = None,
    dry_run: bool,
) -> dict[str, Any]:
    state = scan_state(target_date, since=since, source=source, sections=sections)
    session = current_session_id()
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    replacements: dict[str, str] = {}

    target_rel = daily_rel_path(target_date)
    target_path = ROOT / target_rel
    target_old = target_path.read_text(encoding="utf-8") if target_path.is_file() else None
    target_base = target_old if target_old is not None else daily_skeleton(target_date, session)
    target_working = target_base
    created_target_sections: list[str] = []
    needed_sections = {item["section"] for item in state["items"]}
    for section in DAILY_TASK_SECTIONS:
        if section not in needed_sections:
            continue
        target_working, created = ensure_target_task_section(target_working, section)
        if created:
            created_target_sections.append(section)
    target_lines = target_working.splitlines()
    target_tasks: list[DailyTask] = []
    for section, heading in DAILY_TASK_SECTIONS.items():
        try:
            start, end = h2_section_span(target_working, heading)
        except DailyContractError:
            continue
        for index in range(start, end):
            task = parse_daily_task(target_lines[index], date=target_date, section=section, line_no=index + 1)
            if task is not None:
                target_tasks.append(task)

    source_lines: dict[str, list[str]] = {
        rel: text.splitlines() for rel, text in state["page_texts"].items()
    }
    changed_sources: set[str] = set()
    additions: dict[str, list[str]] = defaultdict(list)
    applied = 0
    warnings = list(state["warnings"])
    for item in state["items"]:
        block_id = item["block_id"]
        same_id = [task for task in target_tasks if task.block_id == block_id]
        if len(same_id) > 1:
            warnings.append({"code": "duplicate_target_identity", "identity": item["identity"]})
            continue
        if same_id and (
            same_id[0].section != item["section"]
            or same_id[0].normalized_text != item["occurrences"][0].normalized_text
        ):
            warnings.append({"code": "target_identity_conflict", "identity": item["identity"]})
            continue
        if same_id and same_id[0].status not in ACTIVE_STATUSES:
            warnings.append({"code": "target_identity_terminal", "identity": item["identity"]})
            continue

        target_present = bool(same_id)
        if not target_present and item["legacy"]:
            legacy_matches = [
                task
                for task in target_tasks
                if task.section == item["section"]
                and task.block_id is None
                and task.normalized_text == item["occurrences"][0].normalized_text
                and task.status in ACTIVE_STATUSES
            ]
            if len(legacy_matches) > 1:
                warnings.append({"code": "ambiguous_target_legacy", "identity": item["identity"]})
                continue
            if legacy_matches:
                task = legacy_matches[0]
                target_lines[task.line_no - 1] = task_open_line(task, block_id)
                target_present = True

        if not target_present:
            additions[item["section"]].append(task_open_line(item["occurrences"][-1], block_id))

        for task in item["occurrences"]:
            rel = daily_rel_path(task.date)
            source_lines[rel][task.line_no - 1] = task_migrated_line(task, target_date, block_id)
            changed_sources.add(rel)
        applied += 1

    if applied:
        warnings.extend(
            {
                "code": "target_section_created",
                "date": target_date,
                "section": section,
            }
            for section in created_target_sections
        )

    for section, lines_to_add in additions.items():
        if not lines_to_add:
            continue
        current_text = "\n".join(target_lines).rstrip() + "\n"
        start, end = h2_section_span(current_text, DAILY_TASK_SECTIONS[section])
        insertion = end
        while insertion > start and not target_lines[insertion - 1].strip():
            insertion -= 1
        if insertion == start:
            # Canonical empty sections keep one blank line on both sides of
            # their task list. Replacing the whitespace-only body also repairs
            # legacy sections that have zero or several blank lines.
            target_lines[start:end] = ["", *lines_to_add, ""]
            continue
        suffix = [] if insertion < end else [""]
        target_lines[insertion:insertion] = [*lines_to_add, *suffix]

    specs: list[dict[str, str]] = []
    for rel in sorted(changed_sources):
        lines = source_lines[rel]
        old = state["page_texts"][rel]
        new = "\n".join(lines).rstrip() + "\n"
        if new == old:
            continue
        new = update_frontmatter(new, today, session)
        replacements[rel] = new
        specs.append(page_spec(rel, old, new))

    target_new = "\n".join(target_lines).rstrip() + "\n"
    if applied and target_new != target_base:
        target_new = update_frontmatter(target_new, today, session)
        replacements[target_rel] = target_new
        specs.append(page_spec(target_rel, target_old, target_new))

    months = {target_date[:7]}
    months.update(
        task.date[:7]
        for item in state["items"]
        for task in item["occurrences"]
        if daily_rel_path(task.date) in changed_sources
    )
    report_paths: list[str] = []
    for month in sorted(months):
        report_rel = month_rel_path(month)
        report_path = ROOT / report_rel
        report_old = report_path.read_text(encoding="utf-8") if report_path.is_file() else None
        report_new = render_month_report(month, month_counts(month, replacements), session, report_old)
        report_paths.append(report_rel)
        if report_new != report_old:
            specs.append(page_spec(report_rel, report_old, report_new))

    if not specs:
        return {
            "schema_version": 1,
            "status": "nothing",
            "written_paths": [],
            "date": target_date,
            "collected": 0,
            "report": report_paths[-1],
            "reports": report_paths,
            "warnings": warnings,
        }

    response = call_writer(specs, session, dry_run=dry_run)
    return {
        **response,
        "date": target_date,
        "collected": applied,
        "report": month_rel_path(target_date[:7]),
        "reports": report_paths,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    scan_parser = sub.add_parser("scan", help="read-only scan of unfinished prior items")
    scan_parser.add_argument("--date", required=True)
    scan_parser.add_argument("--since")
    scan_parser.add_argument("--json", action="store_true")

    collect_parser = sub.add_parser("collect", help="atomically carry unfinished items forward")
    collect_parser.add_argument("--date", required=True)
    collect_parser.add_argument("--since")
    collect_parser.add_argument("--dry-run", action="store_true")
    collect_parser.add_argument("--source", help=argparse.SUPPRESS)
    collect_parser.add_argument("--section", action="append", choices=sorted(DAILY_TASK_SECTIONS), help=argparse.SUPPRESS)

    report_parser = sub.add_parser("report", help="create or refresh a monthly live Tasks report")
    report_parser.add_argument("--month", required=True)
    report_parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    try:
        if args.command == "scan":
            payload = public_scan(scan_state(args.date, since=args.since))
            if args.json:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                print(f"Unfinished before {args.date}: {payload['count']}")
                for item in payload["items"]:
                    print(f"- {item['section']}: {item['text']}")
                for warning in payload["warnings"]:
                    print(f"WARN {warning['code']}", file=sys.stderr)
        elif args.command == "collect":
            payload = collect(
                args.date,
                since=args.since,
                source=args.source,
                sections=set(args.section) if args.section else None,
                dry_run=args.dry_run,
            )
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(json.dumps(report(args.month, dry_run=args.dry_run), ensure_ascii=False))
        return 0
    except (DailyContractError, OSError) as exc:
        print(f"agenda: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
