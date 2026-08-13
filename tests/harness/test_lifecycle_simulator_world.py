#!/usr/bin/env python3
"""Real-core lifecycle world and deterministic production poll seams."""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "harness"))

from harness.contracts import OperationSpec, RuntimeRoute, to_dict  # noqa: E402
from harness.runtime_worker_loop import RuntimeWorkerLoopMixin  # noqa: E402
from harness.store import OperationStore  # noqa: E402


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


class PollProbe(RuntimeWorkerLoopMixin):
    def __init__(self) -> None:
        self.events: list[str] = []
        self.provider_exited = False
        self.wake_source = SimpleNamespace(wait=lambda _timeout: None)
        self.monotonic_clock = lambda: 0.0
        self.next_full_reconcile = 0.0
        self.next_transport_confirmation = float("inf")
        self.next_cross_session_reconcile = float("inf")
        self.next_provider_exit_probe = 0.0
        self.next_wake_retry = float("inf")
        self.wake_retry_attempts = 0
        self.wake_source_disabled = False
        self.poll_seconds = 0.1

    def transport_snapshot(self) -> tuple[str, ...]:
        return tuple(self.events)

    def record_transport_wake(
        self, _observation: object, _before: object, _after: object
    ) -> None:
        self.events.append("wake")

    def inspect_transport(self) -> None:
        self.events.append("inspect")

    def tick_observers(self) -> None:
        self.events.append("tick")

    def observe_provider_exit(self) -> bool:
        self.events.append("observe-exit")
        return True

    def mark_failed_research_runtime(self) -> None:
        self.events.append("classify-exit")

    def mark_failed_task_summary_correction_runtime(self) -> None:
        self.events.append("classify-summary-correction-exit")

    def mark_failed_pipeline_step_correction_runtime(self) -> None:
        self.events.append("classify-step-correction-exit")

    def needs_provider_restart(self) -> bool:
        self.events.append("restart-decision")
        return False

    def restart_provider(self) -> None:
        raise AssertionError("restart was not selected")

    def provider_exit_is_final(self) -> bool:
        self.events.append("final-exit-decision")
        return True


probe = PollProbe()
check(
    "production poll_once owns inspect, observer, and exit observation order",
    probe.poll_once() is True
    and probe.events == ["inspect", "wake", "tick", "observe-exit"],
    probe.events,
)
check(
    "production settle_exit_once owns classification and restart/final decision",
    probe.settle_exit_once() is True
    and probe.events[-5:]
    == [
        "classify-exit",
        "classify-summary-correction-exit",
        "classify-step-correction-exit",
        "restart-decision",
        "final-exit-decision",
    ],
    probe.events,
)


class ClockProbe(RuntimeWorkerLoopMixin):
    def __init__(self, store: OperationStore) -> None:
        self.store = store
        self.spec = {
            "owner_id": "sim-owner",
            "operation_id": "sim-operation",
        }
        self.spec_path = store.root / "runtime" / "runtime.json"
        self.callback_handled = False
        self.monotonic_clock = lambda: 12.0
        self.liveness_policy = SimpleNamespace(probe_seconds=30)
        self.next_liveness_probe = 12.0
        self.next_prompt_probe = 12.0
        self.next_checkpoint_probe = 12.0
        self.checkpoint = ""
        self.events: list[str] = []

    def inspect_liveness(self) -> None:
        self.events.append("liveness")

    def inspect_prompt(self) -> None:
        self.events.append("prompt")

    def capture_checkpoint(self) -> None:
        self.events.append("checkpoint")


with tempfile.TemporaryDirectory(prefix="simulator-clock.") as raw:
    clock_store = OperationStore(Path(raw) / "store")
    clock_store.create(
        OperationSpec(
            operation_id="sim-operation",
            idempotency_key="sim-idempotency",
            kind="dispatch",
            owner_id="sim-owner",
            route=RuntimeRoute("codex", "sol", "high", "dispatch", "a" * 64),
            context_manifest="context/manifest.json",
            verification_profile="scoped",
        ),
        lane_id="sim-lane",
        run_id="sim-run",
    )
    clock_probe = ClockProbe(clock_store)
    clock_probe.tick_observers()
    check(
        "observer pacing consumes the injected monotonic clock",
        clock_probe.events == ["liveness", "prompt", "checkpoint"]
        and clock_probe.next_liveness_probe == 42.0,
        clock_probe.events,
    )


class DrainProbe(RuntimeWorkerLoopMixin):
    def __init__(self) -> None:
        self.callback_handled = False
        self.poll_seconds = 10.0
        self.inspections = 0
        self.sleeps: list[float] = []
        self.sleeper = self.sleeps.append

    def inspect_transport(self) -> None:
        self.inspections += 1
        self.callback_handled = self.inspections == 2


drain = DrainProbe()
drain.drain_callbacks()
check(
    "callback draining uses injected pacing with no real sleep",
    drain.inspections == 2 and drain.sleeps == [10.0],
    {"inspections": drain.inspections, "sleeps": drain.sleeps},
)


from lifecycle_simulator import LifecycleWorld  # noqa: E402
from lifecycle_simulator_oracle import InvariantViolation, assert_snapshot  # noqa: E402


class NonFakeProvider:
    """Mutation probe: delegates safely but must be measured as non-fake."""

    def __init__(self, delegate) -> None:
        self.delegate = delegate

    def deliver(self, effect_id, stream):
        return self.delegate.deliver(effect_id, stream)

    def effects(self):
        return self.delegate.effects()


