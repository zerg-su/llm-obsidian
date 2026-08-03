"""Fail-closed hydration of an exact operation-owned provider checkpoint."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runtime_session_contracts import (
    IDENTIFIER,
    RuntimeSessionError,
    RuntimeSessionResult,
)


MAX_CHECKPOINT_EVIDENCE_BYTES = 262_144


@dataclass(frozen=True)
class DurableCleanupOwnership:
    """Fresh exact-resource observations backed by immutable launch evidence."""

    process_status: str
    supervisor_status: str
    surface_status: str
    workspace_status: str
    workspace_id: str
    window_id: str


def _stable_owned_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    if path.is_symlink():
        raise RuntimeSessionError(f"durable {label} must not be a symlink")
    try:
        before = path.stat(follow_symlinks=False)
        raw = path.read_bytes()
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RuntimeSessionError(f"durable {label} is unavailable") from exc
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
    )
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_mode & 0o022
        or identity(before) != identity(after)
        or not raw
        or len(raw) > MAX_CHECKPOINT_EVIDENCE_BYTES
    ):
        raise RuntimeSessionError(f"durable {label} identity is invalid")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeSessionError(f"durable {label} is invalid JSON") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeSessionError(f"durable {label} schema is invalid")
    return value, hashlib.sha256(raw).hexdigest()


class RuntimeSessionCheckpointMixin:
    """Recover only an immutable checkpoint belonging to one exact parent."""

    def hydrate_durable_checkpoint(
        self,
        owner_id: str,
        operation_id: str,
        lane_id: str,
    ) -> RuntimeSessionResult:
        record = self.store.read(owner_id, operation_id)
        if (
            record.spec.owner_id != owner_id
            or record.spec.operation_id != operation_id
            or record.lane_id != lane_id
            or not IDENTIFIER.fullmatch(record.run_id)
        ):
            raise RuntimeSessionError(
                "durable checkpoint parent identity changed"
            )
        state_root = self._state_root(record)
        expected_root = (
            self.store.root.resolve()
            / "owners"
            / owner_id
            / "runtime"
            / operation_id
        )
        if (
            state_root != expected_root
            or not state_root.is_dir()
            or state_root.is_symlink()
            or any(
                parent.is_symlink()
                for parent in (
                    expected_root.parent,
                    expected_root.parent.parent,
                    expected_root.parent.parent.parent,
                )
            )
        ):
            raise RuntimeSessionError(
                "durable checkpoint root identity is invalid"
            )
        session, session_sha256 = _stable_owned_json(
            state_root / "session.json", "session evidence"
        )
        checkpoint, checkpoint_sha256 = _stable_owned_json(
            state_root / "checkpoint.json", "checkpoint evidence"
        )
        launch, launch_sha256 = _stable_owned_json(
            state_root / "launch.json", "launch evidence"
        )
        route = record.spec.route
        if (
            session.get("operation_id") != operation_id
            or session.get("run_id") != record.run_id
            or session.get("callback_mode", "envelope") != "envelope"
            or str(session.get("checkpoint") or "")
            not in {"", str(checkpoint.get("checkpoint") or "")}
            or set(checkpoint)
            != {
                "schema_version",
                "operation_id",
                "run_id",
                "runtime",
                "checkpoint",
            }
            or checkpoint.get("operation_id") != operation_id
            or checkpoint.get("run_id") != record.run_id
            or checkpoint.get("runtime") != route.runtime
            or launch.get("owner_id") != owner_id
            or launch.get("operation_id") != operation_id
            or launch.get("run_id") != record.run_id
            or launch.get("runtime") != route.runtime
            or launch.get("surface_id") != record.resources.surface_id
        ):
            raise RuntimeSessionError(
                "durable checkpoint session identity changed"
            )
        checkpoint_value = str(checkpoint.get("checkpoint") or "")
        argv = launch.get("argv")
        model_positions = (
            [index for index, value in enumerate(argv) if value == "--model"]
            if isinstance(argv, list)
            else []
        )
        if (
            not IDENTIFIER.fullmatch(checkpoint_value)
            or len(model_positions) != 1
            or model_positions[0] + 1 >= len(argv)
            or argv[model_positions[0] + 1] != route.model
            or not route.routing_sha256
        ):
            raise RuntimeSessionError(
                "durable checkpoint provider route changed"
            )
        binding = {
            "owner_id": owner_id,
            "operation_id": operation_id,
            "run_id": record.run_id,
            "lane_id": lane_id,
            "surface_id": record.resources.surface_id,
            "runtime": route.runtime,
            "model": route.model,
            "routing_sha256": route.routing_sha256,
            "session_sha256": session_sha256,
            "checkpoint_sha256": checkpoint_sha256,
            "launch_sha256": launch_sha256,
            "checkpoint": checkpoint_value,
        }
        binding_sha256 = hashlib.sha256(
            json.dumps(
                binding, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        return RuntimeSessionResult(
            record,
            "checkpoint-hydrated",
            checkpoint=checkpoint_value,
            checkpoint_sha256=binding_sha256,
        )

    def prove_durable_cleanup_ownership(
        self, owner_id: str, operation_id: str
    ) -> DurableCleanupOwnership:
        """Reconstruct one reviewer parent without weakening unknown ownership."""

        record = self.store.read(owner_id, operation_id)
        if (
            record.spec.owner_id != owner_id
            or record.spec.operation_id != operation_id
            or record.spec.route.profile != "reviewer-callback"
        ):
            raise RuntimeSessionError(
                "durable cleanup parent identity is invalid"
            )
        resources = record.resources
        if (
            not resources.surface_id
            or resources.process_group <= 1
            or resources.supervisor_pid <= 1
            or not resources.process_identity
            or not resources.supervisor_identity
        ):
            raise RuntimeSessionError(
                "durable cleanup resources are incomplete"
            )
        self.hydrate_durable_checkpoint(
            owner_id, operation_id, record.lane_id
        )
        state_root = self._state_root(record)
        session, _session_sha256 = _stable_owned_json(
            state_root / "session.json", "cleanup session evidence"
        )
        launch, _launch_sha256 = _stable_owned_json(
            state_root / "launch.json", "cleanup launch evidence"
        )
        ready, _ready_sha256 = _stable_owned_json(
            state_root / "ready.json", "cleanup ready evidence"
        )
        raw_cwd = Path(str(session.get("cwd") or "")).expanduser()
        raw_product_root = Path(
            str(session.get("product_root") or "")
        ).expanduser()
        if (
            not raw_cwd.is_absolute()
            or not raw_product_root.is_absolute()
            or raw_cwd.is_symlink()
            or raw_product_root.is_symlink()
        ):
            raise RuntimeSessionError(
                "durable cleanup worktree identity is invalid"
            )
        cwd = raw_cwd.resolve()
        product_root = raw_product_root.resolve()
        workspace_id = str(session.get("workspace_id") or "")
        window_id = str(session.get("window_id") or "")
        argv = launch.get("argv")
        if (
            session.get("operation_id") != operation_id
            or session.get("run_id") != record.run_id
            or session.get("placement") != "workspace"
            or session.get("callback_mode", "envelope") != "envelope"
            or not cwd.is_dir()
            or not product_root.is_dir()
            or product_root == cwd
            or product_root in cwd.parents
            or cwd in product_root.parents
            or not IDENTIFIER.fullmatch(workspace_id)
            or not IDENTIFIER.fullmatch(window_id)
            or not IDENTIFIER.fullmatch(
                str(session.get("workspace_ref") or "")
            )
            or not IDENTIFIER.fullmatch(
                str(session.get("window_ref") or "")
            )
            or not IDENTIFIER.fullmatch(
                str(session.get("surface_ref") or "")
            )
            or launch.get("owner_id") != owner_id
            or launch.get("operation_id") != operation_id
            or launch.get("run_id") != record.run_id
            or launch.get("runtime") != record.spec.route.runtime
            or launch.get("surface_id") != resources.surface_id
            or launch.get("cwd") != str(cwd)
            or launch.get("store_root") != str(self.store.root.resolve())
            or not isinstance(argv, list)
            or not all(isinstance(item, str) for item in argv)
            or not any(str(product_root) in item for item in argv)
        ):
            raise RuntimeSessionError(
                "durable cleanup launch identity changed"
            )
        if (
            set(ready)
            != {
                "schema_version",
                "status",
                "pid",
                "process_group",
                "process_identity",
                "supervisor_pid",
                "supervisor_identity",
            }
            or ready.get("status") != "ready"
            or ready.get("pid") != resources.process_group
            or ready.get("process_group") != resources.process_group
            or ready.get("process_identity")
            != resources.process_identity
            or ready.get("supervisor_pid") != resources.supervisor_pid
            or ready.get("supervisor_identity")
            != resources.supervisor_identity
            or resources.process_group == resources.supervisor_pid
        ):
            raise RuntimeSessionError(
                "durable cleanup supervisor relationship changed"
            )

        process_status = self._exact_cleanup_process_status(
            self.process.process_status(
                resources.process_group, resources.process_identity
            ),
            resources.process_group,
            resources.process_identity,
            process_group=resources.process_group,
        )
        supervisor_status = self._exact_cleanup_process_status(
            self._supervisor_status(record),
            resources.supervisor_pid,
            resources.supervisor_identity,
        )
        try:
            surface_status = str(self.cmux.status(resources.surface_id))
            workspace_status = str(
                self.cmux.workspace_status(workspace_id, window_id)
            )
        except Exception as exc:
            raise RuntimeSessionError(
                "durable cleanup cmux ownership is unavailable"
            ) from exc
        if (
            surface_status not in {"alive", "missing"}
            or workspace_status not in {"alive", "missing"}
            or surface_status != workspace_status
        ):
            raise RuntimeSessionError(
                "durable cleanup cmux ownership changed"
            )
        return DurableCleanupOwnership(
            process_status,
            supervisor_status,
            surface_status,
            workspace_status,
            workspace_id,
            window_id,
        )

    def _exact_cleanup_process_status(
        self,
        status: object,
        pid: int,
        identity: str,
        *,
        process_group: int = 0,
    ) -> str:
        value = str(status)
        if value not in {"alive", "dead", "unknown"}:
            raise RuntimeSessionError(
                "durable cleanup process status is invalid"
            )
        capture = getattr(self.process, "capture_identity", None)
        if not callable(capture):
            raise RuntimeSessionError(
                "durable cleanup process identity probe is unavailable"
            )
        if value == "dead":
            try:
                capture(pid, process_group=process_group)
            except (ProcessLookupError, OSError):
                return "dead"
            raise RuntimeSessionError(
                "durable cleanup process identity was reused"
            )
        try:
            actual = capture(pid, process_group=process_group)
        except Exception as exc:
            raise RuntimeSessionError(
                "durable cleanup process identity is unavailable"
            ) from exc
        if actual != identity:
            raise RuntimeSessionError(
                "durable cleanup process identity changed"
            )
        return "alive"
