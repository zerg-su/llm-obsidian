#!/usr/bin/env python3
"""Schema contracts for the 2.6.6 RC1 incremental evidence artifacts."""

from __future__ import annotations

import json
import re
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

print("2.6.6 RC1 evidence schemas passed")
