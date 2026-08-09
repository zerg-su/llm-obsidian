"""Read-only public Harness CLI commands with no lifecycle authority."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .contracts import to_dict
from .dashboard_projection import DashboardProjection, project
from .dashboard_view import render as render_dashboard
from .diagnostics import observe as observe_diagnostics
from .status_segment import live_inventory
from .store import OperationStore


COMMANDS = frozenset({"status", "inspect", "doctor", "diagnose", "dashboard"})


def dashboard(
    store_root: Path,
    owner: str,
    *,
    inventory_probe: object | None = None,
) -> DashboardProjection:
    """Project one owner, annotated by one bounded best-effort cmux probe."""

    binary = os.environ.get("CMUX_BUNDLED_CLI_PATH") or "cmux"
    try:
        if inventory_probe is not None:
            inventory = inventory_probe(binary=binary)
        elif shutil.which(binary):
            inventory = live_inventory(binary=binary)
        else:
            inventory = None
    except Exception:
        inventory = None
    return project(
        store_root,
        owner,
        inventory=inventory,
        surface_probe="observed" if inventory is not None else "unavailable",
    )


def execute(
    args: Any,
    store: OperationStore,
    *,
    inventory_probe: object | None = None,
) -> object:
    if args.command == "status":
        return [
            {
                "operation_id": row.spec.operation_id,
                "kind": row.spec.kind,
                "state": row.state,
                "revision": row.revision,
                "lane_id": row.lane_id,
                "run_id": row.run_id,
            }
            for row in store.list(args.owner)
        ]
    if args.command == "inspect":
        return to_dict(store.read(args.owner, args.operation_id))
    if args.command == "doctor":
        return {
            "status": "ok" if shutil.which("cmux") else "degraded",
            "cmux": bool(shutil.which("cmux")),
            "claude": bool(shutil.which("claude")),
            "codex": bool(shutil.which("codex")),
        }
    if args.command == "diagnose":
        return observe_diagnostics(args.store, args.owner)
    projection = dashboard(
        args.store,
        args.owner,
        inventory_probe=inventory_probe,
    )
    return (
        to_dict(projection)
        if args.json
        else render_dashboard(projection).rstrip("\n")
    )
