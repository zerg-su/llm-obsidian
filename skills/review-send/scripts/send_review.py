#!/usr/bin/env python3
"""Validate a reviewer handoff and callback the task executor."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn


VAULT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(VAULT_ROOT / "scripts"))
from review_contract import ReviewContractError, parse_review_json


HANDOFF_EXCLUDES = [
    ".task-prompt.md",
    ".task-summary.md",
    ".task-summary.json",
    ".task-meta.json",
    ".task-cmux-surface",
    ".task-reap-send-skill",
    ".wiki-cmux-surface",
    ".wiki-agent-runtime",
    ".wiki-reap-command",
    ".task-review.md",
    ".task-review.json",
    ".task-review-verify.md",
    ".task-review-verify.json",
    ".task-review-resolution.md",
    ".task-review-skill",
    ".task-review-send-skill",
    ".review-history.json",
    ".review-archive.json",
    ".review-archive-request.json",
    ".review-prompt.md",
    ".review-prompt-verify.md",
    ".review-meta.json",
    ".review-cmux-surface",
    ".review-baseline-status.txt",
    ".review-baseline-state.json",
    ".review-send-blocked.md",
    ".review-outbox.json",
    ".review-callback.json",
    ".review-relay.json",
    ".review-close-armed.json",
    ".task-close-armed.json",
    ".task-reap-prepared.json",
    ".task-reap-complete.json",
    ".task-needs-attention.json",
    ".task-watchdog.json",
    ".task-watchdog.lock",
    ".review-watchdog.json",
    ".review-watchdog.lock",
    ".task-agent-command.json",
    ".review-agent-command.json",
    ".obsidian/workspace.json",
    ".obsidian/workspace-mobile.json",
]
CMUX_PASTE_SETTLE_SECONDS = 0.2
REVIEW_CALLBACK_FILE = ".review-callback.json"
SUPERVISED_RECEIVE_TRANSPORT = "supervised-receive-v1"


def die(message: str, code: int = 1) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        die(f"{path} not found")
    except json.JSONDecodeError as exc:
        die(f"{path} is not valid JSON: {exc}")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_callback(path: Path, data: dict[str, Any]) -> None:
    """Publish one validated callback atomically for the executor."""
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_drive_request(worktree: Path, state_dir: Path) -> Path:
    """Publish one operation-bound, task-local drive request for model-safe execution."""
    operation = read_json(state_dir / "operation.json")
    task_meta = read_json(worktree / ".task-meta.json")
    operation_id = str(operation.get("operation_id") or "").strip()
    if (
        operation.get("project_id") != task_meta.get("project_id")
        or operation.get("task_id") != task_meta.get("task_id")
        or not operation_id
        or operation.get("operation_dir") != str(state_dir)
    ):
        die("review drive request identity does not match the task operation", 3)
    path = worktree / f".task-review-drive-{operation_id}.json"
    payload = {
        "schema_version": 1,
        "project_id": operation["project_id"],
        "task_id": operation["task_id"],
        "operation_id": operation_id,
        "operation_dir": str(state_dir),
    }
    write_callback(path, payload)
    return path


def drive_argv_for_callback(worktree: Path, state_dir: Path) -> list[str]:
    """Build a bounded executor command without repeating operation registry paths."""
    if state_dir != worktree:
        action_file = write_drive_request(worktree, state_dir)
        argv = [
            sys.executable,
            "skills/review-dispatch/scripts/spawn_review.py",
            "drive",
            "--worktree",
            ".",
            "--action-file",
            action_file.name,
        ]
    else:
        argv = [
            sys.executable,
            str(VAULT_ROOT / "skills" / "review-dispatch" / "scripts" / "spawn_review.py"),
            "drive",
            "--worktree",
            str(worktree),
        ]
    argv.append("--apply-action")
    return argv


def resolve_state_dir(worktree: Path, raw: str) -> Path:
    state_dir = Path(raw).expanduser().resolve() if raw else worktree
    meta = read_json(state_dir / ".review-meta.json")
    if Path(str(meta.get("worktree") or "")).expanduser().resolve() != worktree:
        die("review state does not match the inspected worktree", 3)
    if state_dir != worktree:
        operation_dir = str(meta.get("operation_dir") or "")
        if operation_dir != str(state_dir) or state_dir.is_symlink():
            die("review state is not the exact broker operation directory", 3)
    return state_dir


def is_handoff(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in HANDOFF_EXCLUDES)


def git_paths(worktree: Path, *args: str) -> list[str]:
    result = run(["git", *args, "-z"], cwd=worktree)
    if result.returncode != 0:
        die((result.stdout + "\n" + result.stderr).strip() or f"git {' '.join(args)} failed")
    return [path for path in result.stdout.split("\0") if path]


def file_hash(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_state(worktree: Path) -> dict[str, str | None]:
    tracked = git_paths(worktree, "ls-files")
    untracked = git_paths(worktree, "ls-files", "--others", "--exclude-standard")
    files: dict[str, str | None] = {}
    for rel in sorted(set(tracked + untracked)):
        if is_handoff(rel):
            continue
        files[rel] = file_hash(worktree / rel)
    return files


def changed_non_handoff(worktree: Path, state_dir: Path) -> list[str]:
    baseline = read_json(state_dir / ".review-baseline-state.json")
    before = baseline.get("files")
    if not isinstance(before, dict):
        die(".review-baseline-state.json has no files object")
    after = current_state(worktree)
    changed: list[str] = []
    for rel in sorted(set(before) | set(after)):
        if before.get(rel) != after.get(rel):
            changed.append(rel)
    return changed


def send_to_surface(surface: str, text: str) -> None:
    send = run(["cmux", "send", "--surface", surface, text])
    if send.returncode != 0:
        die((send.stdout + "\n" + send.stderr).strip() or "cmux send failed")
    # Codex may still be processing the paste when an immediate Enter arrives.
    # Give the TUI one short settle window so the key submits the pasted callback.
    time.sleep(CMUX_PASTE_SETTLE_SECONDS)
    enter = run(["cmux", "send-key", "--surface", surface, "Enter"])
    if enter.returncode != 0:
        die((enter.stdout + "\n" + enter.stderr).strip() or "cmux send-key failed")


def receive_callback(
    worktree: Path, state_dir: Path, relay_path: Path
) -> tuple[str, Path]:
    """Run the deterministic receive half in the trusted supervisor process."""
    script = VAULT_ROOT / "skills" / "review-dispatch" / "scripts" / "spawn_review.py"
    argv = [
        sys.executable,
        str(script),
        "receive",
        "--worktree",
        str(worktree),
    ]
    if state_dir != worktree:
        argv.extend(["--operation-dir", str(state_dir)])
    argv.extend(["--relay-file", str(relay_path)])
    received = run(argv, cwd=worktree)
    if received.returncode != 0:
        die(
            (received.stdout + "\n" + received.stderr).strip()
            or "trusted review receive failed",
            3,
        )
    current = read_json(state_dir / ".review-meta.json")
    action = str(current.get("recommended_action") or "unknown")
    output = state_dir / str(current.get("output_file") or ".task-review.md")
    return action, output


def cmd_send(ns: argparse.Namespace) -> int:
    worktree = Path(ns.worktree).expanduser().resolve()
    state_dir = resolve_state_dir(worktree, getattr(ns, "state_dir", ""))
    meta = read_json(state_dir / ".review-meta.json")
    output_file = ns.output or str(meta.get("output_file") or ".task-review.md")
    output_path = state_dir / output_file
    if not output_path.exists() or output_path.stat().st_size == 0:
        die(f"{output_file} is missing or empty; write the review before review-send")

    changed = changed_non_handoff(worktree, state_dir)
    if changed:
        report = (
            "# Review Send Blocked\n\n"
            "Reviewer changed non-handoff files since the executor baseline. "
            "Do not callback until these changes are reverted or explained.\n\n"
            "## Changed files\n\n"
            + "\n".join(f"- {rel}" for rel in changed)
            + "\n"
        )
        (state_dir / ".review-send-blocked.md").write_text(report, encoding="utf-8")
        print(report, file=sys.stderr)
        return 2

    callback = str(meta.get("executor_callback_command") or "").strip()
    surface = str(meta.get("executor_surface") or "").strip()
    if not callback:
        die(".review-meta.json missing executor_callback_command")
    if not surface:
        die(".review-meta.json missing executor_surface")

    if ns.no_send:
        meta["status"] = "review_validated"
        meta["updated_at"] = utc_now()
        meta["sent_output_file"] = output_file
        write_json(state_dir / ".review-meta.json", meta)
        print(callback)
        return 0

    send_to_surface(surface, callback)
    meta["status"] = "review_sent"
    meta["updated_at"] = utc_now()
    meta["sent_output_file"] = output_file
    write_json(state_dir / ".review-meta.json", meta)
    print(f"sent review callback to executor surface: {surface}")
    print(f"output: {output_path}")
    return 0


def cmd_submit(ns: argparse.Namespace) -> int:
    """Validate a typed JSON payload and callback without product-file writes."""
    worktree = Path(ns.worktree).expanduser().resolve()
    state_dir = resolve_state_dir(worktree, getattr(ns, "state_dir", ""))
    meta = read_json(state_dir / ".review-meta.json")
    expected_run_id = str(meta.get("run_id") or "").strip()
    expected_mode = str(meta.get("review_mode") or "").strip()
    if not expected_run_id:
        die(".review-meta.json missing run_id")
    input_path: Path | None = None
    if ns.input_file:
        input_path = Path(ns.input_file).expanduser()
        if not input_path.is_absolute():
            input_path = worktree / input_path
        input_path = input_path.resolve()
        raw_runtime = str(meta.get("review_runtime_dir") or "").strip()
        runtime_dir = Path(raw_runtime).expanduser().resolve() if raw_runtime else worktree
        expected = (runtime_dir / ".review-outbox.json").resolve()
        if input_path != expected:
            die("--input-file is restricted to .review-outbox.json", 3)
        try:
            raw_payload = input_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            die(".review-outbox.json is missing", 3)
    else:
        raw_payload = sys.stdin.read()
    try:
        review = parse_review_json(
            raw_payload, expected_run_id=expected_run_id, expected_mode=expected_mode
        )
    except ReviewContractError as exc:
        die(f"invalid review payload: {exc}", 3)

    changed = changed_non_handoff(worktree, state_dir)
    if changed:
        print(
            "Review submission blocked: non-handoff files changed since baseline:\n"
            + "\n".join(f"- {rel}" for rel in changed),
            file=sys.stderr,
        )
        return 2

    callback = str(meta.get("executor_callback_command") or "").strip()
    surface = str(meta.get("executor_surface") or "").strip()
    if not callback or not surface:
        die("review metadata is missing executor callback or surface")
    relay_path = state_dir / REVIEW_CALLBACK_FILE
    write_callback(relay_path, review)
    message = f"{callback} --relay-file {shlex.quote(str(relay_path))}"
    if ns.no_send:
        if input_path is not None:
            input_path.unlink(missing_ok=True)
        print(message)
        return 0
    if meta.get("callback_transport") == SUPERVISED_RECEIVE_TRANSPORT:
        action, output = receive_callback(worktree, state_dir, relay_path)
        drive_argv = drive_argv_for_callback(worktree, state_dir)
        message = (
            "Cross-model review callback was validated and received automatically by the trusted "
            f"supervisor. Review: {output}. Recommended action: {action}. "
            "Continue the Review Gate without running receive again. After the required executor "
            f"self-review or resolution, run this exact command: {shlex.join(drive_argv)}"
        )
    try:
        send_to_surface(surface, message)
    except SystemExit:
        if meta.get("callback_transport") != SUPERVISED_RECEIVE_TRANSPORT:
            raise
        # The durable callback was already received and classified. A failed
        # UI notification must not make the relay retry the same completed
        # transition forever; the executor can discover it from operation state.
        print(
            "review callback was received, but the executor surface notification failed",
            file=sys.stderr,
        )
    if input_path is not None:
        input_path.unlink(missing_ok=True)
    if meta.get("callback_transport") == SUPERVISED_RECEIVE_TRANSPORT:
        print(f"received and notified typed review callback on executor surface: {surface}")
    else:
        print(f"submitted typed review callback to executor surface: {surface}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    send = sub.add_parser("send", help="validate handoff files and callback executor")
    send.add_argument("--worktree", default=".", help="task worktree path")
    send.add_argument("--state-dir", default="", help="exact broker operation directory")
    send.add_argument("--output", default="", help="override review output file")
    send.add_argument("--no-send", action="store_true", help="validate and print callback without cmux send")
    send.set_defaults(func=cmd_send)
    submit = sub.add_parser("submit", help="validate typed JSON and send a product-write-free callback")
    submit.add_argument("--worktree", default=".", help="task worktree path")
    submit.add_argument("--state-dir", default="", help="exact broker operation directory")
    submit.add_argument("--input-file", default="", help="Claude-only isolated .review-outbox.json transport")
    submit.add_argument("--no-send", action="store_true", help="validate and print callback without cmux send")
    submit.set_defaults(func=cmd_submit)
    return parser


def main() -> int:
    parser = build_parser()
    ns = parser.parse_args()
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main())
