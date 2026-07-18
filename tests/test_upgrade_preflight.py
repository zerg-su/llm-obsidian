#!/usr/bin/env python3
"""Hermetic upgrade gate checks."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/upgrade-preflight.py"


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), *args], text=True, capture_output=True, check=False)


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"OK   {name}")


with tempfile.TemporaryDirectory(prefix="upgrade-preflight-test.") as raw:
    root = Path(raw)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "config").mkdir()
    (root / ".codex").mkdir()
    shutil.copy2(ROOT / "config/model-routing.toml", root / "config/model-routing.toml")
    (root / ".codex/dispatch-env.toml").write_text('[codex_dispatch]\nclaude_review_model = "custom-claude"\nclaude_review_effort = "xhigh"\n', encoding="utf-8")
    (root / "seed").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed"], check=True)

    result = run(root)
    check("custom legacy route needs confirmation", result.returncode == 5)
    result = run(root, "--confirm-routing-migration", "--apply")
    check("confirmed migration succeeds", result.returncode == 0)
    check("migration writes ignored-style local config", (root / "config/model-routing.local.toml").is_file())

    (root / ".task-meta.json").write_text(json.dumps({"version": 1, "task_name": "active"}), encoding="utf-8")
    result = run(root)
    check("active task blocks upgrade", result.returncode == 4)

print("upgrade preflight tests passed")
