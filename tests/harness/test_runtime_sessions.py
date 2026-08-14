#!/usr/bin/env python3
"""Behavioral contract for generic provider-backed runtime sessions."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.adapters.claude import (
    ClaudeDriver,
    ClaudeDriverError,
    validate_reviewer_sandbox_command,
)
from harness.adapters.claude_reviewer_statusline import (
    render as render_claude_reviewer_statusline,
)
from harness.adapters.cmux import Surface
from harness.adapters.codex import (
    CodexDriver,
    CodexDriverError,
    validate_reviewer_sandbox_command as validate_codex_reviewer_sandbox_command,
)
from harness.callbacks import CallbackBroker, CallbackTimeoutError
from harness.liveness import LivenessController, LivenessEvidence, LivenessPolicy
from harness.adapters.process import (
    ProcessAdapter,
    ProcessError,
    ProcessHandle,
    SurfaceLaunch,
)
from harness.contracts import (
    AttentionReason,
    CallbackEnvelope,
    CapabilityReport,
    EffectOutcome,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.runtime_sessions import (
    RuntimeSessionError,
    RuntimeSessionManager,
    RuntimeSessionRequest,
)
from harness.runtime_session_contracts import continuation_effect_id
from harness.runtime_session_continuation import (
    _editor_digest,
    _screen_digest,
    await_initial_input_ready,
    await_initial_input_visible,
    await_surface_transport_ready,
)

try:
    from harness.runtime_session_continuation import (
        await_initial_start_acknowledged,
    )
except ImportError:
    # Red before the RC4-E11 repair.  Reporting the absence per fixture keeps
    # every initial-input regression visible instead of collapsing the whole
    # module into one import error.
    def await_initial_start_acknowledged(*_args: object, **_kwargs: object) -> str:
        raise AssertionError(
            "await_initial_start_acknowledged is not implemented"
        )
from harness.runtime_provider_input import interactive_provider_input
from harness.runtime_provider_events import RuntimeProviderEventStream
from harness.cmux_wake_source import WakeObservation
from harness.runtime_worker import (
    load_spec as load_runtime_spec,
    provider_argv as runtime_provider_argv,
    run as run_runtime_worker,
)
from harness.runtime_provider import provider_environment
from harness.runtime_worker_contracts import RuntimeWorkerError
from harness.runtime_worker_loop import RuntimeWorkerLoopMixin
from harness.runtime_worker_execution import RuntimeWorkerExecution
from harness.store import OperationStore
from harness.supervisor import OperationSupervisor


SURFACE = "11111111-1111-1111-1111-111111111111"
ORIGIN = "22222222-2222-2222-2222-222222222222"
WORKSPACE = "33333333-3333-3333-3333-333333333333"
WINDOW = "44444444-4444-4444-4444-444444444444"
PROCESS_IDENTITY = "a" * 64
SUPERVISOR_IDENTITY = "b" * 64


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


class EventFirstClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class EventFirstSource:
    def __init__(
        self,
        clock: EventFirstClock,
        scheduled: list[tuple[float, WakeObservation]],
    ) -> None:
        self.clock = clock
        self.scheduled = scheduled
        self.waits: list[float] = []

    def wait(self, timeout: float) -> WakeObservation | None:
        self.waits.append(timeout)
        deadline = self.clock.now + timeout
        if self.scheduled and self.scheduled[0][0] <= deadline:
            at, observation = self.scheduled.pop(0)
            self.clock.now = at
            return replace(observation, observed_at=at)
        self.clock.now = deadline
        return None

    def retry(self) -> None:
        return None

    def start(self) -> bool:
        return True

    def refresh_generation(self, _generation: int) -> None:
        return None

    def close(self) -> None:
        return None


class ArtifactWakeSource:
    """Script event hints when a new exact callback artifact is visible."""

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root
        self.generations: set[int] = set()
        self.sequence = 0

    def start(self) -> bool:
        return True

    def wait(self, timeout: float) -> WakeObservation | None:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                target = json.loads(
                    (self.runtime_root / "callback-target.json").read_text(
                        encoding="utf-8"
                    )
                )
                generation = int(target["generation"])
                pointer = Path(str(target["callback_pointer"]))
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                generation, pointer = 0, Path()
            if generation not in self.generations and generation > 0 and pointer.is_file():
                self.generations.add(generation)
                self.sequence += 1
                return WakeObservation(
                    "cmux-event",
                    "agent.hook.PostToolUse",
                    self.sequence,
                    time.monotonic(),
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(0.005, remaining))

    def retry(self) -> bool:
        return True

    def refresh_generation(self, _generation: int) -> None:
        return None

    def close(self) -> None:
        return None


class EventFirstLoopProbe(RuntimeWorkerLoopMixin):
    def __init__(self) -> None:
        self.clock = EventFirstClock()
        self.monotonic_clock = self.clock
        self.wall_clock = self.clock
        self.poll_seconds = 0.1
        self.sleeper = lambda seconds: setattr(
            self.clock, "now", self.clock.now + max(0.0, seconds)
        )
        self.wake_source = EventFirstSource(
            self.clock,
            [
                (
                    0.05,
                    WakeObservation(
                        "cmux-event", "agent.hook.PostToolUse", 9, 0.05
                    ),
                )
            ],
        )
        self.next_full_reconcile = 30.0
        self.next_transport_confirmation = float("inf")
        self.next_provider_exit_probe = 0.1
        self.next_wake_retry = 0.0
        self.wake_retry_attempts = 0
        self.wake_source_disabled = False
        self.next_prompt_probe = 0.2
        self.next_checkpoint_probe = 0.5
        self.next_liveness_probe = 60.0
        self.checkpoint = ""
        self.provider_exited = False
        self.callback_handled = False
        self.liveness_policy = type("Policy", (), {"probe_seconds": 60})()
        self.stable_reads = 0
        for field in (
            "review_input_stable_reads",
            "summary_stable_reads",
            "callback_recovery_input_reads",
            "callback_recovery_reads",
            "fix_callback_stable_reads",
            "fix_result_stable_reads",
            "fix_output_stable_reads",
            "custom_callback_stable_reads",
            "custom_result_stable_reads",
            "custom_output_stable_reads",
        ):
            setattr(self, field, 0)
        self.inspections: list[float] = []
        self.prompt_ticks: list[float] = []
        self.exit_ticks: list[float] = []
        self.receipts: list[tuple[str, str]] = []
        self.source_states: list[str] = []

    def inspect_transport(self) -> None:
        self.inspections.append(self.clock.now)
        self.stable_reads += 1

    def tick_observers(self) -> None:
        if self.clock.now >= self.next_prompt_probe:
            self.prompt_ticks.append(self.clock.now)
            self.next_prompt_probe = self.clock.now + 0.2

    def observe_provider_exit(self) -> bool:
        self.exit_ticks.append(self.clock.now)
        return True

    def callback_deadline_monotonic(self, now: float) -> float:
        return float("inf")

    def record_transport_wake(
        self, observation: WakeObservation, before: object, after: object
    ) -> None:
        self.receipts.append((observation.source, "progressed" if before != after else "no-change"))

    def transport_snapshot(self) -> object:
        return tuple(self.inspections)

    def record_wake_source_state(self, observation: WakeObservation) -> None:
        self._last_wake_source_state = observation.source
        self.source_states.append(observation.source)


class LostProviderChildProbe(RuntimeWorkerLoopMixin):
    def __init__(
        self,
        store: OperationStore,
        *,
        owner_id: str,
        operation_id: str,
        run_id: str,
    ) -> None:
        self.store = store
        self.spec = {
            "owner_id": owner_id,
            "operation_id": operation_id,
            "run_id": run_id,
        }
        self.handle = type("Handle", (), {"pid": 9001})()
        self.provider_exited = False
        self.exit_code = 99
        self.recorded_exits: list[int] = []

    def record_provider_exit(self, exit_code: int) -> None:
        self.recorded_exits.append(exit_code)


def lost_provider_child_state(
    root: Path,
    name: str,
    *,
    state: str,
    effect_id: str,
    effect_outcome: EffectOutcome,
    worker_run_id: str | None = None,
) -> tuple[str, LostProviderChildProbe]:
    owner_id = f"owner-{name}"
    operation_id = f"operation-{name}"
    run_id = f"run-{name}"
    store = OperationStore(root / name)
    spec = OperationSpec(
        operation_id,
        f"key-{name}",
        "runtime-lifecycle",
        owner_id,
        RuntimeRoute(
            "claude", "claude-opus-5", "high", "reviewer-callback", "c" * 64
        ),
        "packets/runtime.json",
        "scoped",
    )
    store.create(spec, lane_id=f"lane-{name}", run_id=run_id)
    for next_state in ("preflight", "starting", "running", "cancelling"):
        store.transition(owner_id, operation_id, next_state)
    store.begin_effect(owner_id, operation_id, effect_id)
    if effect_outcome != EffectOutcome.PENDING:
        store.resolve_effect(owner_id, operation_id, effect_outcome)
    if state == "exiting":
        store.transition(owner_id, operation_id, state)
    worker = LostProviderChildProbe(
        store,
        owner_id=owner_id,
        operation_id=operation_id,
        run_id=worker_run_id or run_id,
    )
    with patch.object(os, "waitid", side_effect=ChildProcessError):
        worker.observe_provider_exit()
    return store.read(owner_id, operation_id).state, worker


with tempfile.TemporaryDirectory(prefix="expected-provider-exit.") as raw:
    exit_root = Path(raw)
    expected_exit_cases = (
        (
            "expected",
            "exiting",
            "request-exit",
            EffectOutcome.SUCCEEDED,
            None,
            "exiting",
        ),
        (
            "pending",
            "cancelling",
            "request-exit",
            EffectOutcome.PENDING,
            None,
            "attention-required",
        ),
        (
            "failed",
            "exiting",
            "request-exit",
            EffectOutcome.FAILED,
            None,
            "attention-required",
        ),
        (
            "different",
            "exiting",
            "close-surface",
            EffectOutcome.SUCCEEDED,
            None,
            "attention-required",
        ),
        (
            "nonexiting",
            "cancelling",
            "request-exit",
            EffectOutcome.SUCCEEDED,
            None,
            "attention-required",
        ),
        (
            "run-drift",
            "exiting",
            "request-exit",
            EffectOutcome.SUCCEEDED,
            "run-other",
            "attention-required",
        ),
    )
    exit_results = {
        name: lost_provider_child_state(
            exit_root,
            name,
            state=state,
            effect_id=effect_id,
            effect_outcome=effect_outcome,
            worker_run_id=worker_run_id,
        )
        for name, state, effect_id, effect_outcome, worker_run_id, _expected
        in expected_exit_cases
    }
    check(
        "lost provider child accepts only the exact durable requested-exit branch",
        all(
            exit_results[name][0] == expected
            and exit_results[name][1].provider_exited
            and exit_results[name][1].recorded_exits == [0]
            for name, _state, _effect, _outcome, _run, expected
            in expected_exit_cases
        ),
        {name: result[0] for name, result in exit_results.items()},
    )


_event_first = EventFirstLoopProbe()
check(
    "event wake performs the full durable inspection before the fallback",
    _event_first.poll_once() is True
    and _event_first.inspections == [0.05]
    and _event_first.receipts == [("cmux-event", "progressed")],
    (_event_first.inspections, _event_first.receipts),
)
check(
    "provider exit remains independently observable without a full reconcile",
    _event_first.poll_once() is True
    and _event_first.exit_ticks == [0.1]
    and _event_first.inspections == [0.05],
    (_event_first.exit_ticks, _event_first.inspections),
)
check(
    "one-read transport evidence schedules the unchanged short confirmation",
    _event_first.poll_once() is True
    and _event_first.inspections == [0.05, 0.15]
    and _event_first.stable_reads == 2
    and _event_first.receipts[-1][0] == "stability-confirmation",
    (_event_first.inspections, _event_first.stable_reads, _event_first.receipts),
)
check(
    "prompt cadence remains light-only after stable confirmation",
    _event_first.poll_once() is True
    and _event_first.prompt_ticks == [0.2]
    and _event_first.inspections == [0.05, 0.15],
    (_event_first.prompt_ticks, _event_first.inspections),
)

_fallback = EventFirstLoopProbe()
_fallback.wake_source = EventFirstSource(_fallback.clock, [])
_fallback.next_prompt_probe = float("inf")
_fallback.next_checkpoint_probe = float("inf")
_fallback.next_liveness_probe = float("inf")
_fallback.next_provider_exit_probe = float("inf")
check(
    "eventless transport retains the exact thirty-second full fallback",
    _fallback.poll_once() is True
    and _fallback.inspections == [30.0]
    and _fallback.receipts[-1][0] == "fallback-poll",
    (_fallback.inspections, _fallback.receipts),
)

_cross_session = EventFirstLoopProbe()
_cross_session.spec = {"callback_mode": "task-summary"}
_cross_session.provider_exited = True
_cross_session.wake_source = EventFirstSource(_cross_session.clock, [])
_cross_session.next_prompt_probe = float("inf")
_cross_session.next_checkpoint_probe = float("inf")
_cross_session.next_liveness_probe = float("inf")
_cross_session.next_provider_exit_probe = float("inf")
check(
    "eventless parent transport observes cross-session progress within one second",
    _cross_session.poll_once() is True
    and _cross_session.inspections == [1.0]
    and _cross_session.receipts[-1][0] == "fallback-poll",
    (_cross_session.inspections, _cross_session.receipts),
)

_missing_source = EventFirstLoopProbe()
del _missing_source.wake_source
try:
    _missing_source.poll_once()
except RuntimeWorkerError:
    _missing_source_rejected = True
else:
    _missing_source_rejected = False
check(
    "production polling requires one explicit wake source",
    _missing_source_rejected,
)

_control_wakes = EventFirstLoopProbe()
_control_wakes.wake_source = EventFirstSource(
    _control_wakes.clock,
    [
        (0.01, WakeObservation("reconnect", sequence=20)),
        (0.02, WakeObservation("cursor-gap", sequence=21)),
    ],
)
_control_wakes.next_provider_exit_probe = float("inf")
_control_wakes.next_prompt_probe = float("inf")
_control_wakes.next_checkpoint_probe = float("inf")
_control_wakes.next_liveness_probe = float("inf")
_control_wakes.poll_once()
_control_wakes.poll_once()
check(
    "reconnect and cursor gaps only request durable reconciliation",
    [source for source, _outcome in _control_wakes.receipts]
    == ["reconnect", "cursor-gap"],
    _control_wakes.receipts,
)

_degraded = EventFirstLoopProbe()
_degraded.wake_source = EventFirstSource(
    _degraded.clock,
    [(0.01, WakeObservation("degraded"))],
)
_degraded.next_provider_exit_probe = float("inf")
_degraded.next_prompt_probe = float("inf")
_degraded.next_checkpoint_probe = float("inf")
_degraded.next_liveness_probe = float("inf")
check(
    "a degraded source reconciles once and enters bounded retry without attention",
    _degraded.poll_once() is True
    and _degraded.inspections == [0.01]
    and _degraded.source_states == ["degraded"]
    and _degraded.next_wake_retry > _degraded.clock.now,
    (_degraded.inspections, _degraded.source_states, _degraded.next_wake_retry),
)

_unavailable = EventFirstLoopProbe()
_unavailable.wake_source = EventFirstSource(
    _unavailable.clock,
    [(0.01, WakeObservation("unavailable"))],
)
_unavailable.next_provider_exit_probe = float("inf")
_unavailable.next_prompt_probe = float("inf")
_unavailable.next_checkpoint_probe = float("inf")
_unavailable.next_liveness_probe = float("inf")
check(
    "an unavailable optional source degrades to deadlines without a full poll",
    _unavailable.poll_once() is True
    and not _unavailable.inspections
    and _unavailable.source_states == ["unavailable"],
    (_unavailable.inspections, _unavailable.source_states),
)


class DeadlineLoopProbe(EventFirstLoopProbe):
    def __init__(self) -> None:
        super().__init__()
        self.wake_source = EventFirstSource(self.clock, [])
        self.next_provider_exit_probe = float("inf")
        self.next_prompt_probe = float("inf")
        self.next_checkpoint_probe = 0.5
        self.next_liveness_probe = 0.3
        self.callback_due = 0.25
        self.deadline_ticks: list[str] = []

    def callback_deadline_monotonic(self, _now: float) -> float:
        return self.callback_due

    def tick_observers(self) -> None:
        if self.clock.now >= self.callback_due:
            self.deadline_ticks.append("callback")
            self.callback_due = float("inf")
        if self.clock.now >= self.next_liveness_probe:
            self.deadline_ticks.append("liveness")
            self.next_liveness_probe = float("inf")
        if not self.checkpoint and self.clock.now >= self.next_checkpoint_probe:
            self.deadline_ticks.append("checkpoint")
            self.checkpoint = "captured"


_deadlines = DeadlineLoopProbe()
_deadlines.poll_once()
_deadlines.poll_once()
_deadlines.poll_once()
check(
    "callback liveness and checkpoint deadlines stay independent of full transport",
    _deadlines.deadline_ticks == ["callback", "liveness", "checkpoint"]
    and not _deadlines.inspections,
    (_deadlines.deadline_ticks, _deadlines.inspections),
)


class ElapsedDeadlineStore:
    def read(self, _owner: str, _operation: str) -> object:
        return type("Record", (), {"deadline_at": 100.0})()


class ElapsedDeadlineProbe(EventFirstLoopProbe):
    def __init__(self) -> None:
        super().__init__()
        self.store = ElapsedDeadlineStore()
        self.spec = {"owner_id": "owner", "operation_id": "operation"}
        self.wall_clock = lambda: 200.0
        self.wake_source = EventFirstSource(self.clock, [])
        self.next_full_reconcile = 30.0
        self.next_provider_exit_probe = float("inf")
        self.next_prompt_probe = float("inf")
        self.next_checkpoint_probe = float("inf")
        self.next_liveness_probe = float("inf")

    def callback_deadline_monotonic(self, now: float) -> float:
        return RuntimeWorkerLoopMixin.callback_deadline_monotonic(self, now)

    def tick_observers(self) -> None:
        return None


_elapsed_deadline = ElapsedDeadlineProbe()
for _ in range(3):
    _elapsed_deadline.poll_once()
check(
    "elapsed callback deadlines retain a bounded blocking wait",
    len(_elapsed_deadline.wake_source.waits) == 3
    and min(_elapsed_deadline.wake_source.waits) >= 0.1,
    _elapsed_deadline.wake_source.waits,
)


class PersistentDegradedSource:
    def __init__(self, clock: EventFirstClock) -> None:
        self.clock = clock
        self.retry_count = 0

    def wait(self, timeout: float) -> WakeObservation:
        return WakeObservation("degraded", observed_at=self.clock.now)

    def retry(self) -> bool:
        self.retry_count += 1
        return False


_persistent_degraded = EventFirstLoopProbe()
_persistent_degraded.wake_source = PersistentDegradedSource(
    _persistent_degraded.clock
)
_persistent_degraded.next_provider_exit_probe = float("inf")
_persistent_degraded.next_prompt_probe = float("inf")
_persistent_degraded.next_checkpoint_probe = float("inf")
_persistent_degraded.next_liveness_probe = float("inf")
for _ in range(12):
    _persistent_degraded.poll_once()
check(
    "persistent wake degradation has bounded retries and one reconcile",
    _persistent_degraded.wake_source.retry_count <= 5
    and len(_persistent_degraded.inspections) <= 3
    and _persistent_degraded.wake_source_disabled,
    (
        _persistent_degraded.wake_source.retry_count,
        _persistent_degraded.inspections,
        _persistent_degraded.wake_source_disabled,
    ),
)

with tempfile.TemporaryDirectory(prefix="wake-binding-fallback.") as raw:
    fallback_worker = RuntimeWorkerExecution()
    fallback_worker.spec_path = Path(raw) / "launch.json"
    fallback_worker.monotonic_clock = lambda: 7.0
    fallback_worker.spec = {
        "surface_id": SURFACE,
        "owner_id": "owner-fallback",
        "operation_id": "operation-fallback",
        "run_id": "run-fallback",
    }
    fallback_worker.initial_generation = 1
    fallback_source = fallback_worker._optional_wake_source("not-a-uuid")
    check(
        "invalid optional wake identity degrades without failing worker startup",
        fallback_source.start() is False
        and fallback_source.wait(0.1).source == "unavailable",
    )

for _field in (
    "stable_reads",
    "review_input_stable_reads",
    "summary_stable_reads",
    "callback_recovery_input_reads",
    "callback_recovery_reads",
    "fix_callback_stable_reads",
    "fix_result_stable_reads",
    "fix_output_stable_reads",
    "custom_callback_stable_reads",
    "custom_result_stable_reads",
    "custom_output_stable_reads",
):
    _confirmation = EventFirstLoopProbe()
    _confirmation.stable_reads = 0
    setattr(_confirmation, _field, 1)
    check(
        f"transport confirmation inventory includes {_field}",
        _confirmation.transport_confirmation_pending(),
    )


class FakeCmux:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.opens = 0
        self.sent: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.closed_workspaces: list[tuple[str, str]] = []
        self.surface_status = "alive"
        self.surface_statuses: list[str] = []
        self.workspace_status_value = "alive"
        self.checkpoint = "checkpoint-1"
        self.submit_count = 0
        self.submits_at_last_send = 0
        self.transport_visible = False

    def open_split(self, origin_surface: str) -> Surface:
        self.events.append("surface-open")
        self.opens += 1
        self.sent.clear()
        self.submit_count = 0
        self.submits_at_last_send = 0
        self.transport_visible = False
        check("start anchors the exact origin surface", origin_surface == ORIGIN)
        return Surface(
            SURFACE,
            "surface:9",
            WORKSPACE,
            "workspace:8",
            WINDOW,
            "window:7",
        )

    def open_workspace(
        self, origin_surface: str, *, cwd: Path | None = None
    ) -> Surface:
        del cwd
        return self.open_split(origin_surface)

    def send(self, surface_id: str, text: str) -> None:
        self.events.append("provider-send")
        self.sent.append((surface_id, text))
        self.submits_at_last_send = self.submit_count
        self.transport_visible = False

    def send_key(self, surface_id: str, key: str) -> None:
        check(
            "submission uses exact surface and allowlisted Enter",
            surface_id == SURFACE
            and key == "Enter"
            and (type(self) is not FakeCmux or self.transport_visible),
        )
        self.events.append("provider-submit")
        self.submit_count += 1

    def read(self, surface_id: str) -> str:
        assert surface_id == SURFACE
        if not self.sent:
            return "❯\n›"
        self.transport_visible = True
        prompt = self.sent[-1][1]
        anchor = next((line.strip() for line in prompt.splitlines() if line.strip()), "")
        if self.submit_count == self.submits_at_last_send:
            return f"❯ {anchor}"
        return "✻ Working…(1s · ↓10 tokens)"

    def status(self, surface_id: str) -> str:
        check(
            "status targets an exact origin or owned surface",
            surface_id in {ORIGIN, SURFACE},
        )
        return (
            self.surface_statuses.pop(0)
            if self.surface_statuses
            else self.surface_status
        )

    def close_exact(self, surface_id: str) -> None:
        self.events.append("surface-close")
        self.closed.append(surface_id)

    def close_workspace_exact(
        self, workspace_id: str, window_id: str
    ) -> None:
        self.events.append("workspace-close")
        self.closed_workspaces.append((workspace_id, window_id))
        self.workspace_status_value = "missing"

    def workspace_status(
        self, workspace_id: str, window_id: str
    ) -> str:
        check(
            "workspace probe targets exact durable container",
            workspace_id == WORKSPACE and window_id == WINDOW,
        )
        if self.workspace_status_value == "drift":
            raise RuntimeError("workspace moved to another window")
        return self.workspace_status_value

    def resume_checkpoint(self, surface_id: str, runtime: str) -> str:
        check(
            "checkpoint probe binds exact surface/runtime",
            surface_id == SURFACE and runtime in {"claude", "codex"},
        )
        return self.checkpoint


class InitialReadyPort:
    def __init__(self, screens: list[str]) -> None:
        self.screens = list(screens)
        self.reads = 0

    def read(self, surface_id: str) -> str:
        assert surface_id == SURFACE
        self.reads += 1
        return self.screens.pop(0) if self.screens else ""


initial_ready_port = InitialReadyPort(
    ["", "Starting MCP servers", "› Implement {feature}"]
)
initial_ready_waits: list[float] = []
check(
    "initial provider input waits for the native idle editor",
    await_initial_input_ready(
        initial_ready_port,
        surface_id=SURFACE,
        runtime="codex",
        observation_limit=3,
        observation_interval_seconds=0.01,
        wait=initial_ready_waits.append,
    )
    and initial_ready_port.reads == 3
    and initial_ready_waits == [0.01, 0.01],
    (initial_ready_port.reads, initial_ready_waits),
)

surface_transport_port = InitialReadyPort(["", "", "zak@host project %"])
check(
    "surface command waits for a visible shell before first transport",
    await_surface_transport_ready(
        surface_transport_port,
        surface_id=SURFACE,
        observation_limit=3,
        observation_interval_seconds=0.01,
        wait=lambda _seconds: None,
    )
    and surface_transport_port.reads == 3,
    surface_transport_port.reads,
)

startup_banner_port = InitialReadyPort(
    ["Last login: Thu Aug 6", "Last login: Thu Aug 6\nzak@host project %"]
)
check(
    "surface command does not treat a startup banner as a shell prompt",
    await_surface_transport_ready(
        startup_banner_port,
        surface_id=SURFACE,
        observation_limit=2,
        observation_interval_seconds=0.01,
        wait=lambda _seconds: None,
    )
    and startup_banner_port.reads == 2,
    startup_banner_port.reads,
)


class TransientSurfacePort(InitialReadyPort):
    def __init__(self) -> None:
        super().__init__(["zak@host project >"])
        self.failures = 2

    def read(self, surface_id: str) -> str:
        if self.failures:
            self.failures -= 1
            self.reads += 1
            raise RuntimeError("surface transport is not readable yet")
        return super().read(surface_id)


transient_surface_port = TransientSurfacePort()
check(
    "surface readiness tolerates bounded transient cmux read failures",
    await_surface_transport_ready(
        transient_surface_port,
        surface_id=SURFACE,
        observation_limit=3,
        observation_interval_seconds=0.01,
        wait=lambda _seconds: None,
    )
    and transient_surface_port.reads == 3,
    transient_surface_port.reads,
)


class InitialPromptPort(InitialReadyPort):
    def __init__(self) -> None:
        super().__init__(
            [
                "\n".join(
                    (
                        "Approaching rate limits",
                        "Switch to gpt-5.6-luna for lower credit usage?",
                        "› 1. Switch to gpt-5.6-luna",
                        "2. Keep current model",
                        "3. Keep current model (never show again)",
                        "Press enter to confirm or esc to go back",
                    )
                )
            ]
        )
        self.keys: list[str] = []

    def send_key(self, surface_id: str, key: str) -> None:
        assert surface_id == SURFACE
        self.keys.append(key)
        if key == "Enter":
            self.screens.append("› Implement {feature}")


initial_prompt_port = InitialPromptPort()
check(
    "initial provider input resolves an exact safe native prompt before delivery",
    await_initial_input_ready(
        initial_prompt_port,
        surface_id=SURFACE,
        runtime="codex",
        observation_limit=4,
        observation_interval_seconds=0,
        wait=lambda _seconds: None,
    )
    and initial_prompt_port.keys == ["down", "Enter"],
    initial_prompt_port.keys,
)


class PartialInitialPromptPort(InitialPromptPort):
    def __init__(self) -> None:
        super().__init__()
        complete = self.screens.pop()
        self.screens.extend(
            (
                "\n".join(
                    (
                        "Approaching rate limits",
                        "› 1. Switch to gpt-5.6-luna",
                        "2. Keep current model",
                        "Press enter to confirm or esc to go back",
                    )
                ),
                complete,
            )
        )


partial_initial_prompt_port = PartialInitialPromptPort()
check(
    "partial native prompt repaint waits without guessing a response",
    await_initial_input_ready(
        partial_initial_prompt_port,
        surface_id=SURFACE,
        runtime="codex",
        observation_limit=5,
        observation_interval_seconds=0,
        wait=lambda _seconds: None,
    )
    and partial_initial_prompt_port.keys == ["down", "Enter"],
    partial_initial_prompt_port.keys,
)


class CodexUpdatePromptPort(InitialPromptPort):
    def __init__(self) -> None:
        super().__init__()
        self.screens = [
            "\n".join(
                (
                    "Update available! 0.146.0 -> 0.146.1",
                    "Release notes: https://github.com/openai/codex/releases/latest",
                    "› 1. Update now (runs npm install -g @openai/codex)",
                    "2. Skip",
                    "3. Skip until next version",
                    "Press enter to continue",
                )
            )
        ]


codex_update_prompt_port = CodexUpdatePromptPort()
check(
    "Codex update prompt is skipped without changing reminder policy",
    await_initial_input_ready(
        codex_update_prompt_port,
        surface_id=SURFACE,
        runtime="codex",
        observation_limit=4,
        observation_interval_seconds=0,
        wait=lambda _seconds: None,
    )
    and codex_update_prompt_port.keys == ["down", "Enter"],
    codex_update_prompt_port.keys,
)


review_prompt = "# Harness-owned review\nAxis: openai-engineering\nInspect exact HEAD."
review_pointer = Path("/private/tmp/reviewer-lane/inputs/review.md")
review_digest = hashlib.sha256(review_prompt.encode()).hexdigest()
codex_delivery = interactive_provider_input(
    "codex", review_pointer, review_prompt
)
check(
    "Codex receives one compact pointer bound to the complete prompt digest",
    "\n" not in codex_delivery
    and str(review_pointer) in codex_delivery
    and review_digest in codex_delivery
    and review_prompt not in codex_delivery,
    codex_delivery,
)
claude_delivery = interactive_provider_input(
    "claude", review_pointer, review_prompt
)
check(
    "Claude receives one compact pointer bound to the complete prompt digest",
    "\n" not in claude_delivery
    and str(review_pointer) in claude_delivery
    and review_digest in claude_delivery
    and review_prompt not in claude_delivery,
    claude_delivery,
)

collapsed_claude_paste = InitialReadyPort(
    ["❯ [Pasted text #1 +120 lines]"]
)
check(
    "initial Claude input accepts a changed collapsed editor state",
    await_initial_input_visible(
        collapsed_claude_paste,
        surface_id=SURFACE,
        runtime="claude",
        text="# Harness-owned review verification\nlong payload",
        before_editor_sha256=_editor_digest("claude", "❯"),
        observation_limit=1,
        observation_interval_seconds=0,
        wait=lambda _seconds: None,
    ),
    collapsed_claude_paste.reads,
)


class FakeProcess:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.status_value = "alive"
        self.supervisor_status_value = "alive"
        self.exit_requests: list[int] = []
        self.terminations: list[int] = []

    def prepare_surface_launch(self, **kwargs: object) -> SurfaceLaunch:
        self.events.append("launch-prepared")
        check(
            "worker receives exact callback identity",
            (
                kwargs["owner_id"],
                kwargs["operation_id"],
                kwargs["run_id"],
            )
            in {
                ("owner-1", "runtime-1", "run-1"),
                ("owner-summary", "runtime-summary", "run-summary"),
                (
                    "owner-phased-summary",
                    "runtime-phased-summary",
                    "run-phased-summary",
                ),
                ("owner-1", "runtime-workspace", "run-workspace"),
                (
                    "owner-1",
                    "runtime-workspace-surface",
                    "run-workspace-surface",
                ),
                (
                    "owner-1",
                    "runtime-workspace-drift",
                    "run-workspace-drift",
                ),
            },
        )
        root = Path(str(kwargs["state_root"]))
        return SurfaceLaunch(
            command="exec /usr/bin/python3 harness-runtime-worker.py",
            spec_path=root / "launch.json",
            ready_path=root / "ready.json",
            exit_path=root / "exit.json",
        )

    def await_surface_handle(
        self, launch: SurfaceLaunch, *, timeout_seconds: float
    ) -> ProcessHandle:
        del launch, timeout_seconds
        self.events.append("process-bound")
        return ProcessHandle(
            123,
            123,
            124,
            PROCESS_IDENTITY,
            SUPERVISOR_IDENTITY,
        )

    def process_status(self, process_group: int, identity: str) -> str:
        check(
            "process probe targets exact owned identity",
            process_group == 123 and identity == PROCESS_IDENTITY,
        )
        return self.status_value

    def pid_status(self, pid: int, identity: str) -> str:
        check(
            "supervisor probe targets exact owned identity",
            pid == 124 and identity == SUPERVISOR_IDENTITY,
        )
        return self.supervisor_status_value

    def capture_identity(self, pid: int, *, process_group: int = 0) -> str:
        if pid == 123 and process_group == 123:
            return PROCESS_IDENTITY
        if pid == 124 and process_group == 0:
            return SUPERVISOR_IDENTITY
        return ""

    def request_exit(self, process_group: int, identity: str) -> None:
        check(
            "exit receives exact owned identity",
            process_group == 123 and identity == PROCESS_IDENTITY,
        )
        self.events.append("process-exit")
        self.exit_requests.append(process_group)

    def terminate_exact(self, process_group: int, identity: str) -> None:
        check(
            "termination receives exact owned identity",
            process_group == 123 and identity == PROCESS_IDENTITY,
        )
        self.terminations.append(process_group)

    def request_guardian_signal(
        self,
        control_path: Path,
        *,
        action: str,
        operation_id: str,
        run_id: str,
        process_group: int,
        process_identity: str,
        supervisor_pid: int,
        supervisor_identity: str,
    ) -> None:
        check(
            "guardian signal binds exact durable ownership",
            control_path.name == "process-control.json"
            and action in {"request-exit", "terminate"}
            and (operation_id, run_id)
            in {
                ("runtime-1", "run-1"),
                ("runtime-workspace", "run-workspace"),
                (
                    "runtime-workspace-drift",
                    "run-workspace-drift",
                ),
                ("generic-cleanup", "generic-cleanup-run"),
                (
                    "research-fetch-deadline",
                    "research-fetch-deadline-run",
                ),
                (
                    "research-fetch-deadline-mismatch",
                    "research-fetch-deadline-mismatch-run",
                ),
                (
                    "research-synth-deadline",
                    "research-synth-deadline-run",
                ),
                (
                    "research-synth-profile-mismatch",
                    "research-synth-profile-mismatch-run",
                ),
            }
            and process_group == 123
            and process_identity == PROCESS_IDENTITY
            and supervisor_pid == 124
            and supervisor_identity == SUPERVISOR_IDENTITY,
        )
        if action == "request-exit":
            self.events.append("process-exit")
            self.exit_requests.append(process_group)
        else:
            self.terminations.append(process_group)


class ParentRecordingProcess(ProcessAdapter):
    def __init__(self) -> None:
        self.launch: SurfaceLaunch | None = None

    def prepare_surface_launch(self, **kwargs: object) -> SurfaceLaunch:
        self.launch = super().prepare_surface_launch(**kwargs)
        return self.launch

    def await_surface_handle(
        self, launch: SurfaceLaunch, *, timeout_seconds: float
    ) -> ProcessHandle:
        del launch, timeout_seconds
        return ProcessHandle(
            123,
            123,
            124,
            PROCESS_IDENTITY,
            SUPERVISOR_IDENTITY,
        )


class FakeDriver:
    def command(
        self,
        route: RuntimeRoute,
        *,
        resume: str = "",
        callback_pointer: Path | None = None,
        product_root: Path | None = None,
        session_root: Path | None = None,
    ) -> tuple[str, ...]:
        del callback_pointer, product_root, session_root
        check("driver receives the typed route", route.runtime == "claude")
        result = ("/usr/bin/claude", "--model", route.model)
        return (*result, "--resume", resume) if resume else result


class ShebangDriver:
    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def command(
        self,
        route: RuntimeRoute,
        *,
        resume: str = "",
        callback_pointer: Path | None = None,
        product_root: Path | None = None,
        session_root: Path | None = None,
    ) -> tuple[str, ...]:
        del route, resume, callback_pointer, product_root, session_root
        return (str(self.binary), "--strict-config")


def envelope(
    *,
    callback_id: str = "callback-1",
    verdict: str = "changes-requested",
) -> CallbackEnvelope:
    payload = {"verdict": verdict, "findings": []}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return CallbackEnvelope(
        callback_id,
        "runtime-1",
        "run-1",
        "review",
        payload,
        hashlib.sha256(encoded).hexdigest(),
    )


with tempfile.TemporaryDirectory(prefix="review-cleanup-product-binding.") as raw:
    cleanup_root = Path(raw).resolve()
    cleanup_scratch = cleanup_root / "review-scratch"
    cleanup_scratch.mkdir()
    (cleanup_scratch / "callbacks").mkdir()
    cleanup_product = cleanup_root / "product"
    cleanup_product.mkdir()
    cleanup_events: list[str] = []
    cleanup_store = OperationStore(cleanup_root / "store")
    cleanup_route = RuntimeRoute(
        "codex",
        "gpt-5.6-sol",
        "high",
        "reviewer-callback",
        "d" * 64,
    )
    cleanup_spec = OperationSpec(
        "review-cleanup-parent",
        "review-cleanup-key",
        "deep-review-spec",
        "review-cleanup-owner",
        cleanup_route,
        "packets/review.json",
        "scoped",
    )
    cleanup_store.create(
        cleanup_spec,
        lane_id="review-cleanup-lane",
        run_id="review-cleanup-run",
    )
    for cleanup_state in ("preflight", "starting", "running", "awaiting-callback"):
        cleanup_store.transition(
            "review-cleanup-owner",
            "review-cleanup-parent",
            cleanup_state,
        )
    OperationSupervisor(
        cleanup_store,
        "review-cleanup-owner",
        "review-cleanup-parent",
    ).bind_resources(
        OwnedResources(
            SURFACE,
            123,
            124,
            PROCESS_IDENTITY,
            SUPERVISOR_IDENTITY,
        )
    )
    cleanup_cmux = FakeCmux(cleanup_events)
    cleanup_cmux.surface_status = "missing"
    cleanup_cmux.workspace_status_value = "missing"
    cleanup_process = FakeProcess(cleanup_events)
    cleanup_process.status_value = "dead"
    cleanup_process.supervisor_status_value = "dead"
    cleanup_manager = RuntimeSessionManager(
        cleanup_store,
        cleanup_cmux,
        cleanup_process,
        {"codex": FakeDriver()},
        preflight=lambda _route, _callback_dir: CapabilityReport(
            cleanup_route, True, ("provider:profile-valid",)
        ),
    )
    cleanup_state_root = (
        cleanup_store.root
        / "owners"
        / "review-cleanup-owner"
        / "runtime"
        / "review-cleanup-parent"
    )
    cleanup_manager._write_json(
        cleanup_state_root / "session.json",
        {
            "schema_version": 1,
            "operation_id": "review-cleanup-parent",
            "run_id": "review-cleanup-run",
            "callback_mode": "envelope",
            "cwd": str(cleanup_scratch),
            "product_root": str(cleanup_product),
            "placement": "workspace",
            "workspace_id": WORKSPACE,
            "window_id": WINDOW,
            "workspace_ref": "workspace:8",
            "window_ref": "window:7",
            "surface_ref": "surface:9",
            "checkpoint": "",
        },
    )
    cleanup_manager._write_json(
        cleanup_state_root / "launch.json",
        {
            "schema_version": 1,
            "owner_id": "review-cleanup-owner",
            "operation_id": "review-cleanup-parent",
            "run_id": "review-cleanup-run",
            "runtime": "codex",
            "surface_id": SURFACE,
            "cwd": str(cleanup_scratch),
            "product_root": str(cleanup_product),
            "store_root": str(cleanup_store.root.resolve()),
            "argv": [
                "/usr/bin/codex",
                "--model",
                cleanup_route.model,
                "--cd",
                str(cleanup_scratch / "callbacks"),
            ],
        },
    )
    cleanup_manager._write_json(
        cleanup_state_root / "checkpoint.json",
        {
            "schema_version": 1,
            "operation_id": "review-cleanup-parent",
            "run_id": "review-cleanup-run",
            "runtime": "codex",
            "checkpoint": "review-cleanup-checkpoint",
        },
    )
    cleanup_manager._write_json(
        cleanup_state_root / "ready.json",
        {
            "schema_version": 1,
            "status": "ready",
            "pid": 123,
            "process_group": 123,
            "process_identity": PROCESS_IDENTITY,
            "supervisor_pid": 124,
            "supervisor_identity": SUPERVISOR_IDENTITY,
        },
    )
    cleanup_ownership = cleanup_manager.prove_durable_cleanup_ownership(
        "review-cleanup-owner",
        "review-cleanup-parent",
    )
    check(
        "sandboxed reviewer cleanup trusts the typed product binding instead of argv text",
        cleanup_ownership.process_status == "dead"
        and cleanup_ownership.supervisor_status == "dead"
        and cleanup_ownership.surface_status == "missing"
        and cleanup_ownership.workspace_status == "missing",
        cleanup_ownership,
    )


with tempfile.TemporaryDirectory(prefix="research-home-boundary.") as raw:
    research_root = Path(raw).resolve()
    research_cwd = research_root / "scratch"
    research_cwd.mkdir()
    nested_home = research_cwd / "codex-home"
    nested_home.mkdir(mode=0o700)
    research_spec = OperationSpec(
        "research-overlap",
        "research-overlap-key",
        "research-fetch",
        "owner-research",
        RuntimeRoute(
            "codex",
            "gpt-5.6-sol",
            "high",
            "research-safe",
            "e" * 64,
        ),
        "context/manifest.json",
        "research-cited-artifact",
    )
    try:
        RuntimeSessionRequest(
            spec=research_spec,
            lane_id="research-overlap-lane",
            run_id="research-overlap-run",
            origin_surface=ORIGIN,
            cwd=research_cwd,
            prompt_pointer="prompt.md",
            callback_pointer="artifact.json",
            callback_mode="research-fetch",
            runtime_home=nested_home,
            research_request_sha256="f" * 64,
            callback_wake="continue bounded research",
        )
    except RuntimeSessionError:
        check(
            "research credentials cannot overlap writable scratch",
            True,
        )
    else:
        check(
            "research credentials cannot overlap writable scratch",
            False,
        )
    disjoint_home = research_root / "codex-home"
    disjoint_home.mkdir(mode=0o700)
    try:
        RuntimeSessionRequest(
            spec=research_spec,
            lane_id="research-overlap-lane",
            run_id="research-overlap-run",
            origin_surface=ORIGIN,
            cwd=research_cwd,
            prompt_pointer="prompt.md",
            callback_pointer="artifact.json",
            callback_mode="research-fetch",
            runtime_home=disjoint_home,
            research_request_sha256="f" * 64,
            callback_wake="continue research\nrun another command",
        )
    except RuntimeSessionError:
        check("research wake is exactly one line", True)
    else:
        check("research wake is exactly one line", False)


with tempfile.TemporaryDirectory(prefix="research-parent-shebang.") as raw:
    root = Path(raw)
    cwd = root / "scratch"
    cwd.mkdir()
    (cwd / "prompt.md").write_text("bounded research", encoding="utf-8")
    runtime_home = root / "codex-home"
    runtime_home.mkdir(mode=0o700)
    binary_root = root / "bin"
    binary_root.mkdir()
    node = binary_root / "node"
    node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    node.chmod(0o755)
    codex = root / "codex.js"
    codex.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    codex.chmod(0o755)
    route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "high", "research-safe", "e" * 64
    )
    process = ParentRecordingProcess()
    manager = RuntimeSessionManager(
        OperationStore(root / "store"),
        FakeCmux([]),
        process,
        {"codex": ShebangDriver(codex)},
        preflight=lambda _route, _callback_dir: CapabilityReport(
            route, True, ("provider:profile-valid",)
        ),
    )
    request = RuntimeSessionRequest(
        OperationSpec(
            "runtime-1",
            "runtime-key-1",
            "research-fetch",
            "owner-1",
            route,
            "context/manifest.json",
            "research-cited-artifact",
        ),
        "lane-research",
        "run-1",
        ORIGIN,
        cwd,
        "prompt.md",
        "artifact.json",
        callback_mode="research-fetch",
        runtime_home=runtime_home,
        research_request_sha256="f" * 64,
        callback_wake="continue bounded research",
    )
    with patch.dict(os.environ, {"PATH": str(binary_root)}):
        manager.start(request)
    assert process.launch is not None
    launch_value = json.loads(
        process.launch.spec_path.read_text(encoding="utf-8")
    )
    loaded_launch = load_runtime_spec(process.launch.spec_path)
    relative_launch = load_runtime_spec(
        Path(os.path.relpath(process.launch.spec_path, Path.cwd()))
    )
    check(
        "worker accepts a canonical launch spec passed by relative path",
        relative_launch["ready_path"] == loaded_launch["ready_path"]
        and relative_launch["exit_path"] == loaded_launch["exit_path"],
        relative_launch,
    )
    protected_argv = runtime_provider_argv(
        loaded_launch,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    check(
        "parent pins env shebang interpreter for the protected worker",
        launch_value["runtime_interpreter"] == str(node.resolve())
        and launch_value["argv"]
        == [str(codex), "--strict-config", "bounded research"]
        and protected_argv
        == (
            str(node.resolve()),
            str(codex),
            "--strict-config",
            "bounded research",
        ),
        (launch_value, protected_argv),
    )

    ordinary_route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "high", "executor", "d" * 64
    )
    ordinary_process = ParentRecordingProcess()
    ordinary_manager = RuntimeSessionManager(
        OperationStore(root / "ordinary-store"),
        FakeCmux([]),
        ordinary_process,
        {"codex": ShebangDriver(codex)},
        preflight=lambda _route, _callback_dir: CapabilityReport(
            ordinary_route, True, ("provider:profile-valid",)
        ),
    )
    ordinary_request = RuntimeSessionRequest(
        OperationSpec(
            "ordinary-runtime",
            "ordinary-runtime-key",
            "dispatch",
            "ordinary-owner",
            ordinary_route,
            "context/manifest.json",
            "scoped",
        ),
        "ordinary-lane",
        "ordinary-run",
        ORIGIN,
        cwd,
        "prompt.md",
        "ordinary-callback.json",
        product_root=cwd,
    )
    with patch.dict(os.environ, {"PATH": str(binary_root)}):
        ordinary_manager.start(ordinary_request)
    assert ordinary_process.launch is not None
    ordinary_launch = json.loads(
        ordinary_process.launch.spec_path.read_text(encoding="utf-8")
    )
    ordinary_spec = load_runtime_spec(ordinary_process.launch.spec_path)
    ordinary_argv = runtime_provider_argv(
        ordinary_spec,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    check(
        "parent pins env shebang interpreter for ordinary providers",
        ordinary_launch["runtime_interpreter"] == str(node.resolve())
        and ordinary_spec["product_root"] == cwd.resolve()
        and ordinary_spec["initial_input_pointer"]
        == (cwd / "prompt.md").resolve()
        and ordinary_argv
        == (
            str(node.resolve()),
            str(codex),
            "--strict-config",
        ),
        (ordinary_launch, ordinary_argv),
    )
    foreign_product = root / "foreign-product"
    foreign_product.mkdir()
    mismatched_launch = dict(ordinary_launch)
    mismatched_launch["product_root"] = str(foreign_product)
    ordinary_process.launch.spec_path.write_text(
        json.dumps(mismatched_launch, sort_keys=True), encoding="utf-8"
    )
    try:
        load_runtime_spec(ordinary_process.launch.spec_path)
    except RuntimeWorkerError:
        check("ordinary runtime rejects a product root outside cwd", True)
    else:
        check("ordinary runtime rejects a product root outside cwd", False)

    for label, mutate in (
        (
            "ordinary worker rejects an omitted product root",
            lambda value: value.pop("product_root", None),
        ),
        (
            "ordinary worker rejects an empty product root",
            lambda value: value.update({"product_root": ""}),
        ),
    ):
        invalid_launch = dict(ordinary_launch)
        mutate(invalid_launch)
        ordinary_process.launch.spec_path.write_text(
            json.dumps(invalid_launch, sort_keys=True), encoding="utf-8"
        )
        try:
            load_runtime_spec(ordinary_process.launch.spec_path)
        except RuntimeWorkerError:
            check(label, True)
        else:
            check(label, False)

    parent_bound_request = RuntimeSessionRequest(
        OperationSpec(
            "ordinary-missing-product",
            "ordinary-missing-product-key",
            "dispatch",
            "ordinary-missing-owner",
            ordinary_route,
            "context/manifest.json",
            "scoped",
        ),
        "ordinary-missing-lane",
        "ordinary-missing-run",
        ORIGIN,
        cwd,
        "prompt.md",
        "ordinary-missing-callback.json",
    )
    check(
        "ordinary parent materializes the exact cwd binding",
        parent_bound_request.product_root == cwd.resolve(),
        parent_bound_request.product_root,
    )


with tempfile.TemporaryDirectory(prefix="runtime-sessions.") as raw:
    root = Path(raw)
    cwd = root / "worktree"
    cwd.mkdir()
    (cwd / "prompt.md").write_text("perform the bounded task", encoding="utf-8")
    (cwd / "continue.md").write_text("verify the bounded fix", encoding="utf-8")
    expected_claude_continuation = interactive_provider_input(
        "claude", (cwd / "continue.md").resolve(), "verify the bounded fix"
    )
    (cwd / "continue-no-checkpoint.md").write_text(
        "verify through the retained Claude process", encoding="utf-8"
    )
    (cwd / "callbacks").mkdir()
    route = RuntimeRoute(
        "claude", "fable", "high", "reviewer-readonly", "a" * 64
    )
    spec = OperationSpec(
        "runtime-1",
        "runtime-key-1",
        "runtime-lifecycle",
        "owner-1",
        route,
        "packets/runtime.json",
        "scoped",
    )
    request = RuntimeSessionRequest(
        spec,
        "lane-shared",
        "run-1",
        ORIGIN,
        cwd,
        "prompt.md",
        "callbacks/result.json",
        product_root=cwd,
    )

    incompatible_events: list[str] = []
    incompatible_store = OperationStore(root / "incompatible-store")
    incompatible = RuntimeSessionManager(
        incompatible_store,
        FakeCmux(incompatible_events),
        FakeProcess(incompatible_events),
        {"claude": FakeDriver()},
        preflight=lambda _route, _callback_dir: CapabilityReport(
            route, False, (), AttentionReason.CAPABILITY_MISMATCH
        ),
    )
    try:
        incompatible.start(request)
    except RuntimeSessionError:
        pass
    else:
        raise AssertionError("incompatible preflight must fail")
    check(
        "failed preflight has zero durable or external effects",
        not incompatible_store.list("owner-1") and incompatible_events == [],
        incompatible_events,
    )

    events: list[str] = []
    status_notifications: list[tuple[Path, str, str, str]] = []
    store = OperationStore(root / "store")
    cmux = FakeCmux(events)
    process = FakeProcess(events)
    manager = RuntimeSessionManager(
        store,
        cmux,
        process,
        {"claude": FakeDriver()},
        preflight=lambda _route, _callback_dir: CapabilityReport(
            route, True, ("provider:profile-valid",)
        ),
        status_notifier=lambda state_root, trigger_owner, trigger_operation: (
            status_notifications.append(
                (
                    state_root,
                    trigger_owner,
                    trigger_operation,
                    store.read(trigger_owner, "runtime-1").state,
                )
            )
        ),
    )
    (cwd / "callbacks" / "result.json").write_text(
        "stale callback", encoding="utf-8"
    )
    try:
        manager.start(request)
    except RuntimeSessionError as exc:
        check(
            "new runtime rejects a pre-existing callback outbox",
            str(exc) == "runtime callback pointer must be a fresh owned outbox",
            exc,
        )
    else:
        raise AssertionError("new runtime must reject a stale callback outbox")
    check(
        "stale callback rejection has no durable or external effects",
        not store.list("owner-1") and cmux.opens == 0 and events == [],
        events,
    )
    (cwd / "callbacks" / "result.json").unlink()

    seam_route = RuntimeRoute(
        "claude",
        "fable",
        "high",
        "reviewer-callback",
        "a" * 64,
    )
    seam_parent = OperationSpec(
        "runtime-expired-parent",
        "runtime-expired-parent-key",
        "review-session",
        "owner-expired-seam",
        seam_route,
        "packets/review.json",
        "scoped",
    )
    store.create(
        seam_parent,
        lane_id="lane-expired-seam",
        run_id="run-expired-parent",
    )
    OperationSupervisor(
        store,
        "owner-expired-seam",
        "runtime-expired-parent",
    ).configure_budget(
        attempt_limit=1,
        model_restart_limit=0,
        time_budget_seconds=1,
        token_limit=100,
        now=1.0,
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(
            "owner-expired-seam", "runtime-expired-parent", state
        )
    seam_child = OperationSpec(
        "runtime-expired-round",
        "runtime-expired-round-key",
        "review-round",
        "owner-expired-seam",
        seam_route,
        "packets/review.json",
        "scoped",
    )
    store.create(
        seam_child,
        lane_id="lane-expired-seam",
        run_id="run-expired-round",
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(
            "owner-expired-seam", "runtime-expired-round", state
        )
    seam_payload = {
        "verdict": "approve",
        "findings": [],
        "parent_session_operation_id": "runtime-expired-parent",
    }
    seam_encoded = json.dumps(
        seam_payload, sort_keys=True, separators=(",", ":")
    ).encode()
    seam_envelope = CallbackEnvelope(
        "runtime-expired-callback",
        "runtime-expired-round",
        "run-expired-round",
        "review",
        seam_payload,
        hashlib.sha256(seam_encoded).hexdigest(),
    )
    try:
        manager.accept_callback(seam_envelope)
    except CallbackTimeoutError:
        seam_parent_record = store.read(
            "owner-expired-seam", "runtime-expired-parent"
        )
        seam_child_record = store.read(
            "owner-expired-seam", "runtime-expired-round"
        )
        check(
            "coordinator callback seam arbitrates the exact reviewer parent",
            seam_parent_record.state == "attention-required"
            and seam_parent_record.attention_reason
            == AttentionReason.CALLBACK_TIMEOUT
            and seam_child_record.state == "awaiting-callback"
            and not seam_child_record.accepted_callback_id,
            (seam_parent_record, seam_child_record),
        )
    else:
        check(
            "coordinator callback seam arbitrates the exact reviewer parent",
            False,
        )

    summary_spec = OperationSpec(
        "runtime-summary",
        "runtime-summary-key",
        "runtime-lifecycle",
        "owner-summary",
        route,
        "packets/runtime-summary.json",
        "scoped",
    )
    summary_request = RuntimeSessionRequest(
        summary_spec,
        "lane-summary",
        "run-summary",
        ORIGIN,
        cwd,
        "prompt.md",
        "callbacks/summary-result.json",
        product_root=cwd,
        callback_mode="task-summary",
        task_summary_pointer=".task-summary.json",
    )
    (cwd / ".task-summary.json").write_text(
        "stale task summary", encoding="utf-8"
    )
    try:
        manager.start(summary_request)
    except RuntimeSessionError as exc:
        check(
            "new task runtime rejects a pre-existing summary handoff",
            str(exc) == "task summary source must be a fresh owned handoff",
            exc,
        )
    else:
        raise AssertionError("new task runtime must reject a stale summary")
    check(
        "stale summary rejection has no durable or external effects",
        not store.list("owner-summary") and cmux.opens == 0 and events == [],
        events,
    )
    (cwd / ".task-summary.json").unlink()
    summary_started = manager.start(summary_request)
    (cwd / ".task-summary.json").write_text(
        "late durable task summary", encoding="utf-8"
    )
    summary_replayed = manager.start(summary_request)
    check(
        "idempotent task replay accepts its existing owned summary handoff",
        summary_started.record.state == "awaiting-callback"
        and summary_replayed.action == "already-started"
        and summary_replayed.record == summary_started.record
        and cmux.opens == 1,
    )
    (cwd / ".task-summary.json").unlink()
    events.clear()
    cmux.opens = 0

    phased_parent = OperationSpec(
        "runtime-phased-summary",
        "runtime-phased-summary-key",
        "dispatch",
        "owner-phased-summary",
        route,
        "packets/runtime-summary.json",
        "scoped",
    )
    phased_child = OperationSpec(
        "runtime-phased-reproduce",
        "runtime-phased-reproduce-key",
        "pipeline-model-step",
        "owner-phased-summary",
        route,
        "packets/runtime-summary.json",
        "scoped",
    )
    store.create(
        phased_child,
        lane_id="lane-phased-summary",
        run_id="run-phased-reproduce",
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(
            "owner-phased-summary",
            phased_child.operation_id,
            state,
        )
    phased_request = RuntimeSessionRequest(
        phased_parent,
        "lane-phased-summary",
        "run-phased-summary",
        ORIGIN,
        cwd,
        "prompt.md",
        "callbacks/phased-result.json",
        product_root=cwd,
        callback_mode="task-summary",
        task_summary_pointer=".task-summary.json",
        initial_callback_operation_id=phased_child.operation_id,
        initial_callback_run_id="run-phased-reproduce",
    )
    phased_started = manager.start(phased_request)
    phased_target = json.loads(
        (
            store.root
            / "owners"
            / "owner-phased-summary"
            / "runtime"
            / phased_parent.operation_id
            / "callback-target.json"
        ).read_text(encoding="utf-8")
    )
    check(
        "task-summary runtime can start on an exact phase child target",
        phased_started.callback_pointer
        == "callbacks/phased-result.json"
        and phased_target["operation_id"]
        == phased_child.operation_id
        and phased_target["run_id"] == "run-phased-reproduce"
        and phased_target["generation"] == 1,
        (phased_started, phased_target),
    )
    events.clear()
    cmux.opens = 0

    route_reports = manager.preflight_routes(
        (
            (route, cwd / "callbacks", ORIGIN),
            (route, cwd / "callbacks", ORIGIN),
        )
    )
    check(
        "global route preflight proves every exact origin before first start",
        len(route_reports) == 2
        and all(report.compatible for report in route_reports)
        and all(
            "cmux:origin-alive" in report.capabilities
            for report in route_reports
        )
        and not store.list("owner-1")
        and cmux.opens == 0,
    )

    def prepare_surface(result: object) -> None:
        events.append("surface-prepared")
        check(
            "preparation sees durable exact surface before prompt",
            result.record.resources.surface_id == SURFACE
            and result.surface_ref == "surface:9"
            and result.workspace_id == WORKSPACE
            and result.workspace_ref == "workspace:8"
            and result.window_id == WINDOW
            and result.window_ref == "window:7",
        )

    started = manager.start(request, on_surface_opened=prepare_surface)
    transport_receipt = json.loads(
        (
            store.root
            / "owners"
            / "owner-1"
            / "runtime"
            / "runtime-1"
            / "surface-transport.json"
        ).read_text(encoding="utf-8")
    )
    check(
        "start persists caller-provided lane and run identities",
        started.operation_id == "runtime-1"
        and started.lane_id == "lane-shared"
        and started.run_id == "run-1",
    )
    check(
        "surface preparation happens before provider prompt",
        events.index("surface-open")
        < events.index("surface-prepared")
        < events.index("provider-send"),
        events,
    )
    check(
        "surface transport durably records command submission",
        transport_receipt
        == {"schema_version": 1, "status": "command-submitted"},
        transport_receipt,
    )
    check(
        "start binds exact surface and PGID then awaits callback",
        started.record.state == "awaiting-callback"
        and started.record.resources.surface_id == SURFACE
        and started.record.resources.process_group == 123
        and started.record.resources.process_identity == PROCESS_IDENTITY
        and started.record.resources.supervisor_identity
        == SUPERVISOR_IDENTITY
        and started.callback_pointer == "callbacks/result.json",
        started,
    )
    check(
        "status notifications publish preflight starting and final exact owner states",
        status_notifications
        == [
            (store.root, "owner-1", "runtime-1", "preflight"),
            (store.root, "owner-1", "runtime-1", "starting"),
            (store.root, "owner-1", "runtime-1", "awaiting-callback"),
        ]
        and all(
            state_root == store.root
            for (
                state_root,
                _trigger_owner,
                _trigger_operation,
                _state,
            ) in status_notifications
        )
        and status_notifications[-1]
        == (store.root, "owner-1", "runtime-1", "awaiting-callback"),
        status_notifications,
    )
    checkpoint_root = (
        store.root
        / "owners"
        / "owner-1"
        / "runtime"
        / "runtime-1"
    )
    checkpoint_path = checkpoint_root / "checkpoint.json"
    launch_path = checkpoint_root / "launch.json"
    checkpoint_value = {
        "schema_version": 1,
        "operation_id": "runtime-1",
        "run_id": "run-1",
        "runtime": "claude",
        "checkpoint": "checkpoint-1",
    }
    checkpoint_path.write_text(
        json.dumps(checkpoint_value) + "\n", encoding="utf-8"
    )
    launch_value = {
        "schema_version": 1,
        "owner_id": "owner-1",
        "operation_id": "runtime-1",
        "run_id": "run-1",
        "runtime": "claude",
        "surface_id": SURFACE,
        "argv": ["/usr/bin/claude", "--model", "fable"],
    }
    launch_path.write_text(json.dumps(launch_value) + "\n", encoding="utf-8")
    effects_before_hydration = tuple(events)
    hydrated = manager.hydrate_durable_checkpoint(
        "owner-1", "runtime-1", "lane-shared"
    )
    hydrated_replay = manager.hydrate_durable_checkpoint(
        "owner-1", "runtime-1", "lane-shared"
    )
    check(
        "durable checkpoint hydration binds the exact parent session",
        hydrated.action == "checkpoint-hydrated"
        and hydrated.checkpoint == "checkpoint-1"
        and len(hydrated.checkpoint_sha256) == 64
        and hydrated_replay.checkpoint_sha256
        == hydrated.checkpoint_sha256
        and tuple(events) == effects_before_hydration,
    )
    for label, mutate in (
        (
            "lane mismatch",
            lambda: manager.hydrate_durable_checkpoint(
                "owner-1", "runtime-1", "lane-foreign"
            ),
        ),
        (
            "stale run",
            lambda: (
                checkpoint_path.write_text(
                    json.dumps({**checkpoint_value, "run_id": "run-stale"})
                    + "\n",
                    encoding="utf-8",
                ),
                manager.hydrate_durable_checkpoint(
                    "owner-1", "runtime-1", "lane-shared"
                ),
            )[-1],
        ),
        (
            "model mismatch",
            lambda: (
                launch_path.write_text(
                    json.dumps(
                        {
                            **launch_value,
                            "argv": [
                                "/usr/bin/claude",
                                "--model",
                                "foreign-model",
                            ],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                ),
                manager.hydrate_durable_checkpoint(
                    "owner-1", "runtime-1", "lane-shared"
                ),
            )[-1],
        ),
    ):
        try:
            mutate()
        except RuntimeSessionError:
            check(f"durable checkpoint rejects {label}", True)
        else:
            check(f"durable checkpoint rejects {label}", False)
        checkpoint_path.write_text(
            json.dumps(checkpoint_value) + "\n", encoding="utf-8"
        )
        launch_path.write_text(
            json.dumps(launch_value) + "\n", encoding="utf-8"
        )
    checkpoint_path.unlink()
    try:
        manager.hydrate_durable_checkpoint(
            "owner-1", "runtime-1", "lane-shared"
        )
    except RuntimeSessionError:
        check("durable checkpoint rejects missing evidence", True)
    else:
        check("durable checkpoint rejects missing evidence", False)
    foreign_checkpoint = root / "foreign-checkpoint.json"
    foreign_checkpoint.write_text(
        json.dumps(checkpoint_value) + "\n", encoding="utf-8"
    )
    checkpoint_path.symlink_to(foreign_checkpoint)
    try:
        manager.hydrate_durable_checkpoint(
            "owner-1", "runtime-1", "lane-shared"
        )
    except RuntimeSessionError:
        check("durable checkpoint rejects symlink evidence", True)
    else:
        check("durable checkpoint rejects symlink evidence", False)
    checkpoint_path.unlink()
    checkpoint_path.write_text(
        json.dumps(checkpoint_value) + "\n", encoding="utf-8"
    )
    resumable_parent = store.read("owner-1", "runtime-1")
    try:
        manager.cleanup("owner-1", "runtime-1")
    except RuntimeSessionError:
        pass
    else:
        raise AssertionError(
            "an awaiting-callback parent must not enter terminal surface cleanup"
        )
    check(
        "resumable parent retains its exact surface without closing any surface",
        store.read("owner-1", "runtime-1") == resumable_parent
        and resumable_parent.resources.surface_id == SURFACE
        and cmux.closed == [],
        cmux.closed,
    )
    duplicate = manager.start(request)
    check(
        "idempotent repeated start never opens a duplicate surface",
        duplicate.record == started.record
        and duplicate.action == "already-started"
        and duplicate.process_status == "alive"
        and duplicate.surface_status == "alive"
        and duplicate.checkpoint == "checkpoint-1"
        and cmux.opens == 1,
    )
    process.status_value = "dead"
    dead_duplicate = manager.start(request)
    check(
        "idempotent start exposes non-live replay status",
        dead_duplicate.action == "already-started"
        and dead_duplicate.process_status == "dead"
        and dead_duplicate.surface_status == "alive"
        and not dead_duplicate.checkpoint,
    )
    process.status_value = "alive"
    process.supervisor_status_value = "dead"
    supervisor_dead_duplicate = manager.start(request)
    check(
        "idempotent start preserves supervisor attention status",
        supervisor_dead_duplicate.action == "attention-required"
        and supervisor_dead_duplicate.process_status == "alive"
        and supervisor_dead_duplicate.surface_status == "alive"
        and supervisor_dead_duplicate.checkpoint == "checkpoint-1",
    )
    process.supervisor_status_value = "alive"
    (cwd / "callbacks" / "other").mkdir()
    try:
        manager.start(replace(request, callback_pointer="callbacks/other/result.json"))
    except RuntimeSessionError:
        pass
    else:
        raise AssertionError("mutable duplicate runtime identity must fail")
    check(
        "idempotent start rejects changed runtime pointers",
        cmux.opens == 1,
    )

    accepted = manager.accept_callback(envelope())
    duplicate_callback = manager.accept_callback(envelope())
    check(
        "provider callback is accepted exactly once",
        accepted.record.state == "verifying"
        and accepted.record.accepted_callback_id == "callback-1"
        and duplicate_callback.action == "callback-duplicate",
    )
    (cwd / "callbacks" / "result.json").write_text(
        "late durable callback", encoding="utf-8"
    )
    replayed_callback = manager.start(request)
    check(
        "idempotent replay accepts its existing owned callback outbox",
        replayed_callback.action == "already-started"
        and replayed_callback.record == accepted.record
        and cmux.opens == 1,
    )
    (cwd / "callbacks" / "result.json").unlink()
    callback_target = cwd / "callbacks" / "callback-target"
    callback_target.write_text("late durable callback", encoding="utf-8")
    (cwd / "callbacks" / "result.json").symlink_to(callback_target)
    try:
        manager.start(request)
    except RuntimeSessionError:
        pass
    else:
        raise AssertionError("idempotent replay must reject a symlink outbox")
    check(
        "idempotent replay accepts only a regular non-symlink outbox",
        cmux.opens == 1,
    )
    (cwd / "callbacks" / "result.json").unlink()
    callback_target.unlink()
    timed_out_parent = store.read("owner-1", "runtime-1")
    store.save(
        replace(
            timed_out_parent,
            deadline_at=1.0,
            revision=timed_out_parent.revision + 1,
        ),
        expected_revision=timed_out_parent.revision,
    )
    store.transition(
        "owner-1",
        "runtime-1",
        "attention-required",
        reason=AttentionReason.CALLBACK_TIMEOUT,
    )
    with patch("harness.runtime_session_launch.time", return_value=10.0):
        rearmed_parent = manager.rearm_callback_timeout(
            "owner-1", "runtime-1"
        )
    check(
        "runtime rearms accepted callback state and budget in one boundary",
        rearmed_parent.record.state == "awaiting-callback"
        and rearmed_parent.record.deadline_at
        == 10.0 + request.time_budget_seconds,
    )
    before_continuation = store.read("owner-1", "runtime-1")
    continuation_started = before_continuation.deadline_at + 5.0
    with patch(
        "harness.supervisor.time", return_value=continuation_started
    ):
        continued = manager.continue_session(
            "owner-1", "runtime-1", "checkpoint-1", "continue.md"
        )
    check(
        "continuation reuses exact surface with a fresh bounded attempt",
        continued.record.state == "running"
        and continued.record.attempt == before_continuation.attempt + 1
        and continued.record.deadline_at
        == continuation_started + request.time_budget_seconds
        and cmux.opens == 1
        and cmux.sent[-1] == (SURFACE, expected_claude_continuation),
        cmux.sent,
    )
    checkpointless_cwd = root / "checkpointless-scratch"
    checkpointless_cwd.mkdir()
    (checkpointless_cwd / "callbacks").mkdir()
    (checkpointless_cwd / "prompt.md").write_text("review", encoding="utf-8")
    (checkpointless_cwd / "continue.md").write_text(
        "verify through the retained Claude process", encoding="utf-8"
    )
    expected_checkpointless_continuation = interactive_provider_input(
        "claude",
        (checkpointless_cwd / "continue.md").resolve(),
        "verify through the retained Claude process",
    )
    checkpointless_product = root / "checkpointless-product"
    checkpointless_product.mkdir()
    checkpointless_route = RuntimeRoute(
        "claude", "fable", "high", "reviewer-callback", "a" * 64
    )
    checkpointless_spec = OperationSpec(
        "runtime-1",
        "runtime-key-1",
        "review-session",
        "owner-1",
        checkpointless_route,
        "packets/review.json",
        "scoped",
    )
    checkpointless_events: list[str] = []
    checkpointless_cmux = FakeCmux(checkpointless_events)
    checkpointless_cmux.checkpoint = ""
    checkpointless_manager = RuntimeSessionManager(
        OperationStore(root / "checkpointless-store"),
        checkpointless_cmux,
        FakeProcess(checkpointless_events),
        {"claude": FakeDriver()},
        preflight=lambda _route, _callback_dir: CapabilityReport(
            checkpointless_route, True, ("provider:profile-valid",)
        ),
    )
    checkpointless_manager.start(
        RuntimeSessionRequest(
            checkpointless_spec,
            "lane-shared",
            "run-1",
            ORIGIN,
            checkpointless_cwd,
            "prompt.md",
            "callbacks/result.json",
            product_root=checkpointless_product,
        )
    )
    checkpointless = checkpointless_manager.continue_session(
        "owner-1", "runtime-1", "", "continue.md"
    )
    check(
        "live Claude reviewer continues on exact ownership without a checkpoint",
        checkpointless.record.state == "running"
        and checkpointless.checkpoint == ""
        and checkpointless_cmux.sent[-1]
        == (SURFACE, expected_checkpointless_continuation),
    )

    class CodexContinuationCmux(FakeCmux):
        def read(self, surface_id: str) -> str:
            assert surface_id == SURFACE
            if not self.sent:
                return "›"
            self.transport_visible = True
            prompt = self.sent[-1][1]
            anchor = next(
                (line.strip() for line in prompt.splitlines() if line.strip()),
                "",
            )
            if self.submit_count == self.submits_at_last_send:
                return f"› {anchor}"
            return "• Working (1s • esc to interrupt)"

    codex_continue_root = root / "codex-continuation"
    codex_continue_root.mkdir()
    codex_continue_product = root / "codex-continuation-product"
    codex_continue_product.mkdir()
    (codex_continue_root / "callbacks").mkdir()
    (codex_continue_root / "prompt.md").write_text(
        "start Codex", encoding="utf-8"
    )
    codex_continue_prompt = (
        "# Harness-owned review verification\n"
        "Axis: openai-engineering\n"
        "Inspect the exact fixed HEAD."
    )
    codex_continue_path = codex_continue_root / "continue.md"
    codex_continue_path.write_text(
        codex_continue_prompt, encoding="utf-8"
    )
    codex_continue_route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "high", "reviewer-callback", "c" * 64
    )
    codex_continue_spec = OperationSpec(
        "runtime-1",
        "runtime-key-1",
        "review-session",
        "owner-1",
        codex_continue_route,
        "packets/review.json",
        "scoped",
    )
    codex_continue_cmux = CodexContinuationCmux([])
    codex_continue_manager = RuntimeSessionManager(
        OperationStore(root / "codex-continuation-store"),
        codex_continue_cmux,
        FakeProcess([]),
        {"codex": CodexDriver(Path("/usr/bin/codex"))},
        preflight=lambda _route, _callback_dir: CapabilityReport(
            codex_continue_route, True, ("provider:profile-valid",)
        ),
    )
    codex_continue_manager.start(
        RuntimeSessionRequest(
            codex_continue_spec,
            "lane-shared",
            "run-1",
            ORIGIN,
            codex_continue_root,
            "prompt.md",
            "callbacks/result.json",
            product_root=codex_continue_product,
        )
    )
    codex_continued = codex_continue_manager.continue_session(
        "owner-1", "runtime-1", "checkpoint-1", "continue.md"
    )
    expected_codex_continuation = interactive_provider_input(
        "codex", codex_continue_path.resolve(), codex_continue_prompt
    )
    check(
        "Codex continuation stays one compact contract pointer",
        codex_continued.record.state == "running"
        and codex_continue_cmux.sent[-1]
        == (SURFACE, expected_codex_continuation)
        and sum(
            text == expected_codex_continuation
            for _surface, text in codex_continue_cmux.sent
        )
        == 1
        and "\n" not in expected_codex_continuation,
        codex_continue_cmux.sent,
    )

    class RetainedPromptCmux(FakeCmux):
        def __init__(self, events: list[str], *, acknowledge: bool) -> None:
            super().__init__(events)
            self.acknowledge = acknowledge

        def read(self, surface_id: str) -> str:
            assert surface_id == SURFACE
            if not self.sent:
                return "❯\n›"
            prompt = self.sent[-1][1]
            anchor = next(
                (line.strip() for line in prompt.splitlines() if line.strip()),
                "",
            )
            if self.acknowledge and self.submit_count > self.submits_at_last_send:
                return "✻ Working…(1s · ↓10 tokens)"
            return f"❯ {anchor}"

    stuck_root = root / "stuck-continuation"
    stuck_root.mkdir()
    (stuck_root / "callbacks").mkdir()
    (stuck_root / "prompt.md").write_text("review", encoding="utf-8")
    (stuck_root / "continue.md").write_text(
        "# Harness-owned review verification\nInspect exact HEAD.",
        encoding="utf-8",
    )
    expected_stuck_continuation = interactive_provider_input(
        "claude",
        (stuck_root / "continue.md").resolve(),
        "# Harness-owned review verification\nInspect exact HEAD.",
    )
    stuck_store = OperationStore(root / "stuck-store")
    stuck_cmux = RetainedPromptCmux([], acknowledge=False)
    stuck_manager = RuntimeSessionManager(
        stuck_store,
        stuck_cmux,
        FakeProcess([]),
        {"codex": FakeDriver()},
        preflight=lambda _route, _callback_dir: CapabilityReport(
            route, True, ("provider:profile-valid",)
        ),
    )
    stuck_manager.start(
        replace(
            request,
            cwd=stuck_root,
            product_root=stuck_root,
        )
    )
    try:
        stuck_manager.continue_session(
            "owner-1", "runtime-1", "checkpoint-1", "continue.md"
        )
    except RuntimeSessionError:
        pass
    else:
        raise AssertionError("unacknowledged continuation must fail closed")
    stuck_record = stuck_store.read("owner-1", "runtime-1")
    stuck_receipts = list(
        (stuck_root.parent / "stuck-store" / "owners" / "owner-1" / "runtime" / "runtime-1" / "continuation-deliveries").glob("*.json")
    )
    check(
        "transport success without provider activity becomes typed attention",
        stuck_record.state == "attention-required"
        and stuck_record.attention_reason
        == AttentionReason.CONTINUATION_SUBMIT_UNCONFIRMED
        and stuck_record.effect_outcome == EffectOutcome.FAILED
        and len(stuck_receipts) == 1
        and json.loads(stuck_receipts[0].read_text(encoding="utf-8"))["status"]
        == "unconfirmed"
        and sum(text == expected_stuck_continuation for _surface, text in stuck_cmux.sent)
        == 1,
    )

    legacy_root = root / "legacy-continuation"
    legacy_root.mkdir()
    (legacy_root / "callbacks").mkdir()
    (legacy_root / "prompt.md").write_text("review", encoding="utf-8")
    legacy_prompt = "# Harness-owned review verification\nInspect exact HEAD."
    (legacy_root / "continue.md").write_text(legacy_prompt, encoding="utf-8")
    legacy_store = OperationStore(root / "legacy-store")
    legacy_cmux = RetainedPromptCmux([], acknowledge=True)
    legacy_manager = RuntimeSessionManager(
        legacy_store,
        legacy_cmux,
        FakeProcess([]),
        {"codex": FakeDriver()},
        preflight=lambda _route, _callback_dir: CapabilityReport(
            route, True, ("provider:profile-valid",)
        ),
    )
    legacy_manager.start(
        replace(request, cwd=legacy_root, product_root=legacy_root)
    )
    legacy_record = legacy_store.read("owner-1", "runtime-1")
    legacy_store.save(
        replace(
            legacy_record,
            effect_id=continuation_effect_id(legacy_prompt),
            effect_outcome=EffectOutcome.SUCCEEDED,
            revision=legacy_record.revision + 1,
        ),
        expected_revision=legacy_record.revision,
    )
    legacy_cmux.sent.append((SURFACE, legacy_prompt))
    legacy_cmux.submits_at_last_send = legacy_cmux.submit_count
    try:
        legacy_manager.continue_session(
            "owner-1", "runtime-1", "checkpoint-1", "continue.md"
        )
    except RuntimeSessionError:
        pass
    else:
        raise AssertionError("legacy continuation without a baseline must fail closed")
    legacy = legacy_store.read("owner-1", "runtime-1")
    check(
        "legacy false-success continuation without durable baseline needs attention",
        legacy.state == "attention-required"
        and legacy.attention_reason
        == AttentionReason.CONTINUATION_SUBMIT_UNCONFIRMED
        and sum(text == legacy_prompt for _surface, text in legacy_cmux.sent) == 1
        and legacy_cmux.submit_count == legacy_cmux.submits_at_last_send,
        (legacy, legacy_cmux.sent, legacy_cmux.submit_count, legacy_cmux.submits_at_last_send),
    )

    class SimulatedContinuationCrash(BaseException):
        pass

    class RetryCrashCmux(FakeCmux):
        def read(self, surface_id: str) -> str:
            assert surface_id == SURFACE
            if not self.sent:
                return "❯"
            anchor = next(
                (
                    line.strip()
                    for line in self.sent[-1][1].splitlines()
                    if line.strip()
                ),
                "",
            )
            return f"❯ {anchor}"

        def send_key(self, surface_id: str, key: str) -> None:
            super().send_key(surface_id, key)
            if self.submit_count == 2:
                raise SimulatedContinuationCrash()

    class ReplayActivityCmux(FakeCmux):
        def read(self, surface_id: str) -> str:
            assert surface_id == SURFACE
            return "✻ Resuming…(1s · ↓10 tokens)"

    class RetryReceiptCrashCmux(FakeCmux):
        def read(self, surface_id: str) -> str:
            assert surface_id == SURFACE
            if not self.sent:
                return "❯"
            anchor = next(
                (
                    line.strip()
                    for line in self.sent[-1][1].splitlines()
                    if line.strip()
                ),
                "",
            )
            return f"❯ {anchor}"

    retry_child_spec = OperationSpec(
        "retry-round-1",
        "retry-round-key-1",
        "review-round",
        "owner-1",
        route,
        "packets/retry-round.json",
        "scoped",
    )
    store.create(
        retry_child_spec,
        lane_id="lane-shared",
        run_id="retry-round-run-1",
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition("owner-1", "retry-round-1", state)
    retry_parent = store.read("owner-1", "runtime-1")
    retry_liveness = LivenessController(
        manager._state_root(retry_parent) / "liveness"
    )
    retry_liveness.observe(
        LivenessEvidence(
            observed_at=time.time(),
            process_status="alive",
            operation_revision=retry_parent.revision,
            operation_state=retry_parent.state,
        ),
        LivenessPolicy.default(),
    )
    seeded_retry_state = retry_liveness.current_state()
    check(
        "continuation retry fixture starts with one unused recovery budget",
        seeded_retry_state is not None
        and seeded_retry_state.nudge_count == 0
        and not seeded_retry_state.callback_submit_binding,
        seeded_retry_state,
    )
    retry_cmux = RetryCrashCmux([])
    manager.cmux = retry_cmux
    try:
        manager.continue_same_session_round(
            "owner-1",
            "runtime-1",
            "checkpoint-1",
            "continue.md",
            "retry-round-1",
            "retry-round-run-1",
            "callbacks/retry-round.json",
        )
    except SimulatedContinuationCrash:
        pass
    else:
        raise AssertionError("retry crash seam must stop after the second Enter")
    retry_target_path = manager._callback_target_path(
        store.read("owner-1", "runtime-1")
    )
    retry_target_sha256 = hashlib.sha256(
        retry_target_path.read_bytes()
    ).hexdigest()
    retry_identity = {
        "operation_id": "retry-round-1",
        "run_id": "retry-round-run-1",
        "lane_id": "lane-shared",
        "generation": 2,
        "target_sha256": retry_target_sha256,
        "expected_operation_id": "retry-round-1",
        "expected_run_id": "retry-round-run-1",
        "expected_lane_id": "lane-shared",
        "expected_generation": 2,
        "expected_target_sha256": retry_target_sha256,
    }
    retry_binding = hashlib.sha256(
        json.dumps(
            retry_identity, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    crash_state = retry_liveness.current_state()
    retry_receipt = next(
        (
            manager._state_root(store.read("owner-1", "runtime-1"))
            / "continuation-deliveries"
        ).glob("*.json")
    )
    check(
        "continuation retry crash preserves the worker generation binding",
        crash_state is not None
        and crash_state.callback_submit_binding == retry_binding
        and crash_state.callback_submit_status == "reserved"
        and json.loads(retry_receipt.read_text(encoding="utf-8"))["status"]
        == "submit-retry-reserved",
        crash_state,
    )
    replay_cmux = ReplayActivityCmux([])
    manager.cmux = replay_cmux
    resumed = manager.continue_same_session_round(
        "owner-1",
        "runtime-1",
        "checkpoint-1",
        "continue.md",
        "retry-round-1",
        "retry-round-run-1",
        "callbacks/retry-round.json",
    )
    replay_state = retry_liveness.current_state()
    check(
        "confirmed retry replay promotes the reservation without duplicate input",
        resumed.record.state == "awaiting-callback"
        and replay_state is not None
        and replay_state.callback_submit_binding == retry_binding
        and replay_state.callback_submit_status == "sent"
        and replay_cmux.sent == []
        and replay_cmux.submit_count == 0,
        (resumed, replay_state, replay_cmux.sent, replay_cmux.submit_count),
    )

    late_cwd = root / "late-retry-crash"
    late_cwd.mkdir()
    (late_cwd / "callbacks").mkdir()
    (late_cwd / "prompt.md").write_text(
        "perform the bounded task", encoding="utf-8"
    )
    (late_cwd / "continue.md").write_text(
        "verify the bounded fix", encoding="utf-8"
    )
    late_store = OperationStore(root / "late-retry-store")
    late_cmux = RetryReceiptCrashCmux([])
    late_manager = RuntimeSessionManager(
        late_store,
        late_cmux,
        FakeProcess([]),
        {"claude": FakeDriver()},
        preflight=lambda _route, _callback_dir: CapabilityReport(
            route, True, ("provider:profile-valid",)
        ),
    )
    late_manager.start(
        replace(request, cwd=late_cwd, product_root=late_cwd)
    )
    late_child_spec = OperationSpec(
        "late-retry-round-1",
        "late-retry-round-key-1",
        "review-round",
        "owner-1",
        route,
        "packets/late-retry-round.json",
        "scoped",
    )
    late_store.create(
        late_child_spec,
        lane_id="lane-shared",
        run_id="late-retry-round-run-1",
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        late_store.transition("owner-1", "late-retry-round-1", state)
    late_parent = late_store.read("owner-1", "runtime-1")
    late_liveness = LivenessController(
        late_manager._state_root(late_parent) / "liveness"
    )
    late_liveness.observe(
        LivenessEvidence(
            observed_at=time.time(),
            process_status="alive",
            operation_revision=late_parent.revision,
            operation_state=late_parent.state,
        ),
        LivenessPolicy.default(),
    )

    def crash_before_sent_state(
        _controller: LivenessController, _binding_sha256: str
    ) -> None:
        raise SimulatedContinuationCrash()

    with patch.object(
        LivenessController,
        "mark_callback_submit_sent",
        crash_before_sent_state,
    ):
        try:
            late_manager.continue_same_session_round(
                "owner-1",
                "runtime-1",
                "checkpoint-1",
                "continue.md",
                "late-retry-round-1",
                "late-retry-round-run-1",
                "callbacks/late-retry-round.json",
            )
        except SimulatedContinuationCrash:
            pass
        else:
            raise AssertionError(
                "late retry crash seam must stop before sent-state publication"
            )
    late_parent = late_store.read("owner-1", "runtime-1")
    late_receipt = next(
        (
            late_manager._state_root(late_parent)
            / "continuation-deliveries"
        ).glob("*.json")
    )
    check(
        "late retry crash cannot publish submit-retried before sent-state durability",
        json.loads(late_receipt.read_text(encoding="utf-8"))["status"]
        == "submit-retry-reserved",
        late_receipt.read_text(encoding="utf-8"),
    )
    late_replay_cmux = ReplayActivityCmux([])
    late_manager.cmux = late_replay_cmux
    late_resumed = late_manager.continue_same_session_round(
        "owner-1",
        "runtime-1",
        "checkpoint-1",
        "continue.md",
        "late-retry-round-1",
        "late-retry-round-run-1",
        "callbacks/late-retry-round.json",
    )
    late_replay_state = late_liveness.current_state()
    check(
        "late retry crash replay promotes the exact reservation without duplicate input",
        late_resumed.record.state == "awaiting-callback"
        and late_replay_state is not None
        and late_replay_state.callback_submit_status == "sent"
        and late_replay_cmux.sent == []
        and late_replay_cmux.submit_count == 0,
        (
            late_resumed,
            late_replay_state,
            late_replay_cmux.sent,
            late_replay_cmux.submit_count,
        ),
    )
    retry_payload = {
        "verdict": "approve",
        "findings": [],
        "parent_session_operation_id": "runtime-1",
    }
    retry_encoded = json.dumps(
        retry_payload, sort_keys=True, separators=(",", ":")
    ).encode()
    retry_accepted = manager.accept_callback(
        CallbackEnvelope(
            "callback-retry-round-1",
            "retry-round-1",
            "retry-round-run-1",
            "review",
            retry_payload,
            hashlib.sha256(retry_encoded).hexdigest(),
        )
    )
    check(
        "serial child callback target reuses parent ownership",
        retry_accepted.record.spec.operation_id == "retry-round-1"
        and retry_accepted.record.state == "finalizing"
        and store.read("owner-1", "runtime-1").resources.surface_id == SURFACE,
        retry_accepted,
    )
    manager.cmux = cmux

    artifact_spec = OperationSpec(
        "artifact-round-1",
        "artifact-round-key-1",
        "review-round",
        "owner-1",
        route,
        "packets/artifact-round.json",
        "scoped",
    )
    store.create(
        artifact_spec, lane_id="lane-shared", run_id="artifact-round-run-1"
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition("owner-1", "artifact-round-1", state)
    artifact_target = {
        "operation_id": "artifact-round-1",
        "run_id": "artifact-round-run-1",
        "callback_pointer": "callbacks/artifact-round.json",
        "generation": 3,
    }
    artifact_input = cwd / "callbacks" / ".review-input.json"
    artifact_input.write_text("{}", encoding="utf-8")
    check(
        "unvalidated review input is not continuation evidence",
        not manager._continuation_artifact_ready(
            store.read("owner-1", "runtime-1"), artifact_target
        ),
    )
    artifact_input.unlink()
    artifact_payload = {"verdict": "approve", "findings": []}
    artifact_encoded = json.dumps(
        artifact_payload, sort_keys=True, separators=(",", ":")
    ).encode()
    CallbackBroker(store, "owner-1").accept(
        CallbackEnvelope(
            "callback-artifact-round-1",
            "artifact-round-1",
            "artifact-round-run-1",
            "review",
            artifact_payload,
            hashlib.sha256(artifact_encoded).hexdigest(),
        )
    )
    check(
        "exact accepted review receipt is continuation evidence",
        manager._continuation_artifact_ready(
            store.read("owner-1", "runtime-1"), artifact_target
        ),
    )
    for invalid_state in (
        "failed",
        "cancelled",
        "timed-out",
        "attention-required",
    ):
        operation_id = f"invalid-artifact-{invalid_state}"
        invalid_spec = OperationSpec(
            operation_id,
            f"{operation_id}-key",
            "review-round",
            "owner-1",
            route,
            "packets/invalid-artifact.json",
            "scoped",
        )
        invalid = store.create(
            invalid_spec, lane_id="lane-shared", run_id=f"{operation_id}-run"
        )
        store.save(
            replace(
                invalid,
                state=invalid_state,
                revision=invalid.revision + 1,
            ),
            expected_revision=invalid.revision,
        )
        check(
            f"{invalid_state} child is not continuation evidence",
            not manager._continuation_artifact_ready(
                store.read("owner-1", "runtime-1"),
                {
                    "operation_id": operation_id,
                    "run_id": f"{operation_id}-run",
                    "callback_pointer": "callbacks/invalid.json",
                    "generation": 4,
                },
            ),
        )

    cmux.checkpoint = "different-checkpoint"
    try:
        manager.continue_session(
            "owner-1", "runtime-1", "checkpoint-1", "continue.md"
        )
    except RuntimeSessionError:
        pass
    else:
        raise AssertionError("checkpoint mismatch must fail closed")
    check(
        "checkpoint mismatch sends no prompt",
        cmux.sent[-1] == (SURFACE, expected_claude_continuation),
    )
    cmux.checkpoint = "checkpoint-1"

    snapshot = manager.status("owner-1", "runtime-1")
    check(
        "status is read-only and reports exact live ownership",
        snapshot.record == store.read("owner-1", "runtime-1")
        and snapshot.process_status == "alive"
        and snapshot.surface_status == "alive"
        and snapshot.checkpoint == "checkpoint-1",
    )
    process.status_value = "unknown"
    unknown = manager.status("owner-1", "runtime-1")
    check(
        "unknown ownership becomes visible attention without mutation",
        unknown.action == "attention-required"
        and unknown.record.state == "awaiting-callback"
        and cmux.closed == [],
    )
    process.status_value = "alive"

    noncallback_spec = OperationSpec(
        "noncallback-cleanup",
        "noncallback-cleanup-key",
        "task-split",
        "owner-1",
        route,
        "packets/noncallback.json",
        "typed-result",
    )
    store.create(
        noncallback_spec,
        lane_id="noncallback-cleanup-lane",
        run_id="noncallback-cleanup-run",
    )
    noncallback_supervisor = OperationSupervisor(
        store, "owner-1", "noncallback-cleanup"
    )
    for state in ("preflight", "starting", "running", "finalizing"):
        noncallback_supervisor.transition(state)
    noncallback_supervisor.bind_resources(
        OwnedResources(
            SURFACE,
            123,
            124,
            PROCESS_IDENTITY,
            SUPERVISOR_IDENTITY,
        )
    )
    process.status_value = "unknown"
    process.supervisor_status_value = "unknown"
    noncallback_result = manager.request_exit(
        "owner-1", "noncallback-cleanup"
    )
    check(
        "manager non-callback unknown ownership stays fail-closed",
        noncallback_result.record.state == "attention-required"
        and noncallback_result.process_status == "unknown"
        and process.exit_requests == [],
        noncallback_result,
    )

    mismatch_spec = OperationSpec(
        "generic-cleanup-mismatch",
        "generic-cleanup-mismatch-key",
        "task-split",
        "owner-1",
        route,
        "packets/generic-mismatch.json",
        "typed-result",
    )
    store.create(
        mismatch_spec,
        lane_id="generic-cleanup-mismatch-lane",
        run_id="generic-cleanup-mismatch-run",
    )
    mismatch_supervisor = OperationSupervisor(
        store, "owner-1", "generic-cleanup-mismatch"
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        mismatch_supervisor.transition(state)
    mismatch_supervisor.bind_resources(
        OwnedResources(
            SURFACE,
            123,
            124,
            PROCESS_IDENTITY,
            SUPERVISOR_IDENTITY,
        )
    )
    mismatch_payload = {"status": "complete"}
    mismatch_payload_sha = hashlib.sha256(
        json.dumps(
            mismatch_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    CallbackBroker(store, "owner-1").accept(
        CallbackEnvelope(
            "generic-cleanup-mismatch-callback",
            "generic-cleanup-mismatch",
            "generic-cleanup-mismatch-run",
            "result",
            mismatch_payload,
            mismatch_payload_sha,
        )
    )
    process.status_value = "dead"
    process.supervisor_status_value = "unknown"
    exact_capture = process.capture_identity
    process.capture_identity = lambda pid, process_group=0: "c" * 64  # type: ignore[method-assign]
    mismatch_result = manager.request_exit(
        "owner-1", "generic-cleanup-mismatch"
    )
    check(
        "callback cleanup fails closed when exact identities do not match",
        mismatch_result.record.state == "attention-required"
        and process.exit_requests == [],
        mismatch_result,
    )
    process.capture_identity = exact_capture  # type: ignore[method-assign]
    process.status_value = "unknown"

    generic_spec = OperationSpec(
        "generic-cleanup",
        "generic-cleanup-key",
        "task-split",
        "owner-1",
        route,
        "packets/generic.json",
        "typed-result",
    )
    store.create(
        generic_spec,
        lane_id="generic-cleanup-lane",
        run_id="generic-cleanup-run",
    )
    generic_supervisor = OperationSupervisor(
        store, "owner-1", "generic-cleanup"
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        generic_supervisor.transition(state)
    generic_supervisor.bind_resources(
        OwnedResources(
            SURFACE,
            123,
            124,
            PROCESS_IDENTITY,
            SUPERVISOR_IDENTITY,
        )
    )
    generic_payload = {"status": "complete"}
    generic_payload_sha = hashlib.sha256(
        json.dumps(
            generic_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    CallbackBroker(store, "owner-1").accept(
        CallbackEnvelope(
            "generic-cleanup-callback",
            "generic-cleanup",
            "generic-cleanup-run",
            "result",
            generic_payload,
            generic_payload_sha,
        )
    )
    generic_exiting = manager.request_exit("owner-1", "generic-cleanup")
    check(
        "any accepted callback cleanup re-probes exact identities before exit",
        generic_exiting.record.state == "exiting"
        and generic_exiting.process_status == "alive"
        and process.exit_requests == [123],
        generic_exiting,
    )

    deadline_events: list[str] = []
    deadline_store = OperationStore(root / "research-fetch-deadline-store")
    deadline_process = FakeProcess(deadline_events)
    deadline_manager = RuntimeSessionManager(
        deadline_store,
        FakeCmux(deadline_events),
        deadline_process,
    )

    protected_research_route = RuntimeRoute(
        "codex",
        "gpt-5.6-sol",
        "high",
        "research-safe",
        "a" * 64,
    )

    def prepare_deadline_research(
        operation_id: str,
        run_id: str,
        *,
        kind: str = "research-fetch",
        protected_profile: bool = True,
    ) -> None:
        deadline_spec = OperationSpec(
            operation_id,
            f"{operation_id}-key",
            kind,
            "owner-deadline",
            protected_research_route if protected_profile else route,
            "packets/research.json",
            "research-cited-artifact",
        )
        deadline_store.create(
            deadline_spec,
            lane_id=f"{operation_id}-lane",
            run_id=run_id,
        )
        deadline_supervisor = OperationSupervisor(
            deadline_store, "owner-deadline", operation_id
        )
        deadline_supervisor.configure_budget(
            attempt_limit=1,
            model_restart_limit=0,
            time_budget_seconds=1,
            token_limit=100,
            now=0.0,
        )
        for state in ("preflight", "starting", "running", "awaiting-callback"):
            deadline_supervisor.transition(state)
        deadline_supervisor.bind_resources(
            OwnedResources(
                SURFACE,
                123,
                124,
                PROCESS_IDENTITY,
                SUPERVISOR_IDENTITY,
            )
        )
        deadline_session_root = (
            deadline_store.root
            / "owners"
            / "owner-deadline"
            / "runtime"
            / operation_id
        )
        deadline_session_root.mkdir(parents=True)
        (deadline_session_root / "session.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "run_id": run_id,
                    "placement": "split",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        (deadline_session_root / "callback-target.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation": 1,
                    "operation_id": operation_id,
                    "run_id": run_id,
                    "callback_pointer": "artifact.json",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        deadline_payload = {"status": "complete"}
        deadline_encoded = json.dumps(
            deadline_payload, sort_keys=True, separators=(",", ":")
        ).encode()
        CallbackBroker(deadline_store, "owner-deadline").accept(
            CallbackEnvelope(
                f"{operation_id}-callback",
                operation_id,
                run_id,
                "research",
                deadline_payload,
                hashlib.sha256(deadline_encoded).hexdigest(),
            )
        )
        exiting_fetch = deadline_manager.request_exit(
            "owner-deadline", operation_id
        )
        check(
            "research fetch deadline fixture records successful exact exit request",
            exiting_fetch.record.state == "exiting"
            and exiting_fetch.record.effect_id == "request-exit"
            and exiting_fetch.record.effect_outcome.value == "succeeded",
            exiting_fetch,
        )

    prepare_deadline_research(
        "research-fetch-deadline",
        "research-fetch-deadline-run",
    )
    deadline_process.status_value = "unknown"
    deadline_process.supervisor_status_value = "unknown"
    deadline_cleanup = deadline_manager.cleanup(
        "owner-deadline", "research-fetch-deadline"
    )
    check(
        "accepted research fetch escalates exact cleanup after deadline",
        deadline_cleanup.action == "wait-for-exit"
        and deadline_cleanup.record.state == "exiting"
        and deadline_process.terminations == [123],
        deadline_cleanup,
    )

    deadline_process.status_value = "alive"
    deadline_process.supervisor_status_value = "alive"
    prepare_deadline_research(
        "research-fetch-deadline-mismatch",
        "research-fetch-deadline-mismatch-run",
    )
    deadline_process.status_value = "unknown"
    deadline_process.supervisor_status_value = "unknown"
    deadline_process.capture_identity = (  # type: ignore[method-assign]
        lambda _pid, process_group=0: "c" * 64
    )
    mismatched_deadline = deadline_manager.cleanup(
        "owner-deadline", "research-fetch-deadline-mismatch"
    )
    check(
        "expired research fetch identity mismatch stays fail-closed",
        mismatched_deadline.action == "wait-for-ownership"
        and mismatched_deadline.record.state == "exiting"
        and deadline_process.terminations == [123],
        mismatched_deadline,
    )

    deadline_process.capture_identity = (  # type: ignore[method-assign]
        lambda pid, process_group=0: (
            PROCESS_IDENTITY
            if pid == 123 and process_group == 123
            else SUPERVISOR_IDENTITY
            if pid == 124 and process_group == 0
            else ""
        )
    )
    deadline_process.status_value = "alive"
    deadline_process.supervisor_status_value = "alive"
    prepare_deadline_research(
        "research-synth-deadline",
        "research-synth-deadline-run",
        kind="research-synth",
    )
    deadline_process.status_value = "unknown"
    deadline_process.supervisor_status_value = "unknown"
    synth_deadline = deadline_manager.cleanup(
        "owner-deadline", "research-synth-deadline"
    )
    check(
        "accepted research synthesis escalates exact cleanup after deadline",
        synth_deadline.action == "wait-for-exit"
        and synth_deadline.record.state == "exiting"
        and deadline_process.terminations == [123, 123],
        synth_deadline,
    )

    deadline_process.status_value = "alive"
    deadline_process.supervisor_status_value = "alive"
    prepare_deadline_research(
        "research-synth-profile-mismatch",
        "research-synth-profile-mismatch-run",
        kind="research-synth",
        protected_profile=False,
    )
    profile_mismatch = deadline_manager.cleanup(
        "owner-deadline", "research-synth-profile-mismatch"
    )
    check(
        "research synthesis cleanup rejects a non-protected profile",
        profile_mismatch.action == "wait-for-exit"
        and profile_mismatch.record.state == "exiting"
        and deadline_process.terminations == [123, 123],
        profile_mismatch,
    )

    process.status_value = "alive"
    process.supervisor_status_value = "alive"

    terminal_record = store.read("owner-1", "runtime-1")
    terminal_stream = RuntimeProviderEventStream.create(
        manager._state_root(terminal_record) / "provider-events",
        owner_id="owner-1",
        operation_id="runtime-1",
        run_id="run-1",
        generation=1,
        process_identity=terminal_record.resources.process_identity,
        workspace_id=WORKSPACE,
        surface_id=SURFACE,
        input_sha256="2" * 64,
    )
    assert terminal_stream.start().action == "wait"
    assert terminal_stream.reserve_input().action == "send"
    assert terminal_stream.accept_input().action == "wait"
    assert (
        terminal_stream.result(
            terminal_record.accepted_callback_sha256
        ).action
        == "close"
    )

    exiting = manager.request_exit("owner-1", "runtime-1")
    check(
        "exit requests the exact PGID before surface close",
        exiting.record.state == "exiting"
        and process.exit_requests == [123, 123]
        and cmux.closed == []
        and events.index("process-exit") < len(events),
    )
    process.status_value = "unknown"
    process.supervisor_status_value = "unknown"
    still_alive = manager.cleanup("owner-1", "runtime-1")
    check(
        "accepted callback exiting cleanup re-probes exact identities",
        still_alive.action == "wait-for-exit"
        and still_alive.process_status == "alive"
        and cmux.closed == [],
    )
    process.status_value = "dead"
    process.supervisor_status_value = "alive"
    waiting_supervisor = manager.cleanup("owner-1", "runtime-1")
    check(
        "cleanup also waits for the owned supervisor process",
        waiting_supervisor.action == "wait-for-supervisor"
        and cmux.closed == [],
    )
    process.supervisor_status_value = "dead"
    cmux.surface_statuses = ["unknown", "unknown", "missing"]
    transient = manager.cleanup("owner-1", "runtime-1")
    check(
        "transient terminal ownership stays retryable without mutation",
        transient.action == "wait-for-ownership"
        and transient.record.state == "exiting"
        and cmux.closed == [],
        transient,
    )
    cleaned = manager.cleanup("owner-1", "runtime-1")
    check(
        "dead provider tolerates one transient surface probe and clears ownership",
        cleaned.record.state == "complete"
        and cleaned.record.resources.surface_id == ""
        and cleaned.record.resources.process_group == 0
        and cmux.closed == []
        and "surface-close" not in events,
        events,
    )

    hook_events: list[str] = []
    hook_store = OperationStore(root / "hook-store")
    hook_cmux = FakeCmux(hook_events)
    hook_manager = RuntimeSessionManager(
        hook_store,
        hook_cmux,
        FakeProcess(hook_events),
        {"claude": FakeDriver()},
        preflight=lambda _route, _callback_dir: CapabilityReport(
            route, True, ("provider:profile-valid",)
        ),
    )

    def fail_preparation(_result: object) -> None:
        hook_events.append("surface-preparation-failed")
        raise RuntimeError("metadata write rejected")

    try:
        hook_manager.start(
            replace(request, placement="workspace"),
            on_surface_opened=fail_preparation,
        )
    except RuntimeSessionError:
        pass
    else:
        raise AssertionError("surface preparation failure must abort start")
    failed = hook_store.read("owner-1", "runtime-1")
    check(
        "failed preparation closes only owned surface before provider launch",
        hook_cmux.closed == []
        and hook_cmux.closed_workspaces == [(WORKSPACE, WINDOW)]
        and "provider-send" not in hook_events
        and failed.state == "failed"
        and failed.resources.surface_id == "",
        hook_events,
    )

    workspace_events: list[str] = []
    workspace_store = OperationStore(root / "workspace-store")
    workspace_cmux = FakeCmux(workspace_events)
    workspace_process = FakeProcess(workspace_events)
    workspace_manager = RuntimeSessionManager(
        workspace_store,
        workspace_cmux,
        workspace_process,
        {"claude": FakeDriver()},
        preflight=lambda _route, _callback_dir: CapabilityReport(
            route, True, ("provider:profile-valid",)
        ),
    )
    workspace_spec = replace(
        spec,
        operation_id="runtime-workspace",
        idempotency_key="runtime-workspace-key",
    )
    workspace_request = replace(
        request,
        spec=workspace_spec,
        run_id="run-workspace",
        placement="workspace",
        callback_pointer="callbacks/workspace.json",
    )
    workspace_manager.start(workspace_request)
    workspace_store.transition(
        "owner-1",
        "runtime-workspace",
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )
    attention_cleanup = workspace_manager.cleanup(
        "owner-1", "runtime-workspace"
    )
    check(
        "cleanup preserves a concurrent attention boundary for exact recovery",
        attention_cleanup.action == "attention-required"
        and attention_cleanup.record.state == "attention-required"
        and workspace_cmux.closed_workspaces == [],
    )
    workspace_store.transition(
        "owner-1", "runtime-workspace", "awaiting-callback"
    )
    workspace_payload = {"status": "complete"}
    workspace_payload_sha = hashlib.sha256(
        json.dumps(
            workspace_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    workspace_manager.accept_callback(
        CallbackEnvelope(
            "runtime-workspace-callback",
            "runtime-workspace",
            "run-workspace",
            "result",
            workspace_payload,
            workspace_payload_sha,
        )
    )
    workspace_record = workspace_store.read("owner-1", "runtime-workspace")
    workspace_stream = RuntimeProviderEventStream.create(
        workspace_manager._state_root(workspace_record) / "provider-events",
        owner_id="owner-1",
        operation_id="runtime-workspace",
        run_id="run-workspace",
        generation=1,
        process_identity=workspace_record.resources.process_identity,
        workspace_id=WORKSPACE,
        surface_id=SURFACE,
        input_sha256="3" * 64,
    )
    assert workspace_stream.start().action == "wait"
    assert workspace_stream.reserve_input().action == "send"
    assert workspace_stream.accept_input().action == "wait"
    assert workspace_stream.result(workspace_payload_sha).action == "close"
    ProcessAdapter._write_json(
        workspace_manager._callback_target_path(workspace_record),
        {
            "schema_version": 1,
            "generation": 2,
            "operation_id": "runtime-workspace",
            "run_id": "run-workspace",
            "callback_pointer": "callbacks/workspace.json",
        },
    )
    workspace_manager.request_exit("owner-1", "runtime-workspace")
    workspace_process.status_value = "dead"
    workspace_process.supervisor_status_value = "dead"
    workspace_cmux.surface_status = "missing"
    assert workspace_stream.process_exited(0).action == "close"
    workspace_cleaned = workspace_manager.cleanup(
        "owner-1", "runtime-workspace"
    )
    workspace_receipt = (
        workspace_manager._state_root(workspace_cleaned.record)
        / "provider-events"
        / "resource-closed.json"
    )
    check(
        "retargeted root cleanup uses immutable generation and preserves observer",
        workspace_cleaned.record.state == "complete"
        and workspace_cleaned.record.resources == OwnedResources()
        and workspace_cmux.closed_workspaces == []
        and workspace_cmux.closed == []
        and workspace_cmux.workspace_status_value == "alive"
        and workspace_receipt.is_file(),
        workspace_events,
    )

    workspace_surface_spec = replace(
        spec,
        operation_id="runtime-workspace-surface",
        idempotency_key="runtime-workspace-surface-key",
    )
    workspace_surface_request = replace(
        request,
        spec=workspace_surface_spec,
        run_id="run-workspace-surface",
        placement="workspace",
        callback_pointer="callbacks/workspace-surface.json",
    )
    workspace_cmux.surface_status = "alive"
    workspace_process.status_value = "alive"
    workspace_process.supervisor_status_value = "alive"
    workspace_surface_started = workspace_manager.start(
        workspace_surface_request
    )
    workspace_surface_payload = {"status": "complete", "surface": "exact"}
    workspace_surface_sha = hashlib.sha256(
        json.dumps(
            workspace_surface_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    workspace_manager.accept_callback(
        CallbackEnvelope(
            "runtime-workspace-surface-callback",
            "runtime-workspace-surface",
            "run-workspace-surface",
            "result",
            workspace_surface_payload,
            workspace_surface_sha,
        )
    )
    workspace_surface_stream = RuntimeProviderEventStream.create(
        workspace_manager._state_root(workspace_surface_started.record)
        / "provider-events",
        owner_id="owner-1",
        operation_id="runtime-workspace-surface",
        run_id="run-workspace-surface",
        generation=1,
        process_identity=(
            workspace_surface_started.record.resources.process_identity
        ),
        workspace_id=WORKSPACE,
        surface_id=SURFACE,
        input_sha256="4" * 64,
    )
    assert workspace_surface_stream.start().action == "wait"
    assert workspace_surface_stream.reserve_input().action == "send"
    assert workspace_surface_stream.accept_input().action == "wait"
    assert workspace_surface_stream.result(workspace_surface_sha).action == "close"
    workspace_process.status_value = "dead"
    workspace_process.supervisor_status_value = "dead"
    workspace_manager.request_exit("owner-1", "runtime-workspace-surface")
    assert workspace_surface_stream.process_exited(0).action == "close"
    workspace_cmux.surface_status = "alive"
    workspace_cmux.surface_statuses = ["alive", "missing"]
    workspace_surface_cleaned = workspace_manager.cleanup(
        "owner-1", "runtime-workspace-surface"
    )
    check(
        "live task surface closes exactly without closing observer workspace",
        workspace_surface_cleaned.record.state == "complete"
        and workspace_surface_cleaned.record.resources == OwnedResources()
        and workspace_cmux.closed == [SURFACE]
        and workspace_cmux.closed_workspaces == []
        and workspace_cmux.workspace_status_value == "alive",
        workspace_events,
    )

    drift_events: list[str] = []
    drift_store = OperationStore(root / "workspace-drift-store")
    drift_cmux = FakeCmux(drift_events)
    drift_process = FakeProcess(drift_events)
    drift_manager = RuntimeSessionManager(
        drift_store,
        drift_cmux,
        drift_process,
        {"claude": FakeDriver()},
        preflight=lambda _route, _callback_dir: CapabilityReport(
            route, True, ("provider:profile-valid",)
        ),
    )
    drift_spec = replace(
        spec,
        operation_id="runtime-workspace-drift",
        idempotency_key="runtime-workspace-drift-key",
    )
    drift_request = replace(
        request,
        spec=drift_spec,
        run_id="run-workspace-drift",
        placement="workspace",
        callback_pointer="callbacks/workspace-drift.json",
    )
    drift_manager.start(drift_request)
    drift_manager.request_exit("owner-1", "runtime-workspace-drift")
    drift_process.status_value = "dead"
    drift_process.supervisor_status_value = "dead"
    drift_cmux.surface_status = "missing"
    drift_cmux.workspace_status_value = "drift"
    drift_result = drift_manager.cleanup(
        "owner-1", "runtime-workspace-drift"
    )
    check(
        "modern root without accepted result retains exact surface ownership",
        drift_result.record.state == "attention-required"
        and drift_result.record.resources.surface_id == SURFACE
        and drift_cmux.closed_workspaces == [],
        drift_events,
    )

    worker_store = OperationStore(root / "worker-store")
    worker_spec = OperationSpec(
        "worker-1",
        "worker-key-1",
        "runtime-lifecycle",
        "owner-worker",
        route,
        "packets/runtime.json",
        "scoped",
    )
    worker_store.create(worker_spec, lane_id="lane-worker", run_id="run-worker")
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        worker_store.transition("owner-worker", "worker-1", state)
    worker_callback = cwd / "callbacks" / "worker.json"
    worker_payload = {"status": "ok"}
    worker_encoded = json.dumps(
        worker_payload, sort_keys=True, separators=(",", ":")
    ).encode()
    worker_envelope = {
        "schema_version": 1,
        "callback_id": "callback-worker",
        "operation_id": "worker-1",
        "run_id": "run-worker",
        "kind": "result",
        "payload": worker_payload,
        "payload_sha256": hashlib.sha256(worker_encoded).hexdigest(),
    }
    provider = root / "fake-provider.py"
    provider.write_text(
        "import pathlib,sys,time\n"
        "pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')\n"
        "time.sleep(0.8)\n",
        encoding="utf-8",
    )
    worker_launch = ProcessAdapter().prepare_surface_launch(
        argv=(
            str(Path(sys.executable).resolve()),
            str(provider),
            str(worker_callback),
            json.dumps(worker_envelope, sort_keys=True),
        ),
        cwd=cwd,
        state_root=root / "worker-state",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=worker_callback,
        store_root=worker_store.root,
        owner_id="owner-worker",
        operation_id="worker-1",
        run_id="run-worker",
        surface_id=SURFACE,
        runtime="claude",
        initial_input_pointer=cwd / "prompt.md",
    )
    ProcessAdapter._write_json(
        worker_launch.spec_path.parent / "session.json",
        {
            "schema_version": 1,
            "operation_id": "worker-1",
            "run_id": "run-worker",
            "workspace_id": WORKSPACE,
        },
    )
    check(
        "ordinary launch materializer persists the exact cwd binding",
        json.loads(worker_launch.spec_path.read_text(encoding="utf-8"))[
            "product_root"
        ]
        == str(cwd.resolve()),
    )
    review_wake_launch = ProcessAdapter().prepare_surface_launch(
        argv=(str(Path(sys.executable).resolve()), "-c", "pass"),
        cwd=cwd,
        state_root=root / "review-wake-state",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=cwd / "callbacks" / "review-wake.json",
        product_root=cwd,
        store_root=worker_store.root,
        owner_id="owner-worker",
        operation_id="worker-1",
        run_id="run-worker",
        surface_id=SURFACE,
        runtime="claude",
        callback_mode="envelope",
        callback_wake="resume exact current review",
    )
    review_wake_spec = json.loads(
        review_wake_launch.spec_path.read_text(encoding="utf-8")
    )
    check(
        "envelope worker preserves one bounded current-review wake",
        review_wake_spec["callback_wake"]
        == "resume exact current review",
    )
    reviewer_product = root / "reviewer-product"
    reviewer_product.mkdir()
    reviewer_store = OperationStore(root / "reviewer-store")
    reviewer_spec = OperationSpec(
        "reviewer-worker",
        "reviewer-worker-key",
        "review-session",
        "owner-reviewer-worker",
        RuntimeRoute(
            "codex",
            "gpt-5.6-sol",
            "high",
            "reviewer-callback",
            "d" * 64,
        ),
        "packets/review.json",
        "scoped",
    )
    reviewer_store.create(
        reviewer_spec,
        lane_id="lane-reviewer-worker",
        run_id="run-reviewer-worker",
    )
    reviewer_callback = cwd / "callbacks" / "reviewer-worker.json"
    reviewer_command = CodexDriver(Path("/usr/bin/codex")).command(
        reviewer_spec.route,
        callback_pointer=reviewer_callback,
        product_root=reviewer_product,
        session_root=cwd,
    )
    reviewer_launch = ProcessAdapter().prepare_surface_launch(
        argv=(*reviewer_command, "review"),
        cwd=cwd,
        state_root=root / "reviewer-worker-state",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=reviewer_callback,
        product_root=reviewer_product,
        reviewer_sandbox=True,
        store_root=reviewer_store.root,
        owner_id="owner-reviewer-worker",
        operation_id="reviewer-worker",
        run_id="run-reviewer-worker",
        surface_id=SURFACE,
        runtime="codex",
    )
    tampered_reviewer_spec = json.loads(
        reviewer_launch.spec_path.read_text(encoding="utf-8")
    )
    tampered_reviewer_spec["reviewer_sandbox"] = False
    reviewer_launch.spec_path.write_text(
        json.dumps(tampered_reviewer_spec, sort_keys=True),
        encoding="utf-8",
    )
    try:
        run_runtime_worker(reviewer_launch.spec_path)
    except RuntimeWorkerError:
        check(
            "reviewer sandbox identity is bound to durable operation authority",
            True,
        )
    else:
        check(
            "reviewer sandbox identity is bound to durable operation authority",
            False,
        )
    worker_result: list[int] = []
    worker_cmux = FakeCmux([])
    worker_wake_source = ArtifactWakeSource(worker_launch.spec_path.parent)
    worker_thread = threading.Thread(
        target=lambda: worker_result.append(
            run_runtime_worker(
                worker_launch.spec_path,
                poll_seconds=0.02,
                checkpoint_probe=lambda _surface, _runtime: "checkpoint-worker",
                cmux_adapter=worker_cmux,
                sleeper=time.sleep,
                wake_source=worker_wake_source,
            )
        )
    )
    worker_thread.start()
    receipt = worker_launch.spec_path.parent / "callback-receipt.json"
    deadline = time.monotonic() + 2
    while not receipt.is_file() and time.monotonic() < deadline:
        time.sleep(0.02)
    worker_child_spec = OperationSpec(
        "worker-round-2",
        "worker-round-key-2",
        "review-round",
        "owner-worker",
        route,
        "packets/worker-round.json",
        "scoped",
    )
    worker_store.create(
        worker_child_spec, lane_id="lane-worker", run_id="run-worker-round-2"
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        worker_store.transition("owner-worker", "worker-round-2", state)
    worker_child_payload = {"status": "second"}
    worker_child_encoded = json.dumps(
        worker_child_payload, sort_keys=True, separators=(",", ":")
    ).encode()
    worker_child_callback = cwd / "callbacks" / "worker-round-2.json"
    worker_child_callback.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "callback_id": "callback-worker-round-2",
                "operation_id": "worker-round-2",
                "run_id": "run-worker-round-2",
                "kind": "result",
                "payload": worker_child_payload,
                "payload_sha256": hashlib.sha256(
                    worker_child_encoded
                ).hexdigest(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    ProcessAdapter._write_json(
        worker_launch.spec_path.parent / "callback-target.json",
        {
            "schema_version": 1,
            "generation": 2,
            "operation_id": "worker-round-2",
            "run_id": "run-worker-round-2",
            "callback_pointer": str(worker_child_callback),
        },
    )
    worker_thread.join(timeout=3)
    worker_rc = worker_result[0]
    worker_record = worker_store.read("owner-worker", "worker-1")
    worker_child_record = worker_store.read("owner-worker", "worker-round-2")
    worker_ready = json.loads(worker_launch.ready_path.read_text(encoding="utf-8"))
    worker_exit = json.loads(worker_launch.exit_path.read_text(encoding="utf-8"))
    worker_wake_progress = json.loads(
        (worker_launch.spec_path.parent / "wake-progress.json").read_text(
            encoding="utf-8"
        )
    )
    check(
        "short-lived worker starts exact PGID and brokers provider outbox",
        worker_rc == 0
        and worker_ready["process_group"] > 1
        and worker_ready["supervisor_pid"] > 1
        and len(worker_ready["process_identity"]) == 64
        and len(worker_ready["supervisor_identity"]) == 64
        and worker_exit == {
            "schema_version": 1,
            "status": "exited",
            "exit_code": 0,
        }
        and worker_record.state == "finalizing"
        and worker_record.accepted_callback_id == "callback-worker",
        (worker_ready, worker_exit, worker_record),
    )
    check(
        "live worker retargets serial same-lane child without restart",
        worker_child_record.state == "finalizing"
        and worker_child_record.accepted_callback_id
        == "callback-worker-round-2"
        and json.loads(receipt.read_text(encoding="utf-8"))["generation"] == 2,
        (worker_ready, worker_exit, worker_record),
    )
    check(
        "scripted worker event plus confirmation accepts real stable callback transport",
        worker_wake_source.generations == {1, 2}
        and worker_wake_progress["source"] == "stability-confirmation"
        and worker_wake_progress["generation"] == 2
        and worker_wake_progress["outcome"] == "progressed",
        (worker_wake_source.generations, worker_wake_progress),
    )
    provider_event_payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(
            (
                worker_launch.spec_path.parent
                / "provider-events"
                / "generation-1"
                / "events"
            ).glob("*.json")
        )
    ]
    provider_events = [event["kind"] for event in provider_event_payloads]
    provider_result_sha256s = [
        event["result_sha256"]
        for event in provider_event_payloads
        if event["kind"] == "result-published"
    ]
    check(
        "public launch and live worker durably order input before result and exit",
        provider_events
        == [
            "provider-started",
            "input-accepted",
            "result-published",
            "process-exited",
        ]
        and provider_result_sha256s
        == [worker_record.accepted_callback_sha256]
        and worker_cmux.sent[-1]
        == (
            SURFACE,
            interactive_provider_input(
                "claude",
                (cwd / "prompt.md").resolve(),
                "perform the bounded task",
            ),
        ),
        (provider_event_payloads, worker_cmux.sent),
    )

    compact_prompt_path = cwd / "codex-prompt.md"
    compact_prompt = (
        "# Harness-owned review\n"
        "Axis: openai-engineering\n"
        "Inspect the exact product HEAD and return one typed verdict."
    )
    compact_prompt_path.write_text(compact_prompt, encoding="utf-8")
    compact_callback = cwd / "callbacks" / "compact-worker.json"
    compact_payload = {"status": "compact-ok"}
    compact_encoded = json.dumps(
        compact_payload, sort_keys=True, separators=(",", ":")
    ).encode()
    compact_envelope = {
        "schema_version": 1,
        "callback_id": "callback-compact-worker",
        "operation_id": "compact-worker",
        "run_id": "run-compact-worker",
        "kind": "result",
        "payload": compact_payload,
        "payload_sha256": hashlib.sha256(compact_encoded).hexdigest(),
    }
    compact_store = OperationStore(root / "compact-worker-store")
    compact_spec = OperationSpec(
        "compact-worker",
        "compact-worker-key",
        "runtime-lifecycle",
        "owner-compact-worker",
        RuntimeRoute(
            "codex", "gpt-5.6-sol", "high", "executor", "c" * 64
        ),
        "packets/runtime.json",
        "scoped",
    )
    compact_store.create(
        compact_spec,
        lane_id="lane-compact-worker",
        run_id="run-compact-worker",
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        compact_store.transition(
            "owner-compact-worker", "compact-worker", state
        )
    compact_launch = ProcessAdapter().prepare_surface_launch(
        argv=(
            str(Path(sys.executable).resolve()),
            str(provider),
            str(compact_callback),
            json.dumps(compact_envelope, sort_keys=True),
        ),
        cwd=cwd,
        state_root=root / "compact-worker-state",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=compact_callback,
        product_root=cwd,
        store_root=compact_store.root,
        owner_id="owner-compact-worker",
        operation_id="compact-worker",
        run_id="run-compact-worker",
        surface_id=SURFACE,
        runtime="codex",
        initial_input_pointer=compact_prompt_path,
    )
    ProcessAdapter._write_json(
        compact_launch.spec_path.parent / "session.json",
        {
            "schema_version": 1,
            "operation_id": "compact-worker",
            "run_id": "run-compact-worker",
            "workspace_id": WORKSPACE,
        },
    )
    class CompactCodexCmux(FakeCmux):
        def read(self, surface_id: str) -> str:
            assert surface_id == SURFACE
            if not self.sent:
                return "›"
            self.transport_visible = True
            prompt = self.sent[-1][1]
            anchor = next(
                (line.strip() for line in prompt.splitlines() if line.strip()),
                "",
            )
            if self.submit_count == self.submits_at_last_send:
                return f"› {anchor}"
            return "• Working (1s • esc to interrupt)"

    compact_cmux = CompactCodexCmux([])
    compact_rc = run_runtime_worker(
        compact_launch.spec_path,
        poll_seconds=0.02,
        checkpoint_probe=lambda _surface, _runtime: "checkpoint-compact",
        cmux_adapter=compact_cmux,
        sleeper=time.sleep,
        wake_source=ArtifactWakeSource(compact_launch.spec_path.parent),
    )
    expected_compact_input = interactive_provider_input(
        "codex", compact_prompt_path.resolve(), compact_prompt
    )
    compact_exit = (
        json.loads(compact_launch.exit_path.read_text(encoding="utf-8"))
        if compact_launch.exit_path.is_file()
        else {}
    )
    compact_record = compact_store.read(
        "owner-compact-worker", "compact-worker"
    )
    check(
        "Codex worker sends one compact contract pointer through cmux",
        compact_rc == 0
        and compact_cmux.sent == [(SURFACE, expected_compact_input)]
        and "\n" not in expected_compact_input,
        (compact_rc, compact_exit, compact_record, compact_cmux.sent),
    )

    guard_store = OperationStore(root / "guard-store")
    guard_spec = OperationSpec(
        "guard-worker",
        "guard-worker-key",
        "runtime-lifecycle",
        "owner-guard",
        route,
        "packets/runtime.json",
        "scoped",
    )
    guard_store.create(
        guard_spec, lane_id="guard-lane", run_id="guard-run"
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        guard_store.transition("owner-guard", "guard-worker", state)
    guard_callback = cwd / "callbacks" / "guard.json"
    guard_launch = ProcessAdapter().prepare_surface_launch(
        argv=(
            str(Path(sys.executable).resolve()),
            "-c",
            "import time; time.sleep(30)",
        ),
        cwd=cwd,
        state_root=root / "guard-state",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=guard_callback,
        product_root=cwd,
        store_root=guard_store.root,
        owner_id="owner-guard",
        operation_id="guard-worker",
        run_id="guard-run",
        surface_id=SURFACE,
        runtime="claude",
    )
    guard_result: list[int] = []
    guard_thread = threading.Thread(
        target=lambda: guard_result.append(
            run_runtime_worker(
                guard_launch.spec_path,
                poll_seconds=0.02,
                checkpoint_probe=lambda _surface, _runtime: "",
                wake_source=ArtifactWakeSource(guard_launch.spec_path.parent),
            )
        )
    )
    guard_thread.start()
    deadline = time.monotonic() + 2
    while (
        not guard_launch.ready_path.is_file()
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    guard_ready = json.loads(
        guard_launch.ready_path.read_text(encoding="utf-8")
    )
    guard_control = guard_launch.spec_path.parent / "process-control.json"
    ProcessAdapter.request_guardian_signal(
        guard_control,
        action="request-exit",
        operation_id="guard-worker",
        run_id="guard-run",
        process_group=guard_ready["process_group"],
        process_identity=guard_ready["process_identity"],
        supervisor_pid=guard_ready["supervisor_pid"],
        supervisor_identity=guard_ready["supervisor_identity"],
    )
    guard_thread.join(timeout=3)
    guard_receipt = json.loads(
        (
            guard_launch.spec_path.parent
            / "process-control-receipt.json"
        ).read_text(encoding="utf-8")
    )
    check(
        "sole-parent guardian consumes durable signal before reaping",
        not guard_thread.is_alive()
        and guard_result == [-signal.SIGTERM]
        and guard_receipt["action"] == "request-exit"
        and guard_receipt["status"] == "accepted"
        and ProcessAdapter.process_status(
            guard_ready["process_group"],
            guard_ready["process_identity"],
        )
        == "dead",
        (guard_result, guard_receipt),
    )

    natural_store = OperationStore(root / "natural-exit-store")
    natural_spec = OperationSpec(
        "natural-exit-worker",
        "natural-exit-key",
        "runtime-lifecycle",
        "owner-natural",
        route,
        "packets/runtime.json",
        "scoped",
    )
    natural_store.create(
        natural_spec, lane_id="natural-lane", run_id="natural-run"
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        natural_store.transition(
            "owner-natural", "natural-exit-worker", state
        )
    natural_descendant = root / "natural-descendant-pid"
    natural_provider = root / "natural-provider.py"
    natural_provider.write_text(
        "import pathlib,subprocess,sys\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='utf-8')\n",
        encoding="utf-8",
    )
    natural_launch = ProcessAdapter().prepare_surface_launch(
        argv=(
            str(Path(sys.executable).resolve()),
            str(natural_provider),
            str(natural_descendant),
        ),
        cwd=cwd,
        state_root=root / "natural-state",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=cwd / "callbacks" / "natural.json",
        product_root=cwd,
        store_root=natural_store.root,
        owner_id="owner-natural",
        operation_id="natural-exit-worker",
        run_id="natural-run",
        surface_id=SURFACE,
        runtime="claude",
    )
    natural_result: list[int] = []
    real_child_signal = ProcessAdapter.signal_owned_child_group
    with patch.object(
        ProcessAdapter,
        "signal_owned_child_group",
        wraps=real_child_signal,
    ) as child_signal:
        natural_thread = threading.Thread(
            target=lambda: natural_result.append(
                run_runtime_worker(
                    natural_launch.spec_path,
                    poll_seconds=0.02,
                    checkpoint_probe=lambda _surface, _runtime: "",
                    wake_source=ArtifactWakeSource(
                        natural_launch.spec_path.parent
                    ),
                )
            )
        )
        natural_thread.start()
        natural_thread.join(timeout=3)
    try:
        descendant_pid = int(
            natural_descendant.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        descendant_pid = 0
    deadline = time.monotonic() + 2
    descendant_alive = descendant_pid > 1
    while descendant_alive and time.monotonic() < deadline:
        try:
            os.kill(descendant_pid, 0)
        except ProcessLookupError:
            descendant_alive = False
            break
        time.sleep(0.02)
    check(
        "natural leader exit contains descendants before guardian reap",
        not natural_thread.is_alive()
        and natural_result == [0]
        and any(
            call.args[2] == signal.SIGKILL
            for call in child_signal.call_args_list
        )
        and not descendant_alive,
        (natural_result, child_signal.call_args_list),
    )
    if descendant_alive:
        try:
            os.kill(descendant_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

claude_executor = ClaudeDriver(Path("/usr/bin/claude")).command(
    RuntimeRoute("claude", "fable", "high", "executor", "b" * 64)
)
claude_reviewer = ClaudeDriver(Path("/usr/bin/claude")).command(
    RuntimeRoute("claude", "fable", "high", "reviewer-readonly", "b" * 64)
)
check(
    "Claude executor remains interactive auto while reviewer stays locked",
    "auto" in claude_executor and "dontAsk" in claude_reviewer,
)
check(
    "Claude option parsing stops before the runtime appends its prompt",
    claude_executor[-1] == "--" and claude_reviewer[-1] == "--",
    (claude_executor, claude_reviewer),
)
scratch = Path("/tmp/review-vault/.vault-meta/owned-review-scratch")
product = Path("/tmp/product-worktree")
callback = scratch / "callback.json"
review_input = scratch / ".review-input.json"
callback_route = RuntimeRoute(
    "claude", "fable", "high", "reviewer-callback", "c" * 64
)
claude_callback = ClaudeDriver(Path("/usr/bin/claude")).command(
    callback_route,
    callback_pointer=callback,
    product_root=product,
    session_root=scratch.parent,
)
review_submit = shlex.join(
    (
        str(Path(sys.executable).resolve()),
        str(Path("/tmp/review-vault/scripts/harness/review_submit.py")),
        "--worktree",
        str(product),
        "--state-dir",
        str(callback.parent),
        "--input-file",
        str(review_input),
    )
)
try:
    sandbox_settings = json.loads(
        claude_callback[claude_callback.index("--settings") + 1]
    )
except (ValueError, IndexError, json.JSONDecodeError):
    sandbox_settings = {}
review_statusline = sandbox_settings.get("statusLine", {})
review_statusline_command = review_statusline.get("command", "")
review_statusline_argv = (
    shlex.split(review_statusline_command)
    if isinstance(review_statusline_command, str)
    else []
)
review_statusline_output = ""
if (
    review_statusline.get("type") == "command"
    and review_statusline.get("padding") == 0
    and review_statusline.get("refreshInterval") == 10
    and len(review_statusline_argv) == 2
    and Path(review_statusline_argv[0]).resolve() == Path(sys.executable).resolve()
    and Path(review_statusline_argv[1]).resolve()
    == (ROOT / "scripts/harness/adapters/claude_reviewer_statusline.py").resolve()
):
    rendered_statusline = subprocess.run(
        review_statusline_argv,
        input=json.dumps(
            {
                "model": {"display_name": "Opus 5"},
                "effort": {"level": "xhigh"},
                "context_window": {"used_percentage": 25},
                "rate_limits": {
                    "five_hour": {"used_percentage": 42},
                    "seven_day": {"used_percentage": 9},
                },
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    if rendered_statusline.returncode == 0:
        review_statusline_output = rendered_statusline.stdout.strip()
check(
    "Claude reviewer keeps model, effort, context, and limits visible",
    review_statusline_output
    == "Opus 5 · effort xhigh · CTX 25% · 5H 42% · 7D 9%",
    (review_statusline, review_statusline_output),
)
normal_statusline_payload = {
    "model": {"display_name": "Opus 5"},
    "effort": {"level": "xhigh"},
    "context_window": {"used_percentage": 25},
    "rate_limits": {
        "five_hour": {"used_percentage": 42},
        "seven_day": {"used_percentage": 9},
    },
}
check(
    "Claude reviewer status renderer covers the normal payload in-process",
    render_claude_reviewer_statusline(normal_statusline_payload)
    == "Opus 5 · effort xhigh · CTX 25% · 5H 42% · 7D 9%",
)
fail_safe_statusline = "Claude · effort -- · CTX -- · 5H -- · 7D --"
check(
    "Claude reviewer status renderer fails safe for a malformed payload",
    render_claude_reviewer_statusline("not-an-object")
    == fail_safe_statusline,
)
check(
    "Claude reviewer status renderer fails safe for missing fields",
    render_claude_reviewer_statusline({}) == fail_safe_statusline,
)
check(
    "Claude reviewer status renderer fails safe for wrong field types",
    render_claude_reviewer_statusline(
        {
            "model": [],
            "effort": "xhigh",
            "context_window": {"used_percentage": True},
            "rate_limits": {
                "five_hour": {"used_percentage": "42"},
                "seven_day": [],
            },
        }
    )
    == fail_safe_statusline,
)
try:
    callback_instruction = claude_callback[
        claude_callback.index("--append-system-prompt") + 1
    ]
except (ValueError, IndexError):
    callback_instruction = ""
codex_callback = CodexDriver(Path("/usr/bin/codex")).command(
    RuntimeRoute(
        "codex",
        "gpt-5.6-sol",
        "high",
        "reviewer-callback",
        "d" * 64,
    ),
    callback_pointer=callback,
    product_root=product,
    session_root=scratch.parent,
)
check(
    "review callback profile uses Edit path permissions for isolated scratch transport",
    f"Edit(/{review_input})" in claude_callback
    and f"Write(/{review_input})" not in claude_callback
    and f"Write(/{callback})" not in claude_callback
    and f"Edit(/{callback})" not in claude_callback
    and f"Write({callback})" not in claude_callback
    and f"Edit({callback})" not in claude_callback
    and f"Write({review_input})" not in claude_callback
    and f"Edit({review_input})" not in claude_callback
    and "Write(owned-review-scratch/callback.json)" not in claude_callback
    and "Edit(owned-review-scratch/callback.json)" not in claude_callback
    and "Edit(owned-review-scratch/.review-input.json)" in claude_callback
    and "Write(owned-review-scratch/.review-input.json)" not in claude_callback
    and str(callback) in callback_instruction
    and "absolute path verbatim" in callback_instruction
    and "input file's exact session-relative alias" in callback_instruction
    and "Bash" in claude_callback
    and not any(item.startswith("Bash(") for item in claude_callback)
    and "--bare" not in claude_callback
    and "--safe-mode" not in claude_callback
    and "--strict-mcp-config" in claude_callback
    and claude_callback[claude_callback.index("--setting-sources") + 1] == ""
    and "disableAllHooks" not in sandbox_settings
    and sandbox_settings.get("claudeMdExcludes")
    == ["**/CLAUDE.md", "**/CLAUDE.local.md", "**/.claude/rules/**"]
    and sandbox_settings.get("sandbox", {}).get("enabled") is True
    and sandbox_settings.get("sandbox", {}).get("failIfUnavailable") is True
    and sandbox_settings.get("sandbox", {}).get("autoAllowBashIfSandboxed") is True
    and sandbox_settings.get("sandbox", {}).get("allowUnsandboxedCommands") is False
    and sandbox_settings.get("sandbox", {}).get("excludedCommands") == []
    and sandbox_settings.get("sandbox", {}).get("filesystem", {}).get("allowWrite")
    == [
        str(callback.parent.resolve()),
        str((callback.parent / ".review-test-tmp").resolve()),
    ]
    and not any(
        root == str(scratch.parent.resolve())
        for root in sandbox_settings.get("sandbox", {})
        .get("filesystem", {})
        .get("allowWrite", [])
    )
    and str(product.resolve())
    in sandbox_settings.get("sandbox", {}).get("filesystem", {}).get("denyWrite", [])
    and sandbox_settings.get("sandbox", {}).get("network", {}).get("allowedDomains") == []
    and sandbox_settings.get("sandbox", {}).get("network", {}).get("strictAllowlist") is True
    and sandbox_settings.get("sandbox", {}).get("network", {}).get("allowUnixSockets") == []
    and sandbox_settings.get("sandbox", {}).get("network", {}).get("allowLocalBinding") is False
    and sandbox_settings.get("permissions", {}).get("deny")
    and "WebFetch" in sandbox_settings["permissions"]["deny"]
    and not any(
        item.startswith(("Edit(", "Write("))
        and item
        not in {
            f"Edit(/{review_input})",
            "Edit(owned-review-scratch/.review-input.json)",
        }
        for item in claude_callback
    )
    and "workspace-write" in codex_callback
    and codex_callback[codex_callback.index("--cd") + 1]
    == str(callback.parent.resolve())
    and "--add-dir" not in codex_callback,
    (claude_callback, codex_callback),
)
codex_config_values = {
    codex_callback[index + 1]
    for index, value in enumerate(codex_callback[:-1])
    if value == "--config"
}
check(
    "Codex reviewer excludes ambient temporary roots and shell credentials",
    "sandbox_workspace_write.exclude_slash_tmp=true" in codex_config_values
    and "sandbox_workspace_write.exclude_tmpdir_env_var=true"
    in codex_config_values
    and "sandbox_workspace_write.network_access=false" in codex_config_values
    and "sandbox_workspace_write.writable_roots=[]" in codex_config_values
    and "shell_environment_policy.ignore_default_excludes=false"
    in codex_config_values
    and "--strict-config" in codex_callback,
)
validate_codex_reviewer_sandbox_command(
    codex_callback,
    callback_pointer=callback,
    product_root=product,
    session_root=scratch.parent,
)


def expect_codex_sandbox_rejection(
    label: str, command: tuple[str, ...]
) -> None:
    try:
        validate_codex_reviewer_sandbox_command(
            command,
            callback_pointer=callback,
            product_root=product,
            session_root=scratch.parent,
        )
    except CodexDriverError:
        check(label, True)
    else:
        check(label, False, command)


for required_config in (
    "sandbox_workspace_write.exclude_slash_tmp=true",
    "sandbox_workspace_write.exclude_tmpdir_env_var=true",
    "sandbox_workspace_write.network_access=false",
    "sandbox_workspace_write.writable_roots=[]",
    "shell_environment_policy.ignore_default_excludes=false",
):
    changed = list(codex_callback)
    value_index = changed.index(required_config)
    del changed[value_index - 1 : value_index + 1]
    expect_codex_sandbox_rejection(
        f"Codex reviewer rejects missing {required_config}", tuple(changed)
    )
changed_codex_root = list(codex_callback)
changed_codex_root[changed_codex_root.index("--cd") + 1] = "/tmp/foreign-review"
expect_codex_sandbox_rejection(
    "Codex reviewer rejects a foreign callback root", tuple(changed_codex_root)
)
expect_codex_sandbox_rejection(
    "Codex reviewer rejects an extra writable root",
    (*codex_callback, "--add-dir", str(product)),
)
expect_codex_sandbox_rejection(
    "Codex reviewer rejects a conflicting sandbox override",
    (
        *codex_callback,
        "--config",
        "sandbox_workspace_write.network_access=true",
    ),
)
for label, extra in (
    (
        "Codex reviewer rejects a short-form config override",
        ("-c", "sandbox_workspace_write.network_access=true"),
    ),
    (
        "Codex reviewer rejects a short-form cwd override",
        ("-C", "/"),
    ),
    (
        "Codex reviewer rejects a short-form sandbox override",
        ("-s", "danger-full-access"),
    ),
    (
        "Codex reviewer rejects a short-form approval override",
        ("-a", "on-request"),
    ),
    (
        "Codex reviewer rejects an equals-form writable root",
        (f"--add-dir={product}",),
    ),
):
    expect_codex_sandbox_rejection(label, (*codex_callback, *extra))
try:
    runtime_provider_argv(
        {
            "argv": tuple(changed_codex_root),
            "runtime": "codex",
            "callback_mode": "envelope",
            "reviewer_sandbox": True,
            "callback_pointer": callback,
            "product_root": product,
            "cwd": scratch.parent,
            "surface_id": SURFACE,
        }
    )
except RuntimeWorkerError:
    check("persisted Codex reviewer commands fail closed before replay", True)
else:
    check("persisted Codex reviewer commands fail closed before replay", False)
for runtime, command in (
    ("claude", claude_callback),
    ("codex", codex_callback),
):
    try:
        runtime_provider_argv(
            {
                "argv": command,
                "runtime": runtime,
                "callback_mode": "envelope",
                "reviewer_sandbox": True,
                "callback_pointer": callback,
                "cwd": scratch.parent,
                "surface_id": SURFACE,
            }
        )
    except RuntimeWorkerError:
        check(
            f"persisted {runtime} reviewer requires product_root before replay",
            True,
        )
    else:
        check(
            f"persisted {runtime} reviewer requires product_root before replay",
            False,
        )


def expect_sandbox_rejection(label: str, command: tuple[str, ...]) -> None:
    try:
        validate_reviewer_sandbox_command(
            command,
            callback_pointer=callback,
            product_root=product,
            session_root=scratch.parent,
        )
    except ClaudeDriverError:
        check(label, True)
    else:
        check(label, False, command)


validate_reviewer_sandbox_command(
    claude_callback,
    callback_pointer=callback,
    product_root=product,
    session_root=scratch.parent,
)
settings_position = claude_callback.index("--settings") + 1
for label, mutate in (
    (
        "review sandbox rejects a product write root",
        lambda value: value["sandbox"]["filesystem"]["allowWrite"].append(
            str(product.resolve())
        ),
    ),
    (
        "review sandbox rejects the unsandboxed escape hatch",
        lambda value: value["sandbox"].update(
            {"allowUnsandboxedCommands": True}
        ),
    ),
    (
        "review sandbox rejects network expansion",
        lambda value: value["sandbox"]["network"]["allowedDomains"].append(
            "example.com"
        ),
    ),
    (
        "review sandbox rejects subprocess exclusions",
        lambda value: value["sandbox"]["excludedCommands"].append("git *"),
    ),
    (
        "review sandbox rejects a foreign status line",
        lambda value: value["statusLine"].update(
            {"command": "python3 /tmp/foreign-statusline.py"}
        ),
    ),
):
    changed_settings = json.loads(claude_callback[settings_position])
    mutate(changed_settings)
    changed_command = list(claude_callback)
    changed_command[settings_position] = json.dumps(
        changed_settings, sort_keys=True, separators=(",", ":")
    )
    expect_sandbox_rejection(label, tuple(changed_command))

changed_sources = list(claude_callback)
changed_sources[changed_sources.index("--setting-sources") + 1] = "user"
expect_sandbox_rejection(
    "review sandbox rejects inherited user permissions", tuple(changed_sources)
)
changed_root = list(claude_callback)
changed_root[changed_root.index("--add-dir") + 1] = "/tmp/foreign-product"
expect_sandbox_rejection(
    "review sandbox rejects a foreign product path", tuple(changed_root)
)
expect_sandbox_rejection(
    "review sandbox rejects bare because subscription OAuth is unavailable",
    (*claude_callback, "--bare"),
)
expect_sandbox_rejection(
    "review sandbox rejects safe mode because it suppresses status line",
    (*claude_callback, "--safe-mode"),
)
check(
    "redirect subprocess and git-write attempts inherit the same native sandbox",
    "Bash" in claude_callback
    and sandbox_settings["sandbox"]["excludedCommands"] == []
    and str(product.resolve())
    in sandbox_settings["sandbox"]["filesystem"]["denyWrite"],
)
with tempfile.TemporaryDirectory(prefix="review-sandbox-paths.") as raw:
    sandbox_root = Path(raw)
    sandbox_product = sandbox_root / "product"
    sandbox_product.mkdir()
    (sandbox_product / ".git").mkdir()
    sandbox_session = sandbox_root / "session"
    sandbox_session.mkdir()
    sandbox_callbacks = sandbox_session / "callbacks"
    sandbox_callbacks.mkdir()
    sandbox_callback = sandbox_callbacks / "callback.json"
    sandbox_command = ClaudeDriver(Path("/usr/bin/claude")).command(
        callback_route,
        callback_pointer=sandbox_callback,
        product_root=sandbox_product,
        session_root=sandbox_session,
    )
    sandbox_env = provider_environment(
        {
            "runtime": "claude",
            "callback_mode": "envelope",
            "reviewer_sandbox": True,
            "callback_pointer": sandbox_callback,
            "product_root": sandbox_product,
        },
        env={
            "PATH": "/usr/bin:/bin",
            "GITHUB_TOKEN": "secret",
            "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
            "CLAUDE_CODE_SAFE_MODE": "1",
            "CLAUDE_CODE_SIMPLE": "1",
        },
    )
    test_tmp = (sandbox_callbacks / ".review-test-tmp").resolve()
    check(
        "review sandbox provisions one owned ephemeral test root",
        sandbox_env["TMPDIR"] == str(test_tmp)
        and sandbox_env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] == "0"
        and sandbox_env["CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL"]
        == "1"
        and "CLAUDE_CODE_SAFE_MODE" not in sandbox_env
        and "CLAUDE_CODE_SIMPLE" not in sandbox_env
        and test_tmp.is_dir()
        and not test_tmp.is_symlink()
        and test_tmp.stat().st_mode & 0o077 == 0,
    )
    codex_sandbox_env = provider_environment(
        {
            "runtime": "codex",
            "callback_mode": "envelope",
            "reviewer_sandbox": True,
            "callback_pointer": sandbox_callback,
            "product_root": sandbox_product,
        },
        env={"PATH": "/usr/bin:/bin", "TMPDIR": "/private/tmp"},
    )
    check(
        "Codex reviewer temp is contained by its exact callback lane",
        codex_sandbox_env["TMPDIR"] == str(test_tmp),
    )
    try:
        provider_environment(
            {
                "runtime": "codex",
                "callback_mode": "envelope",
                "reviewer_sandbox": True,
                "callback_pointer": sandbox_callback,
            },
            env={"PATH": "/usr/bin:/bin", "TMPDIR": "/private/tmp"},
        )
    except RuntimeWorkerError:
        check("reviewer environment requires product_root", True)
    else:
        check("reviewer environment requires product_root", False)
    symlink_callbacks = sandbox_root / "callback-link"
    symlink_callbacks.symlink_to(sandbox_callbacks, target_is_directory=True)
    try:
        ClaudeDriver(Path("/usr/bin/claude")).command(
            callback_route,
            callback_pointer=symlink_callbacks / "callback.json",
            product_root=sandbox_product,
            session_root=sandbox_session,
        )
    except ClaudeDriverError:
        check("review sandbox rejects a callback symlink escape", True)
    else:
        check("review sandbox rejects a callback symlink escape", False)
    sibling_callbacks = sandbox_session / "sibling-callbacks"
    sibling_callbacks.mkdir()
    codex_callback_link = sandbox_session / "codex-callback-link"
    codex_callback_link.symlink_to(
        sibling_callbacks, target_is_directory=True
    )
    try:
        CodexDriver(Path("/usr/bin/codex")).command(
            RuntimeRoute(
                "codex",
                "gpt-5.6-sol",
                "high",
                "reviewer-callback",
                "d" * 64,
            ),
            callback_pointer=codex_callback_link / "callback.json",
            product_root=sandbox_product,
            session_root=sandbox_session,
        )
    except CodexDriverError:
        check("Codex reviewer rejects a sibling callback symlink", True)
    else:
        check("Codex reviewer rejects a sibling callback symlink", False)
research_scratch = Path("/tmp/owned-research-scratch")
codex_research = CodexDriver(Path("/usr/bin/codex")).command(
    RuntimeRoute(
        "codex",
        "gpt-5.6-sol",
        "high",
        "research-safe",
        "e" * 64,
    ),
    callback_pointer=research_scratch / "artifact.json",
    session_root=research_scratch,
)
check(
    "Codex research profile is strict and writable only in isolated scratch",
    "--strict-config" in codex_research
    and "--cd" in codex_research
    and str(research_scratch) in codex_research
    and "workspace-write" in codex_research
    and "--add-dir" not in codex_research,
    codex_research,
)
with tempfile.TemporaryDirectory(prefix="runtime-review-timeout.") as raw:
    timeout_root = Path(raw)
    timeout_cwd = timeout_root / "scratch"
    timeout_cwd.mkdir()
    (timeout_cwd / "callbacks").mkdir()
    timeout_product = timeout_root / "product"
    timeout_product.mkdir()
    timeout_provider = timeout_root / "codex"
    timeout_provider.write_text(
        "#!/bin/sh\nsleep 0.2\n", encoding="utf-8"
    )
    timeout_provider.chmod(0o755)
    timeout_store = OperationStore(timeout_root / "store")
    timeout_route = RuntimeRoute(
        "codex",
        "gpt-5.6-sol",
        "high",
        "reviewer-callback",
        "f" * 64,
    )
    timeout_spec = OperationSpec(
        "runtime-review-timeout",
        "runtime-review-timeout-key",
        "simple-review",
        "owner-review-timeout",
        timeout_route,
        "packets/review.json",
        "scoped",
    )
    timeout_store.create(
        timeout_spec,
        lane_id="lane-review-timeout",
        run_id="run-review-timeout",
    )
    OperationSupervisor(
        timeout_store,
        "owner-review-timeout",
        "runtime-review-timeout",
    ).configure_budget(
        attempt_limit=1,
        model_restart_limit=0,
        time_budget_seconds=1,
        token_limit=100,
        now=1.0,
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        timeout_store.transition(
            "owner-review-timeout",
            "runtime-review-timeout",
            state,
        )
    timeout_launch = ProcessAdapter().prepare_surface_launch(
        argv=(
            *CodexDriver(timeout_provider).command(
                timeout_route,
                callback_pointer=timeout_cwd / "callbacks" / "review.json",
                product_root=timeout_product,
                session_root=timeout_cwd,
            ),
            "review",
        ),
        cwd=timeout_cwd,
        state_root=timeout_root / "worker-state",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=timeout_cwd / "callbacks" / "review.json",
        product_root=timeout_product,
        reviewer_sandbox=True,
        store_root=timeout_store.root,
        owner_id="owner-review-timeout",
        operation_id="runtime-review-timeout",
        run_id="run-review-timeout",
        surface_id=SURFACE,
        runtime="codex",
    )
    timeout_result = run_runtime_worker(
        timeout_launch.spec_path,
        poll_seconds=0.02,
        checkpoint_probe=lambda _surface, _runtime: "",
        cmux_adapter=object(),
        sleeper=time.sleep,
        wake_source=ArtifactWakeSource(timeout_launch.spec_path.parent),
    )
    timeout_record = timeout_store.read(
        "owner-review-timeout", "runtime-review-timeout"
    )
    timeout_marker = json.loads(
        (
            timeout_launch.spec_path.parent
            / "callback-timeout.json"
        ).read_text(encoding="utf-8")
    )
    check(
        "live worker enforces reviewer deadline before provider exit",
        timeout_result == 0
        and timeout_record.state == "attention-required"
        and timeout_record.attention_reason
        == AttentionReason.CALLBACK_TIMEOUT
        and timeout_marker == {
            "schema_version": 1,
            "operation_id": "runtime-review-timeout",
            "run_id": "run-review-timeout",
            "status": "attention-required",
        },
        (timeout_result, timeout_record, timeout_marker),
    )
with tempfile.TemporaryDirectory(prefix="runtime-review-early-exit.") as raw:
    timeout_root = Path(raw)
    timeout_cwd = timeout_root / "scratch"
    timeout_cwd.mkdir()
    (timeout_cwd / "callbacks").mkdir()
    timeout_product = timeout_root / "product"
    timeout_product.mkdir()
    timeout_provider = timeout_root / "codex"
    timeout_provider.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    timeout_provider.chmod(0o755)
    timeout_store = OperationStore(timeout_root / "store")
    timeout_route = RuntimeRoute(
        "codex",
        "gpt-5.6-sol",
        "high",
        "reviewer-callback",
        "f" * 64,
    )
    timeout_spec = OperationSpec(
        "runtime-review-early-exit",
        "runtime-review-early-exit-key",
        "simple-review",
        "owner-review-early-exit",
        timeout_route,
        "packets/review.json",
        "scoped",
    )
    timeout_store.create(
        timeout_spec,
        lane_id="lane-review-early-exit",
        run_id="run-review-early-exit",
    )
    OperationSupervisor(
        timeout_store,
        "owner-review-early-exit",
        "runtime-review-early-exit",
    ).configure_budget(
        attempt_limit=1,
        model_restart_limit=0,
        time_budget_seconds=0.2,
        token_limit=100,
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        timeout_store.transition(
            "owner-review-early-exit",
            "runtime-review-early-exit",
            state,
        )
    timeout_launch = ProcessAdapter().prepare_surface_launch(
        argv=(
            *CodexDriver(timeout_provider).command(
                timeout_route,
                callback_pointer=timeout_cwd / "callbacks" / "review.json",
                product_root=timeout_product,
                session_root=timeout_cwd,
            ),
            "review",
        ),
        cwd=timeout_cwd,
        state_root=timeout_root / "worker-state",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=timeout_cwd / "callbacks" / "review.json",
        product_root=timeout_product,
        reviewer_sandbox=True,
        store_root=timeout_store.root,
        owner_id="owner-review-early-exit",
        operation_id="runtime-review-early-exit",
        run_id="run-review-early-exit",
        surface_id=SURFACE,
        runtime="codex",
    )
    timeout_result = run_runtime_worker(
        timeout_launch.spec_path,
        poll_seconds=0.02,
        checkpoint_probe=lambda _surface, _runtime: "",
        cmux_adapter=object(),
        sleeper=time.sleep,
        wake_source=ArtifactWakeSource(timeout_launch.spec_path.parent),
    )
    timeout_record = timeout_store.read(
        "owner-review-early-exit", "runtime-review-early-exit"
    )
    check(
        "reviewer early exit remains supervised until callback deadline",
        timeout_result == 0
        and timeout_record.state == "attention-required"
        and timeout_record.attention_reason
        == AttentionReason.CALLBACK_TIMEOUT
        and (
            timeout_launch.spec_path.parent
            / "callback-timeout.json"
        ).is_file(),
        (timeout_result, timeout_record),
    )
with tempfile.TemporaryDirectory(prefix="runtime-review-boundary.") as raw:
    product_root = Path(raw)
    (product_root / "prompt.md").write_text("review", encoding="utf-8")
    (product_root / "callbacks").mkdir()
    callback_spec = OperationSpec(
        "callback-boundary",
        "callback-boundary-key",
        "review-session",
        "owner-boundary",
        callback_route,
        "packets/review.json",
        "scoped",
    )
    try:
        RuntimeSessionRequest(
            callback_spec,
            "lane-boundary",
            "run-boundary",
            ORIGIN,
            product_root,
            "prompt.md",
            "callbacks/result.json",
            product_root=product_root,
        )
    except RuntimeSessionError:
        pass
    else:
        raise AssertionError("review callback scratch cannot overlap product")
check(
    "review callback profile rejects writable product overlap",
    True,
)
with patch("harness.adapters.process.os.killpg") as unguarded_signal:
    for mutation in (
        ProcessAdapter.request_exit,
        ProcessAdapter.terminate_exact,
    ):
        try:
            mutation(123, PROCESS_IDENTITY)
        except ProcessError:
            pass
        else:
            raise AssertionError(
                "cross-process cleanup requires the guardian"
            )
check(
    "cross-process cleanup never signals without the guardian",
    not unguarded_signal.called,
)


# --- RC4-E11: initial-input semantic start acknowledgement --------------------
#
# A transport-level acknowledgement (send_key("Enter") returned) is not
# evidence that an interactive provider began the task.  These fixtures pin the
# boundary reproduced by dispatch d6a91c45-a4b1-4cec-9e2d-fd6562565e4f: a Claude
# surface that repaints only a rate-limit countdown and a spinner while still
# showing "waiting for first response" must never yield a durable
# `input-accepted`.


INITIAL_ANCHOR = "Implement the exact RC4 addendum"
STUCK_CLAUDE_SCREEN = (
    "waiting for first response\n"
    "✳ Resuming in 41s (approaching usage limit)\n"
    "❯\n"
)
ACTIVE_CLAUDE_SCREEN = "✻ Working…(12s · ↓ 480 tokens · esc to interrupt)\n❯\n"
ACTIVE_CLAUDE_220_SCREEN = "✽ Roosting… (44s · ↓ 2.3k tokens)\n❯\n"
ACTIVE_CODEX_SCREEN = "• Working (3s • esc to interrupt)\n›\n"
CLAUDE_TRUST_SCREEN = (
    "Accessing workspace: /tmp/product\n"
    "Quick safety check: Is this a project you created or one you trust?\n"
    "1. Yes, I trust this\n"
    "Enter to confirm · Esc to cancel\n"
)


class StartAckPort:
    """Replay an exact post-submit screen sequence for one surface."""

    def __init__(self, screens: list[str]) -> None:
        self.screens = list(screens)
        self.reads = 0
        self.sent: list[tuple[str, str]] = []
        self.keys: list[tuple[str, str]] = []

    def read(self, surface_id: str) -> str:
        assert surface_id == SURFACE
        self.reads += 1
        if len(self.screens) > 1:
            return self.screens.pop(0)
        return self.screens[0] if self.screens else ""

    def send(self, surface_id: str, text: str) -> None:
        self.sent.append((surface_id, text))

    def send_key(self, surface_id: str, key: str) -> None:
        self.keys.append((surface_id, key))


def _acknowledge(
    screens: list[str],
    *,
    runtime: str = "claude",
    paste_screen: str = "",
    artifact_ready=lambda: False,
    checkpoint_probe=lambda _surface, _runtime: "",
    observation_limit: int = 6,
) -> tuple[str, StartAckPort, list[float]]:
    port = StartAckPort(screens)
    waits: list[float] = []
    state = await_initial_start_acknowledged(
        port,
        surface_id=SURFACE,
        runtime=runtime,
        anchor=INITIAL_ANCHOR,
        paste_screen_sha256=_screen_digest(
            paste_screen or f"❯ {INITIAL_ANCHOR}\n"
        ),
        artifact_ready=artifact_ready,
        checkpoint_probe=checkpoint_probe,
        observation_limit=observation_limit,
        observation_interval_seconds=0.0,
        wait=waits.append,
    )
    return state, port, waits


def check_initial_start_rejects_false_repaint() -> None:
    """A rate-limit countdown and spinner are not provider progress."""

    repaints = [
        "waiting for first response\n"
        f"{glyph} Resuming in {60 - index}s (approaching usage limit)\n"
        "❯\n"
        for index, glyph in enumerate("·✢✳∗·✢")
    ]
    state, port, _waits = _acknowledge(repaints)
    check(
        "false repaint never becomes an initial provider start",
        state == "unconfirmed" and port.reads == 6 and not port.keys,
        (state, port.reads, port.keys),
    )
    stuck, _port, _waits = _acknowledge([STUCK_CLAUDE_SCREEN])
    check(
        "a static first-response wait stays unconfirmed",
        stuck == "unconfirmed",
        stuck,
    )


def check_initial_start_accepts_normal_claude() -> None:
    """A recognized Claude activity transition is a semantic start."""

    state, port, _waits = _acknowledge(
        [STUCK_CLAUDE_SCREEN, ACTIVE_CLAUDE_SCREEN]
    )
    check(
        "normal Claude activity acknowledges the initial start",
        state == "started" and port.reads == 2,
        (state, port.reads),
    )
    current_state, current_port, _waits = _acknowledge(
        [STUCK_CLAUDE_SCREEN, ACTIVE_CLAUDE_220_SCREEN]
    )
    check(
        "Claude 2.1.220 spaced activity acknowledges the initial start",
        current_state == "started" and current_port.reads == 2,
        (current_state, current_port.reads),
    )


def check_initial_start_accepts_normal_codex() -> None:
    """A recognized Codex activity transition is a semantic start."""

    state, port, _waits = _acknowledge(
        ["›\n", ACTIVE_CODEX_SCREEN],
        runtime="codex",
        paste_screen=f"› {INITIAL_ANCHOR}\n",
    )
    check(
        "normal Codex activity acknowledges the initial start",
        state == "started" and port.reads == 2,
        (state, port.reads),
    )


def check_initial_start_reports_bounded_states() -> None:
    """Every non-started outcome stays inside the typed bounded set."""

    permission, permission_port, _waits = _acknowledge([CLAUDE_TRUST_SCREEN])
    check(
        "a native permission dialog returns immediately as permission",
        permission == "permission" and permission_port.reads == 1,
        (permission, permission_port.reads),
    )
    composing, _port, _waits = _acknowledge([f"❯ {INITIAL_ANCHOR}\n"])
    check(
        "an unchanged composer reports still-composing",
        composing == "still-composing",
        composing,
    )
    unknown, _port, _waits = _acknowledge(["some unrecognized banner\n"])
    check(
        "an unrecognized surface reports unknown",
        unknown == "unknown",
        unknown,
    )
    artifact, artifact_port, _waits = _acknowledge(
        [STUCK_CLAUDE_SCREEN], artifact_ready=lambda: True
    )
    check(
        "a typed artifact acknowledges the start without a screen transition",
        artifact == "started" and artifact_port.reads == 0,
        (artifact, artifact_port.reads),
    )
    checkpoint, _port, _waits = _acknowledge(
        [STUCK_CLAUDE_SCREEN],
        checkpoint_probe=lambda _surface, _runtime: "checkpoint-start",
    )
    check(
        "a provider checkpoint acknowledges the start",
        checkpoint == "started",
        checkpoint,
    )
    identical, _port, _waits = _acknowledge(
        [ACTIVE_CLAUDE_SCREEN], paste_screen=ACTIVE_CLAUDE_SCREEN
    )
    check(
        "an activity screen identical to the paste screen is not a start",
        identical == "unconfirmed",
        identical,
    )


def check_initial_start_observes_within_budget() -> None:
    """The acknowledgement is bounded and never sends provider input."""

    state, port, waits = _acknowledge(
        [STUCK_CLAUDE_SCREEN], observation_limit=3
    )
    check(
        "initial start acknowledgement stays inside its exact budget",
        state == "unconfirmed"
        and port.reads == 3
        and len(waits) == 2
        and not port.sent
        and not port.keys,
        (state, port.reads, waits, port.sent, port.keys),
    )
    for limit, interval in ((0, 0.05), (1, -0.1)):
        try:
            await_initial_start_acknowledged(
                StartAckPort([STUCK_CLAUDE_SCREEN]),
                surface_id=SURFACE,
                runtime="claude",
                anchor=INITIAL_ANCHOR,
                paste_screen_sha256=_screen_digest(STUCK_CLAUDE_SCREEN),
                observation_limit=limit,
                observation_interval_seconds=interval,
                wait=lambda _seconds: None,
            )
        except ValueError:
            continue
        raise AssertionError(
            f"initial start budget accepted {limit}/{interval}"
        )
    check("initial start acknowledgement rejects an invalid budget", True)


class SurfaceVanished(RuntimeError):
    """The exact crash boundary: the surface dies between Enter and the ack."""


class InitialStartWorkerCmux(FakeCmux):
    """A Claude surface whose post-submit behaviour is scripted exactly."""

    def __init__(self, post_submit: list[str], *, fail_after_enter: bool = False) -> None:
        super().__init__([])
        self.post_submit = list(post_submit)
        self.fail_after_enter = fail_after_enter
        self.post_submit_reads = 0
        self.checkpoint = ""

    def read(self, surface_id: str) -> str:
        assert surface_id == SURFACE
        if not self.sent:
            return "❯\n"
        self.transport_visible = True
        prompt = self.sent[-1][1]
        anchor = next(
            (" ".join(line.split()) for line in prompt.splitlines() if line.strip()),
            "",
        )
        if self.submit_count == self.submits_at_last_send:
            return f"❯ {anchor}\n"
        if self.fail_after_enter:
            raise SurfaceVanished("surface transport vanished after submit")
        self.post_submit_reads += 1
        if len(self.post_submit) > 1:
            return self.post_submit.pop(0)
        return self.post_submit[0]


class PostSubmitTrustCmux(InitialStartWorkerCmux):
    """A recognized workspace dialog that appears only after task submit."""

    def __init__(self) -> None:
        super().__init__([ACTIVE_CLAUDE_220_SCREEN])
        self.dialog_acknowledged = False

    def read(self, surface_id: str) -> str:
        assert surface_id == SURFACE
        if not self.sent:
            return "❯\n"
        self.transport_visible = True
        prompt = self.sent[-1][1]
        anchor = next(
            (" ".join(line.split()) for line in prompt.splitlines() if line.strip()),
            "",
        )
        if self.submit_count == self.submits_at_last_send:
            return f"❯ {anchor}\n"
        if not self.dialog_acknowledged:
            return CLAUDE_TRUST_SCREEN
        self.post_submit_reads += 1
        return ACTIVE_CLAUDE_220_SCREEN

    def send_key(self, surface_id: str, key: str) -> None:
        if self.submit_count == self.submits_at_last_send:
            super().send_key(surface_id, key)
            return
        assert surface_id == SURFACE and key == "Enter"
        self.events.append("provider-dialog-confirm")
        self.submit_count += 1
        self.dialog_acknowledged = True


def _initial_start_worker(
    root: Path,
    name: str,
    cmux: FakeCmux,
    *,
    limit: int = 4,
    route_profile: str = "executor",
    before_join: Callable[[SurfaceLaunch], None] | None = None,
    workspace_id: str = WORKSPACE,
    inject_wake_source: bool = True,
) -> tuple[int | None, SurfaceLaunch, OperationStore, Path]:
    product_root = root / f"{name}-product"
    product_root.mkdir()
    product_root = product_root.resolve()
    cwd = (
        root / f"{name}-review-scratch"
        if route_profile == "reviewer-callback"
        else product_root
    )
    (cwd / "callbacks").mkdir(parents=True)
    cwd = cwd.resolve()
    prompt_path = cwd / "prompt.md"
    prompt_path.write_text(f"{INITIAL_ANCHOR}\n", encoding="utf-8")
    callback = cwd / "callbacks" / "result.json"
    route = RuntimeRoute(
        "claude", "claude-opus-5", "high", route_profile, "c" * 64
    )
    # Inline argv: the crash fixture leaves its provider to start after the
    # temporary root is gone, and a missing script file would print to stderr.
    if route_profile == "reviewer-callback":
        provider = root / "claude"
        provider.write_text("#!/bin/sh\nsleep 1\n", encoding="utf-8")
        provider.chmod(0o700)
        provider = provider.resolve()
        provider_argv = (
            *ClaudeDriver(provider).command(
                route,
                callback_pointer=callback,
                product_root=product_root,
                session_root=cwd,
            ),
            "review",
        )
    else:
        provider_argv = (
            str(Path(sys.executable).resolve()),
            "-c",
            "import time; time.sleep(1.0)",
        )
    store = OperationStore(root / f"{name}-store")
    spec = OperationSpec(
        f"{name}-op",
        f"{name}-key",
        "runtime-lifecycle",
        f"owner-{name}",
        route,
        "packets/runtime.json",
        "scoped",
    )
    store.create(spec, lane_id=f"lane-{name}", run_id=f"run-{name}")
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(f"owner-{name}", f"{name}-op", state)
    launch = ProcessAdapter().prepare_surface_launch(
        argv=provider_argv,
        cwd=cwd,
        state_root=root / f"{name}-state",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=callback,
        product_root=product_root,
        reviewer_sandbox=route_profile == "reviewer-callback",
        store_root=store.root,
        owner_id=f"owner-{name}",
        operation_id=f"{name}-op",
        run_id=f"run-{name}",
        surface_id=SURFACE,
        runtime="claude",
        initial_input_pointer=prompt_path,
    )
    ProcessAdapter._write_json(
        launch.spec_path.parent / "session.json",
        {
            "schema_version": 1,
            "operation_id": f"{name}-op",
            "run_id": f"run-{name}",
            "workspace_id": workspace_id,
        },
    )
    outcome: list[int] = []
    failure: list[BaseException] = []

    def drive() -> None:
        try:
            outcome.append(
                run_runtime_worker(
                    launch.spec_path,
                    poll_seconds=0.02,
                    checkpoint_probe=lambda _surface, _runtime: cmux.checkpoint,
                    cmux_adapter=cmux,
                    sleeper=time.sleep,
                    wake_source=(
                        ArtifactWakeSource(launch.spec_path.parent)
                        if inject_wake_source
                        else None
                    ),
                    initial_start_observation_limit=limit,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - crash boundary fixture
            failure.append(exc)

    thread = threading.Thread(target=drive)
    thread.start()
    before_join_failure: BaseException | None = None
    try:
        if before_join is not None:
            before_join(launch)
    except BaseException as exc:  # noqa: BLE001 - release/join before surfacing RED
        before_join_failure = exc
    finally:
        thread.join(timeout=30)
    for exc in failure:
        # Only the scripted crash boundary may escape the worker; anything
        # else is a real defect and must surface as its own red reason.
        if not isinstance(exc, SurfaceVanished):
            raise exc
    if before_join_failure is not None:
        raise before_join_failure
    return (outcome[0] if outcome else None), launch, store, callback


def _initial_start_delivery(launch: SurfaceLaunch) -> tuple[list[str], dict]:
    generation = launch.spec_path.parent / "provider-events" / "generation-1"
    kinds = [
        json.loads(path.read_text(encoding="utf-8"))["kind"]
        for path in sorted((generation / "events").glob("*.json"))
    ]
    state = json.loads(
        (generation / "delivery" / "delivery-state.json").read_text(
            encoding="utf-8"
        )
    )
    return kinds, state


def check_worker_contains_unconfirmed_initial_start() -> None:
    """The exact retained RC4 D shape must not publish a delivery receipt."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cmux = InitialStartWorkerCmux([STUCK_CLAUDE_SCREEN])
        code, launch, store, callback = _initial_start_worker(
            root, "stuck", cmux
        )
        kinds, delivery = _initial_start_delivery(launch)
        exit_record = json.loads(launch.exit_path.read_text(encoding="utf-8"))
        record = store.read("owner-stuck", "stuck-op")
        check(
            "an unconfirmed initial start publishes no input-accepted",
            "input-accepted" not in kinds
            and kinds == ["provider-started"]
            and not callback.is_file(),
            (kinds, callback.is_file()),
        )
        check(
            "an unconfirmed initial start settles the reserved send as ambiguous",
            delivery["send_status"] == "ambiguous"
            and delivery["send_attempts"] == 1,
            delivery,
        )
        check(
            "an unconfirmed initial start fails closed into existing containment",
            code == 2
            and exit_record["status"] == "input-unconfirmed"
            and exit_record["reason"] == "initial-start-unconfirmed"
            and record.state == "attention-required",
            (code, exit_record, record.state),
        )
        check(
            "an unconfirmed initial start never replays the provider input",
            len(cmux.sent) == 1
            and cmux.submit_count == 1
            and cmux.post_submit_reads >= 1,
            (cmux.sent, cmux.submit_count, cmux.post_submit_reads),
        )


