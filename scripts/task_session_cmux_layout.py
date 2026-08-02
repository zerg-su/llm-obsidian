"""Pure cmux topology parsing and exact surface/workspace lookup."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from harness.adapters.cmux import run_cmux
from task_session_contracts import (
    RUNTIMES,
    SAFE_TOKEN_RE,
    SCHEMA_VERSION,
    TaskSessionError,
    require_token,
)


def parse_surface(output: str) -> tuple[str, str]:
    uuid_match = re.search(
        r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b",
        output,
    )
    ref_match = re.search(r"\bsurface:\d+\b", output)
    if uuid_match is None:
        raise TaskSessionError("cmux did not return a surface UUID")
    return uuid_match.group(0), ref_match.group(0) if ref_match else ""


def parse_workspace(output: str) -> tuple[str, str]:
    uuid_match = re.search(
        r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b",
        output,
    )
    ref_match = re.search(r"\bworkspace:\d+\b", output)
    if uuid_match is None and ref_match is None:
        raise TaskSessionError("cmux did not return a workspace identity")
    return (
        uuid_match.group(0) if uuid_match else "",
        ref_match.group(0) if ref_match else "",
    )


def cmux_tree(runner: Any = subprocess.run) -> dict[str, Any]:
    commands = (
        ["--id-format", "both", "rpc", "system.tree", '{"all":true}'],
        ["rpc", "system.tree", '{"all":true}'],
    )
    for index, command in enumerate(commands):
        result = run_cmux(command, runner=runner)
        if result.returncode != 0:
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            if index == 0:
                continue
            raise TaskSessionError("cmux surface workspace lookup returned invalid JSON")
        if isinstance(payload, dict):
            return payload
        if index != 0:
            raise TaskSessionError("cmux surface workspace lookup returned invalid data")
    raise TaskSessionError("cmux surface workspace lookup failed")


def pane_layout(workspace: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    layout: dict[str, list[dict[str, str]]] = {}
    for pane in workspace.get("panes", []) if isinstance(workspace, dict) else []:
        if not isinstance(pane, dict):
            continue
        pane_key = str(pane.get("ref") or pane.get("id") or "")
        if not pane_key:
            continue
        surfaces: list[dict[str, str]] = []
        for candidate in pane.get("surfaces", []):
            if isinstance(candidate, dict):
                surfaces.append({
                    "surface": str(candidate.get("id") or ""),
                    "surface_ref": str(candidate.get("ref") or ""),
                })
        layout[pane_key] = surfaces
    return layout


def workspace_layout(
    payload: dict[str, Any], window_id: str, workspace_id: str, *, missing_ok: bool = False
) -> dict[str, list[dict[str, str]]] | None:
    matches: list[dict[str, list[dict[str, str]]]] = []
    for window in payload.get("windows", []):
        if not isinstance(window, dict) or window_id not in {
            str(window.get("id") or ""), str(window.get("ref") or "")
        }:
            continue
        for workspace in window.get("workspaces", []):
            if isinstance(workspace, dict) and workspace_id in {
                str(workspace.get("id") or ""), str(workspace.get("ref") or "")
            }:
                matches.append(pane_layout(workspace))
    if not matches and missing_ok:
        return None
    if len(matches) != 1:
        raise TaskSessionError("cmux workspace does not resolve to one exact layout")
    return matches[0]


def surface_context(
    surface: str, runner: Any = subprocess.run, *, missing_ok: bool = False
) -> dict[str, Any] | None:
    """Resolve an exact surface to its window/workspace without consulting focus."""

    payload = cmux_tree(runner)
    matches: list[dict[str, Any]] = []
    for window in payload.get("windows", []) if isinstance(payload, dict) else []:
        if not isinstance(window, dict):
            continue
        for workspace in window.get("workspaces", []):
            if not isinstance(workspace, dict):
                continue
            for pane in workspace.get("panes", []):
                if not isinstance(pane, dict):
                    continue
                for candidate in pane.get("surfaces", []):
                    if not isinstance(candidate, dict):
                        continue
                    if surface not in {
                        str(candidate.get("id") or ""), str(candidate.get("ref") or "")
                    }:
                        continue
                    context = {
                        "surface": str(candidate.get("id") or ""),
                        "surface_ref": str(candidate.get("ref") or ""),
                        "pane": str(pane.get("id") or ""),
                        "pane_ref": str(pane.get("ref") or ""),
                        "workspace": str(workspace.get("id") or ""),
                        "workspace_ref": str(workspace.get("ref") or ""),
                        "window": str(window.get("id") or ""),
                        "window_ref": str(window.get("ref") or ""),
                        "workspace_layout": pane_layout(workspace),
                    }
                    if context not in matches:
                        matches.append(context)
    if not matches and missing_ok:
        return None
    if len(matches) != 1:
        raise TaskSessionError("cmux surface does not resolve to one exact workspace")
    return matches[0]


def surface_workspace(surface: str, runner: Any = subprocess.run) -> str:
    """Resolve an exact surface to its workspace without consulting focus."""

    context = surface_context(surface, runner)
    assert context is not None
    return context["workspace"] or context["workspace_ref"]


def validate_checkpoint(value: dict[str, Any], runtime: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TaskSessionError("resume checkpoint must be an object")
    kind = str(value.get("kind") or "").strip().lower()
    checkpoint_id = str(value.get("checkpoint_id") or value.get("checkpoint") or "").strip()
    cwd = str(value.get("cwd") or "").strip()
    if kind != runtime or not SAFE_TOKEN_RE.fullmatch(checkpoint_id):
        raise TaskSessionError("resume checkpoint does not match lane runtime")
    path = Path(cwd).expanduser()
    if not path.is_absolute():
        raise TaskSessionError("resume checkpoint cwd must be absolute")
    return {"kind": kind, "checkpoint_id": checkpoint_id, "cwd": str(path.resolve())}


def cmux_capabilities(runner: Any = subprocess.run) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    commands = {
        "anchored_split": ["new-split", "--help"],
        "typed_resume": ["surface", "resume", "--help"],
    }
    outputs: dict[str, str] = {}
    for name, command in commands.items():
        try:
            result = run_cmux(command, runner=runner)
        except OSError:
            checks[name] = False
            continue
        output = (result.stdout + result.stderr)[:20_000]
        outputs[name] = output
        checks[name] = result.returncode == 0
    checks["anchored_split"] = bool(
        checks.get("anchored_split") and "--surface" in outputs.get("anchored_split", "")
    )
    resume_text = outputs.get("typed_resume", "")
    checks["typed_resume"] = bool(
        checks.get("typed_resume")
        and all(token in resume_text for token in ("resume get", "resume set", "resume show", "resume clear"))
    )
    return {"schema_version": SCHEMA_VERSION, **checks}


def close_replacement_shell(
    context: dict[str, Any], runner: Any = subprocess.run
) -> None:
    """Collapse only the shell replacement created for a last-surface split."""

    before = context.get("workspace_layout")
    if not isinstance(before, dict):
        raise TaskSessionError("cmux surface close layout is incomplete")
    target_pane = str(context.get("pane_ref") or context.get("pane") or "")
    target_surfaces = before.get(target_pane)
    if (
        len(before) <= 1
        or not target_pane
        or not isinstance(target_surfaces, list)
        or len(target_surfaces) != 1
    ):
        return
    window = str(context.get("window_ref") or context.get("window") or "")
    workspace = str(context.get("workspace_ref") or context.get("workspace") or "")
    after = workspace_layout(cmux_tree(runner), window, workspace, missing_ok=True)
    if after is None:
        return
    replacement: dict[str, str] | None = None
    if target_pane in after:
        current = after[target_pane]
        stable = all(
            after.get(pane) == surfaces
            for pane, surfaces in before.items()
            if pane != target_pane
        )
        if set(after) == set(before) and stable and len(current) == 1 and current != target_surfaces:
            replacement = current[0]
    else:
        added_panes = [pane for pane in after if pane not in before]
        removed_panes = [pane for pane in before if pane not in after]
        stable = all(
            after.get(pane) == surfaces
            for pane, surfaces in before.items()
            if pane != target_pane
        )
        if (
            removed_panes == [target_pane]
            and len(added_panes) == 1
            and stable
            and len(after[added_panes[0]]) == 1
        ):
            replacement = after[added_panes[0]][0]
        elif not added_panes:
            return
    if replacement is None:
        raise TaskSessionError("cmux last-surface replacement is ambiguous")
    replacement_target = replacement.get("surface_ref") or replacement.get("surface") or ""
    if not replacement_target:
        raise TaskSessionError("cmux replacement surface identity is incomplete")
    for args in (
        ["send", "--surface", replacement_target, "--workspace", workspace, "--window", window, "exit"],
        ["send-key", "--surface", replacement_target, "--workspace", workspace, "--window", window, "Enter"],
    ):
        result = run_cmux(args, runner=runner)
        if result.returncode != 0:
            raise TaskSessionError("cmux replacement shell could not be exited")
    for _attempt in range(8):
        if surface_context(replacement_target, runner, missing_ok=True) is None:
            return
        time.sleep(0.25)
    raise TaskSessionError("cmux replacement shell remained open")


def close_surface_exact(surface: str, runner: Any = subprocess.run) -> str:
    """Close one exact surface with its anchors and prove it left the cmux tree."""

    surface = require_token(surface, "surface")
    for _attempt in range(2):
        context = surface_context(surface, runner, missing_ok=True)
        if context is None:
            return "already-gone"
        target = context["surface_ref"] or context["surface"]
        workspace = context["workspace_ref"] or context["workspace"]
        window = context["window_ref"] or context["window"]
        if not target or not workspace or not window:
            raise TaskSessionError("cmux surface close context is incomplete")
        run_cmux(
            ["close-surface", "--surface", target, "--workspace", workspace, "--window", window],
            runner=runner,
        )
        if surface_context(surface, runner, missing_ok=True) is None:
            close_replacement_shell(context, runner)
            return "closed"
    raise TaskSessionError("cmux close-surface returned but the exact surface remained open")


def spawn_right(origin_surface: str, runner: Any = subprocess.run) -> dict[str, str]:
    origin_surface = require_token(origin_surface, "origin_surface")
    caps = cmux_capabilities(runner)
    if not caps["anchored_split"]:
        raise TaskSessionError("cmux lacks anchored new-split --surface support")
    result = run_cmux(
        ["--id-format", "both", "new-split", "right", "--surface", origin_surface, "--focus", "false"],
        runner=runner,
    )
    if result.returncode != 0:
        workspace = surface_workspace(origin_surface, runner)
        result = run_cmux(
            ["--id-format", "both", "new-split", "right", "--workspace", workspace,
             "--surface", origin_surface, "--focus", "false"],
            runner=runner,
        )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise TaskSessionError("anchored cmux split failed")
    surface, surface_ref = parse_surface(output)
    return {"surface": surface, "surface_ref": surface_ref, "origin_surface": origin_surface}


def spawn_workspace(
    origin_surface: str, cwd: Path, title: str, runner: Any = subprocess.run,
) -> dict[str, str]:
    """Create one unfocused task workspace in the exact origin window."""

    origin_surface = require_token(origin_surface, "origin_surface")
    cwd = cwd.resolve()
    if not cwd.is_dir():
        raise TaskSessionError("workspace cwd is unavailable")
    if not title.strip() or "\0" in title or len(title) > 120:
        raise TaskSessionError("workspace title is invalid")
    context = surface_context(origin_surface, runner)
    assert context is not None
    window = str(context.get("window_ref") or context.get("window") or "")
    if not window:
        raise TaskSessionError("origin surface has no exact cmux window")
    result = run_cmux(
        ["--id-format", "both", "new-workspace", "--name", title.strip(), "--cwd", str(cwd),
         "--window", window, "--focus", "false"],
        runner=runner,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise TaskSessionError("anchored cmux workspace creation failed")
    workspace, workspace_ref = parse_workspace(output)
    workspace_key = workspace_ref or workspace
    layout = workspace_layout(cmux_tree(runner), window, workspace_key)
    assert layout is not None
    surfaces = [surface for pane in layout.values() for surface in pane]
    if len(surfaces) != 1 or not surfaces[0].get("surface"):
        raise TaskSessionError("new cmux workspace did not contain one exact surface")
    return {
        **surfaces[0],
        "origin_surface": origin_surface,
        "workspace": workspace,
        "workspace_ref": workspace_ref,
        "window": str(context.get("window") or ""),
        "window_ref": str(context.get("window_ref") or ""),
        "placement": "workspace",
    }


def capture_resume(surface: str, runtime: str, runner: Any = subprocess.run) -> dict[str, str]:
    if runtime not in RUNTIMES:
        raise TaskSessionError("runtime is invalid")
    surface = require_token(surface, "surface")
    result = run_cmux(["surface", "resume", "get", "--json", "--surface", surface], runner=runner)
    if result.returncode != 0:
        raise TaskSessionError("cmux resume binding is unavailable")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise TaskSessionError("cmux resume binding is invalid JSON") from exc
    binding = payload.get("resume_binding") if isinstance(payload, dict) else None
    return validate_checkpoint(binding, runtime)
