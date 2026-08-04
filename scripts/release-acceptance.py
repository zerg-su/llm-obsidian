#!/usr/bin/env python3
"""Validate the bounded four-cell harness release acceptance contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
CELL_IDS = (
    "claude-lifecycle",
    "codex-lifecycle",
    "cross-runtime-composition",
    "deep-review",
)
RUNTIMES = {"claude", "codex"}
KINDS = {"runtime-lifecycle", "workflow-composition", "deep-review"}
LEGACY_SKILLS = {
    "autoresearch",
    "dispatch-workspace",
    "reap-send",
    "review-dispatch",
    "review-send",
}
SHA = re.compile(r"[0-9a-f]{40}\Z")
NON_BEHAVIORAL_ROOTS = frozenset({"docs", "references", "wiki"})
NON_BEHAVIORAL_RUNTIME_PATHS = frozenset(
    {
        ".task-origin-session",
        ".task-pipeline-step-callback.json",
        ".task-pipeline-step-request.json",
    }
)
NON_BEHAVIORAL_RUNTIME_ROOTS = frozenset({".task-pipeline"})
BEHAVIORAL_DOCUMENTS = frozenset(
    {
        "AGENTS.md",
        "CLAUDE.md",
        "docs/runtime-capabilities.md",
        "docs/task-sessions.md",
    }
)
BEHAVIORAL_DOCUMENT_ROOTS = ("docs/skill-references/",)
LIVE_DRIVER = "scripts/live_acceptance_driver.py"
RELEASE_DEPENDENCIES = (
    "config/acceptance-cells.toml",
    "scripts/release-acceptance.py",
    "scripts/release_acceptance_support.py",
    "scripts/live-acceptance-runner.py",
    LIVE_DRIVER,
    "Makefile",
    *sorted(BEHAVIORAL_DOCUMENTS),
)
DEPENDENCY_ROOTS = (
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
)
REQUIRED_TRACES = {
    "claude-lifecycle": ("open", "callback", "same-run-continue", "exit", "close"),
    "codex-lifecycle": ("open", "callback", "same-run-continue", "exit", "close"),
    "cross-runtime-composition": ("dispatch", "simple-review", "reap"),
    "deep-review": (
        "anthropic-holistic",
        "openai-holistic",
        "bounded-callback",
        "terminal-cleanup",
    ),
}


class AcceptanceError(ValueError):
    pass


def git_paths(root: Path, *args: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=False,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AcceptanceError("cannot inspect release worktree")
    try:
        return tuple(
            raw.decode("utf-8", "surrogateescape")
            for raw in result.stdout.split(b"\0")
            if raw
        )
    except UnicodeError as exc:
        raise AcceptanceError("release worktree contains an undecodable path") from exc


def behavioral_path(relative: str) -> bool:
    path = Path(relative)
    if not path.parts:
        return False
    normalized = path.as_posix()
    if normalized in NON_BEHAVIORAL_RUNTIME_PATHS:
        return False
    if path.parts[0] in NON_BEHAVIORAL_RUNTIME_ROOTS:
        return False
    if (
        normalized in BEHAVIORAL_DOCUMENTS
        or any(normalized.startswith(prefix) for prefix in BEHAVIORAL_DOCUMENT_ROOTS)
    ):
        return True
    if path.parts[0] in NON_BEHAVIORAL_ROOTS:
        return False
    return not (len(path.parts) == 1 and path.suffix.casefold() == ".md")


def require_clean_head(root: Path) -> None:
    changed = set(git_paths(root, "diff", "--name-only", "-z", "HEAD", "--"))
    changed.update(git_paths(root, "ls-files", "--others", "--exclude-standard", "-z"))
    behavioral = sorted(path for path in changed if behavioral_path(path))
    if behavioral:
        preview = ", ".join(behavioral[:5])
        if len(behavioral) > 5:
            preview += f", +{len(behavioral) - 5} more"
        raise AcceptanceError(f"release evidence requires a clean HEAD; behavioral dirt: {preview}")


def load_manifest(root: Path) -> dict[str, Any]:
    path = root / "config/acceptance-cells.toml"
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AcceptanceError(f"cannot read acceptance manifest: {exc}") from exc
    if value.get("schema_version") != 2:
        raise AcceptanceError("acceptance manifest must use schema_version 2")
    release = value.get("release")
    if not isinstance(release, dict) or release.get("driver") != LIVE_DRIVER:
        raise AcceptanceError("acceptance manifest must select the repo-owned live driver")
    common = release.get("dependencies")
    roots = release.get("dependency_roots")
    if (
        not isinstance(common, list)
        or not all(isinstance(relative, str) for relative in common)
        or len(common) != len(set(common))
        or not set(RELEASE_DEPENDENCIES) <= set(common)
        or roots != list(DEPENDENCY_ROOTS)
    ):
        raise AcceptanceError("acceptance manifest has an incomplete release dependency closure")
    expanded_common = set(common)
    for relative in common:
        candidate = Path(str(relative))
        target = root / candidate
        if (
            candidate.is_absolute()
            or ".." in candidate.parts
            or target.is_symlink()
            or not target.is_file()
        ):
            raise AcceptanceError(f"missing or unsafe release dependency {relative!r}")
    for relative in git_paths(root, "ls-files", "-z", "--", *DEPENDENCY_ROOTS):
        candidate = root / relative
        if candidate.is_symlink() or not candidate.is_file():
            raise AcceptanceError(f"missing or unsafe behavioral dependency {relative!r}")
        expanded_common.add(relative)
    if not any(relative.startswith("scripts/harness/") for relative in expanded_common):
        raise AcceptanceError("acceptance manifest resolves no tracked harness dependencies")
    environment = value.get("environment")
    if (
        not isinstance(environment, dict)
        or environment.get("report_schema") != 3
        or environment.get("state_schema") != 3
        or environment.get("preflight_schema") != 1
        or environment.get("failed_cell_classifications")
        != ["runtime-contract", "mechanism-failure"]
    ):
        raise AcceptanceError("acceptance manifest has an invalid live evidence schema")
    required = value.get("required_cells")
    cells = value.get("cells")
    if required != list(CELL_IDS) or not isinstance(cells, dict) or tuple(cells) != CELL_IDS:
        raise AcceptanceError("acceptance manifest must declare exactly the four release cells")
    for cell_id, cell in cells.items():
        if not isinstance(cell, dict) or cell.get("kind") not in KINDS:
            raise AcceptanceError(f"{cell_id}: invalid cell kind")
        runtimes = cell.get("runtimes")
        expected = cell.get("expected")
        dependencies = cell.get("dependencies")
        if not isinstance(runtimes, list) or not runtimes or not set(runtimes) <= RUNTIMES:
            raise AcceptanceError(f"{cell_id}: invalid runtimes")
        if expected != list(REQUIRED_TRACES[cell_id]):
            raise AcceptanceError(f"{cell_id}: required trace contract changed")
        if not isinstance(dependencies, list) or not dependencies:
            raise AcceptanceError(f"{cell_id}: dependencies are required")
        for relative in dependencies:
            candidate = Path(str(relative))
            if (
                candidate.is_absolute()
                or ".." in candidate.parts
                or (root / candidate).is_symlink()
                or not (root / candidate).is_file()
            ):
                raise AcceptanceError(f"{cell_id}: missing or unsafe dependency {relative!r}")
        cell["_expanded_dependencies"] = sorted(
            expanded_common | {str(relative) for relative in dependencies}
        )
    exposed = {path.parent.name for path in (root / "skills").glob("*/SKILL.md")}
    stale = sorted(exposed & LEGACY_SKILLS)
    if stale:
        raise AcceptanceError("legacy public skills remain: " + ", ".join(stale))
    return value


def git_blob_ids(root: Path, commit_sha: str) -> dict[str, str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", "-z", "--full-tree", commit_sha],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AcceptanceError("cannot resolve release dependency objects")
    objects: dict[str, str] = {}
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            metadata, encoded_path = raw.split(b"\t", 1)
            _mode, kind, object_id = metadata.split(b" ", 2)
            relative = encoded_path.decode("utf-8", "surrogateescape")
        except (ValueError, UnicodeError) as exc:
            raise AcceptanceError("cannot parse release dependency objects") from exc
        if kind == b"blob":
            objects[relative] = object_id.decode("ascii")
    return objects


def dependency_fingerprint(
    manifest: dict[str, Any],
    cell_id: str,
    *,
    blob_ids: dict[str, str],
) -> str:
    cell = manifest["cells"][cell_id]
    digest = hashlib.sha256()
    for relative in cell["_expanded_dependencies"]:
        object_id = blob_ids.get(relative)
        if object_id is None:
            raise AcceptanceError(f"{cell_id}: dependency is not bound to HEAD: {relative}")
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(object_id.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def source_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode or not SHA.fullmatch(value):
        raise AcceptanceError("cannot resolve exact source SHA")
    return value


def contract(root: Path) -> dict[str, Any]:
    require_clean_head(root)
    commit_sha = source_sha(root)
    manifest = load_manifest(root)
    blob_ids = git_blob_ids(root, commit_sha)
    value = {
        "schema_version": 2,
        "commit_sha": commit_sha,
        "cells": [
            {
                "cell_id": cell_id,
                "kind": manifest["cells"][cell_id]["kind"],
                "runtimes": manifest["cells"][cell_id]["runtimes"],
                "route": manifest["cells"][cell_id]["route"],
                "required_trace": manifest["cells"][cell_id]["expected"],
                "dependencies": manifest["cells"][cell_id]["_expanded_dependencies"],
                "dependency_fingerprint": dependency_fingerprint(
                    manifest,
                    cell_id,
                    blob_ids=blob_ids,
                ),
            }
            for cell_id in CELL_IDS
        ],
    }
    require_clean_head(root)
    if source_sha(root) != commit_sha:
        raise AcceptanceError("release HEAD changed while binding acceptance evidence")
    return value


def validate_report(root: Path, report_path: Path) -> dict[str, Any]:
    expected = contract(root)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"cannot read live report: {exc}") from exc
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    from live_acceptance_driver import LiveDriverError, validate_release_evidence

    try:
        validate_release_evidence(expected, report)
    except LiveDriverError as exc:
        raise AcceptanceError(str(exc)) from exc
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "contract", "verify-report"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    try:
        if root != ROOT:
            raise AcceptanceError("release code and --root must resolve to the same checkout")
        if args.command == "verify-report":
            path = args.report or root / ".vault-meta/acceptance/latest-live.json"
            value = validate_report(root, path)
        else:
            value = contract(root)
        if args.command == "check":
            print(f"release-acceptance: 4 harness cells valid at {value['commit_sha']}")
        else:
            print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return 0
    except AcceptanceError as exc:
        print(f"release-acceptance: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
