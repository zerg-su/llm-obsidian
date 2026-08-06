#!/usr/bin/env python3
"""Schema contracts for the 2.6.6 RC1 incremental evidence artifacts."""

from __future__ import annotations

import json
import hashlib
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

dogfood = load("v2.6.6-rc1-real-dogfood.json")
assert dogfood.get("release") == "2.6.6-rc1"
dogfood_head = dogfood.get("subject_head_sha")
assert dogfood_head == "b313632e285b13096e4c2692393cf16610aecd2a"
assert subprocess.run(
    ["git", "merge-base", "--is-ancestor", str(dogfood_head), "HEAD"],
    cwd=ROOT,
    check=False,
).returncode == 0
observations = dogfood.get("observations")
assert isinstance(observations, dict)
assert observations == {
    "provider_typed_artifact_count": 2,
    "accepted_receipt_count": 2,
    "callback_loss_count": 0,
    "manual_callback_write_count": 0,
    "repeated_review_count": 0,
    "reviewer_relaunch_count": 0,
    "terminal_resources_owned": False,
}
callback_receipts = dogfood.get("accepted_callback_receipts")
assert isinstance(callback_receipts, list) and len(callback_receipts) == 2
for callback in callback_receipts:
    assert isinstance(callback, dict)
    assert callback.get("status") == "accepted"
    assert isinstance(callback.get("callback_id"), str)
    assert re.fullmatch(r"review-[0-9a-f]{24}", callback["callback_id"])
    assert isinstance(callback.get("payload_sha256"), str)
    assert re.fullmatch(r"[0-9a-f]{64}", callback["payload_sha256"])
terminal_inventory = dogfood.get("terminal_resource_inventory")
assert isinstance(terminal_inventory, list) and len(terminal_inventory) == 6
assert all(
    isinstance(row, dict)
    and row.get("state") in {"complete", "cancelled"}
    and row.get("terminal") is True
    and row.get("resources_owned") is False
    for row in terminal_inventory
)

dogfood_manifest_path = ROOT / str(dogfood.get("evidence_manifest") or "")
assert dogfood_manifest_path.is_file()
dogfood_manifest = json.loads(dogfood_manifest_path.read_text(encoding="utf-8"))
assert dogfood_manifest.get("schema_version") == 1
assert dogfood_manifest.get("subject_head_sha") == dogfood_head
assert dogfood_manifest.get("owner_id") == dogfood.get("owner_id")
assert dogfood_manifest.get("review_attempt_id") == dogfood.get("review_attempt_id")
dogfood_evidence_root = dogfood_manifest_path.parent
manifest_rows = dogfood_manifest.get("artifacts")
assert isinstance(manifest_rows, list) and len(manifest_rows) == 14
manifest_payloads: dict[str, dict[str, object]] = {}
for row in manifest_rows:
    assert isinstance(row, dict)
    relative = row.get("path")
    digest = row.get("sha256")
    assert isinstance(relative, str) and relative == Path(relative).name
    assert isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
    artifact_path = dogfood_evidence_root / relative
    raw = artifact_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == digest
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    manifest_payloads[relative] = payload

for axis in ("openai-intent", "openai-engineering"):
    callback = manifest_payloads[f"callback-{axis}.json"]
    receipt = manifest_payloads[f"callback-receipt-{axis}.json"]
    summary = next(row for row in callback_receipts if row["axis"] == axis)
    assert callback.get("callback_id") == receipt.get("callback_id") == summary["callback_id"]
    assert callback.get("payload_sha256") == receipt.get("payload_sha256") == summary["payload_sha256"]
    assert receipt.get("status") == "accepted"

archived_operations = {
    payload["spec"]["operation_id"]: payload
    for name, payload in manifest_payloads.items()
    if name.startswith("operation-")
}
assert set(archived_operations) == {
    row["operation_id"] for row in terminal_inventory
}
for row in terminal_inventory:
    operation = archived_operations[row["operation_id"]]
    assert operation["state"] == row["state"]
    assert operation["revision"] == row["revision"]
    assert operation["resources"] == {
        "process_group": 0,
        "process_identity": "",
        "supervisor_identity": "",
        "supervisor_pid": 0,
        "surface_id": "",
    }

quality_baseline = json.loads(
    (ROOT / "config" / "code-quality-baseline.json").read_text(
        encoding="utf-8"
    )
)["rc1_active_authority"]
baseline_subject = quality_baseline.get("baseline_subject_sha")
assert isinstance(baseline_subject, str) and SHA.fullmatch(baseline_subject)
assert subprocess.run(
    ["git", "cat-file", "-e", f"{baseline_subject}^{{commit}}"],
    cwd=ROOT,
    check=False,
).returncode == 0
for entry in slices:
    assert subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            baseline_subject,
            str(entry["commit_sha"]),
        ],
        cwd=ROOT,
        check=False,
    ).returncode == 0
baseline_run = subprocess.run(
    [
        "python3",
        "scripts/code-quality-audit.py",
        "--rc1-authority-json",
        "--rc1-authority-subject",
        baseline_subject,
    ],
    cwd=ROOT,
    check=True,
    capture_output=True,
    text=True,
)
baseline_audit = json.loads(baseline_run.stdout)
assert baseline_audit["production_loc"] == quality_baseline[
    "baseline_production_loc"
]
assert len(baseline_audit["production_files"]) == quality_baseline[
    "baseline_production_file_count"
]
assert len(baseline_audit["writable_authorities"]) == quality_baseline[
    "baseline_writable_authorities"
]
assert len(baseline_audit["incident_literals"]) == quality_baseline[
    "baseline_incident_literals"
]
literal_classes: dict[str, int] = {}
for item in baseline_audit["incident_literals"]:
    literal_classes[item["kind"]] = literal_classes.get(item["kind"], 0) + 1
