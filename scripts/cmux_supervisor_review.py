"""Review relay and exact trust watchers for the legacy cmux supervisor."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any

from task_contract import ContractError, normalize, read_json as read_task_json
from cmux_agent_support import SupervisorError
from cmux_trust_prompt import (
    claude_background_exit_prompt_visible,
    native_dialog_region,
    workspace_trust_prompt_visible,
)
from harness.adapters.cmux import run_cmux
from cmux_supervisor_contracts import (
    ARMED_EXIT_POLL_SECONDS,
    REVIEW_OUTBOX_FILE,
    REVIEW_RELAY_FILE,
    REVIEW_RELAY_POLL_SECONDS,
    REVIEW_RELAY_TIMEOUT_SECONDS,
    SCRIPT_DIR,
    WORKSPACE_TRUST_POLL_SECONDS,
    WORKSPACE_TRUST_TIMEOUT_SECONDS,
    read_json,
    write_json,
)


def relay_state(state_dir: Path) -> dict[str, Any]:
    path = state_dir / REVIEW_RELAY_FILE
    if path.exists():
        try:
            value = read_json(path)
            if value.get("version") == 1:
                return value
        except SupervisorError:
            pass
    return {
        "version": 1,
        "status": "waiting",
        "attempts": 0,
        "sent_count": 0,
        "failure_count": 0,
        "last_payload_sha256": None,
    }


def relay_review_outbox_once(
    worktree: Path,
    runtime: Path,
    runner: Any = subprocess.run,
    *,
    state_dir: Path | None = None,
) -> bool:
    """Validate and forward one stable outbox payload outside the reviewer sandbox."""
    outbox = runtime / REVIEW_OUTBOX_FILE
    try:
        raw = outbox.read_bytes()
    except FileNotFoundError:
        return False
    if not raw:
        return False
    digest = hashlib.sha256(raw).hexdigest()
    state_dir = state_dir or worktree
    state = relay_state(state_dir)
    if state.get("status") == "failed" and state.get("last_payload_sha256") == digest:
        return False

    state["attempts"] = int(state.get("attempts") or 0) + 1
    state["last_payload_sha256"] = digest
    command = [
        sys.executable,
        str(SCRIPT_DIR / "harness" / "review_submit.py"),
        "--worktree",
        str(worktree),
        "--state-dir",
        str(state_dir),
    ]
    try:
        result = runner(
            command,
            input=raw.decode("utf-8"),
            text=True,
            capture_output=True,
            cwd=worktree,
            timeout=REVIEW_RELAY_TIMEOUT_SECONDS,
            check=False,
        )
        succeeded = result.returncode == 0
    except (OSError, UnicodeDecodeError, subprocess.TimeoutExpired):
        succeeded = False

    if succeeded:
        outbox.unlink(missing_ok=True)
        state["status"] = "sent"
        state["sent_count"] = int(state.get("sent_count") or 0) + 1
    else:
        state["status"] = "failed"
        state["failure_count"] = int(state.get("failure_count") or 0) + 1
    write_json(state_dir / REVIEW_RELAY_FILE, state)
    return succeeded


def run_review_relay(worktree: Path, state_dir: Path, runtime: Path, stop: threading.Event) -> None:
    state = relay_state(state_dir)
    state["status"] = "waiting"
    write_json(state_dir / REVIEW_RELAY_FILE, state)
    while not stop.wait(REVIEW_RELAY_POLL_SECONDS):
        relay_review_outbox_once(worktree, runtime, state_dir=state_dir)
    relay_review_outbox_once(worktree, runtime, state_dir=state_dir)
    state = relay_state(state_dir)
    if state.get("status") != "failed":
        state["status"] = "stopped"
        write_json(state_dir / REVIEW_RELAY_FILE, state)


def reviewer_uses_supervised_relay(runtime: str) -> bool:
    """Both reviewer runtimes use the trusted, operation-scoped outbox relay."""
    return runtime in {"claude", "codex"}


def stop_watchdog(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def claude_mcp_trust_prompt_visible(screen: str) -> bool:
    """Recognize only Claude's complete native project-MCP trust dialog."""
    markers = (
        "New MCP server found in this project:",
        "MCP servers may execute code or access system resources.",
        "Use this MCP server",
        "Use this and all future MCP servers in this project",
        "Continue without using this MCP server",
        "Enter to confirm",
    )
    region = native_dialog_region(
        screen,
        "Enter to confirm",
        footer_variants=("Enter to confirm · Esc to cancel",),
    )
    if not region:
        return False
    compact_screen = re.sub(r"\s+", "", region)
    return all(re.sub(r"\s+", "", marker) in compact_screen for marker in markers)


