#!/usr/bin/env python3
"""RC3 English-plan and executable documentation truth contracts."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


implementation = text("skills/implementation-plan/SKILL.md")
save_plan = text("skills/save-plan/SKILL.md")
canonical_language_rule = (
    "Write normative plan prose in English unless the user explicitly requests "
    "another language."
)
normalized_skills = " ".join((implementation + save_plan).split())
assert normalized_skills.count(canonical_language_rule) == 1
normalized_implementation = " ".join(implementation.split())
for required in (
    "Outcome Contract",
    "goal",
    "evidence identifiers",
    "slice definitions",
    "stop rules",
    "verification instructions",
    "user-facing conversation",
):
    assert required in normalized_implementation
assert "../implementation-plan/SKILL.md" in save_plan
assert "before metadata" in save_plan.casefold()

documentation = {
    relative: text(relative)
    for relative in (
        "README.md",
        "README.ru.md",
        "docs/runtime-capabilities.md",
        "docs/task-sessions.md",
        "docs/unattended-pipeline-operations.md",
    )
}
for stale in (
    "The supervisor closes that exact surface",
    "Supervisor закрывает точную surface",
    "Dispatch, review, watchdog, and close",
    "Dispatch, review, watchdog, close",
    "Its supervisor validates",
    "launches the supervisor",
    "pre-supervisor failure",
    "watchdog locks",
    "supervisor-generated",
    "watchdog state",
):
    assert all(stale not in body for body in documentation.values()), stale

assert "lifecycle wrapper requests graceful agent exit" in documentation["README.md"]
assert "lifecycle wrapper запрашивает graceful exit" in documentation[
    "README.ru.md"
].casefold()
assert "provider worker validates and delivers" in documentation[
    "docs/unattended-pipeline-operations.md"
]
assert "harness-generated" in documentation["docs/runtime-capabilities.md"]
assert "liveness state" in documentation["docs/runtime-capabilities.md"]
assert "launches the runtime worker" in documentation["docs/task-sessions.md"]

upgrade = text("docs/ru/upgrading-and-releasing.md")
for command in (
    "python3 scripts/rc3_inventory.py build",
    "--expected-candidate",
    "--attempt-ledger-root",
    "python3 scripts/rc3_attempt_ledger.py authorize-extension",
    "python3 scripts/rc3_release_disposition.py check",
):
    assert command in upgrade
assert "--accepted-deviations" in upgrade
release_check = next(
    line
    for line in upgrade.splitlines()
    if line.startswith("python3 scripts/rc3_release_disposition.py check ")
)
release_check_args = shlex.split(release_check)
for required in (
    "--review-boundary",
    "--plan",
    "--outcome-evidence",
    "--accepted-deviations",
):
    assert required in release_check_args, required
assert "восемь полных попыток" in upgrade
assert "шестая попытка запрещена" not in upgrade

changelogs = text("CHANGELOG.md") + text("CHANGELOG.ru.md")
assert "eight-attempt ceiling" in changelogs
assert "восьми candidate" in changelogs
assert "five-attempt ceiling" not in changelogs
assert "лимит из пяти candidate attempts" not in changelogs

evidence_contract = text("docs/acceptance/v2.6.6-rc3-evidence-contract.md")
for number in range(1, 10):
    assert f"RC3-E{number}-" in evidence_contract
for relative in (
    "scripts/rc3_inventory.py",
    "scripts/rc3_slice_receipt.py",
    "scripts/rc3_coverage.py",
    "scripts/rc3_release_disposition.py",
    "schemas/rc3-release-disposition-v1.schema.json",
):
    assert (ROOT / relative).is_file()

assert not (ROOT / "docs/acceptance/v2.6.6-rc3-machine-inventory.json").exists()
assert "external sidecar" in evidence_contract
assert "not stored in the candidate tree" in evidence_contract

method = json.loads(text("docs/acceptance/v2.6.6-rc3-skill-creator-method.json"))
assert method["order"] == 1
assert {row["skill"] for row in method["skills"]} == {
    "implementation-plan",
    "save-plan",
}
verdict = subprocess.run(
    [
        "python3",
        "skills/improve-skills/scripts/audit_skills.py",
        "--verdicts",
        "docs/acceptance/v2.6.6-rc3-skill-verdicts.json",
        "--scope",
        "implementation-plan",
        "--scope",
        "save-plan",
        "--strict",
    ],
    cwd=ROOT,
    check=False,
    capture_output=True,
    text=True,
)
assert verdict.returncode == 0, verdict.stdout + verdict.stderr

version_carriers = {
    relative: json.loads(text(relative))
    for relative in (
        ".claude-plugin/plugin.json",
        ".claude-plugin/marketplace.json",
        ".codex-plugin/plugin.json",
    )
}
assert version_carriers[".claude-plugin/plugin.json"]["version"] == "2.6.6-rc3"
assert version_carriers[".codex-plugin/plugin.json"]["version"] == "2.6.6-rc3"
marketplace = version_carriers[".claude-plugin/marketplace.json"]
assert marketplace["metadata"]["version"] == "2.6.6-rc3"
assert marketplace["plugins"][0]["version"] == "2.6.6-rc3"
assert json.loads(text(".agents/plugins/marketplace.json"))["name"] == (
    "llm-obsidian-codex"
)

assert "## [2.6.6-rc3] - 2026-08-07" in text("CHANGELOG.md")
assert "## [2.6.6-rc3] — 2026-08-07" in text("CHANGELOG.ru.md")
for relative in ("README.md", "README.ru.md"):
    assert "docs/releases/v2.6.6-rc3.md" in text(relative)

release_notes = text("docs/releases/v2.6.6-rc3.md")
readiness = text("docs/acceptance/v2.6.6-rc3-release-readiness.md")
for evidence_number in range(1, 10):
    assert f"RC3-E{evidence_number}-" in readiness
for required in (
    "set rc3_branch task/llm-obsidian-2-6-6-rc3-release",
    "git merge --ff-only $rc3_branch",
    "git tag -a $rc3_tag",
    "gh release create $rc3_tag --prerelease",
):
    assert required in readiness
assert "does not push, tag, publish, install, or merge" in " ".join(
    release_notes.split()
)

makefile = text("Makefile")
assert "test-code-quality:\n" in makefile
assert "python3 tests/test_rc3_release_evidence.py" in makefile
assert "test-docs:\n" in makefile
assert "python3 tests/test_rc3_documentation.py" in makefile

print("RC3 documentation and English-plan contracts passed")
