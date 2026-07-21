#!/usr/bin/env python3
"""Semantic acceptance-cell dependencies, generations, and fingerprints."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import platform
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any, Iterable

from acceptance_dependencies import (
    DependencyLockError,
    closure as dependency_closure,
    read_lock,
    verify_lock,
)
from model_routing import load_config


DEFAULT_MANIFEST = "config/acceptance-cells.toml"
SAFE_REL = re.compile(r"^[A-Za-z0-9._/#-]+$")
ALLOWED_NON_BEHAVIORAL_PREFIXES = {"tests/"}
ALLOWED_NON_BEHAVIORAL_PATHS = {
    ".claude-plugin/marketplace.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "CHANGELOG.md",
}
ALLOWED_ORCHESTRATION_DEPENDENCIES = {
    "config/acceptance-cells.toml",
    "config/acceptance-dependencies.lock.json",
    "scripts/acceptance_dependencies.py",
    "scripts/acceptance_fingerprints.py",
    "scripts/acceptance-workspace-supervisor.py",
    "scripts/release-acceptance.py",
}
FRAGMENT_RE = re.compile(r"^([A-Za-z0-9._/-]+)#([A-Za-z_][A-Za-z0-9_]*)$")
MANIFEST_BEHAVIOR_FIELDS = {
    ".claude-plugin/plugin.json": ("agents",),
    ".codex-plugin/plugin.json": ("skills",),
}
ROUTING_TOML_PATHS = {
    "config/model-routing.toml",
    ".codex/config.toml",
    ".codex/dispatch-env.toml",
    ".codex/agents/daily-summarizer.toml",
}
ROUTING_TOML_PREFIXES = (".codex/profiles/",)
ROUTING_KEYS = {"model", "effort", "model_reasoning_effort", "reasoning_effort"}


class FingerprintError(ValueError):
    pass


def safe_dependencies(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        item = str(value or "").strip().replace("\\", "/")
        if (
            not item
            or not SAFE_REL.fullmatch(item)
            or item.startswith("/")
            or ".." in Path(item.partition("#")[0]).parts
        ):
            raise FingerprintError(f"unsafe acceptance dependency: {item!r}")
        if item not in result:
            result.append(item)
    return result


def _array(value: dict[str, Any], key: str) -> list[object]:
    raw = value.get(key)
    if not isinstance(raw, list):
        raise FingerprintError(f"acceptance manifest {key} must be an array")
    return raw


def read_manifest(root: Path, path: Path | None = None) -> dict[str, Any]:
    source = path or root / DEFAULT_MANIFEST
    try:
        value = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FingerprintError(f"cannot read acceptance manifest: {exc}") from exc
    if (
        value.get("schema_version") != 1
        or value.get("runner_contract_version") != 3
        or value.get("orchestration_contract_version") != 2
        or value.get("environment_scope_version") != 2
        or value.get("evidence_epoch") != 3
    ):
        raise FingerprintError(
            "acceptance manifest must use schema 1, runner 3, orchestration 2, "
            "environment scope 2, and evidence epoch 3"
        )
    _array(value, "global_dependencies")
    _array(value, "behavioral_abi_dependencies")
    _array(value, "behavioral_abi_fragments")
    _array(value, "registration_dependencies")
    exact_non_behavioral = set(safe_dependencies(_array(value, "non_behavioral_paths")))
    if exact_non_behavioral - ALLOWED_NON_BEHAVIORAL_PATHS:
        raise FingerprintError("acceptance non-behavioral paths exceed the code-owned allowlist")
    prefixes = non_behavioral_prefixes(value)
    if set(prefixes) - ALLOWED_NON_BEHAVIORAL_PREFIXES:
        raise FingerprintError("acceptance manifest may classify only tests/ as non-behavioral")
    orchestration = orchestration_dependencies(value)
    if orchestration != ALLOWED_ORCHESTRATION_DEPENDENCIES:
        raise FingerprintError("acceptance orchestration dependencies must match the code-owned allowlist")
    for rel in exact_non_behavioral | orchestration:
        if not (root / rel).is_file():
            raise FingerprintError(f"acceptance classified path is not a file: {rel}")
    for prefix in prefixes:
        if not (root / prefix.removesuffix("/")).is_dir():
            raise FingerprintError(f"non-behavioral acceptance prefix is not a directory: {prefix}")
    return value


def non_behavioral_paths(manifest: dict[str, Any]) -> set[str]:
    return set(safe_dependencies(_array(manifest, "non_behavioral_paths")))


def non_behavioral_prefixes(manifest: dict[str, Any]) -> tuple[str, ...]:
    prefixes = tuple(safe_dependencies(_array(manifest, "non_behavioral_prefixes")))
    if any(not prefix.endswith("/") for prefix in prefixes):
        raise FingerprintError("non-behavioral acceptance prefixes must end with /")
    return prefixes


def orchestration_dependencies(manifest: dict[str, Any]) -> set[str]:
    return set(safe_dependencies(_array(manifest, "orchestration_dependencies")))


def is_non_behavioral_path(path: str, exact: set[str], prefixes: tuple[str, ...]) -> bool:
    return path in exact or any(path.startswith(prefix) for prefix in prefixes)


def verify_dependency_lock(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    try:
        return verify_lock(root, manifest)
    except DependencyLockError as exc:
        raise FingerprintError(str(exc)) from exc


def runtime_script_references(root: Path, rel: str) -> list[str]:
    """Compatibility view of direct statically locked runtime edges."""

    try:
        lock = read_lock(root)
    except DependencyLockError as exc:
        raise FingerprintError(str(exc)) from exc
    values = lock.get("edges", {}).get(rel, [])
    return [item for item in values if Path(item).suffix in {".py", ".sh"}]


def _row_table(manifest: dict[str, Any], table: str, name: str) -> dict[str, Any]:
    raw = manifest.get(table)
    if not isinstance(raw, dict):
        raise FingerprintError(f"acceptance manifest {table} must be a table")
    item = raw.get(name, {})
    if not isinstance(item, dict):
        raise FingerprintError(f"acceptance manifest {table}.{name} must be a table")
    return item


def _skill_files(root: Path, skill: str) -> list[str]:
    skill_root = root / "skills" / skill
    if not (skill_root / "SKILL.md").is_file():
        raise FingerprintError(f"skill {skill!r} is not installed")
    return [
        path.relative_to(root).as_posix()
        for path in sorted(skill_root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    ]


def behavior_fragments(manifest: dict[str, Any], skill: str, scenario: str) -> list[str]:
    values = list(_array(manifest, "behavioral_abi_fragments"))
    values.extend(_row_table(manifest, "scenarios", scenario).get("behavior_fragments", []))
    values.extend(_row_table(manifest, "skills", skill).get("behavior_fragments", []))
    result = safe_dependencies(values)
    if any(FRAGMENT_RE.fullmatch(item) is None for item in result):
        raise FingerprintError("behavior fragments must use path.py#function syntax")
    return sorted(result)


def expanded_dependencies(root: Path, manifest: dict[str, Any], skill: str, scenario: str) -> list[str]:
    scenario_item = _row_table(manifest, "scenarios", scenario)
    skill_item = _row_table(manifest, "skills", skill)
    common_roots: list[object] = []
    for key in ("global_dependencies", "registration_dependencies", "behavioral_abi_dependencies"):
        common_roots.extend(_array(manifest, key))
    scoped_roots: list[object] = []
    scoped_roots.extend(scenario_item.get("dependencies", []))
    scoped_roots.extend(skill_item.get("dependencies", []))
    scoped_roots.extend(_skill_files(root, skill))
    try:
        lock = read_lock(root)
        fragment_modules = {
            item.partition("#")[0]
            for item in safe_dependencies(_array(manifest, "behavioral_abi_fragments"))
        }
        for table_name in ("scenarios", "skills"):
            table = manifest.get(table_name, {})
            if isinstance(table, dict):
                for item in table.values():
                    if isinstance(item, dict):
                        fragment_modules.update(
                            str(value).partition("#")[0]
                            for value in item.get("behavior_fragments", [])
                        )
        semantic_boundaries = fragment_modules | {"scripts/acceptance/adapters.py"}
        all_scoped_roots: set[str] = set()
        for table_name in ("scenarios", "skills"):
            table = manifest.get(table_name, {})
            if isinstance(table, dict):
                for item in table.values():
                    if isinstance(item, dict):
                        all_scoped_roots.update(safe_dependencies(item.get("dependencies", [])))
        all_scoped_roots.update(
            rel for rel in lock.get("roots", [])
            if isinstance(rel, str) and rel.startswith("skills/")
        )
        all_scoped_roots.difference_update(safe_dependencies(common_roots))
        hard_boundaries = orchestration_dependencies(manifest) | semantic_boundaries
        dependencies = set(dependency_closure(
            lock,
            safe_dependencies(common_roots),
            stop=hard_boundaries | all_scoped_roots,
        ))
        for scoped_root in safe_dependencies(scoped_roots):
            dependencies.update(dependency_closure(
                lock,
                [scoped_root],
                stop=hard_boundaries | (all_scoped_roots - {scoped_root}),
            ))
        return sorted(dependencies)
    except DependencyLockError as exc:
        raise FingerprintError(str(exc)) from exc


def _strip_routing_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_routing_values(item)
            for key, item in sorted(value.items())
            if key not in ROUTING_KEYS and key != "model_registry"
        }
    if isinstance(value, list):
        return [_strip_routing_values(item) for item in value]
    return value


def _semantic_file_bytes(path: Path, rel: str) -> bytes:
    if rel in MANIFEST_BEHAVIOR_FIELDS:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            value = {key: raw.get(key) for key in MANIFEST_BEHAVIOR_FIELDS[rel]}
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise FingerprintError(f"cannot normalize behavioral manifest {rel}: {exc}") from exc
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if rel in ROUTING_TOML_PATHS or any(rel.startswith(prefix) for prefix in ROUTING_TOML_PREFIXES):
        try:
            value = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise FingerprintError(f"cannot normalize routing config {rel}: {exc}") from exc
        return json.dumps(
            _strip_routing_values(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    return path.read_bytes()


def file_hashes(root: Path, dependencies: Iterable[str]) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for rel in sorted(set(dependencies)):
        path = root / rel
        digest = hashlib.sha256(_semantic_file_bytes(path, rel)).hexdigest() if path.is_file() else "missing"
        values.append({"path": rel, "sha256": digest})
    return values


def _fragment_payload(root: Path, fragment: str, *, source_text: str | None = None) -> dict[str, Any]:
    match = FRAGMENT_RE.fullmatch(fragment)
    if match is None:
        raise FingerprintError(f"invalid behavior fragment: {fragment}")
    rel, entry = match.groups()
    path = root / rel
    try:
        source = source_text if source_text is not None else path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=rel)
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise FingerprintError(f"cannot inspect behavior fragment {fragment}: {exc}") from exc
    functions = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assignments: dict[str, ast.AST] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            for item in ast.walk(target):
                if isinstance(item, ast.Name):
                    assignments[item.id] = node
    if entry not in functions:
        raise FingerprintError(f"behavior fragment entry is missing: {fragment}")
    selected_functions: set[str] = set()
    selected_assignments: set[str] = set()
    pending = [entry]
    while pending:
        name = pending.pop()
        if name in selected_functions:
            continue
        selected_functions.add(name)
        loaded = {
            node.id for node in ast.walk(functions[name])
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        pending.extend(sorted((loaded & set(functions)) - selected_functions))
        selected_assignments.update(loaded & set(assignments))
    return {
        "fragment": fragment,
        "functions": {
            name: ast.dump(functions[name], include_attributes=False)
            for name in sorted(selected_functions)
        },
        "assignments": {
            name: ast.dump(assignments[name], include_attributes=False)
            for name in sorted(selected_assignments)
        },
    }


def behavior_fragment_hashes(
    root: Path, manifest: dict[str, Any], skill: str, scenario: str,
    *, source_overrides: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for fragment in behavior_fragments(manifest, skill, scenario):
        rel = fragment.partition("#")[0]
        payload = _fragment_payload(root, fragment, source_text=(source_overrides or {}).get(rel))
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        result.append({"fragment": fragment, "sha256": hashlib.sha256(encoded.encode()).hexdigest()})
    return result


def live_runner_behavior_sha256(
    root: Path, row: dict[str, Any], *, source_text: str | None = None,
) -> str:
    """Compatibility helper: hash the row's semantic ABI without executing source."""

    manifest = read_manifest(root)
    adapter_override = source_text is not None and "def review_acceptance_fixture" in source_text
    launcher_override = source_text is not None and "def agent_argv" in source_text
    abi_hashes = file_hashes(
        root, safe_dependencies(_array(manifest, "behavioral_abi_dependencies"))
    )
    if launcher_override:
        digest = hashlib.sha256(source_text.encode()).hexdigest()
        for item in abi_hashes:
            if item["path"] == "scripts/acceptance/launchers.py":
                item["sha256"] = digest
    overrides = (
        {"scripts/acceptance/skill_adapters.py": source_text}
        if adapter_override else None
    )
    payload = {
        "abi": abi_hashes,
        "fragments": behavior_fragment_hashes(
            root, manifest, str(row["skill"]), str(row["scenario"]), source_overrides=overrides
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def cell_dependency_hashes(
    root: Path, dependencies: Iterable[str], *, skill: str, scenario: str
) -> list[dict[str, str]]:
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
        try:
            registry = json.loads((root / rel).read_text(encoding="utf-8"))
            value = registry[fragment[0]][fragment[1]]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise FingerprintError(f"cannot read acceptance registry fragment {rel}#{fragment[1]}") from exc
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        values.append({"path": f"{rel}#{fragment[1]}", "sha256": hashlib.sha256(encoded.encode()).hexdigest()})
    return values


def command_version(command: str) -> str:
    try:
        result = subprocess.run([command, "--version"], text=True, capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    lines = (result.stdout or result.stderr).splitlines()
    return lines[0].strip()[:120] if result.returncode == 0 and lines else "unavailable"


def environment_contract() -> dict[str, str]:
    return {
        "os": platform.system().lower(),
        "os_release": platform.release(),
        "architecture": platform.machine().lower(),
        "cmux": command_version("cmux"),
        "claude": command_version("claude"),
        "codex": command_version("codex"),
    }


def compatible_runtime_version(value: str) -> str:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)(?:\.\d+)?(?!\d)", value)
    return f"{match.group(1)}.{match.group(2)}" if match else value


def scoped_environment_contract(
    manifest: dict[str, Any], row: dict[str, Any], environment: dict[str, str],
    *, scope_version: int | None = None,
) -> dict[str, str]:
    version = manifest.get("environment_scope_version") if scope_version is None else scope_version
    if version != 2:
        raise FingerprintError(f"unsupported acceptance environment scope: {version}")
    scenario = str(row["scenario"])
    raw_tools = _row_table(manifest, "scenarios", scenario).get("runtime_tools", [row["runtime"]])
    if (
        not isinstance(raw_tools, list)
        or row["runtime"] not in raw_tools
        or any(tool not in {"claude", "codex"} for tool in raw_tools)
    ):
        raise FingerprintError(f"scenario {scenario!r} has invalid runtime_tools")
    keys = ("os", "os_release", "architecture", "cmux", *sorted(set(raw_tools)))
    missing = [key for key in keys if key not in environment]
    if missing:
        raise FingerprintError("acceptance environment is missing: " + ", ".join(missing))
    return {
        key: compatible_runtime_version(environment[key]) if key in {"claude", "codex"} else environment[key]
        for key in keys
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
    raise FingerprintError(f"launch model {model!r} has no registered major generation")


def launch_generations(
    root: Path, manifest: dict[str, Any], *, overrides: dict[str, str] | None = None,
) -> dict[str, dict[str, str]]:
    routes = manifest.get("generation_routes")
    if routes != {"include": ["runtimes.codex", "runtimes.claude"]}:
        raise FingerprintError("generation routes must contain only the two runtime defaults")
    config = load_config(root)
    selected = (
        {
            runtime: str(os.environ.get(f"LLM_OBSIDIAN_ACCEPTANCE_{runtime.upper()}_MODEL") or "").strip()
            for runtime in ("claude", "codex")
        }
        if overrides is None else dict(overrides)
    )
    result: dict[str, dict[str, str]] = {}
    for runtime in ("claude", "codex"):
        model = selected.get(runtime) or str(config.runtime_default(runtime)["model"])
        result[runtime] = {"model": model, "generation": canonical_generation(model, manifest)}
    return result


def production_generations(root: Path, manifest: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Return production defaults, intentionally ignoring acceptance overrides."""

    return launch_generations(root, manifest, overrides={})


def cell_metadata(
    root: Path,
    manifest: dict[str, Any],
    row: dict[str, Any],
    *,
    environment: dict[str, str] | None = None,
    generations: dict[str, dict[str, str]] | None = None,
    environment_scope_version: int | None = None,
    dependencies_override: Iterable[str] | None = None,
    include_live_runner_behavior: bool = True,
    live_runner_source_override: str | None = None,
) -> dict[str, Any]:
    dependencies = (
        expanded_dependencies(root, manifest, str(row["skill"]), str(row["scenario"]))
        if dependencies_override is None else sorted(safe_dependencies(dependencies_override))
    )
    generation_map = generations or launch_generations(root, manifest)
    launch = generation_map[str(row["runtime"])]
    environment_value = scoped_environment_contract(
        manifest, row, environment or environment_contract(), scope_version=environment_scope_version
    )
    behavior = live_runner_behavior_sha256(
        root, row, source_text=live_runner_source_override
    ) if include_live_runner_behavior else None
    payload: dict[str, Any] = {
        "evidence_epoch": manifest["evidence_epoch"],
        "runner_contract_version": manifest["runner_contract_version"],
        "phase": row["phase"],
        "skill": row["skill"],
        "runtime": row["runtime"],
        "scenario": row["scenario"],
        "expected": row["expected"],
        "dependencies": cell_dependency_hashes(root, dependencies, skill=str(row["skill"]), scenario=str(row["scenario"])),
        "behavior_fragments": behavior_fragment_hashes(root, manifest, str(row["skill"]), str(row["scenario"])),
        "environment": environment_value,
        "generation": launch["generation"],
    }
    if behavior is not None:
        payload["live_runner_behavior_sha256"] = behavior
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    environment_encoded = json.dumps(environment_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "cell_fingerprint": hashlib.sha256(encoded.encode()).hexdigest(),
        "dependencies": dependencies,
        "generation": launch["generation"],
        "launch_model": launch["model"],
        "live_runner_behavior_sha256": behavior,
        "environment_sha256": hashlib.sha256(environment_encoded.encode()).hexdigest(),
        "evidence_epoch": manifest["evidence_epoch"],
    }


def generation_snapshot(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    routes = production_generations(root, manifest)
    return {
        "schema_version": 2,
        "generations": {runtime: item["generation"] for runtime, item in routes.items()},
    }


def changed_paths(root: Path, prior_commit: str, *, include_dirty: bool = True) -> set[str] | None:
    if not re.fullmatch(r"[0-9a-f]{40}", prior_commit):
        return None
    ancestry = subprocess.run(["git", "merge-base", "--is-ancestor", prior_commit, "HEAD"], cwd=root, text=True, capture_output=True, check=False)
    if ancestry.returncode != 0:
        return None
    diff = subprocess.run(["git", "diff", "--name-only", prior_commit, "HEAD"], cwd=root, text=True, capture_output=True, check=False)
    dirty = dirty_paths(root) if include_dirty else set()
    if diff.returncode != 0 or dirty is None:
        return None
    return {line.strip() for line in diff.stdout.splitlines() if line.strip()} | dirty


def dirty_paths(root: Path) -> set[str] | None:
    status = subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=root, text=True, capture_output=True, check=False)
    if status.returncode != 0:
        return None
    return {
        line[3:].split(" -> ")[-1].strip('"')
        for line in status.stdout.splitlines() if len(line) >= 4
    }


__all__ = [
    "FingerprintError", "behavior_fragment_hashes", "canonical_generation",
    "cell_metadata", "changed_paths", "dirty_paths", "environment_contract",
    "expanded_dependencies", "generation_snapshot", "is_non_behavioral_path",
    "launch_generations", "live_runner_behavior_sha256", "non_behavioral_paths",
    "non_behavioral_prefixes", "orchestration_dependencies", "production_generations",
    "read_manifest", "scoped_environment_contract", "verify_dependency_lock",
    "runtime_script_references",
]
