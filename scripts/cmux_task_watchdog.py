#!/usr/bin/env python3
"""Observe unattended cmux agents and notify without sending input or stopping them."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from task_contract import ContractError, normalize, normalize_for_runtime


STATE_FILES = {"task": ".task-watchdog.json", "reviewer": ".review-watchdog.json"}
LOCK_FILES = {"task": ".task-watchdog.lock", "reviewer": ".review-watchdog.lock"}
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
DECORATIVE_RE = re.compile(r"^[\s─━═╭╮╰╯├┤┬┴┼│▏▎▍▌▋▊▉█░▒▓]+$")
VOLATILE_STATUS_RE = re.compile(
    r"(?:\bCTX\b|\b5H\b|\b7D\b|reset in|five-hour-limit|weekly-limit|"
    r"don't ask on|bypass permissions on|/effort|\bTip:)",
    re.IGNORECASE,
)
NOTIFY_RETRY_SECONDS = 300


class WatchdogError(RuntimeError):
    pass


@dataclass(frozen=True)
class Route:
    task_name: str
    kind: str
    surface: str
    coordinator_surface: str
    runtime: str
    policy: dict[str, Any]


def die(message: str, code: int = 2) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise WatchdogError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WatchdogError(f"{path} must contain an object")
    return value


def atomic_tmp_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.tmp.{os.getpid()}")


def write_json(path: Path, value: dict[str, Any]) -> None:
    tmp = atomic_tmp_path(path)
    try:
        tmp.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def iso_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False)


def resolve_route(worktree: Path, kind: str, surface: str) -> Route:
    meta = read_json(worktree / ".task-meta.json")
    try:
        task_policy = (
            normalize_for_runtime(meta, worktree)
            if kind == "task"
            else normalize(meta)
        )
    except ContractError as exc:
        raise WatchdogError(str(exc)) from exc
    watchdog = dict(task_policy.get("watchdog_policy") or {})
    task_name = str(meta.get("task_name") or "task").strip()
    if kind == "task":
        expected = str(meta.get("task_surface") or "").strip()
        coordinator = str(meta.get("wiki_surface") or "").strip()
        runtime = str(meta.get("executor_runtime") or meta.get("runtime") or "").strip()
    else:
        review = read_json(worktree / ".review-meta.json")
        expected = str(review.get("review_surface") or "").strip()
        coordinator = str(review.get("executor_surface") or "").strip()
        runtime = str(review.get("reviewer_runtime") or "").strip()
    if surface != expected or not surface:
        raise WatchdogError(f"{kind} watchdog surface does not match metadata")
    if not coordinator:
        raise WatchdogError(f"{kind} watchdog coordinator surface is missing")
    if runtime not in {"claude", "codex"}:
        raise WatchdogError(f"{kind} watchdog runtime is invalid")
    return Route(task_name, kind, surface, coordinator, runtime, watchdog)


def normalized_screen(text: str) -> str:
    """Remove volatile status-only lines while preserving visible agent progress."""
    kept: list[str] = []
    for raw in text.splitlines():
        line = ANSI_RE.sub("", raw).strip()
        if not line or DECORATIVE_RE.fullmatch(line) or VOLATILE_STATUS_RE.search(line):
            continue
        kept.append(re.sub(r"\s+", " ", line))
    return "\n".join(kept[-160:])


def screen_hash(text: str) -> str:
    return hashlib.sha256(normalized_screen(text).encode("utf-8")).hexdigest()


def read_screen(surface: str) -> tuple[str, bool]:
    result = run(["cmux", "read-screen", "--surface", surface])
    if result.returncode == 0:
        return result.stdout, True
    error = (result.stdout + result.stderr).lower()
    if "not_found" in error or "not found" in error:
        return "", False
    raise WatchdogError("cmux screen sampling failed")


def walk_processes(processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    stack = list(processes)
    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        found.append(item)
        children = item.get("children")
        if isinstance(children, list):
            stack.extend(child for child in children if isinstance(child, dict))
    return found


def agent_cpu_percent(surface: str, runtime: str) -> float | None:
    """Return advisory CPU only; CPU never suppresses a stale-screen alert."""
    result = run(["cmux", "top", "--all", "--processes", "--json"])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    needles = {runtime, "node"}
    for window in payload.get("windows", []):
        for workspace in window.get("workspaces", []):
            for pane in workspace.get("panes", []):
                for item in pane.get("surfaces", []):
                    processes = walk_processes(item.get("processes", []))
                    if not any(proc.get("cmux_surface_id") == surface for proc in processes):
                        continue
                    total = 0.0
                    for proc in processes:
                        identity = f"{proc.get('name', '')} {proc.get('path', '')}".lower()
                        if not any(needle in identity for needle in needles):
                            continue
                        resources = proc.get("resources")
                        if isinstance(resources, dict):
                            try:
                                total += float(resources.get("cpu_percent") or 0.0)
                            except (TypeError, ValueError):
                                pass
                    return round(total, 3)
    return None


def notify(route: Route, stage: str, idle_seconds: int) -> bool:
    label = f"{route.runtime.capitalize()} {route.kind} session {route.task_name}"
    minutes = max(1, idle_seconds // 60)
    if stage == "warning":
        title = f"{label} may be idle"
        body = (
            f"No visible screen change for {minutes} min. Watchdog is observing only; "
            f"it sent no input and stopped nothing. Surface {route.surface[:8]}."
        )
    elif stage == "alert":
        title = f"{label} may be stalled"
        body = (
            f"Still no visible screen change after {minutes} min. Inspect the surface before "
            "asking for status; do not cancel a visibly active agent."
        )
    elif stage == "recovered":
        title = f"{label} resumed"
        body = "Visible progress resumed; the prior watchdog alert is cleared."
    elif stage == "sampling-recovered":
        title = f"{label} watchdog sampling restored"
        body = "Screen sampling recovered; visible progress state is unchanged and its stall timer was preserved."
    else:
        title = f"{label} watchdog degraded"
        body = "Screen sampling failed repeatedly. The agent was not interrupted or closed."
    result = run(
        [
            "cmux", "notify", "--surface", route.coordinator_surface,
            "--title", title, "--body", body,
        ]
    )
    return result.returncode == 0


def fresh_state(route: Route, now: float) -> dict[str, Any]:
    return {
        "version": 1,
        "kind": route.kind,
        "task_name": route.task_name,
        "runtime": route.runtime,
        "surface": route.surface,
        "coordinator_surface": route.coordinator_surface,
        "status": "starting",
        "started_at": iso_time(now),
        "started_epoch": now,
        "last_progress_at": iso_time(now),
        "last_progress_epoch": now,
        "last_sample_at": iso_time(now),
        "last_sample_epoch": now,
        "unchanged_seconds": 0,
        "samples": 0,
        "read_failures": 0,
        "notification_failures": 0,
        "warning_sent": False,
        "alert_sent": False,
        "degraded_sent": False,
        "recovery_count": 0,
        "sampling_recovery_count": 0,
        "screen_sha256": "",
        "agent_cpu_percent": None,
    }


def load_state(path: Path, route: Route, now: float, reset: bool) -> dict[str, Any]:
    if reset or not path.is_file():
        return fresh_state(route, now)
    try:
        state = read_json(path)
    except WatchdogError:
        return fresh_state(route, now)
    if state.get("version") != 1 or state.get("kind") != route.kind or state.get("surface") != route.surface:
        return fresh_state(route, now)
    return state


def notification_due(state: dict[str, Any], stage: str, now: float) -> bool:
    if state.get(f"{stage}_sent") is True:
        return False
    last = state.get(f"{stage}_attempt_epoch")
    return not isinstance(last, (int, float)) or now - float(last) >= NOTIFY_RETRY_SECONDS


def attempt_notification(
    state: dict[str, Any], route: Route, stage: str, idle_seconds: int, now: float
) -> None:
    if not notification_due(state, stage, now):
        return
    state[f"{stage}_attempt_epoch"] = now
    if notify(route, stage, idle_seconds):
        state[f"{stage}_sent"] = True
    else:
        state["notification_failures"] = int(state.get("notification_failures") or 0) + 1


def sample_once(worktree: Path, kind: str, surface: str, now: float, reset: bool = False) -> str:
    route = resolve_route(worktree, kind, surface)
    state_path = worktree / STATE_FILES[kind]
    state = load_state(state_path, route, now, reset)
    if route.policy.get("enabled") is not True:
        state.update({"status": "disabled", "last_sample_at": iso_time(now), "last_sample_epoch": now})
        write_json(state_path, state)
        return "stop"

    try:
        screen, exists = read_screen(surface)
    except WatchdogError:
        state["samples"] = int(state.get("samples") or 0) + 1
        state["read_failures"] = int(state.get("read_failures") or 0) + 1
        state.update({"last_sample_at": iso_time(now), "last_sample_epoch": now})
        if state["read_failures"] >= 3:
            state["status"] = "degraded"
            attempt_notification(state, route, "degraded", 0, now)
        write_json(state_path, state)
        return "continue"

    if not exists:
        state.update({"status": "surface-gone", "last_sample_at": iso_time(now), "last_sample_epoch": now})
        write_json(state_path, state)
        return "stop"

    fingerprint = screen_hash(screen)
    previous = str(state.get("screen_sha256") or "")
    had_alert = state.get("warning_sent") is True or state.get("alert_sent") is True
    had_degraded = state.get("degraded_sent") is True
    state["samples"] = int(state.get("samples") or 0) + 1
    state["read_failures"] = 0
    state["screen_sha256"] = fingerprint
    state.update({"last_sample_at": iso_time(now), "last_sample_epoch": now})

    if not previous or fingerprint != previous:
        if had_alert or had_degraded:
            if notify(route, "recovered", 0):
                state["recovery_count"] = int(state.get("recovery_count") or 0) + 1
            else:
                state["notification_failures"] = int(state.get("notification_failures") or 0) + 1
        state.update(
            {
                "status": "running",
                "last_progress_at": iso_time(now),
                "last_progress_epoch": now,
                "unchanged_seconds": 0,
                "warning_sent": False,
                "alert_sent": False,
                "degraded_sent": False,
                "agent_cpu_percent": None,
            }
        )
        for stage in ("warning", "alert", "degraded"):
            state.pop(f"{stage}_attempt_epoch", None)
        write_json(state_path, state)
        return "continue"

    idle = max(0, int(now - float(state.get("last_progress_epoch") or now)))
    state["unchanged_seconds"] = idle
    warn_after = int(route.policy["warn_after_seconds"])
    alert_after = int(route.policy["alert_after_seconds"])
    if had_degraded:
        if notify(route, "sampling-recovered", 0):
            state["sampling_recovery_count"] = int(state.get("sampling_recovery_count") or 0) + 1
        else:
            state["notification_failures"] = int(state.get("notification_failures") or 0) + 1
        state["degraded_sent"] = False
        state.pop("degraded_attempt_epoch", None)
    if idle >= alert_after:
        state["status"] = "stalled"
        state["warning_sent"] = True
        if notification_due(state, "alert", now):
            state["agent_cpu_percent"] = agent_cpu_percent(surface, route.runtime)
        attempt_notification(state, route, "alert", idle, now)
    elif idle >= warn_after:
        state["status"] = "warning"
        if notification_due(state, "warning", now):
            state["agent_cpu_percent"] = agent_cpu_percent(surface, route.runtime)
        attempt_notification(state, route, "warning", idle, now)
    else:
        state["status"] = "running"
    write_json(state_path, state)
    return "continue"


def record_terminal_status(worktree: Path, kind: str, status: str, now: float) -> None:
    path = worktree / STATE_FILES[kind]
    try:
        state = read_json(path)
    except WatchdogError:
        state = {"version": 1, "kind": kind}
    state.update({"status": status, "stopped_at": iso_time(now), "stopped_epoch": now})
    write_json(path, state)


def safe_sample(
    worktree: Path, route: Route, now: float, *, reset: bool = False
) -> str:
    try:
        return sample_once(worktree, route.kind, route.surface, now, reset)
    except (WatchdogError, OSError, ValueError):
        try:
            state_path = worktree / STATE_FILES[route.kind]
            state = load_state(state_path, route, now, False)
            state.update({"status": "failed", "last_sample_at": iso_time(now), "last_sample_epoch": now})
            attempt_notification(state, route, "degraded", 0, now)
            write_json(state_path, state)
        except (WatchdogError, OSError, ValueError):
            pass
        return "stop"


def run_loop(worktree: Path, kind: str, surface: str) -> int:
    lock_path = worktree / LOCK_FILES[kind]
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("r+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            route = resolve_route(worktree, kind, surface)
        except WatchdogError:
            record_terminal_status(worktree, kind, "contract-error", time.time())
            return 0
        if route.policy.get("enabled") is not True:
            safe_sample(worktree, route, time.time(), reset=True)
            return 0

        stopped = threading.Event()

        def stop_handler(_signum: int, _frame: object) -> None:
            stopped.set()

        signal.signal(signal.SIGTERM, stop_handler)
        signal.signal(signal.SIGINT, stop_handler)
        outcome = safe_sample(worktree, route, time.time(), reset=True)
        poll = int(route.policy["poll_seconds"])
        while outcome == "continue" and not stopped.wait(poll):
            outcome = safe_sample(worktree, route, time.time())
        if outcome != "stop":
            record_terminal_status(worktree, kind, "stopped", time.time())
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "sample"):
        command = sub.add_parser(name)
        command.add_argument("--worktree", default=".")
        command.add_argument("--kind", choices=sorted(STATE_FILES), required=True)
        command.add_argument("--surface", required=True)
        if name == "sample":
            command.add_argument("--now", type=float, default=None)
            command.add_argument("--reset", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--worktree", default=".")
    status.add_argument("--kind", choices=sorted(STATE_FILES), required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    worktree = Path(args.worktree).expanduser().resolve()
    if args.command == "status":
        print(json.dumps(read_json(worktree / STATE_FILES[args.kind]), indent=2, sort_keys=True))
        return 0
    if args.command == "run":
        return run_loop(worktree, args.kind, args.surface)
    now = time.time() if args.now is None else args.now
    if now < 0:
        die("--now must be non-negative")
    try:
        outcome = sample_once(worktree, args.kind, args.surface, now, args.reset)
    except (WatchdogError, OSError, ValueError) as exc:
        die(str(exc))
    print(outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
