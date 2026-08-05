"""Provider-neutral facts with exact identity and a fail-closed source cursor."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Iterable, Mapping


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
EVENT_KINDS = frozenset(
    {
        "provider-started",
        "input-accepted",
        "turn-stopped",
        "result-published",
        "process-exited",
        "resource-closed",
        "event-gap",
    }
)
PROFILE_EVENT_KINDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "interactive": EVENT_KINDS,
        "ephemeral": EVENT_KINDS - {"turn-stopped"},
    }
)


class ProviderEventError(ValueError):
    """A provider fact cannot belong to the exact ordered event stream."""


def _identifier(value: str, label: str, *, optional: bool = False) -> None:
    if optional and value == "":
        return
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ProviderEventError(f"{label} must be a bounded identifier")


@dataclass(frozen=True)
class ProviderEventIdentity:
    """Immutable owner and transport-source identity shared by every event."""

    operation_id: str
    run_id: str
    generation: int
    provider_session_id: str
    process_identity: str
    source_id: str
    workspace_id: str = ""
    surface_id: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ProviderEventError("unsupported provider event identity schema")
        for value, label in (
            (self.operation_id, "operation_id"),
            (self.run_id, "run_id"),
            (self.provider_session_id, "provider_session_id"),
            (self.source_id, "source_id"),
        ):
            _identifier(value, label)
        if type(self.generation) is not int or self.generation < 1:
            raise ProviderEventError("generation must be a positive integer")
        if (
            not isinstance(self.process_identity, str)
            or not SHA256.fullmatch(self.process_identity)
        ):
            raise ProviderEventError("process_identity must be a lowercase sha256")
        _identifier(self.workspace_id, "workspace_id", optional=True)
        _identifier(self.surface_id, "surface_id", optional=True)
        if bool(self.workspace_id) != bool(self.surface_id):
            raise ProviderEventError(
                "workspace and surface identities must be both present or both absent"
            )


@dataclass(frozen=True)
class ProviderEvent:
    """One content-free fact from a single provider adapter source."""

    kind: str
    identity: ProviderEventIdentity
    sequence: int
    effect_id: str = ""
    result_sha256: str = ""
    exit_code: int | None = None
    reason: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ProviderEventError("unsupported ProviderEvent schema")
        if self.kind not in EVENT_KINDS:
            raise ProviderEventError("provider event kind is outside the vocabulary")
        if not isinstance(self.identity, ProviderEventIdentity):
            raise ProviderEventError("provider event identity is invalid")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ProviderEventError("provider event sequence must be positive")
        _identifier(self.effect_id, "effect_id", optional=True)
        _identifier(self.reason, "reason", optional=True)
        if self.result_sha256 and not SHA256.fullmatch(self.result_sha256):
            raise ProviderEventError("result_sha256 must be a lowercase sha256")
        if self.exit_code is not None and (
            type(self.exit_code) is not int or not -(2**31) <= self.exit_code < 2**31
        ):
            raise ProviderEventError("exit_code must be a bounded integer")

        required_effect = self.kind == "input-accepted"
        required_result = self.kind == "result-published"
        required_exit = self.kind == "process-exited"
        required_reason = self.kind == "event-gap"
        if bool(self.effect_id) != required_effect:
            raise ProviderEventError(
                "only input-accepted carries one required effect_id"
            )
        if bool(self.result_sha256) != required_result:
            raise ProviderEventError(
                "only result-published carries one required result_sha256"
            )
        if (self.exit_code is not None) != required_exit:
            raise ProviderEventError(
                "only process-exited carries one required exit_code"
            )
        if required_reason and not self.reason:
            raise ProviderEventError("event-gap requires a typed reason")
        if self.reason and self.kind not in {"event-gap", "resource-closed"}:
            raise ProviderEventError(
                "only event-gap or resource-closed may carry a reason"
            )


@dataclass(frozen=True)
class ProviderEventCursor:
    """Validated projection of one ordered provider source."""

    profile: str
    identity: ProviderEventIdentity
    last_sequence: int = 0
    provider_started: bool = False
    input_accepted: bool = False
    result_published: bool = False
    process_exited: bool = False
    resource_closed: bool = False
    event_gap: bool = False
    turn_stops: int = 0
    schema_version: int = 1

    @classmethod
    def start(
        cls, profile: str, identity: ProviderEventIdentity
    ) -> "ProviderEventCursor":
        return cls(profile=profile, identity=identity)

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.profile not in PROFILE_EVENT_KINDS:
            raise ProviderEventError("provider event cursor profile is invalid")
        if not isinstance(self.identity, ProviderEventIdentity):
            raise ProviderEventError("provider event cursor identity is invalid")
        if self.profile == "interactive" and not self.identity.surface_id:
            raise ProviderEventError(
                "interactive provider events require exact workspace and surface"
            )
        if self.profile == "ephemeral" and self.identity.surface_id:
            raise ProviderEventError(
                "ephemeral provider events cannot claim interactive resources"
            )
        if type(self.last_sequence) is not int or self.last_sequence < 0:
            raise ProviderEventError("provider event cursor is invalid")
        if type(self.turn_stops) is not int or self.turn_stops < 0:
            raise ProviderEventError("provider turn-stop cursor is invalid")

    def advance(self, event: ProviderEvent) -> "ProviderEventCursor":
        """Validate and project exactly one next source event."""

        if not isinstance(event, ProviderEvent):
            raise ProviderEventError("provider event is invalid")
        if event.identity != self.identity:
            raise ProviderEventError("provider event identity changed")
        if event.kind not in PROFILE_EVENT_KINDS[self.profile]:
            raise ProviderEventError("provider event is invalid for its profile")
        expected = self.last_sequence + 1
        if event.sequence < expected:
            raise ProviderEventError("provider event is duplicate or stale")
        if event.sequence > expected:
            raise ProviderEventError("provider source cursor has an implicit gap")
        if self.resource_closed:
            raise ProviderEventError("provider event followed durable resource close")
        if not self.provider_started:
            if event.kind != "provider-started" or event.sequence != 1:
                raise ProviderEventError("provider-started must be the first event")
            return replace(
                self,
                last_sequence=event.sequence,
                provider_started=True,
            )
        if event.kind == "provider-started":
            raise ProviderEventError("provider-started cannot repeat")

        if self.event_gap and event.kind not in {
            "process-exited",
            "resource-closed",
        }:
            raise ProviderEventError("event-gap permits only terminal resource facts")
        if self.process_exited and event.kind != "resource-closed":
            raise ProviderEventError("process exit permits only resource close")

        changes: dict[str, object] = {"last_sequence": event.sequence}
        if event.kind == "input-accepted":
            if (
                self.input_accepted
                or self.result_published
                or self.process_exited
                or self.event_gap
            ):
                raise ProviderEventError("input-accepted is out of order")
            changes["input_accepted"] = True
        elif event.kind == "turn-stopped":
            if (
                not self.input_accepted
                or self.result_published
                or self.process_exited
                or self.event_gap
            ):
                raise ProviderEventError("turn-stopped is out of order")
            changes["turn_stops"] = self.turn_stops + 1
        elif event.kind == "result-published":
            if (
                not self.input_accepted
                or self.result_published
                or self.process_exited
                or self.event_gap
            ):
                raise ProviderEventError("result-published is out of order")
            changes["result_published"] = True
        elif event.kind == "process-exited":
            if self.process_exited:
                raise ProviderEventError("process-exited cannot repeat")
            changes["process_exited"] = True
        elif event.kind == "resource-closed":
            changes["resource_closed"] = True
        elif event.kind == "event-gap":
            if self.event_gap or self.process_exited:
                raise ProviderEventError("event-gap is out of order")
            changes["event_gap"] = True
        else:  # pragma: no cover - closed vocabulary makes this unreachable.
            raise ProviderEventError("provider event kind is invalid")
        return replace(self, **changes)


def validate_event_stream(
    profile: str,
    events: Iterable[ProviderEvent],
    *,
    expected_identity: ProviderEventIdentity | None = None,
) -> ProviderEventCursor:
    """Validate a non-empty stream and return its exact terminal cursor."""

    values = tuple(events)
    if not values:
        raise ProviderEventError("provider event stream must not be empty")
    identity = expected_identity or values[0].identity
    cursor = ProviderEventCursor.start(profile, identity)
    for value in values:
        cursor = cursor.advance(value)
    return cursor
