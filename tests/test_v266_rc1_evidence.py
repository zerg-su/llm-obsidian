#!/usr/bin/env python3
"""Schema contracts for the 2.6.6 RC1 incremental evidence artifacts."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = ROOT / "docs" / "acceptance"
SHA = re.compile(r"[0-9a-f]{40}\Z")


def load(name: str) -> dict[str, object]:
    value = json.loads((ACCEPTANCE / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict) and value.get("schema_version") == 1
    return value


receipts = load("v2.6.6-rc1-slice-receipts.json")
assert receipts.get("release") == "2.6.6-rc1"
slices = receipts.get("slices")
assert isinstance(slices, list)
seen_slices: set[str] = set()
for entry in slices:
    assert isinstance(entry, dict)
    assert set(entry) >= {
        "slice_id",
        "commit_sha",
        "red_command",
        "red_failure_reason",
        "green_command",
        "focused_command",
    }
    slice_id = entry["slice_id"]
    assert slice_id in tuple("ABCDEFG") and slice_id not in seen_slices
    seen_slices.add(slice_id)
    assert isinstance(entry["commit_sha"], str) and SHA.fullmatch(
        entry["commit_sha"]
    )
    for field in (
        "red_command",
        "red_failure_reason",
        "green_command",
        "focused_command",
    ):
        assert isinstance(entry[field], str) and entry[field].strip()
    commit_sha = str(entry["commit_sha"])
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit_sha, "HEAD"],
        cwd=ROOT,
        check=False,
    ).returncode == 0
    repair_shas = entry.get("integration_repair_commit_shas", [])
    assert isinstance(repair_shas, list)
    for repair_sha in repair_shas:
        assert isinstance(repair_sha, str) and SHA.fullmatch(repair_sha)
        assert subprocess.run(
            ["git", "merge-base", "--is-ancestor", repair_sha, "HEAD"],
            cwd=ROOT,
            check=False,
        ).returncode == 0

assert seen_slices == set("ABCDEFG")
forbidden = receipts.get("forbidden_before_integration_green")
assert isinstance(forbidden, list) and all(
    isinstance(command, str) and command for command in forbidden
)
for entry in slices:
    assert isinstance(entry, dict)
    command_segments = {
        segment.strip()
        for field in ("red_command", "green_command", "focused_command")
        for segment in str(entry[field]).split("&&")
    }
    assert command_segments.isdisjoint(forbidden)

findings = load("v2.6.6-rc1-findings.json")
assert findings.get("release") == "2.6.6-rc1"
finding_rows = findings.get("findings")
assert isinstance(finding_rows, list)
finding_ids = [row.get("finding_id") for row in finding_rows if isinstance(row, dict)]
assert len(finding_ids) == len(set(finding_ids))
assert set(finding_ids) == {
    "OI.E3.cross-head-rearm",
    "intent-e3-legacy-cross-head-provider-effect",
    "intent-nongoal-cross-head-rearm-added",
    "OI.E4b.ephemeral-unwired",
    "ENG-EPHEMERAL-INTEGRATION",
    "eng-ephemeral-profile-unwired",
    "OI.E6.time-screen-authority",
    "eng-incident-pinned-authorization",
    "ENG-INDEPENDENT-AVAILABILITY",
    "ENG-LEGACY-RESOLUTION-AUTHORITY",
    "ENG-SPLIT-BASE-BINDING",
    "eng-store-private-writers",
    "intent-e9-tdd-skill-verdict-missing",
    "intent-e14-gate-not-run-at-reviewed-head",
    "eng-receipt-subject-is-parent-commit",
    "OI.E14.outcome-proof-invalid",
    "RC1.L4.dead-adapters-unobserved",
}
for row in finding_rows:
    assert isinstance(row, dict)
    assert row.get("disposition") in {
        "fixed-rc1",
        "defer-rc2",
        "out-of-scope-2.7",
        "not-a-defect",
        "blocks-rc1",
    }
    assert isinstance(row.get("rationale"), str) and row["rationale"].strip()

ledger = load("v2.6.6-rc2-defect-ledger.json")
assert ledger.get("release") == "2.6.6-rc2"
ledger_rows = ledger.get("findings")
assert isinstance(ledger_rows, list)
required = {
    "finding_id",
    "observed_at_stage",
    "subject_head_sha",
    "reproducer_or_evidence",
    "severity",
    "rc1_relation",
    "disposition",
    "suggested_owner",
    "external_effects_observed",
}
for row in ledger_rows:
    assert isinstance(row, dict) and set(row) == required
    assert row["disposition"] == "defer-rc2"
    assert isinstance(row["subject_head_sha"], str) and SHA.fullmatch(
        row["subject_head_sha"]
    )
    assert isinstance(row["external_effects_observed"], bool)

failed = load("evidence/v2.6.6-rc1/failed-fbf87a4/diagnostic-receipt.json")
assert failed.get("type") == "diagnostic-only"
assert failed.get("evidence_disposition") == "not-verification-evidence"
assert failed.get("status") == "failed"
assert failed.get("subject_head_sha") == "fbf87a4e8ef532b43e4d55225c87ad0f39f55bd9"
assert failed.get("command_id") == "harness-coverage"
assert failed.get("command_index") == 2

passed = load("evidence/v2.6.6-rc1/replacement-126b5fe/receipt.json")
assert passed.get("status") == "passed"
assert passed.get("profile") == "release-final"
assert passed.get("execution_relation") == "release-candidate"
assert passed.get("subject_head_sha") == "126b5fecb087a231bd6fbec8ce3f5dfe9235a206"
commands = passed.get("commands")
assert isinstance(commands, list) and len(commands) == 15
assert [row.get("command_id") for row in commands if isinstance(row, dict)] == [
    "full-tests",
    "harness-coverage",
    "release-acceptance",
    "vault-validation",
    "code-quality",
    "skill-audit",
    "instruction-budget-adapter",
    "codex-adapter",
    "split-skill-audit",
    "mcp-sync-config",
    "codex-mcp-sync",
    "harness-status",
    "harness-doctor",
    "diff-check",
    "clean-status",
]
coverage_row = commands[1]
assert isinstance(coverage_row, dict)
assert coverage_row.get("observations") == {
    "coverage_kind": "stdlib-trace-ast-statement-lines",
    "covered_lines": 16755,
    "executable_lines": 22599,
    "transition_matrix_cases": 4370,
    "weighted_percent": 74.14,
}

print("2.6.6 RC1 evidence schemas passed")
