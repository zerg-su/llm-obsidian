#!/usr/bin/env python3
"""Registered root-dashboard launch and reuse behavior."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.dashboard_facade import (  # noqa: E402
    DashboardBinding,
    FACADE_KINDS,
    DashboardLaunchReceipt,
    facade_dashboard_command,
    launch_bound_facade_dashboard,
    launch_facade_dashboard,
    launch_review_facade_dashboard,
)
import harness.dashboard_facade as facade_module  # noqa: E402


def load_dashboard_script() -> object:
    path = ROOT / "scripts" / "harness-dashboard.py"
    spec = importlib.util.spec_from_file_location("harness_dashboard_facade", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dashboard = load_dashboard_script()
failures: list[str] = []


def check(label: str, condition: bool) -> None:
    if condition:
        print(f"OK   {label}")
    else:
        print(f"FAIL {label}")
        failures.append(label)


class FakeAdapter:
    def __init__(
        self, caller: str, observer: str | list[str], workspace: str
    ) -> None:
        self.caller = caller
        self.observers = [observer] if isinstance(observer, str) else observer
        self.workspace = workspace
        self.open_count = 0
        self.sent: list[tuple[str, str]] = []
        self.keys: list[tuple[str, str]] = []
        self.closed: list[str] = []

    def surface_workspaces(self) -> object:
        return type(
            "Inventory",
            (),
            {
                "ambiguous_surfaces": frozenset(),
                "surface_workspaces": {
                    self.caller.casefold(): self.workspace,
                    **{
                        observer.casefold(): self.workspace
                        for observer in self.observers
                    },
                },
            },
        )()

    def open_split(self, _caller: str) -> object:
        self.open_count += 1
        observer = self.observers[self.open_count - 1]
        return type(
            "Surface",
            (),
            {"surface_id": observer, "workspace_id": self.workspace},
        )()

    def send(self, surface: str, value: str) -> None:
        self.sent.append((surface, value))

    def send_key(self, surface: str, value: str) -> None:
        self.keys.append((surface, value))

    def close_exact(self, surface: str) -> None:
        self.closed.append(surface)


expected_facades = {
    "dispatch",
    "plan-review",
    "review",
    "verify",
    "fix",
    "recovery",
    "pivot",
    "reap",
}
check("the dashboard facade registry is explicit and complete", FACADE_KINDS == expected_facades)

with tempfile.TemporaryDirectory(prefix="dashboard-facade.") as raw:
    vault = Path(raw) / "vault"
    store = vault / ".vault-meta" / "harness"
    store.mkdir(parents=True)
    caller = "11111111-1111-4111-8111-111111111111"
    request = "request-before-root"
    root = "durable-root"
    rooted = facade_dashboard_command(
        vault=vault,
        store=store,
        caller_surface=caller,
        facade="verify",
        request_id="verify-request",
        root_operation_id=root,
    )
    check(
        "facades compile only exact durable root commands",
        rooted[-4:] == ["--root", root, "--facade", "verify"],
    )
    try:
        facade_dashboard_command(
            vault=vault,
            store=store,
            caller_surface=caller,
            facade="review",
            request_id=request,
        )
    except ValueError:
        temporary_rejected = True
    else:
        temporary_rejected = False
    check(
        "the facade rejects pre-root temporary dashboard launch",
        temporary_rejected
        and not hasattr(facade_module, "rebind_facade_dashboard"),
    )
    (vault / ".task-meta.json").write_text(
        json.dumps(
            {
                "vault_root": str(vault),
                "task_surface": caller,
                "task_id": root,
            }
        ),
        encoding="utf-8",
    )
    bound_commands: list[list[str]] = []
    bound = [
        launch_bound_facade_dashboard(
            worktree=vault,
            facade=facade,
            root_operation_id=root,
            runner=lambda argv: bound_commands.append(list(argv)),
        )
        for facade in sorted(FACADE_KINDS)
    ]
    production_sources = {
        "dispatch": ROOT / "scripts/harness/workflows/dispatch.py",
        "fix": ROOT / "scripts/harness/workflows/dispatch.py",
        "plan-review": ROOT / "scripts/harness/workflows/review.py",
        "review": ROOT / "scripts/harness/workflows/review.py",
        "verify": ROOT / "scripts/harness/runtime_worker_verification.py",
        "recovery": ROOT / "scripts/harness/runtime_worker_loop.py",
        "pivot": ROOT / "scripts/harness/review_finalization.py",
        "reap": ROOT / "scripts/reap-runner.py",
    }
    check(
        "every real facade boundary calls the one bound launcher",
        all(receipt.status == "launched" for receipt in bound)
        and len(bound_commands) == len(FACADE_KINDS)
        and all(
            "launch_bound_facade_dashboard" in path.read_text(encoding="utf-8")
            and f'"{facade}"' in path.read_text(encoding="utf-8")
            for facade, path in production_sources.items()
        ),
    )

    captured: list[list[str]] = []
    receipt = launch_facade_dashboard(
        vault=vault,
        store=store,
        caller_surface=caller,
        facade="review",
        request_id=request,
        root_operation_id=root,
        runner=lambda argv: captured.append(list(argv)),
    )
    check(
        "launch success emits one content-free receipt",
        receipt
        == DashboardLaunchReceipt(
            status="launched",
            facade="review",
            scope="root",
            root_operation_id=root,
        )
        and len(captured) == 1,
    )

    (vault / ".task-meta.json").unlink()
    explicit_commands: list[list[str]] = []
    explicit = launch_review_facade_dashboard(
        binding=DashboardBinding(
            vault=vault,
            store=store,
            caller_surface=caller,
            request_id=root,
        ),
        facade="plan-review",
        root_operation_id=root,
        runner=lambda argv: explicit_commands.append(list(argv)),
    )
    check(
        "current and plan review launch from explicit authority without task metadata",
        explicit.status == "launched"
        and explicit_commands[0][-4:]
        == ["--root", root, "--facade", "plan-review"],
    )

    def fail(_argv: list[str]) -> None:
        raise OSError("secret provider output must not escape")

    contained = launch_facade_dashboard(
        vault=vault,
        store=store,
        caller_surface=caller,
        facade="review",
        request_id=request,
        root_operation_id=root,
        runner=fail,
    )
    check(
        "dashboard launch failure is contained and content-free",
        contained.status == "degraded"
        and contained.facade == "review"
        and "secret" not in json.dumps(contained.__dict__),
    )

    dashboard_script = vault / "scripts" / "harness-dashboard.py"
    dashboard_script.parent.mkdir()
    child_pids = vault / "dashboard-child-pids"
    dashboard_script.write_text(
        "import os\n"
        "import time\n"
        f"with open({str(child_pids)!r}, 'a') as handle:\n"
        "    handle.write(f'{os.getpid()}\\n')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    original_timeout = facade_module.DASHBOARD_TIMEOUT_SECONDS
    facade_module.DASHBOARD_TIMEOUT_SECONDS = 0.2
    timeout_started = time.monotonic()
    try:
        timed_out_launch = launch_facade_dashboard(
            vault=vault,
            store=store,
            caller_surface=caller,
            facade="verify",
            request_id=root,
            root_operation_id=root,
        )
    finally:
        facade_module.DASHBOARD_TIMEOUT_SECONDS = original_timeout
    elapsed = time.monotonic() - timeout_started
    timed_out_pids = [int(value) for value in child_pids.read_text().splitlines()]

    def child_is_reaped(pid: int) -> bool:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True
        return False

    check(
        "blocking root dashboard launch degrades and reaps at the timeout",
        timed_out_launch.status == "degraded"
        and elapsed < 2.0
        and len(timed_out_pids) == 1
        and all(child_is_reaped(pid) for pid in timed_out_pids),
    )

    observer = "22222222-2222-4222-8222-222222222222"
    workspace = "33333333-3333-4333-8333-333333333333"
    adapter = FakeAdapter(caller, observer, workspace)
    markers = Path(raw) / "markers"
    opened = dashboard.open_dashboard(
        vault=vault,
        store=store,
        caller_surface=caller,
        root=root,
        facade="review",
        adapter=adapter,
        marker_root=markers,
    )
    replay = dashboard.open_dashboard(
        vault=vault,
        store=store,
        caller_surface=caller,
        root=root,
        facade="verify",
        adapter=adapter,
        marker_root=markers,
    )
    marker_values = [
        json.loads(path.read_text(encoding="ascii"))
        for path in markers.glob("*.json")
    ]
    check(
        "root dashboard reuse keeps one exact split across facades",
        not opened.reused
        and replay.reused
        and replay.surface_id == observer
        and adapter.open_count == 1
        and adapter.closed == []
        and len(adapter.sent) == 1
        and len(marker_values) == 1
        and marker_values[0]["scope"] == "root"
        and marker_values[0]["root_id"] == root,
    )

if failures:
    raise SystemExit(f"{len(failures)} dashboard facade test(s) failed")
print("All dashboard facade tests passed.")
