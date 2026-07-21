"""Runtime argv, outbox, cmux surface, and process lifecycle primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .contracts import (
    AcceptanceRunnerError, AcceptanceTransientError, SAFE_ID, atomic_json,
    heartbeat, read_json,
)
from .sandbox import scratch_root_for

ROOT = Path(__file__).resolve().parents[2]
SURFACE_RE = re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b")
OUTBOX_MAX_BYTES = 64 * 1024
OUTBOX_INVALID_GRACE_SECONDS = 5.0
OUTBOX_STABLE_SECONDS = 1.0
AGENT_EXIT_GRACE_SECONDS = 300.0
CHILD_SURFACE_SETTLE_SECONDS = 45.0

sys.path.insert(0, str(ROOT / "scripts"))
from cmux_agent_support import resolved_git_common_dir, task_codex_config_values, validated_cmux_socket_path  # noqa: E402
from cmux_trust_prompt import claude_background_exit_prompt_visible, workspace_trust_prompt_visible  # noqa: E402
from model_routing import load_config  # noqa: E402
from task_sessions import TaskSessionError, close_surface_exact  # noqa: E402

def agent_argv(
    runtime: str,
    sandbox: Path,
    model: str,
    effort: str,
    prompt: str,
    *,
    scratch_root: Path | None = None,
    surface: str = "",
    session_id: str = "",
) -> tuple[list[str], dict[str, str]]:
    env = os.environ.copy()
    env["LLM_OBSIDIAN_ACCEPTANCE"] = "1"
    if session_id:
        if SAFE_ID.fullmatch(session_id) is None:
            raise AcceptanceRunnerError("acceptance session id is invalid")
        env["LLM_OBSIDIAN_ACCEPTANCE_SESSION_ID"] = session_id
    env["LLM_OBSIDIAN_WORKTREES"] = str(sandbox / ".vault-meta" / "acceptance-worktrees")
    env["DCG_CONFIG"] = str(sandbox / "config" / "dcg" / "task.toml")
    if surface:
        if SURFACE_RE.fullmatch(surface) is None:
            raise AcceptanceRunnerError("acceptance agent surface is invalid")
        env["CMUX_SURFACE_ID"] = surface
    if scratch_root is not None:
        for name in ("TMPDIR", "TMP", "TEMP"):
            env[name] = str(scratch_root)
    if runtime == "claude":
        return [
            "claude", "--permission-mode", "auto", "--add-dir", str(sandbox),
            "--plugin-dir", str(sandbox),
            "--disallowedTools", "AskUserQuestion",
            "--model", model, "--effort", effort, prompt,
        ], env
    socket = validated_cmux_socket_path()
    argv = [
        "codex", "--cd", str(sandbox), "-a", "never", "-s", "workspace-write",
        "--disable", "hooks",
        "--add-dir", str(resolved_git_common_dir(sandbox)),
        "--model", model,
    ]
    for value in task_codex_config_values(socket, effort):
        argv.extend(["-c", value])
    dispatch_env = sandbox / ".codex" / "dispatch-env.toml"
    if dispatch_env.is_file() and sys.version_info >= (3, 11):
        import tomllib

        raw = tomllib.loads(dispatch_env.read_text(encoding="utf-8")).get("codex_dispatch", {})
        profile = str(raw.get("profile") or "").strip() if isinstance(raw, dict) else ""
        codex_home = str(raw.get("codex_home") or "").strip() if isinstance(raw, dict) else ""
        if profile:
            argv.extend(["--profile", profile])
        if codex_home:
            env["CODEX_HOME"] = str(Path(codex_home).expanduser().resolve())
    env["CMUX_SOCKET_PATH"] = str(socket)
    argv.append(prompt)
    return argv, env

def run_agent_process(spec_path: Path) -> int:
    spec = read_json(spec_path)
    run_dir = spec_path.parent.resolve()
    sandbox = Path(str(spec.get("sandbox") or "")).resolve()
    prompt_path = Path(str(spec.get("prompt_file") or "")).resolve()
    scratch_root = Path(str(spec.get("scratch_root") or "")).resolve()
    if sandbox.parent != run_dir or not (sandbox / ".acceptance-sandbox.json").is_file():
        raise AcceptanceRunnerError("acceptance sandbox is not bound to its run directory")
    if prompt_path != run_dir / "prompt.md" or not prompt_path.is_file():
        raise AcceptanceRunnerError("acceptance prompt is not operation-scoped")
    if scratch_root != scratch_root_for(run_dir) or not (scratch_root / ".acceptance-scratch.json").is_file():
        raise AcceptanceRunnerError("acceptance scratch directory is not operation-scoped")
    runtime = str(spec.get("runtime") or "")
    config = load_config(sandbox)
    route = config.runtime_default(runtime)
    if route["model"] != spec.get("model") or route["effort"] != spec.get("effort"):
        raise AcceptanceRunnerError("acceptance route drifted after preparation")
    argv, env = agent_argv(
        runtime,
        sandbox,
        route["model"],
        route["effort"],
        prompt_path.read_text(encoding="utf-8"),
        scratch_root=scratch_root,
        surface=str(spec.get("surface") or ""),
        session_id=str(spec.get("session_id") or ""),
    )
    try:
        launch_cwd = run_dir if runtime == "claude" else sandbox
        return subprocess.run(argv, cwd=launch_cwd, env=env, check=False).returncode
    finally:
        atomic_json(run_dir / "agent-exit.json", {"schema_version": 1, "finished": True})

def send_surface(surface: str, text: str, *, submit_key: str = "Enter") -> None:
    for argv in (
        ["cmux", "send", "--surface", surface, text],
        ["cmux", "send-key", "--surface", surface, submit_key],
    ):
        result = subprocess.run(argv, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise AcceptanceTransientError(
                "cmux-launch-transient",
                (result.stdout + result.stderr).strip() or "cmux send failed",
            )

def settled_outbox(
    outbox: Path,
    state: dict[str, Any],
    now: float,
) -> dict[str, Any] | None:
    """Return one stable JSON outbox, tolerating a bounded non-atomic write."""
    try:
        metadata = outbox.lstat()
    except FileNotFoundError:
        state.clear()
        return None
    except OSError as exc:
        raise AcceptanceRunnerError("acceptance outbox metadata is unreadable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AcceptanceRunnerError("acceptance outbox must be a regular non-symlink file")
    if metadata.st_size > OUTBOX_MAX_BYTES:
        raise AcceptanceRunnerError("acceptance outbox exceeds the bounded size limit")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(outbox, flags)
    except OSError as exc:
        raise AcceptanceRunnerError("acceptance outbox is unreadable") from exc
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
        ):
            raise AcceptanceRunnerError("acceptance outbox changed identity while opening")
        payload = stream.read(OUTBOX_MAX_BYTES + 1)
        if len(payload) > OUTBOX_MAX_BYTES:
            raise AcceptanceRunnerError("acceptance outbox exceeds the bounded size limit")
    first_seen = float(state.setdefault("first_seen", now))
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        state.pop("digest", None)
        state.pop("stable_since", None)
        if now - first_seen >= OUTBOX_INVALID_GRACE_SECONDS:
            raise AcceptanceRunnerError(
                "acceptance outbox remained invalid after the bounded write grace period"
            ) from exc
        return None
    digest = hashlib.sha256(payload).hexdigest()
    if state.get("digest") != digest:
        state["digest"] = digest
        state["stable_since"] = now
        return None
    if now - float(state.get("stable_since", now)) < OUTBOX_STABLE_SECONDS:
        return None
    if not isinstance(parsed, dict):
        raise AcceptanceRunnerError("acceptance outbox must contain a JSON object")
    return parsed

def wait_for_outbox(
    outbox: Path, exit_marker: Path, timeout: int, *, surface: str, runtime: str,
    activity_paths: tuple[Path, ...] = (),
) -> dict[str, Any]:
    # Active work may run indefinitely. Only unchanged state consumes the
    # inactivity budget; the release contract probes at 15m and blocks at 20m.
    inactivity_timeout = min(1200.0, max(60.0, float(timeout)))
    probe_after = min(900.0, inactivity_timeout * 0.75)
    last_activity = time.monotonic()
    last_screen_digest = ""
    path_state: dict[str, tuple[int, int]] = {}
    probe_sent = False
    trust_accepted = False
    outbox_state: dict[str, Any] = {}
    while True:
        now = time.monotonic()
        candidate = settled_outbox(outbox, outbox_state, now)
        if candidate is not None:
            return candidate
        if exit_marker.is_file():
            raise AcceptanceRunnerError("acceptance agent exited before writing its outbox")
        screen = subprocess.run(
            ["cmux", "read-screen", "--surface", surface, "--lines", "80"],
            text=True,
            capture_output=True,
            check=False,
        )
        if screen.returncode == 0:
            digest = hashlib.sha256(screen.stdout.encode("utf-8", errors="replace")).hexdigest()
            if digest != last_screen_digest:
                last_screen_digest = digest
                last_activity = now
                probe_sent = False
                heartbeat("model-wait", counts={"screen_changes": 1})
            lowered = screen.stdout.lower()
            if any(token in lowered for token in (
                "selected model is at capacity",
                "rate limit exceeded",
                "usage limit reached",
                "too many requests",
            )):
                raise AcceptanceTransientError(
                    "agent-capacity", "agent runtime reported an explicit capacity or rate limit"
                )
            if not trust_accepted and workspace_trust_prompt_visible(runtime, screen.stdout):
                accepted = subprocess.run(
                    ["cmux", "send-key", "--surface", surface, "Enter"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if accepted.returncode != 0:
                    raise AcceptanceRunnerError("exact workspace trust prompt could not be accepted")
                trust_accepted = True
                last_activity = now
        for path in activity_paths:
            try:
                stat_value = path.stat()
            except OSError:
                continue
            value = (stat_value.st_mtime_ns, stat_value.st_size)
            key = str(path)
            if path_state.get(key) != value:
                path_state[key] = value
                last_activity = now
                probe_sent = False
                heartbeat("model-wait", counts={"lifecycle_changes": 1})
        idle = now - last_activity
        if idle >= probe_after and not probe_sent:
            send_surface(
                surface,
                "Acceptance status probe: report a concise status, then continue the task without waiting.",
            )
            probe_sent = True
            heartbeat("model-wait", status="probe")
        if idle >= inactivity_timeout:
            raise AcceptanceRunnerError("acceptance agent exceeded its configured inactivity window")
        time.sleep(1)

def close_surface(
    surface: str, runtime: str, exit_marker: Path, *, force: bool = False
) -> str:
    if force:
        try:
            close_surface_exact(surface, subprocess.run)
        except (TaskSessionError, OSError):
            return "exact surface close failed; surface left visible"
        return "exact surface closed"
    if not exit_marker.is_file():
        try:
            if runtime == "codex":
                for _ in range(40):
                    subprocess.run(["cmux", "send-key", "--surface", surface, "backspace"], capture_output=True, check=False)
                send_surface(surface, "/exit", submit_key="tab")
                subprocess.run(["cmux", "send-key", "--surface", surface, "Enter"], capture_output=True, check=False)
            else:
                send_surface(surface, "/exit")
        except AcceptanceRunnerError:
            return "exit-request-failed; surface left visible"
    deadline = time.monotonic() + AGENT_EXIT_GRACE_SECONDS
    exit_confirmation_sent = False
    while time.monotonic() < deadline and not exit_marker.is_file():
        if runtime == "claude" and not exit_confirmation_sent:
            screen = subprocess.run(
                ["cmux", "read-screen", "--surface", surface, "--lines", "40"],
                text=True,
                capture_output=True,
                check=False,
            )
            if (
                screen.returncode == 0
                and claude_background_exit_prompt_visible(screen.stdout)
            ):
                confirmed = subprocess.run(
                    ["cmux", "send-key", "--surface", surface, "Enter"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if confirmed.returncode != 0:
                    return "exit-confirmation-failed; surface left visible"
                exit_confirmation_sent = True
        time.sleep(0.5)
    if not exit_marker.is_file():
        return "agent did not exit; surface left visible"
    try:
        close_surface_exact(surface, subprocess.run)
    except (TaskSessionError, OSError):
        return "exact surface close failed; surface left visible"
    return "exact surface closed"

def operation_child_surfaces(sandbox: Path, coordinator_surface: str) -> set[str]:
    """Return exact child surfaces durably bound to this coordinator."""
    surfaces: set[str] = set()
    task_root = sandbox / ".vault-meta" / "task-sessions"
    candidates = list(task_root.glob("projects/*/tasks/*/lanes/*/operations/*/state.json"))
    candidates.extend((sandbox / ".vault-meta" / "research-runs").glob("*/state.json"))
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            state = read_json(path)
        except AcceptanceRunnerError:
            continue
        if state.get("coordinator_surface") != coordinator_surface:
            continue
        for key in ("surface", "fetch_surface", "synth_surface"):
            value = str(state.get(key) or "")
            if value != coordinator_surface and SURFACE_RE.fullmatch(value):
                surfaces.add(value)
    for path in (sandbox / ".vault-meta" / "acceptance-worktrees").glob("*/.task-meta.json"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            task_meta = read_json(path)
        except AcceptanceRunnerError:
            continue
        if task_meta.get("wiki_surface") != coordinator_surface:
            continue
        task_surface = str(task_meta.get("task_surface") or "")
        if task_surface != coordinator_surface and SURFACE_RE.fullmatch(task_surface):
            surfaces.add(task_surface)
    return surfaces

def surface_is_open(surface: str) -> bool:
    result = subprocess.run(
        ["cmux", "read-screen", "--surface", surface, "--lines", "1"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    output = (result.stdout + result.stderr).lower()
    return not any(token in output for token in ("not found", "not_found", "unknown surface"))

def wait_for_operation_children(
    sandbox: Path, coordinator_surface: str, grace_seconds: float = CHILD_SURFACE_SETTLE_SECONDS
) -> None:
    """Give armed task/reviewer wrappers a bounded chance to close themselves."""
    surfaces = operation_child_surfaces(sandbox, coordinator_surface)
    if not surfaces or grace_seconds <= 0:
        return
    deadline = time.monotonic() + grace_seconds
    while any(surface_is_open(surface) for surface in surfaces):
        if time.monotonic() >= deadline:
            return
        time.sleep(0.25)

def close_operation_children(sandbox: Path, coordinator_surface: str) -> tuple[int, list[str]]:
    """Close only exact child surfaces durably bound to this coordinator."""
    closed = 0
    failures: list[str] = []
    surfaces = operation_child_surfaces(sandbox, coordinator_surface)
    for surface in sorted(surfaces):
        try:
            status = close_surface_exact(surface, subprocess.run)
        except (TaskSessionError, OSError):
            failures.append(surface)
        else:
            if status == "closed":
                closed += 1
            elif status != "already-gone":
                failures.append(surface)
    return closed, failures

def settle_operation_surfaces(
    sandbox: Path,
    coordinator_surface: str,
    runtime: str,
    exit_marker: Path,
    *,
    force: bool = False,
) -> tuple[str, int, list[str]]:
    """Stop child creation before enumerating exact operation descendants."""
    coordinator_close = close_surface(
        coordinator_surface, runtime, exit_marker, force=force
    )
    if not force:
        wait_for_operation_children(sandbox, coordinator_surface)
    children_closed, child_failures = close_operation_children(sandbox, coordinator_surface)
    return coordinator_close, children_closed, child_failures
