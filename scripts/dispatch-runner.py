#!/usr/bin/env python3
"""Deterministic post-approval runner for one dispatch task split.

The coordinator still owns natural-language parsing, context selection, and the
single user approval.  This runner owns the repetitive stateful mechanics after
that approval: route capture, worktree creation, prompt/metadata rendering,
anchored cmux spawn, supervisor launch, and the dispatch log entry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from model_routing import (  # noqa: E402
    RoutingError,
    capture_session,
    load_config,
    resolve,
    routing_from_environment,
)
from task_contract import ContractError, normalize as normalize_task_contract  # noqa: E402
from task_sessions import TaskSessionError, spawn_right  # noqa: E402
from lifecycle_telemetry import emit_lifecycle_event  # noqa: E402


TASK_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
RUNTIMES = {"claude", "codex"}
REVIEW_MODES = {"light", "full", "skip"}
SUMMARY_TYPES = {"session", "decision", "runbook", "incident", "service-update", "repo-touch"}
RUN_STATES = {"preparing", "launched", "failed"}
COORDINATOR_ACTION = "return-to-idle-without-polling"
DEFAULT_DISPATCH = {
    "codex_home": "",
    "profile": "",
    "reap_skill": "$llm-obsidian:reap",
    "reap_send_skill": "$llm-obsidian:reap-send",
    "review_skill": "$llm-obsidian:review-dispatch",
    "review_send_skill": "$llm-obsidian:review-send",
    "interaction_policy": "unattended",
    "review_mode": "light",
    "max_verify_iterations": 2,
    "auto_close_surfaces": True,
    "default_reap_type": "session",
    "watchdog_enabled": True,
    "watchdog_poll_seconds": 30,
    "watchdog_warn_after_seconds": 900,
    "watchdog_alert_after_seconds": 1200,
}


class DispatchError(ValueError):
    pass


def die(message: str, code: int = 3) -> NoReturn:
    print(f"dispatch-runner: {message}", file=sys.stderr)
    raise SystemExit(code)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DispatchError(f"missing JSON file: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DispatchError(f"JSON root must be an object: {path}")
    return value


def ensure_owned_dir(path: Path) -> None:
    if path.exists():
        info = path.stat()
        if path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid():
            raise DispatchError(f"runtime directory is not owned by the current user: {path}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            path.chmod(0o700)
    else:
        path.mkdir(parents=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise DispatchError(f"runtime directory is not owned by the current user: {path}")


def atomic_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        path.chmod(mode)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_text(
        path,
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def exclusive_json(path: Path, value: dict[str, Any]) -> None:
    """Create one durable claim without a check-then-create race."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def absolute_dir(value: Any, field: str, *, must_exist: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise DispatchError(f"{field} must be a non-empty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise DispatchError(f"{field} must be a non-empty absolute path")
    path = path.resolve()
    if must_exist and not path.is_dir():
        raise DispatchError(f"{field} directory is missing: {path}")
    return path