def check_worker_accepts_acknowledged_initial_start() -> None:
    """A provider that visibly starts still yields exactly one acceptance."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cmux = InitialStartWorkerCmux(
            [STUCK_CLAUDE_SCREEN, ACTIVE_CLAUDE_SCREEN]
        )
        code, launch, _store, _callback = _initial_start_worker(
            root, "started", cmux
        )
        kinds, delivery = _initial_start_delivery(launch)
        check(
            "an acknowledged initial start publishes exactly one input-accepted",
            kinds.count("input-accepted") == 1
            and kinds[:2] == ["provider-started", "input-accepted"]
            and delivery["send_status"] == "accepted"
            and delivery["send_attempts"] == 1,
            (kinds, delivery),
        )
        check(
            "an acknowledged initial start sends exactly one prompt and Enter",
            len(cmux.sent) == 1 and cmux.submit_count == 1 and code == 0,
            (cmux.sent, cmux.submit_count, code),
        )


def check_worker_degrades_invalid_optional_wake_identity() -> None:
    """An optional malformed workspace binding cannot prevent provider start."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cmux = InitialStartWorkerCmux(
            [STUCK_CLAUDE_SCREEN, ACTIVE_CLAUDE_SCREEN]
        )
        code, launch, _store, _callback = _initial_start_worker(
            root,
            "invalid-wake-binding",
            cmux,
            workspace_id="not-a-uuid",
            inject_wake_source=False,
        )
        ready = json.loads(launch.ready_path.read_text(encoding="utf-8"))
        source_state = json.loads(
            (launch.spec_path.parent / "wake-source-state.json").read_text(
                encoding="utf-8"
            )
        )
        check(
            "malformed optional wake binding starts and falls back without attention",
            code == 0
            and ready["status"] == "ready"
            and source_state["source"] == "unavailable",
            (code, ready, source_state),
        )


