#!/usr/bin/env python3
"""Simulator fakes remain a strict subset of both logical provider contracts."""

from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "harness"))

from harness.ephemeral_provider import LOGICAL_PROVIDERS  # noqa: E402
from harness.provider_events import PROFILE_EVENT_KINDS  # noqa: E402
from lifecycle_simulator import (  # noqa: E402
    DeterministicEventSource,
    FakeCmux,
    FakeProcess,
    FakeProvider,
    LifecycleWorld,
)


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


check(
    "both supported logical providers share the closed ephemeral event contract",
    LOGICAL_PROVIDERS == {"anthropic", "openai"}
    and all(FakeProvider.EVENT_KINDS <= PROFILE_EVENT_KINDS["ephemeral"] for _ in LOGICAL_PROVIDERS),
    FakeProvider.EVENT_KINDS,
)
check(
    "deterministic interactive source cannot invent an event kind",
    DeterministicEventSource.EVENT_KINDS <= PROFILE_EVENT_KINDS["interactive"],
    DeterministicEventSource.EVENT_KINDS,
)


class RecordingStream:
    def __init__(self) -> None:
        self.kinds: list[str] = []

    def accept_input(self) -> None:
        self.kinds.append("input-accepted")


with tempfile.TemporaryDirectory(prefix="lifecycle-provider-conformance.") as raw:
    stream = RecordingStream()
    provider = FakeProvider(Path(raw))
    first = provider.deliver("conformance-effect", stream)
    second = provider.deliver("conformance-effect", stream)
    check(
        "fake provider records one idempotency identity and one logical delivery",
        first == second
        and first["deliveries"] == 1
        and provider.effects() == [first]
        and stream.kinds == ["input-accepted"],
        (provider.effects(), stream.kinds),
    )

check(
    "simulator adapters expose facts but no production adapter inheritance",
    all(
        not any(base.__module__.startswith("harness.adapters") for base in adapter.__mro__)
        for adapter in (FakeProvider, FakeProcess, FakeCmux)
    ),
)
source = "\n".join(
    inspect.getsource(adapter) for adapter in (FakeProvider, FakeProcess, FakeCmux)
)
check(
    "fake external adapters contain no process, network, or real cmux launch primitive",
    all(token not in source for token in ("subprocess.", "socket.", "urlopen(", "CmuxAdapter(")),
)
check(
    "conformance path reports zero real provider, model, cmux, and network effects",
    LifecycleWorld.real_effect_counts()
    == {"provider": 0, "model": 0, "cmux": 0, "network": 0},
)

print("\nAll lifecycle provider-conformance tests passed.")
