#!/usr/bin/env python3
"""Hermetic operation-store, state-machine, and CLI tests."""

from __future__ import annotations

import json
import hashlib
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
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from harness.contracts import (
    AttentionReason,
    CallbackEnvelope,
    ContractError,
    EffectOutcome,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.callbacks import CallbackBroker
import harness.cli as harness_cli
from harness.cli import main as harness_cli_main
from harness.runtime_provider_events import RuntimeProviderEventStream
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
    current = store.read("owner-1", "op-1")
    try:
        store.save(current, expected_revision=current.revision + 1)
    except StoreError:
        check("stale operation revision fails closed", True)
    else:
        check("stale operation revision fails closed", False)
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

    timeout_spec = OperationSpec(
        "op-timeout",
        "key-timeout",
        "review-session",
        "owner-timeout",
        route,
        "packet.json",
        "scoped",
    )
    timeout_record = store.create(
        timeout_spec,
        lane_id="lane-timeout",
        run_id="run-timeout",
    )
    timeout_record = replace(
        timeout_record,
        deadline_at=1.0,
        revision=timeout_record.revision + 1,
    )
    store.save(timeout_record, expected_revision=0)
    store.transition(
        "owner-timeout",
        "op-timeout",
        "attention-required",
        reason=AttentionReason.CALLBACK_TIMEOUT,
    )
    for invalid_deadline in (None, True, float("nan"), 0.0):
        try:
            store.rearm_callback_timeout(
                "owner-timeout",
                "op-timeout",
                deadline_at=invalid_deadline,
            )
        except StoreError:
            pass
        else:
            raise AssertionError(
                "callback timeout rearm accepted an invalid deadline"
            )
    check("callback timeout rearm rejects invalid deadlines", True)
    rearmed = store.rearm_callback_timeout(
        "owner-timeout",
        "op-timeout",
        deadline_at=500.0,
    )
    check(
        "callback timeout rearm restores state and deadline atomically",
        rearmed.state == "awaiting-callback"
        and rearmed.attention_reason is None
        and rearmed.deadline_at == 500.0
        and rearmed.revision == 3,
    )
    try:
        store.rearm_callback_timeout(
            "owner-1",
            "op-1",
            deadline_at=500.0,
        )
    except StoreError:
        check("callback timeout rearm rejects unrelated attention", True)
    else:
        check("callback timeout rearm rejects unrelated attention", False)

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

    non_object_spec = OperationSpec(
        "op-non-object",
        "key-non-object",
        "dispatch",
        "owner-non-object",
        route,
        "packet.json",
        "scoped",
    )
    store.create(
        non_object_spec,
        lane_id="lane-non-object",
        run_id="run-non-object",
    )
    non_object_path = (
        root
        / "state/owners/owner-non-object/operations/op-non-object.json"
    )
    non_object_path.write_text("[]\n", encoding="utf-8")
    try:
        store.read("owner-non-object", "op-non-object")
    except StoreError:
        check("non-object durable state fails closed through the store seam", True)
    else:
        check("non-object durable state fails closed through the store seam", False)

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
        def __init__(
            self,
            status: str,
            *,
            supervisor_status: str | None = None,
            capture_matches: bool = False,
        ) -> None:
            self.status = status
            self.supervisor_status = supervisor_status or status
            self.capture_matches = capture_matches
            self.terminated: list[int] = []
            self.guardian_requests: list[dict[str, object]] = []

        def process_status(
            self, _process_group: int, _identity: str
        ) -> str:
            return self.status

        def pid_status(self, _pid: int, _identity: str) -> str:
            return self.supervisor_status

        def capture_identity(
            self, pid: int, *, process_group: int = 0
        ) -> str:
            if not self.capture_matches:
                return "c" * 64
            if pid == 42 and process_group == 42:
                return "a" * 64
            if pid == 43 and process_group == 0:
                return "b" * 64
            return ""

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

    class ExitAfterProbeProcess(FakeProcess):
        exit_requested = False
        exit_probes = 0

        def process_status(
            self, process_group: int, identity: str
        ) -> str:
            del process_group, identity
            if self.exit_requested:
                self.exit_probes += 1
                if self.exit_probes >= 2:
                    self.status = "dead"
                    self.supervisor_status = "dead"
            return self.status

        def request_guardian_signal(
            self,
            control_path: Path,
            **request: object,
        ) -> None:
            super().request_guardian_signal(control_path, **request)
            if request.get("action") == "request-exit":
                self.exit_requested = True

    class UnclosedCmux(FakeCmux):
        def close_exact(self, surface_id: str) -> None:
            self.closed.append(surface_id)

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

    def accept_result_callback(operation_id: str) -> str:
        record = store.read("owner-cli", operation_id)
        payload = {"status": "complete"}
        payload_sha = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        CallbackBroker(store, "owner-cli").accept(
            CallbackEnvelope(
                f"callback-{operation_id}",
                operation_id,
                record.run_id,
                "result",
                payload,
                payload_sha,
            )
        )
        return payload_sha

    def create_provider_stream(
        operation_id: str,
    ) -> RuntimeProviderEventStream:
        record = store.read("owner-cli", operation_id)
        stream = RuntimeProviderEventStream.create(
            store.root
            / "owners"
            / "owner-cli"
            / "runtime"
            / operation_id
            / "provider-events",
            owner_id="owner-cli",
            operation_id=operation_id,
            run_id=record.run_id,
            generation=1,
            process_identity=record.resources.process_identity,
            workspace_id="22222222-2222-4222-8222-222222222222",
            surface_id=record.resources.surface_id,
            input_sha256="c" * 64,
        )
        assert stream.start().action == "wait"
        assert stream.reserve_input().action == "send"
        assert stream.accept_input().action == "wait"
        return stream

    def publish_provider_result(
        operation_id: str,
        payload_sha256: str,
    ) -> None:
        stream = create_provider_stream(operation_id)
        assert stream.result(payload_sha256).action == "close"

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

    def write_split_session(operation_id: str) -> None:
        record = store.read("owner-cli", operation_id)
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
                    "placement": "split",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        write_callback_target(operation_id)

    def create_review_cleanup_operation(
        operation_id: str,
        *,
        awaiting_callback: bool = False,
        missing_checkpoint: bool = False,
        runtime: str = "claude",
        model: str = "fable",
        launch_model: str = "",
        session_checkpoint: str = "",
        launch_surface: str = "11111111-1111-1111-1111-111111111111",
    ) -> tuple[str, str]:
        review_route = RuntimeRoute(
            runtime,
            model,
            "xhigh",
            "reviewer-callback",
            "d" * 64,
        )
        review_spec = OperationSpec(
            operation_id,
            f"key-{operation_id}",
            "deep-review-spec",
            "owner-cli",
            review_route,
            "packets/review.json",
            "scoped",
        )
        run_id = f"run-{operation_id}"
        store.create(review_spec, lane_id="lane-review", run_id=run_id)
        for state in ("preflight", "starting", "running"):
            store.transition("owner-cli", operation_id, state)
        bind_owned_resources(operation_id)
        if awaiting_callback:
            store.transition("owner-cli", operation_id, "awaiting-callback")
        else:
            store.transition(
                "owner-cli",
                operation_id,
                "attention-required",
                reason=AttentionReason.CLEANUP_INCOMPLETE,
            )
        state_root = (
            store.root
            / "owners"
            / "owner-cli"
            / "runtime"
            / operation_id
        )
        state_root.mkdir(parents=True)
        scratch = root / "review-scratch" / operation_id
        product = root / "review-product" / operation_id
        scratch.mkdir(parents=True)
        product.mkdir(parents=True)
        workspace_id = "22222222-2222-4222-8222-222222222222"
        window_id = "33333333-3333-4333-8333-333333333333"
        values = {
            "session.json": {
                "schema_version": 1,
                "operation_id": operation_id,
                "run_id": run_id,
                "cwd": str(scratch.resolve()),
                "product_root": str(product.resolve()),
                "placement": "workspace",
                "workspace_id": workspace_id,
                "workspace_ref": "workspace:1",
                "window_id": window_id,
                "window_ref": "window:1",
                "surface_ref": "surface:1",
                "callback_mode": "envelope",
                "checkpoint": session_checkpoint,
            },
            "launch.json": {
                "schema_version": 1,
                "owner_id": "owner-cli",
                "operation_id": operation_id,
                "run_id": run_id,
                "runtime": runtime,
                "cwd": str(scratch.resolve()),
                "product_root": str(product.resolve()),
                "surface_id": launch_surface,
                "store_root": str(store.root.resolve()),
                "argv": [
                    runtime,
                    "--model",
                    launch_model or model,
                    f"Product worktree (read-only): `{product.resolve()}`.",
                ],
            },
            "ready.json": {
                "schema_version": 1,
                "status": "ready",
                "pid": 42,
                "process_group": 42,
                "process_identity": "a" * 64,
                "supervisor_pid": 43,
                "supervisor_identity": "b" * 64,
            },
            "checkpoint.json": {
                "schema_version": 1,
                "operation_id": operation_id,
                "run_id": run_id,
                "runtime": runtime,
                "checkpoint": f"checkpoint-{operation_id}",
            },
        }
        if missing_checkpoint:
            values.pop("checkpoint.json")
        for name, value in values.items():
            (state_root / name).write_text(
                json.dumps(value, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        write_callback_target(operation_id)
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

    create_cli_operation("op-accepted-reconcile-cli", state="exiting")
    accepted_reconcile = store.read("owner-cli", "op-accepted-reconcile-cli")
    store.save(
        replace(
            accepted_reconcile,
            revision=accepted_reconcile.revision + 1,
            accepted_callback_id="accepted-callback-1",
            accepted_callback_kind="result",
            accepted_callback_sha256="a" * 64,
        ),
        expected_revision=accepted_reconcile.revision,
    )
    reconciled_accepted = run_cli("reconcile")
    accepted_rows = json.loads(reconciled_accepted.stdout)
    check(
        "CLI reconcile preserves an accepted callback as completion",
        reconciled_accepted.returncode == 0
        and store.read("owner-cli", "op-accepted-reconcile-cli").state
        == "complete"
        and any(
            row["operation_id"] == "op-accepted-reconcile-cli"
            and row["state"] == "complete"
            and row["action"] == "callback-complete"
            for row in accepted_rows
        ),
    )

    create_cli_operation("op-accepted-cancel-cli", state="exiting")
    accepted_cancel = store.read("owner-cli", "op-accepted-cancel-cli")
    store.save(
        replace(
            accepted_cancel,
            revision=accepted_cancel.revision + 1,
            accepted_callback_id="accepted-callback-cancel",
            accepted_callback_kind="result",
            accepted_callback_sha256="b" * 64,
        ),
        expected_revision=accepted_cancel.revision,
    )
    cancelled_accepted = run_cli("cancel", "op-accepted-cancel-cli")
    check(
        "CLI cancel preserves an accepted callback as completion",
        cancelled_accepted.returncode == 0
        and store.read("owner-cli", "op-accepted-cancel-cli").state
        == "complete",
    )

    create_cli_operation("op-plain-reconcile-cli", state="cancelling")
    plain_reconcile = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/harness-cli.py"),
            "--store",
            str(root / "state"),
            "--owner",
            "owner-cli",
            "reconcile",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "CLI plain reconcile renders action rows without a kind field",
        plain_reconcile.returncode == 0
        and "op-plain-reconcile-cli\tcancelled\tcancel-complete"
        in plain_reconcile.stdout,
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

    create_cli_operation("op-invalid-callback-cli", state="running")
    bind_owned_resources("op-invalid-callback-cli")
    store.transition(
        "owner-cli",
        "op-invalid-callback-cli",
        "attention-required",
        reason=AttentionReason.CALLBACK_INVALID,
    )
    invalid_callback_rc, _invalid_callback_output = run_cli_in_process(
        "cancel",
        "op-invalid-callback-cli",
        process=FakeProcess("dead"),
        cmux=FakeCmux("missing"),
    )
    invalid_callback_after = store.read(
        "owner-cli", "op-invalid-callback-cli"
    )
    check(
        "CLI cancel clears proven-dead callback-invalid resources",
        invalid_callback_rc == 0
        and invalid_callback_after.state == "cancelled"
        and invalid_callback_after.resources == OwnedResources(),
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
    write_workspace_session(
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
        "CLI cancel closes only the exact surface in a live observer workspace",
        orphan_workspace_rc == 0
        and orphan_workspace_cmux.closed_workspaces == []
        and orphan_workspace_cmux.closed
        == ["11111111-1111-1111-1111-111111111111"]
        and orphan_workspace_cmux.workspace_current == "alive"
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
        "CLI cancel ignores workspace drift after exact surface disappearance",
        drift_workspace_rc == 0
        and drift_workspace_after.state == "cancelled"
        and drift_workspace_after.resources == OwnedResources()
        and drift_workspace_cmux.closed_workspaces == [],
    )

    create_cli_operation("op-orphan-process-cli", state="running")
    bind_owned_resources("op-orphan-process-cli")
    orphan_process = FakeProcess("alive")
    orphan_process_cmux = FakeCmux("missing")
    orphan_process_rc, orphan_process_output = run_cli_in_process(
        "cancel",
        "op-orphan-process-cli",
        process=orphan_process,
        cmux=orphan_process_cmux,
    )
    orphan_process_after = store.read("owner-cli", "op-orphan-process-cli")
    check(
        "CLI cancel leaves an unguarded orphan process attention-required",
        orphan_process_rc == harness_cli.CASCADE_PARTIAL_EXIT
        and orphan_process_output["status"] == "partial"
        and orphan_process.terminated == []
        and orphan_process_after.state == "attention-required"
        and orphan_process_after.attention_reason
        == AttentionReason.CLEANUP_INCOMPLETE
        and orphan_process_after.resources.process_group == 42,
    )

    create_cli_operation("op-owned-cli", state="running")
    bind_owned_resources("op-owned-cli")
    write_split_session("op-owned-cli")
    owned_process = ExitAfterProbeProcess("alive")
    owned_cmux = FakeCmux("alive")
    contained_rc, _contained_output = run_cli_in_process(
        "cancel",
        "op-owned-cli",
        process=owned_process,
        cmux=owned_cmux,
    )
    owned_after_cancel = store.read("owner-cli", "op-owned-cli")
    check(
        "one CLI cancel exits and cleans one exact live provider",
        contained_rc == 0
        and owned_after_cancel.state == "cancelled"
        and owned_after_cancel.resources == OwnedResources()
        and len(owned_process.guardian_requests) == 1
        and owned_process.guardian_requests[0]["action"] == "request-exit"
        and owned_process.guardian_requests[0]["operation_id"]
        == "op-owned-cli"
        and owned_cmux.closed
        == ["11111111-1111-1111-1111-111111111111"],
    )
    replay_rc, _replay_output = run_cli_in_process(
        "cancel",
        "op-owned-cli",
        process=owned_process,
        cmux=owned_cmux,
    )
    check(
        "repeated CLI cancel replays no provider exit or cleanup effect",
        replay_rc == 0
        and store.read("owner-cli", "op-owned-cli") == owned_after_cancel
        and len(owned_process.guardian_requests) == 1
        and owned_cmux.closed
        == ["11111111-1111-1111-1111-111111111111"],
    )

    create_cli_operation("op-modern-cancel-cli", state="running")
    bind_owned_resources("op-modern-cancel-cli")
    write_workspace_session("op-modern-cancel-cli")
    write_callback_target("op-modern-cancel-cli")
    create_provider_stream("op-modern-cancel-cli")
    modern_cancel_process = ExitAfterProbeProcess("alive")
    modern_cancel_cmux = FakeCmux("alive")
    modern_cancel_rc, _modern_cancel_output = run_cli_in_process(
        "cancel",
        "op-modern-cancel-cli",
        process=modern_cancel_process,
        cmux=modern_cancel_cmux,
    )
    modern_cancelled = store.read("owner-cli", "op-modern-cancel-cli")
    check(
        "modern cancellation publishes close authority without a result",
        modern_cancel_rc == 0
        and modern_cancelled.state == "cancelled"
        and modern_cancelled.resources == OwnedResources()
        and modern_cancel_cmux.closed_workspaces == []
        and modern_cancel_cmux.closed
        == ["11111111-1111-1111-1111-111111111111"],
    )

    create_cli_operation("op-unclosed-cli", state="running")
    bind_owned_resources("op-unclosed-cli")
    write_split_session("op-unclosed-cli")
    unclosed_process = ExitAfterProbeProcess("alive")
    unclosed_cmux = UnclosedCmux("alive")
    unclosed_rc, unclosed_output = run_cli_in_process(
        "cancel",
        "op-unclosed-cli",
        process=unclosed_process,
        cmux=unclosed_cmux,
    )
    unclosed_after = store.read("owner-cli", "op-unclosed-cli")
    check(
        "CLI cancel fails closed when exact surface close is unproved",
        unclosed_rc == harness_cli.CASCADE_PARTIAL_EXIT
        and unclosed_output["status"] == "partial"
        and unclosed_after.state == "attention-required"
        and unclosed_after.attention_reason
        == AttentionReason.CLEANUP_INCOMPLETE
        and bool(unclosed_after.resources.surface_id)
        and len(unclosed_process.guardian_requests) == 1,
    )

    create_cli_operation("op-still-alive-cli", state="running")
    bind_owned_resources("op-still-alive-cli")
    write_split_session("op-still-alive-cli")
    still_alive_process = FakeProcess("alive")
    cancel_waits: list[float] = []
    with patch(
        "harness.runtime_session_cancel.sleep",
        side_effect=cancel_waits.append,
    ):
        still_alive_rc, still_alive_output = run_cli_in_process(
            "cancel",
            "op-still-alive-cli",
            process=still_alive_process,
            cmux=FakeCmux("alive"),
        )
    still_alive_after = store.read("owner-cli", "op-still-alive-cli")
    check(
        "CLI cancel exhausts one fixed probe budget without replaying exit",
        still_alive_rc == harness_cli.CASCADE_PARTIAL_EXIT
        and still_alive_output["status"] == "partial"
        and still_alive_after.state == "exiting"
        and bool(still_alive_after.resources.surface_id)
        and len(still_alive_process.guardian_requests) == 1
        and len(cancel_waits) == 39
        and set(cancel_waits) == {0.05},
    )

    create_review_cleanup_operation("op-review-proof-cli")
    proof_process = FakeProcess(
        "unknown", supervisor_status="unknown", capture_matches=True
    )
    proof_cmux = FakeCmux("alive")
    proof_rc, _proof_output = run_cli_in_process(
        "close",
        "op-review-proof-cli",
        process=proof_process,
        cmux=proof_cmux,
    )
    proof_after_exit = store.read("owner-cli", "op-review-proof-cli")
    check(
        "CLI reviewer cleanup reconstructs exact durable parent ownership",
        proof_rc == 0
        and proof_after_exit.state == "exiting"
        and len(proof_process.guardian_requests) == 1
        and proof_process.guardian_requests[0]["operation_id"]
        == "op-review-proof-cli"
        and proof_cmux.closed_workspaces == [],
    )
    proof_process.status = "dead"
    proof_process.supervisor_status = "dead"
    proof_cmux.current = "missing"
    proof_cmux.workspace_current = "missing"
    partial_rc, _partial_output = run_cli_in_process(
        "close",
        "op-review-proof-cli",
        process=proof_process,
        cmux=proof_cmux,
    )
    replay_rc, _replay_output = run_cli_in_process(
        "close",
        "op-review-proof-cli",
        process=proof_process,
        cmux=proof_cmux,
    )
    proof_terminal = store.read("owner-cli", "op-review-proof-cli")
    check(
        "CLI reviewer cleanup without result retains ownership idempotently",
        partial_rc == 0
        and replay_rc == 0
        and proof_terminal.state == "exiting"
        and proof_terminal.resources.surface_id
        == "11111111-1111-1111-1111-111111111111"
        and len(proof_process.guardian_requests) == 1
        and proof_cmux.closed_workspaces == [],
    )

    create_review_cleanup_operation(
        "op-review-observer-cli", awaiting_callback=True
    )
    observer_payload_sha256 = accept_result_callback(
        "op-review-observer-cli"
    )
    publish_provider_result(
        "op-review-observer-cli", observer_payload_sha256
    )
    observer_process = FakeProcess(
        "unknown", supervisor_status="unknown", capture_matches=True
    )
    observer_cmux = FakeCmux("alive")
    observer_exit_rc, _observer_exit_output = run_cli_in_process(
        "close",
        "op-review-observer-cli",
        process=observer_process,
        cmux=observer_cmux,
    )
    observer_process.status = "dead"
    observer_process.supervisor_status = "dead"
    observer_cmux.current = "missing"
    observer_cmux.workspace_current = "alive"
    observer_cleanup_rc, _observer_cleanup_output = run_cli_in_process(
        "close",
        "op-review-observer-cli",
        process=observer_process,
        cmux=observer_cmux,
    )
    observer_terminal = store.read("owner-cli", "op-review-observer-cli")
    check(
        "CLI reviewer cleanup preserves a live observer workspace",
        observer_exit_rc == 0
        and observer_cleanup_rc == 0
        and observer_terminal.state == "complete"
        and observer_terminal.resources == OwnedResources()
        and observer_cmux.closed_workspaces == []
        and observer_cmux.workspace_current == "alive",
    )

    create_review_cleanup_operation("op-review-reused-cli")
    reused_process = FakeProcess(
        "unknown", supervisor_status="unknown", capture_matches=False
    )
    reused_rc, _reused_output = run_cli_in_process(
        "close",
        "op-review-reused-cli",
        process=reused_process,
        cmux=FakeCmux("alive"),
    )
    reused_after = store.read("owner-cli", "op-review-reused-cli")
    check(
        "CLI reviewer cleanup rejects a stale or reused process identity",
        reused_rc == 0
        and reused_after.state == "attention-required"
        and reused_process.guardian_requests == [],
    )

    create_review_cleanup_operation("op-review-foreign-cli")
    foreign_cmux = FakeCmux("alive")
    foreign_cmux.workspace_current = "drift"
    foreign_process = FakeProcess(
        "unknown", supervisor_status="unknown", capture_matches=True
    )
    foreign_rc, _foreign_output = run_cli_in_process(
        "close",
        "op-review-foreign-cli",
        process=foreign_process,
        cmux=foreign_cmux,
    )
    foreign_after = store.read("owner-cli", "op-review-foreign-cli")
    check(
        "CLI reviewer cleanup ignores workspace drift with exact surface identity",
        foreign_rc == 0
        and foreign_after.state == "exiting"
        and len(foreign_process.guardian_requests) == 1
        and foreign_cmux.closed_workspaces == [],
    )

    create_review_cleanup_operation(
        "op-review-foreign-surface-cli",
        launch_surface="44444444-4444-4444-8444-444444444444",
    )
    foreign_surface_process = FakeProcess(
        "unknown", supervisor_status="unknown", capture_matches=True
    )
    foreign_surface_rc, _foreign_surface_output = run_cli_in_process(
        "close",
        "op-review-foreign-surface-cli",
        process=foreign_surface_process,
        cmux=FakeCmux("alive"),
    )
    foreign_surface_after = store.read(
        "owner-cli", "op-review-foreign-surface-cli"
    )
    check(
        "CLI reviewer cleanup rejects a foreign launch surface",
        foreign_surface_rc == 0
        and foreign_surface_after.state == "attention-required"
        and foreign_surface_process.guardian_requests == [],
    )

    create_review_cleanup_operation(
        "op-review-missing-checkpoint-cli", missing_checkpoint=True
    )
    missing_process = FakeProcess(
        "unknown", supervisor_status="unknown", capture_matches=True
    )
    missing_rc, _missing_output = run_cli_in_process(
        "close",
        "op-review-missing-checkpoint-cli",
        process=missing_process,
        cmux=FakeCmux("alive"),
    )
    missing_after = store.read(
        "owner-cli", "op-review-missing-checkpoint-cli"
    )
    check(
        "CLI exact Claude cleanup does not require unavailable checkpoint evidence",
        missing_rc == 0
        and missing_after.state == "exiting"
        and len(missing_process.guardian_requests) == 1
        and missing_process.guardian_requests[0]["operation_id"]
        == "op-review-missing-checkpoint-cli"
        and missing_process.guardian_requests[0]["action"]
        == "request-exit",
    )

    create_review_cleanup_operation(
        "op-review-missing-checkpoint-codex-cli",
        missing_checkpoint=True,
        runtime="codex",
        model="gpt-5.6-sol",
    )
    missing_codex_process = FakeProcess(
        "unknown", supervisor_status="unknown", capture_matches=True
    )
    missing_codex_rc, _missing_codex_output = run_cli_in_process(
        "close",
        "op-review-missing-checkpoint-codex-cli",
        process=missing_codex_process,
        cmux=FakeCmux("alive"),
    )
    missing_codex_after = store.read(
        "owner-cli", "op-review-missing-checkpoint-codex-cli"
    )
    check(
        "CLI non-Claude cleanup keeps missing checkpoint evidence fail-closed",
        missing_codex_rc == 0
        and missing_codex_after.state == "attention-required"
        and missing_codex_after.attention_reason
        == AttentionReason.CLEANUP_INCOMPLETE
        and missing_codex_process.guardian_requests == [],
    )

    create_review_cleanup_operation(
        "op-review-lost-requested-checkpoint-cli",
        missing_checkpoint=True,
        session_checkpoint="expected-resume-checkpoint",
    )
    lost_requested_process = FakeProcess(
        "unknown", supervisor_status="unknown", capture_matches=True
    )
    lost_requested_rc, _lost_requested_output = run_cli_in_process(
        "close",
        "op-review-lost-requested-checkpoint-cli",
        process=lost_requested_process,
        cmux=FakeCmux("alive"),
    )
    lost_requested_after = store.read(
        "owner-cli", "op-review-lost-requested-checkpoint-cli"
    )
    check(
        "CLI checkpointless Claude cleanup rejects lost requested evidence",
        lost_requested_rc == 0
        and lost_requested_after.state == "attention-required"
        and lost_requested_process.guardian_requests == [],
    )

    create_review_cleanup_operation(
        "op-review-missing-checkpoint-route-drift-cli",
        missing_checkpoint=True,
        launch_model="foreign-model",
    )
    route_drift_process = FakeProcess(
        "unknown", supervisor_status="unknown", capture_matches=True
    )
    route_drift_rc, _route_drift_output = run_cli_in_process(
        "close",
        "op-review-missing-checkpoint-route-drift-cli",
        process=route_drift_process,
        cmux=FakeCmux("alive"),
    )
    route_drift_after = store.read(
        "owner-cli", "op-review-missing-checkpoint-route-drift-cli"
    )
    check(
        "CLI checkpointless Claude cleanup still proves the exact model route",
        route_drift_rc == 0
        and route_drift_after.state == "attention-required"
        and route_drift_process.guardian_requests == [],
    )

    create_cli_operation(
        "op-accepted-unknown-cli", state="awaiting-callback"
    )
    bind_owned_resources("op-accepted-unknown-cli")
    write_workspace_session("op-accepted-unknown-cli")
    write_callback_target("op-accepted-unknown-cli")
    accepted_payload_sha256 = accept_result_callback(
        "op-accepted-unknown-cli"
    )
    publish_provider_result(
        "op-accepted-unknown-cli",
        accepted_payload_sha256,
    )
    accepted_process = FakeProcess(
        "unknown",
        supervisor_status="unknown",
        capture_matches=True,
    )
    accepted_cmux = FakeCmux("alive")
    accepted_rc, _accepted_output = run_cli_in_process(
        "close",
        "op-accepted-unknown-cli",
        process=accepted_process,
        cmux=accepted_cmux,
    )
    accepted_after_exit = store.read(
        "owner-cli", "op-accepted-unknown-cli"
    )
    check(
        "CLI accepted callback exact identity advances signal-less exit",
        accepted_rc == 0
        and accepted_after_exit.state == "exiting"
        and len(accepted_process.guardian_requests) == 1,
    )
    accepted_process.status = "dead"
    accepted_process.supervisor_status = "dead"
    cleanup_rc, _cleanup_output = run_cli_in_process(
        "resume",
        "op-accepted-unknown-cli",
        process=accepted_process,
        cmux=accepted_cmux,
    )
    accepted_complete = store.read(
        "owner-cli", "op-accepted-unknown-cli"
    )
    check(
        "CLI accepted callback exact identity completes cleanup",
        cleanup_rc == 0
        and accepted_complete.state == "complete"
        and accepted_complete.resources == OwnedResources(),
    )

    create_cli_operation(
        "op-accepted-mismatch-cli", state="awaiting-callback"
    )
    bind_owned_resources("op-accepted-mismatch-cli")
    accept_result_callback("op-accepted-mismatch-cli")
    mismatch_process = FakeProcess(
        "unknown",
        supervisor_status="unknown",
        capture_matches=False,
    )
    mismatch_rc, _mismatch_output = run_cli_in_process(
        "close",
        "op-accepted-mismatch-cli",
        process=mismatch_process,
        cmux=FakeCmux("alive"),
    )
    mismatch_after = store.read(
        "owner-cli", "op-accepted-mismatch-cli"
    )
    check(
        "CLI accepted callback identity mismatch stays fail-closed",
        mismatch_rc == 0
        and mismatch_after.state == "attention-required"
        and mismatch_after.attention_reason
        == AttentionReason.CLEANUP_INCOMPLETE
        and mismatch_process.guardian_requests == [],
    )

    create_cli_operation(
        "op-accepted-weak-cli", state="awaiting-callback"
    )
    bind_owned_resources(
        "op-accepted-weak-cli",
        OwnedResources(
            "11111111-1111-1111-1111-111111111111",
            42,
            43,
            "a" * 64,
            "",
        ),
    )
    accept_result_callback("op-accepted-weak-cli")
    weak_rc, _weak_output = run_cli_in_process(
        "close",
        "op-accepted-weak-cli",
        process=FakeProcess(
            "unknown",
            supervisor_status="unknown",
            capture_matches=True,
        ),
        cmux=FakeCmux("alive"),
    )
    weak_after = store.read("owner-cli", "op-accepted-weak-cli")
    check(
        "CLI accepted callback weak identity stays fail-closed",
        weak_rc == 0
        and weak_after.state == "attention-required"
        and weak_after.attention_reason
        == AttentionReason.CLEANUP_INCOMPLETE,
    )

    class ProbeErrorProcess(FakeProcess):
        def process_status(
            self, _process_group: int, _identity: str
        ) -> str:
            raise RuntimeError("probe unavailable")

    create_cli_operation(
        "op-accepted-probe-error-cli", state="awaiting-callback"
    )
    bind_owned_resources("op-accepted-probe-error-cli")
    accept_result_callback("op-accepted-probe-error-cli")
    probe_error_rc, _probe_error_output = run_cli_in_process(
        "close",
        "op-accepted-probe-error-cli",
        process=ProbeErrorProcess(
            "unknown",
            supervisor_status="unknown",
            capture_matches=True,
        ),
        cmux=FakeCmux("alive"),
    )
    probe_error_after = store.read(
        "owner-cli", "op-accepted-probe-error-cli"
    )
    check(
        "CLI accepted callback probe error stays fail-closed",
        probe_error_rc == 0
        and probe_error_after.state == "attention-required"
        and probe_error_after.attention_reason
        == AttentionReason.CLEANUP_INCOMPLETE,
    )

    create_cli_operation("op-noncallback-unknown-cli", state="exiting")
    bind_owned_resources("op-noncallback-unknown-cli")
    noncallback_rc, _noncallback_output = run_cli_in_process(
        "close",
        "op-noncallback-unknown-cli",
        process=FakeProcess(
            "unknown",
            supervisor_status="unknown",
            capture_matches=True,
        ),
        cmux=FakeCmux("alive"),
    )
    noncallback_after = store.read(
        "owner-cli", "op-noncallback-unknown-cli"
    )
    check(
        "CLI non-callback unknown ownership stays fail-closed",
        noncallback_rc == 0
        and noncallback_after.state == "attention-required"
        and noncallback_after.attention_reason
        == AttentionReason.CLEANUP_INCOMPLETE,
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

    create_cli_operation("op-finalizing-review-cli", state="running")
    store.transition("owner-cli", "op-finalizing-review-cli", "finalizing")
    store.transition(
        "owner-cli",
        "op-finalizing-review-cli",
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )
    create_cli_operation("op-approved-finalizing-review-cli", state="running")
    store.transition(
        "owner-cli", "op-approved-finalizing-review-cli", "finalizing"
    )
    create_cli_operation("op-approved-exiting-review-cli", state="exiting")
    store.transition(
        "owner-cli",
        "op-approved-exiting-review-cli",
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )
    create_cli_operation(
        "op-approved-live-review-cli", state="awaiting-callback"
    )
    bind_owned_resources("op-approved-live-review-cli")
    store.transition(
        "owner-cli",
        "op-approved-live-review-cli",
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )
    create_cli_operation(
        "op-findings-live-review-cli", state="awaiting-callback"
    )
    bind_owned_resources("op-findings-live-review-cli")
    store.transition(
        "owner-cli",
        "op-findings-live-review-cli",
        "attention-required",
        reason=AttentionReason.CALLBACK_INVALID,
    )
    check(
        "terminal exact approval selects callback-free recovery",
        harness_cli._review_recovery_kind(
            {
                "status": "approved",
                "execution_protocol": "exact-head-attempt-v1",
                "attempt": {
                    "status": "terminal",
                    "terminal": {"result": "approved"},
                },
            },
            ROOT / "missing-verification-response.json",
        )
        == "accepted-exact-callbacks",
    )
    check(
        "terminal exact findings select callback-free transport recovery",
        harness_cli._review_recovery_kind(
            {
                "status": "changes-requested",
                "execution_protocol": "exact-head-attempt-v1",
                "attempt": {
                    "status": "terminal",
                    "terminal": {"result": "changes-requested"},
                },
            },
            ROOT / "missing-verification-response.json",
        )
        == "accepted-exact-callbacks",
    )
    recovery_calls: list[str] = []
    original_recovery = harness_cli._recover_finalizing_review_if_present

    def accepted_review_recovery(
        _store: OperationStore,
        _owner: str,
        operation_id: str,
        *,
        runtime_manager: object | None = None,
        cmux_adapter: object | None = None,
    ) -> str:
        recovery_calls.append(operation_id)
        assert runtime_manager is None
        assert cmux_adapter is not None
        return (
            "changes-requested"
            if operation_id == "op-findings-live-review-cli"
            else "approved"
        )

    harness_cli._recover_finalizing_review_if_present = accepted_review_recovery
    try:
        first_finalizing = harness_cli._resume(
            store,
            "owner-cli",
            "op-finalizing-review-cli",
            process_adapter=FakeProcess("dead"),
            cmux_adapter=FakeCmux("missing"),
        )
        resumed_durable_finalizing = harness_cli._resume(
            store,
            "owner-cli",
            "op-approved-finalizing-review-cli",
            process_adapter=FakeProcess("dead"),
            cmux_adapter=FakeCmux("missing"),
        )
        resumed_durable_exiting = harness_cli._resume(
            store,
            "owner-cli",
            "op-approved-exiting-review-cli",
            process_adapter=FakeProcess("dead"),
            cmux_adapter=FakeCmux("missing"),
        )
        resumed_live_executor = harness_cli._resume(
            store,
            "owner-cli",
            "op-approved-live-review-cli",
            process_adapter=FakeProcess("alive"),
            cmux_adapter=FakeCmux("alive"),
        )
        resumed_findings_executor = harness_cli._resume(
            store,
            "owner-cli",
            "op-findings-live-review-cli",
            process_adapter=FakeProcess("alive"),
            cmux_adapter=FakeCmux("alive"),
        )
        second_finalizing = harness_cli._resume(
            store,
            "owner-cli",
            "op-finalizing-review-cli",
            process_adapter=FakeProcess("dead"),
            cmux_adapter=FakeCmux("missing"),
        )
    finally:
        harness_cli._recover_finalizing_review_if_present = original_recovery
    finalizing_record = store.read(
        "owner-cli", "op-finalizing-review-cli"
    )
    check(
        "accepted review recovery terminalizes dispatch exactly once",
        first_finalizing.state == "complete"
        and second_finalizing.state == "complete"
        and resumed_durable_finalizing.state == "complete"
        and resumed_durable_exiting.state == "complete"
        and resumed_live_executor.state == "awaiting-callback"
        and resumed_live_executor.changed
        and resumed_live_executor.attention_reason is None
        and resumed_findings_executor.state == "awaiting-callback"
        and resumed_findings_executor.changed
        and resumed_findings_executor.attention_reason is None
        and store.read(
            "owner-cli", "op-approved-live-review-cli"
        ).resources
        != OwnedResources()
        and finalizing_record.state == "complete"
        and recovery_calls
        == [
            "op-finalizing-review-cli",
            "op-approved-finalizing-review-cli",
            "op-approved-exiting-review-cli",
            "op-approved-live-review-cli",
            "op-findings-live-review-cli",
        ],
    )


with tempfile.TemporaryDirectory(prefix="review-resolution-recovery.") as raw:
    recovery_vault = Path(raw).resolve() / "vault"
    recovery_store_root = recovery_vault / ".vault-meta/harness"
    recovery_owner = "review-resolution-owner"
    recovery_operation = "review-resolution-operation"
    recovery_surface = "509DF5B5-B499-4FB9-A206-BC99134C9093"
    recovery_worktree = Path(raw).resolve() / "product"
    recovery_worktree.mkdir(parents=True)
    recovery_runtime_root = (
        recovery_store_root
        / "owners"
        / recovery_owner
        / "runtime"
        / recovery_operation
    )
    recovery_runtime_root.mkdir(parents=True)
    recovery_summary = b'{"schema_version":1}\n'
    (recovery_worktree / ".task-summary.json").write_bytes(recovery_summary)
    (recovery_runtime_root / "launch.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner_id": recovery_owner,
                "operation_id": recovery_operation,
                "cwd": str(recovery_worktree),
                "surface_id": recovery_surface,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (recovery_runtime_root / "session.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": recovery_operation,
                "cwd": str(recovery_worktree),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    recovery_gate_root = (
        recovery_store_root
        / "review-data"
        / recovery_operation
        / recovery_operation
    )
    recovery_gate_root.mkdir(parents=True)
    recovery_gate_path = recovery_gate_root / "review-gate.json"
    recovery_gate = {
        "schema_version": 1,
        "status": "changes-requested",
        "execution_protocol": "exact-head-attempt-v1",
        "attempt": {
            "status": "terminal",
            "terminal": {"result": "changes-requested"},
        },
    }
    recovery_gate_path.write_text(
        json.dumps(recovery_gate) + "\n", encoding="utf-8"
    )
    recovery_transport_calls: list[dict[str, object]] = []
    original_transport = harness_cli.publish_review_resolution_transport

    def capture_recovered_transport(**kwargs: object) -> None:
        recovery_transport_calls.append(kwargs)

    harness_cli.publish_review_resolution_transport = capture_recovered_transport
    try:
        harness_cli._publish_recovered_review_resolution(
            store_root=recovery_store_root,
            owner=recovery_owner,
            operation_id=recovery_operation,
            worktree=recovery_worktree,
            gate_path=recovery_gate_path,
            gate_state=recovery_gate,
            cmux_adapter=FakeCmux("alive"),
        )
    finally:
        harness_cli.publish_review_resolution_transport = original_transport
    check(
        "changes-requested recovery republishes the exact review generation",
        len(recovery_transport_calls) == 1
        and recovery_transport_calls[0]["gate_state"] == recovery_gate
        and recovery_transport_calls[0]["gate_root"] == recovery_gate_root
        and recovery_transport_calls[0]["worktree"] == recovery_worktree
        and recovery_transport_calls[0]["operation_id"] == recovery_operation
        and recovery_transport_calls[0]["surface_id"] == recovery_surface
        and recovery_transport_calls[0]["summary_sha256"]
        == hashlib.sha256(recovery_summary).hexdigest()
        and recovery_transport_calls[0]["runtime_spec_path"]
        == recovery_runtime_root / "launch.json",
    )
    recovery_entry_calls: list[dict[str, object]] = []
    original_recovery_transport = (
        harness_cli._publish_recovered_review_resolution
    )

    def capture_recovery_entry(**kwargs: object) -> None:
        recovery_entry_calls.append(kwargs)

    harness_cli._publish_recovered_review_resolution = capture_recovery_entry
    try:
        recovery_status = harness_cli._recover_finalizing_review_if_present(
            OperationStore(recovery_store_root),
            recovery_owner,
            recovery_operation,
            cmux_adapter=FakeCmux("alive"),
        )
    finally:
        harness_cli._publish_recovered_review_resolution = (
            original_recovery_transport
        )
    check(
        "terminal findings recovery bypasses stale resolution parsing",
        recovery_status == "changes-requested"
        and len(recovery_entry_calls) == 1,
    )
    original_handoff_ready = harness_cli._review_resolution_handoff_ready
    harness_cli._review_resolution_handoff_ready = lambda **_kwargs: True
    try:
        transport_required = harness_cli._review_findings_transport_required(
            worktree=recovery_worktree,
            operation_id=recovery_operation,
            gate_state=recovery_gate,
        )
    finally:
        harness_cli._review_resolution_handoff_ready = original_handoff_ready
    check(
        "completed resolution handoff advances instead of redelivering findings",
        not transport_required,
    )
