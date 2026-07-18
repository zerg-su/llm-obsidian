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
from model_routing import LOCAL_CONFIG, RoutingError, load_config


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


def active_sessions(root: Path) -> list[str]:
    active: list[str] = []
    for tree in worktrees(root):
        task = read_object(tree / ".task-meta.json")
        if task and not (tree / ".task-reap-complete.json").is_file():
            active.append(f"task:{tree.name}")
        review = read_object(tree / ".review-meta.json")
        if review and review.get("status") not in {"finish_sent", "finished", "archived"}:
            active.append(f"review:{tree.name}")
    return sorted(set(active))


def legacy_routing(root: Path) -> dict[str, dict[str, str]]:
    path = root / ".codex/dispatch-env.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")).get("codex_dispatch", {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    result: dict[str, dict[str, str]] = {}
    for runtime in ("codex", "claude"):
        model = data.get(f"{runtime}_review_model")
        effort = data.get(f"{runtime}_review_effort")
        if isinstance(model, str) or isinstance(effort, str):
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
        lines.extend([f"[runtimes.{runtime}]"])
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
            print("upgrade-preflight: active sessions require restart: " + ", ".join(running), file=sys.stderr)
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
            if args.apply:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
                try:
                    tmp.write_text(render_local(legacy), encoding="utf-8")
                    os.replace(tmp, target)
                finally:
                    tmp.unlink(missing_ok=True)
                load_config(root)
        print(json.dumps({"status": "ready", "active_sessions": [], "legacy_routing": bool(legacy), "migration_applied": bool(legacy and args.apply)}, sort_keys=True))
        return 0
    except RoutingError as exc:
        print(f"upgrade-preflight: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
