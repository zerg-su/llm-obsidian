#!/usr/bin/env python3
"""Archive the exact harness review gate associated with one dispatch task."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from harness.review_finalization import require_task_review, review_gate_root


def fail(message: str) -> int:
    print(f"archive-task-reviews: {message}", file=sys.stderr)
    return 3


def read_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


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
        if meta.get("version") not in {3, 4}:
            print(json.dumps({"schema_version": 1, "status": "legacy", "markers": []}))
            return 0
        task_id = str(meta.get("task_id") or "")
        authorization = require_task_review(
            meta,
            worktree,
            expected_vault=vault,
            expected_operation_id=task_id,
        )
        operation = review_gate_root(
            meta,
            worktree,
            expected_vault=vault,
            expected_operation_id=task_id,
        )
        markers: list[str] = []
        if authorization.approved:
            command = [
                sys.executable,
                str(vault / "scripts" / "harness" / "review_archive.py"),
                "--worktree", str(worktree), "--operation-dir", str(operation),
                "--vault-root", str(vault), "--json",
            ]
            if args.dry_run:
                command.append("--dry-run")
            result = subprocess.run(command, cwd=vault, text=True, capture_output=True, check=False)
            if result.returncode != 0:
                return fail((result.stderr or result.stdout).strip() or "review archive failed")
            value = json.loads(result.stdout)
            if value.get("status") not in ({"dry-run"} if args.dry_run else {"archived", "already-current"}):
                return fail("exact review gate did not archive")
            if not args.dry_run:
                marker = operation / ".review-archive.json"
                if not marker.is_file():
                    return fail("exact review archive marker is missing")
                markers.append(str(marker))
        print(json.dumps({
            "schema_version": 1,
            "status": "dry-run" if args.dry_run else "archived",
            "markers": markers,
            "failed_operations": [],
        }, ensure_ascii=False, sort_keys=True))
        return 0
    except (KeyError, OSError, json.JSONDecodeError, ValueError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
