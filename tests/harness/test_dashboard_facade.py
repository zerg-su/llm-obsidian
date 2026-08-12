#!/usr/bin/env python3
"""Registered any-facade dashboard launch and rebind behavior."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
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
    rebind_facade_dashboard,
)


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
    temporary = facade_dashboard_command(
        vault=vault,
        store=store,
        caller_surface=caller,
        facade="review",
        request_id=request,
    )
    rooted = facade_dashboard_command(
        vault=vault,
        store=store,
        caller_surface=caller,
        facade="verify",
        request_id="verify-request",
        root_operation_id=root,
    )
    check(
        "facades compile exact temporary or durable root commands",
        temporary[-4:] == ["--temporary", request, "--facade", "review"]
        and rooted[-4:] == ["--root", root, "--facade", "verify"],
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
        runner=lambda argv: captured.append(list(argv)),
    )
    check(
        "launch success emits one content-free receipt",
        receipt
        == DashboardLaunchReceipt(
            status="launched",
            facade="review",
            scope="temporary",
            root_operation_id="",
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

    rebind_commands: list[list[str]] = []
    rebound_receipt = rebind_facade_dashboard(
        vault=vault,
        store=store,
        caller_surface=caller,
        facade="dispatch",
        temporary_request_id=request,
        root_operation_id=root,
        runner=lambda argv: rebind_commands.append(list(argv)),
    )
    check(
        "root creation compiles one temporary-to-durable rebind effect",
        rebound_receipt.status == "launched"
        and rebind_commands[0][-6:]
        == [
            "--temporary",
            request,
            "--root",
            root,
            "--facade",
            "dispatch",
        ],
    )

    def fail(_argv: list[str]) -> None:
        raise OSError("secret provider output must not escape")

    contained = launch_facade_dashboard(
        vault=vault,
        store=store,
        caller_surface=caller,
        facade="review",
        request_id=request,
        runner=fail,
    )
    check(
        "dashboard launch failure is contained and content-free",
        contained.status == "degraded"
        and contained.facade == "review"
        and "secret" not in json.dumps(contained.__dict__),
    )

    observer = "22222222-2222-4222-8222-222222222222"
    workspace = "33333333-3333-4333-8333-333333333333"
    adapter = FakeAdapter(caller, observer, workspace)
    markers = Path(raw) / "markers"
    opened = dashboard.open_dashboard(
        vault=vault,
        store=store,
        caller_surface=caller,
        temporary=request,
        facade="review",
        adapter=adapter,
        marker_root=markers,
    )
    before_rebind_send_count = len(adapter.sent)
    rebound = dashboard.rebind_dashboard(
        vault=vault,
        store=store,
        caller_surface=caller,
        temporary=request,
        root=root,
        facade="review",
        adapter=adapter,
        marker_root=markers,
    )
    replay = dashboard.rebind_dashboard(
        vault=vault,
        store=store,
        caller_surface=caller,
        temporary=request,
        root=root,
        facade="review",
        adapter=adapter,
        marker_root=markers,
    )
    marker_values = [
        json.loads(path.read_text(encoding="ascii"))
        for path in markers.glob("*.json")
    ]
    check(
        "temporary-to-root rebind reuses one split without duplication",
        not opened.reused
        and rebound.surface_id == observer
        and replay.reused
        and adapter.open_count == 1
        and adapter.closed == []
        and len(adapter.sent) == before_rebind_send_count + 1
        and adapter.keys[-2:] == [(observer, "C-c"), (observer, "Enter")]
        and len(marker_values) == 1
        and marker_values[0]["scope"] == "root"
        and marker_values[0]["root_id"] == root,
    )

    root_first_markers = Path(raw) / "root-first-markers"
    temporary_observer = "44444444-4444-4444-8444-444444444444"
    root_first = FakeAdapter(
        caller, [observer, temporary_observer], workspace
    )
    dashboard.open_dashboard(
        vault=vault,
        store=store,
        caller_surface=caller,
        root="root-first",
        facade="review",
        adapter=root_first,
        marker_root=root_first_markers,
    )
    dashboard.open_dashboard(
        vault=vault,
        store=store,
        caller_surface=caller,
        temporary="late-temporary",
        facade="verify",
        adapter=root_first,
        marker_root=root_first_markers,
    )
    converged = dashboard.rebind_dashboard(
        vault=vault,
        store=store,
        caller_surface=caller,
        temporary="late-temporary",
        root="root-first",
        facade="pivot",
        adapter=root_first,
        marker_root=root_first_markers,
    )
    root_first_values = [
        json.loads(path.read_text(encoding="ascii"))
        for path in root_first_markers.glob("*.json")
    ]
    check(
        "root-first cross-facade rebind closes the duplicate observer",
        converged.reused
        and converged.surface_id == observer
        and root_first.closed == [temporary_observer]
        and len(root_first_values) == 1
        and root_first_values[0]["scope"] == "root"
        and "facade" not in root_first_values[0],
    )

if failures:
    raise SystemExit(f"{len(failures)} dashboard facade test(s) failed")
print("All dashboard facade tests passed.")
