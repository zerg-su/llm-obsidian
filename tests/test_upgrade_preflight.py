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

    (root / "config/model-routing.local.toml").unlink()
    (root / ".codex/dispatch-env.toml").write_text(
        '[codex_dispatch]\n'
        'codex_review_model = "gpt-5.6-sol"\n'
        'codex_review_effort = "high"\n'
        'claude_review_model = "fable"\n'
        'claude_review_effort = "high"\n',
        encoding="utf-8",
    )
    result = run(root)
    check("stock v2.0.8 routes need no migration", result.returncode == 0)
    check("stock routes write no local override", not (root / "config/model-routing.local.toml").exists())

    (root / ".codex/dispatch-env.toml").write_text(
        '[codex_dispatch]\nclaude_review_model = "custom-claude"\nclaude_review_effort = "ultra"\n',
        encoding="utf-8",
    )
    result = run(root, "--confirm-routing-migration", "--apply")
    check("invalid migration fails before install", result.returncode == 3)
    check("invalid migration leaves no local override", not (root / "config/model-routing.local.toml").exists())

    (root / ".vault-meta/research-runs/11111111-1111-1111-1111-111111111111").mkdir(parents=True)
    (root / ".vault-meta/research-runs/11111111-1111-1111-1111-111111111111/state.json").write_text(
        json.dumps({"schema_version": 1, "status": "fetch_ready"}), encoding="utf-8"
    )
    result = run(root)
    check("unfinished legacy research blocks upgrade", result.returncode == 4 and "research:" in result.stderr)
    shutil.rmtree(root / ".vault-meta")

    (root / ".task-meta.json").write_text(json.dumps({"version": 1, "task_name": "active"}), encoding="utf-8")
    result = run(root)
    check("active task blocks upgrade", result.returncode == 4)

    (root / ".task-meta.json").unlink()
    broker = root / ".vault-meta/task-sessions/projects/11111111-1111-4111-8111-111111111111/tasks/22222222-2222-4222-8222-222222222222"
    broker.mkdir(parents=True)
    (broker / "task.json").write_text(json.dumps({
        "task_id": "22222222-2222-4222-8222-222222222222", "status": "active"
    }), encoding="utf-8")
    result = run(root)
    check("active broker task blocks upgrade", result.returncode == 4 and "broker-task:" in result.stderr)

print("upgrade preflight tests passed")
