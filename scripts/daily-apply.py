#!/usr/bin/env python3
"""Validate a daily summary and atomically apply both vault targets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from time import monotonic

from daily_contract import (
    DailyContractError,
    load_json,
    replace_h2,
    update_frontmatter,
    validate_evidence,
    validate_summary,
)
from daily_timing import elapsed_since_iso_ms, script_ms
from pipeline_events import emit_event


ROOT = Path(os.environ.get("LLM_OBSIDIAN_ROOT") or Path(__file__).resolve().parents[1]).resolve()
RUN_DIR = ROOT / ".vault-meta" / "daily-runs"
WRITER = ROOT / "scripts" / "vault-write.py"
TEMPLATE = ROOT / "_templates" / "daily.md"
STATUS_LOG_REL = "wiki/routines/Daily Status Log.md"
SESSION_RUNTIME_ORDER = ("claude", "codex", "other")
SESSION_RUNTIME_TITLES = {"claude": "Claude", "codex": "Codex", "other": "Other"}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def current_session_id() -> str:
    helper = ROOT / "scripts" / "current-session-id.sh"
    if not helper.is_file():
        return "unknown"
    result = subprocess.run([str(helper)], cwd=ROOT, text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "unknown"


def daily_skeleton(date: str, session_id: str) -> str:
    if not TEMPLATE.is_file():
        raise DailyContractError(f"canonical daily template missing: {TEMPLATE.relative_to(ROOT)}")
    text = TEMPLATE.read_text(encoding="utf-8").replace("<DATE>", date)
    if session_id and session_id != "unknown":
        text = text.replace("sessions: []", f"sessions:\n  - {session_id}")
    return text


def status_log_skeleton(date: str, session_id: str) -> str:
    sessions = f"sessions:\n  - {session_id}" if session_id and session_id != "unknown" else "sessions: []"
    return f'''---
type: meta
title: "Daily Status Log"
status: evergreen
created: {date}
updated: {date}
last_done: {date}
tags:
  - daily
  - routine
{sessions}
---

# Daily Status Log

## Журнал
'''


def latest_status_date(text: str, fallback: str) -> str:
    dates = re.findall(r"^### (\d{4}-\d{2}-\d{2})\s*$", text, flags=re.M)
    return max([fallback, *dates])


def upsert_status_entry(text: str, date: str, body_lines: list[str]) -> str:
    lines = text.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == "## Журнал") + 1
    except StopIteration as exc:
        raise DailyContractError("required section missing: ## Журнал") from exc
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    body = lines[start:end]
    first_entry = next((index for index, line in enumerate(body) if line.startswith("### ")), len(body))
    preamble = [line for line in body[:first_entry] if line.strip()]
    blocks: dict[str, list[str]] = {}
    index = first_entry
    while index < len(body):
        if not body[index].startswith("### "):
            if body[index].strip():
                raise DailyContractError("Daily Status Log has content outside a dated entry")
            index += 1
            continue
        match = re.fullmatch(r"### (\d{4}-\d{2}-\d{2})", body[index].strip())
        if match is None:
            raise DailyContractError("Daily Status Log contains a non-date H3 entry")
        block_end = index + 1
        while block_end < len(body) and not body[block_end].startswith("### "):
            block_end += 1
        blocks[match.group(1)] = [line for line in body[index:block_end] if line.strip()]
        index = block_end
    blocks[date] = [f"### {date}", *body_lines]
    rendered = list(preamble)
    for entry_date in sorted(blocks, reverse=True):
        if rendered:
            rendered.append("")
        rendered.extend(blocks[entry_date])
    return replace_h2(text, "## Журнал", rendered)


def page_spec(rel: str, current: str | None, content: str) -> dict:
    if current is None:
        return {"op": "create", "path": rel, "content": content}
    return {"op": "update", "path": rel, "content": content, "expected_sha256": sha256(current)}


def render_pages(evidence: dict, summary: dict, session_id: str) -> tuple[list[dict], str]:
    date = evidence["date"]
    updated = datetime.now().astimezone().strftime("%Y-%m-%d")
    daily_rel = f"wiki/daily/{date[:4]}/{date[5:7]}/{date}.md"
    daily_path = ROOT / daily_rel
    current_daily = daily_path.read_text(encoding="utf-8") if daily_path.is_file() else None
    daily = current_daily if current_daily is not None else daily_skeleton(date, session_id)
    full_lines = [f"- {item['subject']}: {item['outcome']}" for item in summary["bullets"]]
    daily = replace_h2(daily, "## Сделано", full_lines)

    label_overrides = {item["session_id"]: item["label"] for item in summary.get("session_labels", [])}
    session_lines: list[str] = []
    for runtime in SESSION_RUNTIME_ORDER:
        sessions = [item for item in evidence["session_map"] if item.get("runtime", "other") == runtime]
        if not sessions:
            continue
        if session_lines:
            session_lines.append("")
        session_lines.extend([f"### {SESSION_RUNTIME_TITLES[runtime]}", ""])
        session_lines.extend(
            f"- {label_overrides.get(item['session_id'], item['label'])} · `{item['session_id']}`"
            for item in sessions
        )
    daily = replace_h2(daily, "## Сессии", session_lines)
    daily = update_frontmatter(daily, updated, session_id)

    status_path = ROOT / STATUS_LOG_REL
    current_status = status_path.read_text(encoding="utf-8") if status_path.is_file() else None
    status = current_status if current_status is not None else status_log_skeleton(date, session_id)
    compact_lines = [f"- {item['subject']}: {item['compact']}" for item in summary["bullets"]]
    status = upsert_status_entry(status, date, [*compact_lines, f"→ [[{date}]]"])
    status = update_frontmatter(status, updated, session_id, last_done=latest_status_date(status, date))
    return [page_spec(daily_rel, current_daily, daily), page_spec(STATUS_LOG_REL, current_status, status)], "\n".join(full_lines) + "\n"


def invoke_writer(pages: list[dict], session_id: str, *, dry_run: bool) -> dict:
    payload = {
        "schema_version": 1,
        "request_id": f"daily:{pages[0]['path'].rsplit('/', 1)[-1][:-3]}",
        "actor": "daily",
        "session": session_id,
        "pages": pages,
    }
    command = [sys.executable, str(WRITER), "--output", "json"]
    if dry_run:
        command.append("--dry-run")
    result = subprocess.run(command, cwd=ROOT, input=json.dumps(payload, ensure_ascii=False), text=True, capture_output=True)
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DailyContractError(f"vault writer returned invalid JSON: {(result.stderr or result.stdout).strip()}") from exc
    if result.returncode != 0:
        message = response.get("error", {}).get("message") or result.stderr.strip()
        raise DailyContractError(f"vault writer failed: {message}")
    return response


def copy_clipboard(text: str) -> str | None:
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="daily-status.", suffix=".txt") as handle:
            handle.write(text)
            handle.flush()
            with open(handle.name, "rb") as source:
                result = subprocess.run(["pbcopy"], stdin=source, capture_output=True)
        return None if result.returncode == 0 else "pbcopy returned non-zero"
    except OSError:
        return "pbcopy unavailable"


def safe_cleanup(paths: list[Path]) -> None:
    run_dir = RUN_DIR.resolve()
    for path in paths:
        resolved = path.resolve()
        if run_dir in resolved.parents:
            resolved.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True, help="daily-summary-v1 JSON")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-clipboard", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    started = monotonic()
    evidence: dict | None = None
    try:
        evidence = validate_evidence(load_json(args.evidence))
        summary = validate_summary(load_json(args.input), evidence)
        if args.validate_only:
            emit_event(
                "daily-validate",
                actor="daily-apply",
                counts={"duration_ms": script_ms(started), "bullets": len(summary["bullets"])},
                status="noop",
                root=ROOT,
            )
            print(json.dumps({"schema_version": 1, "status": "valid", "date": evidence["date"]}))
            return 0
        session_id = current_session_id()
        pages, clipboard_text = render_pages(evidence, summary, session_id)
        response = invoke_writer(pages, session_id, dry_run=args.dry_run)
        warning = None if args.no_clipboard or args.dry_run else copy_clipboard(clipboard_text)
        if args.cleanup and not args.dry_run:
            safe_cleanup([args.evidence, args.input])
        local_ms = script_ms(started)
        written_paths = response.get("written_paths", [])
        emit_event(
            "daily-run",
            actor="daily",
            paths=written_paths,
            counts={
                "duration_ms": elapsed_since_iso_ms(evidence["generated_at"], fallback_ms=local_ms),
                "apply_ms": local_ms,
                "bullets": len(summary["bullets"]),
                "written_paths": len(written_paths),
                "dry_run": int(args.dry_run),
            },
            status="noop" if args.dry_run else "ok",
            root=ROOT,
        )
        print(json.dumps({"schema_version": 1, "status": response["status"], "date": evidence["date"], "written_paths": response["written_paths"], "clipboard_warning": warning}, ensure_ascii=False))
        return 0
    except (DailyContractError, OSError) as exc:
        local_ms = script_ms(started)
        generated_at = evidence.get("generated_at") if evidence else None
        emit_event(
            "daily-run",
            actor="daily",
            counts={
                "duration_ms": elapsed_since_iso_ms(generated_at, fallback_ms=local_ms),
                "apply_ms": local_ms,
                "exit_code": 3,
            },
            status="error",
            root=ROOT,
        )
        print(f"daily-apply: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
