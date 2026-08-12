"""One contained dashboard launcher for every registered Harness facade."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .contracts import ID_RE


FACADE_KINDS = {
    "dispatch",
    "plan-review",
    "review",
    "verify",
    "fix",
    "recovery",
    "pivot",
    "reap",
}


@dataclass(frozen=True)
class DashboardLaunchReceipt:
    """Content-free outcome; provider or command text is never retained."""

    status: str
    facade: str
    scope: str
    root_operation_id: str = ""

    def __post_init__(self) -> None:
        if (
            self.status not in {"launched", "degraded"}
            or self.facade not in FACADE_KINDS
            or self.scope not in {"temporary", "root"}
            or (
                self.scope == "root"
                and not ID_RE.fullmatch(self.root_operation_id)
            )
            or (self.scope == "temporary" and self.root_operation_id)
        ):
            raise ValueError("dashboard launch receipt is invalid")


def facade_dashboard_command(
    *,
    vault: Path,
    store: Path,
    caller_surface: str,
    facade: str,
    request_id: str,
    root_operation_id: str = "",
) -> list[str]:
    """Compile one exact allowlisted observer command with no lifecycle effect."""

    if facade not in FACADE_KINDS:
        raise ValueError("dashboard facade is not registered")
    if not ID_RE.fullmatch(request_id):
        raise ValueError("dashboard request identity is invalid")
    if root_operation_id and not ID_RE.fullmatch(root_operation_id):
        raise ValueError("dashboard root identity is invalid")
    root = Path(vault).expanduser().resolve()
    state = Path(store).expanduser().resolve()
    if not root.is_dir() or not state.is_relative_to(root):
        raise ValueError("dashboard facade paths are invalid")
    command = [
        sys.executable,
        str(root / "scripts" / "harness-dashboard.py"),
        "open",
        "--vault",
        str(root),
        "--store",
        str(state),
    ]
    if caller_surface:
        command.extend(["--surface", caller_surface])
    command.extend(
        ["--root", root_operation_id]
        if root_operation_id
        else ["--temporary", request_id]
    )
    command.extend(["--facade", facade])
    return command


def _run(argv: Sequence[str]) -> None:
    subprocess.run(
        list(argv),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def launch_facade_dashboard(
    *,
    vault: Path,
    store: Path,
    caller_surface: str,
    facade: str,
    request_id: str,
    root_operation_id: str = "",
    runner: Callable[[Sequence[str]], None] = _run,
) -> DashboardLaunchReceipt:
    """Launch best-effort; dashboard failure never blocks its product facade."""

    scope = "root" if root_operation_id else "temporary"
    command = facade_dashboard_command(
        vault=vault,
        store=store,
        caller_surface=caller_surface,
        facade=facade,
        request_id=request_id,
        root_operation_id=root_operation_id,
    )
    try:
        runner(command)
    except Exception:
        return DashboardLaunchReceipt(
            "degraded", facade, scope, root_operation_id
        )
    return DashboardLaunchReceipt("launched", facade, scope, root_operation_id)


def launch_bound_facade_dashboard(
    *,
    worktree: Path,
    facade: str,
    root_operation_id: str,
    runner: Callable[[Sequence[str]], None] = _run,
) -> DashboardLaunchReceipt:
    """Launch from one task binding at a real facade boundary, best-effort."""

    if facade not in FACADE_KINDS:
        raise ValueError("bound dashboard facade identity is invalid")
    if not ID_RE.fullmatch(root_operation_id):
        return DashboardLaunchReceipt(
            "degraded", facade, "root", root_operation_id="unbound"
        )
    try:
        root = Path(worktree).expanduser().resolve()
        meta = json.loads((root / ".task-meta.json").read_text(encoding="utf-8"))
        vault = Path(str(meta["vault_root"])).expanduser().resolve()
        surface = str(meta["task_surface"])
        if str(meta["task_id"]) != root_operation_id:
            raise ValueError("task dashboard root identity changed")
        return launch_facade_dashboard(
            vault=vault,
            store=vault / ".vault-meta" / "harness",
            caller_surface=surface,
            facade=facade,
            request_id=root_operation_id,
            root_operation_id=root_operation_id,
            runner=runner,
        )
    except Exception:
        return DashboardLaunchReceipt(
            "degraded", facade, "root", root_operation_id
        )


__all__ = [
    "DashboardLaunchReceipt",
    "FACADE_KINDS",
    "facade_dashboard_command",
    "launch_bound_facade_dashboard",
    "launch_facade_dashboard",
]
