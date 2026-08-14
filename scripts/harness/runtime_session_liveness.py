"""Time-last resource observation and idempotent durable close receipts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .provider_events import (
    IDENTIFIER,
    SHA256,
    ProviderEvent,
    ProviderEventCursor,
    ProviderEventIdentity,
)


class ResourceCloseError(ValueError):
    """Owned resource closure is unproven, ambiguous, or rebound."""


def _identifier(value: str, label: str, *, optional: bool = False) -> None:
    if optional and value == "":
        return
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ResourceCloseError(f"{label} must be a bounded identifier")


@dataclass(frozen=True)
class ResourceIdentity:
    owner_id: str
    operation_id: str
    run_id: str
    generation: int
    provider_session_id: str
    process_identity: str
    supervisor_identity: str
    source_id: str
    workspace_id: str = ""
    surface_id: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ResourceCloseError("resource identity schema is invalid")
        for value, label in (
            (self.owner_id, "owner_id"),
            (self.operation_id, "operation_id"),
            (self.run_id, "run_id"),
            (self.provider_session_id, "provider_session_id"),
            (self.source_id, "source_id"),
        ):
            _identifier(value, label)
        if type(self.generation) is not int or self.generation < 1:
            raise ResourceCloseError("resource generation is invalid")
        for value, label in (
            (self.process_identity, "process_identity"),
            (self.supervisor_identity, "supervisor_identity"),
        ):
            if not isinstance(value, str) or not SHA256.fullmatch(value):
                raise ResourceCloseError(f"{label} must be a lowercase sha256")
        _identifier(self.workspace_id, "workspace_id", optional=True)
        _identifier(self.surface_id, "surface_id", optional=True)
        if bool(self.workspace_id) != bool(self.surface_id):
            raise ResourceCloseError(
                "workspace and surface identities must be both present or absent"
            )

    def provider_identity(self) -> ProviderEventIdentity:
        return ProviderEventIdentity(
            owner_id=self.owner_id,
            operation_id=self.operation_id,
            run_id=self.run_id,
            generation=self.generation,
            provider_session_id=self.provider_session_id,
            process_identity=self.process_identity,
            source_id=self.source_id,
            workspace_id=self.workspace_id,
            surface_id=self.surface_id,
        )


@dataclass(frozen=True)
class ResourceObservation:
    process_status: str
    supervisor_status: str
    surface_status: str
    deadline_reached: bool = False
    screen_changed: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or self.process_status not in {"alive", "dead", "unknown"}
            or self.supervisor_status not in {"alive", "dead", "unknown"}
            or self.surface_status not in {"alive", "missing", "unknown"}
            or not isinstance(self.deadline_reached, bool)
            or not isinstance(self.screen_changed, bool)
        ):
            raise ResourceCloseError("resource observation is invalid")


@dataclass(frozen=True)
class ResourceCloseDecision:
    action: str
    reason: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.action not in {
            "recheck",
            "attention",
            "close",
        }:
            raise ResourceCloseError("resource close decision is invalid")
        _identifier(self.reason, "resource close reason")


def observe_resource_liveness(
    observation: ResourceObservation,
) -> ResourceCloseDecision:
    """Prove closure from exact task resources; workspace is provenance only."""

    if not isinstance(observation, ResourceObservation):
        raise ResourceCloseError("resource observation is invalid")
    owned_statuses = (
        observation.process_status,
        observation.supervisor_status,
        observation.surface_status,
    )
    if "unknown" in owned_statuses:
        return ResourceCloseDecision("attention", "ownership-unknown")
    if owned_statuses == ("dead", "dead", "missing"):
        return ResourceCloseDecision("close", "owned-resources-gone")
    if observation.deadline_reached:
        return ResourceCloseDecision("attention", "deadline-reached")
    # screen_changed intentionally cannot select a different positive action.
    return ResourceCloseDecision("recheck", "resources-still-observable")


@dataclass(frozen=True)
class ResourceClosedReceipt:
    identity: ResourceIdentity
    close_id: str
    status: str = "resource-closed"
    schema_version: int = 1

    def __post_init__(self) -> None:
        expected = hashlib.sha256(
            json.dumps(
                asdict(self.identity),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if (
            self.schema_version != 1
            or not isinstance(self.identity, ResourceIdentity)
            or self.close_id != expected
            or self.status != "resource-closed"
        ):
            raise ResourceCloseError("resource close receipt is invalid")

    @classmethod
    def create(cls, identity: ResourceIdentity) -> "ResourceClosedReceipt":
        close_id = hashlib.sha256(
            json.dumps(
                asdict(identity), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        return cls(identity, close_id)


@dataclass(frozen=True)
class ResourceCloseResult:
    receipt: ResourceClosedReceipt
    created: bool


def _receipt_from_dict(value: object) -> ResourceClosedReceipt:
    if not isinstance(value, dict) or set(value) != {
        item.name for item in fields(ResourceClosedReceipt)
    }:
        raise ResourceCloseError("durable resource close receipt is invalid")
    identity = value.get("identity")
    if not isinstance(identity, dict):
        raise ResourceCloseError("durable resource identity is invalid")
    try:
        return ResourceClosedReceipt(
            **{**value, "identity": ResourceIdentity(**identity)}
        )
    except (TypeError, ResourceCloseError) as exc:
        raise ResourceCloseError("durable resource close receipt is invalid") from exc


class ResourceClosureLedger:
    """Publish exactly one owner-bound close receipt with durable replacement."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def _locked(self):
        root_existed = self.root.exists()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        if not root_existed:
            self._fsync_directory(self.root.parent)
        lock_path = self.root / ".resource-close.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.chmod(lock_path, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read(self, path: Path) -> ResourceClosedReceipt:
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise ResourceCloseError("resource close receipt is not owner-only")
        try:
            raw = path.read_bytes()
            if not raw or len(raw) > 65_536:
                raise ValueError
            return _receipt_from_dict(json.loads(raw))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ResourceCloseError("resource close receipt is invalid") from exc

    def _write(self, path: Path, receipt: ResourceClosedReceipt) -> None:
        encoded = (
            json.dumps(asdict(receipt), sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        descriptor, raw = tempfile.mkstemp(
            prefix=".resource-closed.", dir=self.root
        )
        temporary = Path(raw)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, path)
            self._fsync_directory(self.root)
        finally:
            temporary.unlink(missing_ok=True)

    def close(
        self,
        identity: ResourceIdentity,
        observation: ResourceObservation,
    ) -> ResourceCloseResult:
        if observe_resource_liveness(observation).action != "close":
            raise ResourceCloseError("resource closure is not proven")
        receipt = ResourceClosedReceipt.create(identity)
        path = self.root / "resource-closed.json"
        with self._locked():
            if path.exists() or path.is_symlink():
                existing = self._read(path)
                if existing != receipt:
                    raise ResourceCloseError("resource close receipt identity changed")
                return ResourceCloseResult(existing, False)
            self._write(path, receipt)
            return ResourceCloseResult(receipt, True)


def resource_closed_event(
    cursor: ProviderEventCursor,
    receipt: ResourceClosedReceipt,
) -> ProviderEvent:
    """Map the durable receipt to the next exact ProviderEvent cursor value."""

    if cursor.identity != receipt.identity.provider_identity():
        raise ResourceCloseError("resource close event identity changed")
    return ProviderEvent(
        "resource-closed",
        cursor.identity,
        cursor.last_sequence + 1,
        reason="owned-resources-gone",
    )
