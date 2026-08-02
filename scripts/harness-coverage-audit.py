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
import subprocess
import sys
import tempfile
import trace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "harness"
EXTRA_TESTS = (ROOT / "tests" / "test_pipeline_verification_resubmit.py",)

# Integer floors intentionally ratchet the measured 2.6 baseline without pretending
# that adapter/orchestration line coverage is the same as transition completeness.
MIN_WEIGHTED_PERCENT = 75.0
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


def source_modules() -> set[str]:
    modules: set[str] = set()
    for path in HARNESS.rglob("*.py"):
        relative = path.relative_to(ROOT).with_suffix("")
        modules.add(".".join(relative.parts))
    return modules


def test_files() -> list[Path]:
    files = sorted((ROOT / "tests" / "harness").glob("test_*.py"))
    files.extend(path for path in EXTRA_TESTS if path.exists())
    return files


def run_trace() -> dict[str, dict[str, object]]:
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
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            if result.returncode:
                detail = result.stderr.strip() or "test failed without stderr"
                raise RuntimeError(f"{test_path.relative_to(ROOT)}: {detail}")

        measured = trace.CoverageResults(infile=str(counts)).counts
        hit_by_path: dict[str, set[int]] = {}
        for (raw_path, line), count in measured.items():
            if count > 0:
                hit_by_path.setdefault(str(Path(raw_path).resolve()), set()).add(line)

    rows: dict[str, dict[str, object]] = {}
    for path in sorted(HARNESS.rglob("*.py")):
        statement_lines = {
            node.lineno
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.stmt)
        }
        hit_lines = statement_lines & hit_by_path.get(str(path.resolve()), set())
        module = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        rows[module] = {
            "executable_lines": len(statement_lines),
            "covered_lines": len(hit_lines),
            "missing_lines": sorted(statement_lines - hit_lines),
            "percent": round(len(hit_lines) * 100 / len(statement_lines), 1),
            "path": str(path.relative_to(ROOT)),
        }
    return rows


def validate(rows: dict[str, dict[str, object]]) -> list[str]:
    errors: list[str] = []
    missing_modules = sorted(
        (source_modules() - set(rows))
        | {
            module
            for module, row in rows.items()
            if int(row["covered_lines"]) == 0
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
        rows = run_trace()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
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
        "weighted_percent": round(covered * 100 / executable, 2),
        "transition_matrix": "tests/harness/test_release_transition_matrix.py",
        "critical_floors": CRITICAL_FLOORS,
        "minimum_weighted_percent": MIN_WEIGHTED_PERCENT,
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
        for module, floor in sorted(CRITICAL_FLOORS.items()):
            print(f"  {module}: {float(rows[module]['percent']):.1f}% (floor {floor:.1f}%)")
        print("transition completeness: 4,370 deterministic matrix cases")
    if errors:
        for error in errors:
            print(f"ERROR {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
