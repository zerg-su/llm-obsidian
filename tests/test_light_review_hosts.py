#!/usr/bin/env python3
"""Host packaging and deterministic helper integration for Light Review."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


claude_agent = (ROOT / "agents/light-reviewer.md").read_text(encoding="utf-8")
frontmatter = claude_agent.split("---", 2)[1]
check("Claude Light agent inherits its model", "model: inherit" in frontmatter)
check("Claude Light agent has bounded read-only inspection tools", all(tool in frontmatter for tool in ("Read", "Grep", "Glob", "Bash")))
check("Claude Light agent cannot delegate", "Agent" not in frontmatter and "Skill" not in frontmatter)
check(
    "Claude Light agent never dereferences escaping changed symlinks",
    "Never dereference a changed" in claude_agent and "exact target root" in claude_agent,
)
check(
    "Claude Light agent preserves snapshot-origin coverage gaps",
    "LightSnapshot.coverage_gaps" in claude_agent
    and "Never drop or rewrite" in claude_agent,
)

codex_agent = tomllib.loads((ROOT / ".codex/agents/light-reviewer.toml").read_text(encoding="utf-8"))
check("Codex Light agent is read-only and non-interactive", codex_agent["sandbox_mode"] == "read-only" and codex_agent["approval_policy"] == "never")
check("Codex Light agent inherits model and effort", "model" not in codex_agent and "model_reasoning_effort" not in codex_agent)
check("Codex Light agent disables nested capabilities", codex_agent["features"] == {"apps": False, "multi_agent": False, "memories": False, "hooks": False})
check(
    "Codex Light agent never dereferences escaping changed symlinks",
    "Never dereference a changed" in codex_agent["developer_instructions"]
    and "exact target root" in codex_agent["developer_instructions"],
)
check(
    "Codex Light agent preserves snapshot-origin coverage gaps",
    "LightSnapshot.coverage_gaps" in codex_agent["developer_instructions"]
    and "Never drop or rewrite" in codex_agent["developer_instructions"],
)

plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
check("Claude plugin registers both bounded agents", plugin["agents"] == ["./agents/daily-summarizer.md", "./agents/light-reviewer.md"])

with tempfile.TemporaryDirectory(prefix="light-review-host-test.") as raw:
    repo = Path(raw) / "product"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "review@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Light Review Test"], cwd=repo, check=True)
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    before = subprocess.run(["git", "status", "--porcelain"], cwd=repo, text=True, capture_output=True, check=True).stdout
    snapshot = subprocess.run(
        [sys.executable, str(ROOT / "scripts/review_target.py"), "light", "--target", str(repo)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    after = subprocess.run(["git", "status", "--porcelain"], cwd=repo, text=True, capture_output=True, check=True).stdout
    snapshot_payload = json.loads(snapshot.stdout) if snapshot.returncode == 0 else {}
    check("Light snapshot CLI succeeds without target writes", snapshot.returncode == 0 and before == after, snapshot.stderr)
    check("Light snapshot CLI emits its exact target", snapshot_payload.get("root") == str(repo.resolve()))

env = dict(
    os.environ,
    LLM_OBSIDIAN_SESSION_RUNTIME="codex",
    LLM_OBSIDIAN_SESSION_MODEL="gpt-5.6-sol",
    LLM_OBSIDIAN_SESSION_EFFORT="high",
)
route = subprocess.run(
    [sys.executable, str(ROOT / "scripts/model_routing.py"), "--root", str(ROOT), "light-review", "--model", "terra", "--effort", "xhigh"],
    cwd=ROOT,
    env=env,
    text=True,
    capture_output=True,
    check=False,
)
route_payload = json.loads(route.stdout) if route.returncode == 0 else {}
check("Light route CLI preserves the host runtime", route.returncode == 0 and route_payload.get("runtime") == "codex" and route_payload.get("model") == "gpt-5.6-terra", route.stderr)

cross = subprocess.run(
    [sys.executable, str(ROOT / "scripts/model_routing.py"), "--root", str(ROOT), "light-review", "--model", "opus"],
    cwd=ROOT,
    env=env,
    text=True,
    capture_output=True,
    check=False,
)
check("Light route CLI rejects cross-runtime before delegation", cross.returncode == 3 and "same runtime" in cross.stderr)

print("\nAll Light Review host tests passed.")
