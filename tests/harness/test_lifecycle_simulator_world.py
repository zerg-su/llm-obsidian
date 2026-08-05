#!/usr/bin/env python3
"""Real-core lifecycle world and deterministic production poll seams."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "harness"))

from harness.contracts import OperationSpec, RuntimeRoute  # noqa: E402
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

    def inspect_transport(self) -> None:
        self.events.append("inspect")

    def tick_observers(self) -> None:
        self.events.append("tick")

    def observe_provider_exit(self) -> bool:
        self.events.append("observe-exit")
        return True

    def mark_failed_research_runtime(self) -> None:
        self.events.append("classify-exit")

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
    and probe.events == ["inspect", "tick", "observe-exit"],
    probe.events,
)
check(
    "production settle_exit_once owns classification and restart/final decision",
    probe.settle_exit_once() is True
    and probe.events[-3:]
    == ["classify-exit", "restart-decision", "final-exit-decision"],
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

print("\nAll lifecycle simulator world tests passed.")
