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
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, NoReturn


SCHEMA_VERSION = 1
TASK_STATES = {"active", "archiving", "archived", "degraded"}
LANE_STATES = {"idle", "starting", "running", "callback-ready", "failed", "archived"}
OPERATION_STATES = {"queued", "starting", "running", "callback-ready", "complete", "failed"}
DOMAINS = {"normal", "review", "secure-fetch", "secure-synth"}
RUNTIMES = {"claude", "codex"}
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


class TaskSessionError(ValueError):
    pass


def die(message: str, code: int = 3) -> NoReturn:
    print(f"task-sessions: {message}", file=sys.stderr)
    raise SystemExit(code)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def require_uuid(value: str, field: str) -> str:
    normalized = value.strip().lower()
    if UUID_RE.fullmatch(normalized) is None:
        raise TaskSessionError(f"{field} must be a UUID")
    return normalized


def require_token(value: str, field: str) -> str:
    normalized = value.strip()
    if SAFE_TOKEN_RE.fullmatch(normalized) is None:
        raise TaskSessionError(f"{field} is invalid")
    return normalized


def read_object(path: Path, *, required: bool = True) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not required:
            return {}
        raise TaskSessionError(f"missing state file: {path}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskSessionError(f"invalid state file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TaskSessionError(f"state file must contain an object: {path}")
    return value


def ensure_owner_only_dir(path: Path) -> Path:
    try:
        info = path.stat()
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        info = path.stat()
    else:
        if path.is_symlink() or not path.is_dir():
            raise TaskSessionError(f"state directory is not an owned directory: {path}")
        if info.st_uid != os.getuid():
            raise TaskSessionError(f"state directory is not owner-only: {path}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            path.chmod(0o700)
            info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise TaskSessionError(f"state directory is not owner-only: {path}")
    return path


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    ensure_owner_only_dir(path.parent)
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_write_file_only(path: Path, value: dict[str, Any]) -> None:
    """Atomically write an owner-only file without changing its parent mode."""
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir() or parent.stat().st_uid != os.getuid():
        raise TaskSessionError(f"state file parent is missing or not owned by the current user: {parent}")
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        path.chmod(0o600)
    finally:
        temp.unlink(missing_ok=True)


def remove_owned_research_scratch(path: Path, vault_root: Path) -> bool:
    """Remove only a coordinator-created fetch/synth temp directory."""
    try:
        resolved = path.expanduser().resolve()
        resolved.relative_to(vault_root)
    except ValueError:
        pass
    else:
        return False
    if (
        path.is_symlink()
        or not resolved.is_dir()
        or resolved.stat().st_uid != os.getuid()
        or not resolved.name.startswith(("llm-obsidian-fetch-", "llm-obsidian-synth-"))
    ):
        return False
    shutil.rmtree(resolved)
    return True


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    ensure_owner_only_dir(path.parent)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with os.fdopen(descriptor, "r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        pass


def git_common_dir(worktree: Path) -> Path:
    root = worktree.expanduser().resolve()
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
        text=True,
        capture_output=True,
        check=False,
    )
    raw = result.stdout.strip()
    if result.returncode != 0 or not raw or "\n" in raw or "\0" in raw:
        raise TaskSessionError("persistent task sessions require a Git worktree")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    common = candidate.resolve()
    if not common.is_dir() or common.stat().st_uid != os.getuid():
        raise TaskSessionError("Git common directory is missing or not owned by the current user")
    return common


def project_id_for(worktree: Path, *, create: bool) -> str:
    common = git_common_dir(worktree)
    marker_dir = common / "llm-obsidian"
    marker = marker_dir / "project-id"
    lock = marker_dir / "project-id.lock"
    with file_lock(lock):
        if marker.exists():
            try:
                value = marker.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise TaskSessionError(f"cannot read project identity: {exc}") from exc
            if marker.stat().st_uid != os.getuid() or stat.S_IMODE(marker.stat().st_mode) & 0o077:
                raise TaskSessionError("project identity marker is not owner-only")
            return require_uuid(value, "project_id")
        if not create:
            raise TaskSessionError("project identity marker is missing")
        value = str(uuid.uuid4())
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return value


def lane_id_for(project_id: str, task_id: str, domain: str, runtime: str, model: str) -> str:
    project_id = require_uuid(project_id, "project_id")
    task_id = require_uuid(task_id, "task_id")
    if domain not in DOMAINS:
        raise TaskSessionError(f"domain must be one of {sorted(DOMAINS)}")
    if runtime not in RUNTIMES:
        raise TaskSessionError(f"runtime must be one of {sorted(RUNTIMES)}")
    if not model.strip() or len(model) > 200 or "\0" in model:
        raise TaskSessionError("model is invalid")
    payload = "\0".join((project_id, task_id, domain, runtime, model)).encode()
    return hashlib.sha256(payload).hexdigest()[:32]


class TaskSessionStore:
    def __init__(self, vault_root: Path):
        self.vault_root = vault_root.expanduser().resolve()
        if not (self.vault_root / "wiki").is_dir():
            raise TaskSessionError("vault root must contain wiki/")
        self.root = ensure_owner_only_dir(self.vault_root / ".vault-meta" / "task-sessions")

    def project_dir(self, project_id: str) -> Path:
        return self.root / "projects" / require_uuid(project_id, "project_id")

    def task_dir(self, project_id: str, task_id: str) -> Path:
        return self.project_dir(project_id) / "tasks" / require_uuid(task_id, "task_id")

    def task_path(self, project_id: str, task_id: str) -> Path:
        return self.task_dir(project_id, task_id) / "task.json"

    def task_lock(self, project_id: str, task_id: str) -> Path:
        return self.task_dir(project_id, task_id) / "task.lock"

    def lane_dir(self, project_id: str, task_id: str, lane_id: str) -> Path:
        return self.task_dir(project_id, task_id) / "lanes" / require_token(lane_id, "lane_id")

    def create_task(self, project_id: str, task_id: str, *, worktree: Path) -> dict[str, Any]:
        project_id = require_uuid(project_id, "project_id")
        task_id = require_uuid(task_id, "task_id")
        task_path = self.task_path(project_id, task_id)
        with file_lock(self.task_lock(project_id, task_id)):
            current = read_object(task_path, required=False)
            resolved_worktree = str(worktree.expanduser().resolve())
            if current:
                if current.get("project_id") != project_id or current.get("task_id") != task_id:
                    raise TaskSessionError("task registry identity conflict")
                known = current.get("worktrees", [])
                if not isinstance(known, list) or any(not isinstance(item, str) for item in known):
                    raise TaskSessionError("task worktree registry is corrupt")
                if resolved_worktree not in known:
                    known.append(resolved_worktree)
                    current["worktrees"] = sorted(set(known))
                    current["updated_at"] = utc_now()
                    atomic_write(task_path, current)
                return current
            now = utc_now()
            value = {
                "schema_version": SCHEMA_VERSION,
                "project_id": project_id,
                "task_id": task_id,
                "status": "active",
                "worktrees": [resolved_worktree],
                "created_at": now,
                "updated_at": now,
            }
            atomic_write(task_path, value)
            return value

    def bind_session(
        self,
        project_id: str,
        task_id: str,
        *,
        runtime: str,
        session_id: str,
        explicit: bool,
    ) -> dict[str, Any]:
        if runtime not in RUNTIMES:
            raise TaskSessionError("binding runtime is invalid")
        session_id = require_token(session_id, "session_id")
        project_id = require_uuid(project_id, "project_id")
        task_id = require_uuid(task_id, "task_id")
        session_key = hashlib.sha256(f"{runtime}\0{session_id}".encode()).hexdigest()
        binding_id = hashlib.sha256(
            f"{runtime}\0{session_id}\0{project_id}\0{task_id}".encode()
        ).hexdigest()
        path = self.root / "session-bindings" / runtime / f"{binding_id}.json"
        lock = self.root / "session-bindings" / runtime / f"{session_key}.lock"
        with file_lock(lock):
            if not explicit:
                for current in self.session_bindings(runtime=runtime, session_id=session_id):
                    if current.get("project_id") == project_id and current.get("task_id") == task_id:
                        continue
                    prior = read_object(
                        self.task_path(str(current["project_id"]), str(current["task_id"])),
                        required=False,
                    )
                    if prior.get("status") != "archived":
                        raise TaskSessionError("session is already bound to another active task")
            value = {
                "schema_version": SCHEMA_VERSION,
                "runtime": runtime,
                "session_id": session_id,
                "project_id": project_id,
                "task_id": task_id,
                "explicit": bool(explicit),
                "updated_at": utc_now(),
            }
            atomic_write(path, value)
            return value

    def session_bindings(self, *, runtime: str, session_id: str) -> list[dict[str, Any]]:
        """Return every task explicitly associated with one provider session."""
        if runtime not in RUNTIMES:
            raise TaskSessionError("binding runtime is invalid")
        session_id = require_token(session_id, "session_id")
        root = self.root / "session-bindings" / runtime
        values: dict[tuple[str, str], dict[str, Any]] = {}
        for path in sorted(root.glob("*.json")) if root.is_dir() else []:
            current = read_object(path)
            if current.get("runtime") != runtime or current.get("session_id") != session_id:
                continue
            project_id = require_uuid(str(current.get("project_id") or ""), "project_id")
            task_id = require_uuid(str(current.get("task_id") or ""), "task_id")
            values[(project_id, task_id)] = current
        return [values[key] for key in sorted(values)]

    def session_binding(self, *, runtime: str, session_id: str) -> dict[str, Any]:
        values = self.session_bindings(runtime=runtime, session_id=session_id)
        if len(values) > 1:
            raise TaskSessionError(
                "session is bound to multiple tasks; an explicit task_id is required"
            )
        return values[0] if values else {}

    def enqueue_operation(
        self,
        project_id: str,
        task_id: str,
        *,
        domain: str,
        runtime: str,
        model: str,
        operation_type: str,
        effort: str,
        coordinator_surface: str,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        operation_id = require_uuid(operation_id or str(uuid.uuid4()), "operation_id")
        lane_id = lane_id_for(project_id, task_id, domain, runtime, model)
        task_path = self.task_path(project_id, task_id)
        lane_dir = self.lane_dir(project_id, task_id, lane_id)
        lane_path = lane_dir / "lane.json"
        operation_dir = lane_dir / "operations" / operation_id
        operation_path = operation_dir / "operation.json"
        with file_lock(self.task_lock(project_id, task_id)):
            task = read_object(task_path)
            if task.get("status") != "active":
                raise TaskSessionError(f"task does not accept operations in state {task.get('status')!r}")
            with file_lock(lane_dir / "lane.lock"):
                current_operation = read_object(operation_path, required=False)
                if current_operation:
                    immutable = {
                        "project_id": project_id,
                        "task_id": task_id,
                        "lane_id": lane_id,
                        "operation_id": operation_id,
                        "operation_type": operation_type,
                    }
                    if any(current_operation.get(key) != value for key, value in immutable.items()):
                        raise TaskSessionError("operation id collision")
                    return current_operation
                lane = read_object(lane_path, required=False)
                now = utc_now()
                if not lane:
                    lane = {
                        "schema_version": SCHEMA_VERSION,
                        "project_id": project_id,
                        "task_id": task_id,
                        "lane_id": lane_id,
                        "domain": domain,
                        "runtime": runtime,
                        "model": model,
                        "status": "idle",
                        "active_operation_id": None,
                        "queue": [],
                        "checkpoint": None,
                        "created_at": now,
                        "updated_at": now,
                    }
                if lane.get("status") not in LANE_STATES - {"archived"}:
                    raise TaskSessionError("lane state is corrupt or archived")
                if any(lane.get(key) != value for key, value in {
                    "project_id": project_id, "task_id": task_id, "lane_id": lane_id,
                    "domain": domain, "runtime": runtime, "model": model,
                }.items()):
                    raise TaskSessionError("lane identity conflict")
                queue = lane.get("queue")
                if not isinstance(queue, list) or any(not isinstance(item, str) for item in queue):
                    raise TaskSessionError("lane queue is corrupt")
                ensure_owner_only_dir(operation_dir)
                operation = {
                    "schema_version": SCHEMA_VERSION,
                    "project_id": project_id,
                    "task_id": task_id,
                    "lane_id": lane_id,
                    "operation_id": operation_id,
                    "operation_type": require_token(operation_type, "operation_type"),
                    "domain": domain,
                    "runtime": runtime,
                    "model": model,
                    "effort": require_token(effort, "effort"),
                    "coordinator_surface": require_token(coordinator_surface, "coordinator_surface"),
                    "status": "queued",
                    "created_at": now,
                    "updated_at": now,
                    "operation_dir": str(operation_dir),
                }
                queue.append(operation_id)
                lane["queue"] = queue
                lane["updated_at"] = now
                atomic_write(operation_path, operation)
                atomic_write(lane_path, lane)
                return operation

    def claim_next(
        self,
        project_id: str,
        task_id: str,
        lane_id: str,
        expected_operation_id: str | None = None,
    ) -> dict[str, Any] | None:
        expected = (
            require_uuid(expected_operation_id, "expected_operation_id")
            if expected_operation_id is not None else None
        )
        lane_dir = self.lane_dir(project_id, task_id, lane_id)
        lane_path = lane_dir / "lane.json"
        with file_lock(lane_dir / "lane.lock"):
            lane = read_object(lane_path)
            active = lane.get("active_operation_id")
            if active:
                return None
            queue = lane.get("queue")
            if not isinstance(queue, list) or any(not isinstance(item, str) for item in queue):
                raise TaskSessionError("lane queue is corrupt")
            expected_discarded = False
            while queue:
                raw_operation_id = queue[0]
                try:
                    operation_id = require_uuid(raw_operation_id, "queued operation_id")
                    operation_path = lane_dir / "operations" / operation_id / "operation.json"
                    operation = read_object(operation_path)
                    if any(operation.get(key) != value for key, value in {
                        "project_id": project_id,
                        "task_id": task_id,
                        "lane_id": lane_id,
                        "operation_id": operation_id,
                    }.items()):
                        raise TaskSessionError("queued operation identity is corrupt")
                except TaskSessionError:
                    queue.pop(0)
                    now = utc_now()
                    discarded = lane.get("discarded_queue_entries", [])
                    if not isinstance(discarded, list):
                        discarded = []
                    discarded.append({
                        "operation_id": raw_operation_id if UUID_RE.fullmatch(raw_operation_id) else None,
                        "entry_sha256": hashlib.sha256(raw_operation_id.encode()).hexdigest(),
                        "reason": "invalid-operation-state",
                        "discarded_at": now,
                    })
                    lane["discarded_queue_entries"] = discarded[-50:]
                    lane["queue"] = queue
                    lane["status"] = "failed"
                    lane["updated_at"] = now
                    atomic_write(lane_path, lane)
                    print(
                        "task-sessions: skipped corrupt queued operation "
                        f"{raw_operation_id if UUID_RE.fullmatch(raw_operation_id) else '<invalid-id>'}; "
                        "the exact lane remains available",
                        file=sys.stderr,
                    )
                    if expected == raw_operation_id:
                        expected_discarded = True
                    continue
                if operation.get("status") != "queued":
                    queue.pop(0)
                    lane["queue"] = queue
                    lane["updated_at"] = utc_now()
                    atomic_write(lane_path, lane)
                    continue
                if expected_discarded:
                    raise TaskSessionError("expected queued operation state was corrupt and was discarded")
                if expected is not None and operation_id != expected:
                    return None
                queue.pop(0)
                now = utc_now()
                operation["status"] = "starting"
                operation["updated_at"] = now
                lane["status"] = "starting"
                lane["active_operation_id"] = operation_id
                lane["queue"] = queue
                lane["updated_at"] = now
                atomic_write(operation_path, operation)
                atomic_write(lane_path, lane)
                return operation
            if expected_discarded:
                raise TaskSessionError("expected queued operation state was corrupt and was discarded")
            lane["queue"] = []
            lane["status"] = "idle"
            lane["updated_at"] = utc_now()
            atomic_write(lane_path, lane)
            return None

    def lane_state(self, project_id: str, task_id: str, lane_id: str) -> dict[str, Any]:
        lane_dir = self.lane_dir(project_id, task_id, lane_id)
        with file_lock(lane_dir / "lane.lock"):
            lane = read_object(lane_dir / "lane.json")
            if lane.get("project_id") != project_id or lane.get("task_id") != task_id:
                raise TaskSessionError("lane identity is corrupt")
            return lane

    def transition_operation(
        self,
        project_id: str,
        task_id: str,
        lane_id: str,
        operation_id: str,
        status: str,
        *,
        surface: str = "",
        checkpoint: dict[str, Any] | None = None,
        degradation: str = "",
    ) -> dict[str, Any]:
        if status not in OPERATION_STATES:
            raise TaskSessionError("operation status is invalid")
        if status == "queued":
            raise TaskSessionError("queued state is created only by enqueue_operation")
        lane_dir = self.lane_dir(project_id, task_id, lane_id)
        lane_path = lane_dir / "lane.json"
        operation_path = lane_dir / "operations" / require_uuid(operation_id, "operation_id") / "operation.json"
        with file_lock(lane_dir / "lane.lock"):
            lane = read_object(lane_path)
            operation = read_object(operation_path)
            current_status = str(operation.get("status") or "")
            if current_status in {"complete", "failed"}:
                if current_status != status:
                    raise TaskSessionError(
                        f"terminal operation cannot transition from {current_status} to {status}"
                    )
                # Recover an interrupted two-file terminal transition. The
                # operation record is written before lane.json; process loss
                # between those writes must not leave the exact terminal
                # operation holding the lane forever. Never touch a lane that
                # has already advanced to a different operation.
                if lane.get("active_operation_id") == operation_id:
                    if checkpoint is not None:
                        lane["checkpoint"] = validate_checkpoint(
                            checkpoint, str(lane.get("runtime") or "")
                        )
                    lane["status"] = "failed" if status == "failed" else "idle"
                    lane["active_operation_id"] = None
                    lane.pop("surface", None)
                    lane["updated_at"] = utc_now()
                    atomic_write(lane_path, lane)
                return operation
            if lane.get("active_operation_id") != operation_id:
                raise TaskSessionError("only the lane's active operation may transition")
            now = utc_now()
            operation["status"] = status
            operation["updated_at"] = now
            if surface:
                operation["surface"] = require_token(surface, "surface")
                lane["surface"] = surface
            if degradation:
                operation["degradation"] = degradation[:300]
            if checkpoint is not None:
                lane["checkpoint"] = validate_checkpoint(checkpoint, str(lane.get("runtime") or ""))
            if status in {"starting", "running", "callback-ready"}:
                lane["status"] = status
                lane["active_operation_id"] = operation_id
            else:
                lane["status"] = "failed" if status == "failed" else "idle"
                lane["active_operation_id"] = None
                lane.pop("surface", None)
            lane["updated_at"] = now
            atomic_write(operation_path, operation)
            atomic_write(lane_path, lane)
            return operation

    def archive_task(self, project_id: str, task_id: str) -> dict[str, Any]:
        task_path = self.task_path(project_id, task_id)
        with file_lock(self.task_lock(project_id, task_id)):
            task = read_object(task_path)
            if task.get("status") == "archived":
                return task
            if task.get("status") not in {"active", "degraded", "archiving"}:
                raise TaskSessionError("task cannot enter archive from its current state")
            lanes_root = self.task_dir(project_id, task_id) / "lanes"
            lanes = sorted(lanes_root.glob("*/lane.json")) if lanes_root.is_dir() else []
            for lane_path in lanes:
                lane = read_object(lane_path)
                if lane.get("active_operation_id") or lane.get("queue"):
                    raise TaskSessionError("task has active or queued operations")
            if task.get("status") != "archiving":
                task["status"] = "archiving"
                task["updated_at"] = utc_now()
                atomic_write(task_path, task)
            archived_lanes = 0
            try:
                for lane_path in lanes:
                    lane = read_object(lane_path)
                    lane["status"] = "archived"
                    lane["surface"] = None
                    lane["updated_at"] = utc_now()
                    atomic_write(lane_path, lane)
                    archived_lanes += 1
                task["status"] = "archived"
                task["archived_at"] = utc_now()
                task["updated_at"] = task["archived_at"]
                atomic_write(task_path, task)
            except (OSError, TaskSessionError) as exc:
                task["status"] = "degraded" if archived_lanes else "active"
                task["archive_failure"] = "partial-lane-archive" if archived_lanes else "pre-lane-archive"
                task["updated_at"] = utc_now()
                try:
                    atomic_write(task_path, task)
                except (OSError, TaskSessionError):
                    pass
                raise TaskSessionError(
                    "task archive failed and was contained for an idempotent retry"
                ) from exc
            cleanup_failures: list[str] = []
            for lane_path in lanes:
                runtime_dir = lane_path.parent / "runtime"
                try:
                    if runtime_dir.exists():
                        shutil.rmtree(runtime_dir)
                except OSError:
                    cleanup_failures.append(lane_path.parent.name)
                for state_path in lane_path.parent.glob("operations/*/state.json"):
                    state = read_object(state_path, required=False)
                    for key in ("fetch_dir", "synth_dir"):
                        raw = str(state.get(key) or "").strip()
                        if not raw:
                            continue
                        try:
                            removed = remove_owned_research_scratch(Path(raw), self.vault_root)
                        except OSError:
                            removed = False
                        if not removed and Path(raw).exists():
                            cleanup_failures.append(f"{lane_path.parent.name}:{key}")
            if cleanup_failures:
                task["cleanup_status"] = "degraded"
                task["runtime_cleanup_failures"] = cleanup_failures
                task["updated_at"] = utc_now()
                atomic_write(task_path, task)
            return task

    def list_operations(
        self, project_id: str, task_id: str, *, domain: str = ""
    ) -> list[dict[str, Any]]:
        root = self.task_dir(project_id, task_id) / "lanes"
        values: list[dict[str, Any]] = []
        for path in sorted(root.glob("*/operations/*/operation.json")) if root.is_dir() else []:
            value = read_object(path)
            if domain and value.get("domain") != domain:
                continue
            value = dict(value)
            value["operation_dir"] = str(path.parent)
            values.append(value)
        return values


def validate_checkpoint(value: dict[str, Any], runtime: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TaskSessionError("resume checkpoint must be an object")
    kind = str(value.get("kind") or "").strip().lower()
    checkpoint_id = str(value.get("checkpoint_id") or value.get("checkpoint") or "").strip()
    cwd = str(value.get("cwd") or "").strip()
    if kind != runtime or not SAFE_TOKEN_RE.fullmatch(checkpoint_id):
        raise TaskSessionError("resume checkpoint does not match lane runtime")
    path = Path(cwd).expanduser()
    if not path.is_absolute():
        raise TaskSessionError("resume checkpoint cwd must be absolute")
    return {"kind": kind, "checkpoint_id": checkpoint_id, "cwd": str(path.resolve())}


def cmux_capabilities(runner: Any = subprocess.run) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    commands = {
        "anchored_split": ["cmux", "new-split", "--help"],
        "typed_resume": ["cmux", "surface", "resume", "--help"],
    }
    outputs: dict[str, str] = {}
    for name, command in commands.items():
        try:
            result = runner(command, text=True, capture_output=True, check=False)
        except OSError:
            checks[name] = False
            continue
        output = (result.stdout + result.stderr)[:20_000]
        outputs[name] = output
        checks[name] = result.returncode == 0
    checks["anchored_split"] = bool(checks.get("anchored_split") and "--surface" in outputs.get("anchored_split", ""))
    resume_text = outputs.get("typed_resume", "")
    checks["typed_resume"] = bool(
        checks.get("typed_resume")
        and all(token in resume_text for token in ("resume get", "resume set", "resume show", "resume clear"))
    )
    return {"schema_version": SCHEMA_VERSION, **checks}


def parse_surface(output: str) -> tuple[str, str]:
    uuid_match = re.search(
        r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b",
        output,
    )
    ref_match = re.search(r"\bsurface:\d+\b", output)
    if uuid_match is None:
        raise TaskSessionError("cmux did not return a surface UUID")
    return uuid_match.group(0), ref_match.group(0) if ref_match else ""


def cmux_tree(runner: Any = subprocess.run) -> dict[str, Any]:
    result = runner(
        ["cmux", "rpc", "system.tree", '{"all":true}'],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise TaskSessionError("cmux surface workspace lookup failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise TaskSessionError("cmux surface workspace lookup returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise TaskSessionError("cmux surface workspace lookup returned invalid data")
    return payload


def pane_layout(workspace: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    layout: dict[str, list[dict[str, str]]] = {}
    for pane in workspace.get("panes", []) if isinstance(workspace, dict) else []:
        if not isinstance(pane, dict):
            continue
        pane_key = str(pane.get("ref") or pane.get("id") or "")
        if not pane_key:
            continue
        surfaces: list[dict[str, str]] = []
        for candidate in pane.get("surfaces", []):
            if isinstance(candidate, dict):
                surfaces.append({
                    "surface": str(candidate.get("id") or ""),
                    "surface_ref": str(candidate.get("ref") or ""),
                })
        layout[pane_key] = surfaces
    return layout


def workspace_layout(
    payload: dict[str, Any], window_id: str, workspace_id: str
) -> dict[str, list[dict[str, str]]]:
    matches: list[dict[str, list[dict[str, str]]]] = []
    for window in payload.get("windows", []):
        if not isinstance(window, dict) or window_id not in {
            str(window.get("id") or ""), str(window.get("ref") or "")
        }:
            continue
        for workspace in window.get("workspaces", []):
            if isinstance(workspace, dict) and workspace_id in {
                str(workspace.get("id") or ""), str(workspace.get("ref") or "")
            }:
                matches.append(pane_layout(workspace))
    if len(matches) != 1:
        raise TaskSessionError("cmux workspace does not resolve to one exact layout")
    return matches[0]


def surface_context(
    surface: str, runner: Any = subprocess.run, *, missing_ok: bool = False
) -> dict[str, Any] | None:
    """Resolve an exact surface to its window/workspace without consulting focus."""

    payload = cmux_tree(runner)
    matches: list[dict[str, Any]] = []
    for window in payload.get("windows", []) if isinstance(payload, dict) else []:
        if not isinstance(window, dict):
            continue
        for workspace in window.get("workspaces", []):
            if not isinstance(workspace, dict):
                continue
            for pane in workspace.get("panes", []):
                if not isinstance(pane, dict):
                    continue
                for candidate in pane.get("surfaces", []):
                    if not isinstance(candidate, dict):
                        continue
                    if surface not in {str(candidate.get("id") or ""), str(candidate.get("ref") or "")}:
                        continue
                    context = {
                        "surface": str(candidate.get("id") or ""),
                        "surface_ref": str(candidate.get("ref") or ""),
                        "pane": str(pane.get("id") or ""),
                        "pane_ref": str(pane.get("ref") or ""),
                        "workspace": str(workspace.get("id") or ""),
                        "workspace_ref": str(workspace.get("ref") or ""),
                        "window": str(window.get("id") or ""),
                        "window_ref": str(window.get("ref") or ""),
                        "workspace_layout": pane_layout(workspace),
                    }
                    if context not in matches:
                        matches.append(context)
    if not matches and missing_ok:
        return None
    if len(matches) != 1:
        raise TaskSessionError("cmux surface does not resolve to one exact workspace")
    return matches[0]


def surface_workspace(surface: str, runner: Any = subprocess.run) -> str:
    """Resolve an exact surface to its workspace without consulting focus."""

    context = surface_context(surface, runner)
    assert context is not None
    return context["workspace"] or context["workspace_ref"]


def close_replacement_shell(
    context: dict[str, Any], runner: Any = subprocess.run
) -> None:
    """Collapse only the shell replacement created for a last-surface split."""

    before = context.get("workspace_layout")
    if not isinstance(before, dict):
        raise TaskSessionError("cmux surface close layout is incomplete")
    target_pane = str(context.get("pane_ref") or context.get("pane") or "")
    target_surfaces = before.get(target_pane)
    if (
        len(before) <= 1
        or not target_pane
        or not isinstance(target_surfaces, list)
        or len(target_surfaces) != 1
    ):
        return
    window = str(context.get("window_ref") or context.get("window") or "")
    workspace = str(context.get("workspace_ref") or context.get("workspace") or "")
    after = workspace_layout(cmux_tree(runner), window, workspace)
    replacement: dict[str, str] | None = None
    if target_pane in after:
        current = after[target_pane]
        stable = all(
            after.get(pane) == surfaces
            for pane, surfaces in before.items()
            if pane != target_pane
        )
        if set(after) == set(before) and stable and len(current) == 1 and current != target_surfaces:
            replacement = current[0]
    else:
        added_panes = [pane for pane in after if pane not in before]
        removed_panes = [pane for pane in before if pane not in after]
        stable = all(
            after.get(pane) == surfaces
            for pane, surfaces in before.items()
            if pane != target_pane
        )
        if (
            removed_panes == [target_pane]
            and len(added_panes) == 1
            and stable
            and len(after[added_panes[0]]) == 1
        ):
            replacement = after[added_panes[0]][0]
        elif not added_panes:
            return
    if replacement is None:
        raise TaskSessionError("cmux last-surface replacement is ambiguous")
    replacement_target = replacement.get("surface_ref") or replacement.get("surface") or ""
    if not replacement_target:
        raise TaskSessionError("cmux replacement surface identity is incomplete")
    for args in (
        ["cmux", "send", "--surface", replacement_target, "--workspace", workspace, "--window", window, "exit"],
        ["cmux", "send-key", "--surface", replacement_target, "--workspace", workspace, "--window", window, "Enter"],
    ):
        result = runner(args, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise TaskSessionError("cmux replacement shell could not be exited")
    for _attempt in range(8):
        if surface_context(replacement_target, runner, missing_ok=True) is None:
            return
        time.sleep(0.25)
    raise TaskSessionError("cmux replacement shell remained open")


def close_surface_exact(surface: str, runner: Any = subprocess.run) -> str:
    """Close one exact surface with its anchors and prove it left the cmux tree."""

    surface = require_token(surface, "surface")
    for _attempt in range(2):
        context = surface_context(surface, runner, missing_ok=True)
        if context is None:
            return "already-gone"
        target = context["surface_ref"] or context["surface"]
        workspace = context["workspace_ref"] or context["workspace"]
        window = context["window_ref"] or context["window"]
        if not target or not workspace or not window:
            raise TaskSessionError("cmux surface close context is incomplete")
        runner(
            [
                "cmux", "close-surface", "--surface", target,
                "--workspace", workspace, "--window", window,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if surface_context(surface, runner, missing_ok=True) is None:
            close_replacement_shell(context, runner)
            return "closed"
    raise TaskSessionError("cmux close-surface returned but the exact surface remained open")


def spawn_right(origin_surface: str, runner: Any = subprocess.run) -> dict[str, str]:
    origin_surface = require_token(origin_surface, "origin_surface")
    caps = cmux_capabilities(runner)
    if not caps["anchored_split"]:
        raise TaskSessionError("cmux lacks anchored new-split --surface support")
    result = runner(
        ["cmux", "--id-format", "both", "new-split", "right", "--surface", origin_surface, "--focus", "false"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        workspace = surface_workspace(origin_surface, runner)
        result = runner(
            [
                "cmux", "--id-format", "both", "new-split", "right",
                "--workspace", workspace, "--surface", origin_surface,
                "--focus", "false",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise TaskSessionError("anchored cmux split failed")
    surface, surface_ref = parse_surface(output)
    return {"surface": surface, "surface_ref": surface_ref, "origin_surface": origin_surface}


def capture_resume(surface: str, runtime: str, runner: Any = subprocess.run) -> dict[str, str]:
    if runtime not in RUNTIMES:
        raise TaskSessionError("runtime is invalid")
    surface = require_token(surface, "surface")
    result = runner(
        ["cmux", "surface", "resume", "get", "--json", "--surface", surface],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise TaskSessionError("cmux resume binding is unavailable")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise TaskSessionError("cmux resume binding is invalid JSON") from exc
    binding = payload.get("resume_binding") if isinstance(payload, dict) else None
    return validate_checkpoint(binding, runtime)


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
