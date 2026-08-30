#!/usr/bin/env python3
"""Behavior tests for explicit coordinator-vault resolution and registration."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from review_coordinator import (  # noqa: E402
    CoordinatorError,
    register_coordinator,
    resolve_coordinator,
)


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def coordinator(path: Path) -> Path:
    (path / "wiki").mkdir(parents=True)
    (path / "scripts").mkdir()
    (path / "skills/review").mkdir(parents=True)
    (path / "config").mkdir()
    (path / "scripts/task-review-runner.py").write_text("# fixture\n", encoding="utf-8")
    (path / "skills/review/SKILL.md").write_text("# Review\n", encoding="utf-8")
    (path / "config/model-routing.toml").write_text("schema_version = 1\n", encoding="utf-8")
    (path / "config/verification-profiles.toml").write_text("[profiles]\n", encoding="utf-8")
    return path.resolve()


with tempfile.TemporaryDirectory(prefix="review-coordinator-test.") as raw:
    base = Path(raw)
    explicit = coordinator(base / "explicit")
    environment = coordinator(base / "environment")
    ancestor = coordinator(base / "ancestor")
    nested = ancestor / "projects/product/src"
    nested.mkdir(parents=True)
    registered = coordinator(base / "registered")
    registry = base / "config/llm-obsidian/coordinators-v1.json"

    register_payload = register_coordinator(registered, registry_path=registry)
    check("explicit registration records the exact coordinator", register_payload["default_vault_root"] == str(registered))
    check("coordinator registry is private", stat.S_IMODE(registry.stat().st_mode) == 0o600)
    check("coordinator registry schema is exact", json.loads(registry.read_text(encoding="utf-8")) == {"schema_version": 1, "default_vault_root": str(registered)})

    resolved = resolve_coordinator(
        explicit=explicit,
        environment={"LLM_OBSIDIAN_PROJECT_ROOT": str(environment)},
        cwd=nested,
        registry_path=registry,
    )
    check("explicit coordinator has highest priority", resolved.root == explicit and resolved.source == "explicit")

    resolved = resolve_coordinator(
        environment={"LLM_OBSIDIAN_PROJECT_ROOT": str(environment)},
        cwd=nested,
        registry_path=registry,
    )
    check("environment coordinator precedes ancestors", resolved.root == environment and resolved.source == "environment")

    resolved = resolve_coordinator(environment={}, cwd=nested, registry_path=registry)
    check("LLM Obsidian ancestor precedes registry", resolved.root == ancestor and resolved.source == "ancestor")

    external = base / "plain/product"
    external.mkdir(parents=True)
    resolved = resolve_coordinator(environment={}, cwd=external, registry_path=registry)
    check("external repo uses explicitly registered default", resolved.root == registered and resolved.source == "registry")

    try:
        resolve_coordinator(
            environment={"LLM_OBSIDIAN_PROJECT_ROOT": str(base / "missing")},
            cwd=external,
            registry_path=registry,
        )
    except CoordinatorError as exc:
        check("invalid higher-priority root fails closed", "environment" in str(exc))
    else:
        check("invalid higher-priority root fails closed", False)

    plugin_cache = coordinator(base / ".codex/plugins/cache/llm-obsidian")
    try:
        register_coordinator(plugin_cache, registry_path=base / "cache-registry.json")
    except CoordinatorError as exc:
        check("plugin cache cannot own coordinator state", "plugin cache" in str(exc))
    else:
        check("plugin cache cannot own coordinator state", False)

    malformed = base / "malformed.json"
    malformed.write_text('{"schema_version":1,"default_vault_root":"x","extra":true}\n', encoding="utf-8")
    try:
        resolve_coordinator(environment={}, cwd=external, registry_path=malformed)
    except CoordinatorError as exc:
        check("malformed registry never falls through", "registry" in str(exc))
    else:
        check("malformed registry never falls through", False)

    empty = base / "empty"
    empty.mkdir()
    try:
        resolve_coordinator(environment={}, cwd=empty, registry_path=base / "absent.json")
    except CoordinatorError as exc:
        check("missing coordinator stops before review effects", "coordinator" in str(exc))
    else:
        check("missing coordinator stops before review effects", False)

print("\nAll review coordinator tests passed.")
