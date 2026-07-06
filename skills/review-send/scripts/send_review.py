#!/usr/bin/env python3
"""Validate a reviewer handoff and callback the task executor."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn


HANDOFF_EXCLUDES = [
    ".task-prompt.md",
    ".task-summary.md",
    ".task-meta.json",
    ".task-cmux-surface",
    ".task-reap-send-skill",
    ".wiki-cmux-surface",
    ".wiki-agent-runtime",
    ".wiki-reap-command",
    ".task-review.md",
    ".task-review-verify.md",
    ".task-review-resolution.md",
    ".task-review-skill",
    ".task-review-send-skill",
    ".review-prompt.md",
    ".review-prompt-verify.md",
    ".review-meta.json",
    ".review-cmux-surface",
    ".review-baseline-status.txt",
    ".review-baseline-state.json",
    ".review-send-blocked.md",
    ".obsidian/workspace.json",
    ".obsidian/workspace-mobile.json",
]


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


def changed_non_handoff(worktree: Path) -> list[str]:
    baseline = read_json(worktree / ".review-baseline-state.json")
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
    enter = run(["cmux", "send-key", "--surface", surface, "Enter"])
    if enter.returncode != 0:
        die((enter.stdout + "\n" + enter.stderr).strip() or "cmux send-key failed")


def cmd_send(ns: argparse.Namespace) -> int:
    worktree = Path(ns.worktree).expanduser().resolve()
    meta = read_json(worktree / ".review-meta.json")
    output_file = ns.output or str(meta.get("output_file") or ".task-review.md")
    output_path = worktree / output_file
    if not output_path.exists() or output_path.stat().st_size == 0:
        die(f"{output_file} is missing or empty; write the review before review-send")

    changed = changed_non_handoff(worktree)
    if changed:
        report = (
            "# Review Send Blocked\n\n"
            "Reviewer changed non-handoff files since the executor baseline. "
            "Do not callback until these changes are reverted or explained.\n\n"
            "## Changed files\n\n"
            + "\n".join(f"- {rel}" for rel in changed)
            + "\n"
        )
        (worktree / ".review-send-blocked.md").write_text(report, encoding="utf-8")
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
        write_json(worktree / ".review-meta.json", meta)
        print(callback)
        return 0

    send_to_surface(surface, callback)
    meta["status"] = "review_sent"
    meta["updated_at"] = utc_now()
    meta["sent_output_file"] = output_file
    write_json(worktree / ".review-meta.json", meta)
    print(f"sent review callback to executor surface: {surface}")
    print(f"output: {output_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    send = sub.add_parser("send", help="validate handoff files and callback executor")
    send.add_argument("--worktree", default=".", help="task worktree path")
    send.add_argument("--output", default="", help="override review output file")
    send.add_argument("--no-send", action="store_true", help="validate and print callback without cmux send")
    send.set_defaults(func=cmd_send)
    return parser


def main() -> int:
    parser = build_parser()
    ns = parser.parse_args()
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main())