def absolute_file(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise DispatchError(f"{field} must be a non-empty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise DispatchError(f"{field} must be a non-empty absolute path")
    path = path.resolve()
    if not path.is_file():
        raise DispatchError(f"{field} file is missing: {path}")
    return path


def require_string(value: Any, field: str, *, maximum: int = 10_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DispatchError(f"{field} must be a non-empty string")
    value = value.strip()
    if "\0" in value or len(value) > maximum:
        raise DispatchError(f"{field} is invalid")
    return value


def materialize_current_context(raw: dict[str, Any]) -> dict[str, Any]:
    """Resolve process-bound coordinator identity without guessing globally."""
    value = dict(raw)
    vault_root = absolute_dir(value.get("vault_root"), "vault_root")
    if not str(value.get("origin_surface") or "").strip():
        surface = str(os.environ.get("CMUX_SURFACE_ID") or "").strip()
        if not surface:
            raise DispatchError("origin_surface is absent and CMUX_SURFACE_ID is unavailable")
        value["origin_surface"] = surface
    if not str(value.get("origin_session") or "").strip():
        session = run_command(
            [str(vault_root / "scripts" / "current-session-id.sh")],
            cwd=vault_root,
            label="current coordinator session",
        ).stdout.strip()
        if not session or session == "unknown":
            raise DispatchError("origin_session is absent and the current session is unknown")
        value["origin_session"] = session
    if not isinstance(value.get("session_route"), dict):
        config = load_config(vault_root)
        route, source = routing_from_environment(config)
        value["session_route"] = {**route, "source": source}
    return value


def validate_request(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("schema_version") != 1:
        raise DispatchError("request schema_version must be 1")
    try:
        request_id = str(uuid.UUID(str(raw.get("request_id") or "")))
    except (ValueError, TypeError, AttributeError):
        raise DispatchError("request_id must be a canonical UUID") from None
    if request_id != raw.get("request_id"):
        raise DispatchError("request_id must be a canonical lowercase UUID")
    task_name = require_string(raw.get("task_name"), "task_name", maximum=64)
    if TASK_RE.fullmatch(task_name) is None:
        raise DispatchError("task_name must be lowercase ASCII kebab-case")
    description = require_string(raw.get("description"), "description")
    vault_root = absolute_dir(raw.get("vault_root"), "vault_root")
    target_repo = absolute_dir(raw.get("target_repo"), "target_repo")
    if not (vault_root / "wiki").is_dir() or not (vault_root / "skills" / "dispatch").is_dir():
        raise DispatchError("vault_root is not an llm-obsidian coordinator vault")
    if not (target_repo / ".git").exists():
        result = subprocess.run(
            ["git", "-C", str(target_repo), "rev-parse", "--git-dir"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise DispatchError("target_repo is not a Git repository")
    plan_file = absolute_file(raw.get("plan_file"), "plan_file")
    try:
        plan_file.relative_to(vault_root / "wiki" / "plans")
    except ValueError as exc:
        raise DispatchError("plan_file must be under vault_root/wiki/plans") from exc
    plan_text = plan_file.read_text(encoding="utf-8")
    if re.search(r"(?m)^status:\s*pending\s*$", plan_text) is None:
        raise DispatchError("approved plan status must be pending")
    worktree = absolute_dir(raw.get("worktree"), "worktree", must_exist=False)
    if worktree.exists():
        raise DispatchError(f"worktree already exists: {worktree}")
    branch = require_string(raw.get("branch"), "branch", maximum=200)
    if branch != f"task/{task_name}":
        raise DispatchError("new dispatch branch must equal task/<task_name>")
    base_branch = require_string(raw.get("base_branch"), "base_branch", maximum=300)
    origin_surface = require_string(raw.get("origin_surface"), "origin_surface", maximum=100)
    origin_session = require_string(raw.get("origin_session"), "origin_session", maximum=128)
    session_route = raw.get("session_route")
    if not isinstance(session_route, dict):
        raise DispatchError("session_route must be an object")
    session_runtime = require_string(session_route.get("runtime"), "session_route.runtime", maximum=10)
    session_model = require_string(session_route.get("model"), "session_route.model", maximum=200)
    session_effort = require_string(session_route.get("effort"), "session_route.effort", maximum=20)
    session_source = require_string(session_route.get("source"), "session_route.source", maximum=100)
    if session_runtime not in RUNTIMES or session_source == "tracked-default":
        raise DispatchError("session_route must be host-confirmed for claude or codex")
    executor = raw.get("executor") or {}
    if not isinstance(executor, dict):
        raise DispatchError("executor must be an object")
    explicit_runtime = str(executor.get("runtime") or "").strip()
    explicit_model = str(executor.get("model") or "").strip()
    explicit_effort = str(executor.get("effort") or "").strip()
    if explicit_runtime and explicit_runtime not in RUNTIMES:
        raise DispatchError("executor.runtime must be claude or codex")
    if any("\0" in value or len(value) > 200 for value in (explicit_model, explicit_effort)):
        raise DispatchError("executor model/effort override is invalid")
    context = raw.get("wiki_context") or []
    if not isinstance(context, list) or len(context) > 5:
        raise DispatchError("wiki_context must contain at most five entries")
    normalized_context: list[dict[str, str]] = []
    for item in context:
        if not isinstance(item, dict):
            raise DispatchError("wiki_context entries must be objects")
        title = require_string(item.get("title"), "wiki_context.title", maximum=200)
        summary = require_string(item.get("summary"), "wiki_context.summary", maximum=500)
        matches = list((vault_root / "wiki").rglob(f"{title}.md"))
        if len(matches) != 1:
            raise DispatchError(f"wiki context target must exist exactly once: {title}")
        normalized_context.append({"title": title, "summary": summary})
    agents = raw.get("suggested_agents") or []
    if not isinstance(agents, list) or len(agents) > 2:
        raise DispatchError("suggested_agents must contain at most two entries")
    normalized_agents: list[dict[str, str]] = []
    for item in agents:
        if not isinstance(item, dict):
            raise DispatchError("suggested_agents entries must be objects")
        normalized_agents.append({
            "name": require_string(item.get("name"), "suggested_agents.name", maximum=100),
            "hint": require_string(item.get("hint"), "suggested_agents.hint", maximum=500),
        })
    reap = raw.get("reap")
    if not isinstance(reap, dict):
        raise DispatchError("reap must be an object")
    reap_type = require_string(reap.get("type"), "reap.type", maximum=50)
    if reap_type not in SUMMARY_TYPES:
        raise DispatchError("reap.type is not supported")
    reap_title = require_string(reap.get("title"), "reap.title", maximum=200)
    return {
        "schema_version": 1,
        "request_id": request_id,
        "task_name": task_name,
        "description": description,
        "vault_root": vault_root,
        "target_repo": target_repo,
        "worktree": worktree,
        "branch": branch,
        "base_branch": base_branch,
        "plan_file": plan_file,
        "origin_surface": origin_surface,
        "origin_session": origin_session,
        "session_route": {
            "runtime": session_runtime,
            "model": session_model,
            "effort": session_effort,
            "source": session_source,
        },
        "executor": {
            "runtime": explicit_runtime,
            "model": explicit_model,
            "effort": explicit_effort,
        },
        "wiki_context": normalized_context,
        "suggested_agents": normalized_agents,
        "reap": {"type": reap_type, "title": reap_title},
        "review_mode": str(raw.get("review_mode") or "").strip(),
    }


def load_dispatch_config(vault_root: Path, target_repo: Path) -> dict[str, Any]:
    path = target_repo / ".codex" / "dispatch-env.toml"
    if not path.is_file():
        path = vault_root / ".codex" / "dispatch-env.toml"
    values = dict(DEFAULT_DISPATCH)
    if path.is_file():
        try:
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise DispatchError(f"invalid dispatch config {path}: {exc}") from exc
        section = parsed.get("codex_dispatch", {})
        if not isinstance(section, dict):
            raise DispatchError(f"invalid codex_dispatch table in {path}")
        unknown = set(section) - set(DEFAULT_DISPATCH)
        if unknown:
            raise DispatchError("unknown dispatch config keys: " + ", ".join(sorted(unknown)))
        values.update(section)
    if values["interaction_policy"] != "unattended":
        raise DispatchError("dispatch-runner supports approved unattended plans only")
    if values["review_mode"] not in REVIEW_MODES:
        raise DispatchError("dispatch review_mode must be light, full, or skip")
    bounds = {
        "max_verify_iterations": (0, 5),
        "watchdog_poll_seconds": (5, 300),
        "watchdog_warn_after_seconds": (300, 7200),
        "watchdog_alert_after_seconds": (600, 14400),
    }
    for key, (lower, upper) in bounds.items():
        if isinstance(values[key], bool) or not isinstance(values[key], int) or not lower <= values[key] <= upper:
            raise DispatchError(f"dispatch config {key} must be {lower}..{upper}")
    if values["watchdog_alert_after_seconds"] <= values["watchdog_warn_after_seconds"]:
        raise DispatchError("dispatch watchdog alert must follow its warning")
    for key in ("auto_close_surfaces", "watchdog_enabled"):
        if not isinstance(values[key], bool):
            raise DispatchError(f"dispatch config {key} must be boolean")
    for key in ("reap_skill", "reap_send_skill", "review_skill", "review_send_skill"):
        values[key] = require_string(values[key], f"dispatch config {key}", maximum=300)
    codex_home = str(values.get("codex_home") or "").strip()
    if codex_home:
        home = Path(codex_home).expanduser().resolve()
        if not home.is_dir():
            raise DispatchError(f"configured Codex home is missing: {home}")
        values["codex_home"] = str(home)
    values["source_file"] = str(path) if path.is_file() else "environment"
    return values


def extract_prompt_body(template: str) -> str:
    marker = "```markdown\n# Task: <task_name>"
    start = template.find(marker)
    end = template.rfind("\n```")
    if start < 0 or end < 0 or end <= start:
        raise DispatchError("dispatch prompt template markers are invalid")
    return template[start + len("```markdown\n") : end]


def keep_plan_branch(body: str) -> str:
    a_start = body.find("<!-- BRANCH A:")
    a_content = body.find("\n", a_start) + 1
    a_end = body.find("<!-- END BRANCH A -->", a_content)
    b_start = body.find("<!-- BRANCH B:", a_end)
    b_end = body.find("<!-- END BRANCH B -->", b_start)
    if min(a_start, a_content, a_end, b_start, b_end) < 0:
        raise DispatchError("dispatch prompt template branch markers are invalid")
    body = body[:a_start] + body[a_content:a_end] + body[b_end + len("<!-- END BRANCH B -->") :]
    return body


def render_task_prompt(request: dict[str, Any], config: dict[str, Any]) -> str:
    template_path = request["vault_root"] / "skills" / "dispatch" / "references" / "task-prompt-template.md"
    body = keep_plan_branch(extract_prompt_body(template_path.read_text(encoding="utf-8")))
    context = request["wiki_context"]
    context_text = "\n".join(
        f"- [[{item['title']}]] — {item['summary']}" for item in context
    ) or "- No additional wiki pages were pre-loaded."
    body = re.sub(
        r"- \[\[<wiki-page-1>\]\] — <one-line summary>\n"
        r"- \[\[<wiki-page-2>\]\] — \.\.\.\n"
        r"- \[\[<wiki-page-3>\]\] — \.\.\.",
        lambda _match: context_text,
        body,
        count=1,
    )
    optional_start = body.find("## Suggested sub-agents (optional, hint)")
    optional_end = body.find("## Wiki access (read-only, live as you go)", optional_start)
    if optional_start < 0 or optional_end < 0:
        raise DispatchError("dispatch prompt optional-agent markers are invalid")
    agents = request["suggested_agents"]
    if agents:
        agent_lines = "\n".join(f"- Agent(\"{item['name']}\") — {item['hint']}" for item in agents)
        optional = (
            "## Suggested sub-agents (optional, hint)\n\n"
            "This task falls into the scope of the following specialized sub-agents.\n"
            "You may delegate audit / deep-dive work when useful:\n\n"
            f"{agent_lines}\n\n"
            "A hint, not a command. Simpler work should stay in this task session.\n\n"
        )
    else:
        optional = ""
    body = body[:optional_start] + optional + body[optional_end:]
    codex_env = (
        f"{config['codex_home']} / {config['profile']}"
        if config.get("codex_home")
        else "inherited current Codex environment"
    )
    replacements = {
        "<task_name>": request["task_name"],
        "<description from user, multi-line ok>": request["description"],
        "<vault-root>": str(request["vault_root"]),
        "<worktree-path>": str(request["worktree"]),
        "<repo-path>": str(request["target_repo"]),
        "<base-branch>": request["base_branch"],
        "<codex-home/profile or inherited>": codex_env,
        "<wiki-reap-command>": config["reap_skill"],
        "<review-skill>": config["review_skill"],
        "<review-send-skill>": config["review_send_skill"],
        "<reap-send-skill>": config["reap_send_skill"],
        "<absolute path to wiki/plans/<file>.md>": str(request["plan_file"]),
    }
    for old, new in replacements.items():
        body = body.replace(old, new)
    if "<!-- BRANCH" in body or "<description from user" in body:
        raise DispatchError("dispatch prompt rendering left control placeholders")
    return body.rstrip() + "\n"


def resolved_routes(request: dict[str, Any], *, persist: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    config = load_config(request["vault_root"])
    if persist:
        session = capture_session(
            config,
            request["origin_session"],
            request["session_route"]["runtime"],
            request["session_route"]["model"],
            request["session_route"]["effort"],
            source=request["session_route"]["source"],
        )
    else:
        session = {
            "schema_version": 1,
            "session_id": request["origin_session"],
            **request["session_route"],
            "config_sha256": config.fingerprint,
        }
    effective = resolve(
        config,
        "dispatch",
        session=session,
        explicit_runtime=request["executor"]["runtime"],
        explicit_model=request["executor"]["model"],
        explicit_effort=request["executor"]["effort"],
    )
    return session, effective


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


def identify_origin(vault_root: Path, surface: str) -> dict[str, str]:
    result = run_command(
        [sys.executable, str(vault_root / "scripts" / "cmux_agent_supervisor.py"), "identify-caller", "--surface", surface],
        label="cmux caller identity",
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DispatchError("cmux caller identity returned invalid JSON") from exc
    if value.get("surface_id") != surface:
        raise DispatchError("cmux caller identity does not match origin_surface")
    return {"surface_id": surface, "surface_ref": str(value.get("surface_ref") or "")}


def create_worktree(request: dict[str, Any]) -> None:
    request["worktree"].parent.mkdir(parents=True, exist_ok=True)
    run_command(
        ["git", "check-ref-format", "--branch", request["branch"]],
        label="branch validation",
    )
    existing = run_command(
        ["git", "-C", str(request["target_repo"]), "branch", "--list", request["branch"]],
        label="branch lookup",
    )
    if existing.stdout.strip():
        raise DispatchError(f"dispatch branch already exists: {request['branch']}")
    run_command(
        [
            "git", "-C", str(request["target_repo"]), "worktree", "add", "-b",
            request["branch"], str(request["worktree"]), request["base_branch"],
        ],
        label="worktree creation",
    )


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
    gateway = request["vault_root"] / "scripts" / "mcp-gateway" / "mcp-gateway.sh"
    run_command([str(gateway), "sync-config", "--apply"], cwd=request["vault_root"], label="MCP config sync")
    profile = str(config.get("profile") or "").strip()
    if not profile:
        return
    env = os.environ.copy()
    if config.get("codex_home"):
        env["CODEX_HOME"] = str(config["codex_home"])
    run_command(
        [str(gateway), "codex-sync", "--apply", "--only-profile", profile],
        cwd=request["vault_root"],
        env=env,
        label="Codex dispatch profile sync",
    )


def write_task_files(
    request: dict[str, Any], config: dict[str, Any], session: dict[str, Any],
    effective: dict[str, Any], identity: dict[str, str], origin: dict[str, str],
    child: dict[str, str],
) -> dict[str, Any]:
    worktree = request["worktree"]
    handoffs = {
        ".task-cmux-surface": child["surface"],
        ".wiki-cmux-surface": origin["surface_id"],
        ".wiki-agent-runtime": request["session_route"]["runtime"],
        ".wiki-reap-command": config["reap_skill"],
        ".task-reap-send-skill": config["reap_send_skill"],
        ".task-review-skill": config["review_skill"],
        ".task-review-send-skill": config["review_send_skill"],
    }
    for name, value in handoffs.items():
        atomic_text(worktree / name, value + "\n")
    atomic_text(worktree / ".task-prompt.md", render_task_prompt(request, config))
    plan_hash = sha256_file(request["plan_file"])
    review_mode = request["review_mode"] or config["review_mode"]
    if review_mode not in REVIEW_MODES:
        raise DispatchError("review_mode must be light, full, or skip")
    meta: dict[str, Any] = {
        "version": 3,
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
        "target_repo": str(request["target_repo"]),
        "vault_root": str(request["vault_root"]),
        "branch": request["branch"],
        "base_branch": request["base_branch"],
        "codex_home": config.get("codex_home") or None,
        "codex_profile": config.get("profile") or None,
        "wiki_reap_command": config["reap_skill"],
        "reap_send_skill": config["reap_send_skill"],
        "review_skill": config["review_skill"],
        "review_send_skill": config["review_send_skill"],
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
        "approved_plan_sha256": plan_hash,
        "interaction_policy": "unattended",
        "review_policy": {
            "mode": review_mode,
            "max_verify_iterations": config["max_verify_iterations"],
            "auto_resolve_severities": ["warning", "nit"],
            "escalate_severities": ["blocking"],
        },
        "reap_policy": {
            "mode": "final",
            "auto_file": True,
            "allowed_types": [request["reap"]["type"]],
            "title": request["reap"]["title"],
        },
        "surface_policy": {"auto_close": config["auto_close_surfaces"]},
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


def launch_task(request: dict[str, Any], child: dict[str, str]) -> None:
    supervisor = request["vault_root"] / "scripts" / "cmux_agent_supervisor.py"
    run_command(
        [sys.executable, str(supervisor), "prepare-task", "--worktree", str(request["worktree"]), "--surface", child["surface"]],
        label="task agent preparation",
    )
    command = shlex.join([
        sys.executable,
        str(supervisor),
        "run",
        "--worktree",
        str(request["worktree"]),
        "--kind",
        "task",
        "--surface",
        child["surface"],
    ])
    run_command(["cmux", "send", "--surface", child["surface"], command], label="task supervisor handoff")
    run_command(["cmux", "send-key", "--surface", child["surface"], "Enter"], label="task supervisor submit")
    time.sleep(0.2)
    run_command(["cmux", "read-screen", "--surface", child["surface"], "--lines", "1"], label="task surface verification")


def dispatch_log(request: dict[str, Any], effective: dict[str, Any], child: dict[str, str]) -> None:
    links = ", ".join(f"[[{item['title']}]]" for item in request["wiki_context"]) or "none"
    entry = (
        f"## [{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}] dispatch | {request['task_name']}\n\n"
        f"Spawned an approved unattended task split (cmux `{child['surface']}`, runtime "
        f"{effective['runtime']}, model {effective['model']}) to the right of the coordinator in worktree "
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


def run_state_path(vault_root: Path, request_id: str) -> Path:
    return vault_root / ".vault-meta" / "dispatch-runs" / f"{request_id}.json"


def completed_replay(raw: dict[str, Any], spec_sha256: str) -> dict[str, Any] | None:
    """Return an exact completed result before mutable plan/worktree validation."""
    request_id = str(raw.get("request_id") or "")
    vault_value = raw.get("vault_root")
    try:
        canonical_request_id = str(uuid.UUID(request_id))
    except (ValueError, TypeError, AttributeError):
        return None
    if canonical_request_id != request_id or not isinstance(vault_value, str):
        return None
    vault = Path(vault_value).expanduser()
    if not vault.is_absolute():
        return None
    state_dir = run_state_path(vault.resolve(), request_id).parent
    if not state_dir.exists():
        return None
    ensure_owned_dir(state_dir)
    state_path = state_dir / f"{request_id}.json"
    if not state_path.is_file():
        return None
    state = read_object(state_path)
    if state.get("request_sha256") != spec_sha256:
        raise DispatchError(f"dispatch request {request_id} was reused with different bytes")
    if state.get("status") == "launched" and isinstance(state.get("result"), dict):
        return {**state["result"], "idempotent": True}
    return None


def begin_run(request: dict[str, Any], spec_sha256: str) -> tuple[Path, dict[str, Any] | None]:
    path = run_state_path(request["vault_root"], request["request_id"])
    ensure_owned_dir(path.parent)
    claim = {
        "schema_version": 1,
        "request_id": request["request_id"],
        "request_sha256": spec_sha256,
        "task_name": request["task_name"],
        "status": "preparing",
        "worktree": str(request["worktree"]),
        "created_at": utc_now(),
    }
    try:
        exclusive_json(path, claim)
    except FileExistsError:
        current = read_object(path)
        if current.get("request_sha256") != spec_sha256:
            raise DispatchError(f"dispatch request {request['request_id']} was reused with different bytes")
        if current.get("status") == "launched" and isinstance(current.get("result"), dict):
            return path, current["result"]
        raise DispatchError(
            f"dispatch request {request['request_id']} is already {current.get('status', 'unknown')}; "
            "inspect its exact run state instead of spawning again"
        )
    return path, None


def mark_failed(path: Path, stage: str, message: str) -> None:
    current = read_object(path)
    current.update({"status": "failed", "stage": stage, "failure": message[:500], "updated_at": utc_now()})
    atomic_json(path, current)


def start(request: dict[str, Any], spec_sha256: str) -> dict[str, Any]:
    state_path, prior = begin_run(request, spec_sha256)
    if prior is not None:
        return {**prior, "idempotent": True}
    stage = "preflight"
    stage_started = time.monotonic()
    run_started = stage_started
    child: dict[str, str] | None = None
    launched = False
    try:
        config = load_dispatch_config(request["vault_root"], request["target_repo"])
        session, effective = resolved_routes(request)
        origin = identify_origin(request["vault_root"], request["origin_surface"])
        emit_lifecycle_event(request["worktree"], "dispatch-runner-stage", actor=stage,
                             counts={"duration_ms": round((time.monotonic() - stage_started) * 1000)},
                             vault_root=request["vault_root"])
        stage = "worktree"
        stage_started = time.monotonic()
        create_worktree(request)
        identity = initialize_task(request)
        atomic_text(request["worktree"] / ".task-prompt.md", render_task_prompt(request, config))
        emit_lifecycle_event(request["worktree"], "dispatch-runner-stage", actor=stage,
                             counts={"duration_ms": round((time.monotonic() - stage_started) * 1000)},
                             vault_root=request["vault_root"])
        stage = "runtime-sync"
        stage_started = time.monotonic()
        sync_codex_profile(request, config, effective)
        emit_lifecycle_event(request["worktree"], "dispatch-runner-stage", actor=stage,
                             counts={"duration_ms": round((time.monotonic() - stage_started) * 1000)},
                             vault_root=request["vault_root"])
        stage = "surface"
        stage_started = time.monotonic()
        child = spawn_right(request["origin_surface"])
        emit_lifecycle_event(request["worktree"], "dispatch-runner-stage", actor=stage,
                             counts={"duration_ms": round((time.monotonic() - stage_started) * 1000)},
                             vault_root=request["vault_root"])
        stage = "task-contract"
        stage_started = time.monotonic()
        write_task_files(request, config, session, effective, identity, origin, child)
        emit_lifecycle_event(request["worktree"], "dispatch-runner-stage", actor=stage,
                             counts={"duration_ms": round((time.monotonic() - stage_started) * 1000)},
                             vault_root=request["vault_root"])
        stage = "launch"
        stage_started = time.monotonic()
        launch_task(request, child)
        launched = True
        emit_lifecycle_event(request["worktree"], "dispatch-runner-stage", actor=stage,
                             counts={"duration_ms": round((time.monotonic() - stage_started) * 1000)},
                             vault_root=request["vault_root"])
        stage = "log"
        stage_started = time.monotonic()
        log_status = "ok"
        try:
            dispatch_log(request, effective, child)
        except DispatchError:
            log_status = "degraded"
        emit_lifecycle_event(request["worktree"], "dispatch-runner-stage", actor=stage,
                             counts={"duration_ms": round((time.monotonic() - stage_started) * 1000)},
                             status=log_status, vault_root=request["vault_root"])
        result = {
            "schema_version": 1,
            "status": "launched",
            "request_id": request["request_id"],
            "project_id": identity["project_id"],
            "task_id": identity["task_id"],
            "task_name": request["task_name"],
            "runtime": effective["runtime"],
            "model": effective["model"],
            "effort": effective["effort"],
            "worktree": str(request["worktree"]),
            "branch": request["branch"],
            "task_surface": child["surface"],
            "task_surface_ref": child["surface_ref"],
            "origin_surface": origin["surface_id"],
            "log_status": log_status,
            "coordinator_action": COORDINATOR_ACTION,
            "setup_duration_ms": round((time.monotonic() - run_started) * 1000),
            "idempotent": False,
        }
        atomic_json(state_path, {
            "schema_version": 1,
            "request_id": request["request_id"],
            "request_sha256": spec_sha256,
            "task_name": request["task_name"],
            "status": "launched",
            "worktree": str(request["worktree"]),
            "result": result,
            "updated_at": utc_now(),
        })
        return result
    except (DispatchError, RoutingError, TaskSessionError, OSError, ValueError) as exc:
        emit_lifecycle_event(request["worktree"], "dispatch-runner-stage", actor=stage,
                             counts={"duration_ms": round((time.monotonic() - stage_started) * 1000)},
                             status="error", vault_root=request["vault_root"])
        if child is not None and not launched:
            subprocess.run(
                ["cmux", "close-surface", "--surface", child["surface"]],
                text=True,
                capture_output=True,
                check=False,
            )
        mark_failed(state_path, stage, str(exc))
        raise DispatchError(
            f"{stage} failed for request {request['request_id']}; no retry was attempted: {exc}"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--spec", type=Path, required=True)
    launch = sub.add_parser("start")
    launch.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    try:
        spec_path = args.spec.expanduser().resolve()
        spec_sha256 = sha256_file(spec_path)
        raw = read_object(spec_path)
        if args.command == "start":
            replay = completed_replay(raw, spec_sha256)
            if replay is not None:
                print(json.dumps(replay, ensure_ascii=False, sort_keys=True))
                return 0
        request = validate_request(materialize_current_context(raw))
        if args.command == "validate":
            config = load_dispatch_config(request["vault_root"], request["target_repo"])
            session, effective = resolved_routes(request, persist=False)
            prompt = render_task_prompt(request, config)
            print(json.dumps({
                "schema_version": 1,
                "status": "valid",
                "request_id": request["request_id"],
                "runtime": effective["runtime"],
                "model": effective["model"],
                "effort": effective["effort"],
                "plan_sha256": sha256_file(request["plan_file"]),
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "session_source": session["source"],
            }, sort_keys=True))
            return 0
        print(json.dumps(start(request, spec_sha256), ensure_ascii=False, sort_keys=True))
        return 0
    except (DispatchError, RoutingError, TaskSessionError, ContractError, OSError, ValueError) as exc:
        die(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
