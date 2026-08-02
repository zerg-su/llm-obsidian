#!/usr/bin/env python3
"""Unit contracts for the hermetic harness coverage audit."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "harness_coverage_audit",
    ROOT / "scripts" / "harness-coverage-audit.py",
)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


assert audit.coverage_percent(0, 0) == 100.0
assert audit.coverage_percent(3, 4) == 75.0
assert round(audit.coverage_percent(1, 3), 2) == 33.33
assert "scripts.task-review-runner" in audit.source_modules()
assert "scripts.dispatch-runner" in audit.source_modules()
assert "scripts.dispatch_contracts" in audit.source_modules()
assert "scripts.dispatch_setup" in audit.source_modules()
for module in (
    "approval",
    "custom_contracts",
    "execution",
    "io",
    "lifecycle",
    "workspace",
):
    assert f"scripts.dispatch_{module}" in audit.source_modules()
assert "scripts.task_session_contracts" in audit.source_modules()
assert "scripts.task_session_cmux_layout" in audit.source_modules()
assert "scripts.task_session_store" in audit.source_modules()
assert "scripts.task_session_store_io" in audit.source_modules()
for module in (
    "context",
    "current",
    "finalizing",
    "flow",
    "identity",
    "mechanism_recovery",
    "replay",
    "request",
    "resolution_bundle",
    "resolution_flow",
    "shared",
    "transport",
    "verification",
    "verification_recovery",
    "verification_resubmit",
):
    assert f"scripts.task_review_{module}" in audit.source_modules()
assert any(
    path == "scripts/review-runner.py" and reason
    for path, reason in audit.MANIFEST.excluded_entrypoints
)

missing_lines = audit.critical_report_lines({})
assert len(missing_lines) == len(audit.CRITICAL_FLOORS)
assert all("missing from report" in line for line in missing_lines)

matrix_output = "\n".join(
    (
        "OK   operation state matrix covers every source/target pair",
        "release transition matrix passed: 4370 cases",
    )
)
assert audit.transition_matrix_case_count(matrix_output) == 4370

try:
    audit.transition_matrix_case_count("release transition matrix passed")
except ValueError as exc:
    assert "case count" in str(exc)
else:
    raise AssertionError("missing matrix case count was accepted")

print("harness coverage audit unit contracts passed")
