"""Semantic, content-free delivery for retained-session notifications."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Mapping, Protocol

from .prompts import classify
from .runtime_session_continuation import (
    _editor_digest,
    _editor_state,
    _prompt_anchor,
    await_initial_input_visible,
    deliver_continuation,
)


DELIVERY_STAGES = frozenset(
    {"paste-reserved", "transport-accepted", "submit-reserved", "submit-accepted"}
)
_PROCESS_NOTIFICATION_LOCK = threading.Lock()


class RetainedNotificationError(RuntimeError):
    pass


class _NotificationSubmitted(RuntimeError):
    """Internal control flow after the durable submit acceptance is published."""


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


@contextmanager
def _receipt_lock(receipt_path: Path) -> Iterator[None]:
    """Serialize one notification effect across threads and worker processes."""

    receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = receipt_path.with_name(f".{receipt_path.name}.lock")
    if lock_path.is_symlink():
        raise RetainedNotificationError("notification receipt lock is invalid")
    with _PROCESS_NOTIFICATION_LOCK:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _read_receipt(
    path: Path, expected: Mapping[str, object]
) -> dict[str, object] | None:
    if path.is_symlink():
        raise RetainedNotificationError(
            "notification recovery receipt cannot be a symlink"
        )
    if not path.is_file():
        return None
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RetainedNotificationError("notification recovery receipt is invalid") from exc
    if not isinstance(current, dict) or any(
        current.get(key) != value for key, value in expected.items()
    ):
        raise RetainedNotificationError("notification recovery identity changed")
    return current


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

    before_screen = port.read(surface_id)
    port.send(surface_id, message)
    kwargs: dict[str, object] = {}
    if wait is not None:
        kwargs["wait"] = wait
    visible = await_initial_input_visible(
        port,
        surface_id=surface_id,
        runtime=runtime,
        text=message,
        before_editor_sha256=_editor_digest(runtime, before_screen),
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
    with _receipt_lock(receipt_path):
        current = _read_receipt(receipt_path, expected)
        if current is not None:
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
        anchor, editor_lines = _prompt_anchor(message), _editor_state(runtime, screen)
        exact_editor = bool(editor_lines and anchor and anchor in editor_lines[-1])
        if classify(runtime, screen).interactive or status not in {"idle", "needs-input"} or not exact_editor:
            return False
        _atomic_json(receipt_path, {**expected, "status": "reserved"})
        port.send_key(surface_id, "Enter")
        _atomic_json(receipt_path, {**expected, "status": "accepted"})
        return True


def _deliver_new_notification(
    port: NotificationPort,
    *,
    surface_id: str,
    runtime: str,
    message: str,
    receipt_path: Path,
    identity: Mapping[str, object],
) -> None:
    """Run one write-ahead notification delivery under its effect lock."""

    digest = _identity_sha256(identity)
    expected = {
        "schema_version": 1,
        "identity": dict(identity),
        "identity_sha256": digest,
    }
    with _receipt_lock(receipt_path):
        current = _read_receipt(receipt_path, expected)
        stage = str(current.get("stage") or "") if current is not None else ""
        if stage and stage not in DELIVERY_STAGES:
            raise RetainedNotificationError("notification delivery stage is invalid")
        if stage == "paste-reserved" or stage == "submit-reserved":
            raise RetainedNotificationError(
                "notification delivery effect is uncertain"
            )
        if stage == "submit-accepted":
            return

        def observe_stage(
            next_stage: str,
            submit_count: int,
            pre_screen_sha256: str,
            pre_editor_sha256: str,
            paste_screen_sha256: str,
        ) -> None:
            if next_stage not in DELIVERY_STAGES:
                return
            _atomic_json(
                receipt_path,
                {
                    **expected,
                    "stage": next_stage,
                    "submit_count": submit_count,
                    "pre_screen_sha256": pre_screen_sha256,
                    "pre_editor_sha256": pre_editor_sha256,
                    "paste_screen_sha256": paste_screen_sha256,
                },
            )
            if next_stage == "submit-accepted":
                raise _NotificationSubmitted

        try:
            result = deliver_continuation(
                port,
                surface_id=surface_id,
                prompt=message,
                runtime=runtime,
                artifact_ready=lambda: False,
                ownership_ready=lambda: True,
                reserve_retry=lambda: False,
                observe_stage=observe_stage,
                send_prompt=current is None,
                pre_send_screen_sha256=(
                    str(current.get("pre_screen_sha256") or "") if current else ""
                ),
                pre_send_editor_sha256=(
                    str(current.get("pre_editor_sha256") or "") if current else ""
                ),
                observation_limit=40,
                observation_interval_seconds=0.05,
            )
        except _NotificationSubmitted:
            return
        settled = _read_receipt(receipt_path, expected)
        if settled is not None and settled.get("stage") == "submit-accepted":
            return
        raise RetainedNotificationError(
            f"retained notification delivery is incomplete: {result.evidence}"
        )


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
    identity = {
        "operation_id": str(marker.get("operation_id") or ""),
        "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
        "notification": notify_path.name,
    }
    delivery_path = notify_path.with_name(f"{notify_path.stem}-delivery.json")
    _deliver_new_notification(
        port,
        surface_id=str(spec["surface_id"]),
        runtime=str(spec["runtime"]),
        message=message,
        receipt_path=delivery_path,
        identity=identity,
    )
    writer(notify_path, dict(marker))
