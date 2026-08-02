#!/usr/bin/env python3
"""Persistent task/session registry and anchored cmux primitives.

The registry is authoritative local runtime state.  It contains identifiers,
state, and provider checkpoints; callers keep prompts/results in the exact
operation directory returned by :func:`enqueue_operation`.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, NoReturn
from task_session_contracts import (
    DOMAINS,
    LANE_STATES,
    OPERATION_STATES,
    RUNTIMES,
    SCHEMA_VERSION,
    TASK_STATES,
    TaskSessionError,
    UUID_RE,
    require_token,
    require_uuid,
)
from task_session_cmux_layout import (
    capture_resume,
    close_replacement_shell,
    close_surface_exact,
    cmux_capabilities,
    cmux_tree,
    pane_layout,
    parse_surface,
    parse_workspace,
    surface_context,
    surface_workspace,
    spawn_right,
    spawn_workspace,
    validate_checkpoint,
    workspace_layout,
)
from task_session_store import TaskSessionStore
from task_session_store_io import (
    atomic_write,
    atomic_write_file_only,
    ensure_owner_only_dir,
    file_lock,
    git_common_dir,
    lane_id_for,
    project_id_for,
    read_object,
    remove_owned_research_scratch,
    utc_now,
)


def die(message: str, code: int = 3) -> NoReturn:
    print(f"task-sessions: {message}", file=sys.stderr)
    raise SystemExit(code)
























def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", type=Path, default=Path(__file__).resolve().parents[1])
    sub = parser.add_subparsers(dest="command", required=True)
    identity = sub.add_parser("identity")
    identity.add_argument("--worktree", type=Path, default=Path.cwd())
    identity.add_argument("--create", action="store_true")
    capabilities = sub.add_parser("capabilities")
    initialize = sub.add_parser("init-task")
    initialize.add_argument("--worktree", type=Path, default=Path.cwd())
    initialize.add_argument("--task-id", default="")
    initialize.add_argument("--runtime", choices=sorted(RUNTIMES), required=True)
    initialize.add_argument("--session-id", required=True)
    ensure_session = sub.add_parser("ensure-session-task")
    ensure_session.add_argument("--worktree", type=Path, default=Path.cwd())
    ensure_session.add_argument("--task-id", default="")
    ensure_session.add_argument("--runtime", choices=sorted(RUNTIMES), required=True)
    ensure_session.add_argument("--session-id", required=True)
    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("--worktree", type=Path, default=Path.cwd())
    enqueue.add_argument("--project-id", required=True)
    enqueue.add_argument("--task-id", required=True)
    enqueue.add_argument("--domain", choices=sorted(DOMAINS), required=True)
    enqueue.add_argument("--runtime", choices=sorted(RUNTIMES), required=True)
    enqueue.add_argument("--model", required=True)
    enqueue.add_argument("--effort", required=True)
    enqueue.add_argument("--operation-type", required=True)
    enqueue.add_argument("--operation-id")
    enqueue.add_argument("--coordinator-surface", required=True)
    archive = sub.add_parser("archive")
    archive.add_argument("--project-id", required=True)
    archive.add_argument("--task-id", required=True)
    list_operations = sub.add_parser("list-operations")
    list_operations.add_argument("--project-id", required=True)
    list_operations.add_argument("--task-id", required=True)
    list_operations.add_argument("--domain", choices=sorted(DOMAINS), default="")
    fail_operation = sub.add_parser(
        "fail-operation",
        help="release one exact active operation after a confirmed launcher/runtime failure",
    )
    fail_operation.add_argument("--project-id", required=True)
    fail_operation.add_argument("--task-id", required=True)
    fail_operation.add_argument("--lane-id", required=True)
    fail_operation.add_argument("--operation-id", required=True)
    fail_operation.add_argument(
        "--reason", default="coordinator-confirmed operation recovery"
    )
    args = parser.parse_args()
    try:
        if args.command == "capabilities":
            print(json.dumps(cmux_capabilities(), sort_keys=True))
            return 0
        if args.command == "identity":
            print(json.dumps({"project_id": project_id_for(args.worktree, create=args.create)}, sort_keys=True))
            return 0
        store = TaskSessionStore(args.vault_root)
        if args.command == "init-task":
            project_id = project_id_for(args.worktree, create=True)
            task_id = require_uuid(args.task_id or str(uuid.uuid4()), "task_id")
            store.create_task(project_id, task_id, worktree=args.worktree)
            store.bind_session(
                project_id, task_id, runtime=args.runtime,
                session_id=args.session_id, explicit=True,
            )
            pointer = args.worktree.resolve() / ".task-session-binding.json"
            current = read_object(pointer, required=False)
            if current and (
                current.get("project_id") != project_id or current.get("task_id") != task_id
            ):
                raise TaskSessionError("worktree is already bound to another active task")
            atomic_write_file_only(pointer, {
                "schema_version": SCHEMA_VERSION,
                "project_id": project_id,
                "task_id": task_id,
                "updated_at": utc_now(),
            })
            print(json.dumps({"project_id": project_id, "task_id": task_id}, sort_keys=True))
            return 0
        if args.command == "ensure-session-task":
            project_id = project_id_for(args.worktree, create=True)
            requested = require_uuid(args.task_id, "task_id") if args.task_id else ""
            if not requested:
                active: list[dict[str, Any]] = []
                for current in store.session_bindings(
                    runtime=args.runtime, session_id=args.session_id
                ):
                    if current.get("project_id") != project_id:
                        current_task = read_object(
                            store.task_path(str(current["project_id"]), str(current["task_id"])),
                            required=False,
                        )
                        if current_task.get("status") in {"active", "degraded", "archiving"}:
                            raise TaskSessionError("session binding belongs to a different active project")
                        continue
                    current_task = read_object(
                        store.task_path(str(current["project_id"]), str(current["task_id"])),
                        required=False,
                    )
                    if current_task.get("status") in {"active", "degraded"}:
                        active.append(current)
                    elif current_task.get("status") not in {"archived"}:
                        raise TaskSessionError("session binding points to an unavailable task state")
                if len(active) > 1:
                    raise TaskSessionError(
                        "session has multiple active tasks; pass an explicit task_id"
                    )
                if active:
                    print(json.dumps({
                        "project_id": active[0]["project_id"], "task_id": active[0]["task_id"],
                    }, sort_keys=True))
                    return 0
            task_id = requested or str(uuid.uuid4())
            store.create_task(project_id, task_id, worktree=args.worktree)
            store.bind_session(
                project_id, task_id, runtime=args.runtime,
                session_id=args.session_id, explicit=bool(requested),
            )
            print(json.dumps({"project_id": project_id, "task_id": task_id}, sort_keys=True))
            return 0
        if args.command == "enqueue":
            store.create_task(args.project_id, args.task_id, worktree=args.worktree)
            value = store.enqueue_operation(
                args.project_id, args.task_id, domain=args.domain, runtime=args.runtime,
                model=args.model, effort=args.effort, operation_type=args.operation_type,
                coordinator_surface=args.coordinator_surface, operation_id=args.operation_id,
            )
            print(json.dumps(value, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "list-operations":
            print(json.dumps(store.list_operations(
                args.project_id, args.task_id, domain=args.domain
            ), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "fail-operation":
            operation = store.transition_operation(
                args.project_id,
                args.task_id,
                args.lane_id,
                args.operation_id,
                "failed",
                degradation=str(args.reason)[:300],
            )
            lane = store.lane_state(args.project_id, args.task_id, args.lane_id)
            queue = lane.get("queue")
            next_operation_id = queue[0] if isinstance(queue, list) and queue else None
            print(json.dumps({
                "operation_id": operation["operation_id"],
                "status": operation["status"],
                "lane_status": lane["status"],
                "next_operation_id": next_operation_id,
            }, ensure_ascii=False, sort_keys=True))
            return 0
        print(json.dumps(store.archive_task(args.project_id, args.task_id), sort_keys=True))
        return 0
    except (TaskSessionError, OSError) as exc:
        die(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
