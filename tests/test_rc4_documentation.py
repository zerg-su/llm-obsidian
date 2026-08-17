#!/usr/bin/env python3
"""Executable RC4 packaging contract with immutable RC3 acceptance evidence."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION = "2.6.7-rc4"
HISTORICAL_VERSION = "2.6.7-rc3"
SUBJECT = "e0b419fb2a97dde3c4bc321ec170f93997a0063781822caf5d9d81347db9bfd6"


def text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


# The current packaged release moved past RC4; the authoritative
# current-release packaging gate lives in tests/test_v266_rc1_evidence.py.
# This contract keeps only the immutable historical RC4 evidence below.

assert "## [2.6.7-rc4] - 2026-08-11" in text("CHANGELOG.md")
assert "## [2.6.7-rc4] — 2026-08-11" in text("CHANGELOG.ru.md")
for relative in ("README.md", "README.ru.md"):
    assert "docs/releases/v2.6.7-rc4.md" in text(relative)

historical_notes = text("docs/releases/v2.6.7-rc3.md")
normalized_historical = " ".join(historical_notes.split()).lower()
notes = text("docs/releases/v2.6.7-rc4.md")
normalized_notes = " ".join(notes.split()).lower()
for required in (
    "terminal-only",
    "root-scoped",
    "human-readable",
    "task-name-first",
    "owner-wide diagnostic",
    "user-owned",
    "--no-color",
    "durable timestamps",
    "time unavailable",
    "ancestor symlink",
    "gate, head, axes, lane, run, and attempt",
    "display-only",
    "no lifecycle authority",
    "independent review",
    "not tagged or published",
):
    assert required in normalized_notes
for forbidden_claim in (
    "this release is published",
    "published as the prerelease",
    "tagged as v2.6.7-rc4",
    "was pushed",
    "was merged",
    "has been released",
):
    assert forbidden_claim not in normalized_notes

observability = " ".join(text("docs/pipeline-observability.md").split()).lower()
runtime = " ".join(text("docs/runtime-capabilities.md").split()).lower()
for required in (
    "root elapsed",
    "terminal duration",
    "durable timestamps",
    "time unavailable",
    "ancestor symlink",
    "exact gate, reviewed head, axes, lane, run, and attempt",
    "best-effort telemetry",
):
    assert required in observability
for required in (
    "root-scoped terminal observer",
    "task-name-first",
    "owner-wide diagnostic",
    "diagnostic-only fields",
    "user-owned",
    "--no-color",
    "no lifecycle authority",
):
    assert required in runtime
readiness = text("docs/acceptance/v2.6.6-rc4-release-readiness.md")
for evidence_number in range(1, 11):
    assert f"RC4-E{evidence_number}-" in readiness
for required in (
    "task/llm-obsidian-2-6-6-rc4-join",
    "git merge --ff-only $rc4_branch",
    "git tag -a $rc4_tag",
    "gh release create $rc4_tag --prerelease",
):
    assert required in readiness
assert "single holistic Opus" in readiness
assert "final stabilization candidate" in normalized_historical
assert "three consecutive rc1 cells" in normalized_historical
assert "distinct concurrent roots" in normalized_historical
assert "observer splits remain external and user-owned" in normalized_historical
assert "published as the prerelease tag" in normalized_historical

evidence_root = ROOT / "docs" / "acceptance" / "evidence" / "v2.6.7"


def evidence(name: str, evidence_id: str) -> dict[str, object]:
    value = json.loads((evidence_root / name).read_text(encoding="utf-8"))
    assert value["schema_version"] == 1
    assert value["release"] == HISTORICAL_VERSION
    assert value["evidence_id"] == evidence_id
    assert value["lifecycle_subject_sha256"] == SUBJECT
    return value


streak = evidence("rc1-live-streak-receipt.json", "E267.RC1.LIVE_STREAK")
assert streak["complete"] is True
assert streak["streak"] == 3
assert streak["coordinator_recovery_count"] == 0
cells = streak["cells"]
assert isinstance(cells, list) and len(cells) == 3
assert [cell["sequence"] for cell in cells] == [1, 2, 3]
assert all(cell["result"] == "success" for cell in cells)
assert all(cell["resource_free"] is True for cell in cells)
assert sum(cell["material_cycle"] is True for cell in cells) >= 1
packaging = streak["packaging_boundary"]
assert packaging["live_subject_is_pre_packaging"] is True
assert packaging["runtime_code_changed"] is False
assert set(packaging["post_live_subject_paths_changed"]) == {
    ".claude-plugin/marketplace.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
}

sequential = evidence("rc3-sequential-receipt.json", "E267.RC3.SEQUENTIAL")
assert sequential["terminal_state"] == "complete"
assert sequential["terminal_effect"] == "request-exit/succeeded"
assert sequential["resource_free"] is True
assert sequential["material_cycle"] is True
assert sequential["observer"] == {
    "root_scoped": True,
    "opened_before_provider_start": True,
    "harness_owned": False,
    "user_closed": True,
}

parallel = evidence("rc3-parallel-receipt.json", "E267.RC3.PARALLEL")
assert parallel["concurrent"] is True
assert 0 <= parallel["launch_deadline_delta_seconds"] < 1
roots = parallel["roots"]
assert isinstance(roots, list) and len(roots) == 2
for key in (
    "request_id",
    "run_id",
    "worktree",
    "task_surface_id",
    "dashboard_surface_id",
):
    assert len({root[key] for root in roots}) == 2
assert {root["executor"].split("/", 1)[0] for root in roots} == {"claude", "codex"}
assert {root["review"].split("/", 1)[0] for root in roots} == {"claude", "codex"}
assert all(root["terminal_state"] == "complete" for root in roots)
assert all(root["resource_free"] is True for root in roots)

cleanup = evidence("rc3-cleanup-proof.json", "E267.RC3.CLEANUP")
assert cleanup["resource_free"] is True
assert cleanup["user_owned_observers_excluded"] is True
inventory = cleanup["inventory"]
assert isinstance(inventory, list) and len(inventory) == 3
assert all(row["state"] == "complete" for row in inventory)
assert all(row["pending_effect"] == "" for row in inventory)
assert all(row["process_group"] == row["supervisor_pid"] == 0 for row in inventory)
assert all(row["surface_id"] == "" for row in inventory)
assert all(value == 0 for value in cleanup["assertions"].values())

repair_ledger = json.loads(
    (evidence_root / "rc3-repair-classification-ledger.json").read_text(
        encoding="utf-8"
    )
)
assert repair_ledger["schema_version"] == 1
assert repair_ledger["release"] == HISTORICAL_VERSION
assert repair_ledger["evidence_id"] == "E267.RC3.REPAIR_CLASSIFICATION"
assert repair_ledger["stop_rule"] == {
    "independent_new_lifecycle_class_count": 0,
    "limit": 3,
    "verdict": "within-limit",
}
classes = repair_ledger["classes"]
assert isinstance(classes, list) and len(classes) == 4
assert all(row["counts_against_release_stop"] is False for row in classes)
classified_commits = {
    commit for row in classes for commit in row["commits"]
}
program_digest = json.loads(
    (evidence_root / "rc3-program-digest.json").read_text(encoding="utf-8")
)
assert classified_commits == {
    row["commit"] for row in program_digest["bound_repairs"]
}

final_review = json.loads(
    (evidence_root / "rc3-final-fable-review.json").read_text(encoding="utf-8")
)
assert final_review["schema_version"] == 1
assert final_review["release"] == HISTORICAL_VERSION
assert final_review["evidence_id"] == "E267.RELEASE.FABLE_REVIEW"
assert final_review["reviewed_head_sha"] == "9b12e3453a6cd81da7361e32b7cc60aa3c3187d0"
assert final_review["verdict"] == "approve"
assert all(row["severity"] == "minor" for row in final_review["findings"])
assert {row["disposition"] for row in final_review["findings"]} == {
    "accepted-follow-up",
    "resolved-evidence-only",
    "accepted-known-mechanism",
}

release_gate = json.loads(
    (evidence_root / "rc3-release-gate.json").read_text(encoding="utf-8")
)
assert release_gate["schema_version"] == 1
assert release_gate["release"] == HISTORICAL_VERSION
assert release_gate["evidence_id"] == "E267.RELEASE.GATE"
assert release_gate["verdict"] == "green"
assert release_gate["attested_candidate_head_sha"] == final_review["reviewed_head_sha"]
assert release_gate["live_lifecycle_subject_sha256"] == SUBJECT
assert release_gate["review"]["verdict"] == "approve"
assert release_gate["review"]["material_finding_count"] == 0
assert release_gate["review"]["unexplained_deviation_count"] == 0
assert all(value == "pass" for value in release_gate["verification"].values())

makefile = text("Makefile")
assert "python3 tests/test_rc4_documentation.py" in makefile

print("v2.6.7 RC4 packaging and immutable RC3 evidence contracts passed")