assert literal_classes == quality_baseline["baseline_incident_literal_classes"]

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
    "ENG.EXACT_HEAD.STALE_APPROVAL",
    "ENG.EVENT.STOP_SYNTHESIS",
    "ENG.E1.INVALID_BASELINE_OID",
    "OI-E1-BASELINE-SUBJECT",
    "OI-E7-FRESH-DOGFOOD-MISSING",
    "OI-E8-EXACT-HEAD-DRIFT",
    "ENG.E3.TURN_STOP_UNREACHABLE",
    "OI.E6.UNBOUND-SLICE-RECEIPTS",
    "OI.E7.UNBOUND-DOGFOOD-REPORT",
    "OI.E8.CANDIDATE-BUDGET-EXCEEDED",
    "ENG.SPLIT.BASE_REQUEST_DRIFT",
    "ENG.SPLIT.REPLAY.BASE_SHA_MISSING",
    "ENG.SPLIT.LAUNCH_RECEIPT_BASE_OMITTED",
    "ENG.SPLIT.RECOVERED_BASE_ASSUMED",
    "TEST.SPLIT.REPLAY_FIXTURE_INVALID_REQUEST",
    "ENG.SPLIT.DUPLICATE_LAUNCH_GUARD",
    "OI.E6.RECEIPTS_NOT_ESTABLISHED",
    "OI.E8.CANDIDATE_LEDGER_STALE",
    "OI.E8.RECEIPT_NOT_BOUND_IN_BOUNDARY",
}
for row in finding_rows:
    assert isinstance(row, dict)
    assert row.get("disposition") in {
        "fixed-rc1",
        "defer-rc2",
        "out-of-scope-2.7",
        "not-a-defect",
        "blocks-rc1",
        "accepted-deviation",
    }
    assert isinstance(row.get("rationale"), str) and row["rationale"].strip()

deviations = load("v2.6.6-rc1-accepted-deviations.json")
assert deviations.get("release") == "2.6.6-rc1"
deviation_rows = deviations.get("deviations")
assert isinstance(deviation_rows, list) and len(deviation_rows) == 1
deviation = deviation_rows[0]
assert isinstance(deviation, dict)
assert deviation.get("id") == "D-266-RC1-E6-01"
assert deviation.get("status") == "accepted"
assert deviation.get("authority") == "approved-rc1-closure-plan-section-13"
assert deviation.get("affected_slices") == list("ABCDEFG")
assert deviation.get("finding_ids") == [
    "OI.E6.UNBOUND-SLICE-RECEIPTS",
    "OI.E6.RECEIPTS_NOT_ESTABLISHED",
]
repair_heads = deviation.get("affected_repair_heads")
assert isinstance(repair_heads, list) and repair_heads
for repair_head in repair_heads:
    assert isinstance(repair_head, str) and SHA.fullmatch(repair_head)
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", repair_head, "HEAD"],
        cwd=ROOT,
        check=False,
    ).returncode == 0
assert "not independently generated immutable RED/GREEN receipts" in str(
    deviation.get("constraint")
)
assert "RC2.SLICE_RECEIPT_PROVENANCE" in str(deviation.get("remediation"))

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
assert {row["finding_id"] for row in ledger_rows} >= {
    "RC2.REVIEW_CALLBACK_INGESTION_FINALIZING",
    "RC2.SLICE_RECEIPT_PROVENANCE",
    "RC2.AUTHENTICATED_TURN_COMPLETE_ADAPTER",
}

failed = load("evidence/v2.6.6-rc1/failed-fbf87a4/diagnostic-receipt.json")
assert failed.get("type") == "diagnostic-only"
assert failed.get("evidence_disposition") == "not-verification-evidence"
assert failed.get("status") == "failed"
assert failed.get("subject_head_sha") == "fbf87a4e8ef532b43e4d55225c87ad0f39f55bd9"
assert failed.get("command_id") == "harness-coverage"
assert failed.get("command_index") == 2

historical_passed = load("evidence/v2.6.6-rc1/replacement-126b5fe/receipt.json")
assert historical_passed.get("status") == "passed"
assert historical_passed.get("profile") == "release-final"
assert historical_passed.get("execution_relation") == "release-candidate"
assert historical_passed.get("subject_head_sha") == "126b5fecb087a231bd6fbec8ce3f5dfe9235a206"
commands = historical_passed.get("commands")
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

readiness = (ACCEPTANCE / "v2.6.6-rc1-release-readiness.md").read_text(
    encoding="utf-8"
)
assert "superseded historical gate" in readiness
assert "owner-controlled exact-HEAD sidecar" in readiness
assert "v2.6.6-rc1-real-dogfood.json" in readiness
assert "RC2.REVIEW_CALLBACK_INGESTION_FINALIZING" in readiness
assert "authoritative technical candidate is exact clean HEAD\n`126b5fe" not in readiness

release_version = "2.6.6-rc1"
claude_plugin = json.loads(
    (ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8")
)
codex_plugin = json.loads(
    (ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
)
marketplace = json.loads(
    (ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
)
assert claude_plugin["version"] == release_version
assert codex_plugin["version"] == release_version
assert marketplace["metadata"]["version"] == release_version
assert marketplace["plugins"][0]["version"] == release_version
for relative_path in (
    "CHANGELOG.md",
    "CHANGELOG.ru.md",
    "README.md",
    "README.ru.md",
    "docs/releases/v2.6.6-rc1.md",
):
    assert release_version in (ROOT / relative_path).read_text(encoding="utf-8")

print("2.6.6 RC1 evidence schemas passed")
