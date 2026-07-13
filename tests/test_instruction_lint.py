#!/usr/bin/env python3
"""Tests for canonical pipeline instruction drift linting."""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lint-instructions.py"
spec = importlib.util.spec_from_file_location("instruction_lint_test", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

assert module.check_repo(ROOT) == []
print("OK   repository instructions align")
bad = "---\nname: autoresearch\nallowed-tools: Read WebSearch WebFetch\n---\n"
assert module.protected_tool_issues("autoresearch", bad)
print("OK   protected web-tool regression detected")

bad_writer = "---\nname: daily\nallowed-tools: Read Write Edit Bash\n---\nmkdir -p \"$DIR\"\nwrite lines under `## Сделано`\n"
issues = module.writer_path_issues("daily", bad_writer)
assert any("Write/Edit" in issue for issue in issues)
assert any("vault-write.py" in issue for issue in issues)
assert any("direct wiki" in issue for issue in issues)
print("OK   direct wiki mutation regression detected")

bad_daily = "On Claude or when that custom agent is unavailable, produce the same JSON in the parent"
issues = module.daily_runtime_issues(bad_daily)
assert any("Agent tool" in issue for issue in issues)
assert any("runtime invariant" in issue for issue in issues)
assert any("parent fallback" in issue for issue in issues)
assert any("detect-runtime.sh --three-way" in issue for issue in issues)
print("OK   Claude subscription fallback regression detected")

with tempfile.TemporaryDirectory(prefix="instruction-lint-test.") as raw:
    assert module.daily_runtime_repo_issues(Path(raw)) == []
print("OK   missing daily skill handled without traceback")

issues = module.failure_repair_issues("", "", "", "", "")
assert any("CLAUDE.md" in issue for issue in issues)
assert any("AGENTS.md" in issue for issue in issues)
assert any("dispatch task prompt" in issue for issue in issues)
assert any("mechanism-failure category" in issue for issue in issues)
assert any("reference missing" in issue for issue in issues)
print("OK   failure-repair auto-repair boundary drift detected")

print("\nAll instruction lint tests passed.")
