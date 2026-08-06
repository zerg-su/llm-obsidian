"""Production adapter from runtime facts to the durable delivery reducer."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

from .provider_events import ProviderEvent, ProviderEventError, ProviderEventIdentity
from .runtime_session_delivery import (
    DeliveryController,
    DeliveryDecision,
    DeliveryError,
)
from .runtime_session_liveness import (
    ResourceCloseError,
    ResourceClosedReceipt,
    resource_closed_event,
)


class RuntimeProviderEventError(DeliveryError):
    """A runtime fact cannot be bound to the exact durable provider stream."""


def input_effect_id(
    identity: ProviderEventIdentity, input_sha256: str
) -> str:
    if (
        not isinstance(input_sha256, str)
        or len(input_sha256) != 64
        or any(char not in "0123456789abcdef" for char in input_sha256)
    ):
        raise RuntimeProviderEventError("provider input digest is invalid")
    payload = {
        "identity": asdict(identity),
        "input_sha256": input_sha256,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class RuntimeProviderEventStream:
    """Serialize code-owned runtime observations into one exact event cursor."""

    def __init__(
        self,
        root: Path,
        *,
        identity: ProviderEventIdentity,
        idempotency_key: str,
    ) -> None:
        self.root = root
        self.controller = DeliveryController(
            root / "delivery",
            profile="interactive",
            identity=identity,
            idempotency_key=idempotency_key,
        )

    @classmethod
    def create(
        cls,
        provider_root: Path,
        *,
        owner_id: str,
        operation_id: str,
        run_id: str,
        generation: int,
        process_identity: str,
        workspace_id: str,
        surface_id: str,
        input_sha256: str,
    ) -> "RuntimeProviderEventStream":
        try:
            identity = ProviderEventIdentity(
                owner_id=owner_id,
                operation_id=operation_id,
                run_id=run_id,
                generation=generation,
                provider_session_id=run_id,
                process_identity=process_identity,
                source_id=f"process:{process_identity}",
                workspace_id=workspace_id,
                surface_id=surface_id,
            )
            return cls(
                provider_root / f"generation-{generation}",
                identity=identity,
                idempotency_key=input_effect_id(identity, input_sha256),
            )
        except (ProviderEventError, DeliveryError) as exc:
            raise RuntimeProviderEventError(
                "runtime provider event identity is invalid"
            ) from exc

    @classmethod
    def rehydrate(
        cls, provider_root: Path, generation: int
    ) -> "RuntimeProviderEventStream":
        root = provider_root / f"generation-{generation}"
        try:
            controller = DeliveryController.rehydrate(root / "delivery")
        except DeliveryError as exc:
            raise RuntimeProviderEventError(
                "runtime provider delivery state is unavailable"
            ) from exc
        return cls(
            root,
            identity=controller.initial.identity,
            idempotency_key=controller.initial.idempotency_key,
        )

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        path = self.root / ".events.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.chmod(path, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _event_payload(event: ProviderEvent) -> dict[str, object]:
        return asdict(event)

    def _event_path(self, sequence: int) -> Path:
        return self.root / "events" / f"{sequence:04d}.json"

    def _write_event(self, event: ProviderEvent) -> None:
        path = self._event_path(event.sequence)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        encoded = (
            json.dumps(
                self._event_payload(event),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != encoded:
                raise RuntimeProviderEventError("provider event receipt changed") from None
            return
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    def _assert_completed_event(
        self, kind: str, values: dict[str, object]
    ) -> None:
        matches: list[dict[str, object]] = []
        events = self.root / "events"
        for path in sorted(events.glob("*.json")) if events.is_dir() else ():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeProviderEventError(
                    "provider event receipt is invalid"
                ) from exc
            if isinstance(payload, dict) and payload.get("kind") == kind:
                matches.append(payload)
        if len(matches) != 1 or any(
            matches[0].get(key) != value for key, value in values.items()
        ):
            raise RuntimeProviderEventError("completed provider event changed")

    def emit(self, kind: str, **values: object) -> DeliveryDecision:
        """Persist the exact next event before advancing the reducer cursor."""

        with self._locked():
            state = self.controller.current_state()
            completed = {
                "provider-started": state.cursor.provider_started,
                "input-accepted": state.cursor.input_accepted,
                "result-published": state.cursor.result_published,
                "process-exited": state.cursor.process_exited,
                "resource-closed": state.cursor.resource_closed,
                "event-gap": state.cursor.event_gap,
            }
            if kind != "turn-stopped" and completed.get(kind):
                self._assert_completed_event(kind, dict(values))
                return self.controller.decide()
            event = ProviderEvent(
                kind,
                state.identity,
                state.cursor.last_sequence + 1,
                **values,
            )
            self._write_event(event)
            return self.controller.decide(event=event)

    def start(self) -> DeliveryDecision:
        return self.emit("provider-started")

    def reserve_input(self) -> DeliveryDecision:
        return self.controller.decide()

    def accept_input(self) -> DeliveryDecision:
        effect_id = self.controller.initial.idempotency_key
        self.controller.record_send_outcome(effect_id, "accepted")
        return self.emit("input-accepted", effect_id=effect_id)

    def ambiguous_input(self) -> None:
        self.controller.record_send_outcome(
            self.controller.initial.idempotency_key, "ambiguous"
        )

    def result(self, sha256: str) -> DeliveryDecision:
        return self.emit("result-published", result_sha256=sha256)

    def turn_stopped(self) -> DeliveryDecision:
        return self.emit("turn-stopped")

    def callback_submit_effect(self) -> str:
        """Return the already-reserved callback effect without minting an event."""

        state = self.controller.current_state()
        if (
            state.attention_reason
            or state.callback_submits != 1
            or state.cursor.turn_stops != 1
            or state.cursor.result_published
            or state.cursor.process_exited
            or state.cursor.resource_closed
        ):
            return ""
        return hashlib.sha256(
            f"{state.idempotency_key}:callback-submit".encode()
        ).hexdigest()

    def process_exited(self, exit_code: int) -> DeliveryDecision:
        return self.emit("process-exited", exit_code=exit_code)

    def event_gap(self, reason: str) -> DeliveryDecision:
        return self.emit("event-gap", reason=reason)

    def resource_closed(self, reason: str = "owned-resources-gone") -> DeliveryDecision:
        return self.emit("resource-closed", reason=reason)

    def resource_closed_receipt(
        self, receipt: ResourceClosedReceipt
    ) -> DeliveryDecision:
        """Advance only from the exact durable resource-close receipt."""

        with self._locked():
            state = self.controller.current_state()
            if state.cursor.resource_closed:
                self._assert_completed_event(
                    "resource-closed", {"reason": "owned-resources-gone"}
                )
                return self.controller.decide()
            try:
                event = resource_closed_event(state.cursor, receipt)
            except ResourceCloseError as exc:
                raise RuntimeProviderEventError(
                    "runtime resource close identity changed"
                ) from exc
            self._write_event(event)
            return self.controller.decide(event=event)
