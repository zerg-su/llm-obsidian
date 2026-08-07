#!/usr/bin/env python3
"""RC2 frozen scope, typed dispositions, and deletion-topology contracts."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_SHA = "3f8d02e936af99b49aaf83a466c756799c38736c"
LEDGER = ROOT / "docs/acceptance/v2.6.6-rc2-defect-ledger.json"
DEVIATIONS = ROOT / "docs/acceptance/v2.6.6-rc2-accepted-deviations.json"
CLASSIC = (
    "scripts/cmux_agent_supervisor.py",
    "scripts/cmux_supervisor_policy.py",
    "scripts/cmux_supervisor_review.py",
    "scripts/cmux_supervisor_contracts.py",
    "scripts/cmux_task_watchdog.py",
    "scripts/cmux_trust_prompt.py",
)
CONDITIONAL = "scripts/archive_task_reviews.py"


def module_name(path: str) -> str:
    return Path(path).stem


def production_importers(module: str) -> list[str]:
    callers: list[str] = []
    excluded = {module_name(path) for path in CLASSIC}
    excluded.add(module_name(CONDITIONAL))
    for path in sorted((ROOT / "scripts").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if module_name(relative) in excluded:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        if module in imported:
            callers.append(relative)
    return callers


ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
assert ledger["schema_version"] == 2
assert ledger["release"] == "2.6.6-rc2"
assert ledger["baseline_subject_sha"] == BASE_SHA
assert ledger["disposition_kinds"] == [
    "accepted-deviation-rc2",
    "defer-post-rc2",
    "deleted-rc2",
    "fixed-rc2",
    "implemented-rc2",
    "not-in-scope",
]

rows = ledger["findings"]
assert isinstance(rows, list) and rows
by_id = {row["finding_id"]: row for row in rows}
assert len(by_id) == len(rows)
for row in rows:
    assert row["disposition"] in ledger["disposition_kinds"]
    assert set(row["owner"]) == {"kind", "id"}
    assert row["owner"]["kind"] in {"component", "governance", "release"}
    assert isinstance(row["owner"]["id"], str) and row["owner"]["id"]
    assert isinstance(row["rationale"], str) and row["rationale"]

required = {
    "RC2.REVIEW_CALLBACK_INGESTION_FINALIZING": "fixed-rc2",
    "RC2.SLICE_RECEIPT_PROVENANCE": "fixed-rc2",
    "RC2.AUTHENTICATED_TURN_COMPLETE_ADAPTER": "defer-post-rc2",
    "RC2.CMUX_PROVIDER_START_HANDSHAKE": "defer-post-rc2",
    "eng-store-private-writers": "fixed-rc2",
    "intent-e9-tdd-skill-verdict-missing": "defer-post-rc2",
    "RC2.CLASSIC_CMUX_CONTOUR": "deleted-rc2",
    "RC2.VAULT_REPAIR": "implemented-rc2",
    "RC2.REVIEW_APPROVAL_CRASH_GAP": "fixed-rc2",
    "RC2.IMPLEMENTATION_AUTHORITY_OPAQUE_EVIDENCE": "fixed-rc2",
    "RC2.EXACT_CANDIDATE_SECRET_CHECK": "implemented-rc2",
    "RC2.REPLACEMENT_CANDIDATE_CEILING": "accepted-deviation-rc2",
    "RC2.SWARM_PLAN_STATUS_PROOF": "accepted-deviation-rc2",
}
assert {finding: by_id[finding]["disposition"] for finding in required} == required
assert by_id["RC2.CMUX_PROVIDER_START_HANDSHAKE"]["owner"] == {
    "kind": "component",
    "id": "harness.runtime_provider",
}

deviations = json.loads(DEVIATIONS.read_text(encoding="utf-8"))
assert deviations["schema_version"] == 1
assert deviations["release"] == "2.6.6-rc2"
assert {row["id"] for row in deviations["deviations"]} == {
    "D-266-RC2-L4-01",
    "D-266-RC2-E7-01",
}
for row in deviations["deviations"]:
    assert row["status"] == "accepted"
    assert row["finding_ids"]
    assert all(
        by_id[finding]["disposition"] == "accepted-deviation-rc2"
        for finding in row["finding_ids"]
    )

allowlist = ledger["deletion_allowlist"]
assert allowlist["subject_sha"] == BASE_SHA
assert allowlist["required_zero_caller"] == list(CLASSIC)
assert allowlist["conditional_zero_caller"] == [CONDITIONAL]
for relative in (*CLASSIC, CONDITIONAL):
    assert not (ROOT / relative).exists()
    assert production_importers(module_name(relative)) == []

removed_lines = 0
for relative in (*CLASSIC, CONDITIONAL):
    shown = subprocess.run(
        ["git", "show", f"{BASE_SHA}:{relative}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    removed_lines += len(shown.stdout.splitlines())
assert removed_lines == 2217

current_scripts = tuple(sorted((ROOT / "scripts").rglob("*.py")))
assert len(current_scripts) < 259
assert sum(len(path.read_text(encoding="utf-8").splitlines()) for path in current_scripts) < 85431

tdd_verdict = json.loads(
    (ROOT / "docs/acceptance/v2.6.6-rc2-tdd-verdict.json").read_text(
        encoding="utf-8"
    )
)
assert tdd_verdict == {
    "schema_version": 1,
    "finding_id": "intent-e9-tdd-skill-verdict-missing",
    "disposition": "defer-post-rc2",
    "semantic_skill_change": False,
    "owner": {"kind": "governance", "id": "skills.tdd"},
    "evidence": [
        "tests/test_skill_workstream_b.py",
        "docs/acceptance/v2.6.6-rc2-defect-ledger.json",
    ],
    "rationale": "Existing TDD semantics are green; RC2 adds exact release evidence without changing the skill contract.",
}

for retained in (
    "scripts/cmux_agent_support.py",
    "scripts/cmux_surface_lifecycle.py",
    "scripts/cmux_workspace_lifecycle.py",
    "scripts/task_sessions.py",
    "scripts/harness/adapters/cmux.py",
    "scripts/harness/runtime_sessions.py",
):
    assert (ROOT / retained).is_file()

print("RC2 frozen scope and deletion topology contracts passed")
