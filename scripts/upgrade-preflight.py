#!/usr/bin/env python3
"""Fail-closed overlay-upgrade gate for active sessions and legacy routing."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from model_routing import (
    LOCAL_CONFIG,
    RoutingError,
    load_tracked_config,
    validate_local_config,
)
from harness.contracts import (
    ContractError,
    OwnedResources,
    operation_record_from_dict,
)
from harness.state_machine import TERMINAL


def worktrees(root: Path) -> list[Path]:
    result = subprocess.run(["git", "-C", str(root), "worktree", "list", "--porcelain"], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RoutingError("cannot enumerate git worktrees")
    return [Path(line.removeprefix("worktree ")).resolve() for line in result.stdout.splitlines() if line.startswith("worktree ")]


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def same_path(value: Any, path: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return Path(value).expanduser().resolve() == path
    except OSError:
        return False


def harness_sessions(root: Path) -> tuple[set[str], list[str]]:
    released_dispatches: set[str] = set()
    active: list[str] = []
    harness_root = root / ".vault-meta/harness/owners"
    if not harness_root.is_dir():
        return released_dispatches, active
    for operation_path in sorted(harness_root.glob("*/operations/*.json")):
        operation = read_object(operation_path)
        raw_spec = operation.get("spec")
        raw_kind = (
            str(raw_spec.get("kind") or "unknown")
            if isinstance(raw_spec, dict)
            else "unknown"
        )
        raw_operation_id = (
            str(raw_spec.get("operation_id") or operation_path.stem)
            if isinstance(raw_spec, dict)
            else operation_path.stem
        )
        try:
            record = operation_record_from_dict(operation)
        except (AttributeError, ContractError, TypeError, ValueError):
            active.append(f"harness:{raw_kind}:{raw_operation_id}")
            continue
        kind = record.spec.kind
        operation_id = record.spec.operation_id
        if record.state in TERMINAL:
            if record.pending_effect or record.resources != OwnedResources():
                active.append(f"harness:{kind}:{operation_id}")
            elif kind == "dispatch":
                if (
                    operation_id == operation_path.stem
                    and record.spec.owner_id == operation_id
                    and operation_path.parents[1].name == operation_id
                ):
                    released_dispatches.add(operation_id)
                else:
                    active.append(f"harness:{kind}:{operation_id}")
            continue
        active.append(f"harness:{kind}:{operation_id}")
    return released_dispatches, active


def active_sessions(root: Path) -> list[str]:
    active: list[str] = []
    released_dispatches, harness_active = harness_sessions(root)
    for tree in worktrees(root):
        task = read_object(tree / ".task-meta.json")
        if task and not (tree / ".task-reap-complete.json").is_file():
            released = (
                task.get("version") == 3
                and task.get("task_id") in released_dispatches
                and same_path(task.get("worktree"), tree)
            )
            if not released:
                active.append(f"task:{tree.name}")
        review = read_object(tree / ".review-meta.json")
        if review and review.get("status") not in {"finish_sent", "finished", "archived"}:
            active.append(f"review:{tree.name}")
    state_root = root / ".vault-meta/research-runs"
    if state_root.is_dir():
        for state_path in sorted(state_root.glob("*/state.json")):
            state = read_object(state_path)
            if not state or state.get("status") in {"complete", "fetch_rejected", "rejected"}:
                continue
            suffix = ":legacy-route" if not isinstance(state.get("routing"), dict) else ""
            active.append(f"research:{state_path.parent.name}{suffix}")
    broker_root = root / ".vault-meta/task-sessions/projects"
    if broker_root.is_dir():
        for task_path in sorted(broker_root.glob("*/tasks/*/task.json")):
            task = read_object(task_path)
            if not task or task.get("status") == "archived":
                continue
            raw_task_id = task.get("task_id")
            task_id = str(raw_task_id or task_path.parent.name)
            worktrees_value = task.get("worktrees")
            released = (
                task.get("schema_version") == 1
                and task.get("status") == "active"
                and raw_task_id == task_path.parent.name
                and task.get("project_id") == task_path.parents[2].name
                and task_id in released_dispatches
                and isinstance(worktrees_value, list)
                and bool(worktrees_value)
                and all(isinstance(value, str) and value for value in worktrees_value)
                and not (task_path.parent / "lanes").exists()
            )
            if not released:
                active.append(f"broker-task:{task_id}")
    active.extend(harness_active)
    return sorted(set(active))


def legacy_routing(root: Path) -> dict[str, dict[str, str]]:
    path = root / ".codex/dispatch-env.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")).get("codex_dispatch", {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    tracked = load_tracked_config(root)
    result: dict[str, dict[str, str]] = {}
    for runtime in ("codex", "claude"):
        model = data.get(f"{runtime}_review_model")
        effort = data.get(f"{runtime}_review_effort")
        present: dict[str, str] = {}
        if isinstance(model, str):
            present["model"] = model
        if isinstance(effort, str):
            present["effort"] = effort
        defaults = tracked.reviewer_default(runtime)
        legacy_defaults = tracked.legacy_reviewer_default(runtime)
        customized = not (
            all(value == defaults[key] for key, value in present.items())
            or all(value == legacy_defaults[key] for key, value in present.items())
        )
        if present and customized:
            result[runtime] = {}
            if isinstance(model, str):
                result[runtime]["model"] = model
            if isinstance(effort, str):
                result[runtime]["effort"] = effort
    return result


def render_local(values: dict[str, dict[str, str]]) -> str:
    lines = ["# Migrated from .codex/dispatch-env.toml after explicit confirmation."]
    registry: dict[str, str] = {}
    for runtime in ("codex", "claude"):
        if runtime not in values:
            continue
        lines.extend([f"[review_profiles.simple.{runtime}]"])
        for key in ("model", "effort"):
            if key in values[runtime]:
                lines.append(f'{key} = {json.dumps(values[runtime][key])}')
        lines.append("")
        if "model" in values[runtime]:
            registry[values[runtime]["model"]] = runtime
    if registry:
        lines.append("[model_registry]")
        for model, runtime in sorted(registry.items()):
            lines.append(f'{json.dumps(model)} = {json.dumps(runtime)}')
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-routing-migration", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        running = active_sessions(root)
        if running:
            print(
                "upgrade-preflight: active operations block clean-cut upgrade: "
                + ", ".join(running),
                file=sys.stderr,
            )
            print(
                "Recovery: finish or cancel live operations with the installed runtime; "
                "terminal harness records retaining an effect or owned resource require "
                "exact ownership reconciliation. Then rerun upgrade-preflight.",
                file=sys.stderr,
            )
            return 4
        legacy = legacy_routing(root)
        if legacy:
            if not args.confirm_routing_migration:
                print("upgrade-preflight: legacy custom model routing needs --confirm-routing-migration", file=sys.stderr)
                return 5
            target = root / LOCAL_CONFIG
            if target.exists():
                print(f"upgrade-preflight: refusing to overwrite existing {LOCAL_CONFIG}", file=sys.stderr)
                return 5
            rendered = render_local(legacy)
            validate_local_config(root, rendered)
            if args.apply:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
                try:
                    tmp.write_text(rendered, encoding="utf-8")
                    os.replace(tmp, target)
                finally:
                    tmp.unlink(missing_ok=True)
        print(json.dumps({"status": "ready", "active_sessions": [], "legacy_routing": bool(legacy), "migration_applied": bool(legacy and args.apply)}, sort_keys=True))
        return 0
    except RoutingError as exc:
        print(f"upgrade-preflight: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