class DelayedInitialAcknowledgementCmux(InitialStartWorkerCmux):
    """Hold semantic acknowledgement after the provider process exists."""

    def __init__(self) -> None:
        super().__init__([ACTIVE_CLAUDE_SCREEN])
        self.ack_started = threading.Event()
        self.release_ack = threading.Event()

    def read(self, surface_id: str) -> str:
        if self.sent and self.submit_count > self.submits_at_last_send:
            self.ack_started.set()
            if not self.release_ack.wait(timeout=5):
                raise AssertionError("initial acknowledgement fixture was not released")
        return super().read(surface_id)


def check_worker_handshakes_before_semantic_initial_ack() -> None:
    """Process ownership must not inherit the slower input-ack budget."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cmux = DelayedInitialAcknowledgementCmux()
        observed: list[ProcessHandle] = []

        def observe_handshake(launch: SurfaceLaunch) -> None:
            try:
                observed.append(
                    ProcessAdapter().await_surface_handle(
                        launch, timeout_seconds=2.0
                    )
                )
                check(
                    "worker reached the delayed semantic acknowledgement",
                    cmux.ack_started.wait(timeout=2),
                )
            finally:
                cmux.release_ack.set()

        code, _launch, _store, _callback = _initial_start_worker(
            root,
            "delayed-ack",
            cmux,
            route_profile="reviewer-callback",
            before_join=observe_handshake,
        )
        check(
            "worker publishes exact process ownership before input acknowledgement",
            code == 0
            and len(observed) == 1
            and observed[0].process_group > 1
            and observed[0].process_identity,
            (code, observed),
        )


class SwallowedEnterCmux(InitialStartWorkerCmux):
    """The composer keeps the pasted prompt until a second Enter arrives."""

    def __init__(self, *, recover_after: int = 2) -> None:
        super().__init__([ACTIVE_CLAUDE_SCREEN])
        self.recover_after = recover_after

    def read(self, surface_id: str) -> str:
        assert surface_id == SURFACE
        if not self.sent:
            return "❯\n"
        self.transport_visible = True
        prompt = self.sent[-1][1]
        anchor = next(
            (" ".join(line.split()) for line in prompt.splitlines() if line.strip()),
            "",
        )
        if self.submit_count - self.submits_at_last_send < self.recover_after:
            return f"❯ {anchor}\n"
        self.post_submit_reads += 1
        return ACTIVE_CLAUDE_SCREEN


def check_worker_recovers_one_swallowed_initial_enter() -> None:
    """The live RC3 reviewer shape: paste visible, first Enter swallowed.

    The composer keeps the exact pasted prompt through the whole first
    acknowledgement window; exactly one second identity-bound Enter (never a
    prompt resend) must start provider activity and reach exactly one durable
    input acceptance.
    """

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cmux = SwallowedEnterCmux()
        code, launch, _store, _callback = _initial_start_worker(
            root, "swallowed", cmux
        )
        kinds, delivery = _initial_start_delivery(launch)
        check(
            "one swallowed Enter recovers with exactly one second Enter",
            code == 0
            and len(cmux.sent) == 1
            and cmux.submit_count == 2
            and kinds.count("input-accepted") == 1
            and kinds[:2] == ["provider-started", "input-accepted"]
            and delivery["send_status"] == "accepted"
            and delivery["send_attempts"] == 1,
            (code, cmux.sent, cmux.submit_count, kinds, delivery),
        )


def check_worker_contains_double_swallowed_initial_enter() -> None:
    """A composer that never clears stays fail-closed after two Enters."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cmux = SwallowedEnterCmux(recover_after=99)
        code, launch, store, callback = _initial_start_worker(
            root, "double-swallowed", cmux
        )
        kinds, delivery = _initial_start_delivery(launch)
        exit_record = json.loads(launch.exit_path.read_text(encoding="utf-8"))
        record = store.read("owner-double-swallowed", "double-swallowed-op")
        check(
            "a still-composing second window stays contained without replay",
            code == 2
            and len(cmux.sent) == 1
            and cmux.submit_count == 2
            and "input-accepted" not in kinds
            and delivery["send_status"] == "ambiguous"
            and exit_record["status"] == "input-unconfirmed"
            and exit_record["reason"] == "initial-start-still-composing"
            and record.state == "attention-required"
            and not callback.is_file(),
            (code, cmux.submit_count, kinds, delivery, exit_record),
        )


