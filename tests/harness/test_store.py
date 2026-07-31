#!/usr/bin/env python3
"""Hermetic operation-store, state-machine, and CLI tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from harness.contracts import (
    AttentionReason,
    ContractError,
    EffectOutcome,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.cli import main as harness_cli_main
from harness.store import OperationStore, StoreError


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


with tempfile.TemporaryDirectory(prefix="harness-store.") as raw:
    root = Path(raw)
    missing = root / "missing"
    readonly_store = OperationStore(missing)
    check("constructing a store is read-only", not missing.exists())
    check("listing an absent store is read-only", readonly_store.list("owner-1") == [] and not missing.exists())
    try:
        readonly_store.read("owner-1", "op-1")
    except StoreError:
        check("reading an absent store is read-only", not missing.exists())
    else:
        check("reading an absent store is read-only", False)

    for command in (("status",), ("inspect", "op-1"), ("doctor",)):
        cli_missing = root / f"cli-{'-'.join(command)}"
        readonly = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts/harness-cli.py"),
                "--store", str(cli_missing), "--owner", "owner-1", "--json", *command,
            ],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        check(
            f"CLI {command[0]} does not initialize an absent store",
            readonly.returncode in {0, 2} and not cli_missing.exists(),
        )

    store = OperationStore(root / "state")
    route = RuntimeRoute("codex", "gpt-5.6-sol", "high", "executor", "a" * 64)
    spec = OperationSpec("op-1", "key-1", "dispatch", "owner-1", route, "packet.json", "scoped")
    created = store.create(spec, lane_id="lane-1", run_id="run-1")
    check("create persists initial record", created.state == "created")
    reloaded = store.read("owner-1", "op-1")
    check(
        "lane and run identity survive restart",
        reloaded.lane_id == "lane-1" and reloaded.run_id == "run-1",
    )
    check(
        "same assigned spec create is idempotent",
        store.create(spec, lane_id="lane-1", run_id="run-1") == created,
    )
    try:
        replace(created, effect_id="effect", effect_outcome="succeeded")
    except ContractError:
        check("effect outcome requires the bounded enum", True)
    else:
        check("effect outcome requires the bounded enum", False)
    try:
        store.save(replace(created, run_id="run-rebound"), expected_revision=created.revision)
    except StoreError:
        check("durable operation identity cannot be rebound by save", True)
    else:
        check("durable operation identity cannot be rebound by save", False)
    try:
        store.create(
            OperationSpec(
                "op-same-run",
                "key-same-run",
                "dispatch",
                "owner-1",
                route,
                "packet.json",
                "scoped",
            ),
            lane_id="lane-1",
            run_id="run-1",
        )
    except StoreError:
        check("run identity belongs to exactly one operation", True)
    else:
        check("run identity belongs to exactly one operation", False)
    try:
        store.create(
            OperationSpec("op-2", "key-1", "dispatch", "owner-1", route, "other.json", "scoped"),
            lane_id="lane-1",
            run_id="run-2",
        )
    except StoreError:
        check("idempotency collision fails closed", True)
    else:
        check("idempotency collision fails closed", False)
    pending = store.begin_effect("owner-1", "op-1", "open-surface")
    check(
        "effect intent is durable before effect",
        pending.pending_effect == "open-surface"
        and pending.effect_outcome == EffectOutcome.PENDING,
    )
    check("same effect intent is idempotent", store.begin_effect("owner-1", "op-1", "open-surface") == pending)
    restarted = OperationStore(root / "state").read("owner-1", "op-1")
    check(
        "restart preserves unresolved effect boundary",
        restarted.pending_effect == "open-surface"
        and restarted.effect_outcome == EffectOutcome.PENDING,
    )
    try:
        store.transition("owner-1", "op-1", "preflight")
    except ValueError:
        check("unresolved effect blocks state advance", True)
    else:
        check("unresolved effect blocks state advance", False)
    resolved = store.resolve_effect("owner-1", "op-1", EffectOutcome.SUCCEEDED)
    check(
        "effect outcome is durable and clears reconciliation",
        not resolved.pending_effect
        and resolved.effect_id == "open-surface"
        and resolved.effect_outcome == EffectOutcome.SUCCEEDED,
    )
    check(
        "same effect resolution is idempotent",
        store.resolve_effect("owner-1", "op-1", EffectOutcome.SUCCEEDED) == resolved,
    )
    check(
        "completed effect intent cannot be reopened by retry",
        store.begin_effect("owner-1", "op-1", "open-surface") == resolved,
    )
    moved = store.transition("owner-1", "op-1", "preflight")
    check("legal transition advances revision", moved.changed and moved.revision > pending.revision)
    try:
        store.transition("owner-1", "op-1", "complete")
    except ValueError:
        check("illegal transition rejected", True)
    else:
        check("illegal transition rejected", False)
    store.transition(
        "owner-1", "op-1", "attention-required",
        reason=AttentionReason.CAPABILITY_MISMATCH,
    )
    check("attention reason round-trips", store.read("owner-1", "op-1").attention_reason == AttentionReason.CAPABILITY_MISMATCH)

    terminal_spec = OperationSpec(
        "op-terminal",
        "key-terminal",
        "dispatch",
        "owner-1",
        route,
        "packet.json",
        "scoped",
    )
    store.create(terminal_spec, lane_id="lane-1", run_id="run-terminal")
    for state in ("preflight", "starting", "running", "finalizing", "exiting", "complete"):
        store.transition("owner-1", "op-terminal", state)
    before_terminal_repeat = store.read("owner-1", "op-terminal")
    repeated_terminal = store.transition("owner-1", "op-terminal", "complete")
    check(
        "repeated terminal transition is an idempotent no-op",
        not repeated_terminal.changed
        and store.read("owner-1", "op-terminal") == before_terminal_repeat,
    )

    concurrent_specs = (
        OperationSpec(
            "op-concurrent-a",
            "key-concurrent",
            "dispatch",
            "owner-concurrent",
            route,
            "packet.json",
            "scoped",
        ),
        OperationSpec(
            "op-concurrent-b",
            "key-concurrent",
            "dispatch",
            "owner-concurrent",
            route,
            "packet.json",
            "scoped",
        ),
    )
    create_barrier = threading.Barrier(2)

    def create_concurrently(specification: OperationSpec) -> str:
        create_barrier.wait()
        try:
            OperationStore(root / "state").create(
                specification,
                lane_id="lane-concurrent",
                run_id=specification.operation_id,
            )
        except StoreError:
            return "collision"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        create_results = list(pool.map(create_concurrently, concurrent_specs))
    check(
        "concurrent create serializes idempotency-key ownership",
        sorted(create_results) == ["collision", "created"]
        and len(store.list("owner-concurrent")) == 1,
    )

    corrupt_spec = OperationSpec(
        "op-corrupt",
        "key-corrupt",
        "dispatch",
        "owner-corrupt",
        route,
        "packet.json",
        "scoped",
    )
    store.create(corrupt_spec, lane_id="lane-corrupt", run_id="run-corrupt")
    corrupt_path = root / "state/owners/owner-corrupt/operations/op-corrupt.json"
    corrupt_value = json.loads(corrupt_path.read_text(encoding="utf-8"))
    corrupt_value["effect_outcome"] = "impossible"
    corrupt_path.write_text(json.dumps(corrupt_value), encoding="utf-8")
    try:
        store.read("owner-corrupt", "op-corrupt")
    except StoreError:
        check("corrupt durable state fails closed through the store seam", True)
    else:
        check("corrupt durable state fails closed through the store seam", False)

    cli = subprocess.run(
        [
            sys.executable, str(ROOT / "scripts/harness-cli.py"),
            "--store", str(root / "state"), "--owner", "owner-1", "--json", "status",
        ],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    value = json.loads(cli.stdout)
    check(
        "CLI status reports durable operation assignment without cmux",
        cli.returncode == 0
        and value[0]["operation_id"] == "op-1"
        and value[0]["lane_id"] == "lane-1"
        and value[0]["run_id"] == "run-1",
    )
    before = (root / "state/owners/owner-1/operations/op-1.json").read_bytes()
    inspect = subprocess.run(
        [
            sys.executable, str(ROOT / "scripts/harness-cli.py"),
            "--store", str(root / "state"), "--owner", "owner-1", "--json", "inspect", "op-1",
        ],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    check("CLI inspect is read-only", inspect.returncode == 0 and before == (root / "state/owners/owner-1/operations/op-1.json").read_bytes())

    def create_cli_operation(operation_id: str, *, state: str = "created") -> None:
        cli_spec = OperationSpec(
            operation_id,
            f"key-{operation_id}",
            "dispatch",
            "owner-cli",
            route,
            "packet.json",
            "scoped",
        )
        store.create(
            cli_spec,
            lane_id="lane-cli",
            run_id=f"run-{operation_id}",
        )
        for next_state in {
            "created": (),
            "starting": ("preflight", "starting"),
            "running": ("preflight", "starting", "running"),
            "awaiting-callback": (
                "preflight",
                "starting",
                "running",
                "awaiting-callback",
            ),
            "cancelling": ("cancelling",),
            "exiting": (
                "preflight",
                "starting",
                "running",
                "finalizing",
                "exiting",
            ),
        }[state]:
            store.transition("owner-cli", operation_id, next_state)

    def run_cli(command: str, operation_id: str = "") -> subprocess.CompletedProcess[str]:
        argv = [
            sys.executable,
            str(ROOT / "scripts/harness-cli.py"),
            "--store",
            str(root / "state"),
            "--owner",
            "owner-cli",
            "--json",
            command,
        ]
        if operation_id:
            argv.append(operation_id)
        return subprocess.run(
            argv,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    class FakeProcess:
        def __init__(self, status: str) -> None:
            self.status = status
            self.terminated: list[int] = []
            self.guardian_requests: list[dict[str, object]] = []

        def process_status(
            self, _process_group: int, _identity: str
        ) -> str:
            return self.status

        def terminate_exact(
            self, process_group: int, _identity: str
        ) -> None:
            self.terminated.append(process_group)
            self.status = "dead"

        def request_guardian_signal(
            self,
            _control_path: Path,
            **request: object,
        ) -> None:
            self.guardian_requests.append(dict(request))

    class FakeCmux:
        def __init__(self, status: str) -> None:
            self.current = status
            self.closed: list[str] = []
            self.workspace_current = status
            self.closed_workspaces: list[tuple[str, str]] = []

        def status(self, _surface_id: str) -> str:
            if self.current == "unknown":
                raise RuntimeError("status unavailable")
            return self.current

        def close_exact(self, surface_id: str) -> None:
            self.closed.append(surface_id)
            self.current = "missing"

        def workspace_status(
            self, workspace_id: str, window_id: str
        ) -> str:
            del workspace_id, window_id
            if self.workspace_current == "drift":
                raise RuntimeError("workspace moved to another window")
            return self.workspace_current

        def close_workspace_exact(
            self, workspace_id: str, window_id: str
        ) -> None:
            self.closed_workspaces.append((workspace_id, window_id))
            self.workspace_current = "missing"

    def run_cli_in_process(
        command: str,
        operation_id: str,
        *,
        process: FakeProcess,
        cmux: FakeCmux,
    ) -> tuple[int, object]:
        argv = [
            "--store",
            str(root / "state"),
            "--owner",
            "owner-cli",
            "--json",
            command,
        ]
        if operation_id:
            argv.append(operation_id)
        output = StringIO()
        with redirect_stdout(output):
            rc = harness_cli_main(
                argv,
                process_adapter=process,
                cmux_adapter=cmux,
            )
        return rc, json.loads(output.getvalue())

    def bind_owned_resources(
        operation_id: str,
        resources: OwnedResources = OwnedResources(
            "11111111-1111-1111-1111-111111111111",
            42,
            43,
            "a" * 64,
            "b" * 64,
        ),
    ) -> None:
        record = store.read("owner-cli", operation_id)
        store.save(
            replace(
                record,
                resources=resources,
                revision=record.revision + 1,
            ),
            expected_revision=record.revision,
        )

    def write_workspace_session(operation_id: str) -> tuple[str, str]:
        record = store.read("owner-cli", operation_id)
        workspace_id = "22222222-2222-4222-8222-222222222222"
        window_id = "33333333-3333-4333-8333-333333333333"
        path = (
            store.root
            / "owners"
            / "owner-cli"
            / "runtime"
            / operation_id
            / "session.json"
        )
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "run_id": record.run_id,
                    "placement": "workspace",
                    "workspace_id": workspace_id,
                    "window_id": window_id,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return workspace_id, window_id

    def write_callback_target(operation_id: str) -> None:
        record = store.read("owner-cli", operation_id)
        path = (
            store.root
            / "owners"
            / "owner-cli"
            / "runtime"
            / operation_id
            / "callback-target.json"
        )
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation": 1,
                    "operation_id": operation_id,
                    "run_id": record.run_id,
                    "callback_pointer": "callbacks/result.json",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    create_cli_operation("op-pending-cli")
    store.begin_effect("owner-cli", "op-pending-cli", "open-surface")
    resumed = run_cli("resume", "op-pending-cli")
    pending_after_resume = store.read("owner-cli", "op-pending-cli")
    check(
        "CLI resume contains an unresolved effect as attention-required",
        resumed.returncode == 0
        and pending_after_resume.state == "attention-required"
        and pending_after_resume.pending_effect == "open-surface",
    )

    create_cli_operation("op-resume-timeout-cli", state="awaiting-callback")
    timed_out = store.read("owner-cli", "op-resume-timeout-cli")
    store.save(
        replace(
            timed_out,
            attempt=1,
            attempt_limit=3,
            deadline_at=1.0,
            token_limit=100,
            revision=timed_out.revision + 1,
        ),
        expected_revision=timed_out.revision,
    )
    timeout_session = (
        store.root
        / "owners"
        / "owner-cli"
        / "runtime"
        / "op-resume-timeout-cli"
        / "session.json"
    )
    timeout_session.parent.mkdir(parents=True)
    timeout_session.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": "op-resume-timeout-cli",
                "run_id": "run-op-resume-timeout-cli",
                "time_budget_seconds": 30.0,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    store.transition(
        "owner-cli",
        "op-resume-timeout-cli",
        "attention-required",
        reason=AttentionReason.CALLBACK_INVALID,
    )
    resumed_timeout = run_cli("resume", "op-resume-timeout-cli")
    timeout_after_resume = store.read(
        "owner-cli", "op-resume-timeout-cli"
    )
    check(
        "CLI resume rearms one expired callback window after explicit recovery",
        resumed_timeout.returncode == 0
        and timeout_after_resume.state == "awaiting-callback"
        and timeout_after_resume.attempt == 2
        and timeout_after_resume.deadline_at > time.time(),
    )

    create_cli_operation("op-resume-cleanup-cli", state="exiting")
    bind_owned_resources(
        "op-resume-cleanup-cli",
        OwnedResources(
            "11111111-1111-1111-1111-111111111111",
            42,
            0,
            "a" * 64,
            "",
        ),
    )
    write_workspace_session("op-resume-cleanup-cli")
    write_callback_target("op-resume-cleanup-cli")
    store.transition(
        "owner-cli",
        "op-resume-cleanup-cli",
        "attention-required",
        reason=AttentionReason.CLEANUP_INCOMPLETE,
    )
    cleanup_resume_rc, _cleanup_resume_output = run_cli_in_process(
        "resume",
        "op-resume-cleanup-cli",
        process=FakeProcess("dead"),
        cmux=FakeCmux("missing"),
    )
    cleanup_resume = store.read("owner-cli", "op-resume-cleanup-cli")
    check(
        "CLI resume restores interrupted cleanup and proves zero owned resources",
        cleanup_resume_rc == 0
        and cleanup_resume.state == "complete"
        and cleanup_resume.resources == OwnedResources(),
    )
    reconciled_pending = run_cli("reconcile")
    reconcile_rows = json.loads(reconciled_pending.stdout)
    check(
        "CLI reconcile reports the durable pending-effect recovery boundary",
        reconciled_pending.returncode == 0
        and any(
            row["operation_id"] == "op-pending-cli"
            and row["state"] == "attention-required"
            and row["action"] == "inspect-pending-effect"
            for row in reconcile_rows
        ),
    )

    create_cli_operation("op-cancel-cli", state="running")
    cancelled = run_cli("cancel", "op-cancel-cli")
    check(
        "CLI cancel reaches a terminal state when no resources remain",
        cancelled.returncode == 0
        and store.read("owner-cli", "op-cancel-cli").state == "cancelled",
    )

    create_cli_operation("op-close-cli", state="cancelling")
    closed = run_cli("close", "op-close-cli")
    check(
        "CLI close completes an interrupted cancelling transition",
        closed.returncode == 0
        and store.read("owner-cli", "op-close-cli").state == "cancelled",
    )

    create_cli_operation("op-reconcile-cli", state="cancelling")
    reconciled_cancel = run_cli("reconcile")
    cancel_rows = json.loads(reconciled_cancel.stdout)
    check(
        "CLI reconcile completes a resource-free cancelling operation",
        reconciled_cancel.returncode == 0
        and store.read("owner-cli", "op-reconcile-cli").state == "cancelled"
        and any(
            row["operation_id"] == "op-reconcile-cli"
            and row["state"] == "cancelled"
            and row["action"] == "cancel-complete"
            for row in cancel_rows
        ),
    )

    create_cli_operation("op-healthy-cli", state="awaiting-callback")
    bind_owned_resources(
        "op-healthy-cli",
        OwnedResources("11111111-1111-1111-1111-111111111111"),
    )
    healthy_rc, healthy_rows = run_cli_in_process(
        "reconcile",
        "",
        process=FakeProcess("alive"),
        cmux=FakeCmux("alive"),
    )
    healthy_after_reconcile = store.read("owner-cli", "op-healthy-cli")
    check(
        "CLI reconcile keeps healthy exact resources report-only",
        healthy_rc == 0
        and healthy_after_reconcile.state == "awaiting-callback"
        and bool(healthy_after_reconcile.resources.surface_id)
        and any(
            row["operation_id"] == "op-healthy-cli"
            and row["state"] == "awaiting-callback"
            and row["action"] == "resources-live"
            for row in healthy_rows
        ),
    )

    create_cli_operation("op-dead-cli", state="running")
    bind_owned_resources(
        "op-dead-cli",
        OwnedResources("11111111-1111-1111-1111-111111111111"),
    )
    dead_rc, _dead_output = run_cli_in_process(
        "cancel",
        "op-dead-cli",
        process=FakeProcess("dead"),
        cmux=FakeCmux("missing"),
    )
    dead_after_cancel = store.read("owner-cli", "op-dead-cli")
    check(
        "CLI cancel clears only proven-dead exact resources",
        dead_rc == 0
        and dead_after_cancel.state == "cancelled"
        and dead_after_cancel.resources == OwnedResources(),
    )

    create_cli_operation("op-orphan-surface-cli", state="running")
    bind_owned_resources("op-orphan-surface-cli")
    orphan_surface_process = FakeProcess("dead")
    orphan_surface_cmux = FakeCmux("alive")
    orphan_surface_rc, _orphan_surface_output = run_cli_in_process(
        "cancel",
        "op-orphan-surface-cli",
        process=orphan_surface_process,
        cmux=orphan_surface_cmux,
    )
    orphan_surface_after = store.read("owner-cli", "op-orphan-surface-cli")
    check(
        "CLI cancel closes an exact orphan surface and confirms its removal",
        orphan_surface_rc == 0
        and orphan_surface_cmux.closed
        == ["11111111-1111-1111-1111-111111111111"]
        and orphan_surface_after.state == "cancelled"
        and orphan_surface_after.resources == OwnedResources(),
    )

    create_cli_operation("op-orphan-workspace-cli", state="running")
    bind_owned_resources("op-orphan-workspace-cli")
    workspace_identity = write_workspace_session(
        "op-orphan-workspace-cli"
    )
    orphan_workspace_process = FakeProcess("dead")
    orphan_workspace_cmux = FakeCmux("alive")
    orphan_workspace_rc, _orphan_workspace_output = run_cli_in_process(
        "cancel",
        "op-orphan-workspace-cli",
        process=orphan_workspace_process,
        cmux=orphan_workspace_cmux,
    )
    orphan_workspace_after = store.read(
        "owner-cli", "op-orphan-workspace-cli"
    )
    check(
        "CLI cancel closes and verifies a metadata-owned workspace",
        orphan_workspace_rc == 0
        and orphan_workspace_cmux.closed_workspaces
        == [workspace_identity]
        and orphan_workspace_cmux.closed == []
        and orphan_workspace_after.state == "cancelled"
        and orphan_workspace_after.resources == OwnedResources(),
    )

    create_cli_operation("op-starting-workspace-cli", state="starting")
    bind_owned_resources(
        "op-starting-workspace-cli",
        OwnedResources("11111111-1111-1111-1111-111111111111"),
    )
    write_workspace_session("op-starting-workspace-cli")
    starting_workspace_cmux = FakeCmux("alive")
    starting_workspace_rc, starting_workspace_rows = run_cli_in_process(
        "reconcile",
        "",
        process=FakeProcess("unknown"),
        cmux=starting_workspace_cmux,
    )
    starting_workspace_after = store.read(
        "owner-cli", "op-starting-workspace-cli"
    )
    check(
        "CLI reconcile never closes a workspace before process binding",
        starting_workspace_rc == 0
        and starting_workspace_after.state == "starting"
        and bool(starting_workspace_after.resources.surface_id)
        and starting_workspace_cmux.closed_workspaces == []
        and any(
            row["operation_id"] == "op-starting-workspace-cli"
            and row["action"] == "resources-live"
            for row in starting_workspace_rows
        ),
    )

    create_cli_operation("op-drift-workspace-cli", state="running")
    bind_owned_resources("op-drift-workspace-cli")
    write_workspace_session("op-drift-workspace-cli")
    drift_workspace_cmux = FakeCmux("missing")
    drift_workspace_cmux.workspace_current = "drift"
    drift_workspace_rc, _drift_workspace_output = run_cli_in_process(
        "cancel",
        "op-drift-workspace-cli",
        process=FakeProcess("dead"),
        cmux=drift_workspace_cmux,
    )
    drift_workspace_after = store.read(
        "owner-cli", "op-drift-workspace-cli"
    )
    check(
        "CLI cancel retains ownership on workspace identity drift",
        drift_workspace_rc == 0
        and drift_workspace_after.state == "attention-required"
        and bool(drift_workspace_after.resources.surface_id)
        and drift_workspace_cmux.closed_workspaces == [],
    )

    create_cli_operation("op-orphan-process-cli", state="running")
    bind_owned_resources("op-orphan-process-cli")
    orphan_process = FakeProcess("alive")
    orphan_process_cmux = FakeCmux("missing")
    orphan_process_rc, _orphan_process_output = run_cli_in_process(
        "cancel",
        "op-orphan-process-cli",
        process=orphan_process,
        cmux=orphan_process_cmux,
    )
    orphan_process_after = store.read("owner-cli", "op-orphan-process-cli")
    check(
        "CLI cancel leaves an unguarded orphan process attention-required",
        orphan_process_rc == 0
        and orphan_process.terminated == []
        and orphan_process_after.state == "attention-required"
        and orphan_process_after.attention_reason
        == AttentionReason.CLEANUP_INCOMPLETE
        and orphan_process_after.resources.process_group == 42,
    )

    create_cli_operation("op-owned-cli", state="running")
    bind_owned_resources("op-owned-cli")
    owned_process = FakeProcess("alive")
    contained_rc, _contained_output = run_cli_in_process(
        "cancel",
        "op-owned-cli",
        process=owned_process,
        cmux=FakeCmux("alive"),
    )
    owned_after_cancel = store.read("owner-cli", "op-owned-cli")
    check(
        "CLI cancel asks the exact live guardian to exit",
        contained_rc == 0
        and owned_after_cancel.state == "exiting"
        and bool(owned_after_cancel.resources.surface_id)
        and len(owned_process.guardian_requests) == 1
        and owned_process.guardian_requests[0]["action"] == "request-exit"
        and owned_process.guardian_requests[0]["operation_id"]
        == "op-owned-cli",
    )

    create_cli_operation("op-unknown-cli", state="running")
    bind_owned_resources("op-unknown-cli")
    unknown_rc, _unknown_output = run_cli_in_process(
        "close",
        "op-unknown-cli",
        process=FakeProcess("unknown"),
        cmux=FakeCmux("unknown"),
    )
    unknown_after_close = store.read("owner-cli", "op-unknown-cli")
    check(
        "CLI close keeps unknown exact resources attention-required",
        unknown_rc == 0
        and unknown_after_close.state == "attention-required"
        and bool(unknown_after_close.resources.surface_id)
        and unknown_after_close.attention_reason
        == AttentionReason.CLEANUP_INCOMPLETE,
    )
