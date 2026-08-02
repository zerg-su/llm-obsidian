"""Owner-scoped durable operation store with atomic replace and fcntl locks."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import math
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from .contracts import (
    AttentionReason,
    ContractError,
    EffectOutcome,
    OperationRecord,
    OperationSpec,
    OwnedResources,
    TransitionResult,
    operation_record_from_dict,
    to_dict,
)
from .state_machine import begin_effect, resolve_effect, transition


class StoreError(RuntimeError):
    pass


class OperationStore:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()

    def _owner_dir(self, owner_id: str) -> Path:
        from .contracts import _identifier

        _identifier(owner_id, "owner_id")
        return self.root / "owners" / owner_id

    def _ensure_owner_dir(self, owner_id: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        path = self._owner_dir(owner_id)
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
        return path

    def _operation_path(self, owner_id: str, operation_id: str) -> Path:
        from .contracts import _identifier

        _identifier(operation_id, "operation_id")
        directory = self._owner_dir(owner_id) / "operations"
        return directory / f"{operation_id}.json"

    @contextlib.contextmanager
    def locked(self, owner_id: str) -> Iterator[None]:
        lock_path = self._ensure_owner_dir(owner_id) / ".lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _write(path: Path, value: object) -> None:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        tmp = Path(raw)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.chmod(0o600)
            os.replace(tmp, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            tmp.unlink(missing_ok=True)

    def read(self, owner_id: str, operation_id: str) -> OperationRecord:
        path = self._operation_path(owner_id, operation_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ContractError("record must be an object")
            return operation_record_from_dict(value)
        except FileNotFoundError as exc:
            raise StoreError(f"unknown operation: {operation_id}") from exc
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise StoreError(f"invalid operation record {operation_id}: {exc}") from exc

    def list(self, owner_id: str) -> list[OperationRecord]:
        directory = self._owner_dir(owner_id) / "operations"
        if not directory.is_dir():
            return []
        return [self.read(owner_id, path.stem) for path in sorted(directory.glob("*.json"))]

    def create(self, spec: OperationSpec, *, lane_id: str, run_id: str) -> OperationRecord:
        record = OperationRecord(spec, "created", 0, lane_id, run_id, OwnedResources())
        with self.locked(spec.owner_id):
            records = self.list(spec.owner_id)
            for existing in records:
                if (
                    existing.run_id == run_id
                    and existing.spec.operation_id != spec.operation_id
                ):
                    raise StoreError("run identity already belongs to a different operation")
                if existing.spec.idempotency_key == spec.idempotency_key:
                    if (
                        existing.spec != spec
                        or existing.lane_id != lane_id
                        or existing.run_id != run_id
                    ):
                        raise StoreError("idempotency key already belongs to a different specification")
                    return existing
            path = self._operation_path(spec.owner_id, spec.operation_id)
            if path.exists():
                raise StoreError("operation id already exists")
            path.parent.mkdir(exist_ok=True)
            self._write(path, to_dict(record))
            return record

    def save(self, record: OperationRecord, *, expected_revision: int) -> None:
        with self.locked(record.spec.owner_id):
            self._save_locked(record, expected_revision=expected_revision)

    def _save_locked(self, record: OperationRecord, *, expected_revision: int) -> None:
        current = self.read(record.spec.owner_id, record.spec.operation_id)
        if current.revision != expected_revision:
            raise StoreError("stale operation writer")
        if (
            current.spec != record.spec
            or current.lane_id != record.lane_id
            or current.run_id != record.run_id
        ):
            raise StoreError("operation identity is immutable")
        self._write(
            self._operation_path(record.spec.owner_id, record.spec.operation_id),
            to_dict(record),
        )

    def transition(
        self,
        owner_id: str,
        operation_id: str,
        state: str,
        *,
        reason: AttentionReason | None = None,
    ) -> TransitionResult:
        with self.locked(owner_id):
            record = self.read(owner_id, operation_id)
            updated, result = transition(record, state, reason=reason)
            if result.changed:
                self._write(self._operation_path(owner_id, operation_id), to_dict(updated))
            return result

    def rearm_callback_timeout(
        self,
        owner_id: str,
        operation_id: str,
        *,
        deadline_at: float,
    ) -> OperationRecord:
        """Atomically restore one exact timed-out callback wait.

        The state and replacement deadline must be published together so a
        concurrent runtime worker cannot observe an awaiting callback with the
        already-expired deadline and immediately return it to attention.
        """

        if (
            not isinstance(deadline_at, (int, float))
            or isinstance(deadline_at, bool)
            or not math.isfinite(float(deadline_at))
            or deadline_at <= 0
        ):
            raise StoreError("callback timeout rearm deadline is invalid")
        with self.locked(owner_id):
            record = self.read(owner_id, operation_id)
            if (
                record.state != "attention-required"
                or record.attention_reason != AttentionReason.CALLBACK_TIMEOUT
                or not record.deadline_at
                or deadline_at <= record.deadline_at
            ):
                raise StoreError("operation is not an exact expired callback wait")
            updated, _result = transition(record, "awaiting-callback")
            updated = replace(updated, deadline_at=float(deadline_at))
            self._write(
                self._operation_path(owner_id, operation_id),
                to_dict(updated),
            )
            return updated

    def begin_effect(self, owner_id: str, operation_id: str, effect: str) -> OperationRecord:
        with self.locked(owner_id):
            record = self.read(owner_id, operation_id)
            updated = begin_effect(record, effect)
            if updated is not record:
                self._write(self._operation_path(owner_id, operation_id), to_dict(updated))
            return updated

    def resolve_effect(
        self,
        owner_id: str,
        operation_id: str,
        outcome: EffectOutcome,
    ) -> OperationRecord:
        with self.locked(owner_id):
            record = self.read(owner_id, operation_id)
            updated = resolve_effect(record, outcome)
            if updated is not record:
                self._write(self._operation_path(owner_id, operation_id), to_dict(updated))
            return updated
