#!/usr/bin/env python3
"""Named durable-prefix crashes restart through production constructors."""

from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "harness"))

from harness.contracts import AttentionReason  # noqa: E402
from harness.runtime_sessions import RuntimeSessionManager  # noqa: E402
from harness.runtime_worker import run as runtime_worker_run  # noqa: E402
from harness.runtime_worker_execution import RuntimeWorkerExecution  # noqa: E402
from harness.store import OperationStore  # noqa: E402
from lifecycle_simulator import (  # noqa: E402
    LifecycleWorld,
    SimulatedCrash,
)
from lifecycle_simulator_oracle import assert_snapshot  # noqa: E402


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def expect_crash(label: str, callback, boundary: str) -> None:
    try:
        callback()
    except SimulatedCrash as exc:
        check(label, str(exc) == boundary, exc)
    else:
        raise AssertionError(f"{label}: failpoint did not crash")


with tempfile.TemporaryDirectory(prefix="lifecycle-crash-before.") as raw:
    world = LifecycleWorld.fresh(Path(raw))
    initial = world.durable_digest()
    for boundary in (
        "operation-transition-published",
        "liveness-published",
        "error-latch-published",
        "effect-reserved",
        "effect-resolved",
        "cleanup-receipt-published",
    ):
        expect_crash(
            f"crash before {boundary} preserves its durable prefix",
            lambda boundary=boundary: world.apply(
                {"action": "crash-at", "failpoint": boundary, "phase": "before"}
            ),
            boundary,
        )
        world = LifecycleWorld.restart(Path(raw))
        check(
            f"restart before {boundary} sees byte-identical durable state",
            world.durable_digest() == initial,
        )


with tempfile.TemporaryDirectory(prefix="lifecycle-operation-crash.") as raw:
    root = Path(raw)
    world = LifecycleWorld.fresh(root)
    world.apply(
        {
            "action": "crash-at",
            "failpoint": "operation-transition-published",
            "phase": "after",
        }
    )
    expect_crash(
        "operation transition crashes only after its durable record",
        lambda: world.store.transition(
            "sim-owner", "sim-operation", "preflight"
        ),
        "operation-transition-published",
    )
    world = LifecycleWorld.restart(root)
    check("restart observes the persisted operation transition", world.record().state == "preflight")
    world.store.transition(
        "sim-owner",
        "sim-operation",
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )
    world._publish_liveness()
    assert_snapshot(world.snapshot())


with tempfile.TemporaryDirectory(prefix="lifecycle-effect-crash.") as raw:
    root = Path(raw)
    world = LifecycleWorld.fresh(root)
    world.apply({"action": "start-worker"})
    provider_effects = world.provider.effects()
    world.apply(
        {"action": "crash-at", "failpoint": "effect-reserved", "phase": "after"}
    )
    expect_crash(
        "effect reservation crashes after the pending identity is durable",
        lambda: world.apply(
            {"action": "reserve-effect", "effect_id": "matrix-effect"}
        ),
        "effect-reserved",
    )
    world = LifecycleWorld.restart(root)
    check("restart rehydrates the pending effect", world.record().pending_effect == "matrix-effect")
    world.apply({"action": "reserve-effect", "effect_id": "matrix-effect"})
    world.apply(
        {"action": "crash-at", "failpoint": "effect-resolved", "phase": "after"}
    )
    expect_crash(
        "effect resolution crashes after the terminal disposition is durable",
        lambda: world.apply({"action": "resolve-effect", "outcome": "succeeded"}),
        "effect-resolved",
    )
    world = LifecycleWorld.restart(root)
    world.apply({"action": "resolve-effect", "outcome": "succeeded"})
    check(
        "effect replay preserves the single provider-facing identity",
        world.provider.effects() == provider_effects,
        world.provider.effects(),
    )


with tempfile.TemporaryDirectory(prefix="lifecycle-liveness-crash.") as raw:
    root = Path(raw)
    world = LifecycleWorld.fresh(root)
    world.apply(
        {"action": "crash-at", "failpoint": "liveness-published", "phase": "after"}
    )
    expect_crash(
        "liveness publication crashes after atomic state replacement",
        lambda: world.apply({"action": "publish-liveness"}),
        "liveness-published",
    )
    world = LifecycleWorld.restart(root)
    assert_snapshot(world.snapshot())


with tempfile.TemporaryDirectory(prefix="lifecycle-latch-crash.") as raw:
    root = Path(raw)
    world = LifecycleWorld.fresh(root)
    world.apply(
        {"action": "crash-at", "failpoint": "error-latch-published", "phase": "after"}
    )
    expect_crash(
        "error latch crashes after the immutable failure fact is durable",
        lambda: world.apply({"action": "publish-error-latch"}),
        "error-latch-published",
    )
    latch = world.runtime_root / "callback-error.json"
    before = latch.read_bytes()
    world = LifecycleWorld.restart(root)
    world.apply({"action": "worker-tick"})
    check("restart preserves the original error marker bytes", latch.read_bytes() == before)


with tempfile.TemporaryDirectory(prefix="lifecycle-cleanup-crash.") as raw:
    root = Path(raw)
    world = LifecycleWorld.fresh(root)
    world.apply({"action": "start-worker"})
    world.apply({"action": "publish-provider-event", "kind": "result-published"})
    world.apply({"action": "publish-callback"})
    provider_effects = world.provider.effects()
    world.apply(
        {
            "action": "crash-at",
            "failpoint": "cleanup-receipt-published",
            "phase": "after",
        }
    )
    expect_crash(
        "cleanup crashes after the exact close receipt is durable",
        lambda: world.apply({"action": "close"}),
        "cleanup-receipt-published",
    )
    world = LifecycleWorld.restart(root)
    world.apply({"action": "close"})
    check(
        "cleanup restart converges once without replaying provider input",
        world.record().state == "complete"
        and world.resource_close_count() == 1
        and world.provider.effects() == provider_effects,
        world.snapshot(),
    )


with tempfile.TemporaryDirectory(prefix="lifecycle-unconsumed.") as raw:
    world = LifecycleWorld.fresh(Path(raw))
    world.apply(
        {"action": "crash-at", "failpoint": "effect-reserved", "phase": "after"}
    )
    world.apply({"action": "advance-clock", "delta": 1})
    try:
        world.crashes.assert_consumed()
    except AssertionError:
        check("an armed but unconsumed failpoint makes the gate red", True)
    else:
        raise AssertionError("unconsumed failpoint unexpectedly passed")


check(
    "fault injection is test-only and absent from production CLI entrypoints",
    "fault_observer" in inspect.signature(OperationStore).parameters
    and "fault_observer" in inspect.signature(RuntimeWorkerExecution.execute).parameters
    and "fault_observer" not in inspect.signature(runtime_worker_run).parameters
    and "fault_observer" not in inspect.signature(RuntimeSessionManager.for_root).parameters,
)
check(
    "the simulator performed no real external effects",
    LifecycleWorld.real_effect_counts()
    == {"provider": 0, "model": 0, "cmux": 0, "network": 0},
)

print("\nAll lifecycle crash-matrix tests passed.")
