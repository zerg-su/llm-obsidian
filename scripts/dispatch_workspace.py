"""Dispatch worktree, runtime sync, and task handoff effects."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from lifecycle_telemetry import emit_lifecycle_event
from task_contract import ContractError, normalize as normalize_task_contract
from harness.git_ops import GitAdapter, GitError
from harness.finalization_policy import (
    FinalizationPolicy,
    finalization_policy_payload,
)
from dispatch_contracts import TASK_LOCAL_GIT_EXCLUDES, _validated_context
from dispatch_custom_contracts import (
    approved_outcome_contract_sha256,
    approved_plan_file,
    approved_plan_sha256,
    custom_contract_for_request,
    execution_pipeline_for_request,
    task_pipeline_policy,
)
from dispatch_io import DispatchError, atomic_json, atomic_text, utc_now
from dispatch_setup import render_task_prompt, review_policy, review_topology_preview
from approved_plan_snapshot import bind_approved_plan_snapshot
from harness.dashboard_facade import facade_dashboard_command


def run_command(
    argv: list[str], *, cwd: Path | None = None, input_text: str | None = None,
    env: dict[str, str] | None = None, label: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            input=input_text,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise DispatchError(f"{label} could not start: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1][:300]}" if detail else ""
        raise DispatchError(f"{label} failed{suffix}")
    return result


def observer_command(vault_root: Path, request_id: str) -> list[str]:
    """Exact temporary observer argv before durable task-root creation.

    The caller appends its own exact coordinator surface. The observer stays
    outside Harness ownership and carries no lifecycle authority, so this
    argv can neither reorder validate/start nor block an approved pipeline.
    """

    root = Path(vault_root)
    return facade_dashboard_command(
        vault=root,
        store=root / ".vault-meta" / "harness",
        caller_surface="",
        facade="dispatch",
        request_id=request_id,
    )


def ensure_task_git_excludes(worktree: Path) -> None:
    """Keep root task bindings out of only this worktree's Git status."""

    run_command(
        ["git", "config", "extensions.worktreeConfig", "true"],
        cwd=worktree,
        label="task Git worktree config",
    )
    raw_git_dir = run_command(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=worktree,
        label="task Git directory",
    ).stdout.strip()
    if not raw_git_dir:
        raise DispatchError("task Git directory is empty")
    git_dir = Path(raw_git_dir)
    exclude = git_dir / "info" / "task-exclude"
    if (
        exclude.parent.is_symlink()
        or exclude.is_symlink()
        or (exclude.exists() and not exclude.is_file())
    ):
        raise DispatchError("task Git exclude path is not a regular file")
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        current = handle.read()
        existing = set(current.splitlines())
        missing = [
            pattern
            for pattern in TASK_LOCAL_GIT_EXCLUDES
            if pattern not in existing
        ]
        if missing:
            handle.seek(0, os.SEEK_END)
            if current and not current.endswith("\n"):
                handle.write("\n")
            handle.write("".join(f"{pattern}\n" for pattern in missing))
            handle.flush()
            os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    run_command(
        [
            "git",
            "config",
            "--worktree",
            "core.excludesFile",
            str(exclude),
        ],
        cwd=worktree,
        label="task Git exclude config",
    )


def create_worktree(request: dict[str, Any]) -> None:
    try:
        GitAdapter(request["target_repo"]).create_worktree(
            request["worktree"],
            request["branch"],
            request["base_sha"],
        )
    except GitError as exc:
        raise DispatchError(f"worktree creation failed: {exc}") from exc


