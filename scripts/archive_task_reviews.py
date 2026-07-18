#!/usr/bin/env python3
"""Archive every exact review operation associated with one dispatch task."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from task_sessions import TaskSessionError, TaskSessionStore, read_object


def fail(message: str) -> int:
    print(f"archive-task-reviews: {message}", file=sys.stderr)
    return 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    worktree = args.worktree.expanduser().resolve()
    vault = args.vault_root.expanduser().resolve()
    try:
        meta = read_object(worktree / ".task-meta.json")
        if meta.get("version") != 3:
            print(json.dumps({"schema_version": 1, "status": "legacy", "markers": []}))
            return 0
        store = TaskSessionStore(vault)
        operations = store.list_operations(
            str(meta["project_id"]), str(meta["task_id"]), domain="review"
        )
        markers: list[str] = []
        for operation in operations:
            state_dir = Path(str(operation["operation_dir"])).resolve()
            review_meta = state_dir / ".review-meta.json"
            if not review_meta.is_file():
                if operation.get("status") in {"queued", "starting", "running", "callback-ready"}:
                    return fail(f"review operation {operation['operation_id']} is unfinished")
                continue
            if operation.get("status") != "complete":
                return fail(f"review operation {operation['operation_id']} is not complete")
            command = [
                sys.executable,
                str(vault / "skills" / "review-dispatch" / "scripts" / "archive_review.py"),
                "--worktree", str(worktree), "--operation-dir", str(state_dir),
                "--vault-root", str(vault), "--json",
            ]
            if args.dry_run:
                command.append("--dry-run")
            result = subprocess.run(command, cwd=vault, text=True, capture_output=True, check=False)
            if result.returncode != 0:
                return fail((result.stderr or result.stdout).strip() or "review archive failed")
            value = json.loads(result.stdout)
            if value.get("status") not in ({"dry-run"} if args.dry_run else {"archived", "already-current"}):
                return fail(f"review operation {operation['operation_id']} did not archive")
            if not args.dry_run:
                markers.append(str(state_dir / ".review-archive.json"))
        print(json.dumps({
            "schema_version": 1,
            "status": "dry-run" if args.dry_run else "archived",
            "markers": markers,
        }, ensure_ascii=False, sort_keys=True))
        return 0
    except (KeyError, OSError, json.JSONDecodeError, TaskSessionError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