def auto_confirm_armed_claude_exit(
    worktree: Path,
    state_dir: Path,
    kind: str,
    surface: str,
    stop: threading.Event,
    state: dict[str, int],
    runner: Any = subprocess.run,
    *,
    poll_seconds: float = ARMED_EXIT_POLL_SECONDS,
) -> None:
    """Confirm only an exact Claude exit dialog after lifecycle authorization."""
    marker = (
        state_dir / ".review-close-armed.json"
        if kind == "reviewer"
        else worktree / ".task-close-armed.json"
    )
    while not stop.wait(max(0.001, poll_seconds)):
        if not marker.is_file():
            continue
        try:
            result = run_cmux(
                ["read-screen", "--surface", surface, "--lines", "40"],
                runner=runner, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            state["read_failures"] = state.get("read_failures", 0) + 1
            continue
        if result.returncode != 0:
            state["read_failures"] = state.get("read_failures", 0) + 1
            continue
        if not claude_background_exit_prompt_visible(result.stdout):
            continue
        try:
            confirmed = run_cmux(
                ["send-key", "--surface", surface, "Enter"], runner=runner, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            state["send_failures"] = state.get("send_failures", 0) + 1
            return
        if confirmed.returncode == 0:
            state["confirms"] = state.get("confirms", 0) + 1
        else:
            state["send_failures"] = state.get("send_failures", 0) + 1
        return


def primary_coordinator_review(
    worktree: Path, state_dir: Path, review_meta: dict[str, Any]
) -> bool:
    """Recognize one explicit root review without relying on task metadata."""

    root = worktree.resolve()
    declared = [
        str(review_meta.get(field) or "").strip()
        for field in ("worktree", "vault_root", "operation_dir")
    ]
    return (
        review_meta.get("archive_mode") == "coordinator"
        and all(declared)
        and state_dir.resolve() == root
        and all(Path(value).expanduser().resolve() == root for value in declared)
        and (root / ".git").is_dir()
    )


def automatic_workspace_trust_allowed(
    worktree: Path, state_dir: Path, kind: str
) -> bool:
    """Allow bootstrap for an approved task or one explicit root review."""

    if kind == "reviewer":
        try:
            review_meta = read_json(state_dir / ".review-meta.json")
        except (OSError, SupervisorError):
            review_meta = {}
        if primary_coordinator_review(worktree, state_dir, review_meta):
            return True
    try:
        meta = read_task_json(worktree / ".task-meta.json")
        policy = normalize(meta)
    except (ContractError, OSError, ValueError):
        return False
    return meta.get("version") in {2, 3, 4} and policy["interaction_policy"] == "unattended"


def auto_accept_workspace_trust(
    surface: str,
    runtime: str,
    stop: threading.Event,
    state: dict[str, int],
    runner: Any = subprocess.run,
    *,
    timeout_seconds: float | None = WORKSPACE_TRUST_TIMEOUT_SECONDS,
    poll_seconds: float = WORKSPACE_TRUST_POLL_SECONDS,
) -> None:
    """Handle exact native bootstrap prompts while the owned agent is alive."""
    deadline = (
        time.monotonic() + max(0.0, timeout_seconds)
        if timeout_seconds is not None
        else None
    )
    workspace_handled = False
    while (
        (deadline is None or time.monotonic() < deadline)
        and not stop.wait(max(0.001, poll_seconds))
    ):
        try:
            result = run_cmux(
                ["read-screen", "--surface", surface, "--lines", "80"],
                runner=runner, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            state["read_failures"] = state.get("read_failures", 0) + 1
            continue
        if result.returncode != 0:
            state["read_failures"] = state.get("read_failures", 0) + 1
            continue
        if runtime == "claude" and claude_mcp_trust_prompt_visible(result.stdout):
            commands = [
                ["send-key", "--surface", surface, "down"],
                ["send-key", "--surface", surface, "down"],
                ["send-key", "--surface", surface, "Enter"],
            ]
            for command in commands:
                try:
                    declined = run_cmux(command, runner=runner, timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    state["send_failures"] = state.get("send_failures", 0) + 1
                    return
                if declined.returncode != 0:
                    state["send_failures"] = state.get("send_failures", 0) + 1
                    return
            state["mcp_declines"] = state.get("mcp_declines", 0) + 1
            return
        if workspace_handled or not workspace_trust_prompt_visible(runtime, result.stdout):
            continue
        try:
            accepted = run_cmux(
                ["send-key", "--surface", surface, "Enter"], runner=runner, timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            state["send_failures"] = state.get("send_failures", 0) + 1
            return
        if accepted.returncode == 0:
            state["accepts"] = state.get("accepts", 0) + 1
            workspace_handled = True
            if runtime != "claude":
                return
        else:
            state["send_failures"] = state.get("send_failures", 0) + 1
            return