def initialize_task(request: dict[str, Any]) -> dict[str, str]:
    result = run_command(
        [
            sys.executable,
            str(request["vault_root"] / "scripts" / "task_sessions.py"),
            "--vault-root", str(request["vault_root"]),
            "init-task", "--worktree", str(request["worktree"]),
            "--task-id", request["request_id"],
            "--runtime", request["session_route"]["runtime"],
            "--session-id", request["origin_session"],
        ],
        label="task identity initialization",
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DispatchError("task identity initialization returned invalid JSON") from exc
    if value.get("task_id") != request["request_id"]:
        raise DispatchError("task identity initialization drifted from request_id")
    return {"project_id": str(value["project_id"]), "task_id": str(value["task_id"])}


def sync_codex_profile(request: dict[str, Any], config: dict[str, Any], effective: dict[str, Any]) -> None:
    if effective["runtime"] != "codex":
        return
    target_root = request["target_repo"]
    profile_root = (
        target_root
        if (target_root / ".codex" / "dispatch-env.toml").is_file()
        else request["vault_root"]
    )
    gateway = profile_root / "scripts" / "mcp-gateway" / "mcp-gateway.sh"
    run_command(
        [str(gateway), "sync-config", "--apply"],
        cwd=profile_root,
        label="MCP config sync",
    )
    profile = str(config.get("profile") or "").strip()
    if not profile:
        return
    env = os.environ.copy()
    if config.get("codex_home"):
        env["CODEX_HOME"] = str(config["codex_home"])
    run_command(
        [str(gateway), "codex-sync", "--apply", "--only-profile", profile],
        cwd=profile_root,
        env=env,
        label="Codex dispatch profile sync",
    )


def finalization_policy_for_request(
    request: dict[str, Any],
) -> dict[str, Any]:
    """Project the exact approved custom policy or the compatibility default."""

    policy = FinalizationPolicy()
    if request.get("pipeline") == "custom":
        declared = custom_contract_for_request(request)[0].finalization_policy
        if declared is not None:
            policy = declared
    return finalization_policy_payload(policy)


def write_task_files(
    request: dict[str, Any], config: dict[str, Any], session: dict[str, Any],
    effective: dict[str, Any], identity: dict[str, str], origin: dict[str, str],
    child: dict[str, str],
) -> dict[str, Any]:
    request = bind_approved_plan_snapshot(request)
    worktree = request["worktree"]
    ensure_task_git_excludes(worktree)
    handoffs = {
        ".task-cmux-surface": child["surface"],
        ".task-origin-session": request["origin_session"],
        ".wiki-cmux-surface": origin["surface_id"],
        ".wiki-agent-runtime": request["session_route"]["runtime"],
        ".wiki-reap-command": config["reap_skill"],
        ".task-review-skill": config["review_skill"],
    }
    for name, value in handoffs.items():
        atomic_text(worktree / name, value + "\n")
    atomic_text(worktree / ".task-prompt.md", render_task_prompt(request, config))
    plan_hash = approved_plan_sha256(request)
    outcome_hash = approved_outcome_contract_sha256(request)
    review = review_policy(request, config)
    review_topology = review_topology_preview(request, review)
    meta: dict[str, Any] = {
        "version": 4,
        "project_id": identity["project_id"],
        "task_id": identity["task_id"],
        "task_name": request["task_name"],
        "wiki_runtime": request["session_route"]["runtime"],
        "executor_runtime": effective["runtime"],
        "runtime": effective["runtime"],
        "origin_session": request["origin_session"],
        "spawned_at": utc_now(),
        "wiki_surface": origin["surface_id"],
        "wiki_surface_ref": origin["surface_ref"],
        "task_surface": child["surface"],
        "task_surface_ref": child["surface_ref"],
        "task_workspace": child.get("workspace", ""),
        "task_workspace_ref": child.get("workspace_ref", ""),
        "task_window": child.get("window", ""),
        "task_window_ref": child.get("window_ref", ""),
        "worktree": str(worktree),
        "target_repo": str(request["target_repo"]),
        "vault_root": str(request["vault_root"]),
        "branch": request["branch"],
        "base_branch": request["base_branch"],
        "base_sha": request["base_sha"],
        "codex_home": config.get("codex_home") or None,
        "codex_profile": config.get("profile") or None,
        "wiki_reap_command": config["reap_skill"],
        "review_skill": config["review_skill"],
        "routing": {
            "schema_version": 1,
            "session": {
                "runtime": session["runtime"],
                "model": session["model"],
                "effort": session["effort"],
                "source": session["source"],
            },
            "effective": effective,
        },
        "plan_file": str(request["plan_file"]),
        "plan_snapshot_file": str(approved_plan_file(request)),
        "approved_plan_sha256": plan_hash,
        "outcome_contract_sha256": outcome_hash,
        "interaction_policy": "unattended",
        "finalization_policy": finalization_policy_for_request(request),
        "pipeline_policy": task_pipeline_policy(request),
        "review_policy": {
            "mode": review.mode,
            "cross_model": review.cross_model,
            "runtime": review.runtime,
            "model": review.model,
            "effort": review.effort,
            "max_verify_iterations": review.max_verify_iterations,
            "verification_profile": review.verification_profile,
            "verification_profile_sha256": (
                review.verification_profile_sha256
            ),
        },
        "reap_policy": {
            "mode": request["reap"]["plan_mode"],
            "auto_file": True,
            "allowed_types": [request["reap"]["type"]],
            "title": request["reap"]["title"],
        },
        "surface_policy": {
            "auto_close": config["auto_close_surfaces"],
            "placement": request["placement"],
        },
        "watchdog_policy": {
            "enabled": config["watchdog_enabled"],
            "poll_seconds": config["watchdog_poll_seconds"],
            "warn_after_seconds": config["watchdog_warn_after_seconds"],
            "alert_after_seconds": config["watchdog_alert_after_seconds"],
        },
        "forbidden_actions": [
            "push", "deploy", "publish", "delete-worktree", "delete-branch", "expand-scope",
        ],
        "suggested_agents": request["suggested_agents"],
    }
    if review.enabled:
        meta["review_topology"] = {
            "payload": review_topology["topology"],
            "sha256": review_topology["topology_sha256"],
        }
    if request.get("split") is not None:
        meta["split_policy"] = request["split"]
    if request["executor"]["model"]:
        meta["model"] = request["executor"]["model"]
    if request["executor"]["effort"]:
        meta["effort"] = request["executor"]["effort"]
    atomic_json(worktree / ".task-meta.json", meta)
    try:
        normalize_task_contract(meta)
    except ContractError as exc:
        raise DispatchError(f"rendered task contract is invalid: {exc}") from exc
    return meta


def dispatch_log(request: dict[str, Any], effective: dict[str, Any], child: dict[str, str]) -> None:
    context = _validated_context(
        {"wiki_context": request["wiki_context"]},
        request["vault_root"],
        request["plan_file"],
        None,
    )
    links = ", ".join(
        f"[[{PurePosixPath(item['context_path']).stem}|{item['title']}]]"
        for item in context
    ) or "none"
    entry = (
        f"## [{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}] dispatch | {request['task_name']}\n\n"
        f"Spawned an approved unattended task session (cmux `{child['surface']}`, runtime "
        f"{effective['runtime']}, model {effective['model']}) in {request['placement']} placement in worktree "
        f"`{request['worktree']}`. Target repo `{request['target_repo']}`, branch `{request['branch']}` "
        f"from `{request['base_branch']}`. Plan: `{request['plan_file']}`. Pre-loaded context: {links}. "
        "Awaiting typed review and final reap."
    )
    run_command(
        [sys.executable, str(request["vault_root"] / "scripts" / "vault-write.py")],
        cwd=request["vault_root"],
        input_text=json.dumps({"log_entry": entry}, ensure_ascii=False),
        label="dispatch log transaction",
    )
