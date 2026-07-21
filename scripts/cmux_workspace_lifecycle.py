#!/usr/bin/env python3
"""Exact ownership and cleanup for task-dedicated cmux workspaces."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from task_sessions import (
    TaskSessionError,
    close_surface_exact,
    cmux_tree,
    require_token,
    surface_context,
    workspace_layout,
)


def bind_workspace_identity(
    child: dict[str, str], runner: Any = subprocess.run,
) -> dict[str, str]:
    """Replace short creation output with the exact UUIDs from the child surface."""

    surface = require_token(str(child.get("surface") or ""), "task surface")
    context = surface_context(surface, runner)
    if context is None:  # pragma: no cover - non-optional lookup
        raise TaskSessionError("task workspace surface disappeared during binding")
    advertised_workspace = str(child.get("workspace") or child.get("workspace_ref") or "")
    advertised_window = str(child.get("window") or child.get("window_ref") or "")
    if advertised_workspace and advertised_workspace not in {
        str(context.get("workspace") or ""), str(context.get("workspace_ref") or ""),
    }:
        raise TaskSessionError("task workspace identity changed during binding")
    if advertised_window and advertised_window not in {
        str(context.get("window") or ""), str(context.get("window_ref") or ""),
    }:
        raise TaskSessionError("task workspace window changed during binding")
    workspace = str(context.get("workspace") or "")
    window = str(context.get("window") or "")
    if not workspace or not window:
        raise TaskSessionError("task workspace UUID binding is incomplete")
    return {
        **child,
        "workspace": workspace,
        "workspace_ref": str(context.get("workspace_ref") or ""),
        "window": window,
        "window_ref": str(context.get("window_ref") or ""),
    }


def close_workspace_exact(
    workspace: str, window: str, runner: Any = subprocess.run,
) -> str:
    """Close one UUID-bound workspace and prove it disappeared from its window."""

    workspace = require_token(workspace, "workspace")
    window = require_token(window, "window")
    for _attempt in range(2):
        layout = workspace_layout(cmux_tree(runner), window, workspace, missing_ok=True)
        if layout is None:
            return "already-gone"
        result = runner(
            ["cmux", "workspace", "close", workspace],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0 and workspace_layout(
            cmux_tree(runner), window, workspace, missing_ok=True
        ) is None:
            return "closed"
        time.sleep(0.1)
    raise TaskSessionError("cmux workspace close returned but the exact workspace remained open")


def close_task_container(
    worktree: Path, surface: str, runner: Any = subprocess.run,
) -> str:
    """Close a task's dedicated workspace, or its ordinary split surface."""

    from task_sessions import read_object

    meta = read_object(worktree / ".task-meta.json")
    policy = meta.get("surface_policy")
    placement = str(policy.get("placement") or "split") if isinstance(policy, dict) else "split"
    if placement != "workspace":
        return close_surface_exact(surface, runner)
    workspace = str(meta.get("task_workspace") or "")
    window = str(meta.get("task_window") or "")
    if not workspace or not window:
        raise TaskSessionError("workspace task lacks exact workspace/window UUID metadata")
    return close_workspace_exact(workspace, window, runner)
