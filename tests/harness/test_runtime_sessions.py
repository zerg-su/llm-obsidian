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
from harness.runtime_worker import (
    load_spec as load_runtime_spec,
    provider_argv as runtime_provider_argv,
    run as run_runtime_worker,
)
from harness.runtime_provider import provider_environment
from harness.runtime_worker_contracts import RuntimeWorkerError
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
        self.submit_count = 0
        self.submits_at_last_send = 0

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
        self.submits_at_last_send = self.submit_count

    def send_key(self, surface_id: str, key: str) -> None:
        check(
            "submission uses exact surface and allowlisted Enter",
            surface_id == SURFACE and key == "Enter",
        )
        self.events.append("provider-submit")
        self.submit_count += 1

    def read(self, surface_id: str) -> str:
        assert surface_id == SURFACE
        if not self.sent:
            return "›"
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
        and ordinary_argv
        == (
            str(node.resolve()),
            str(codex),
            "--strict-config",
            "bounded research",
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
        and cmux.sent[-1] == (SURFACE, "verify the bounded fix"),
        cmux.sent,
    )
    checkpointless_cwd = root / "checkpointless-scratch"
    checkpointless_cwd.mkdir()
    (checkpointless_cwd / "callbacks").mkdir()
    (checkpointless_cwd / "prompt.md").write_text("review", encoding="utf-8")
    (checkpointless_cwd / "continue.md").write_text(
        "verify through the retained Claude process", encoding="utf-8"
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
        == (SURFACE, "verify through the retained Claude process"),
    )

    class RetainedPromptCmux(FakeCmux):
        def __init__(self, events: list[str], *, acknowledge: bool) -> None:
            super().__init__(events)
            self.acknowledge = acknowledge

        def read(self, surface_id: str) -> str:
            assert surface_id == SURFACE
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
        and sum(text.startswith("# Harness-owned") for _surface, text in stuck_cmux.sent)
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
            f"Write(/{review_input})",
            "Edit(owned-review-scratch/.review-input.json)",
            "Write(owned-review-scratch/.review-input.json)",
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
