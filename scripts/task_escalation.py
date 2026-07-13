#!/usr/bin/env python3
"""Relay a contract-approved task escalation through the coordinator surface."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from lifecycle_telemetry import elapsed_ms, emit_lifecycle_event
from task_contract import ContractError, normalize_for_runtime


CATEGORIES = {
    "blocking-review",
    "scope",
    "public-interface",
    "migration",
    "security",
    "external-effect",
    "contract-drift",
    "mechanism-failure",
    "permission",
}


def die(message: str, code: int = 2) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compact(value: str, field: str, limit: int = 2000) -> str:
    value = " ".join(value.split()).strip()
    if not value or len(value) > limit:
        die(f"{field} must contain 1..{limit} characters")
    return value


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        die(f"cannot read {path}: {exc}")
    if not isinstance(value, dict):
        die(f"{path} must contain an object")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_surface(worktree: Path, meta: dict[str, Any], key: str, fallback: str) -> str:
    value = str(meta.get(key) or "").strip()
    path = worktree / fallback
    if not value and path.is_file():
        value = path.read_text(encoding="utf-8").strip()
    if not value:
        die(f"missing {key} surface metadata")
    return value


def send(surface: str, message: str, *, clear_codex: bool = False) -> None:
    if clear_codex:
        for _ in range(40):
            subprocess.run(
                ["cmux", "send-key", "--surface", surface, "backspace"],
                text=True,
                capture_output=True,
                check=False,
            )
    sent = subprocess.run(
        ["cmux", "send", "--surface", surface, message], text=True, capture_output=True, check=False
    )
    if sent.returncode != 0:
        die((sent.stdout + sent.stderr).strip() or "cmux send failed", 3)
    time.sleep(0.2)
    entered = subprocess.run(
        ["cmux", "send-key", "--surface", surface, "Enter"],
        text=True,
        capture_output=True,
        check=False,
    )
    if entered.returncode != 0:
        die((entered.stdout + entered.stderr).strip() or "cmux send-key failed", 3)


def load_unattended(worktree: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    meta = read_json(worktree / ".task-meta.json")
    try:
        policy = normalize_for_runtime(meta, worktree)
    except ContractError as exc:
        die(str(exc))
    if policy["interaction_policy"] != "unattended":
        die("task escalation relay is only for unattended tasks")
    return meta, policy


def notify(surface: str, title: str, body: str) -> None:
    result = subprocess.run(
        ["cmux", "notify", "--surface", surface, "--title", title, "--body", body],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        die((result.stdout + result.stderr).strip() or "cmux notify failed", 3)


def raise_escalation(worktree: Path, category: str, reason: str, question: str) -> int:
    meta, _ = load_unattended(worktree)
    coordinator = read_surface(worktree, meta, "wiki_surface", ".wiki-cmux-surface")
    task_surface = read_surface(worktree, meta, "task_surface", ".task-cmux-surface")
    task_name = compact(str(meta.get("task_name") or "task"), "task_name", 200)
    marker = {
        "version": 1,
        "id": str(uuid.uuid4()),
        "status": "pending",
        "task_name": task_name,
        "category": category,
        "reason": compact(reason, "reason"),
        "question": compact(question, "question"),
        "worktree": str(worktree),
        "task_surface": task_surface,
        "raised_at": utc_now(),
    }
    marker_path = worktree / ".task-needs-attention.json"
    write_json(marker_path, marker)
    title = f"Task {task_name} needs a decision"
    body = (
        f"{category}: {marker['reason']} Question: {marker['question']} "
        f"Paused. Resolve from the coordinator with task_escalation.py resolve --worktree "
        f"{shlex.quote(str(worktree))}."
    )
    try:
        notify(coordinator, title, body)
    except SystemExit:
        marker["status"] = "delivery-failed"
        write_json(marker_path, marker)
        raise
    emit_lifecycle_event(
        worktree,
        "task-escalation",
        actor=f"raise:{category}",
        counts={"raised": 1},
    )
    print(f"escalation {marker['id']} sent to coordinator; task must remain paused")
    return 0


def resolve_escalation(worktree: Path, decision: str) -> int:
    meta, _ = load_unattended(worktree)
    marker_path = worktree / ".task-needs-attention.json"
    marker = read_json(marker_path)
    if marker.get("status") != "pending":
        die("there is no pending task escalation", 3)
    current = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "unknown"
    if current == "unknown" or current != str(meta.get("origin_session") or ""):
        die("only the originating coordinator session may resolve this escalation", 3)
    task_surface = read_surface(worktree, meta, "task_surface", ".task-cmux-surface")
    answer = compact(decision, "decision")
    send(
        task_surface,
        f"[Coordinator decision for escalation {marker.get('id')}] {answer} "
        "Continue only within this decision and the approved plan; escalate again on further drift.",
        clear_codex=str(meta.get("executor_runtime") or meta.get("runtime") or "") == "codex",
    )
    marker.update({"status": "resolved", "decision": answer, "resolved_at": utc_now()})
    write_json(marker_path, marker)
    duration = elapsed_ms(marker.get("raised_at"), marker.get("resolved_at"))
    emit_lifecycle_event(
        worktree,
        "task-escalation",
        actor=f"resolve:{marker.get('category') or 'unknown'}",
        counts={"resolved": 1, **({"duration_ms": duration} if duration is not None else {})},
    )
    print(f"decision relayed to task surface {task_surface}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    raised = sub.add_parser("raise")
    raised.add_argument("--worktree", default=".")
    raised.add_argument("--category", choices=sorted(CATEGORIES), required=True)
    raised.add_argument("--reason", required=True)
    raised.add_argument("--question", required=True)
    resolved = sub.add_parser("resolve")
    resolved.add_argument("--worktree", default=".")
    resolved.add_argument("--decision", required=True)
    args = parser.parse_args()
    worktree = Path(args.worktree).expanduser().resolve()
    if args.command == "raise":
        return raise_escalation(worktree, args.category, args.reason, args.question)
    return resolve_escalation(worktree, args.decision)


if __name__ == "__main__":
    raise SystemExit(main())
