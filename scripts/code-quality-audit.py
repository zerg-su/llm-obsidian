#!/usr/bin/env python3
"""Report Python cohesion signals and fail on release-blocking monoliths."""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCAN = ROOT / "scripts" / "harness"
FILE_REVIEW_LINES = 200
FILE_HARD_LINES = 1_000
FUNCTION_REVIEW_LINES = 60
FUNCTION_HARD_LINES = 300
FUNCTION_HARD_BRANCHES = 60


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


def inspect_file(path: Path, scan_root: Path) -> FileSignal:
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
        path=str(path.relative_to(scan_root.parent.parent)),
        lines=len(source.splitlines()),
        functions=tuple(sorted(functions, key=lambda item: (item.line, item.name))),
    )


def inspect_tree(scan_root: Path) -> tuple[FileSignal, ...]:
    return tuple(inspect_file(path, scan_root) for path in sorted(scan_root.rglob("*.py")))


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", type=Path, default=DEFAULT_SCAN)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    scan_root = args.scan.resolve()
    rows = inspect_tree(scan_root)
    blockers, warnings = classify(rows)
    signals = blocking_signals(rows)
    baseline = None
    errors = blockers
    if args.baseline is not None:
        try:
            baseline = load_baseline(args.baseline.resolve())
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
        "release_blockers": blockers,
        "blocking_signals": signals,
        "ratchet_mode": baseline is not None,
        "warnings": warnings,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"code quality audit: {len(rows)} files, {len(blockers)} release blockers, "
            f"{len(errors)} active gate errors, "
            f"{len(warnings)} review signals"
        )
        for message in errors:
            print(f"ERROR {message}")
        for message in warnings:
            print(f"WARN  {message}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
