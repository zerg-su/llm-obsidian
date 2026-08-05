"""Durable reducer for provider input, callback submit, and close decisions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from .provider_events import (
    IDENTIFIER,
    PROFILE_EVENT_KINDS,
    ProviderEvent,
    ProviderEventCursor,
    ProviderEventError,
    ProviderEventIdentity,
)


DELIVERY_ACTIONS = frozenset(
    {"send", "wait", "submit-callback", "attention", "close"}
)
SEND_STATUSES = frozenset(
    {"ready", "reserved", "failed-before-input", "accepted", "ambiguous"}
)
SEND_OUTCOMES = frozenset({"failed-before-input", "accepted", "ambiguous"})


class DeliveryError(ValueError):
    """A delivery transition would cross or replay an irreversible boundary."""


def _identifier(value: str, label: str, *, optional: bool = False) -> None:
    if optional and value == "":
        return
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise DeliveryError(f"{label} must be a bounded identifier")


@dataclass(frozen=True)
class DeliveryDecision:
    action: str
    action_id: str
    effect_id: str = ""
    reason: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or self.action not in DELIVERY_ACTIONS
            or not isinstance(self.action_id, str)
            or len(self.action_id) != 64
        ):
            raise DeliveryError("delivery decision is invalid")
        _identifier(self.effect_id, "delivery effect_id", optional=True)
        _identifier(self.reason, "delivery reason", optional=True)
        if self.action in {"send", "submit-callback"} and not self.effect_id:
            raise DeliveryError("provider-facing delivery requires an effect_id")
        if self.effect_id and self.action not in {"send", "submit-callback"}:
            raise DeliveryError("non-provider delivery cannot carry an effect_id")


@dataclass(frozen=True)
class DeliveryState:
    profile: str
    identity: ProviderEventIdentity
    idempotency_key: str
    cursor: ProviderEventCursor
    send_attempts: int = 0
    send_status: str = "ready"
    callback_submits: int = 0
    attention_reason: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or self.profile not in PROFILE_EVENT_KINDS
            or not isinstance(self.identity, ProviderEventIdentity)
            or not isinstance(self.cursor, ProviderEventCursor)
            or self.cursor.identity != self.identity
            or self.cursor.profile != self.profile
            or self.send_status not in SEND_STATUSES
            or type(self.send_attempts) is not int
            or not 0 <= self.send_attempts <= 2
            or type(self.callback_submits) is not int
            or not 0 <= self.callback_submits <= 1
        ):
            raise DeliveryError("delivery state is invalid")
        _identifier(self.idempotency_key, "delivery idempotency_key")
        _identifier(
            self.attention_reason,
            "delivery attention reason",
            optional=True,
        )
        if self.send_status == "ready" and self.send_attempts:
            raise DeliveryError("ready delivery cannot retain send attempts")
        if self.send_status != "ready" and not self.send_attempts:
            raise DeliveryError("delivery outcome requires a send attempt")


def _state_from_dict(value: object) -> DeliveryState:
    if not isinstance(value, dict) or set(value) != {
        item.name for item in fields(DeliveryState)
    }:
        raise DeliveryError("durable delivery state is invalid")
    identity_value = value.get("identity")
    cursor_value = value.get("cursor")
    if not isinstance(identity_value, dict) or not isinstance(cursor_value, dict):
        raise DeliveryError("durable delivery identity is invalid")
    try:
        identity = ProviderEventIdentity(**identity_value)
        cursor_identity = cursor_value.get("identity")
        if not isinstance(cursor_identity, dict):
            raise TypeError
        cursor = ProviderEventCursor(
            **{
                **cursor_value,
                "identity": ProviderEventIdentity(**cursor_identity),
            }
        )
        return DeliveryState(
            **{
                **value,
                "identity": identity,
                "cursor": cursor,
            }
        )
    except (ProviderEventError, TypeError) as exc:
        raise DeliveryError("durable delivery state is invalid") from exc


class DeliveryController:
    """Persist the irreversible input boundary before returning any effect."""

    def __init__(
        self,
        root: Path | str,
        *,
        profile: str,
        identity: ProviderEventIdentity,
        idempotency_key: str,
    ):
        self.root = Path(root)
        self.initial = DeliveryState(
            profile,
            identity,
            idempotency_key,
            ProviderEventCursor.start(profile, identity),
        )

    @classmethod
    def rehydrate(cls, root: Path | str) -> "DeliveryController":
        """Open only an existing owner-only state without caller identity input."""

        path = Path(root) / "delivery-state.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise DeliveryError("durable delivery state is not owner-only")
        try:
            raw = path.read_bytes()
            if not raw or len(raw) > 65_536:
                raise ValueError
            state = _state_from_dict(json.loads(raw))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise DeliveryError("durable delivery state is invalid") from exc
        return cls(
            root,
            profile=state.profile,
            identity=state.identity,
            idempotency_key=state.idempotency_key,
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def _locked(self):
        parent_existed = self.root.exists()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        if not parent_existed:
            self._fsync_directory(self.root.parent)
        lock_path = self.root / ".delivery.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.chmod(lock_path, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read(self) -> DeliveryState:
        path = self.root / "delivery-state.json"
        if not path.exists():
            return self.initial
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise DeliveryError("durable delivery state is not owner-only")
        try:
            raw = path.read_bytes()
            if not raw or len(raw) > 65_536:
                raise ValueError
            state = _state_from_dict(json.loads(raw))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise DeliveryError("durable delivery state is invalid") from exc
        if (
            state.profile != self.initial.profile
            or state.identity != self.initial.identity
            or state.idempotency_key != self.initial.idempotency_key
        ):
            raise DeliveryError("durable delivery identity changed")
        return state

    def _write(self, state: DeliveryState) -> None:
        path = self.root / "delivery-state.json"
        encoded = (
            json.dumps(asdict(state), sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        descriptor, raw = tempfile.mkstemp(prefix=".delivery-state.", dir=self.root)
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

    @staticmethod
    def _decision(
        state: DeliveryState,
        action: str,
        *,
        effect_id: str = "",
        reason: str = "",
    ) -> DeliveryDecision:
        payload = json.dumps(
            {
                "state": asdict(state),
                "action": action,
                "effect_id": effect_id,
                "reason": reason,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return DeliveryDecision(
            action,
            hashlib.sha256(payload.encode()).hexdigest(),
            effect_id,
            reason,
        )

    def current_state(self) -> DeliveryState:
        with self._locked():
            return self._read()

    def record_send_outcome(self, effect_id: str, outcome: str) -> DeliveryState:
        """Settle one reserved send without reopening an ambiguous boundary."""

        if effect_id != self.initial.idempotency_key or outcome not in SEND_OUTCOMES:
            raise DeliveryError("delivery send outcome is invalid")
        with self._locked():
            state = self._read()
            if state.send_status == outcome:
                return state
            if state.send_status != "reserved":
                raise DeliveryError("delivery send is not reserved")
            updated = replace(state, send_status=outcome)
            self._write(updated)
            return updated

    def decide(
        self,
        *,
        event: ProviderEvent | None = None,
        deadline_reached: bool = False,
        screen_changed: bool = False,
    ) -> DeliveryDecision:
        """Reduce one typed event or non-authoritative observation durably."""

        if not isinstance(deadline_reached, bool) or not isinstance(screen_changed, bool):
            raise DeliveryError("delivery observation flags are invalid")
        with self._locked():
            state = self._read()
            if event is not None:
                try:
                    cursor = state.cursor.advance(event)
                except ProviderEventError as exc:
                    raise DeliveryError("provider event failed delivery validation") from exc
                if (
                    event.kind == "input-accepted"
                    and event.effect_id != state.idempotency_key
                ):
                    raise DeliveryError("accepted input idempotency key changed")
                state = replace(state, cursor=cursor)
                if event.kind == "input-accepted":
                    if state.send_attempts == 0:
                        raise DeliveryError("input accepted without a reserved send")
                    state = replace(state, send_status="accepted")
                if event.kind == "event-gap":
                    state = replace(state, attention_reason="event-gap")
                elif event.kind == "turn-stopped" and not cursor.result_published:
                    if cursor.turn_stops == 1 and state.callback_submits == 0:
                        state = replace(state, callback_submits=1)
                        self._write(state)
                        callback_effect = hashlib.sha256(
                            f"{state.idempotency_key}:callback-submit".encode()
                        ).hexdigest()
                        return self._decision(
                            state,
                            "submit-callback",
                            effect_id=callback_effect,
                            reason="turn-stopped",
                        )
                    state = replace(
                        state,
                        attention_reason="callback-submit-exhausted",
                    )
                elif event.kind == "process-exited" and not cursor.result_published:
                    state = replace(state, attention_reason="result-missing")
                elif event.kind == "resource-closed" and not cursor.result_published:
                    state = replace(state, attention_reason="result-missing")

                self._write(state)
                if state.attention_reason:
                    return self._decision(
                        state,
                        "attention",
                        reason=state.attention_reason,
                    )
                if cursor.result_published or cursor.resource_closed:
                    return self._decision(state, "close", reason="terminal-event")
                return self._decision(state, "wait", reason="event-observed")

            if state.attention_reason:
                return self._decision(
                    state,
                    "attention",
                    reason=state.attention_reason,
                )
            if state.cursor.result_published or state.cursor.resource_closed:
                return self._decision(state, "close", reason="terminal-event")
            if deadline_reached:
                state = replace(state, attention_reason="deadline-reached")
                self._write(state)
                return self._decision(
                    state,
                    "attention",
                    reason="deadline-reached",
                )
            if state.send_status in {"ready", "failed-before-input"}:
                if state.send_attempts >= 2:
                    state = replace(state, attention_reason="send-retry-exhausted")
                    self._write(state)
                    return self._decision(
                        state,
                        "attention",
                        reason="send-retry-exhausted",
                    )
                state = replace(
                    state,
                    send_attempts=state.send_attempts + 1,
                    send_status="reserved",
                )
                self._write(state)
                return self._decision(
                    state,
                    "send",
                    effect_id=state.idempotency_key,
                    reason="pre-accept-send",
                )
            # Repaint is deliberately absent from the decision identity and state.
            del screen_changed
            return self._decision(state, "wait", reason="await-provider-event")