class HeldSemanticFailureCmux(InitialStartWorkerCmux):
    """Hold the first post-submit read, then never acknowledge the start."""

    def __init__(self) -> None:
        super().__init__([STUCK_CLAUDE_SCREEN])
        self.ack_started = threading.Event()
        self.release_ack = threading.Event()

    def read(self, surface_id: str) -> str:
        if self.sent and self.submit_count > self.submits_at_last_send:
            self.ack_started.set()
            if not self.release_ack.wait(timeout=5):
                raise AssertionError(
                    "executor acknowledgement fixture was not released"
                )
        return super().read(surface_id)


def check_executor_handshake_precedes_semantic_ack_and_failure_stays_typed() -> None:
    """Executor process ownership must not inherit the input-ack budget.

    The live RC3 reproducer: a real executor needs longer than the manager's
    8s start budget to prove its semantic start, so the ready handshake must
    carry only process/supervisor ownership and be published before the
    acknowledgement window, exactly like the reviewer boundary.  A later
    semantic failure must stay typed attention without input replay.
    """

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cmux = HeldSemanticFailureCmux()
        observed: list[ProcessHandle] = []
        pre_release_kinds: list[list[str]] = []

        def observe_handshake(launch: SurfaceLaunch) -> None:
            try:
                observed.append(
                    ProcessAdapter().await_surface_handle(
                        launch, timeout_seconds=2.0
                    )
                )
                check(
                    "executor worker reached the held semantic window",
                    cmux.ack_started.wait(timeout=2),
                )
                generation = (
                    launch.spec_path.parent / "provider-events" / "generation-1"
                )
                pre_release_kinds.append(
                    [
                        json.loads(path.read_text(encoding="utf-8"))["kind"]
                        for path in sorted((generation / "events").glob("*.json"))
                    ]
                )
            finally:
                cmux.release_ack.set()

        code, launch, store, callback = _initial_start_worker(
            root,
            "executor-ready",
            cmux,
            before_join=observe_handshake,
        )
        kinds, delivery = _initial_start_delivery(launch)
        exit_record = json.loads(launch.exit_path.read_text(encoding="utf-8"))
        ready_record = json.loads(launch.ready_path.read_text(encoding="utf-8"))
        record = store.read("owner-executor-ready", "executor-ready-op")
        check(
            "an executor publishes exact process ownership before input acknowledgement",
            len(observed) == 1
            and observed[0].process_group > 1
            and observed[0].process_identity,
            observed,
        )
        check(
            "the early executor handshake does not synthesize input-accepted",
            pre_release_kinds == [["provider-started"]],
            pre_release_kinds,
        )
        check(
            "a later semantic failure stays typed attention without replay",
            code == 2
            and exit_record["status"] == "input-unconfirmed"
            and record.state == "attention-required"
            and "input-accepted" not in kinds
            and delivery["send_status"] == "ambiguous"
            and len(cmux.sent) == 1
            and cmux.submit_count == 1
            and not callback.is_file(),
            (code, exit_record, record.state, kinds, delivery),
        )
        check(
            "a failed semantic start rewrites the ready handshake fail-closed",
            ready_record.get("status") == "failed",
            ready_record,
        )


