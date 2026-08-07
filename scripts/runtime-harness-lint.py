#!/usr/bin/env python3
"""Inventory direct runtime effects during the 2.3.0 clean-cut migration."""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_BINARIES = {"cmux", "claude", "codex"}
RUNTIME_BINARY_ENV = {
    "CMUX_BUNDLED_CLI_PATH": "cmux",
    "CLAUDE_BINARY": "claude",
    "CODEX_BINARY": "codex",
}
SHELL_CALL = re.compile(r"(?m)^[^#\n]*(?:^|[;&|(\"])\s*(cmux|claude|codex)\s+")
GENERATED_RUNTIME_ARGV = re.compile(
    r"""\[\s*["'](cmux|claude|codex)["']\s*,"""
)
PRODUCTION_SEAMS = {
    "scripts/dispatch_execution.py": "harness.workflows.dispatch.start_dispatch",
    "scripts/reap-runner.py": "harness.workflows.reap.run_reap",
    "scripts/research-isolation.py": "harness.workflows.research.start_research",
}
FORBIDDEN_RUNNER_IMPORTS = {
    "scripts/dispatch-runner.py": {
        "task_sessions.close_surface_exact",
        "task_sessions.spawn_right",
        "task_sessions.spawn_workspace",
        "cmux_workspace_lifecycle.bind_workspace_identity",
        "harness.adapters.cmux.CmuxAdapter",
    },
    "scripts/reap-runner.py": {
        "harness.callbacks.CallbackBroker",
        "harness.store.OperationStore",
        "harness.supervisor.OperationSupervisor",
    },
    "scripts/research-isolation.py": {
        "harness.adapters.cmux.CmuxAdapter",
        "harness.adapters.process.ProcessAdapter",
        "task_sessions.TaskSessionStore",
        "task_sessions.project_id_for",
    },
}


def _literal_runtime(value: str) -> set[str]:
    candidate = value.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if candidate.endswith(".exe"):
        candidate = candidate[:-4]
    return {candidate} if candidate in RUNTIME_BINARIES else set()


def _runtime_bindings(tree: ast.AST) -> dict[str, set[str]]:
    """Resolve bounded executable aliases used as subprocess argv[0]."""

    bindings: dict[str, set[str]] = {}

    def resolve(node: ast.AST | None) -> set[str]:
        if node is None:
            return set()
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return _literal_runtime(node.value)
        if isinstance(node, ast.Name):
            return bindings.get(node.id, set())
        if isinstance(node, ast.IfExp):
            return resolve(node.body) | resolve(node.orelse)
        if isinstance(node, ast.BoolOp):
            return set().union(*(resolve(item) for item in node.values))
        if isinstance(node, (ast.List, ast.Tuple)):
            return resolve(node.elts[0]) if node.elts else set()
        if isinstance(node, ast.BinOp):
            return resolve(node.left) | resolve(node.right)
        if isinstance(node, ast.Subscript):
            key = node.slice
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value in RUNTIME_BINARY_ENV
            ):
                return {RUNTIME_BINARY_ENV[key.value]}
            return set()
        if not isinstance(node, ast.Call):
            return set()
        if isinstance(node.func, ast.Name) and node.func.id in {"str", "Path"}:
            return resolve(node.args[0]) if node.args else set()
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in {"resolve", "expanduser", "absolute"}:
                return resolve(node.func.value)
            if node.func.attr == "which":
                return resolve(node.args[0]) if node.args else set()
            if node.func.attr in {"get", "getenv"} and node.args:
                key = node.args[0]
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value in RUNTIME_BINARY_ENV
                ):
                    return {RUNTIME_BINARY_ENV[key.value]}
        return set()

    assignments: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    assignments.append((target.id, value))
    for _ in range(len(assignments) + 1):
        changed = False
        for name, value in assignments:
            resolved = resolve(value)
            combined = bindings.get(name, set()) | resolved
            if combined != bindings.get(name, set()):
                bindings[name] = combined
                changed = True
        if not changed:
            break
    return bindings


