"""Fail-closed hydration of an exact operation-owned provider checkpoint."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from .runtime_session_contracts import (
    IDENTIFIER,
    RuntimeSessionError,
    RuntimeSessionResult,
)


MAX_CHECKPOINT_EVIDENCE_BYTES = 262_144


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