def check_late_ready_review_recovery_is_exact_and_replay_free() -> None:
    """Adopt one late worker handshake without replaying provider input."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        owner = "late-ready-owner"
        parent_id = "late-ready-parent"
        child_id = "late-ready-round"
        parent_run = "late-ready-parent-run"
        child_run = "late-ready-round-run"
        lane = "anthropic-holistic"
        route = RuntimeRoute(
            "claude",
            "fable",
            "high",
            "reviewer-callback",
            "d" * 64,
        )
        store = OperationStore(root / "store")
        parent_spec = OperationSpec(
            parent_id,
            "late-ready-parent-key",
            "simple-review-holistic",
            owner,
            route,
            "packets/review.json",
            "scoped",
        )
        child_spec = OperationSpec(
            child_id,
            "late-ready-child-key",
            "review-round",
            owner,
            route,
            "packets/review.json",
            "scoped",
            parent_operation_id=parent_id,
        )
        store.create(parent_spec, lane_id=lane, run_id=parent_run)
        store.transition(owner, parent_id, "preflight")
        store.transition(owner, parent_id, "starting")
        store.begin_effect(owner, parent_id, "start-provider")
        OperationSupervisor(store, owner, parent_id).bind_resources(
            OwnedResources(surface_id=SURFACE)
        )
        store.transition(
            owner,
            parent_id,
            "attention-required",
            reason=AttentionReason.PROCESS_START_FAILED,
        )
        store.create(child_spec, lane_id=lane, run_id=child_run)
        for state in ("preflight", "starting", "running", "awaiting-callback", "failed"):
            store.transition(owner, child_id, state)

        runtime_root = store.root / "owners" / owner / "runtime" / parent_id
        callback_root = root / "callbacks" / lane
        callback_root.mkdir(parents=True)
        runtime_root.mkdir(parents=True)
        callback_pointer = callback_root / ".review-callback.json"
        payload = {
            "schema_version": 1,
            "parent_session_operation_id": parent_id,
            "axis": lane,
            "verification_iteration": 0,
            "verdict": "approve",
            "findings": [],
        }
        payload_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        callback_value = {
            "schema_version": 1,
            "callback_id": f"review-{payload_sha256[:24]}",
            "operation_id": child_id,
            "run_id": child_run,
            "kind": "review",
            "payload": payload,
            "payload_sha256": payload_sha256,
        }
        callback_pointer.write_text(
            json.dumps(callback_value, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for name, value in {
            "session.json": {
                "schema_version": 1,
                "operation_id": parent_id,
                "run_id": parent_run,
                "workspace_id": WORKSPACE,
                "cwd": str(root),
            },
            "callback-target.json": {
                "schema_version": 1,
                "generation": 2,
                "operation_id": child_id,
                "run_id": child_run,
                "callback_pointer": f"callbacks/{lane}/.review-callback.json",
            },
            "ready.json": {
                "schema_version": 1,
                "status": "ready",
                "pid": 123,
                "process_group": 123,
                "supervisor_pid": 124,
                "process_identity": PROCESS_IDENTITY,
                "supervisor_identity": SUPERVISOR_IDENTITY,
            },
        }.items():
            (runtime_root / name).write_text(
                json.dumps(value, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        stream = RuntimeProviderEventStream.create(
            runtime_root / "provider-events",
            owner_id=owner,
            operation_id=parent_id,
            run_id=parent_run,
            generation=2,
            process_identity=PROCESS_IDENTITY,
            workspace_id=WORKSPACE,
            surface_id=SURFACE,
            input_sha256="e" * 64,
        )
        stream.start()
        stream.reserve_input()
        stream.accept_input()

        manager = RuntimeSessionManager(
            store,
            FakeCmux([]),
            FakeProcess([]),
            preflight=lambda route, callback: CapabilityReport(route, True, ()),
        )
        recovered = manager.recover_late_started_review_callback(
            owner, parent_id
        )
        parent = store.read(owner, parent_id)
        child = store.read(owner, child_id)
        before_replay = (parent.revision, child.revision)
        manager.recover_late_started_review_callback(owner, parent_id)
        replay_parent = store.read(owner, parent_id)
        replay_child = store.read(owner, child_id)
        delivery = stream.controller.current_state()
        check(
            "late ready recovery binds exact ownership and reopens one round",
            recovered.action == "late-start-recovered"
            and parent.state == "awaiting-callback"
            and parent.effect_outcome == EffectOutcome.SUCCEEDED
            and parent.resources.process_group == 123
            and child.state == "awaiting-callback"
            and not child.accepted_callback_id,
            (recovered, parent, child),
        )
        check(
            "late ready recovery is idempotent and never replays input",
            before_replay == (replay_parent.revision, replay_child.revision)
            and delivery.send_attempts == 1
            and delivery.send_status == "accepted",
            (before_replay, replay_parent.revision, replay_child.revision, delivery),
        )


def check_worker_handles_recognized_post_submit_prompt() -> None:
    """A safe native prompt after Enter must not lose or replay the task."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cmux = PostSubmitTrustCmux()
        code, launch, _store, _callback = _initial_start_worker(
            root, "post-submit-trust", cmux
        )
        kinds, delivery = _initial_start_delivery(launch)
        check(
            "a recognized post-submit dialog preserves one task delivery",
            code == 0
            and len(cmux.sent) == 1
            and cmux.submit_count == 2
            and cmux.dialog_acknowledged,
            (code, cmux.sent, cmux.submit_count),
        )
        check(
            "a recognized post-submit dialog reaches exact input acceptance",
            kinds[:2] == ["provider-started", "input-accepted"]
            and kinds.count("input-accepted") == 1
            and delivery["send_status"] == "accepted",
            (kinds, delivery),
        )


