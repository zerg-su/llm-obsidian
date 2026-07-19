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
    adapter = vault / "hooks" / "run-hook.py"
    if not adapter.is_file():
        adapter = ADAPTER
    return subprocess.run(
        [sys.executable, str(adapter), route],
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
    shutil.copy2(ROOT / "scripts" / "pipeline_events.py", vault / "scripts" / "pipeline_events.py")
    shutil.copy2(ROOT / "scripts" / "lifecycle_telemetry.py", vault / "scripts" / "lifecycle_telemetry.py")
    shutil.copy2(ROOT / "scripts" / "turn_telemetry.py", vault / "scripts" / "turn_telemetry.py")
    shutil.copy2(ROOT / ".claude" / "skill-rules.json", vault / ".claude" / "skill-rules.json")
    shutil.copy2(ROOT / ".claude" / "hooks" / "command-capture.py", vault / ".claude" / "hooks" / "command-capture.py")
    shutil.copy2(ROOT / ".claude" / "hooks" / "skill-router.py", vault / ".claude" / "hooks" / "skill-router.py")
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

    marker_dir = vault / ".vault-meta" / "turn-markers"
    check("Codex turn marker created", len(list(marker_dir.glob("*.json"))) == 1)
    result = invoke("stop", {**common, "runtime": "codex", "hook_event_name": "Stop"}, vault)
    events = [json.loads(line) for line in (vault / ".vault-meta" / "pipeline-events.jsonl").read_text().splitlines()]
    turn = events[-1]
    check(
        "Codex turn duration emitted before Stop",
        result.returncode == 0
        and turn["op"] == "model-turn"
        and turn["runtime"] == "codex"
        and turn["session"] == "codex-session"
        and turn["counts"]["duration_ms"] >= 0,
        result.stderr,
    )
    check("completed marker removed", not list(marker_dir.glob("*.json")))

    claude = {**common, "session_id": "claude-session", "runtime": "claude"}
    invoke("router", {**claude, "hook_event_name": "UserPromptSubmit", "prompt": "status"}, vault)
    invoke("router", {**claude, "hook_event_name": "UserPromptSubmit", "prompt": "status again"}, vault)
    events = [json.loads(line) for line in (vault / ".vault-meta" / "pipeline-events.jsonl").read_text().splitlines()]
    check(
        "stale turn is incomplete",
        events[-1]["op"] == "model-turn-incomplete"
        and events[-1]["runtime"] == "claude"
        and events[-1]["status"] == "degraded",
    )
    invoke("session-start", {**claude, "hook_event_name": "SessionStart", "source": "resume"}, vault)
    check("SessionStart clears stale marker", not list(marker_dir.glob("*.json")))

    before = (vault / ".vault-meta" / "pipeline-events.jsonl").read_text()
    no_session_env = dict(os.environ, LLM_OBSIDIAN_PROJECT_ROOT=str(vault))
    for key in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID"):
        no_session_env.pop(key, None)
    for route in ("router", "stop"):
        subprocess.run(
            [sys.executable, str(vault / "hooks" / "run-hook.py"), route],
            input=json.dumps({"cwd": str(vault), "runtime": "codex", "prompt": "no identity"}),
            text=True, capture_output=True, env=no_session_env,
        )
    check("missing session identity is silent no-op", (vault / ".vault-meta" / "pipeline-events.jsonl").read_text() == before)

    task = vault / "task-worktree"
    task.mkdir()
    (task / ".task-meta.json").write_text(json.dumps({"vault_root": str(vault)}), encoding="utf-8")
    task_payload = {
        "session_id": "task-session",
        "runtime": "codex",
        "cwd": str(task),
        "prompt": "private task content",
    }
    task_env = dict(os.environ)
    task_result = subprocess.run(
        [sys.executable, str(vault / "hooks" / "run-hook.py"), "router"],
        input=json.dumps(task_payload), text=True, capture_output=True, env=task_env,
    )
    task_marker = json.loads(next(marker_dir.glob("*.json")).read_text())
    check("task origin routes to coordinator vault", task_result.returncode == 0 and task_marker["actor"] == "task")
    check("turn marker is content-free", "private task content" not in json.dumps(task_marker))

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
