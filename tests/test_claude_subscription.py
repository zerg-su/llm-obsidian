#!/usr/bin/env python3
"""Hermetic fail-closed tests for Claude subscription-only daily synthesis."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "claude-subscription-check.py"
BLOCKERS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise SystemExit(f"FAIL {label}: {detail}")
    print(f"OK   {label}")


def run(
    fake: Path,
    payload: object,
    *,
    extra_env: dict[str, str] | None = None,
    exit_code: int = 0,
    checker_args: tuple[str, ...] = (),
):
    env = dict(os.environ)
    for name in BLOCKERS:
        env.pop(name, None)
    env.update(
        {
            "FAKE_CLAUDE_STATUS": json.dumps(payload),
            "FAKE_CLAUDE_EXIT": str(exit_code),
            "SENSITIVE_SENTINEL": "sk-ant-never-print-this",
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(CHECKER), "--claude-bin", str(fake), *checker_args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


with tempfile.TemporaryDirectory(prefix="claude-subscription-test.") as raw:
    fake = Path(raw) / "claude"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "print(os.environ.get('FAKE_CLAUDE_STATUS', '{}'))\n"
        "raise SystemExit(int(os.environ.get('FAKE_CLAUDE_EXIT', '0')))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)

    valid = {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "subscriptionType": "max",
        "email": "private@example.invalid",
        "orgId": "private-org",
    }
    result = run(fake, valid)
    output = json.loads(result.stdout)
    check("subscription accepted", result.returncode == 0 and output["subscription_type"] == "max", result.stderr)
    check("private auth metadata omitted", "private@" not in result.stdout and "private-org" not in result.stdout)

    for name in BLOCKERS:
        result = run(fake, valid, extra_env={name: "sk-ant-never-print-this"})
        check(f"override rejected: {name}", result.returncode == 4)
        check(f"override value redacted: {name}", "sk-ant-never-print-this" not in result.stderr)

    result = run(fake, valid, extra_env={"CLAUDE_CODE_USE_BEDROCK": "0"})
    check("false-like override remains fail-closed", result.returncode == 4 and "including 0/false" in result.stderr)

    rejected = (
        ("not logged in", {**valid, "loggedIn": False}),
        ("API key auth", {**valid, "authMethod": "api_key"}),
        ("OAuth setup token", {**valid, "authMethod": "oauth_token"}),
        ("cloud provider", {**valid, "apiProvider": "bedrock"}),
        ("console account", {**valid, "subscriptionType": "console"}),
    )
    for label, payload in rejected:
        result = run(fake, payload)
        check(f"{label} rejected", result.returncode == 4)

    result = run(fake, "not-an-object")
    check("non-object status rejected", result.returncode == 4)
    result = run(fake, valid, exit_code=1)
    check("auth command failure rejected", result.returncode == 4)
    result = run(Path(raw) / "missing-claude", valid)
    check("missing Claude CLI rejected", result.returncode == 4)
    result = run(fake, valid, checker_args=("--timeout", "0"))
    check("invalid timeout rejected", result.returncode == 2)

    home = Path(raw) / "home"
    native = home / ".local" / "bin" / "claude"
    native.parent.mkdir(parents=True)
    native.write_text(fake.read_text(encoding="utf-8"), encoding="utf-8")
    native.chmod(0o755)
    shim = Path(raw) / "cmux-cli-shims" / "surface" / "claude"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\nprintf '%s\\n' '{\"loggedIn\":false}'\n", encoding="utf-8")
    shim.chmod(0o755)
    shim_env = dict(os.environ)
    for name in BLOCKERS:
        shim_env.pop(name, None)
    shim_env.update({
        "HOME": str(home),
        "PATH": f"{shim.parent}:{shim_env.get('PATH', '')}",
        "FAKE_CLAUDE_STATUS": json.dumps(valid),
        "FAKE_CLAUDE_EXIT": "0",
    })
    result = subprocess.run(
        [sys.executable, str(CHECKER)], cwd=ROOT, env=shim_env,
        text=True, capture_output=True,
    )
    check("cmux shim bypassed for auth status", result.returncode == 0, result.stderr)

agent = (ROOT / "agents" / "daily-summarizer.md").read_text(encoding="utf-8")
plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
frontmatter = agent.split("---", 2)[1]
check("Claude daily inherits model at medium", "model: inherit" in frontmatter and "effort: medium" in frontmatter)
check("Claude agent bounded", "maxTurns: 4" in frontmatter and "tools: Read" in frontmatter)
check(
    "Claude agent has no mutation tools",
    all(name not in frontmatter for name in ("Write", "Edit", "Bash", "Agent", "Skill", "permissionMode", "mcpServers")),
)
check("Claude agent carries summary contract", '"evidence_bundle_id"' in agent and '"evidence_ids"' in agent)

skill = (ROOT / "skills" / "daily" / "SKILL.md").read_text(encoding="utf-8")
makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
check("daily runs subscription preflight", "scripts/claude-subscription-check.py" in skill)
check(
    "daily live acceptance uses harness proof",
    "LLM_OBSIDIAN_ACCEPTANCE=1" in skill
    and "harness preflight" in skill
    and "do not probe auth" in skill,
)
check(
    "both live acceptance entrypoints preflight Claude once",
    makefile.count("@python3 scripts/claude-subscription-check.py") == 2,
)
check("daily selects scoped Claude agent", "llm-obsidian:daily-summarizer" in skill)
check(
    "daily corrects invalid Codex JSON in the same agent thread",
    "correct once in the same agent thread" in skill
    and "validator error" in skill
    and "never spawn/fallback" in skill,
)
check("daily forbids Claude parent fallback", "Never fall back to the parent Claude model" in skill)
stats = (ROOT / "scripts" / "pipeline-stats.py").read_text(encoding="utf-8")
check("Claude agent telemetry registered", 'CUSTOM_AGENTS: set[str] = {"daily-summarizer"}' in stats)
check(
    "Claude plugin explicitly registers the bounded daily agent",
    plugin.get("agents") == ["./agents/daily-summarizer.md"],
)
check(
    "Claude plugin author uses the current manifest schema",
    plugin.get("author") == {"name": "zerg-su", "url": "https://github.com/zerg-su"},
)

print("\nAll Claude subscription tests passed.")
