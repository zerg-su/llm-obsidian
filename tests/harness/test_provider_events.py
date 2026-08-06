#!/usr/bin/env python3
"""Closed ProviderEvent vocabulary, identity, ordering, and cursor matrix."""

from __future__ import annotations

import dataclasses
import itertools
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.provider_events import (  # noqa: E402
    EVENT_KINDS,
    PROFILE_EVENT_KINDS,
    ProviderEvent,
    ProviderEventCursor,
    ProviderEventError,
    ProviderEventIdentity,
    validate_event_stream,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


INTERACTIVE_IDENTITY = ProviderEventIdentity(
    owner_id="review-owner",
    operation_id="review-attempt",
    run_id="review-run",
    generation=3,
    provider_session_id="provider-session",
    process_identity="a" * 64,
    source_id="boot-17",
    workspace_id="workspace-1",
    surface_id="surface-1",
)
EPHEMERAL_IDENTITY = ProviderEventIdentity(
    owner_id="bounded-owner",
    operation_id="bounded-review",
    run_id="bounded-run",
    generation=1,
    provider_session_id="ephemeral-session",
    process_identity="b" * 64,
    source_id="process-epoch-1",
)


def event(
    kind: str,
    sequence: int,
    *,
    identity: ProviderEventIdentity = INTERACTIVE_IDENTITY,
) -> ProviderEvent:
    values: dict[str, object] = {
        "kind": kind,
        "identity": identity,
        "sequence": sequence,
    }
    if kind == "input-accepted":
        values["effect_id"] = "delivery-effect"
    elif kind == "result-published":
        values["result_sha256"] = "c" * 64
    elif kind == "process-exited":
        values["exit_code"] = 0
    elif kind == "event-gap":
        values["reason"] = "source-gap"
    elif kind == "resource-closed":
        values["reason"] = "owned-resources-gone"
    return ProviderEvent(**values)


check(
    "ProviderEvent vocabulary is exact and closed",
    EVENT_KINDS
    == frozenset(
        {
            "provider-started",
            "input-accepted",
            "turn-stopped",
            "result-published",
            "process-exited",
            "resource-closed",
            "event-gap",
        }
    ),
)
check(
    "ephemeral profile cannot emit an interactive Stop",
    PROFILE_EVENT_KINDS["ephemeral"] == EVENT_KINDS - {"turn-stopped"}
    and PROFILE_EVENT_KINDS["interactive"] == EVENT_KINDS,
)

interactive = (
    event("provider-started", 1),
    event("input-accepted", 2),
    event("turn-stopped", 3),
    event("result-published", 4),
    event("process-exited", 5),
    event("resource-closed", 6),
)
cursor = validate_event_stream("interactive", interactive)
check(
    "interactive event stream binds one exact terminal cursor",
    cursor.identity == INTERACTIVE_IDENTITY
    and cursor.last_sequence == 6
    and cursor.input_accepted
    and cursor.result_published
    and cursor.process_exited
    and cursor.resource_closed,
)

ephemeral = tuple(
    dataclasses.replace(item, identity=EPHEMERAL_IDENTITY)
    for item in (
        event("provider-started", 1),
        event("input-accepted", 2),
        event("result-published", 3),
        event("process-exited", 4),
        event("resource-closed", 5),
    )
)
ephemeral_cursor = validate_event_stream("ephemeral", ephemeral)
check(
    "ephemeral stream reaches one result and durable close without a surface",
    ephemeral_cursor.last_sequence == 5
    and ephemeral_cursor.result_published
    and ephemeral_cursor.resource_closed,
)


def rejected(label: str, action) -> None:
    try:
        action()
    except (ProviderEventError, TypeError):
        check(label, True)
    else:
        check(label, False)


base_cursor = ProviderEventCursor.start("interactive", INTERACTIVE_IDENTITY)
started_cursor = base_cursor.advance(event("provider-started", 1))
for label, candidate in (
    ("duplicate cursor rejected", event("provider-started", 1)),
    ("stale cursor rejected", event("input-accepted", 1)),
    ("implicit cursor gap rejected", event("input-accepted", 3)),
    (
        "wrong durable owner rejected",
        event(
            "input-accepted",
            2,
            identity=dataclasses.replace(
                INTERACTIVE_IDENTITY, owner_id="other-owner"
            ),
        ),
    ),
    (
        "wrong generation rejected",
        event(
            "input-accepted",
            2,
            identity=dataclasses.replace(INTERACTIVE_IDENTITY, generation=4),
        ),
    ),
    (
        "wrong surface rejected",
        event(
            "input-accepted",
            2,
            identity=dataclasses.replace(
                INTERACTIVE_IDENTITY, surface_id="surface-2"
            ),
        ),
    ),
    (
        "source boot change rejected",
        event(
            "input-accepted",
            2,
            identity=dataclasses.replace(
                INTERACTIVE_IDENTITY, source_id="boot-18"
            ),
        ),
    ),
):
    rejected(label, lambda candidate=candidate: started_cursor.advance(candidate))

rejected(
    "ephemeral turn-stopped rejected",
    lambda: validate_event_stream(
        "ephemeral",
        (
            dataclasses.replace(event("provider-started", 1), identity=EPHEMERAL_IDENTITY),
            dataclasses.replace(event("input-accepted", 2), identity=EPHEMERAL_IDENTITY),
            dataclasses.replace(event("turn-stopped", 3), identity=EPHEMERAL_IDENTITY),
        ),
    ),
)
rejected(
    "unknown vocabulary rejected at construction",
    lambda: ProviderEvent("provider-repainted", INTERACTIVE_IDENTITY, 1),
)
rejected(
    "screen and time cannot be smuggled into provider facts",
    lambda: ProviderEvent(
        "provider-started",
        INTERACTIVE_IDENTITY,
        1,
        screen_sha256="d" * 64,
        observed_at=10,
    ),
)

for label, values in (
    (
        "cursor flags require exact booleans",
        {
            "last_sequence": 1,
            "provider_started": 1,
        },
    ),
    (
        "cursor cannot claim unreachable terminal events",
        {
            "result_published": True,
            "resource_closed": True,
        },
    ),
    (
        "cursor result cannot precede accepted input",
        {
            "last_sequence": 2,
            "provider_started": True,
            "result_published": True,
        },
    ),
):
    rejected(
        label,
        lambda values=values: ProviderEventCursor(
            profile="interactive",
            identity=INTERACTIVE_IDENTITY,
            **values,
        ),
    )

gap_stream = (
    event("provider-started", 1),
    event("input-accepted", 2),
    event("event-gap", 3),
    event("process-exited", 4),
    event("resource-closed", 5),
)
gap_cursor = validate_event_stream("interactive", gap_stream)
check(
    "an explicit contiguous event-gap remains terminal business evidence",
    gap_cursor.event_gap
    and not gap_cursor.result_published
    and gap_cursor.resource_closed,
)

for first, second in itertools.permutations(
    ("input-accepted", "result-published", "process-exited"), 2
):
    if (first, second) in {
        ("input-accepted", "result-published"),
        ("input-accepted", "process-exited"),
        ("result-published", "process-exited"),
    }:
        continue
    rejected(
        f"generated invalid lifecycle ordering rejected: {first}/{second}",
        lambda first=first, second=second: validate_event_stream(
            "interactive",
            (
                event("provider-started", 1),
                event(first, 2),
                event(second, 3),
            ),
        ),
    )

print("provider event contract matrix: ok")
