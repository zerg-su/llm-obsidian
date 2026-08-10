#!/usr/bin/env python3
"""Executable documentation, packaging, and live evidence contract for RC3."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION = "2.6.7-rc3"
SUBJECT = "e0b419fb2a97dde3c4bc321ec170f93997a0063781822caf5d9d81347db9bfd6"


def text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


claude = json.loads(text(".claude-plugin/plugin.json"))
codex = json.loads(text(".codex-plugin/plugin.json"))
marketplace = json.loads(text(".claude-plugin/marketplace.json"))
assert claude["version"] == VERSION
assert codex["version"] == VERSION
assert marketplace["metadata"]["version"] == VERSION
assert marketplace["plugins"][0]["version"] == VERSION

assert "## [2.6.7-rc3] - 2026-08-11" in text("CHANGELOG.md")
assert "## [2.6.7-rc3] — 2026-08-11" in text("CHANGELOG.ru.md")
for relative in ("README.md", "README.ru.md"):
    assert "docs/releases/v2.6.7-rc3.md" in text(relative)

notes = text("docs/releases/v2.6.7-rc3.md")
normalized_notes = " ".join(notes.split()).lower()
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
assert "final stabilization candidate" in normalized_notes
assert "three consecutive rc1 cells" in normalized_notes
assert "distinct concurrent roots" in normalized_notes
assert "observer splits remain external and user-owned" in normalized_notes
assert "published as the prerelease tag" in normalized_notes

evidence_root = ROOT / "docs" / "acceptance" / "evidence" / "v2.6.7"


def evidence(name: str, evidence_id: str) -> dict[str, object]:
    value = json.loads((evidence_root / name).read_text(encoding="utf-8"))
    assert value["schema_version"] == 1
    assert value["release"] == VERSION
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

makefile = text("Makefile")
assert "python3 tests/test_rc4_documentation.py" in makefile

print("v2.6.7 RC3 documentation, packaging, and live evidence contracts passed")
