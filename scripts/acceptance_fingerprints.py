#!/usr/bin/env python3
"""Deterministic acceptance-cell dependencies, generations, and fingerprints."""

from __future__ import annotations

import ast
import functools
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
    "scripts/acceptance-workspace-supervisor.py",
    "scripts/live-acceptance-runner.py",
    "scripts/release-acceptance.py",
}
RUNTIME_SCRIPT_PREFIXES = ("scripts/", "skills/", "hooks/", ".claude/hooks/", "bin/")
RUNTIME_SCRIPT_SUFFIXES = {".py", ".sh"}
LIVE_RUNNER_BEHAVIOR_PATH = "scripts/live-acceptance-runner.py"
LIVE_RUNNER_PROMPT_FUNCTION = "prompt_text"
LIVE_RUNNER_COMMON_FUNCTIONS = {
    "die", "read_json", "atomic_json", "load_scenarios", "load_skill_fixtures",
    "validate_row", "result_payload", "validate_agent_result", "git_head",
    "create_sandbox", "disable_acceptance_autocommit", "install_acceptance_model_overrides",
    "run_checked", "git_output", "agent_argv", "run_agent_process", "send_surface",
    "settled_outbox", "wait_for_outbox", "close_surface", "scratch_root_for",
    "safe_cleanup", "operation_child_surfaces", "surface_is_open",
    "wait_for_operation_children", "close_operation_children", "settle_operation_surfaces",
    "is_disposable_bookkeeping", "sandbox_cleanup_proof", "run_with_backend", "run_live",
    "blocked", "build_parser", "main",
}
LIVE_RUNNER_SCENARIO_FUNCTIONS = {
    "dispatch-review-reap": {
        "install_acceptance_runtime_fixture",
        "lifecycle_acceptance_cleanup_proof",
    },
}
LIVE_RUNNER_SKILL_FUNCTIONS = {
    "dispatch": {
        "dispatch_acceptance_fixture", "dispatch_fixture_prompt",
        "write_dispatch_acceptance_request", "dispatch_acceptance_proof",
    },
    "dispatch-workspace": {
        "dispatch_acceptance_fixture", "dispatch_fixture_prompt",
        "write_dispatch_acceptance_request", "dispatch_acceptance_proof",
    },
    "review-dispatch": {
        "review_acceptance_fixture", "bind_review_acceptance_fixture", "review_fixture_prompt",
    },
    "review-send": {
        "review_acceptance_fixture", "bind_review_acceptance_fixture", "review_fixture_prompt",
    },
    "close": {"close_acceptance_fixture", "close_fixture_prompt", "close_acceptance_proof"},
    "autoresearch": {"commit_file", "autoresearch_acceptance_cleanup"},
    "daily": {"daily_acceptance_cleanup"},
}


class FingerprintError(ValueError):
    pass


