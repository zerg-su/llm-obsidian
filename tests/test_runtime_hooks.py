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
    for key in (
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_PROJECT_DIR",
        "CODEX_THREAD_ID",
        "LLM_OBSIDIAN_PROJECT_ROOT",
        "LLM_OBSIDIAN_SESSION_ROLE",
    ):
        env.pop(key, None)
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
shell_matchers = {
    group.get("matcher", "")
    for group in hooks["PostToolUse"]
    if "command-capture" in json.dumps(group)
}
check(
    "PostToolUse matches only supported shell tools",
    shell_matchers == {"Bash|exec_command|shell|unified_exec"},
    repr(shell_matchers),
)

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
    shutil.copy2(ROOT / "scripts" / "command_evidence.py", vault / "scripts" / "command_evidence.py")
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

    stop_script = vault / ".claude" / "hooks" / "stop.sh"
    stop_script.write_text(
        "#!/usr/bin/env python3\n"
        "print('VAULT_LINT_FAIL: questions: 1 page(s) without status open|answered')\n"
        "print('COMMIT_BLOCKED: strict vault validation failed; changes remain unstaged/dirty for repair.')\n",
        encoding="utf-8",
    )
    stop_script.chmod(0o755)
    invoke("router", {**common, "hook_event_name": "UserPromptSubmit", "prompt": "status"}, vault)
    result = invoke("stop", {**common, "runtime": "codex", "hook_event_name": "Stop"}, vault)
    blocked_output = json.loads(result.stdout)
    check(
        "blocked turn-end surfaces to the operator",
        result.returncode == 0
        and blocked_output["continue"] is True
        and "COMMIT_BLOCKED" in blocked_output["systemMessage"]
        and "VAULT_LINT_FAIL" in blocked_output["systemMessage"],
        result.stdout,
    )
    check(
        "blocked turn-end still writes the full log",
        "COMMIT_BLOCKED" in (vault / ".vault-meta" / "stop-hook-last.log").read_text(encoding="utf-8"),
    )
    stop_script.write_text(
        "#!/usr/bin/env python3\nprint('WIKI_CHANGED: validated and handled')\n", encoding="utf-8"
    )
    stop_script.chmod(0o755)
    invoke("router", {**common, "hook_event_name": "UserPromptSubmit", "prompt": "status"}, vault)
    result = invoke("stop", {**common, "runtime": "codex", "hook_event_name": "Stop"}, vault)
    check("clean turn-end stays quiet", result.returncode == 0 and not result.stdout.strip(), result.stdout)
    stop_script.unlink()

    claude = {**common, "session_id": "claude-session", "runtime": "claude"}
    invoke("router", {**claude, "hook_event_name": "UserPromptSubmit", "prompt": "status"}, vault)
    invoke("router", {**claude, "hook_event_name": "UserPromptSubmit", "prompt": "status again"}, vault)
    events = [json.loads(line) for line in (vault / ".vault-meta" / "pipeline-events.jsonl").read_text().splitlines()]
    check(
        "stale turn is incomplete",
        events[-1]["op"] == "model-turn-incomplete"
        and events[-1]["runtime"] == "claude"
        and events[-1]["status"] == "degraded"
        and "duration_ms" not in events[-1]["counts"],
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
    (task / "wiki").mkdir()
    (task / "scripts").mkdir()
    (task / ".vault-meta").mkdir()
    (task / "scripts" / "vault-write.py").write_text(
        "# marker\n", encoding="utf-8"
    )
    shutil.copy2(
        ROOT / "scripts" / "lib_sanitize.py",
        task / "scripts" / "lib_sanitize.py",
    )
    shutil.copy2(
        ROOT / "scripts" / "command_evidence.py",
        task / "scripts" / "command_evidence.py",
    )
    (task / ".task-meta.json").write_text(json.dumps({"vault_root": str(vault)}), encoding="utf-8")
    (task / ".task-origin-session").write_text("task-origin\n", encoding="utf-8")
    task_payload = {
        "session_id": "task-session",
        "runtime": "codex",
        "cwd": str(task),
        "prompt": "private task content",
    }
    task_env = dict(os.environ)
    for key in (
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_PROJECT_DIR",
        "CODEX_THREAD_ID",
        "LLM_OBSIDIAN_PROJECT_ROOT",
        "LLM_OBSIDIAN_SESSION_ROLE",
    ):
        task_env.pop(key, None)
    task_result = subprocess.run(
        [sys.executable, str(vault / "hooks" / "run-hook.py"), "router"],
        input=json.dumps(task_payload), text=True, capture_output=True, env=task_env,
    )
    task_marker = json.loads(next(marker_dir.glob("*.json")).read_text())
    check(
        "task origin routes only telemetry to coordinator vault",
        task_result.returncode == 0 and task_marker["actor"] == "task" and not task_result.stdout,
    )
    check("turn marker is content-free", "private task content" not in json.dumps(task_marker))
    task_start = subprocess.run(
        [sys.executable, str(vault / "hooks" / "run-hook.py"), "session-start"],
        input=json.dumps({**task_payload, "source": "resume"}),
        text=True, capture_output=True, env=task_env,
    )
    check(
        "task SessionStart does not inject coordinator context",
        task_start.returncode == 0 and "parity marker" not in task_start.stdout,
        task_start.stderr,
    )
    task_compact = subprocess.run(
        [sys.executable, str(vault / "hooks" / "run-hook.py"), "post-compact"],
        input=json.dumps(task_payload), text=True, capture_output=True, env=task_env,
    )
    check(
        "task PostCompact does not claim coordinator context reload",
        task_compact.returncode == 0 and not task_compact.stdout,
        task_compact.stderr,
    )
    command_log = vault / ".vault-meta" / "command-log.jsonl"
    command_log_before = command_log.read_text(encoding="utf-8")
    task_command_log = task / ".vault-meta" / "command-log.jsonl"
    codex_commands = [
        "pwd && git branch --show-current",
        "git status --short",
        "python3 scripts/codex-adapter.py --check",
        "scripts/mcp-gateway/mcp-gateway.sh codex-sync --check",
        "python3 references/upstream-skills/verify_snapshots.py",
        "python3 tests/harness/test_runtime_research.py",
        "git diff --check",
    ]
    task_capture = None
    for index, command in enumerate(codex_commands, start=1):
        source = (
            "const r = await tools.exec_command("
            + json.dumps(
                {
                    "cmd": command,
                    "workdir": str(task),
                    "yield_time_ms": 10000,
                    "max_output_tokens": 5000,
                },
                separators=(",", ":"),
            )
            + "); text(r.output);"
        )
        task_capture = subprocess.run(
            [sys.executable, str(vault / "hooks" / "run-hook.py"), "command-capture"],
            input=json.dumps(
                {
                    **task_payload,
                    "tool_name": "unified_exec",
                    "tool_use_id": f"codex-call-{index}",
                    "tool_input": {"source": source},
                    "tool_response": {"is_error": False},
                }
            ),
            text=True, capture_output=True, env=task_env,
        )
    task_records = (
        [json.loads(line) for line in task_command_log.read_text(encoding="utf-8").splitlines()]
        if task_command_log.is_file()
        else []
    )
    check(
        "seven Codex shell fixtures write only the task-worktree log",
        task_capture is not None
        and task_capture.returncode == 0
        and len(task_records) == 7
        and [record["command"] for record in task_records] == codex_commands
        and all(record["execution_session"] == "task-session" for record in task_records)
        and all(record["provenance_session"] == "task-origin" for record in task_records)
        and all(record["origin"] == "agent-executed" for record in task_records)
        and all(record["outcome"] == "unknown" for record in task_records)
        and command_log.read_text(encoding="utf-8") == command_log_before,
        task_capture.stderr if task_capture is not None else "missing capture result",
    )
    replay_source = (
        "const r = await tools.exec_command("
        + json.dumps({"cmd": codex_commands[0], "workdir": str(task)}, separators=(",", ":"))
        + "); text(r.output);"
    )
    for tool_name, tool_input, tool_use_id in (
        ("Read", {"command": "echo ignored"}, "ignored-call"),
        (
            "unified_exec",
            {
                "source": "const r = await tools.exec_command("
                + json.dumps(
                    {"cmd": "-----BEGIN PRIVATE KEY-----", "workdir": str(task)},
                    separators=(",", ":"),
                )
                + "); text(r.output);"
            },
            "secret-call",
        ),
        (
            "unified_exec",
            {
                "source": "const r = await tools.exec_command("
                + json.dumps(
                    {
                        "cmd": "rg --fixed-strings 'abcdef123456' .vault-meta/command-log.jsonl",
                        "workdir": str(task),
                    },
                    separators=(",", ":"),
                )
                + "); text(r.output);"
            },
            "literal-secret-search",
        ),
        ("unified_exec", {"source": replay_source}, "codex-call-1"),
    ):
        subprocess.run(
            [
                sys.executable,
                str(vault / "hooks" / "run-hook.py"),
                "command-capture",
            ],
            input=json.dumps(
                {
                    **task_payload,
                    "tool_name": tool_name,
                    "tool_use_id": tool_use_id,
                    "tool_input": tool_input,
                    "tool_response": {"is_error": False},
                }
            ),
            text=True,
            capture_output=True,
            env=task_env,
        )
    check(
        "task capture ignores non-shell tools, rejects secret searches, and deduplicates replay",
        [json.loads(line) for line in task_command_log.read_text(encoding="utf-8").splitlines()]
        == task_records
        and command_log.read_text(encoding="utf-8") == command_log_before,
    )
    explicit_outcomes = []
    for index, response in enumerate(
        (
            {"exit_code": 0},
            {"status": "completed"},
            {"status": "failed"},
        ),
        start=1,
    ):
        command = f"python3 explicit-{index}.py"
        source = (
            "const r = await tools.exec_command("
            + json.dumps(
                {"cmd": command, "workdir": str(task)},
                separators=(",", ":"),
            )
            + "); text(r.output);"
        )
        subprocess.run(
            [
                sys.executable,
                str(vault / "hooks" / "run-hook.py"),
                "command-capture",
            ],
            input=json.dumps(
                {
                    **task_payload,
                    "tool_name": "unified_exec",
                    "tool_use_id": f"explicit-outcome-{index}",
                    "tool_input": {"source": source},
                    "tool_response": response,
                }
            ),
            text=True,
            capture_output=True,
            env=task_env,
        )
        explicit_outcomes.append(
            json.loads(
                task_command_log.read_text(encoding="utf-8").splitlines()[-1]
            )["outcome"]
        )
    check(
        "only explicit Codex exit or status evidence sets an outcome",
        explicit_outcomes == ["success", "success", "error"],
        explicit_outcomes,
    )
    subprocess.run(
        [sys.executable, str(vault / "hooks" / "run-hook.py"), "router"],
        input=json.dumps(task_payload), text=True, capture_output=True, env=task_env,
    )
    stop_probe = vault / ".vault-meta" / "task-stop-probe"
    (vault / ".claude" / "hooks" / "stop.sh").write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        f"Path({str(stop_probe)!r}).write_text('blocked', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (vault / ".claude" / "hooks" / "stop.sh").chmod(0o755)
    task_stop = subprocess.run(
        [sys.executable, str(vault / "hooks" / "run-hook.py"), "stop"],
        input=json.dumps(task_payload), text=True, capture_output=True, env=task_env,
    )
    check(
        "task Stop records telemetry without coordinator pipeline",
        task_stop.returncode == 0 and not stop_probe.exists() and not list(marker_dir.glob("*.json")),
        task_stop.stderr,
    )
    broken_task = vault / "broken-task-worktree"
    broken_task.mkdir()
    (broken_task / ".task-meta.json").write_text("{}\n", encoding="utf-8")
    broken_stop = subprocess.run(
        [sys.executable, str(vault / "hooks" / "run-hook.py"), "stop"],
        input=json.dumps({**task_payload, "cwd": str(broken_task)}),
        text=True, capture_output=True, env={**task_env, "LLM_OBSIDIAN_PROJECT_ROOT": str(vault)},
    )
    check(
        "invalid task origin fails closed before coordinator pipeline",
        broken_stop.returncode == 0 and not stop_probe.exists(),
        broken_stop.stderr,
    )

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
