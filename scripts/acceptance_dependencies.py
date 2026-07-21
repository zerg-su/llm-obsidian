#!/usr/bin/env python3
"""Generate and verify the model-free live-acceptance dependency lock.

The lock is deliberately boring: manifest roots are expanded through static
Python imports and constant repo-relative path literals.  It never imports or
executes product code.  A newly reachable edge therefore changes the generated
lock and blocks acceptance until the reviewed lock is regenerated.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = "config/acceptance-cells.toml"
DEFAULT_LOCK = "config/acceptance-dependencies.lock.json"
LOCK_SCHEMA_VERSION = 1
GENERATOR_CONTRACT_VERSION = 1
SCAN_PREFIXES = (
    ".claude/",
    ".codex/",
    "agents/",
    "bin/",
    "config/",
    "evals/",
    "hooks/",
    "scripts/",
    "skills/",
)
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.@+-]+)+)")
PATH_BEARING_DATA = {
    "hooks/hooks.json",
    ".claude/skill-rules.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
}


class DependencyLockError(ValueError):
    pass


def _path_parts(node: ast.AST) -> list[str]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _path_parts(node.left) + _path_parts(node.right)
    if isinstance(node, ast.Name) and node.id == "ROOT":
        return ["$ROOT"]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value.strip("/")]
    return ["*"]


def _dynamic_repo_prefixes(tree: ast.AST) -> set[str]:
    prefixes: set[str] = set()
    tops = {prefix.rstrip("/") for prefix in SCAN_PREFIXES}
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            continue
        parts = _path_parts(node)
        if len(parts) < 3 or parts[0] != "$ROOT" or "*" not in parts:
            continue
        first = parts[1]
        if first in tops:
            prefixes.add(first + "/")
    return prefixes


def _dynamic_declarations(manifest: dict[str, Any]) -> dict[str, set[str]]:
    raw = manifest.get("dynamic_dependency_prefixes", {})
    if not isinstance(raw, dict):
        raise DependencyLockError("acceptance manifest dynamic_dependency_prefixes must be a table")
    result: dict[str, set[str]] = {}
    for source, values in raw.items():
        if not isinstance(source, str) or not isinstance(values, list):
            raise DependencyLockError("dynamic dependency declarations must map sources to arrays")
        prefixes = {_safe_rel(value) for value in values}
        if any(not prefix.endswith("/") or prefix not in SCAN_PREFIXES for prefix in prefixes):
            raise DependencyLockError(f"invalid dynamic dependency prefix declaration: {source}")
        result[_safe_rel(source)] = prefixes
    return result


def _safe_rel(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    parts = Path(text).parts
    if not text or text.startswith("/") or ".." in parts or "\x00" in text:
        raise DependencyLockError(f"unsafe dependency path: {text!r}")
    return text


def _manifest(root: Path, path: Path | None = None) -> dict[str, Any]:
    source = path or root / DEFAULT_MANIFEST
    try:
        value = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DependencyLockError(f"cannot read acceptance manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise DependencyLockError("acceptance manifest must be a table")
    return value


def _declared_roots(root: Path, manifest: dict[str, Any]) -> list[str]:
    values: list[object] = []
    for key in ("behavioral_abi_dependencies", "global_dependencies", "registration_dependencies"):
        raw = manifest.get(key, [])
        if not isinstance(raw, list):
            raise DependencyLockError(f"acceptance manifest {key} must be an array")
        values.extend(raw)
    raw_fragments = manifest.get("behavioral_abi_fragments", [])
    if not isinstance(raw_fragments, list):
        raise DependencyLockError("acceptance manifest behavioral_abi_fragments must be an array")
    values.extend(str(item).partition("#")[0] for item in raw_fragments)
    for table_name in ("scenarios", "skills"):
        table = manifest.get(table_name, {})
        if not isinstance(table, dict):
            raise DependencyLockError(f"acceptance manifest {table_name} must be a table")
        for name, item in table.items():
            if not isinstance(item, dict):
                raise DependencyLockError(f"acceptance manifest {table_name}.{name} must be a table")
            raw = item.get("dependencies", [])
            if not isinstance(raw, list):
                raise DependencyLockError(
                    f"acceptance manifest {table_name}.{name}.dependencies must be an array"
                )
            values.extend(raw)
            fragments = item.get("behavior_fragments", [])
            if not isinstance(fragments, list):
                raise DependencyLockError(
                    f"acceptance manifest {table_name}.{name}.behavior_fragments must be an array"
                )
            values.extend(str(fragment).partition("#")[0] for fragment in fragments)
    values.extend(
        path.relative_to(root).as_posix()
        for path in sorted((root / "skills").glob("*/**/*"))
        if path.is_file() and "__pycache__" not in path.parts
    )
    values.extend(
        path.relative_to(root).as_posix()
        for path in sorted((root / "evals" / "acceptance" / "seed").rglob("*"))
        if path.is_file()
    )
    for prefixes in _dynamic_declarations(manifest).values():
        for prefix in prefixes:
            values.extend(
                path.relative_to(root).as_posix()
                for path in sorted((root / prefix).rglob("*"))
                if path.is_file() and "__pycache__" not in path.parts
            )
    result = sorted({_safe_rel(item) for item in values})
    missing = [rel for rel in result if not (root / rel).is_file()]
    if missing:
        raise DependencyLockError("declared acceptance dependencies are missing: " + ", ".join(missing))
    return result


def _source_index(root: Path) -> tuple[set[str], dict[str, tuple[str, ...]]]:
    paths: set[str] = set()
    by_name: dict[str, list[str]] = {}
    for prefix in SCAN_PREFIXES:
        base = root / prefix
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            paths.add(rel)
            by_name.setdefault(path.name, []).append(rel)
    for rel in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        if (root / rel).is_file():
            paths.add(rel)
            by_name.setdefault(Path(rel).name, []).append(rel)
    return paths, {name: tuple(sorted(items)) for name, items in by_name.items()}


def _resolve_python_import(root: Path, source: str, module: str, level: int) -> str | None:
    source_path = Path(source)
    if level:
        base = source_path.parent
        for _ in range(max(0, level - 1)):
            base = base.parent
        candidate = base / module.replace(".", "/") if module else base
    else:
        candidate = Path(module.replace(".", "/"))
        candidates = [candidate, Path("scripts") / candidate]
        candidate = next(
            (item for item in candidates if (root / item).with_suffix(".py").is_file() or (root / item / "__init__.py").is_file()),
            candidate,
        )
    file_candidate = (root / candidate).with_suffix(".py")
    package_candidate = root / candidate / "__init__.py"
    if file_candidate.is_file():
        return file_candidate.relative_to(root).as_posix()
    if package_candidate.is_file():
        return package_candidate.relative_to(root).as_posix()
    return None


def _literal_edges(
    root: Path,
    source: str,
    text: str,
    known: set[str],
    by_name: dict[str, tuple[str, ...]],
) -> set[str]:
    edges: set[str] = set()
    suffix = Path(source).suffix
    scan_text_paths = suffix in {".py", ".sh", ".md"} or source in PATH_BEARING_DATA
    candidates = (
        {match.group(1).strip("'\"`()[]{}.,:;") for match in TOKEN_RE.finditer(text)}
        if scan_text_paths else set()
    )
    try:
        tree = ast.parse(text, filename=source) if source.endswith(".py") else None
    except (SyntaxError, UnicodeError) as exc:
        raise DependencyLockError(f"cannot parse runtime dependency {source}: {exc}") from exc
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                candidates.add(node.value.strip().replace("\\", "/"))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    resolved = _resolve_python_import(root, source, alias.name, 0)
                    if resolved:
                        edges.add(resolved)
            elif isinstance(node, ast.ImportFrom):
                resolved = _resolve_python_import(root, source, node.module or "", node.level)
                if resolved:
                    edges.add(resolved)
                if node.module:
                    for alias in node.names:
                        nested = _resolve_python_import(
                            root, source, f"{node.module}.{alias.name}", node.level
                        )
                        if nested:
                            edges.add(nested)
    for raw in candidates:
        value = raw.removeprefix("./")
        if value in known:
            edges.add(value)
            continue
        if any(value.startswith(prefix) for prefix in SCAN_PREFIXES) and (root / value).is_file():
            edges.add(value)
            continue
        matches = by_name.get(Path(value).name, ())
        if len(matches) == 1 and ("/" in value or Path(value).suffix):
            edges.add(matches[0])
    edges.discard(source)
    return edges


def dependency_graph(root: Path, manifest: dict[str, Any]) -> tuple[list[str], dict[str, list[str]]]:
    roots = _declared_roots(root, manifest)
    declarations = _dynamic_declarations(manifest)
    known, by_name = _source_index(root)
    edges: dict[str, list[str]] = {}
    detected_dynamic: dict[str, set[str]] = {}
    pending = list(roots)
    seen: set[str] = set()
    while pending:
        rel = pending.pop()
        if rel in seen:
            continue
        seen.add(rel)
        path = root / rel
        if not path.is_file():
            raise DependencyLockError(f"reachable acceptance dependency is missing: {rel}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            edges[rel] = []
            continue
        if path.suffix == ".py":
            try:
                tree = ast.parse(text, filename=rel)
            except (SyntaxError, UnicodeError) as exc:
                raise DependencyLockError(f"cannot parse runtime dependency {rel}: {exc}") from exc
            prefixes = _dynamic_repo_prefixes(tree)
            if prefixes:
                detected_dynamic[rel] = prefixes
        children = sorted(_literal_edges(root, rel, text, known, by_name))
        edges[rel] = children
        pending.extend(child for child in children if child not in seen)
    if detected_dynamic != declarations:
        missing = {
            source: sorted(prefixes - declarations.get(source, set()))
            for source, prefixes in detected_dynamic.items()
            if prefixes - declarations.get(source, set())
        }
        stale = {
            source: sorted(prefixes - detected_dynamic.get(source, set()))
            for source, prefixes in declarations.items()
            if prefixes - detected_dynamic.get(source, set())
        }
        raise DependencyLockError(
            "dynamic repo dependencies require exact declarations; "
            f"missing={missing}, stale={stale}"
        )
    payload_edges = {rel: edges[rel] for rel in sorted(edges)}
    return roots, payload_edges


def lock_payload(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    roots, edges = dependency_graph(root, manifest)
    payload: dict[str, Any] = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "generator_contract_version": GENERATOR_CONTRACT_VERSION,
        "roots": roots,
        "edges": edges,
        "dynamic_dependency_prefixes": {
            source: sorted(prefixes)
            for source, prefixes in sorted(_dynamic_declarations(manifest).items())
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["graph_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return payload


def read_lock(root: Path, path: Path | None = None) -> dict[str, Any]:
    source = path or root / DEFAULT_LOCK
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DependencyLockError(f"cannot read acceptance dependency lock: {exc}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != LOCK_SCHEMA_VERSION
        or value.get("generator_contract_version") != GENERATOR_CONTRACT_VERSION
        or not isinstance(value.get("roots"), list)
        or not isinstance(value.get("edges"), dict)
        or not isinstance(value.get("dynamic_dependency_prefixes"), dict)
        or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("graph_sha256") or ""))
    ):
        raise DependencyLockError("acceptance dependency lock has an unsupported schema")
    return value


def verify_lock(root: Path, manifest: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    current = lock_payload(root, manifest)
    committed = read_lock(root, path)
    if committed != current:
        raise DependencyLockError(
            "acceptance dependency lock is stale; run scripts/acceptance_dependencies.py --write"
        )
    return committed


def closure(
    lock: dict[str, Any], roots: Iterable[str], *, stop: Iterable[str] = (),
) -> list[str]:
    edges = lock.get("edges")
    if not isinstance(edges, dict):
        raise DependencyLockError("acceptance dependency lock edges are malformed")
    pending = [_safe_rel(item) for item in roots]
    boundaries = {_safe_rel(item) for item in stop}
    result: set[str] = set()
    while pending:
        rel = pending.pop()
        if rel in result:
            continue
        if rel in boundaries:
            continue
        children = edges.get(rel)
        if not isinstance(children, list) or any(not isinstance(item, str) for item in children):
            raise DependencyLockError(f"dependency root is absent from lock: {rel}")
        result.add(rel)
        pending.extend(children)
    return sorted(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = _manifest(root, args.manifest)
    target = args.lock or root / DEFAULT_LOCK
    try:
        if args.write:
            value = lock_payload(root, manifest)
            target.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(target)
        else:
            verify_lock(root, manifest, target)
            print("acceptance dependency lock: ok")
        return 0
    except DependencyLockError as exc:
        print(f"acceptance dependency lock: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