def expect_invariant(label: str, invariant_id: str, action) -> None:
    try:
        action()
    except InvariantViolation as exc:
        check(label, exc.invariant_id == invariant_id, exc)
    else:
        raise AssertionError(f"{label}: mutation unexpectedly passed")


with tempfile.TemporaryDirectory(prefix="lifecycle-world.") as raw:
    world = LifecycleWorld.fresh(Path(raw))
    check(
        "fresh world uses the production OperationStore",
        isinstance(world.store, OperationStore)
        and world.record().state == "created",
    )
    world.apply({"action": "start-worker"})
    started = world.snapshot()
    check(
        "fake provider crosses one real delivery reducer and store effect",
        started["operation"]["state"] == "awaiting-callback"
        and started["effects"][0]["deliveries"] == 1
        and world.real_effect_counts()
        == {"provider": 0, "model": 0, "cmux": 0, "network": 0},
        started,
    )
    check(
        "effect observations survive restart and count the fake attempt",
        world.external_attempt_counts()["provider"] == 1,
        world.external_attempt_counts(),
    )
    durable_before = world.durable_digest()
    restarted = LifecycleWorld.restart(Path(raw))
    check(
        "restart rebuilds volatile adapters from unchanged durable bytes",
        restarted.durable_digest() == durable_before
        and restarted.snapshot() == started,
    )
    restarted.apply({"action": "advance-clock", "delta": 900})
    check(
        "virtual time alone has zero transition authority",
        restarted.record().state == "awaiting-callback",
    )
    restarted.apply({"action": "worker-tick"})
    check(
        "worker tick uses the production poll seam without replaying input",
        restarted.snapshot()["effects"][0]["deliveries"] == 1,
    )
    restarted.apply(
        {"action": "publish-provider-event", "kind": "result-published"}
    )
    restarted.apply({"action": "publish-callback", "kind": "result"})
    restarted.apply({"action": "close"})
    check(
        "close enters the production cleanup owner and reaches resource-free completion",
        restarted.record().state == "complete"
        and not any(restarted.snapshot()["operation"]["resources"].values())
        and restarted.resource_close_count() == 1,
        restarted.snapshot(),
    )

with tempfile.TemporaryDirectory(prefix="lifecycle-resource-restart.") as raw:
    world = LifecycleWorld.fresh(Path(raw))
    world.apply({"action": "start-worker"})
    world.apply({"action": "resource-disappears"})
    world.apply({"action": "restart-worker"})
    world.apply({"action": "reconcile"})
    check(
        "restart preserves independently disappeared process and cmux resources",
        world.process.process_status(4101, "a" * 64) == "dead"
        and world.cmux.status("sim-surface") == "missing"
        and world.record().state == "awaiting-callback",
        world.snapshot(),
    )

with tempfile.TemporaryDirectory(prefix="lifecycle-real-effect-mutation.") as raw:
    world = LifecycleWorld.fresh(Path(raw))
    world.provider = NonFakeProvider(world.provider)
    world.apply({"action": "start-worker"})
    check(
        "a non-fake provider mutation is measured instead of hidden by a constant",
        world.real_effect_counts()["provider"] == 1
        and world.external_attempt_counts()["provider"] == 1,
        {
            "real": world.real_effect_counts(),
            "attempts": world.external_attempt_counts(),
        },
    )
    try:
        world.assert_no_real_effects()
    except AssertionError:
        check("a measured real effect makes the simulator gate red", True)
    else:
        raise AssertionError("measured real provider effect unexpectedly passed")

with tempfile.TemporaryDirectory(prefix="lifecycle-identity-mutation.") as raw:
    world = LifecycleWorld.fresh(Path(raw))
    world.apply({"action": "start-worker"})
    world.apply({"action": "publish-provider-event", "kind": "result-published"})
    expect_invariant(
        "persisted expected identity catches a production-accepted wrong callback",
        "SIM-INV-IDENTITY",
        lambda: world.apply(
            {
                "action": "publish-callback",
                "expected_identity_sha256": "f" * 64,
            }
        ),
    )

with tempfile.TemporaryDirectory(prefix="lifecycle-effect-once-mutation.") as raw:
    world = LifecycleWorld.fresh(Path(raw))
    world.apply({"action": "start-worker"})
    effect_id = str(world.provider.effects()[0]["effect_id"])
    world._deliver_provider(effect_id, world.stream())
    expect_invariant(
        "persisted provider attempts catch a second external delivery invocation",
        "SIM-INV-EFFECT-ONCE",
        lambda: assert_snapshot(world.snapshot()),
    )

with tempfile.TemporaryDirectory(prefix="lifecycle-terminal-mutation.") as raw:
    world = LifecycleWorld.fresh(Path(raw))
    world.apply({"action": "start-worker"})
    world.apply({"action": "publish-provider-event", "kind": "result-published"})
    world.apply({"action": "publish-callback"})
    world.apply({"action": "close"})
    terminal = world.record()
    resurrected = replace(terminal, state="running", revision=terminal.revision + 1)
    OperationStore._write(
        world.store._operation_path("sim-owner", "sim-operation"),
        to_dict(resurrected),
    )
    world._publish_liveness()
    expect_invariant(
        "persisted terminal history catches production-record resurrection",
        "SIM-INV-TERMINAL-MONOTONIC",
        lambda: assert_snapshot(world.snapshot()),
    )

print("\nAll lifecycle simulator world tests passed.")
