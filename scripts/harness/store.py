"""Owner-scoped durable operation store with atomic replace and fcntl locks."""

from __future__ import annotations

MODEL_JSON_BOUNDARIES = ("operation-store",)

import contextlib
import fcntl
import json
import os
import tempfile
import math
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterator

from .contracts import (
    AttentionReason,
    CallbackEnvelope,
    ContractError,
    EffectOutcome,
    OperationRecord,
    OperationSpec,
    OwnedResources,
    TransitionResult,
    operation_record_from_dict,
    to_dict,
)
from .state_machine import TERMINAL, begin_effect, resolve_effect, transition


class StoreError(RuntimeError):
    pass


class OperationStore:
    def __init__(
        self,
        root: Path | str,
        *,
        fault_observer: Callable[[str], None] | None = None,
    ):
        self.root = Path(root).expanduser().resolve()
        self._fault_observer = fault_observer

    def _observe_durable_boundary(
        self, boundary: str, *, phase: str = "after"
    ) -> None:
        if self._fault_observer is not None:
            self._fault_observer(
                boundary if phase == "after" else f"{boundary}:before"
            )

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
            if spec.root_operation_id:
                if spec.parent_operation_id:
                    parent = next(
                        (
                            item
                            for item in records
                            if item.spec.operation_id == spec.parent_operation_id
                        ),
                        None,
                    )
                    if parent is None and not (
                        spec.parent_operation_id
                        == spec.root_operation_id
                        == spec.owner_id
                    ):
                        raise StoreError("operation parent lineage is unavailable")
                    parent_root = (
                        parent.spec.root_operation_id or parent.spec.operation_id
                        if parent is not None
                        else spec.root_operation_id
                    )
                    if parent is not None and (
                        parent.spec.owner_id != spec.owner_id
                        or parent_root != spec.root_operation_id
                    ):
                        raise StoreError("operation root lineage is foreign")
                elif spec.root_operation_id != spec.operation_id:
                    raise StoreError("root operation lineage is inconsistent")
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
        self._observe_durable_boundary("operation-record-published", phase="before")
        self._write(
            self._operation_path(record.spec.owner_id, record.spec.operation_id),
            to_dict(record),
        )
        self._observe_durable_boundary("operation-record-published")

    def accept_callback(
        self,
        owner_id: str,
        envelope: CallbackEnvelope,
        *,
        expected_revision: int,
        next_state: str,
        reason: AttentionReason | None,
        deadline_operation_id: str = "",
        enforce_deadline: bool = False,
        now: float,
    ) -> tuple[OperationRecord, bool, bool]:
        """Atomically publish one callback transition and immutable identity.

        Returns ``(record, accepted, timed_out)``. An exact duplicate is the
        only stale expected revision accepted as an idempotent no-op.
        """

        with self.locked(owner_id):
            record = self.read(owner_id, envelope.operation_id)
            if envelope.operation_id != record.spec.operation_id:
                raise StoreError("callback belongs to a different operation")
            if envelope.run_id != record.run_id:
                raise StoreError("callback belongs to a different run")
            exact_duplicate = (
                record.accepted_callback_id == envelope.callback_id
                and record.accepted_callback_kind == envelope.kind
                and record.accepted_callback_sha256 == envelope.payload_sha256
            )
            if record.revision != expected_revision:
                if exact_duplicate:
                    return record, False, False
                raise StoreError("stale callback writer")
            if record.accepted_callback_id:
                if not exact_duplicate:
                    raise StoreError(
                        "operation already accepted a different callback"
                    )
                return record, False, False
            if record.state in TERMINAL:
                raise StoreError("terminal operation cannot accept a callback")

            deadline_record = record
            if deadline_operation_id and envelope.kind == "review":
                deadline_record = self.read(owner_id, deadline_operation_id)
                if (
                    deadline_record.lane_id != record.lane_id
                    or (
                        deadline_operation_id != envelope.operation_id
                        and envelope.payload.get("parent_session_operation_id")
                        != deadline_operation_id
                    )
                ):
                    raise StoreError(
                        "callback deadline owner mismatches its parent lane"
                    )
            if enforce_deadline and deadline_record.state not in TERMINAL:
                timed_out = (
                    deadline_record.state == "attention-required"
                    and deadline_record.attention_reason
                    == AttentionReason.CALLBACK_TIMEOUT
                )
                if (
                    not timed_out
                    and deadline_record.deadline_at
                    and now >= deadline_record.deadline_at
                ):
                    timed_out_record, _ = transition(
                        deadline_record,
                        "attention-required",
                        reason=AttentionReason.CALLBACK_TIMEOUT,
                    )
                    self._save_locked(
                        timed_out_record,
                        expected_revision=deadline_record.revision,
                    )
                    timed_out = True
                if timed_out:
                    return record, False, True
            if record.state != "awaiting-callback":
                raise StoreError("operation is not awaiting a callback")

            updated, _ = transition(record, next_state, reason=reason)
            updated = replace(
                updated,
                accepted_callback_id=envelope.callback_id,
                accepted_callback_kind=envelope.kind,
                accepted_callback_sha256=envelope.payload_sha256,
            )
            self._save_locked(updated, expected_revision=record.revision)
            return updated, True, False

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
                self._observe_durable_boundary(
                    "operation-transition-published", phase="before"
                )
                self._write(self._operation_path(owner_id, operation_id), to_dict(updated))
                self._observe_durable_boundary("operation-transition-published")
            return result

    def rearm_callback_timeout(
        self,
        owner_id: str,
        operation_id: str,
        *,
        deadline_at: float,
    ) -> OperationRecord:
        # Publish state and deadline together: the runtime worker must never
        # observe an awaiting callback with its already-expired deadline.
        invalid_deadline = (
            not isinstance(deadline_at, (int, float))
            or isinstance(deadline_at, bool)
            or not math.isfinite(float(deadline_at))
            or deadline_at <= 0
        )
        if invalid_deadline:
            raise StoreError("callback timeout rearm deadline is invalid")
        with self.locked(owner_id):
            record = self.read(owner_id, operation_id)
            invalid_timeout = (
                record.state != "attention-required"
                or record.attention_reason != AttentionReason.CALLBACK_TIMEOUT
                or not record.deadline_at
                or deadline_at <= record.deadline_at
            )
            if invalid_timeout:
                raise StoreError("operation is not an exact expired callback wait")
            updated, _result = transition(record, "awaiting-callback")
            updated = replace(updated, deadline_at=float(deadline_at))
            self._write(
                self._operation_path(owner_id, operation_id),
                to_dict(updated),
            )
            return updated

    def recover_late_started_review_round(
        self,
        owner_id: str,
        parent_operation_id: str,
        child_operation_id: str,
        resources: OwnedResources,
    ) -> tuple[OperationRecord, OperationRecord]:
        """Adopt one exact late process handshake and reopen its false-failed round.

        This is deliberately narrower than a terminal-state retry.  The parent
        must still carry the unresolved ``start-provider`` effect and the child
        must be the untouched review round failed by start_review containment.
        No provider or callback effect is performed here.
        """

        with self.locked(owner_id):
            parent = self.read(owner_id, parent_operation_id)
            child = self.read(owner_id, child_operation_id)
            already_recovered = (
                parent.state == "awaiting-callback"
                and not parent.pending_effect
                and parent.effect_id == "start-provider"
                and parent.effect_outcome == EffectOutcome.SUCCEEDED
                and parent.resources == resources
                and child.state == "awaiting-callback"
                and not child.accepted_callback_id
            )
            if already_recovered:
                return parent, child
            if (
                parent.spec.owner_id != owner_id
                or parent.spec.operation_id != parent_operation_id
                or parent.spec.route.profile != "reviewer-callback"
                or parent.state != "attention-required"
                or parent.attention_reason != AttentionReason.PROCESS_START_FAILED
                or parent.resume_state != "starting"
                or parent.pending_effect != "start-provider"
                or parent.effect_id != "start-provider"
                or parent.effect_outcome != EffectOutcome.PENDING
                or parent.resources != OwnedResources(surface_id=resources.surface_id)
                or resources.process_group <= 1
                or resources.supervisor_pid <= 1
                or child.spec.owner_id != owner_id
                or child.spec.operation_id != child_operation_id
                or child.spec.kind != "review-round"
                or child.spec.parent_operation_id != parent_operation_id
                or child.spec.route.profile != "reviewer-callback"
                or child.lane_id != parent.lane_id
                or child.state != "failed"
                or child.pending_effect
                or child.effect_id
                or child.effect_outcome != EffectOutcome.NONE
                or child.resources != OwnedResources()
                or child.accepted_callback_id
                or child.accepted_callback_kind
                or child.accepted_callback_sha256
            ):
                raise StoreError("late started review identity changed")

            reopened_child = replace(
                child,
                state="awaiting-callback",
                revision=child.revision + 1,
                attention_reason=None,
                resume_state="",
            )
            recovered_parent = resolve_effect(parent, EffectOutcome.SUCCEEDED)
            recovered_parent = replace(
                recovered_parent,
                resources=resources,
                revision=recovered_parent.revision + 1,
            )
            for state in ("starting", "running", "awaiting-callback"):
                recovered_parent, _ = transition(recovered_parent, state)

            # Child first is fail-safe: until the parent commit appears the
            # worker still cannot classify this as the active generation.
            self._write(
                self._operation_path(owner_id, child_operation_id),
                to_dict(reopened_child),
            )
            self._write(
                self._operation_path(owner_id, parent_operation_id),
                to_dict(recovered_parent),
            )
            return recovered_parent, reopened_child

    def begin_effect(self, owner_id: str, operation_id: str, effect: str) -> OperationRecord:
        with self.locked(owner_id):
            record = self.read(owner_id, operation_id)
            updated = begin_effect(record, effect)
            if updated is not record:
                self._observe_durable_boundary(
                    "effect-reserved", phase="before"
                )
                self._write(self._operation_path(owner_id, operation_id), to_dict(updated))
                self._observe_durable_boundary("effect-reserved")
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
                self._observe_durable_boundary(
                    "effect-resolved", phase="before"
                )
                self._write(self._operation_path(owner_id, operation_id), to_dict(updated))
                self._observe_durable_boundary("effect-resolved")
            return updated
