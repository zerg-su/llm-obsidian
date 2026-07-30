"""Content-free aggregation for the cmux harness workspace progress bar."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .adapters.cmux import run_cmux
from .state_machine import TERMINAL
from .store import OperationStore, StoreError


LEGACY_STATUS_KEY = "llm-obsidian-harness"
Runner = Callable[..., subprocess.CompletedProcess[str]]


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


def collect(
    state_root: Path | str,
    *,
    terminal_owner: str | None = None,
) -> HarnessStatus:
    """Read active owners, or one explicitly selected terminal owner."""

    store = OperationStore(state_root)
    owners = store.root / "owners"
    if not owners.is_dir():
        return HarnessStatus()

    records_by_owner: dict[str, list[object]] = {}
    invalid = 0
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
                invalid += 1
                continue
            records_by_owner.setdefault(owner_dir.name, []).append(record)

    active_owners = {
        owner
        for owner, records in records_by_owner.items()
        if any(record.state not in TERMINAL for record in records)
    }
    selected_owners = active_owners
    terminal_records = records_by_owner.get(terminal_owner or "", [])
    if (
        not selected_owners
        and terminal_records
        and all(record.state == "complete" for record in terminal_records)
    ):
        selected_owners = {terminal_owner}
    current = [
        record
        for owner, records in records_by_owner.items()
        if owner in selected_owners
        for record in records
    ]
    active_records = [record for record in current if record.state not in TERMINAL]
    return HarnessStatus(
        completed=sum(record.state == "complete" for record in current),
        total=len(current),
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
    status = collect(state_root, terminal_owner=terminal_owner)
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
        commands.append([
            "set-progress",
            f"{progress:.6f}",
            "--label",
            render(status),
            "--workspace",
            workspace,
        ])
    try:
        results = [
            run_cmux(
                command,
                runner=runner,
                binary=binary
                or os.environ.get("CMUX_BUNDLED_CLI_PATH")
                or "cmux",
            )
            for command in commands
        ]
    except (OSError, ValueError):
        return False
    return all(result.returncode == 0 for result in results)