def python_effects(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return set()
    effects: set[str] = set()
    bindings = _runtime_bindings(tree)

    def resolve_command(node: ast.AST) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return _literal_runtime(node.value)
        if isinstance(node, ast.Name):
            return bindings.get(node.id, set())
        return set()

    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            literal = [
                item.value
                for item in node.elts
                if isinstance(item, ast.Constant)
                and isinstance(item.value, str)
            ]
            if literal and literal[0] == "env":
                effects.update(RUNTIME_BINARIES & set(literal[1:]))
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "subprocess.run" in node.value
        ):
            effects.update(GENERATED_RUNTIME_ARGV.findall(node.value))
        if not isinstance(node, ast.Call) or not node.args:
            continue
        arg = node.args[0]
        subprocess_call = (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.func.attr
            in {"run", "Popen", "call", "check_call", "check_output"}
        )
        if isinstance(arg, (ast.List, ast.Tuple)) and arg.elts:
            first = arg.elts[0]
            if isinstance(first, ast.Constant):
                effects.update(resolve_command(first))
            elif subprocess_call:
                effects.update(resolve_command(first))
        elif subprocess_call:
            effects.update(resolve_command(arg))
    return effects


def inventory(root: Path = ROOT) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for top in ("scripts", "skills"):
        for path in sorted((root / top).rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".sh"}:
                continue
            relative = path.relative_to(root).as_posix()
            if "scripts/harness/adapters" in path.as_posix():
                continue
            effects = python_effects(path) if path.suffix == ".py" else {
                match.group(1) for match in SHELL_CALL.finditer(path.read_text(encoding="utf-8"))
            }
            if effects:
                found[relative] = sorted(effects)
    return found


def _python_symbols(path: Path) -> tuple[set[str], set[str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return set(), set()
    imports: dict[str, str] = {}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                imports[alias.asname or alias.name] = full
                imported.add(full)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name.split(".")[0]] = alias.name
                imported.add(alias.name)

    def call_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return imports.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            prefix = call_name(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        return ""

    calls = {
        call_name(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    return imported, calls


def lifecycle_seam_violations(root: Path = ROOT) -> dict[str, list[str]]:
    violations: dict[str, list[str]] = {}
    for relative, required in PRODUCTION_SEAMS.items():
        path = root / relative
        imported, calls = _python_symbols(path)
        problems: list[str] = []
        if required not in calls:
            problems.append(f"missing executable call: {required}")
        forbidden = sorted(FORBIDDEN_RUNNER_IMPORTS.get(relative, set()) & imported)
        problems.extend(f"forbidden runner import: {name}" for name in forbidden)
        if problems:
            violations[relative] = problems
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--strict", action="store_true", help="reject even allowlisted migration callers")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    actual = inventory(root)
    expected = json.loads(
        (root / "config/harness-direct-call-allowlist.json").read_text(
            encoding="utf-8"
        )
    )
    allowed = expected.get("temporary_direct_callers", {})
    missing = {path: bins for path, bins in actual.items() if allowed.get(path) != bins}
    stale = {path: bins for path, bins in allowed.items() if actual.get(path) != bins}
    direct_violations = actual if args.strict else missing
    seam_violations = lifecycle_seam_violations(root)
    result = {
        "status": (
            "clean"
            if not direct_violations and not stale and not seam_violations
            else "violations"
        ),
        "direct_callers": actual,
        "unlisted": missing,
        "stale_allowlist": stale,
        "lifecycle_seam_violations": seam_violations,
    }
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        for path, bins in sorted(direct_violations.items()):
            print(f"{path}: {','.join(bins)}")
        for path in sorted(stale):
            print(f"{path}: stale allowlist")
        for path, problems in sorted(seam_violations.items()):
            for problem in problems:
                print(f"{path}: {problem}")
    return 1 if direct_violations or stale or seam_violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
