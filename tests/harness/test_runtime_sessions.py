#!/usr/bin/env python3
"""Behavioral contract for generic provider-backed runtime sessions."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.adapters.claude import ClaudeDriver
from harness.adapters.cmux import Surface
from harness.adapters.codex import CodexDriver
from harness.callbacks import CallbackTimeoutError
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
    OperationSpec,
    RuntimeRoute,
)
from harness.runtime_sessions import (
    RuntimeSessionError,
    RuntimeSessionManager,
    RuntimeSessionRequest,
)
from harness.runtime_worker import run as run_runtime_worker
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

    def open_split(self, origin_surface: str) -> Surface:
        self.events.append("surface-open")
        self.opens += 1
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

    def send_key(self, surface_id: str, key: str) -> None:
        check(
            "submission uses exact surface and allowlisted Enter",
            surface_id == SURFACE and key == "Enter",
        )
        self.events.append("provider-submit")

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


with tempfile.TemporaryDirectory(prefix="runtime-sessions.") as raw:
    root = Path(raw)
    cwd = root / "worktree"
    cwd.mkdir()
    (cwd / "prompt.md").write_text("perform the bounded task", encoding="utf-8")
    (cwd / "continue.md").write_text("verify the bounded fix", encoding="utf-8")
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
        and cmux.sent[-1] == (SURFACE, "verify the bounded fix"),
        cmux.sent,
    )
    child_spec = OperationSpec(
        "round-1",
        "round-key-1",
        "review-round",
        "owner-1",
        route,
        "packets/round.json",
        "scoped",
    )
    store.create(child_spec, lane_id="lane-shared", run_id="run-round-1")
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition("owner-1", "round-1", state)
    registered = manager.continue_same_session_round(
        "owner-1",
        "runtime-1",
        "checkpoint-1",
        "continue.md",
        "round-1",
        "run-round-1",
        "callbacks/round.json",
    )
    child_payload = {"status": "ok"}
    child_encoded = json.dumps(
        child_payload, sort_keys=True, separators=(",", ":")
    ).encode()
    child_callback = CallbackEnvelope(
        "callback-round-1",
        "round-1",
        "run-round-1",
        "result",
        child_payload,
        hashlib.sha256(child_encoded).hexdigest(),
    )
    child_accepted = manager.accept_callback(child_callback)
    check(
        "serial child callback target reuses parent ownership",
        registered.callback_pointer == "callbacks/round.json"
        and registered.record.state == "awaiting-callback"
        and registered.record.resources.surface_id == SURFACE
        and child_accepted.record.spec.operation_id == "round-1"
        and child_accepted.record.state == "finalizing"
        and store.read("owner-1", "runtime-1").resources.surface_id == SURFACE,
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
        cmux.sent[-1] == (SURFACE, "verify the bounded fix"),
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

    exiting = manager.request_exit("owner-1", "runtime-1")
    check(
        "exit requests the exact PGID before surface close",
        exiting.record.state == "exiting"
        and process.exit_requests == [123]
        and cmux.closed == []
        and events.index("process-exit") < len(events),
    )
    still_alive = manager.cleanup("owner-1", "runtime-1")
    check(
        "cleanup cannot close while provider remains alive",
        still_alive.action == "wait-for-exit" and cmux.closed == [],
    )
    process.status_value = "dead"
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
    workspace_manager.request_exit("owner-1", "runtime-workspace")
    workspace_process.status_value = "dead"
    workspace_process.supervisor_status_value = "dead"
    workspace_cmux.surface_status = "missing"
    workspace_cleaned = workspace_manager.cleanup(
        "owner-1", "runtime-workspace"
    )
    check(
        "terminal cleanup closes and verifies metadata-owned workspace",
        workspace_cleaned.record.state == "complete"
        and workspace_cmux.closed_workspaces == [(WORKSPACE, WINDOW)]
        and workspace_cmux.closed == []
        and workspace_cmux.workspace_status_value == "missing",
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
        "workspace identity drift retains cleanup ownership",
        drift_result.action == "wait-for-ownership"
        and drift_result.record.state == "exiting"
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
    )
    review_wake_launch = ProcessAdapter().prepare_surface_launch(
        argv=(str(Path(sys.executable).resolve()), "-c", "pass"),
        cwd=cwd,
        state_root=root / "review-wake-state",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=cwd / "callbacks" / "review-wake.json",
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
    worker_result: list[int] = []
    worker_thread = threading.Thread(
        target=lambda: worker_result.append(
            run_runtime_worker(
                worker_launch.spec_path,
                poll_seconds=0.02,
                checkpoint_probe=lambda _surface, _runtime: "checkpoint-worker",
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
quoted_product = shlex.quote(str(product))
reviewer_readonly_probes = {
    f"Bash(git -C {quoted_product} --no-pager log --oneline -20)",
    (
        f"Bash(git -C {quoted_product} --no-pager show "
        "--stat --oneline HEAD)"
    ),
    f"Bash(python3 {quoted_product}/scripts/check-skill-budget.py)",
    f"Bash(make -C {quoted_product} test-harness)",
    f"Bash(make -C {quoted_product} test-model-routing)",
    f"Bash(git -C {quoted_product} diff --check)",
}
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
)
check(
    "review callback profile edits and writes only isolated scratch transport",
    f"Write(/{review_input})" in claude_callback
    and f"Edit(/{review_input})" in claude_callback
    and f"Write(/{callback})" not in claude_callback
    and f"Edit(/{callback})" not in claude_callback
    and f"Write({callback})" not in claude_callback
    and f"Edit({callback})" not in claude_callback
    and f"Write({review_input})" not in claude_callback
    and f"Edit({review_input})" not in claude_callback
    and "Write(owned-review-scratch/callback.json)" not in claude_callback
    and "Edit(owned-review-scratch/callback.json)" not in claude_callback
    and "Write(owned-review-scratch/.review-input.json)" in claude_callback
    and "Edit(owned-review-scratch/.review-input.json)" in claude_callback
    and str(callback) in callback_instruction
    and "absolute path verbatim" in callback_instruction
    and "input file's exact session-relative alias" in callback_instruction
    and f"Bash({review_submit})" in claude_callback
    and f"Bash(git -C {shlex.quote(str(product))} rev-parse HEAD)"
    in claude_callback
    and reviewer_readonly_probes.issubset(claude_callback)
    and "Bash(*)" not in claude_callback
    and not any(
        token in item
        for item in claude_callback
        if item.startswith("Bash(")
        for token in (" && ", " || ", ";", "$(", "`")
    )
    and not any(
        item.startswith(("Edit(", "Write("))
        and item
        not in {
            f"Edit(/{review_input})",
            f"Write(/{review_input})",
            "Edit(owned-review-scratch/.review-input.json)",
            "Write(owned-review-scratch/.review-input.json)",
        }
        for item in claude_callback
    )
    and "workspace-write" in codex_callback
    and "--add-dir" not in codex_callback,
    (claude_callback, codex_callback),
)
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
            str(Path(sys.executable).resolve()),
            "-c",
            "import time; time.sleep(0.2)",
        ),
        cwd=timeout_cwd,
        state_root=timeout_root / "worker-state",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=timeout_cwd / "callbacks" / "review.json",
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
        argv=(str(Path(sys.executable).resolve()), "-c", "pass"),
        cwd=timeout_cwd,
        state_root=timeout_root / "worker-state",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=timeout_cwd / "callbacks" / "review.json",
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
