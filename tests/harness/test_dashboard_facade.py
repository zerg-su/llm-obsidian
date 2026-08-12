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
    FACADE_KINDS,
    DashboardLaunchReceipt,
    facade_dashboard_command,
    launch_facade_dashboard,
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
    def __init__(self, caller: str, observer: str, workspace: str) -> None:
        self.caller = caller
        self.observer = observer
        self.workspace = workspace
        self.open_count = 0
        self.sent: list[tuple[str, str]] = []
        self.keys: list[tuple[str, str]] = []

    def surface_workspaces(self) -> object:
        return type(
            "Inventory",
            (),
            {
                "ambiguous_surfaces": frozenset(),
                "surface_workspaces": {
                    self.caller.casefold(): self.workspace,
                    self.observer.casefold(): self.workspace,
                },
            },
        )()

    def open_split(self, _caller: str) -> object:
        self.open_count += 1
        return type(
            "Surface",
            (),
            {"surface_id": self.observer, "workspace_id": self.workspace},
        )()

    def send(self, surface: str, value: str) -> None:
        self.sent.append((surface, value))

    def send_key(self, surface: str, value: str) -> None:
        self.keys.append((surface, value))

    def close_exact(self, _surface: str) -> None:
        raise AssertionError("rebind must not close its exact observer split")


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
        and len(adapter.sent) == before_rebind_send_count + 1
        and adapter.keys[-2:] == [(observer, "C-c"), (observer, "Enter")]
        and len(marker_values) == 1
        and marker_values[0]["scope"] == "root"
        and marker_values[0]["root_id"] == root,
    )

if failures:
    raise SystemExit(f"{len(failures)} dashboard facade test(s) failed")
print("All dashboard facade tests passed.")
