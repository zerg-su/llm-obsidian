"""Hermetic deterministic world over the production Harness lifecycle core."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Mapping

from harness.callbacks import CallbackBroker
from harness.contracts import (
    CallbackEnvelope,
    EffectOutcome,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
    to_dict,
)
from harness.liveness import LivenessController, LivenessEvidence, LivenessPolicy
from harness.reconciliation import reconcile
from harness.runtime_provider_events import RuntimeProviderEventStream
from harness.runtime_session_contracts import RuntimeSessionRequest
from harness.runtime_sessions import RuntimeSessionManager
from harness.runtime_worker import _atomic_json
from harness.runtime_worker_control import RuntimeWorkerControlMixin
from harness.runtime_worker_loop import RuntimeWorkerLoopMixin
from harness.store import OperationStore
from harness.supervisor import OperationSupervisor

from lifecycle_simulator_oracle import assert_snapshot


OWNER_ID = "sim-owner"
OPERATION_ID = "sim-operation"
RUN_ID = "sim-run"
LANE_ID = "sim-lane"
SURFACE_ID = "sim-surface"
WORKSPACE_ID = "sim-workspace"
PROCESS_IDENTITY = "a" * 64
SUPERVISOR_IDENTITY = "b" * 64
ROUTING_SHA256 = "c" * 64
RESULT_SHA256 = "d" * 64
CALLBACK_IDENTITY_SHA256 = "e" * 64
EFFECT_CATEGORIES = ("provider", "model", "cmux", "network")


class SimulatedCrash(RuntimeError):
    """The volatile simulator process stopped after a durable prefix."""


class EffectAudit:
    """Durable observations made immediately before external adapter calls."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def create(cls, path: Path) -> "EffectAudit":
        audit = cls(path)
        _atomic_json(path, {"schema_version": 1, "observations": []})
        return audit

    def _read(self) -> list[dict[str, object]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("external effect audit is unavailable") from exc
        observations = value.get("observations") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or not isinstance(observations, list)
        ):
            raise RuntimeError("external effect audit is invalid")
        for index, item in enumerate(observations, start=1):
            if (
                not isinstance(item, dict)
                or item.get("sequence") != index
                or item.get("category") not in EFFECT_CATEGORIES
                or item.get("adapter") not in {"fake", "real"}
            ):
                raise RuntimeError("external effect observation is invalid")
        return observations

    def observe(self, category: str, *, real: bool) -> None:
        if category not in EFFECT_CATEGORIES:
            raise ValueError("external effect category is invalid")
        observations = self._read()
        observations.append(
            {
                "sequence": len(observations) + 1,
                "category": category,
                "adapter": "real" if real else "fake",
            }
        )
        _atomic_json(
            self.path,
            {"schema_version": 1, "observations": observations},
        )

    def counts(self, *, real_only: bool) -> dict[str, int]:
        result = {category: 0 for category in EFFECT_CATEGORIES}
        for item in self._read():
            if not real_only or item["adapter"] == "real":
                result[str(item["category"])] += 1
        return result


class CrashController:
    """Arm one named durable boundary and fail if a schedule never consumes it."""

    def __init__(self) -> None:
        self.armed = ""
        self.phase = ""
        self.observed: list[str] = []

    def arm(self, boundary: str, *, phase: str) -> None:
        if self.armed:
            raise RuntimeError("a lifecycle failpoint is already armed")
        if not boundary:
            raise ValueError("lifecycle failpoint identity is required")
        if phase not in {"before", "after"}:
            raise ValueError("crash phase must be before or after")
        self.armed = boundary
        self.phase = phase

    def observe(self, observed: str) -> None:
        self.observed.append(observed)
        phase = "before" if observed.endswith(":before") else "after"
        boundary = observed.removesuffix(":before")
        if boundary == self.armed and phase == self.phase:
            self.armed = ""
            self.phase = ""
            raise SimulatedCrash(boundary)

    def assert_consumed(self) -> None:
        if self.armed:
            raise AssertionError(f"unconsumed lifecycle failpoint: {self.armed}")


class VirtualClock:
    def __init__(self, world: "LifecycleWorld", value: float) -> None:
        self.world = world
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def advance(self, delta: float) -> None:
        if (
            not isinstance(delta, (int, float))
            or isinstance(delta, bool)
            or delta < 0
        ):
            raise ValueError("virtual clock delta must be non-negative")
        self.value += float(delta)
        self.world._write_world_state(clock=self.value)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


