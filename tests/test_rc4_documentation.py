#!/usr/bin/env python3
"""Executable documentation and version contract for v2.6.6 RC4."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION = "2.6.6-rc4"


def text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


claude = json.loads(text(".claude-plugin/plugin.json"))
codex = json.loads(text(".codex-plugin/plugin.json"))
marketplace = json.loads(text(".claude-plugin/marketplace.json"))
assert claude["version"] == VERSION
assert codex["version"] == VERSION
assert marketplace["metadata"]["version"] == VERSION
assert marketplace["plugins"][0]["version"] == VERSION

assert "## [2.6.6-rc4] - 2026-08-08" in text("CHANGELOG.md")
assert "## [2.6.6-rc4] — 2026-08-08" in text("CHANGELOG.ru.md")
for relative in ("README.md", "README.ru.md"):
    assert "docs/releases/v2.6.6-rc4.md" in text(relative)

notes = text("docs/releases/v2.6.6-rc4.md")
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
assert "does not push, tag, publish, install, or merge" in " ".join(
    notes.split()
)
assert "RC4 transition certificate" in notes
assert "six-part review denominator" in notes

makefile = text("Makefile")
assert "python3 tests/test_rc4_documentation.py" in makefile

print("RC4 documentation and release contracts passed")
