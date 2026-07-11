#!/usr/bin/env python3
"""Wire-format parity tests for the shared Claude/Codex hook adapter."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "hooks" / "run-hook.py"


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise SystemExit(f"FAIL {label}: {detail}")
    print(f"OK   {label}")


def invoke(route: str, payload: dict, vault: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["LLM_OBSIDIAN_PROJECT_ROOT"] = str(vault)
    return subprocess.run(
        [sys.executable, str(ADAPTER), route],
        input=json.dumps(payload), text=True, capture_output=True, env=env,
    )


hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
commands = [handler["command"] for groups in hooks.values() for group in groups for handler in group["hooks"]]
check("all routes use adapter", all("run-hook.sh" in command for command in commands))
check("legacy Codex guard removed", all("CODEX_THREAD_ID" not in command for command in commands))
check("Codex events present", all(name in hooks for name in ("SessionStart", "UserPromptSubmit", "PostToolUse", "PostCompact", "Stop")))

with tempfile.TemporaryDirectory(prefix="runtime-hooks-test.") as raw:
    vault = Path(raw)
    (vault / "wiki").mkdir()
    (vault / "scripts").mkdir()
    (vault / "hooks").mkdir()
    (vault / ".claude" / "hooks").mkdir(parents=True)
    (vault / ".claude-plugin").mkdir()
    (vault / ".vault-meta").mkdir()
    (vault / "scripts" / "vault-write.py").write_text("# marker\n", encoding="utf-8")
    shutil.copy2(ROOT / "scripts" / "lib_sanitize.py", vault / "scripts" / "lib_sanitize.py")
    shutil.copy2(ROOT / ".claude" / "skill-rules.json", vault / ".claude" / "skill-rules.json")
    shutil.copy2(ROOT / ".claude" / "hooks" / "command-capture.py", vault / ".claude" / "hooks" / "command-capture.py")
    shutil.copy2(ROOT / "hooks" / "run-hook.sh", vault / "hooks" / "run-hook.sh")
    shutil.copy2(ROOT / "hooks" / "run-hook.py", vault / "hooks" / "run-hook.py")
    (vault / ".claude-plugin" / "plugin.json").write_text('{"name":"llm-obsidian"}\n', encoding="utf-8")
    hot = "# Hot\n\n## Recent Changes\n\n- parity marker\n"
    (vault / "wiki" / "hot.md").write_text(hot, encoding="utf-8")

    fallback_env = dict(
        os.environ,
        PLUGIN_ROOT=str(vault / "removed-plugin-cache"),
        CLAUDE_PLUGIN_ROOT=str(vault / "removed-claude-cache"),
        LLM_OBSIDIAN_PROJECT_ROOT=str(vault),
    )
    hook_command = hooks["PostToolUse"][0]["hooks"][0]["command"]
    fallback_payload = {
        "session_id": "fallback-session",
        "cwd": str(vault),
        "tool_name": "Bash",
        "tool_input": {"command": "python3 scripts/retrieve.py fallback --json"},
        "tool_response": {"stdout": "ok", "stderr": "", "is_error": False},
    }
    result = subprocess.run(
        hook_command,
        shell=True,
        cwd=vault,
        env=fallback_env,
        input=json.dumps(fallback_payload),
        text=True,
        capture_output=True,
    )
    fallback_log = vault / ".vault-meta" / "command-log.jsonl"
    fallback_record = json.loads(fallback_log.read_text(encoding="utf-8").splitlines()[-1]) if fallback_log.is_file() else {}
    check(
        "stale plugin root runs vault adapter",
        result.returncode == 0
        and fallback_record.get("session_id") == "fallback-session"
        and "retrieve.py" in fallback_record.get("command", ""),
        result.stderr,
    )
    missing_env = dict(fallback_env, LLM_OBSIDIAN_PROJECT_ROOT=str(vault / "removed-vault"))
    result = subprocess.run(
        hook_command,
        shell=True,
        cwd=vault,
        env=missing_env,
        input="{}",
        text=True,
        capture_output=True,
    )
    check("missing hook roots fail open", result.returncode == 0 and not result.stdout, result.stderr)

    common = {
        "session_id": "codex-session",
        "cwd": str(vault),
        "model": "gpt-test",
        "permission_mode": "default",
    }
    result = invoke("session-start", {**common, "hook_event_name": "SessionStart", "source": "resume"}, vault)
    check("Codex SessionStart context", result.returncode == 0 and "parity marker" in result.stdout, result.stderr)

    result = invoke("post-compact", {**common, "hook_event_name": "PostCompact", "trigger": "auto", "turn_id": "t1"}, vault)
    compact_output = json.loads(result.stdout)
    check("Codex PostCompact valid JSON", result.returncode == 0 and compact_output["continue"] is True)
    check("Codex PostCompact reload hint", "SessionStart(compact)" in compact_output["systemMessage"])

    prompt = "сохрани в вики HOOK_PRIVATE_SENTINEL"
    result = invoke("router", {**common, "hook_event_name": "UserPromptSubmit", "turn_id": "t1", "prompt": prompt}, vault)
    check("Codex prompt router", 'Skill("save")' in result.stdout, result.stderr)
    router_record = json.loads((vault / ".vault-meta" / "router-hits.jsonl").read_text().splitlines()[-1])
    check("router content-free", "prompt_preview" not in router_record and "HOOK_PRIVATE_SENTINEL" not in json.dumps(router_record))

    command_payload = {
        **common,
        "hook_event_name": "PostToolUse",
        "turn_id": "t1",
        "tool_name": "Bash",
        "tool_use_id": "call1",
        "tool_input": {"command": "python3 scripts/retrieve.py parity --top 5 --json"},
        "tool_response": {"stdout": "ok", "stderr": "", "is_error": False},
    }
    result = invoke("command-capture", command_payload, vault)
    check("Codex command capture", result.returncode == 0, result.stderr)
    record = json.loads((vault / ".vault-meta" / "command-log.jsonl").read_text().splitlines()[-1])
    check("command fields normalized", record["session_id"] == "codex-session" and "retrieve.py" in record["command"])

print("\nAll runtime hook parity tests passed.")
