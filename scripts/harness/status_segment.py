"""Content-free aggregation for the cmux harness workspace progress bar."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .adapters.cmux import run_cmux
from .contracts import OperationRecord
from .state_machine import TERMINAL
from .store import OperationStore, StoreError


LEGACY_STATUS_KEY = "llm-obsidian-harness"
Runner = Callable[..., subprocess.CompletedProcess[str]]
CONTROLLER_KINDS = frozenset(
    {
        "dispatch",
        "research",
        "simple-review-holistic",
        "deep-review-spec",
        "deep-review-correctness",
    }
)
SURFACE_BOUND_STATES = frozenset({"running", "awaiting-callback"})


@dataclass(frozen=True)
class HarnessStatus:
    completed: int = 0
    total: int = 0
    active: int = 0
    waiting: int = 0
    attention: int = 0
    invalid: int = 0

    @property
    def visible_attention(self) -> int:
        return self.attention + self.invalid


@dataclass(frozen=True)
class LiveInventory:
    """One bounded exact cmux surface-to-workspace snapshot."""

    surface_workspaces: Mapping[str, str]

    def workspace_for(self, surface_id: str) -> str:
        return self.surface_workspaces.get(surface_id.casefold(), "")

    def contains(self, surface_id: str) -> bool:
        return surface_id.casefold() in self.surface_workspaces


def _live_inventory(
    *,
    runner: Runner | None,
    binary: str,
) -> LiveInventory | None:
    try:
        result = run_cmux(
            ["--id-format", "both", "tree", "--all", "--json"],
            runner=runner,
            binary=binary,
        )
        if result.returncode:
            return None
        value = json.loads(result.stdout)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("windows"), list):
        return None

    placements: dict[str, str] = {}
    for window in value["windows"]:
        if not isinstance(window, dict) or not isinstance(
            window.get("workspaces", []), list
        ):
            return None
        for workspace in window.get("workspaces", []):
            if not isinstance(workspace, dict) or not isinstance(
                workspace.get("panes", []), list
            ):
                return None
            workspace_id = str(
                workspace.get("id")
                or workspace.get("workspace_id")
                or workspace.get("ref")
                or ""
            )
            if not workspace_id:
                return None
            for pane in workspace.get("panes", []):
                if not isinstance(pane, dict) or not isinstance(
                    pane.get("surfaces", []), list
                ):
                    return None
                for surface in pane.get("surfaces", []):
                    if not isinstance(surface, dict):
                        return None
                    surface_id = str(
                        surface.get("id")
                        or surface.get("surface_id")
                        or ""
                    )
                    if not surface_id:
                        return None
                    key = surface_id.casefold()
                    prior = placements.get(key)
                    if prior is not None and prior.casefold() != workspace_id.casefold():
                        return None
                    placements[key] = workspace_id
    return LiveInventory(placements)


def _records(
    state_root: Path | str,
) -> tuple[
    OperationStore,
    dict[str, list[OperationRecord]],
    dict[str, int],
]:
    store = OperationStore(state_root)
    owners = store.root / "owners"
    records_by_owner: dict[str, list[OperationRecord]] = {}
    invalid_by_owner: dict[str, int] = {}
    if not owners.is_dir():
        return store, records_by_owner, invalid_by_owner
    for owner_dir in sorted(owners.iterdir()):
        if not owner_dir.is_dir():
            continue
        operations = owner_dir / "operations"
        if not operations.is_dir():
            continue
        for path in sorted(operations.glob("*.json")):
            try:
                record = store.read(owner_dir.name, path.stem)
            except StoreError:
                invalid_by_owner[owner_dir.name] = (
                    invalid_by_owner.get(owner_dir.name, 0) + 1
                )
                continue
            records_by_owner.setdefault(owner_dir.name, []).append(record)
    return store, records_by_owner, invalid_by_owner


def _runtime_origin(
    store: OperationStore,
    record: OperationRecord,
) -> tuple[str, bool]:
    path = (
        store.root
        / "owners"
        / record.spec.owner_id
        / "runtime"
        / record.spec.operation_id
        / "session.json"
    )
    if not path.is_file() or path.is_symlink():
        return "", False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", True
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("operation_id") != record.spec.operation_id
        or value.get("run_id") != record.run_id
    ):
        return "", True
    origin_surface = value.get("origin_surface")
    if not isinstance(origin_surface, str) or not origin_surface:
        return "", True
    return origin_surface, False


def _research_origin(
    store: OperationStore,
    records: list[OperationRecord],
) -> tuple[str, bool]:
    origins: set[str] = set()
    invalid = False
    for record in records:
        if (
            record.spec.kind not in {"research-fetch", "research-synth"}
            or record.state in TERMINAL
        ):
            continue
        origin, broken = _runtime_origin(store, record)
        invalid = invalid or broken
        if origin:
            origins.add(origin)
    if invalid or len(origins) > 1:
        return "", True
    return (next(iter(origins)) if origins else ""), False


def _controller_is_current(
    store: OperationStore,
    controller: OperationRecord,
    owner_records: list[OperationRecord],
    *,
    workspace_id: str,
    trigger_owner: str,
    inventory: LiveInventory,
) -> tuple[bool, bool]:
    if controller.state in TERMINAL:
        return False, False
    origin_surface, invalid = _runtime_origin(store, controller)
    if not origin_surface and not invalid and controller.spec.kind == "research":
        origin_surface, invalid = _research_origin(store, owner_records)
    if invalid:
        return False, True
    if origin_surface:
        origin_workspace = inventory.workspace_for(origin_surface)
        if (
            not origin_workspace
            or origin_workspace.casefold() != workspace_id.casefold()
        ):
            return False, False
    elif controller.spec.owner_id != trigger_owner:
        return False, False

    surface_id = controller.resources.surface_id
    if (
        controller.state in SURFACE_BOUND_STATES
        and surface_id
        and not inventory.contains(surface_id)
    ):
        return False, False
    return True, False


def _belongs_to_controller(
    record: OperationRecord,
    controller: OperationRecord,
) -> bool:
    if record.spec.kind in CONTROLLER_KINDS:
        return record.spec.operation_id == controller.spec.operation_id
    if record.lane_id == controller.lane_id:
        return True
    if (
        controller.spec.contract_sha256
        and record.spec.contract_sha256 == controller.spec.contract_sha256
    ):
        return True
    return (
        controller.spec.kind == "research"
        and record.spec.kind in {"research-fetch", "research-synth"}
    )


def collect(
    state_root: Path | str,
    *,
    terminal_owner: str | None = None,
    trigger_owner: str | None = None,
    workspace_id: str = "",
    inventory: LiveInventory | None = None,
) -> HarnessStatus:
    """Select exact current programs without mutating durable lifecycle state."""

    trigger = trigger_owner or terminal_owner or ""
    store, records_by_owner, invalid_by_owner = _records(state_root)
    if inventory is None or not workspace_id:
        return HarnessStatus()

    selected: list[OperationRecord] = []
    selected_ids: set[tuple[str, str]] = set()
    selected_owners: set[str] = set()
    invalid = 0
    for owner, records in records_by_owner.items():
        controllers: list[OperationRecord] = []
        for record in records:
            if record.spec.kind not in CONTROLLER_KINDS:
                continue
            current, broken = _controller_is_current(
                store,
                record,
                records,
                workspace_id=workspace_id,
                trigger_owner=trigger,
                inventory=inventory,
            )
            if broken and owner == trigger:
                invalid += 1
            if current:
                controllers.append(record)
        if not controllers:
            continue
        selected_owners.add(owner)
        for controller in controllers:
            for record in records:
                identity = (owner, record.spec.operation_id)
                if (
                    identity not in selected_ids
                    and _belongs_to_controller(record, controller)
                ):
                    selected_ids.add(identity)
                    selected.append(record)

    invalid += sum(invalid_by_owner.get(owner, 0) for owner in selected_owners)
    active_records = [record for record in selected if record.state not in TERMINAL]
    return HarnessStatus(
        completed=sum(record.state == "complete" for record in selected),
        total=len(selected),
        active=len(active_records),
        waiting=sum(record.state == "awaiting-callback" for record in active_records),
        attention=sum(record.state == "attention-required" for record in active_records),
        invalid=invalid,
    )


def render(status: HarnessStatus) -> str:
    parts = [f"{status.completed}/{status.total}", "·", f"{status.active}▶"]
    if status.waiting:
        parts.append(f"{status.waiting}⌛")
    if status.visible_attention:
        parts.append(f"{status.visible_attention}!")
    return " ".join(parts)


def publish(
    state_root: Path | str,
    *,
    terminal_owner: str | None = None,
    trigger_owner: str | None = None,
    workspace_id: str | None = None,
    runner: Runner | None = None,
    binary: str | None = None,
) -> bool:
    """Best-effort update of one exact cmux workspace progress bar."""

    workspace = workspace_id if workspace_id is not None else os.environ.get(
        "CMUX_WORKSPACE_ID", ""
    )
    if not workspace:
        return False
    cmux_binary = (
        binary
        or os.environ.get("CMUX_BUNDLED_CLI_PATH")
        or "cmux"
    )
    inventory = _live_inventory(runner=runner, binary=cmux_binary)
    if inventory is None:
        return False
    status = collect(
        state_root,
        terminal_owner=terminal_owner,
        trigger_owner=trigger_owner,
        workspace_id=workspace,
        inventory=inventory,
    )
    commands = [
        [
            "clear-status",
            LEGACY_STATUS_KEY,
            "--workspace",
            workspace,
        ]
    ]
    if not status.total and not status.invalid:
        commands.append(["clear-progress", "--workspace", workspace])
    else:
        progress = status.completed / status.total if status.total else 0.0
        commands.append(
            [
                "set-progress",
                f"{progress:.6f}",
                "--label",
                render(status),
                "--workspace",
                workspace,
            ]
        )
    try:
        results = [
            run_cmux(
                command,
                runner=runner,
                binary=cmux_binary,
            )
            for command in commands
        ]
    except (OSError, ValueError):
        return False
    return all(result.returncode == 0 for result in results)
