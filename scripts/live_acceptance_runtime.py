"""Callback transport and exact runtime cleanup for live acceptance."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Callable

from harness.contracts import CallbackEnvelope, OperationRecord, OwnedResources
from live_acceptance_contracts import (
    RETRYABLE_CLEANUP_ATTENTION,
    LiveDriverError,
    RuntimeSessions,
    _stable_id,
)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _callback_template(
    operation_id: str,
    run_id: str,
    kind: str,
) -> CallbackEnvelope:
    if kind == "runtime-lifecycle":
        callback_kind = "review"
        payload: dict[str, Any] = {"verdict": "changes-requested"}
    elif "review" in kind:
        callback_kind = "review"
        payload = {"verdict": "approve"}
    else:
        callback_kind = "result"
        payload = {"status": "complete"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return CallbackEnvelope(
        callback_id=f"live-{_stable_id(operation_id, run_id, callback_kind)}",
        operation_id=operation_id,
        run_id=run_id,
        kind=callback_kind,
        payload=payload,
        payload_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _render_prompt(envelope: CallbackEnvelope, callback_pointer: str) -> str:
    callback = {
        "schema_version": envelope.schema_version,
        "callback_id": envelope.callback_id,
        "operation_id": envelope.operation_id,
        "run_id": envelope.run_id,
        "kind": envelope.kind,
        "payload": dict(envelope.payload),
        "payload_sha256": envelope.payload_sha256,
    }
    return (
        "This is a bounded LLM Obsidian live-acceptance probe. "
        "Do not edit tracked repository files and do not start another model. "
        "Confirm that this interactive session is usable, then atomically write "
        "the exact JSON object below to the registered callback pointer and wait "
        "for the coordinator's next instruction.\n\n"
        f"Callback pointer: {callback_pointer}\n"
        f"Callback JSON: {json.dumps(callback, ensure_ascii=False, sort_keys=True)}\n"
    )


def _callback_from_value(value: object) -> CallbackEnvelope:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "callback_id",
        "operation_id",
        "run_id",
        "kind",
        "payload",
        "payload_sha256",
    }:
        raise LiveDriverError("provider callback has an invalid typed shape")
    try:
        return CallbackEnvelope(
            callback_id=value["callback_id"],
            operation_id=value["operation_id"],
            run_id=value["run_id"],
            kind=value["kind"],
            payload=value["payload"],
            payload_sha256=value["payload_sha256"],
            schema_version=value["schema_version"],
        )
    except (TypeError, ValueError) as exc:
        raise LiveDriverError(f"provider callback is invalid: {exc}") from exc


def _result_record(value: object) -> OperationRecord:
    record = getattr(value, "record", value)
    if not isinstance(record, OperationRecord):
        raise LiveDriverError("runtime session port returned no typed operation record")
    return record


def _resources_released(record: OperationRecord) -> bool:
    return record.resources == OwnedResources()


def _read_callback(
    path: Path,
    manager: RuntimeSessions,
    *,
    owner_id: str,
    operation_id: str,
    expected: CallbackEnvelope,
    deadline: float,
    sleep: Callable[[float], None],
) -> OperationRecord:
    while True:
        if path.is_file():
            try:
                if path.stat().st_size > CallbackEnvelope.MAX_PAYLOAD_BYTES:
                    raise LiveDriverError("provider callback exceeds size cap")
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise LiveDriverError(f"cannot read provider callback: {exc}") from exc
            envelope = _callback_from_value(value)
            if envelope != expected:
                raise LiveDriverError("provider callback mismatches the bounded live request")
            if (
                envelope.operation_id
                != _result_record(manager.status(owner_id, operation_id)).spec.operation_id
            ):
                raise LiveDriverError("provider callback operation identity mismatches")
            manager.accept_callback(envelope)
            return _result_record(manager.status(owner_id, operation_id))
        status = _result_record(manager.status(owner_id, operation_id))
        if status.state in {"complete", "failed", "cancelled", "attention-required"}:
            raise LiveDriverError(
                f"{operation_id}: runtime stopped before its typed callback ({status.state})"
            )
        if time.monotonic() >= deadline:
            raise LiveDriverError(f"{operation_id}: typed callback timed out")
        sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def _operation_evidence(
    record: OperationRecord,
    *,
    callback_count: int | None = None,
) -> dict[str, Any]:
    resources = record.resources
    remaining = sum(
        bool(value)
        for value in (
            resources.surface_id,
            resources.process_group,
            resources.supervisor_pid,
        )
    )
    return {
        "operation_id": record.spec.operation_id,
        "kind": record.spec.kind,
        "runtime": record.spec.route.runtime,
        "lane_id": record.lane_id,
        "run_id": record.run_id,
        "terminal_state": record.state,
        "effect_outcome": record.effect_outcome.value,
        "callback_count": (
            int(bool(record.accepted_callback_id))
            if callback_count is None
            else callback_count
        ),
        "owned_resources_remaining": remaining,
    }


def _accepted_callback_matches(
    record: OperationRecord,
    expected: CallbackEnvelope,
) -> bool:
    return (
        record.accepted_callback_id == expected.callback_id
        and record.accepted_callback_kind == expected.kind
        and record.accepted_callback_sha256 == expected.payload_sha256
    )


def _await_cleanup(
    manager: RuntimeSessions,
    *,
    owner_id: str,
    operation_id: str,
    deadline: float,
    sleep: Callable[[float], None],
) -> OperationRecord:
    ambiguous_retries = 0
    while True:
        current = _result_record(manager.status(owner_id, operation_id))
        if current.state == "attention-required":
            if current.attention_reason not in RETRYABLE_CLEANUP_ATTENTION:
                raise LiveDriverError(
                    f"{operation_id}: cleanup stopped in attention-required"
                )
            if ambiguous_retries >= 2:
                raise LiveDriverError(
                    f"{operation_id}: exit ownership remained ambiguous"
                )
            ambiguous_retries += 1
            exit_result = manager.request_exit(owner_id, operation_id)
            current = _result_record(exit_result)
            if current.state == "attention-required":
                if (
                    ambiguous_retries >= 2
                    or time.monotonic() >= deadline
                ):
                    raise LiveDriverError(
                        f"{operation_id}: exit ownership remained ambiguous"
                    )
                sleep(min(0.25, max(0.0, deadline - time.monotonic())))
                continue
        if current.state in {"complete", "failed", "cancelled"}:
            if _resources_released(current):
                return current
            raise LiveDriverError(
                f"{operation_id}: terminal {current.state} retained owned resources"
            )
        if current.state != "exiting":
            raise LiveDriverError(
                f"{operation_id}: cleanup requires an exiting operation"
            )
        result = manager.cleanup(owner_id, operation_id)
        record = _result_record(result)
        if record.state in {"complete", "failed", "cancelled"}:
            if _resources_released(record):
                return record
            raise LiveDriverError(
                f"{operation_id}: terminal {record.state} retained owned resources"
            )
        if record.state == "attention-required":
            if record.attention_reason not in RETRYABLE_CLEANUP_ATTENTION:
                raise LiveDriverError(
                    f"{operation_id}: cleanup stopped in attention-required"
                )
            if ambiguous_retries >= 2 or time.monotonic() >= deadline:
                raise LiveDriverError(
                    f"{operation_id}: exit ownership remained ambiguous"
                )
            sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            continue
        if getattr(result, "action", "") not in {
            "wait-for-exit",
            "wait-for-ownership",
            "wait-for-supervisor",
            "exit-requested",
        }:
            raise LiveDriverError(
                f"{operation_id}: cleanup did not prove provider exit"
            )
        if time.monotonic() >= deadline:
            raise LiveDriverError(f"{operation_id}: provider exit timed out")
        sleep(min(0.25, max(0.0, deadline - time.monotonic())))


class _StartedOperations:
    """Record exactly which operations this run started.

    The acceptance run owns one cmux surface per started operation
    (`runtime_sessions.start` binds it before the provider launches).  Release
    is otherwise wired only to the success path, so an abnormal exit would
    abandon a live surface.  This proxy is the run's own ownership ledger: it
    can never name a surface this run did not open, which is what keeps
    abort-time release from touching an unrelated or coordinator surface.
    """

    def __init__(self, manager: RuntimeSessions) -> None:
        self._manager = manager
        self.started: list[tuple[str, str]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)

    def _remember(self, key: tuple[str, str]) -> None:
        if key not in self.started:
            self.started.append(key)

    def start(self, request: Any, **kwargs: Any) -> Any:
        key = (request.spec.owner_id, request.spec.operation_id)
        supplied = kwargs.pop("on_surface_opened", None)

        def surface_opened(opened: Any) -> None:
            # `start` binds the surface and notifies this seam long before it
            # returns, and an interrupt in that window escapes its own
            # `except Exception` handlers.  Record ownership at bind time so
            # the surface can never be owned by nobody.
            self._remember(key)
            if supplied is not None:
                supplied(opened)

        result = self._manager.start(
            request, on_surface_opened=surface_opened, **kwargs
        )
        # A replay returns an existing record without reopening a surface, so
        # the seam above may never fire.
        self._remember(key)
        return result


def _release_started(
    tracked: _StartedOperations,
    *,
    sleep: Callable[[float], None],
    budget_seconds: float = 30.0,
) -> list[str]:
    """Release exactly the still-owned operations this run started.

    Best effort by design: this runs while an exception is already unwinding,
    so a failure to release must never replace the original classification.
    Unreleased operations are returned for visible reporting and remain
    recoverable through `harness-cli.py reconcile`.
    """

    unreleased: list[str] = []
    for owner_id, operation_id in reversed(tracked.started):
        try:
            record = _result_record(tracked.status(owner_id, operation_id))
            if record.state in {"complete", "failed", "cancelled"}:
                if _resources_released(record):
                    continue
                unreleased.append(
                    f"{operation_id}: terminal {record.state} retained owned resources"
                )
                continue
            if record.state == "attention-required":
                # A deliberate attention state is a fail-closed boundary: the
                # coordinator classifies it and owns the decision.  Releasing
                # it here would destroy the evidence it exists to preserve.
                reason = getattr(
                    record.attention_reason, "value", record.attention_reason
                )
                unreleased.append(f"{operation_id}: attention-required ({reason})")
                continue
            if record.state != "exiting":
                tracked.request_exit(owner_id, operation_id)
            # A fresh budget: the original deadline is usually already spent,
            # which is precisely why this operation still owns a surface.
            _await_cleanup(
                tracked,
                owner_id=owner_id,
                operation_id=operation_id,
                deadline=time.monotonic() + budget_seconds,
                sleep=sleep,
            )
        # BaseException: a second interrupt arriving mid-release must not
        # abandon the surfaces still queued behind it.  Each operation has a
        # bounded budget, so recording and continuing stays short.
        except BaseException as exc:  # noqa: BLE001 - never mask the original
            unreleased.append(f"{operation_id}: {exc!r}")
    if unreleased:
        print(
            "live acceptance could not release every owned surface; recover the "
            "exact operations with harness-cli.py reconcile: "
            + "; ".join(unreleased),
            file=sys.stderr,
        )
    return unreleased
