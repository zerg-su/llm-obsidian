"""Semantic, content-free delivery for retained-session notifications."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Mapping, Protocol

from .runtime_session_continuation import (
    _prompt_anchor,
    await_initial_input_visible,
    classify_continuation_screen,
)


SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class RetainedNotificationError(RuntimeError):
    pass


class NotificationPort(Protocol):
    def read(self, surface_id: str) -> str: ...
    def send(self, surface_id: str, text: str) -> None: ...
    def send_key(self, surface_id: str, key: str) -> None: ...


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _identity_sha256(identity: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            dict(identity), sort_keys=True, separators=(",", ":")
        ).encode()
    except (TypeError, ValueError) as exc:
        raise RetainedNotificationError(
            "notification recovery identity is invalid"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def send_visible_notification(
    port: NotificationPort,
    *,
    surface_id: str,
    runtime: str,
    message: str,
    observation_limit: int = 40,
    observation_interval_seconds: float = 0.05,
    wait: Callable[[float], None] | None = None,
) -> None:
    """Paste once, prove editor visibility, then submit exactly one Enter."""

    port.send(surface_id, message)
    kwargs: dict[str, object] = {}
    if wait is not None:
        kwargs["wait"] = wait
    visible = await_initial_input_visible(
        port,
        surface_id=surface_id,
        runtime=runtime,
        text=message,
        observation_limit=observation_limit,
        observation_interval_seconds=observation_interval_seconds,
        **kwargs,
    )
    if not visible:
        raise RetainedNotificationError(
            "retained notification transport is not visible"
        )
    port.send_key(surface_id, "Enter")


def recover_visible_notification(
    port: NotificationPort,
    *,
    surface_id: str,
    workspace_id: str,
    runtime: str,
    message: str,
    receipt_path: Path,
    identity: Mapping[str, object],
) -> bool:
    """Submit an already-visible notification once, never re-paste it.

    This is the narrow recovery for the historical ``sent`` marker race: the
    marker can be durable even when cmux accepted ``Enter`` before the provider
    editor accepted the paste.  Exact editor visibility plus cmux semantic idle
    state proves that one submit is still pending.
    """

    digest = _identity_sha256(identity)
    expected = {
        "schema_version": 1,
        "identity": dict(identity),
        "identity_sha256": digest,
    }
    if receipt_path.is_symlink():
        raise RetainedNotificationError(
            "notification recovery receipt cannot be a symlink"
        )
    if receipt_path.is_file():
        try:
            current = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RetainedNotificationError(
                "notification recovery receipt is invalid"
            ) from exc
        if not isinstance(current, dict) or any(
            current.get(key) != value for key, value in expected.items()
        ):
            raise RetainedNotificationError(
                "notification recovery identity changed"
            )
        if current.get("status") == "accepted":
            return True
        raise RetainedNotificationError(
            "notification submit effect is uncertain"
        )

    status_probe = getattr(port, "agent_status", None)
    if status_probe is None:
        return False
    try:
        status = str(status_probe(workspace_id, runtime))
        screen = port.read(surface_id)
    except Exception:
        return False
    anchor = _prompt_anchor(message)
    if status not in {"idle", "needs-input"} or (
        classify_continuation_screen(runtime, screen, anchor) != "input-ready"
    ):
        return False
    _atomic_json(receipt_path, {**expected, "status": "reserved"})
    port.send_key(surface_id, "Enter")
    _atomic_json(receipt_path, {**expected, "status": "accepted"})
    return True


def deliver_worker_notification(
    worker: object,
    *,
    notify_path: Path,
    marker: Mapping[str, object],
    message: str,
) -> None:
    """Deliver or recover one worker-owned retained-session notification."""

    spec = getattr(worker, "spec", None)
    port = getattr(worker, "cmux_adapter", None)
    if not isinstance(spec, dict) or port is None:
        raise RetainedNotificationError("notification worker is incomplete")
    existing: object | None = None
    if notify_path.is_symlink():
        raise RetainedNotificationError("notification marker cannot be a symlink")
    if notify_path.is_file():
        try:
            existing = json.loads(notify_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RetainedNotificationError(
                "notification marker is invalid"
            ) from exc
        if existing != dict(marker):
            raise RetainedNotificationError("notification marker changed")
        workspace_probe = getattr(worker, "_workspace_id", None)
        if workspace_probe is None:
            return
        try:
            workspace_id = str(workspace_probe())
        except Exception:
            return
        identity = {
            "operation_id": str(marker.get("operation_id") or ""),
            "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
            "notification": notify_path.name,
        }
        recover_visible_notification(
            port,
            surface_id=str(spec["surface_id"]),
            workspace_id=workspace_id,
            runtime=str(spec["runtime"]),
            message=message,
            receipt_path=notify_path.with_name(
                f"{notify_path.stem}-submit-recovery.json"
            ),
            identity=identity,
        )
        return
    writer = getattr(worker, "write_immutable_json", None)
    if writer is None:
        raise RetainedNotificationError("notification worker writer is unavailable")
    send_visible_notification(
        port,
        surface_id=str(spec["surface_id"]),
        runtime=str(spec["runtime"]),
        message=message,
    )
    writer(notify_path, dict(marker))
