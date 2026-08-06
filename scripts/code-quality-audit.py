#!/usr/bin/env python3
"""Report Python cohesion signals and fail on release-blocking monoliths."""

from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "config" / "code-quality-baseline.json"
FILE_REVIEW_LINES = 200
FILE_HARD_LINES = 1_000
FUNCTION_REVIEW_LINES = 60
FUNCTION_HARD_LINES = 300
FUNCTION_HARD_BRANCHES = 60
OWNED_PYTHON_ROOTS = (
    ".claude",
    "evals",
    "hooks",
    "prototypes",
    "scripts",
    "skills",
)
NON_PRODUCTION_DIRECTORIES = frozenset(
    {"__pycache__", "references", "tests"}
)

RC1_ACTIVE_AUTHORITY_FILES = (
    "scripts/harness/callback_submit_recovery.py",
    "scripts/harness/liveness.py",
    "scripts/harness/provider_events.py",
    "scripts/harness/runtime_provider_events.py",
    "scripts/harness/runtime_worker_liveness.py",
    "scripts/harness/workflows/review_gate_attempt.py",
    "scripts/harness/workflows/review_gate_recovery.py",
    "scripts/task_review_flow.py",
)
RC1_WRITABLE_AUTHORITY_SYMBOLS = frozenset(
    {
        "rearm_review_drive",
        "restart_for_boundary",
        "restart_for_liveness",
        "restart_task_review_for_boundary",
    }
)
_UUID_LITERAL = re.compile(
    r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FunctionSignal:
    name: str
    line: int
    lines: int
    branch_points: int


@dataclass(frozen=True)
class FileSignal:
    path: str
    lines: int
    functions: tuple[FunctionSignal, ...]


BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.IfExp,
    ast.comprehension,
)


def branch_points(node: ast.AST) -> int:
    total = 0
    for child in ast.walk(node):
        if isinstance(child, BRANCH_NODES):
            total += 1
        elif isinstance(child, ast.BoolOp):
            total += max(1, len(child.values) - 1)
        elif isinstance(child, ast.Try):
            total += len(child.handlers) + bool(child.orelse) + bool(child.finalbody)
        elif isinstance(child, ast.Match):
            total += len(child.cases)
    return total


def inspect_file(path: Path, repo_root: Path) -> FileSignal:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    functions = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = node.end_lineno or node.lineno
        functions.append(
            FunctionSignal(
                name=node.name,
                line=node.lineno,
                lines=end - node.lineno + 1,
                branch_points=branch_points(node),
            )
        )
    return FileSignal(
        path=str(path.relative_to(repo_root)),
        lines=len(source.splitlines()),
        functions=tuple(sorted(functions, key=lambda item: (item.line, item.name))),
    )


def inspect_tree(scan_root: Path) -> tuple[FileSignal, ...]:
    repo_root = scan_root.parent.parent
    return tuple(inspect_file(path, repo_root) for path in sorted(scan_root.rglob("*.py")))


def inspect_paths(paths: tuple[Path, ...], repo_root: Path) -> tuple[FileSignal, ...]:
    return tuple(inspect_file(path, repo_root) for path in sorted(paths))


def owned_source_paths(repo_root: Path = ROOT) -> tuple[Path, ...]:
    """Return repository-owned production Python, excluding pinned references.

    Skills may contain their own executable helpers, so they belong to the same
    maintainability gate as top-level scripts. Tests and byte-pinned upstream
    snapshots have separate ownership/evidence contracts.
    """

    roots = tuple(repo_root / name for name in OWNED_PYTHON_ROOTS)

    def is_production(path: Path, root: Path) -> bool:
        relative = path.relative_to(root)
        return (
            path.is_file()
            and not path.is_symlink()
            and not any(
                part in NON_PRODUCTION_DIRECTORIES
                for part in relative.parts[:-1]
            )
            and relative.name != "conftest.py"
            and not relative.name.startswith("test_")
            and not relative.name.endswith("_test.py")
        )

    return tuple(
        sorted(
            path
            for root in roots
            if root.is_dir() and not root.is_symlink()
            for path in root.rglob("*.py")
            if is_production(path, root)
        )
    )


def _incident_literal_kinds(value: str) -> tuple[str, ...]:
    kinds = []
    if _UUID_LITERAL.search(value):
        kinds.append("operation-uuid")
    if "/private/tmp/" in value:
        kinds.append("operator-local-path")
    if value.startswith(("Classified as ", "Choose boundary ")):
        kinds.append("decision-prose")
    return tuple(kinds)


def audit_rc1_active_authority(repo_root: Path = ROOT) -> dict[str, object]:
    """Inventory the bounded RC1 review/recovery contour without content."""

    production_files = []
    writable_authorities = []
    incident_literals = []
    for relative in RC1_ACTIVE_AUTHORITY_FILES:
        path = repo_root / relative
        if not path.is_file() or path.is_symlink():
            continue
        source = path.read_text(encoding="utf-8")
        production_files.append(
            {"path": relative, "loc": len(source.splitlines())}
        )
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in RC1_WRITABLE_AUTHORITY_SYMBOLS
            ):
                writable_authorities.append(
                    {"path": relative, "line": node.lineno, "symbol": node.name}
                )
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for kind in _incident_literal_kinds(node.value):
                    incident_literals.append(
                        {"path": relative, "line": node.lineno, "kind": kind}
                    )
    production_files.sort(key=lambda item: item["path"])
    writable_authorities.sort(
        key=lambda item: (item["path"], item["line"], item["symbol"])
    )
    incident_literals.sort(
        key=lambda item: (item["path"], item["line"], item["kind"])
    )
    return {
        "schema_version": 1,
        "production_files": production_files,
        "production_loc": sum(item["loc"] for item in production_files),
        "writable_authorities": writable_authorities,
        "incident_literals": incident_literals,
    }


