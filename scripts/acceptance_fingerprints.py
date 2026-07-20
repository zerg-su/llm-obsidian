#!/usr/bin/env python3
"""Deterministic acceptance-cell dependencies, generations, and fingerprints."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any, Iterable

from model_routing import load_config


DEFAULT_MANIFEST = "config/acceptance-cells.toml"
SAFE_REL = re.compile(r"^[A-Za-z0-9._/-]+$")
ALLOWED_NON_BEHAVIORAL_PREFIXES = {"tests/"}
ALLOWED_NON_BEHAVIORAL_PATHS = {
    ".claude-plugin/marketplace.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "CHANGELOG.md",
}
ALLOWED_ORCHESTRATION_DEPENDENCIES = {
    "config/acceptance-cells.toml",
    "scripts/acceptance_fingerprints.py",
    "scripts/release-acceptance.py",
}


class FingerprintError(ValueError):
    pass


def read_manifest(root: Path, path: Path | None = None) -> dict[str, Any]:
    source = path or root / DEFAULT_MANIFEST
    try:
        value = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FingerprintError(f"cannot read acceptance manifest: {exc}") from exc
    if (
        value.get("schema_version") != 1
        or value.get("runner_contract_version") != 2
        or value.get("orchestration_contract_version") != 1
    ):
        raise FingerprintError(
            "acceptance manifest must use schema 1, runner contract 2, and orchestration contract 1"
        )
    if not isinstance(value.get("global_dependencies"), list):
        raise FingerprintError("acceptance manifest global_dependencies must be an array")
    raw_non_behavioral = value.get("non_behavioral_paths")
    if not isinstance(raw_non_behavioral, list):
        raise FingerprintError("acceptance manifest non_behavioral_paths must be an array")
    exact_non_behavioral = set(safe_dependencies(raw_non_behavioral))
    if exact_non_behavioral - ALLOWED_NON_BEHAVIORAL_PATHS:
        raise FingerprintError("acceptance non-behavioral paths exceed the code-owned allowlist")
    for rel in exact_non_behavioral:
        if not (root / rel).is_file():
            raise FingerprintError(f"non-behavioral acceptance path is not a file: {rel}")
    prefixes = non_behavioral_prefixes(value)
    if set(prefixes) - ALLOWED_NON_BEHAVIORAL_PREFIXES:
        raise FingerprintError("acceptance manifest may classify only tests/ as a non-behavioral prefix")
    for prefix in prefixes:
        if not (root / prefix.removesuffix("/")).is_dir():
            raise FingerprintError(f"non-behavioral acceptance prefix is not a directory: {prefix}")
    orchestration = orchestration_dependencies(value)
    if orchestration != ALLOWED_ORCHESTRATION_DEPENDENCIES:
        raise FingerprintError("acceptance orchestration dependencies must match the code-owned allowlist")
    for rel in orchestration:
        if not (root / rel).is_file():
            raise FingerprintError(f"acceptance orchestration dependency is not a file: {rel}")
    return value


def safe_dependencies(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        item = str(value or "").strip().replace("\\", "/")
        if not item or not SAFE_REL.fullmatch(item) or item.startswith("/") or ".." in Path(item).parts:
            raise FingerprintError(f"unsafe acceptance dependency: {item!r}")
        if item not in result:
            result.append(item)
    return result


def expanded_dependencies(root: Path, manifest: dict[str, Any], skill: str, scenario: str) -> list[str]:
    scenario_table = manifest.get("scenarios")
    if not isinstance(scenario_table, dict) or not isinstance(scenario_table.get(scenario), dict):
        raise FingerprintError(f"scenario {scenario!r} is missing from acceptance manifest")
    values = list(manifest["global_dependencies"])
    values.extend(scenario_table[scenario].get("dependencies") or [])
    skill_table = manifest.get("skills") or {}
    if not isinstance(skill_table, dict):
        raise FingerprintError("acceptance manifest skills must be a table")
    override = skill_table.get(skill) or {}
    if not isinstance(override, dict):
        raise FingerprintError(f"acceptance manifest skill {skill!r} must be a table")
    values.extend(override.get("dependencies") or [])
    skill_root = root / "skills" / skill
    if not (skill_root / "SKILL.md").is_file():
        raise FingerprintError(f"skill {skill!r} is not installed")
    values.extend(
        path.relative_to(root).as_posix()
        for path in sorted(skill_root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    )
    return sorted(safe_dependencies(values))


def non_behavioral_paths(manifest: dict[str, Any]) -> set[str]:
    """Return exact reviewed paths that are known not to affect live behavior."""

    values = manifest.get("non_behavioral_paths")
    if not isinstance(values, list):
        raise FingerprintError("acceptance manifest non_behavioral_paths must be an array")
    return set(safe_dependencies(values))


def non_behavioral_prefixes(manifest: dict[str, Any]) -> tuple[str, ...]:
    """Return code-restricted directory prefixes that cannot affect product behavior."""

    values = manifest.get("non_behavioral_prefixes")
    if not isinstance(values, list):
        raise FingerprintError("acceptance manifest non_behavioral_prefixes must be an array")
    prefixes = tuple(safe_dependencies(values))
    if any(not prefix.endswith("/") for prefix in prefixes):
        raise FingerprintError("non-behavioral acceptance prefixes must end with /")
    return prefixes


def orchestration_dependencies(manifest: dict[str, Any]) -> set[str]:
    """Return code-owned evidence orchestration paths excluded from cell behavior."""

    values = manifest.get("orchestration_dependencies")
    if not isinstance(values, list):
        raise FingerprintError("acceptance manifest orchestration_dependencies must be an array")
    return set(safe_dependencies(values))


def is_non_behavioral_path(path: str, exact: set[str], prefixes: tuple[str, ...]) -> bool:
    return path in exact or any(path.startswith(prefix) for prefix in prefixes)


def file_hashes(root: Path, dependencies: Iterable[str]) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for rel in sorted(set(dependencies)):
        path = root / rel
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            digest = "missing"
        values.append({"path": rel, "sha256": digest})
    return values


def cell_dependency_hashes(
    root: Path, dependencies: Iterable[str], *, skill: str, scenario: str
) -> list[dict[str, str]]:
    """Hash shared registries by the exact row fragment, not as all-cell blobs."""

    fragment_keys = {
        "evals/acceptance/skills.json": ("skills", skill),
        "evals/acceptance/scenarios.json": ("scenarios", scenario),
    }
    values: list[dict[str, str]] = []
    for rel in sorted(set(dependencies)):
        fragment = fragment_keys.get(rel)
        if fragment is None:
            values.extend(file_hashes(root, [rel]))
            continue
        path = root / rel
        try:
            registry = json.loads(path.read_text(encoding="utf-8"))
            value = registry[fragment[0]][fragment[1]]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise FingerprintError(f"cannot read acceptance registry fragment {rel}#{fragment[1]}") from exc
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        values.append({
            "path": rel,
            "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        })
    return values


def command_version(command: str) -> str:
    try:
        result = subprocess.run(
            [command, "--version"], text=True, capture_output=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    line = (result.stdout or result.stderr).splitlines()
    return line[0].strip()[:120] if result.returncode == 0 and line else "unavailable"


def environment_contract() -> dict[str, str]:
    return {
        "os": platform.system().lower(),
        "os_release": platform.release(),
        "architecture": platform.machine().lower(),
        "cmux": command_version("cmux"),
        "claude": command_version("claude"),
        "codex": command_version("codex"),
    }


def canonical_generation(model: str, manifest: dict[str, Any]) -> str:
    registered = manifest.get("model_generations")
    if not isinstance(registered, dict):
        raise FingerprintError("acceptance manifest model_generations must be a table")
    explicit = registered.get(model)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    match = re.fullmatch(r"gpt-(\d+\.\d+)(?:-[A-Za-z0-9._-]+)?", model)
    if match:
        return f"codex:{match.group(1)}"
    raise FingerprintError(f"production model {model!r} has no registered major generation")


def production_generations(root: Path, manifest: dict[str, Any]) -> dict[str, dict[str, str]]:
    routes = manifest.get("generation_routes")
    if not isinstance(routes, dict):
        raise FingerprintError("acceptance manifest generation_routes must be a table")
    include = routes.get("include")
    exclude = routes.get("exclude")
    if include != ["runtimes.codex", "runtimes.claude"] or not isinstance(exclude, list):
        raise FingerprintError("generation routes must explicitly include both production runtimes")
    config = load_config(root)
    result: dict[str, dict[str, str]] = {}
    for runtime in ("claude", "codex"):
        model = str(config.runtime_default(runtime)["model"])
        result[runtime] = {"model": model, "generation": canonical_generation(model, manifest)}
    return result


def cell_metadata(
    root: Path,
    manifest: dict[str, Any],
    row: dict[str, Any],
    *,
    environment: dict[str, str] | None = None,
    generations: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    dependencies = expanded_dependencies(root, manifest, str(row["skill"]), str(row["scenario"]))
    generation_map = generations or production_generations(root, manifest)
    generation = generation_map[str(row["runtime"])]["generation"]
    payload = {
        "runner_contract_version": manifest["runner_contract_version"],
        "phase": row["phase"],
        "skill": row["skill"],
        "runtime": row["runtime"],
        "scenario": row["scenario"],
        "expected": row["expected"],
        "dependencies": cell_dependency_hashes(
            root,
            dependencies,
            skill=str(row["skill"]),
            scenario=str(row["scenario"]),
        ),
        "environment": environment or environment_contract(),
        "generation": generation,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "cell_fingerprint": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "dependencies": dependencies,
        "generation": generation,
    }


def generation_snapshot(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    routes = production_generations(root, manifest)
    return {
        "schema_version": 1,
        "generations": {runtime: item["generation"] for runtime, item in routes.items()},
    }


def changed_paths(root: Path, prior_commit: str, *, include_dirty: bool = True) -> set[str] | None:
    if not re.fullmatch(r"[0-9a-f]{40}", prior_commit):
        return None
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", prior_commit, "HEAD"],
        cwd=root, text=True, capture_output=True, check=False,
    )
    if ancestry.returncode != 0:
        return None
    diff = subprocess.run(
        ["git", "diff", "--name-only", prior_commit, "HEAD"],
        cwd=root, text=True, capture_output=True, check=False,
    )
    dirty = dirty_paths(root) if include_dirty else set()
    if diff.returncode != 0 or dirty is None:
        return None
    values = {line.strip() for line in diff.stdout.splitlines() if line.strip()}
    values.update(dirty)
    return values


def dirty_paths(root: Path) -> set[str] | None:
    """Return staged, unstaged, and non-ignored untracked repo-relative paths."""

    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root, text=True, capture_output=True, check=False,
    )
    if status.returncode != 0:
        return None
    values: set[str] = set()
    for line in status.stdout.splitlines():
        if len(line) >= 4:
            values.add(line[3:].split(" -> ")[-1].strip('"'))
    return values


__all__ = [
    "FingerprintError",
    "cell_metadata",
    "changed_paths",
    "dirty_paths",
    "environment_contract",
    "expanded_dependencies",
    "generation_snapshot",
    "non_behavioral_paths",
    "non_behavioral_prefixes",
    "orchestration_dependencies",
    "is_non_behavioral_path",
    "production_generations",
    "read_manifest",
]
