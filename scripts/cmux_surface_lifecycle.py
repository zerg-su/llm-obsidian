#!/usr/bin/env python3
"""Arm agent exit and close only the exact cmux surface after process return."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from plan_lifecycle import PlanCloseError, render_plan_close
from task_contract import (
    ContractError,
    normalize,
    normalize_for_runtime,
    read_json as read_contract_json,
    validate_handoff,
)


HANDOFF_PREFIXES = (".task-", ".review-", ".wiki-")


def die(message: str, code: int = 2) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        die(f"cannot read {path}: {exc}")
    if not isinstance(data, dict):
        die(f"{path} must contain an object")
    return data


def write_marker(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def current_session_id() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "unknown"


def require_origin_session(worktree: Path, supplied: str = "") -> None:
    meta = read_json(worktree / ".task-meta.json")
    origin = str(meta.get("origin_session") or "")
    actual = current_session_id()
    if actual == "unknown" or not origin or actual != origin or (supplied and supplied != actual):
        die("only the originating coordinator session may finalize or close this task", 3)


def run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


def names(kind: str) -> tuple[str, str, str]:
    if kind == "reviewer":
        return ".review-close-armed.json", "review_surface", "reviewer_runtime"
    return ".task-close-armed.json", "task_surface", "executor_runtime"


def surface_and_runtime(worktree: Path, kind: str) -> tuple[str, str]:
    task_meta = read_json(worktree / ".task-meta.json")
    try:
        # A task plan is intentionally executed before its final /exit.  Only
        # the exact coordinator-prepared close is valid during that phase.
        policy = (
            normalize_for_runtime(task_meta, worktree)
            if kind == "task"
            else normalize(task_meta)
        )
    except ContractError as exc:
        die(str(exc), 3 if kind == "task" else 2)
    if policy["interaction_policy"] != "unattended":
        die("surface auto-close is allowed only for unattended tasks")
    if policy["surface_policy"].get("auto_close") is not True:
        die("surface auto-close is not approved by the task contract")
    _, surface_key, runtime_key = names(kind)
    source = task_meta
    if kind == "reviewer":
        source = read_json(worktree / ".review-meta.json")
    surface = str(source.get(surface_key) or "").strip()
    runtime = str(source.get(runtime_key) or source.get("runtime") or "").strip()
    if not surface or runtime not in {"claude", "codex"}:
        die(f"missing {kind} surface/runtime metadata")
    return surface, runtime


def non_handoff_dirty(worktree: Path) -> list[str]:
    result = run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=worktree)
    if result.returncode != 0:
        die(result.stderr.strip() or "git status failed")
    dirty: list[str] = []
    for line in result.stdout.splitlines():
        path = line[3:].split(" -> ")[-1]
        if path.startswith(HANDOFF_PREFIXES) or path in {
            ".obsidian/workspace.json", ".obsidian/workspace-mobile.json"
        }:
            continue
        dirty.append(path)
    return dirty


def arm(worktree: Path, kind: str) -> tuple[Path, str, str]:
    surface, runtime = surface_and_runtime(worktree, kind)
    if kind == "task":
        attention_path = worktree / ".task-needs-attention.json"
        if attention_path.is_file() and read_json(attention_path).get("status") != "resolved":
            die("task has an unresolved coordinator escalation", 3)
        complete = read_json(worktree / ".task-reap-complete.json")
        summary = worktree / ".task-summary.json"
        try:
            validate_handoff(
                read_contract_json(worktree / ".task-meta.json"),
                read_contract_json(summary),
                str(complete.get("current_session") or ""),
                verify_plan_hash=False,
            )
        except ContractError as exc:
            die(str(exc), 3)
        if complete.get("summary_sha256") != hashlib.sha256(summary.read_bytes()).hexdigest():
            die("task reap completion marker does not match the current summary", 3)
        meta_path = worktree / ".task-meta.json"
        if complete.get("meta_sha256") != hashlib.sha256(meta_path.read_bytes()).hexdigest():
            die("task reap completion marker does not match the current metadata", 3)
        result_path = Path(str(complete.get("result_path") or "")).expanduser().resolve()
        vault_root = Path(str(complete.get("vault_root") or "")).expanduser().resolve()
        try:
            result_path.relative_to(vault_root / "wiki")
        except ValueError:
            die("task reap result is outside the recorded vault wiki", 3)
        if complete.get("validated") is not True or not result_path.is_file() or result_path.suffix != ".md":
            die("task reap completion marker is not validated or result page is missing", 3)
        if complete.get("result_sha256") != hashlib.sha256(result_path.read_bytes()).hexdigest():
            die("task reap result changed after coordinator validation", 3)
        plan_path = Path(str(complete.get("plan_path") or "")).expanduser().resolve()
        expected_plan = str(complete.get("closed_plan_sha256") or "")
        if not plan_path.is_file() or hashlib.sha256(plan_path.read_bytes()).hexdigest() != expected_plan:
            die("task plan no longer matches the coordinator-prepared closed state", 3)
        dirty = non_handoff_dirty(worktree)
        if dirty:
            die("task worktree has non-handoff changes: " + ", ".join(dirty), 3)
    sentinel_name, _, _ = names(kind)
    sentinel = worktree / sentinel_name
    write_marker(
        sentinel,
        {"version": 1, "kind": kind, "surface": surface, "armed_at": utc_now()},
    )
    return sentinel, surface, runtime


def request_exit(worktree: Path, kind: str) -> int:
    if kind == "task":
        require_origin_session(worktree)
    sentinel, surface, runtime = arm(worktree, kind)
    if runtime == "codex":
        for _ in range(40):
            run(["cmux", "send-key", "--surface", surface, "backspace"])
    sent = run(["cmux", "send", "--surface", surface, "/exit"])
    if sent.returncode != 0:
        sentinel.unlink(missing_ok=True)
        die((sent.stdout + sent.stderr).strip() or "cmux send failed")
    time.sleep(0.2)
    if runtime == "codex":
        accepted = run(["cmux", "send-key", "--surface", surface, "tab"])
        if accepted.returncode != 0:
            sentinel.unlink(missing_ok=True)
            die((accepted.stdout + accepted.stderr).strip() or "cmux send-key tab failed")
        time.sleep(0.1)
    entered = run(["cmux", "send-key", "--surface", surface, "Enter"])
    if entered.returncode != 0:
        sentinel.unlink(missing_ok=True)
        die((entered.stdout + entered.stderr).strip() or "cmux send-key failed")
    print(f"armed and sent /exit to {kind} surface {surface}")
    return 0


def after_exit(worktree: Path, kind: str, surface: str) -> int:
    sentinel_name, _, _ = names(kind)
    sentinel = worktree / sentinel_name
    if not sentinel.exists():
        print(f"{kind} surface left open: close was not armed")
        return 0
    payload = read_json(sentinel)
    if payload.get("kind") != kind or payload.get("surface") != surface:
        die("close sentinel does not match the exiting surface", 3)
    closed = run(["cmux", "close-surface", "--surface", surface])
    if closed.returncode != 0:
        die((closed.stdout + closed.stderr).strip() or "cmux close-surface failed")
    sentinel.unlink(missing_ok=True)
    print(f"closed {kind} surface {surface}")
    return 0


def prepare_reap(worktree: Path, current_session: str, result_path: Path, vault_root: Path) -> int:
    require_origin_session(worktree, current_session)
    attention_path = worktree / ".task-needs-attention.json"
    if attention_path.is_file() and read_json(attention_path).get("status") != "resolved":
        die("task has an unresolved coordinator escalation", 3)
    meta_path = worktree / ".task-meta.json"
    meta = read_contract_json(meta_path)
    summary_path = worktree / ".task-summary.json"
    summary = read_contract_json(summary_path)
    try:
        validate_handoff(meta, summary, current_session)
    except ContractError as exc:
        die(str(exc))
    result = result_path.expanduser().resolve()
    vault = vault_root.expanduser().resolve()
    try:
        result.relative_to(vault / "wiki")
    except ValueError:
        die("validated reap result must be inside the selected vault wiki", 3)
    if result.suffix != ".md":
        die("prepared reap result must be a wiki Markdown page", 3)
    plan = Path(str(meta.get("plan_file") or "")).expanduser().resolve()
    try:
        plan.relative_to(vault / "wiki" / "plans")
    except ValueError:
        die("approved task plan must be inside the selected vault plans directory", 3)
    result_link = f"[[{str(summary.get('title') or '').strip()}]]"
    exec_session = str(summary.get("session") or "").strip() or None
    prepared_date = time.strftime("%Y-%m-%d")
    try:
        closed_plan = render_plan_close(
            plan.read_text(encoding="utf-8"),
            today=prepared_date,
            result_link=result_link,
            exec_session=exec_session,
            label=str(plan.relative_to(vault)),
        )
    except (OSError, PlanCloseError) as exc:
        die(str(exc), 3)
    marker = {
        "version": 1,
        "task_name": meta.get("task_name"),
        "current_session": current_session,
        "result_path": str(result),
        "vault_root": str(vault),
        "summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
        "meta_sha256": hashlib.sha256(meta_path.read_bytes()).hexdigest(),
        "plan_path": str(plan),
        "approved_plan_sha256": meta.get("approved_plan_sha256"),
        "closed_plan_sha256": hashlib.sha256(closed_plan.encode("utf-8")).hexdigest(),
        "result_link": result_link,
        "exec_session": exec_session,
        "prepared_date": prepared_date,
        "prepared_at": utc_now(),
    }
    write_marker(worktree / ".task-reap-prepared.json", marker)
    print(f"prepared contract-bound final reap: {result}")
    return 0


def complete_reap(worktree: Path, current_session: str, result_path: Path, vault_root: Path) -> int:
    require_origin_session(worktree, current_session)
    attention_path = worktree / ".task-needs-attention.json"
    if attention_path.is_file() and read_json(attention_path).get("status") != "resolved":
        die("task has an unresolved coordinator escalation", 3)
    meta_path = worktree / ".task-meta.json"
    summary_path = worktree / ".task-summary.json"
    meta = read_contract_json(meta_path)
    summary = read_contract_json(summary_path)
    prepared = read_json(worktree / ".task-reap-prepared.json")
    try:
        validate_handoff(meta, summary, current_session, verify_plan_hash=False)
    except ContractError as exc:
        die(str(exc))
    result = result_path.expanduser().resolve()
    vault = vault_root.expanduser().resolve()
    expected_fields = {
        "task_name": meta.get("task_name"),
        "current_session": current_session,
        "result_path": str(result),
        "vault_root": str(vault),
        "summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
        "meta_sha256": hashlib.sha256(meta_path.read_bytes()).hexdigest(),
        "approved_plan_sha256": meta.get("approved_plan_sha256"),
    }
    for field, expected in expected_fields.items():
        if prepared.get(field) != expected:
            die(f"reap preparation no longer matches {field}", 3)
    try:
        result.relative_to(vault / "wiki")
    except ValueError:
        die("validated reap result must be inside the selected vault wiki", 3)
    if not result.is_file() or result.suffix != ".md":
        die("validated reap result must be an existing wiki Markdown page", 3)
    plan = Path(str(meta.get("plan_file") or "")).expanduser().resolve()
    if str(plan) != prepared.get("plan_path"):
        die("reap preparation points at a different approved plan", 3)
    expected_closed = str(prepared.get("closed_plan_sha256") or "")
    if not plan.is_file() or hashlib.sha256(plan.read_bytes()).hexdigest() != expected_closed:
        die("approved plan does not match the coordinator-prepared closed state", 3)
    marker = {
        "version": 1,
        "task_name": meta.get("task_name"),
        "current_session": current_session,
        "result_path": str(result),
        "vault_root": str(vault),
        "summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
        "meta_sha256": hashlib.sha256(meta_path.read_bytes()).hexdigest(),
        "plan_path": str(plan),
        "closed_plan_sha256": expected_closed,
        "result_sha256": hashlib.sha256(result.read_bytes()).hexdigest(),
        "validated": True,
        "completed_at": utc_now(),
    }
    write_marker(worktree / ".task-reap-complete.json", marker)
    print(f"recorded validated final reap: {result}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    request = sub.add_parser("request-exit")
    request.add_argument("--worktree", default=".")
    request.add_argument("--kind", choices=["reviewer", "task"], required=True)
    after = sub.add_parser("after-exit")
    after.add_argument("--worktree", default=".")
    after.add_argument("--kind", choices=["reviewer", "task"], required=True)
    after.add_argument("--surface", required=True)
    complete = sub.add_parser("complete-reap")
    complete.add_argument("--worktree", default=".")
    complete.add_argument("--current-session", required=True)
    complete.add_argument("--result-path", required=True)
    complete.add_argument("--vault-root", default=str(Path(__file__).resolve().parents[1]))
    prepare = sub.add_parser("prepare-reap")
    prepare.add_argument("--worktree", default=".")
    prepare.add_argument("--current-session", required=True)
    prepare.add_argument("--result-path", required=True)
    prepare.add_argument("--vault-root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    worktree = Path(args.worktree).expanduser().resolve()
    if args.command == "request-exit":
        return request_exit(worktree, args.kind)
    if args.command == "after-exit":
        return after_exit(worktree, args.kind, args.surface)
    if args.command == "prepare-reap":
        return prepare_reap(worktree, args.current_session, Path(args.result_path), Path(args.vault_root))
    return complete_reap(worktree, args.current_session, Path(args.result_path), Path(args.vault_root))


if __name__ == "__main__":
    raise SystemExit(main())
