#!/usr/bin/env python3
"""Active Harness lifecycle contracts after classic contour removal."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cmux_surface_lifecycle import prepared_reap_plan  # noqa: E402
from harness.prompts import classify  # noqa: E402


REMOVED = (
    "cmux_agent_supervisor.py",
    "cmux_supervisor_policy.py",
    "cmux_supervisor_review.py",
    "cmux_supervisor_contracts.py",
    "cmux_task_watchdog.py",
    "cmux_trust_prompt.py",
    "archive_task_reviews.py",
)
RETAINED = (
    "cmux_agent_support.py",
    "cmux_surface_lifecycle.py",
    "cmux_workspace_lifecycle.py",
    "task_sessions.py",
    "harness/runtime_sessions.py",
    "harness/adapters/cmux.py",
    "harness/adapters/codex.py",
    "harness/adapters/process.py",
)

assert all(not (ROOT / "scripts" / path).exists() for path in REMOVED)
assert all((ROOT / "scripts" / path).is_file() for path in RETAINED)

shared_plan = "---\nstatus: pending\n---\n# Shared plan\n"
assert prepared_reap_plan(
    {"reap_policy": {"mode": "shared"}},
    shared_plan,
    today="2026-08-07",
    result_link="[[Shared task result]]",
    exec_session="executor-session",
    label="wiki/plans/shared.md",
) == shared_plan

claude = (
    "Accessing workspace:\nQuick safety check: Is this a project you created "
    "or one you trust?\n1. Yes, I trust this folder\n2. No, exit\n"
    "Enter to confirm · Esc to cancel\n"
)
codex = (
    "Do you trust the contents of this directory?\n1. Yes, continue\n"
    "2. No, quit\nPress enter to continue\n"
)
assert classify("claude", claude).recognized
assert classify("codex", codex).recognized
assert not classify(
    "claude", claude.replace("Quick safety check:", "Safety check:")
).recognized

lint = subprocess.run(
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
inventory = json.loads(lint.stdout)
assert lint.returncode == 0, inventory
assert inventory["direct_callers"] == {}
assert inventory["lifecycle_seam_violations"] == {}

print("active unattended task lifecycle contracts passed")
