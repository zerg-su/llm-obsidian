#!/usr/bin/env python3
"""Runtime-neutral adapter for Claude and Codex lifecycle hook payloads."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from lifecycle_telemetry import origin_vault  # noqa: E402
from turn_telemetry import clear_stale, finish_turn, start_turn  # noqa: E402


def payload() -> tuple[dict[str, Any], str]:
    raw = sys.stdin.read()
    try:
        value = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        value = {}
    return (value if isinstance(value, dict) else {}), raw


def vault_root(data: dict[str, Any]) -> Path | None:
    raw = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if raw:
        path = Path(str(raw)).expanduser().resolve()
        for candidate in (path, *path.parents):
            if (candidate / ".task-meta.json").is_file():
                declared = origin_vault(candidate)
                if declared is not None:
                    return declared
    candidates = [
        os.environ.get("LLM_OBSIDIAN_PROJECT_ROOT"),
        os.environ.get("CLAUDE_PROJECT_DIR"),
        data.get("cwd"),
        os.getcwd(),
    ]
    seen: set[Path] = set()
    for raw in candidates:
        if not raw:
            continue
        path = Path(str(raw)).expanduser().resolve()
        for candidate in (path, *path.parents):
            if candidate in seen:
                continue
            seen.add(candidate)
            if (candidate / "wiki").is_dir() and (candidate / "scripts" / "vault-write.py").is_file():
                return candidate
    return None


def hook_env(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["LLM_OBSIDIAN_PROJECT_ROOT"] = str(root)
    env["CLAUDE_PROJECT_DIR"] = str(root)
    env["LLM_OBSIDIAN_ALLOW_CLAUDE_HOOKS"] = "1"
    return env


def invoke(path: Path, raw: str, root: Path, *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    command = [str(path)] if os.access(path, os.X_OK) else [sys.executable, str(path)]
    return subprocess.run(
        command,
        input=raw,
        text=True,
        capture_output=capture,
        env=hook_env(root),
        cwd=root,
        check=False,
    )


def emit(text: str) -> None:
    if text:
        sys.stdout.write(text.rstrip() + "\n")


def session_context(root: Path, data: dict[str, Any], raw: str) -> None:
    clear_stale(root, data)
    hot = root / "wiki" / "hot.md"
    if hot.is_file():
        emit(hot.read_text(encoding="utf-8"))
    if data.get("source") == "startup":
        preflight = root / "scripts" / "session-preflight.py"
        if preflight.is_file():
            command = [sys.executable, str(preflight), "--root", str(root)]
            session_id = str(data.get("session_id") or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "")
            if session_id:
                command.extend(["--session-id", session_id])
            runtime = str(data.get("runtime") or "")
            model = str(data.get("model") or "")
            effort = str(data.get("effort") or data.get("model_reasoning_effort") or "")
            if runtime and model and effort:
                command.extend(["--runtime", runtime, "--model", model, "--effort", effort])
            result = subprocess.run(command, text=True, capture_output=True, cwd=root, env=hook_env(root), check=False)
            emit(result.stdout)
        result = invoke(PLUGIN_ROOT / ".claude" / "hooks" / "session-nudge.sh", raw, root)
        if result.returncode == 0:
            emit(result.stdout)


def stop_pipeline(root: Path, raw: str) -> None:
    result = invoke(PLUGIN_ROOT / ".claude" / "hooks" / "stop.sh", raw, root)
    log = root / ".vault-meta" / "stop-hook-last.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text((result.stdout or "") + (result.stderr or ""), encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    if len(sys.argv) != 2:
        return 0
    route = sys.argv[1]
    data, raw = payload()
    root = vault_root(data)
    if root is None:
        return 0
    if route == "router":
        context = Path(str(data.get("cwd") or os.getcwd())).expanduser().resolve()
        start_turn(root, data, context_root=context)
        emit(invoke(PLUGIN_ROOT / ".claude" / "hooks" / "skill-router.py", raw, root).stdout)
    elif route == "command-capture":
        tool_name = str(data.get("tool_name") or "")
        if tool_name in {"Bash", "exec_command", "shell", "unified_exec"}:
            invoke(PLUGIN_ROOT / ".claude" / "hooks" / "command-capture.py", raw, root)
    elif route == "plan-capture":
        # Codex has no ExitPlanMode tool event. This route remains Claude-only
        # by matcher, while sharing the same adapter and root resolution.
        invoke(PLUGIN_ROOT / ".claude" / "hooks" / "plan-capture.sh", raw, root)
    elif route == "session-start":
        session_context(root, data, raw)
    elif route == "post-compact":
        # Codex ignores plain PostCompact stdout. SessionStart(source=compact)
        # reloads the actual hot cache; this valid shared JSON is only a nudge.
        print(
            json.dumps(
                {
                    "continue": True,
                    "systemMessage": "Compaction finished; wiki/hot.md is reloaded by SessionStart(compact).",
                }
            )
        )
    elif route == "stop":
        finish_turn(root, data)
        stop_pipeline(root, raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
