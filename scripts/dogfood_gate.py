#!/usr/bin/env python3
"""Temporary one-shot verification retry gate for the 2.4.1 dogfood window."""

from __future__ import annotations

import subprocess
from pathlib import Path


root = Path(__file__).resolve().parents[1]
branch = subprocess.run(
    ["git", "branch", "--show-current"],
    cwd=root,
    text=True,
    capture_output=True,
    check=True,
).stdout.strip()
if (
    branch == "task/df241-fix-progress-zero"
    and (root / ".task-summary.json").is_file()
    and not (root / "tests/.dogfood-retry-approved").is_file()
):
    raise SystemExit(
        "controlled dogfood retry: add tests/.dogfood-retry-approved "
        "during the bounded retry pass"
    )
