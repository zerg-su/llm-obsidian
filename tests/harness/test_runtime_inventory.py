#!/usr/bin/env python3
"""The frozen runtime caller inventory stays complete after the clean cut."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FROZEN = (
    "scripts/cmux_agent_support.py",
    "scripts/cmux_agent_supervisor.py",
    "scripts/cmux_supervisor_contracts.py",
    "scripts/cmux_surface_lifecycle.py",
    "scripts/cmux_workspace_lifecycle.py",
    "scripts/cmux_task_watchdog.py",
    "scripts/task_sessions.py",
    "scripts/dispatch-runner.py",
    "scripts/reap-runner.py",
    "scripts/research-isolation.py",
    "skills/review-dispatch/scripts/spawn_review.py",
    "skills/reap-send/scripts/send_reap.py",
    "skills/review-send/scripts/send_review.py",
    "skills/review-dispatch/scripts/archive_review.py",
    "scripts/cmux_trust_prompt.py",
    "scripts/queue-session-exit.py",
    "scripts/task_escalation.py",
    "scripts/acceptance-workspace-supervisor.py",
    "scripts/release-acceptance.py",
)

result = subprocess.run(
    [
        sys.executable,
        str(ROOT / "scripts/runtime-harness-lint.py"),
        "--strict",
        "--json",
    ],
    cwd=ROOT,
    text=True,
    capture_output=True,
    check=False,
)
value = json.loads(result.stdout)
assert result.returncode == 0, value
assert value["direct_callers"] == {}
assert value["stale_allowlist"] == {}

document = (
    ROOT / "docs/runtime-harness-migration.md"
).read_text(encoding="utf-8")
for caller in FROZEN:
    assert f"`{caller}`" in document, caller
for destination in (
    "adapters.cmux",
    "adapters.claude",
    "adapters.codex",
    "adapters.process",
    "workflows.dispatch",
    "workflows.reap",
    "workflows.research",
    "workflows.review",
):
    assert destination in document, destination
print(f"OK   frozen runtime inventory covers {len(FROZEN)} exact callers")