def read_manifest(root: Path, path: Path | None = None) -> dict[str, Any]:
    source = path or root / DEFAULT_MANIFEST
    try:
        value = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FingerprintError(f"cannot read acceptance manifest: {exc}") from exc
    environment_scope_version = value.get("environment_scope_version", 1)
    if (
        value.get("schema_version") != 1
        or value.get("runner_contract_version") != 2
        or value.get("orchestration_contract_version") != 1
        or isinstance(environment_scope_version, bool)
        or not isinstance(environment_scope_version, int)
        or environment_scope_version not in {1, 2}
    ):
        raise FingerprintError(
            "acceptance manifest must use schema 1, runner contract 2, "
            "orchestration contract 1, and environment scope 1 or 2"
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


@functools.lru_cache(maxsize=32)
def _runtime_script_index(root_text: str) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    root = Path(root_text)
    candidates = [
        path.relative_to(root).as_posix()
        for prefix in RUNTIME_SCRIPT_PREFIXES
        for path in (root / prefix).rglob("*")
        if path.is_file() and path.suffix in RUNTIME_SCRIPT_SUFFIXES
    ]
    by_name: dict[str, list[str]] = {}
    for candidate in candidates:
        by_name.setdefault(Path(candidate).name, []).append(candidate)
    return tuple(candidates), {name: tuple(values) for name, values in by_name.items()}


@functools.lru_cache(maxsize=1024)
def _cached_runtime_script_references(
    root_text: str, rel: str, source_sha256: str,
) -> tuple[str, ...]:
    root = Path(root_text)
    source = root / rel
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=rel)
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise FingerprintError(f"cannot inspect acceptance runtime dependency {rel}: {exc}") from exc
    _candidates, by_name = _runtime_script_index(root_text)
    runtime_helpers = {"run", "run_checked", "runner"}

    def invokes_process(function: ast.AST) -> bool:
        for item in ast.walk(function):
            if not isinstance(item, ast.Call):
                continue
            if isinstance(item.func, ast.Name) and item.func.id in runtime_helpers:
                return True
            if (
                isinstance(item.func, ast.Attribute)
                and isinstance(item.func.value, ast.Name)
                and item.func.value.id == "subprocess"
                and item.func.attr in {"run", "Popen", "check_call", "check_output"}
            ):
                return True
        return False

    runtime_functions = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and invokes_process(node)
    ]
    loaded_names = {
        node.id
        for function in runtime_functions
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    relevant_nodes: list[ast.AST] = list(runtime_functions)
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = {
            item.id for target in targets for item in ast.walk(target)
            if isinstance(item, ast.Name)
        }
        if names & loaded_names:
            relevant_nodes.append(node)

    references: set[str] = set()
    for relevant in relevant_nodes:
        for node in ast.walk(relevant):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            value = node.value.strip().replace("\\", "/")
            if Path(value).suffix not in RUNTIME_SCRIPT_SUFFIXES:
                continue
            if (
                any(value.startswith(prefix) for prefix in RUNTIME_SCRIPT_PREFIXES)
                and (root / value).is_file()
            ):
                references.add(value)
                continue
            matches = by_name.get(Path(value).name, [])
            if len(matches) == 1:
                references.add(matches[0])
    references.discard(rel)
    return tuple(sorted(references))


def runtime_script_references(root: Path, rel: str) -> list[str]:
    """Resolve repo-owned script literals used by a Python runtime dependency."""

    source = root / rel
    if source.suffix != ".py" or not source.is_file():
        return []
    try:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError as exc:
        raise FingerprintError(f"cannot inspect acceptance runtime dependency {rel}: {exc}") from exc
    return list(_cached_runtime_script_references(str(root.resolve()), rel, digest))


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
    dependencies = set(safe_dependencies(values))
    pending = list(dependencies)
    while pending:
        rel = pending.pop()
        for reference in runtime_script_references(root, rel):
            if reference not in dependencies:
                dependencies.add(reference)
                pending.append(reference)
    return sorted(dependencies)


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


@functools.lru_cache(maxsize=16)
def _live_runner_program(
    path_text: str, _source_sha256: str, source: str,
) -> tuple[tuple[str, ...], dict[str, str], Any]:
    try:
        tree = ast.parse(source, filename=LIVE_RUNNER_BEHAVIOR_PATH)
    except (SyntaxError, UnicodeError) as exc:
        raise FingerprintError(f"cannot inspect live acceptance behavior: {exc}") from exc
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    scoped_names = set().union(*LIVE_RUNNER_SCENARIO_FUNCTIONS.values())
    scoped_names.update(set().union(*LIVE_RUNNER_SKILL_FUNCTIONS.values()))
    expected_names = LIVE_RUNNER_COMMON_FUNCTIONS | scoped_names | {LIVE_RUNNER_PROMPT_FUNCTION}
    if set(functions) != expected_names:
        missing = sorted(expected_names - set(functions))
        unknown = sorted(set(functions) - expected_names)
        raise FingerprintError(
            "live acceptance behavior classification drifted; "
            f"missing={missing}, unclassified={unknown}"
        )
    module_nodes: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {
                item.id for target in targets for item in ast.walk(target)
                if isinstance(item, ast.Name)
            }
            if "VAULT_REINDEX_SCENARIOS" in names:
                continue
        module_nodes.append(ast.dump(node, include_attributes=False))
    namespace: dict[str, Any] = {
        "__file__": path_text,
        "__name__": "acceptance_behavior_fingerprint",
    }
    try:
        exec(compile(source, path_text, "exec"), namespace)
    except Exception as exc:
        raise FingerprintError(f"cannot load live acceptance behavior: {exc}") from exc
    return (
        tuple(module_nodes),
        {
            name: ast.dump(node, include_attributes=False)
            for name, node in functions.items()
        },
        namespace[LIVE_RUNNER_PROMPT_FUNCTION],
    )


def live_runner_behavior_sha256(
    root: Path, row: dict[str, Any], *, source_text: str | None = None,
) -> str:
    """Hash common code, scoped proof helpers, and the exact rendered row prompt."""

    path = root / LIVE_RUNNER_BEHAVIOR_PATH
    try:
        source = source_text if source_text is not None else path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FingerprintError(f"cannot inspect live acceptance behavior: {exc}") from exc
    module_nodes, function_dumps, prompt_renderer = _live_runner_program(
        str(path), hashlib.sha256(source.encode("utf-8")).hexdigest(), source,
    )
    selected_names = set(LIVE_RUNNER_COMMON_FUNCTIONS)
    selected_names.update(LIVE_RUNNER_SCENARIO_FUNCTIONS.get(str(row["scenario"]), set()))
    selected_names.update(LIVE_RUNNER_SKILL_FUNCTIONS.get(str(row["skill"]), set()))
    try:
        runner_fixture = None
        if row["skill"] in {"review-dispatch", "review-send"}:
            runner_fixture = {
                "fixture_kind": "review",
                "nested_worktree": "/acceptance/task",
            }
        elif row["skill"] in {"dispatch", "dispatch-workspace"}:
            runner_fixture = {
                "fixture_kind": "dispatch",
                "nested_worktree": "/acceptance/task",
            }
        prompt = prompt_renderer(
            row,
            {"network": "network-class", "instructions": "scenario instructions"},
            Path("/acceptance/sandbox"),
            Path("/acceptance/outbox.json"),
            "acceptance-model",
            "medium",
            "0" * 40,
            "skill fixture",
            runner_fixture,
        )
    except Exception as exc:
        raise FingerprintError(f"cannot render live acceptance behavior: {exc}") from exc
    payload = {
        "module_nodes": module_nodes,
        "functions": {
            name: function_dumps[name]
            for name in sorted(selected_names)
        },
        "prompt": prompt,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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


def compatible_runtime_version(value: str) -> str:
    """Collapse agent CLI patch releases into their supported major.minor line."""

    match = re.search(r"(?<!\d)(\d+)\.(\d+)(?:\.\d+)?(?!\d)", value)
    return f"{match.group(1)}.{match.group(2)}" if match else value


def scoped_environment_contract(
    manifest: dict[str, Any], row: dict[str, Any], environment: dict[str, str],
    *, scope_version: int | None = None,
) -> dict[str, str]:
    """Return only host/runtime versions that can affect this acceptance cell."""

    raw_version = (
        manifest.get("environment_scope_version", 1)
        if scope_version is None else scope_version
    )
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise FingerprintError("acceptance environment scope must be an integer")
    version = raw_version
    if version == 1:
        return dict(environment)
    if version != 2:
        raise FingerprintError(f"unsupported acceptance environment scope: {version}")
    scenario = str(row["scenario"])
    scenario_table = manifest.get("scenarios")
    if not isinstance(scenario_table, dict) or not isinstance(scenario_table.get(scenario), dict):
        raise FingerprintError(f"scenario {scenario!r} is missing from acceptance manifest")
    runtime = str(row["runtime"])
    raw_tools = scenario_table[scenario].get("runtime_tools", [runtime])
    if (
        not isinstance(raw_tools, list)
        or runtime not in raw_tools
        or any(tool not in {"claude", "codex"} for tool in raw_tools)
    ):
        raise FingerprintError(f"scenario {scenario!r} has invalid runtime_tools")
    keys = ("os", "os_release", "architecture", "cmux", *sorted(set(raw_tools)))
    missing = [key for key in keys if key not in environment]
    if missing:
        raise FingerprintError("acceptance environment is missing: " + ", ".join(missing))
    return {
        key: (
            compatible_runtime_version(environment[key])
            if key in {"claude", "codex"}
            else environment[key]
        )
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
    raise FingerprintError(f"production model {model!r} has no registered major generation")


def production_generations(root: Path, manifest: dict[str, Any]) -> dict[str, dict[str, str]]:
    routes = manifest.get("generation_routes")
    expected_routes = {"include": ["runtimes.codex", "runtimes.claude"]}
    if routes != expected_routes:
        raise FingerprintError(
            "generation routes must contain only the two production runtime defaults"
        )
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
    environment_scope_version: int | None = None,
    dependencies_override: Iterable[str] | None = None,
    include_live_runner_behavior: bool = True,
    live_runner_source_override: str | None = None,
) -> dict[str, Any]:
    dependencies = (
        expanded_dependencies(root, manifest, str(row["skill"]), str(row["scenario"]))
        if dependencies_override is None
        else sorted(safe_dependencies(dependencies_override))
    )
    generation_map = generations or production_generations(root, manifest)
    generation = generation_map[str(row["runtime"])]["generation"]
    environment_value = scoped_environment_contract(
        manifest,
        row,
        environment or environment_contract(),
        scope_version=environment_scope_version,
    )
    live_behavior = (
        live_runner_behavior_sha256(
            root, row, source_text=live_runner_source_override,
        ) if include_live_runner_behavior else None
    )
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
        "environment": environment_value,
        "generation": generation,
    }
    if live_behavior is not None:
        payload["live_runner_behavior_sha256"] = live_behavior
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    environment_encoded = json.dumps(
        environment_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {
        "cell_fingerprint": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "dependencies": dependencies,
        "generation": generation,
        "live_runner_behavior_sha256": live_behavior,
        "environment_sha256": hashlib.sha256(
            environment_encoded.encode("utf-8")
        ).hexdigest(),
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
    "live_runner_behavior_sha256",
    "scoped_environment_contract",
    "non_behavioral_paths",
    "non_behavioral_prefixes",
    "orchestration_dependencies",
    "is_non_behavioral_path",
    "production_generations",
    "read_manifest",
    "runtime_script_references",
]
