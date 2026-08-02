"""Persistent task, lane, operation, and provider-session registry."""

from __future__ import annotations

import hashlib
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from task_session_contracts import (
    LANE_STATES,
    OPERATION_STATES,
    RUNTIMES,
    SCHEMA_VERSION,
    TaskSessionError,
    UUID_RE,
    require_token,
    require_uuid,
)
from task_session_cmux_layout import validate_checkpoint
from task_session_store_io import (
    atomic_write,
    ensure_owner_only_dir,
    file_lock,
    lane_id_for,
    read_object,
    remove_owned_research_scratch,
    utc_now,
)


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
