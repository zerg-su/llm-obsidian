#!/usr/bin/env python3
"""Measure and ratchet hermetic statement-line coverage for the harness.

The standard-library ``trace`` summary can display a misleading 100% over only
observed lines. This audit combines its runtime counts with an AST-derived statement
denominator, runs the existing fake/unit harness suite, requires every repo-owned
harness module to appear, and enforces conservative release floors for the stateful
core. Provider-backed and platform acceptance remain separate gates.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import trace
from pathlib import Path

from harness.audit_manifest import AuditManifestError, load_audit_manifest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = load_audit_manifest(ROOT)

# Integer floors intentionally ratchet the measured 2.6 baseline without pretending
# that adapter/orchestration line coverage is the same as transition completeness.
MIN_WEIGHTED_PERCENT = 73.0
CRITICAL_FLOORS = {
    "scripts.harness.callbacks": 87.0,
    "scripts.harness.contracts": 98.0,
    "scripts.harness.liveness": 86.0,
    "scripts.harness.pipeline_builtins": 93.0,
    "scripts.harness.pipelines": 84.0,
    "scripts.harness.runtime_sessions": 74.0,
    "scripts.harness.runtime_worker": 71.0,
    "scripts.harness.state_machine": 97.0,
    "scripts.harness.store": 94.0,
    "scripts.harness.workflows.review_gate": 81.0,
}


def coverage_percent(covered: int, executable: int) -> float:
    """Return a stable percentage; an empty module has no missed statements."""
    if executable == 0:
        return 100.0
    return covered * 100 / executable


def transition_matrix_case_count(output: str) -> int:
    """Read the executed matrix count from its own terminal contract."""
    match = re.search(r"^release transition matrix passed: (\d+) cases$", output, re.M)
    if match is None:
        raise ValueError("transition matrix output has no case count")
    return int(match.group(1))


def critical_report_lines(rows: dict[str, dict[str, object]]) -> list[str]:
    """Format critical module coverage without assuming every row exists."""
    lines: list[str] = []
    for module, floor in sorted(CRITICAL_FLOORS.items()):
        row = rows.get(module)
        if row is None:
            lines.append(f"  {module}: missing from report (floor {floor:.1f}%)")
        else:
            lines.append(
                f"  {module}: {float(row['percent']):.1f}% (floor {floor:.1f}%)"
            )
    return lines


def source_modules() -> set[str]:
    modules: set[str] = set()
    for path in MANIFEST.source_paths(ROOT):
        relative = path.relative_to(ROOT).with_suffix("")
        modules.add(".".join(relative.parts))
    return modules


def test_files() -> list[Path]:
    return list(MANIFEST.test_paths(ROOT))


def run_trace() -> tuple[dict[str, dict[str, object]], int]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part
        for part in (str(ROOT / "scripts"), env.get("PYTHONPATH", ""))
        if part
    )
    with tempfile.TemporaryDirectory(prefix="llm-obsidian-harness-coverage.") as raw:
        scratch = Path(raw)
        counts = scratch / "counts.dat"
        report_dir = scratch / "reports"
        matrix_cases: int | None = None
        for test_path in test_files():
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "trace",
                    "--count",
                    "--file",
                    str(counts),
                    "--coverdir",
                    str(report_dir),
                    str(test_path.relative_to(ROOT)),
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode:
                detail = result.stderr.strip() or "test failed without stderr"
                raise RuntimeError(f"{test_path.relative_to(ROOT)}: {detail}")
            if test_path.name == "test_release_transition_matrix.py":
                try:
                    matrix_cases = transition_matrix_case_count(result.stdout)
                except ValueError as exc:
                    raise RuntimeError(str(exc)) from exc

        measured = trace.CoverageResults(infile=str(counts)).counts
        hit_by_path: dict[str, set[int]] = {}
        for (raw_path, line), count in measured.items():
            if count > 0:
                hit_by_path.setdefault(str(Path(raw_path).resolve()), set()).add(line)

    rows: dict[str, dict[str, object]] = {}
    for path in MANIFEST.source_paths(ROOT):
        statement_lines = {
            node.lineno
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.stmt)
        }
        hit_lines = statement_lines & hit_by_path.get(str(path.resolve()), set())
        module = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        executable = len(statement_lines)
        covered = len(hit_lines)
        rows[module] = {
            "executable_lines": executable,
            "covered_lines": covered,
            "missing_lines": sorted(statement_lines - hit_lines),
            "percent": round(coverage_percent(covered, executable), 1),
            "path": str(path.relative_to(ROOT)),
        }
    if matrix_cases is None:
        raise RuntimeError("transition matrix test was not executed")
    return rows, matrix_cases


def validate(rows: dict[str, dict[str, object]]) -> list[str]:
    errors: list[str] = []
    missing_modules = sorted(
        (source_modules() - set(rows))
        | {
            module
            for module, row in rows.items()
            if int(row["executable_lines"]) > 0 and int(row["covered_lines"]) == 0
        }
    )
    if missing_modules:
        errors.append("unobserved harness modules: " + ", ".join(missing_modules))
    for module, floor in sorted(CRITICAL_FLOORS.items()):
        row = rows.get(module)
        if row is None:
            errors.append(f"{module}: missing from report")
            continue
        percent = float(row["percent"])
        if percent < floor:
            errors.append(f"{module}: {percent:.1f}% is below {floor:.1f}%")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable result")
    args = parser.parse_args()

    try:
        rows, matrix_cases = run_trace()
    except (
        AuditManifestError,
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"harness coverage audit failed: {exc}", file=sys.stderr)
        return 2
    errors = validate(rows)
    executable = sum(int(row["executable_lines"]) for row in rows.values())
    covered = sum(int(row["covered_lines"]) for row in rows.values())
    payload = {
        "schema_version": 1,
        "coverage_kind": "stdlib-trace-ast-statement-lines",
        "module_count": len(rows),
        "executable_lines": executable,
        "covered_lines": covered,
        "weighted_percent": round(coverage_percent(covered, executable), 2),
        "transition_matrix": "tests/harness/test_release_transition_matrix.py",
        "transition_matrix_cases": matrix_cases,
        "critical_floors": CRITICAL_FLOORS,
        "minimum_weighted_percent": MIN_WEIGHTED_PERCENT,
        "manifest": "config/harness-audit-manifest.json",
        "excluded_entrypoints": [
            {"path": path, "reason": reason}
            for path, reason in MANIFEST.excluded_entrypoints
        ],
        "modules": dict(sorted(rows.items())),
        "errors": errors,
    }
    if payload["weighted_percent"] < MIN_WEIGHTED_PERCENT:
        errors.append(
            f"weighted coverage: {payload['weighted_percent']:.2f}% is below "
            f"{MIN_WEIGHTED_PERCENT:.2f}%"
        )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "harness statement-line coverage: "
            f"{payload['weighted_percent']:.2f}% across {len(rows)} modules "
            f"({covered}/{executable} lines)"
        )
        for line in critical_report_lines(rows):
            print(line)
        print(
            "transition completeness: "
            f"{matrix_cases:,} deterministic matrix cases"
        )
    if errors:
        for error in errors:
            print(f"ERROR {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