def classify(rows: tuple[FileSignal, ...]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for row in rows:
        if row.lines > FILE_HARD_LINES:
            errors.append(f"{row.path}: {row.lines} lines exceeds hard limit {FILE_HARD_LINES}")
        elif row.lines > FILE_REVIEW_LINES:
            warnings.append(f"{row.path}: {row.lines} lines requires cohesion review")
        for function in row.functions:
            location = f"{row.path}:{function.line}:{function.name}"
            if function.lines > FUNCTION_HARD_LINES:
                errors.append(
                    f"{location}: {function.lines} lines exceeds hard limit {FUNCTION_HARD_LINES}"
                )
            elif function.lines > FUNCTION_REVIEW_LINES:
                warnings.append(
                    f"{location}: {function.lines} lines requires extraction review"
                )
            if function.branch_points > FUNCTION_HARD_BRANCHES:
                errors.append(
                    f"{location}: {function.branch_points} branch points exceeds hard limit "
                    f"{FUNCTION_HARD_BRANCHES}"
                )
    return errors, warnings


def blocking_signals(rows: tuple[FileSignal, ...]) -> dict[str, int]:
    """Return stable hotspot identities for the no-growth ratchet."""

    signals: dict[str, int] = {}
    for row in rows:
        if row.lines > FILE_HARD_LINES:
            signals[f"file-lines:{row.path}"] = row.lines
        for function in row.functions:
            if function.lines > FUNCTION_HARD_LINES:
                signals[f"function-lines:{row.path}:{function.name}"] = function.lines
            if function.branch_points > FUNCTION_HARD_BRANCHES:
                signals[
                    f"function-branches:{row.path}:{function.name}"
                ] = function.branch_points
    return signals


def load_baseline(path: Path) -> dict[str, Mapping[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("code-quality baseline is unavailable or invalid") from exc
    hotspots = value.get("hotspots") if isinstance(value, dict) else None
    if value.get("schema_version") != 1 or not isinstance(hotspots, dict):
        raise ValueError("code-quality baseline schema is invalid")
    for identity, entry in hotspots.items():
        if (
            not isinstance(identity, str)
            or not isinstance(entry, dict)
            or type(entry.get("max_value")) is not int
            or entry["max_value"] < 1
            or not isinstance(entry.get("owner"), str)
            or not entry["owner"].strip()
            or not isinstance(entry.get("evidence"), str)
            or not entry["evidence"].strip()
        ):
            raise ValueError("code-quality baseline hotspot is invalid")
    return hotspots


def ratchet_failures(
    signals: Mapping[str, int],
    baseline: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    failures = []
    for identity in sorted(set(signals) - set(baseline)):
        failures.append(f"new unowned blocker: {identity}={signals[identity]}")
    for identity in sorted(set(baseline) - set(signals)):
        failures.append(f"stale blocker baseline must be removed: {identity}")
    for identity in sorted(set(signals) & set(baseline)):
        maximum = int(baseline[identity]["max_value"])
        if signals[identity] > maximum:
            failures.append(
                f"blocker grew: {identity}={signals[identity]} exceeds {maximum}"
            )
    return failures


def effective_baseline_path(*, scan: Path | None, baseline: Path | None) -> Path | None:
    """Use the release ratchet for the canonical manifest, not ad-hoc scans."""

    if baseline is not None:
        return baseline.resolve()
    if scan is None:
        return DEFAULT_BASELINE
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--rc1-authority-json", action="store_true")
    args = parser.parse_args()
    if args.rc1_authority_json:
        try:
            authority = audit_rc1_active_authority(ROOT)
        except (OSError, SyntaxError) as exc:
            print(f"RC1 authority audit: {exc}")
            return 1
        print(json.dumps(authority, indent=2, sort_keys=True))
        return 0
    try:
        if args.scan is None:
            rows = inspect_paths(owned_source_paths(ROOT), ROOT)
        else:
            rows = inspect_tree(args.scan.resolve())
    except (OSError, SyntaxError) as exc:
        print(f"code quality audit: {exc}")
        return 1
    blockers, warnings = classify(rows)
    signals = blocking_signals(rows)
    baseline = None
    errors = blockers
    baseline_path = effective_baseline_path(scan=args.scan, baseline=args.baseline)
    if baseline_path is not None:
        try:
            baseline = load_baseline(baseline_path)
        except ValueError as exc:
            errors = [str(exc)]
        else:
            errors = ratchet_failures(signals, baseline)
    payload = {
        "schema_version": 1,
        "thresholds": {
            "file_review_lines": FILE_REVIEW_LINES,
            "file_hard_lines": FILE_HARD_LINES,
            "function_review_lines": FUNCTION_REVIEW_LINES,
            "function_hard_lines": FUNCTION_HARD_LINES,
            "function_hard_branches": FUNCTION_HARD_BRANCHES,
        },
        "files": [asdict(row) for row in rows],
        "errors": errors,
        "release_blockers": errors,
        "owned_hotspots": blockers if baseline is not None else [],
        "blocking_signals": signals,
        "ratchet_mode": baseline is not None,
        "manifest": (
            "repository-owned production Python roots"
            if args.scan is None
            else ""
        ),
        "excluded_entrypoints": [],
        "warnings": warnings,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"code quality audit: {len(rows)} files, {len(errors)} release blockers, "
            f"{len(blockers) if baseline is not None else 0} owned hotspots, "
            f"{len(warnings)} review signals"
        )
        for message in errors:
            print(f"ERROR {message}")
        for message in warnings:
            print(f"WARN  {message}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
