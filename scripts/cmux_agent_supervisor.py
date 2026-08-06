#!/usr/bin/env python3
"""Run one interactive cmux agent with a watchdog and post-exit lifecycle.

This compatibility façade preserves the legacy migration-evidence import and
CLI surface while specs, safety policy, and trust/review watchers live in
focused collaborators.

Source-audit anchors retained by those collaborators include
``require_option(argv, "--disable", "hooks")``,
``Codex reviewer command must not request additional writable roots``,
``"--permission-mode", "dontAsk"``,
``CLAUDE_REVIEW_TOOL_SURFACE = "Read,Glob,Grep,Write,Bash"``,
``Edit(./.review-outbox.json)``, ``Write(./.review-outbox.json)``,
``Bash(python3 tests/test_*.py)``, ``Bash(bash tests/test_*.sh)``, and
``Bash(python3 scripts/lint-instructions.py)``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, NoReturn

from lifecycle_telemetry import emit_lifecycle_event, nonnegative_int, read_object
from model_routing import (
    RoutingError,
    load_config as load_routing_config,
    resolve as resolve_model_route,
    session_from_meta,
)
from task_contract import ContractError, normalize, read_json as read_task_json
from cmux_agent_support import (
    CODEX_EFFORTS,
    DEFAULT_CODEX_EFFORT,
    ROUTING_CONFIG as _ROUTING_CONFIG,
    SupervisorError,
    codex_automation_service_tier_config,
    codex_effort_config,
    codex_resume_cwd_config,
    resolved_git_common_dir,
    task_codex_config_values,
    validated_cmux_socket_path,
)
from cmux_trust_prompt import (
    claude_background_exit_prompt_visible,
    native_dialog_region,
    workspace_trust_prompt_visible,
)
from harness.adapters.cmux import run_cmux
from cmux_supervisor_contracts import (
    ALLOWED_ENV,
    ARMED_EXIT_POLL_SECONDS,
    CLAUDE_EFFORTS,
    CLAUDE_REVIEW_BASE_ALLOWED_TOOLS,
    CLAUDE_REVIEW_TOOL_SURFACE,
    CODEX_FORBIDDEN_OPTIONS,
    PROMPT_FILES,
    REVIEW_OUTBOX_FILE,
    REVIEW_RELAY_FILE,
    REVIEW_RELAY_POLL_SECONDS,
    REVIEW_RELAY_TIMEOUT_SECONDS,
    RUNTIME_COMMANDS,
    RUNTIME_DIRS,
    SCRIPT_DIR,
    SPEC_FILES,
    WORKSPACE_TRUST_POLL_SECONDS,
    WORKSPACE_TRUST_TIMEOUT_SECONDS,
    atomic_tmp_path,
    claude_review_allowed_tools,
    die,
    exact_spec_path,
    identify_caller,
    read_json,
    runtime_directory_is_stable,
    runtime_directory_is_trusted,
    task_dcg_config,
    trusted_claude_wrapper,
    trusted_runtime_path,
    validate_spec_shape,
    validate_trusted_runtime_path,
    validated_caller_identity,
    write_agent_spec,
    write_json,
)
from cmux_supervisor_policy import (
    append_task_codex_network_policy,
    expected_codex_home,
    option_value,
    option_values,
    require_option,
    resolved_task_model_route,
    reviewer_codex_config_values,
    validate_reviewer_safety,
    validate_routing,
    validate_task_safety,
    validated_review_runtime,
    validated_task_git_common_dir,
    validated_task_session_dir,
)
from cmux_supervisor_review import (
    auto_accept_workspace_trust,
    auto_confirm_armed_claude_exit,
    automatic_workspace_trust_allowed,
    claude_mcp_trust_prompt_visible,
    primary_coordinator_review,
    relay_review_outbox_once,
    relay_state,
    reviewer_uses_supervised_relay,
    run_review_relay,
    stop_watchdog,
)


def load_validated_spec(
    worktree: Path, state_dir: Path, kind: str, surface: str, raw_path: str = ""
) -> dict[str, Any]:
    spec = read_json(exact_spec_path(state_dir, kind, raw_path))
    validate_spec_shape(spec, kind)
    validate_routing(worktree, state_dir, kind, surface, spec)
    prompt_root = state_dir if kind == "reviewer" else worktree
    prompt = (prompt_root / PROMPT_FILES[kind]).resolve()
    try:
        prompt.relative_to(prompt_root)
    except ValueError as exc:
        raise SupervisorError("agent prompt resolves outside its state root") from exc
    if not prompt.is_file():
        raise SupervisorError(f"agent prompt is missing: {prompt}")
    return spec


def prepare_task(worktree: Path, surface: str) -> Path:
    meta = read_task_json(worktree / ".task-meta.json")
    try:
        policy = normalize(meta)
    except ContractError as exc:
        raise SupervisorError(str(exc)) from exc
    runtime = str(meta.get("executor_runtime") or meta.get("runtime") or "")
    if surface != str(meta.get("task_surface") or ""):
        raise SupervisorError("task preparation surface does not match metadata")
    route = resolved_task_model_route(worktree, meta, runtime)
    model = str(route["model"])
    effort = str(route["effort"])
    env: dict[str, str] = {
        "LLM_OBSIDIAN_PROJECT_ROOT": str(Path(str(meta.get("vault_root") or "")).expanduser().resolve()),
        "LLM_OBSIDIAN_SESSION_ROLE": "task",
    }
    if runtime == "codex":
        argv = ["codex", "--cd", str(worktree)]
        profile = str(meta.get("codex_profile") or "").strip()
        if profile:
            argv.extend(["--profile", profile])
        if effort not in CODEX_EFFORTS:
            raise SupervisorError(f"Codex task effort must be one of {sorted(CODEX_EFFORTS)}")
        argv.extend(["--model", model])
        if policy["interaction_policy"] == "unattended":
            cmux_socket = validated_cmux_socket_path()
            argv.extend(["--add-dir", str(validated_task_git_common_dir(worktree, meta))])
            task_session_dir = validated_task_session_dir(meta)
            if task_session_dir is not None:
                argv.extend(["--add-dir", str(task_session_dir)])
            argv.extend(["-a", "never", "-s", "workspace-write"])
            append_task_codex_network_policy(argv, cmux_socket, effort)
            env["CMUX_SOCKET_PATH"] = str(cmux_socket)
        else:
            argv.extend(["-c", codex_automation_service_tier_config()])
            argv.extend(["-c", codex_resume_cwd_config()])
            argv.extend(["-c", codex_effort_config(effort)])
        codex_home = str(meta.get("codex_home") or "").strip()
        if codex_home:
            env["CODEX_HOME"] = str(Path(codex_home).expanduser().resolve())
    elif runtime == "claude":
        if effort not in CLAUDE_EFFORTS:
            raise SupervisorError(f"Claude task effort must be one of {sorted(CLAUDE_EFFORTS)}")
        argv = [
            "claude", "--permission-mode", "auto",
            "--model", model,
            "--effort", effort,
        ]
    else:
        raise SupervisorError("task executor runtime must be claude or codex")
    if policy["interaction_policy"] == "unattended":
        env["DCG_CONFIG"] = str(task_dcg_config(meta))
    return write_agent_spec(worktree, "task", runtime, argv, PROMPT_FILES["task"], env)


def run_agent(
    worktree: Path, state_dir: Path, kind: str, surface: str, raw_spec: str = ""
) -> int:
    spec = load_validated_spec(worktree, state_dir, kind, surface, raw_spec)
    started = time.monotonic()
    prompt_root = state_dir if kind == "reviewer" else worktree
    prompt = (prompt_root / spec["prompt_file"]).read_text(encoding="utf-8")
    argv = [*spec["argv"], prompt]
    if spec["runtime"] == "claude":
        wrapper = trusted_claude_wrapper(surface)
        if wrapper is not None:
            argv[0] = str(wrapper)
    env = os.environ.copy()
    env.update(spec["env"])
    watchdog: subprocess.Popen[bytes] | None = None
    relay_stop: threading.Event | None = None
    relay_thread: threading.Thread | None = None
    trust_stop: threading.Event | None = None
    trust_thread: threading.Thread | None = None
    trust_state: dict[str, int] = {}
    exit_stop: threading.Event | None = None
    exit_thread: threading.Thread | None = None
    exit_state: dict[str, int] = {}
    review_runtime: Path | None = None
    agent_rc = 127
    try:
        if kind == "reviewer":
            review_meta = read_json(state_dir / ".review-meta.json")
            raw_review_runtime = str(review_meta.get("review_runtime_dir") or "").strip()
            review_runtime = (
                validated_review_runtime(worktree, review_meta)
                if raw_review_runtime
                else worktree
            )
            if reviewer_uses_supervised_relay(spec["runtime"]):
                relay_stop = threading.Event()
                relay_thread = threading.Thread(
                    target=run_review_relay,
                    args=(worktree, state_dir, review_runtime, relay_stop),
                    name="review-outbox-relay",
                    daemon=True,
                )
                relay_thread.start()
        watchdog = subprocess.Popen(
            [
                sys.executable, str(SCRIPT_DIR / "cmux_task_watchdog.py"), "run",
                "--worktree", str(worktree), "--kind", kind, "--surface", surface,
                "--state-dir", str(state_dir),
            ],
            cwd=worktree,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if sys.stdout.isatty():
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        if automatic_workspace_trust_allowed(worktree, state_dir, kind):
            trust_stop = threading.Event()
            trust_thread = threading.Thread(
                target=auto_accept_workspace_trust,
                args=(surface, spec["runtime"], trust_stop, trust_state),
                name="workspace-trust-bootstrap",
                daemon=True,
            )
            trust_thread.start()
            if spec["runtime"] == "claude":
                exit_stop = threading.Event()
                exit_thread = threading.Thread(
                    target=auto_confirm_armed_claude_exit,
                    args=(worktree, state_dir, kind, surface, exit_stop, exit_state),
                    name="armed-exit-confirmation",
                    daemon=True,
                )
                exit_thread.start()
        agent_cwd = review_runtime or worktree
        agent_rc = subprocess.run(argv, cwd=agent_cwd, env=env, check=False).returncode
    except KeyboardInterrupt:
        agent_rc = 130
    except OSError as exc:
        print(f"ERROR: cannot start {spec['runtime']} agent: {exc}", file=sys.stderr)
    finally:
        stop_watchdog(watchdog)
        if relay_stop is not None:
            relay_stop.set()
        if relay_thread is not None:
            relay_thread.join(timeout=REVIEW_RELAY_TIMEOUT_SECONDS + 1)
        if trust_stop is not None:
            trust_stop.set()
        if trust_thread is not None:
            trust_thread.join(timeout=3)
        if exit_stop is not None:
            exit_stop.set()
        if exit_thread is not None:
            exit_thread.join(timeout=3)

    lifecycle = subprocess.run(
        [
            sys.executable, str(SCRIPT_DIR / "cmux_surface_lifecycle.py"), "after-exit",
            "--worktree", str(worktree), "--kind", kind, "--surface", surface,
            "--state-dir", str(state_dir),
        ],
        cwd=worktree,
        check=False,
    )
    watchdog_state = read_object(
        state_dir / (".review-watchdog.json" if kind == "reviewer" else ".task-watchdog.json")
    )
    relay = read_object(state_dir / REVIEW_RELAY_FILE) if kind == "reviewer" else {}
    normalized_agent_rc = nonnegative_int(agent_rc)
    normalized_lifecycle_rc = nonnegative_int(lifecycle.returncode)
    counts = {
        "duration_ms": round((time.monotonic() - started) * 1000),
        "agent_exit_code": normalized_agent_rc,
        "agent_signal": abs(agent_rc) if agent_rc < 0 else 0,
        "lifecycle_exit_code": normalized_lifecycle_rc,
        "lifecycle_signal": abs(lifecycle.returncode) if lifecycle.returncode < 0 else 0,
        "watchdog_warnings": nonnegative_int(watchdog_state.get("warning_count")),
        "watchdog_alerts": nonnegative_int(watchdog_state.get("alert_count")),
        "watchdog_degraded": nonnegative_int(watchdog_state.get("degraded_count")),
        "watchdog_recoveries": nonnegative_int(watchdog_state.get("recovery_count")),
        "watchdog_sampling_recoveries": nonnegative_int(
            watchdog_state.get("sampling_recovery_count")
        ),
        "watchdog_read_failures": nonnegative_int(watchdog_state.get("read_failure_count")),
        "watchdog_notification_failures": nonnegative_int(
            watchdog_state.get("notification_failures")
        ),
        "relay_sent": nonnegative_int(relay.get("sent_count")),
        "relay_failures": nonnegative_int(relay.get("failure_count")),
        "workspace_trust_accepts": nonnegative_int(trust_state.get("accepts")),
        "workspace_mcp_declines": nonnegative_int(trust_state.get("mcp_declines")),
        "workspace_trust_read_failures": nonnegative_int(trust_state.get("read_failures")),
        "workspace_trust_send_failures": nonnegative_int(trust_state.get("send_failures")),
        "armed_exit_confirms": nonnegative_int(exit_state.get("confirms")),
        "armed_exit_read_failures": nonnegative_int(exit_state.get("read_failures")),
        "armed_exit_send_failures": nonnegative_int(exit_state.get("send_failures")),
    }
    emit_lifecycle_event(
        worktree,
        "agent-run",
        actor=f"{kind}:{spec['runtime']}",
        counts=counts,
        status="ok" if agent_rc == 0 and lifecycle.returncode == 0 else "error",
    )
    return agent_rc if agent_rc != 0 else lifecycle.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare-task")
    prepare.add_argument("--worktree", default=".")
    prepare.add_argument("--surface", required=True)
    identify = sub.add_parser("identify-caller")
    identify.add_argument("--surface", required=True)
    for name in ("validate", "run"):
        command = sub.add_parser(name)
        command.add_argument("--worktree", default=".")
        command.add_argument("--state-dir", default="")
        command.add_argument("--kind", choices=sorted(SPEC_FILES), required=True)
        command.add_argument("--surface", required=True)
        command.add_argument("--spec", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    worktree = Path(getattr(args, "worktree", ".")).expanduser().resolve()
    state_dir = Path(getattr(args, "state_dir", "") or worktree).expanduser().resolve()
    try:
        if args.command == "identify-caller":
            print(json.dumps(identify_caller(args.surface), sort_keys=True))
            return 0
        if args.command == "prepare-task":
            print(prepare_task(worktree, args.surface))
            return 0
        if args.command == "validate":
            spec = load_validated_spec(worktree, state_dir, args.kind, args.surface, args.spec)
            print(shlex.join([*spec["argv"], f"<{spec['prompt_file']}>"]))
            return 0
        return run_agent(worktree, state_dir, args.kind, args.surface, args.spec)
    except (ContractError, SupervisorError, OSError, ValueError) as exc:
        die(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
