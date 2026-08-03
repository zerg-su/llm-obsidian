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
) -> tuple[str, str, bool]:
    origins: set[str] = set()
    surfaces: set[str] = set()
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
        if record.resources.surface_id:
            surfaces.add(record.resources.surface_id)
        else:
            invalid = True
    if invalid or len(origins) > 1 or len(surfaces) > 1:
        return "", "", True
    return (
        next(iter(origins)) if origins else "",
        next(iter(surfaces)) if surfaces else "",
        False,
    )


def _record_controller(
    record: OperationRecord,
    controllers: list[OperationRecord],
) -> OperationRecord | None:
    """Bind one record to at most one exact top-level program."""

    if record.spec.kind in CONTROLLER_KINDS:
        return next(
            (
                controller
                for controller in controllers
                if controller.spec.operation_id == record.spec.operation_id
            ),
            None,
        )

    if record.spec.parent_operation_id:
        return next(
            (
                controller
                for controller in controllers
                if controller.spec.operation_id
                == record.spec.parent_operation_id
            ),
            None,
        )

    lane_matches = [
        controller
        for controller in controllers
        if controller.lane_id == record.lane_id
    ]
    if len(lane_matches) == 1:
        return lane_matches[0]
    if lane_matches:
        return None

    if record.spec.contract_sha256:
        contract_matches = [
            controller
            for controller in controllers
            if controller.spec.contract_sha256 == record.spec.contract_sha256
        ]
        if len(contract_matches) == 1:
            return contract_matches[0]
    return None


def _controller_is_current(
    store: OperationStore,
    controller: OperationRecord,
    program_records: list[OperationRecord],
    *,
    workspace_id: str,
    trigger_owner: str,
    inventory: LiveInventory,
) -> tuple[bool, bool]:
    if controller.state in TERMINAL:
        return False, False
    origin_surface, invalid = _runtime_origin(store, controller)
    live_surface = controller.resources.surface_id
    if not origin_surface and not invalid and controller.spec.kind == "research":
        origin_surface, live_surface, invalid = _research_origin(
            store, program_records
        )
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

    if live_surface:
        if not inventory.contains(live_surface):
            return False, False
    elif controller.state in SURFACE_BOUND_STATES:
        return False, False
    return True, False


def collect(
    state_root: Path | str,
    *,
    trigger_owner: str | None = None,
    workspace_id: str = "",
    inventory: LiveInventory | None = None,
) -> HarnessStatus:
    """Select exact current programs without mutating durable lifecycle state."""

    trigger = trigger_owner or ""
    store, records_by_owner, invalid_by_owner = _records(state_root)
    if inventory is None or not workspace_id:
        return HarnessStatus()

    selected: list[OperationRecord] = []
    selected_ids: set[tuple[str, str]] = set()
    selected_owners: set[str] = set()
    invalid = 0
    for owner, records in records_by_owner.items():
        all_controllers = [
            record for record in records if record.spec.kind in CONTROLLER_KINDS
        ]
        bindings = {
            record.spec.operation_id: _record_controller(record, all_controllers)
            for record in records
        }
        controllers: list[tuple[OperationRecord, list[OperationRecord]]] = []
        for record in all_controllers:
            program_records = [
                candidate
                for candidate in records
                if bindings[candidate.spec.operation_id] is not None
                and bindings[candidate.spec.operation_id].spec.operation_id
                == record.spec.operation_id
            ]
            current, broken = _controller_is_current(
                store,
                record,
                program_records,
                workspace_id=workspace_id,
                trigger_owner=trigger,
                inventory=inventory,
            )
            if broken and owner == trigger:
                invalid += 1
            if current:
                controllers.append((record, program_records))
        if not controllers:
            continue
        selected_owners.add(owner)
        for controller, program_records in controllers:
            for record in program_records:
                identity = (owner, record.spec.operation_id)
                if identity not in selected_ids:
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
    if not status.total:
        return f"{status.visible_attention}!" if status.visible_attention else ""
    parts = [f"{status.completed}/{status.total}", "·", f"{status.active}▶"]
    if status.waiting:
        parts.append(f"{status.waiting}⌛")
    if status.visible_attention:
        parts.append(f"{status.visible_attention}!")
    return " ".join(parts)


def publish(
    state_root: Path | str,
    *,
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
