#!/usr/bin/env python3
"""Queue graceful /exit into this exact cmux surface; never close the surface."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from harness.adapters.cmux import CmuxAdapter, CmuxError


SURFACE_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
# The queued /exit wins the race against the Stop hook, so a closing session
# would otherwise leave its own writes uncommitted. Run turn-end here instead.
TURN_END_BLOCKERS = (
    "COMMIT_BLOCKED:",
    "COMMIT_FAILED:",
    "MEMORY_BACKUP_BLOCKED:",
    "STOP_LOCK_BUSY:",
    # Zero-exit markers still mean the just-saved vault stayed uncommitted, so
    # close must not terminate the agent on them. Plain /exit remains available.
    "AUTO_COMMIT_DISABLED:",
    "TASK_SPLIT_STOP_SKIPPED:",
)
TURN_END_TIMEOUT = 240.0
TURN_END_LOCK_WAIT = "45"
TURN_END_OUTPUT_CAP = 1500


def runtime() -> str:
    return "codex" if any(os.environ.get(key) for key in ("CODEX_THREAD_ID", "CODEX_CI", "CODEX_MANAGED_BY_NPM")) else "claude"


def run(cmux: CmuxAdapter, action: str, surface: str, value: str) -> bool:
    try:
        if action == "send":
            cmux.send(surface, value)
        elif action == "send-key":
            cmux.send_key(surface, value)
        else:
            raise ValueError("unsupported cmux action")
    except (CmuxError, OSError, ValueError):
        return False
    return True


def vault_root() -> Path | None:
    candidates = [
        os.environ.get("LLM_OBSIDIAN_PROJECT_ROOT"),
        os.environ.get("CLAUDE_PROJECT_DIR"),
        os.getcwd(),
    ]
    for raw in candidates:
        if not raw:
            continue
        path = Path(str(raw)).expanduser().resolve()
        for candidate in (path, *path.parents):
            if (candidate / "wiki").is_dir() and (candidate / "scripts" / "vault-write.py").is_file():
                return candidate
    return None


def run_turn_end() -> dict[str, object]:
    """Flush recovery/reindex/validation/commit before the agent can exit."""
    root = vault_root()
    if root is None:
        return {"status": "skipped", "reason": "vault root unavailable"}
    helper = root / "scripts" / "stop-hook.py"
    if not helper.is_file():
        return {"status": "skipped", "reason": "turn-end pipeline unavailable"}
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(root),
        "LLM_OBSIDIAN_PROJECT_ROOT": str(root),
        "LLM_OBSIDIAN_STOP_LOCK_TIMEOUT_SEC": os.environ.get(
            "LLM_OBSIDIAN_STOP_LOCK_TIMEOUT_SEC", TURN_END_LOCK_WAIT
        ),
    }
    try:
        result = subprocess.run(
            [sys.executable, str(helper)],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=TURN_END_TIMEOUT,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "blocked", "reason": f"turn-end pipeline did not finish: {exc}"}
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    blocked = [line for line in lines if line.startswith(TURN_END_BLOCKERS)]
    if not blocked and result.returncode == 0:
        return {"status": "clean"}
    return {
        "status": "blocked",
        "reason": "; ".join(blocked) or f"turn-end pipeline exited {result.returncode}",
        "output": output[:TURN_END_OUTPUT_CAP],
    }


def queue_exit() -> dict[str, object]:
    selected_runtime = runtime()
    surface = str(os.environ.get("CMUX_SURFACE_ID") or "").strip()
    configured = str(os.environ.get("CMUX_BUNDLED_CLI_PATH") or "").strip()
    binary = configured if configured and Path(configured).is_file() else "cmux"
    cmux = CmuxAdapter(binary=binary)
    turn_end = run_turn_end()
    base = {
        "schema_version": 1,
        "runtime": selected_runtime,
        "surface_closed": False,
        "manual_fallback": selected_runtime == "codex",
        "turn_end": turn_end,
    }
    if turn_end["status"] == "blocked":
        return {
            **base,
            "status": "blocked",
            "reason": "turn-end pipeline blocked; vault changes are uncommitted",
        }
    if not surface or not SURFACE_RE.fullmatch(surface):
        return {**base, "status": "manual", "reason": "exact cmux surface unavailable"}
    if selected_runtime == "claude":
        ok = run(cmux, "send-key", surface, "ctrl+u")
        ok = run(cmux, "send", surface, "/exit") and ok
        ok = run(cmux, "send-key", surface, "Enter") and ok
    else:
        for _index in range(40):
            run(cmux, "send-key", surface, "Backspace")
        ok = run(cmux, "send", surface, "/exit")
        ok = run(cmux, "send-key", surface, "Tab") and ok
    return {
        **base,
        "status": "queued" if ok else "manual",
        "reason": "exact-surface graceful exit queued" if ok else "cmux queue failed",
    }


def main() -> int:
    print(json.dumps(queue_exit(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