def check_worker_crash_after_enter_stays_replay_free() -> None:
    """A crash between Enter and acknowledgement stays ambiguous, never accepted."""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cmux = InitialStartWorkerCmux([STUCK_CLAUDE_SCREEN], fail_after_enter=True)
        _code, launch, _store, _callback = _initial_start_worker(
            root, "crash", cmux
        )
        kinds, delivery = _initial_start_delivery(launch)
        check(
            "a crash after Enter never records a durable acceptance",
            "input-accepted" not in kinds
            and delivery["send_status"] in {"reserved", "ambiguous"}
            and delivery["send_attempts"] == 1,
            (kinds, delivery),
        )
        check(
            "a crash after Enter leaves exactly one unreplayable send",
            len(cmux.sent) == 1 and cmux.submit_count == 1,
            (cmux.sent, cmux.submit_count),
        )


_INITIAL_START_FIXTURES = (
    check_initial_start_rejects_false_repaint,
    check_initial_start_accepts_normal_claude,
    check_initial_start_accepts_normal_codex,
    check_initial_start_reports_bounded_states,
    check_initial_start_observes_within_budget,
    check_worker_contains_unconfirmed_initial_start,
    check_worker_accepts_acknowledged_initial_start,
    check_worker_degrades_invalid_optional_wake_identity,
    check_worker_handshakes_before_semantic_initial_ack,
    check_worker_recovers_one_swallowed_initial_enter,
    check_worker_contains_double_swallowed_initial_enter,
    check_executor_handshake_precedes_semantic_ack_and_failure_stays_typed,
    check_late_ready_review_recovery_is_exact_and_replay_free,
    check_worker_handles_recognized_post_submit_prompt,
    check_worker_crash_after_enter_stays_replay_free,
)

_initial_start_failures: list[str] = []
for _fixture in _INITIAL_START_FIXTURES:
    try:
        _fixture()
    except AssertionError as _exc:
        _initial_start_failures.append(f"{_fixture.__name__}: {_exc}")
    except Exception as _exc:  # noqa: BLE001 - report every red fixture once
        _initial_start_failures.append(
            f"{_fixture.__name__}: {type(_exc).__name__}: {_exc}"
        )
if _initial_start_failures:
    raise AssertionError(
        "RC4-E11 initial-input regressions failed:\n  "
        + "\n  ".join(_initial_start_failures)
    )