class FakeProvider:
    """External provider boundary that records only idempotency identities."""

    EVENT_KINDS = frozenset({"input-accepted"})

    def __init__(self, root: Path) -> None:
        self.root = root

    def deliver(
        self, effect_id: str, stream: RuntimeProviderEventStream
    ) -> dict[str, object]:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.root / f"{effect_id}.json"
        value = {
            "schema_version": 1,
            "effect_id": effect_id,
            "idempotency_key": effect_id,
            "outcome": "accepted",
            "deliveries": 1,
        }
        encoded = (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != encoded:
                raise RuntimeError("fake provider effect identity changed") from None
            return value
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        stream.accept_input()
        return value

    def effects(self) -> list[dict[str, object]]:
        if not self.root.is_dir():
            return []
        result: list[dict[str, object]] = []
        for path in sorted(self.root.glob("*.json")):
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise RuntimeError("fake provider receipt is invalid")
            result.append(value)
        return result


class FakeProcess:
    def __init__(self) -> None:
        self.process_status_value = "alive"
        self.supervisor_status_value = "alive"
        self.simulated_signals = 0

    def process_status(self, process_group: int, identity: str) -> str:
        if process_group != 4101 or identity != PROCESS_IDENTITY:
            return "unknown"
        return self.process_status_value

    def pid_status(self, pid: int, identity: str) -> str:
        if pid != 4102 or identity != SUPERVISOR_IDENTITY:
            return "unknown"
        return self.supervisor_status_value

    def capture_identity(self, pid: int, *, process_group: int = 0) -> str:
        if pid == 4101 or process_group == 4101:
            return PROCESS_IDENTITY
        if pid == 4102:
            return SUPERVISOR_IDENTITY
        return ""

    def request_guardian_signal(self, _path: Path, **_identity: object) -> None:
        self.simulated_signals += 1
        self.process_status_value = "dead"
        self.supervisor_status_value = "dead"

    def disappear(self) -> None:
        self.process_status_value = "dead"
        self.supervisor_status_value = "dead"


class FakeCmux:
    def __init__(self, effect_observer: Callable[[], None] | None = None) -> None:
        self.surface_status_value = "alive"
        self.workspace_status_value = "missing"
        self.simulated_closes = 0
        self.effect_observer = effect_observer or (lambda: None)

    def status(self, surface_id: str) -> str:
        return self.surface_status_value if surface_id == SURFACE_ID else "missing"

    def close_exact(self, surface_id: str) -> None:
        if surface_id != SURFACE_ID:
            raise RuntimeError("fake cmux surface identity changed")
        self.effect_observer()
        self.simulated_closes += 1
        self.surface_status_value = "missing"

    def workspace_status(self, workspace_id: str, _window_id: str) -> str:
        return (
            self.workspace_status_value
            if workspace_id == WORKSPACE_ID
            else "missing"
        )

    def close_workspace_exact(self, workspace_id: str, _window_id: str) -> None:
        if workspace_id != WORKSPACE_ID:
            raise RuntimeError("fake cmux workspace identity changed")
        self.effect_observer()
        self.simulated_closes += 1
        self.workspace_status_value = "missing"

    def disappear(self) -> None:
        self.surface_status_value = "missing"
        self.workspace_status_value = "missing"


class DeterministicEventSource:
    """Closed FIFO source; the scheduler owns ordering, never callbacks."""

    EVENT_KINDS = frozenset(
        {
            "turn-stopped",
            "result-published",
            "process-exited",
            "event-gap",
            "resource-closed",
        }
    )

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def publish(self, event: Mapping[str, object]) -> None:
        if event.get("kind") not in self.EVENT_KINDS:
            raise ValueError("event source kind is outside the closed vocabulary")
        self.events.append(dict(event))

    def pop(self) -> dict[str, object] | None:
        return self.events.pop(0) if self.events else None


class LifecycleWorker(RuntimeWorkerControlMixin, RuntimeWorkerLoopMixin):
    """Minimal volatile shell around the production poll/exit decision seam."""

    def __init__(self, world: "LifecycleWorld") -> None:
        self.world = world
        self.store = world.store
        self.spec = {
            "owner_id": OWNER_ID,
            "operation_id": OPERATION_ID,
            "callback_mode": "envelope",
            "run_id": RUN_ID,
        }
        self.spec_path = world.runtime_root / "runtime.json"
        self.callback_handled = bool(world.record().accepted_callback_id)
        self.provider_exited = world.process.process_status_value == "dead"
        self.exit_code = 0
        self.exit_containment_failed = False
        self._pipeline_name = ""
        self.is_custom_pipeline = False
        self.fix_transport_complete = True
        self.custom_transport_complete = True
        self.poll_seconds = 0.0
        self.wall_clock = world.clock
        self.monotonic_clock = world.clock
        self.sleeper = world.clock.sleep
        self.liveness_policy = LivenessPolicy.default()
        self.next_liveness_probe = float("inf")
        self.next_prompt_probe = float("inf")
        self.next_checkpoint_probe = float("inf")
        self.checkpoint = ""
        self.fault_observer = world.crashes.observe

    def inspect_control(self) -> None:
        return None

    def inspect_callback(self) -> None:
        event = self.world.events.pop()
        if event is not None:
            self.world._publish_provider_event(event)

    def inspect_prompt(self) -> None:
        return None

    def capture_checkpoint(self) -> None:
        return None

    def observe_provider_exit(self) -> bool:
        dead = self.world.process.process_status_value == "dead"
        if dead and not self.provider_exited:
            self.provider_exited = True
            self.world.stream().process_exited(0)
        return dead

    def restart_provider(self) -> None:
        raise AssertionError("simulator never authorizes provider restart")


class LifecycleWorld:
    """One public test-support entrypoint over real production collaborators."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.crashes = CrashController()
        self.effect_audit = EffectAudit(self.root / "simulator" / "effect-audit.json")
        self.store = OperationStore(
            self.root / "harness", fault_observer=self.crashes.observe
        )
        state = self._world_state()
        self.process = FakeProcess()
        self.cmux = FakeCmux(
            lambda: self.effect_audit.observe("cmux", real=False)
        )
        self.provider = FakeProvider(self.root / "external" / "provider-effects")
        self.events = DeterministicEventSource()
        self.clock = VirtualClock(self, float(state["clock"]))
        self.manager = RuntimeSessionManager(
            self.store,
            self.cmux,
            self.process,
            status_notifier=None,
            fault_observer=self.crashes.observe,
        )
        if self.record().state in {"exiting", "complete", "failed", "cancelled"}:
            self.process.disappear()
        if self.record().state in {"complete", "failed", "cancelled"}:
            self.cmux.disappear()
        close_receipt = self.runtime_root / "provider-events" / "resource-closed.json"
        if close_receipt.is_file() and not close_receipt.is_symlink():
            self.cmux.disappear()

    @classmethod
    def fresh(cls, root: Path) -> "LifecycleWorld":
        root = root.resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_path = root / "simulator" / "world.json"
        if state_path.exists():
            raise RuntimeError("fresh lifecycle world already exists")
        state_path.parent.mkdir(mode=0o700, parents=True)
        store = OperationStore(root / "harness")
        spec = OperationSpec(
            operation_id=OPERATION_ID,
            idempotency_key="sim-idempotency",
            kind="dispatch",
            owner_id=OWNER_ID,
            route=RuntimeRoute(
                "codex", "sol", "high", "dispatch", ROUTING_SHA256
            ),
            context_manifest="context/manifest.json",
            verification_profile="scoped",
        )
        record = store.create(spec, lane_id=LANE_ID, run_id=RUN_ID)
        _atomic_json(
            state_path,
            {
                "schema_version": 1,
                "owner_id": OWNER_ID,
                "operation_id": OPERATION_ID,
                "run_id": RUN_ID,
                "generation": 1,
                "clock": 0.0,
            },
        )
        EffectAudit.create(root / "simulator" / "effect-audit.json")
        controller = LivenessController(root / "runtime" / "liveness")
        controller.observe(
            LivenessEvidence(0.0, "alive", record.revision, record.state),
            LivenessPolicy.default(),
        )
        return cls(root)

    @classmethod
    def restart(cls, root: Path) -> "LifecycleWorld":
        return cls(root)

    @property
    def runtime_root(self) -> Path:
        return (
            self.store.root
            / "owners"
            / OWNER_ID
            / "runtime"
            / OPERATION_ID
        )

    @property
    def liveness(self) -> LivenessController:
        return LivenessController(
            self.root / "runtime" / "liveness",
            fault_observer=self.crashes.observe,
        )

    def _world_state(self) -> dict[str, object]:
        path = self.root / "simulator" / "world.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("lifecycle world identity is unavailable") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or value.get("owner_id") != OWNER_ID
            or value.get("operation_id") != OPERATION_ID
            or value.get("run_id") != RUN_ID
            or value.get("generation") != 1
            or not isinstance(value.get("clock"), (int, float))
        ):
            raise RuntimeError("lifecycle world identity changed")
        return value

    def _write_world_state(self, *, clock: float) -> None:
        _atomic_json(
            self.root / "simulator" / "world.json",
            {
                "schema_version": 1,
                "owner_id": OWNER_ID,
                "operation_id": OPERATION_ID,
                "run_id": RUN_ID,
                "generation": 1,
                "clock": float(clock),
            },
        )

    def record(self):
        return self.store.read(OWNER_ID, OPERATION_ID)

    def stream(self) -> RuntimeProviderEventStream:
        event_root = self.runtime_root / "provider-events"
        delivery = event_root / "generation-1" / "delivery" / "delivery-state.json"
        if delivery.is_file() and not delivery.is_symlink():
            return RuntimeProviderEventStream.rehydrate(event_root, 1)
        return RuntimeProviderEventStream.create(
            event_root,
            owner_id=OWNER_ID,
            operation_id=OPERATION_ID,
            run_id=RUN_ID,
            generation=1,
            process_identity=PROCESS_IDENTITY,
            workspace_id=WORKSPACE_ID,
            surface_id=SURFACE_ID,
            input_sha256="f" * 64,
        )

    def _publish_liveness(self) -> None:
        record = self.record()
        self.liveness.observe(
            LivenessEvidence(
                self.clock(),
                self.process.process_status_value,
                record.revision,
                record.state,
            ),
            LivenessPolicy.default(),
        )

    def _write_runtime_metadata(self) -> None:
        record = self.record()
        cwd = self.root / "product"
        cwd.mkdir(exist_ok=True)
        (cwd / "prompt.md").write_text("simulator\n", encoding="utf-8")
        request = RuntimeSessionRequest(
            spec=record.spec,
            lane_id=LANE_ID,
            run_id=RUN_ID,
            origin_surface="00000000-0000-4000-8000-000000000001",
            cwd=cwd,
            prompt_pointer="prompt.md",
            callback_pointer="callback.json",
            placement="split",
            callback_mode="envelope",
        )
        self.manager._write_metadata(record, request)
        metadata = self.manager._metadata(record)
        metadata.update(
            {
                "workspace_id": WORKSPACE_ID,
                "workspace_ref": "workspace:sim",
                "window_id": "sim-window",
                "window_ref": "window:sim",
                "surface_ref": "surface:sim",
            }
        )
        self.manager._write_json(self.manager._metadata_path(record), metadata)
        self.manager._write_callback_target(
            record,
            operation_id=OPERATION_ID,
            run_id=RUN_ID,
            callback_pointer="callback.json",
            generation=1,
        )

    def _start_worker(self) -> None:
        if self.record().state != "created":
            return
        supervisor = OperationSupervisor(self.store, OWNER_ID, OPERATION_ID)
        supervisor.transition("preflight")
        supervisor.transition("starting")
        supervisor.bind_resources(
            OwnedResources(
                surface_id=SURFACE_ID,
                process_group=4101,
                supervisor_pid=4102,
                process_identity=PROCESS_IDENTITY,
                supervisor_identity=SUPERVISOR_IDENTITY,
            )
        )
        stream = self.stream()
        stream.start()
        decision = stream.reserve_input()
        if decision.action != "send":
            raise RuntimeError("real delivery reducer did not reserve input")
        supervisor.effect(
            "provider-input",
            lambda _record: self._deliver_provider(decision.effect_id, stream),
        )
        supervisor.transition("running")
        supervisor.transition("awaiting-callback")
        self._write_runtime_metadata()
        self._publish_liveness()

    def _deliver_provider(
        self, effect_id: str, stream: RuntimeProviderEventStream
    ) -> dict[str, object]:
        self.effect_audit.observe(
            "provider", real=not isinstance(self.provider, FakeProvider)
        )
        return self.provider.deliver(effect_id, stream)

    def _publish_provider_event(self, action: Mapping[str, object]) -> None:
        kind = action.get("kind")
        stream = self.stream()
        if kind == "result-published":
            stream.result(str(action.get("result_sha256") or RESULT_SHA256))
        elif kind == "turn-stopped":
            stream.turn_stopped()
        elif kind == "process-exited":
            stream.process_exited(int(action.get("exit_code") or 0))
            self.process.disappear()
        elif kind == "event-gap":
            stream.event_gap(str(action.get("reason") or "source-gap"))
        elif kind == "resource-closed":
            stream.resource_closed()
            self.process.disappear()
            self.cmux.disappear()
        else:
            raise ValueError("provider event kind is outside the simulator vocabulary")

    def _publish_callback(self, action: Mapping[str, object]) -> None:
        kind = str(action.get("kind") or "result")
        payload = (
            {"verdict": str(action.get("verdict") or "approve")}
            if kind == "review"
            else {"status": "complete"}
        )
        payload_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        CallbackBroker(self.store, OWNER_ID).accept(
            CallbackEnvelope(
                str(action.get("callback_id") or "sim-callback"),
                OPERATION_ID,
                RUN_ID,
                kind,
                payload,
                payload_sha256,
            )
        )
        self._publish_liveness()

    def _close(self) -> None:
        result = self.manager.request_exit(OWNER_ID, OPERATION_ID)
        if result.action not in {"exit-requested", "terminal"}:
            raise RuntimeError(f"runtime exit was not accepted: {result.action}")
        if result.action != "terminal":
            cleaned = self.manager.cleanup(OWNER_ID, OPERATION_ID)
            if cleaned.action not in {"cleaned", "terminal"}:
                raise RuntimeError(f"runtime cleanup did not converge: {cleaned.action}")
        self._publish_liveness()

    def apply(self, action: Mapping[str, object]) -> dict[str, object]:
        name = action.get("action")
        if name == "start-worker":
            self._start_worker()
        elif name == "publish-provider-event":
            self._publish_provider_event(action)
        elif name == "reserve-effect":
            self.store.begin_effect(
                OWNER_ID, OPERATION_ID, str(action.get("effect_id") or "sim-effect")
            )
            self._publish_liveness()
        elif name == "resolve-effect":
            outcome = EffectOutcome(str(action.get("outcome") or "succeeded"))
            self.store.resolve_effect(OWNER_ID, OPERATION_ID, outcome)
            self._publish_liveness()
        elif name == "publish-callback":
            self._publish_callback(action)
        elif name in {"publish-summary", "publish-resolution"}:
            _atomic_json(
                self.runtime_root / f"{name.removeprefix('publish-')}.json",
                {
                    "schema_version": 1,
                    "operation_id": OPERATION_ID,
                    "run_id": RUN_ID,
                    "identity_sha256": str(
                        action.get("identity_sha256") or CALLBACK_IDENTITY_SHA256
                    ),
                    "status": "published",
                },
            )
        elif name == "publish-liveness":
            record = self.record()
            self.liveness.observe(
                LivenessEvidence(
                    self.clock(),
                    self.process.process_status_value,
                    int(action.get("revision", record.revision)),
                    str(action.get("state", record.state)),
                ),
                LivenessPolicy.default(),
            )
        elif name == "publish-error-latch":
            self.runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            worker = LifecycleWorker(self)
            worker.publish_error_latch(
                str(action.get("kind") or "review-drive-failed")
            )
            _atomic_json(
                self.runtime_root / "sim-error-binding.json",
                {
                    "schema_version": 1,
                    "binding_sha256": str(
                        action.get("binding_sha256") or CALLBACK_IDENTITY_SHA256
                    ),
                },
            )
        elif name == "resource-disappears":
            self.process.disappear()
            self.cmux.disappear()
        elif name == "advance-clock":
            self.clock.advance(float(action.get("delta", 0)))
        elif name == "worker-tick":
            worker = LifecycleWorker(self)
            if worker.poll_once():
                worker.settle_exit_once()
        elif name == "restart-worker":
            # Rebuild volatile adapters only; the caller can retain the returned
            # object or use LifecycleWorld.restart explicitly.
            restarted = type(self).restart(self.root)
            self.__dict__.update(restarted.__dict__)
        elif name == "reconcile":
            reconcile(self.record(), self.process, self.cmux)
        elif name == "close":
            self._close()
        elif name == "crash-at":
            boundary = str(action.get("failpoint") or "")
            phase = str(action.get("phase") or "after")
            self.crashes.arm(boundary, phase=phase)
        else:
            raise ValueError("action is outside the closed simulator vocabulary")
        snapshot = self.snapshot()
        assert_snapshot(snapshot)
        return snapshot

    def snapshot(self) -> dict[str, object]:
        record = self.record()
        operation = to_dict(record)
        operation = {
            "operation_id": record.spec.operation_id,
            "run_id": record.run_id,
            "revision": record.revision,
            "state": record.state,
            "attention_reason": (
                record.attention_reason.value if record.attention_reason else None
            ),
            "pending_effect": record.pending_effect,
            "effect_id": record.effect_id,
            "effect_outcome": record.effect_outcome.value,
            "accepted_callback_id": record.accepted_callback_id,
            "accepted_callback_sha256": record.accepted_callback_sha256,
            "resources": operation["resources"],
        }
        live = self.liveness.current_state()
        if live is None:
            raise RuntimeError("liveness state is unavailable")
        liveness = {
            "operation_revision": live.operation_revision,
            "operation_state": live.operation_state,
            "callback_submit_binding": live.callback_submit_binding,
            "callback_submit_status": live.callback_submit_status,
        }
        recovery: dict[str, object] = {}
        recovery_path = self.runtime_root / "review-drive-rearm.json"
        if recovery_path.is_file() and not recovery_path.is_symlink():
            stored = json.loads(recovery_path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                recovery = {
                    key: stored[key]
                    for key in (
                        "status",
                        "attention_revision",
                        "target_revision",
                        "resume_state",
                        "binding_sha256",
                    )
                    if key in stored
                }
                recovery["source_revision"] = stored.get("attention_revision")
        latch: dict[str, object] = {}
        latch_path = self.runtime_root / "callback-error.json"
        if latch_path.is_file() and not latch_path.is_symlink():
            stored = json.loads(latch_path.read_text(encoding="utf-8"))
            binding_path = self.runtime_root / "sim-error-binding.json"
            binding = ""
            if binding_path.is_file() and not binding_path.is_symlink():
                binding_value = json.loads(binding_path.read_text(encoding="utf-8"))
                binding = str(binding_value.get("binding_sha256") or "")
            if isinstance(stored, dict):
                binding = binding or str(recovery.get("binding_sha256") or "")
                active = not (
                    recovery.get("status") == "applied"
                    and recovery.get("target_revision") == record.revision
                )
                latch = {
                    "kind": str(stored.get("status") or ""),
                    "binding_sha256": binding,
                    "active": active,
                }
        callbacks: list[dict[str, object]] = []
        if record.accepted_callback_id:
            callbacks.append(
                {
                    "callback_id": record.accepted_callback_id,
                    "identity_sha256": record.accepted_callback_sha256,
                    "expected_identity_sha256": record.accepted_callback_sha256,
                    "accepted": True,
                }
            )
        receipts: list[dict[str, object]] = []
        close_path = self.runtime_root / "provider-events" / "resource-closed.json"
        if close_path.is_file() and not close_path.is_symlink():
            value = json.loads(close_path.read_text(encoding="utf-8"))
            receipts.append(
                {
                    "identity": str(value.get("close_id") or "resource-close"),
                    "status": "closed",
                }
            )
        return {
            "operation": operation,
            "liveness": liveness,
            "recovery": recovery,
            "error_latch": latch,
            "effects": self.provider.effects(),
            "callbacks": callbacks,
            "resource_receipts": receipts,
            "terminal_history": [record.state]
            if record.state in {"complete", "failed", "cancelled"}
            else [],
        }

    def durable_digest(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.root.rglob("*")):
            if (
                not path.is_file()
                or path.is_symlink()
                or path.name.endswith(".lock")
                or path.name.startswith(".")
            ):
                continue
            digest.update(path.relative_to(self.root).as_posix().encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def real_effect_counts(self) -> dict[str, int]:
        return self.effect_audit.counts(real_only=True)

    def external_attempt_counts(self) -> dict[str, int]:
        return self.effect_audit.counts(real_only=False)

    def assert_no_real_effects(self) -> None:
        counts = self.real_effect_counts()
        if any(counts.values()):
            raise AssertionError(f"simulator crossed a real effect boundary: {counts}")

    def resource_close_count(self) -> int:
        path = self.runtime_root / "provider-events" / "resource-closed.json"
        return int(path.is_file() and not path.is_symlink())
