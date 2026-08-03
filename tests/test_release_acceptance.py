#!/usr/bin/env python3
"""Hermetic checks for the bounded four-cell release contract."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/release-acceptance.py"
SPEC = importlib.util.spec_from_file_location("release_acceptance_test", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"OK   {name}")


def operation(
    operation_id: str,
    kind: str,
    runtime: str,
    lane_id: str,
    run_id: str,
) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "kind": kind,
        "runtime": runtime,
        "lane_id": lane_id,
        "run_id": run_id,
        "terminal_state": "complete",
        "effect_outcome": "succeeded",
        "callback_count": 1,
        "owned_resources_remaining": 0,
    }


def cell_evidence(row: dict[str, object], commit_sha: str) -> dict[str, object]:
    cell_id = str(row["cell_id"])
    if cell_id == "claude-lifecycle":
        operations = [operation("claude-op", "runtime-lifecycle", "claude", "claude-lane", "claude-run")]
    elif cell_id == "codex-lifecycle":
        operations = [operation("codex-op", "runtime-lifecycle", "codex", "codex-lane", "codex-run")]
    elif cell_id == "cross-runtime-composition":
        operations = [
            operation("dispatch-op", "dispatch", "codex", "composition-lane", "dispatch-run"),
            operation(
                "review-op",
                "simple-review-holistic",
                "claude",
                "composition-lane",
                "review-run",
            ),
        ]
    else:
        operations = [
            operation(
                "fable-op",
                "simple-review-holistic",
                "claude",
                "fable-lane",
                "fable-run",
            ),
            operation(
                "sol-op",
                "simple-review-holistic",
                "codex",
                "sol-lane",
                "sol-run",
            ),
        ]
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": 2,
        "cell_id": cell_id,
        "commit_sha": commit_sha,
        "dependency_fingerprint": row["dependency_fingerprint"],
        "started_at": now,
        "finished_at": now,
        "operations": operations,
        "trace": row["required_trace"],
        "status": "passed",
    }


with tempfile.TemporaryDirectory(prefix="release-acceptance-test.") as raw:
    root = Path(raw)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "config").mkdir()
    shutil.copy2(ROOT / "config/acceptance-cells.toml", root / "config/acceptance-cells.toml")
    source_manifest = module.load_manifest(ROOT)
    check(
        "manifest pins restartable live evidence schemas",
        source_manifest["environment"]["report_schema"] == 3
        and source_manifest["environment"]["state_schema"] == 3
        and source_manifest["environment"]["preflight_schema"] == 1
        and source_manifest["environment"]["failed_cell_classifications"]
        == ["runtime-contract", "mechanism-failure"],
    )
    dependencies: set[str] = set()
    for row in source_manifest["cells"].values():
        dependencies.update(row["_expanded_dependencies"])
    for relative in dependencies:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    (root / "skills").mkdir(exist_ok=True)
    (root / "seed").write_text("seed\n", encoding="utf-8")
    (root / ".gitignore").write_text("report.json\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed"], check=True)
    value = module.contract(root)
    check("exactly four cells", [row["cell_id"] for row in value["cells"]] == list(module.CELL_IDS))
    check("contract binds exact SHA", module.SHA.fullmatch(value["commit_sha"]) is not None)
    check("every cell has a required lifecycle trace", all(3 <= len(row["required_trace"]) <= 5 for row in value["cells"]))
    check("every cell has dependency fingerprint", all(len(row["dependency_fingerprint"]) == 64 for row in value["cells"]))
    release_dependencies = {
        "config/acceptance-cells.toml",
        "scripts/release-acceptance.py",
        "scripts/release_acceptance_support.py",
        "scripts/live-acceptance-runner.py",
        "scripts/live_acceptance_driver.py",
    }
    check(
        "every fingerprint covers the release execution path",
        all(release_dependencies <= set(row.get("dependencies", ())) for row in value["cells"]),
    )
    behavioral_dependencies = {
        path
        for path in subprocess.run(
            [
                "git",
                "-C",
                str(ROOT),
                "ls-files",
                "scripts",
                "skills",
                "hooks",
                "schemas",
                "config",
                ".claude",
                ".codex",
                ".agents",
                ".codex-plugin",
                ".claude-plugin",
                "docs/skill-references",
            ],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.splitlines()
        if path
    }
    behavioral_dependencies.update(
        {
            "Makefile",
            "AGENTS.md",
            "CLAUDE.md",
            "docs/runtime-capabilities.md",
            "docs/task-sessions.md",
        }
    )
    check(
        "every fingerprint covers the tracked live behavioral closure",
        all(behavioral_dependencies <= set(row["dependencies"]) for row in value["cells"]),
    )
    alternate_cli = subprocess.run(
        [sys.executable, str(SCRIPT), "contract", "--root", str(root)],
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "release CLI rejects an alternate code checkout",
        alternate_cli.returncode == 3 and "same checkout" in alternate_cli.stderr,
    )
    check("legacy public skills are absent", not (module.LEGACY_SKILLS & {p.parent.name for p in (ROOT / "skills").glob("*/SKILL.md")}))
    task_origin = root / ".task-origin-session"
    task_origin.write_text("runtime-only coordinator identity\n", encoding="utf-8")
    module.contract(root)
    check("untracked task origin marker is non-behavioral runtime state", True)
    task_origin.unlink()
    dirty_dependency = root / "scripts/harness/callbacks.py"
    original_dependency = dirty_dependency.read_bytes()
    dirty_dependency.write_bytes(original_dependency + b"\n# unstaged acceptance bypass\n")
    try:
        module.contract(root)
    except module.AcceptanceError as exc:
        check("unstaged behavioral dirt rejected", "clean HEAD" in str(exc))
    else:
        raise AssertionError("unstaged behavioral dirt rejected")
    dirty_dependency.write_bytes(original_dependency)

    dirty_dependency.write_bytes(original_dependency + b"\n# staged acceptance bypass\n")
    subprocess.run(["git", "-C", str(root), "add", str(dirty_dependency)], check=True)
    try:
        module.contract(root)
    except module.AcceptanceError as exc:
        check("staged behavioral dirt rejected", "clean HEAD" in str(exc))
    else:
        raise AssertionError("staged behavioral dirt rejected")
    dirty_dependency.write_bytes(original_dependency)
    subprocess.run(["git", "-C", str(root), "add", str(dirty_dependency)], check=True)

    untracked_behavior = root / "scripts/untracked-acceptance-bypass.py"
    untracked_behavior.write_text("print('fabricated pass')\n", encoding="utf-8")
    try:
        module.contract(root)
    except module.AcceptanceError as exc:
        check("untracked behavioral dirt rejected", "clean HEAD" in str(exc))
    else:
        raise AssertionError("untracked behavioral dirt rejected")
    untracked_behavior.unlink()

    agent_instructions = root / "AGENTS.md"
    original_instructions = agent_instructions.read_bytes()
    agent_instructions.write_bytes(original_instructions + b"\n# dirty runtime instruction\n")
    try:
        module.contract(root)
    except module.AcceptanceError as exc:
        check("dirty agent instructions are behavioral dirt", "clean HEAD" in str(exc))
    else:
        raise AssertionError("dirty agent instructions are behavioral dirt")
    agent_instructions.write_bytes(original_instructions)

    fixture = module.contract(root)
    report = {
        "schema_version": 3,
        "commit_sha": fixture["commit_sha"],
        "preflight": {
            "schema_version": 1,
            "commit_sha": fixture["commit_sha"],
            "origin_surface": "11111111-1111-4111-8111-111111111111",
            "routes": [
                {
                    "runtime": "claude",
                    "model": "opus-5",
                    "effort": "high",
                    "profile": "executor",
                    "capabilities": [
                        "binary:claude",
                        "provider:authenticated",
                        "cmux:origin-alive",
                    ],
                }
            ],
            "status": "compatible",
        },
        "cells": [cell_evidence(row, fixture["commit_sha"]) for row in fixture["cells"]],
        "failures": [],
    }
    path = root / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    module.validate_report(root, path)
    check("complete exact-SHA report accepted", True)
    report["cells"][0]["operations"][0]["owned_resources_remaining"] = 1
    path.write_text(json.dumps(report), encoding="utf-8")
    try:
        module.validate_report(root, path)
    except module.AcceptanceError:
        check("owned resource leak rejected", True)
    else:
        raise AssertionError("owned resource leak rejected")

    prior_fingerprints = {
        row["cell_id"]: row["dependency_fingerprint"] for row in fixture["cells"]
    }
    common_dependency = root / "scripts/live-acceptance-runner.py"
    common_dependency.write_bytes(common_dependency.read_bytes() + b"\n# committed release change\n")
    subprocess.run(["git", "-C", str(root), "add", str(common_dependency)], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "change release runner"], check=True)
    changed = module.contract(root)
    check(
        "committed release-path change invalidates every cell",
        all(
            row["dependency_fingerprint"] != prior_fingerprints[row["cell_id"]]
            for row in changed["cells"]
        ),
    )
    transitive_fingerprints = {
        row["cell_id"]: row["dependency_fingerprint"] for row in changed["cells"]
    }
    transitive_dependency = root / "scripts/model_routing.py"
    transitive_dependency.write_bytes(
        transitive_dependency.read_bytes() + b"\n# committed transitive live change\n"
    )
    subprocess.run(["git", "-C", str(root), "add", str(transitive_dependency)], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "change transitive helper"], check=True)
    transitive_changed = module.contract(root)
    check(
        "committed transitive live dependency invalidates every cell",
        all(
            row["dependency_fingerprint"] != transitive_fingerprints[row["cell_id"]]
            for row in transitive_changed["cells"]
        ),
    )
    plugin_fingerprints = {
        row["cell_id"]: row["dependency_fingerprint"]
        for row in transitive_changed["cells"]
    }
    plugin_dependency = root / ".claude-plugin/plugin.json"
    plugin_dependency.write_bytes(
        plugin_dependency.read_bytes() + b"\n"
    )
    subprocess.run(["git", "-C", str(root), "add", str(plugin_dependency)], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "change plugin wiring"], check=True)
    plugin_changed = module.contract(root)
    check(
        "committed Claude plugin wiring invalidates every cell",
        all(
            row["dependency_fingerprint"] != plugin_fingerprints[row["cell_id"]]
            for row in plugin_changed["cells"]
        ),
    )

print("release acceptance tests passed")
