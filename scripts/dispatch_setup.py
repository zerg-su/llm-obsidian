"""Dispatch prompt, routing, workspace, and task-file preparation."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

from dispatch_contracts import (
    COMPLETION_PASS_LIMITS,
    DEFAULT_DISPATCH,
    REVIEW_MODES,
    TASK_LOCAL_GIT_EXCLUDES,
    DispatchError,
    absolute_dir,
    approved_outcome_contract_sha256,
    approved_plan_file,
    approved_plan_sha256,
    atomic_json,
    atomic_text,
    custom_contract_for_request,
    ensure_owned_dir,
    execution_pipeline_for_request,
    require_string,
    task_pipeline_policy,
    utc_now,
)
from lifecycle_telemetry import emit_lifecycle_event
from model_routing import (
    RoutingError,
    capture_session,
    load_config,
    resolve,
    routing_from_environment,
)
from outcome_contract import extract_from_bytes
from task_contract import ContractError, normalize as normalize_task_contract
from harness.git_ops import GitAdapter, GitError
from harness.verification import load_profiles
from harness.workflows.dispatch import ReviewPolicy


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


def run_state_path(vault_root: Path, request_id: str) -> Path:
    return vault_root / ".vault-meta" / "dispatch-runs" / f"{request_id}.json"



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
        raise DispatchError("dispatch review_mode must be simple, deep, or skip")
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
    for key in ("reap_skill", "review_skill"):
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
    approved = request.get("_approved_prompt")
    if isinstance(approved, str):
        return approved
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
    outcome_contract = extract_from_bytes(request["plan_file"].read_bytes())
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
        "<absolute path to wiki/plans/<file>.md>": str(approved_plan_file(request)),
        "<canonical-task-summary-json>": json.dumps(
            {
                "schema_version": 2,
                "type": request["reap"]["type"],
                "title": request["reap"]["title"],
                "session": request["origin_session"],
                "body": "<bounded Markdown summary>",
                "outcome_disposition": "achieved",
                "outcome_evidence_ids": list(outcome_contract.evidence_ids),
                "residual_gap_pointers": [],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    for old, new in replacements.items():
        body = body.replace(old, new)
    if request["placement"] == "workspace":
        body = body.replace("the left wiki split", "the coordinator workspace")
    if request["pipeline"] == "custom":
        _spec, compiled, _policy, _card = custom_contract_for_request(request)
        phase_contract = "\n".join(
            (
                "## Typed custom pipeline steps",
                "",
                "This is one persistent executor session controlled by the harness.",
                "For this prompt, execute only the exact registered model step in",
                "`.task-pipeline-step-request.json`. Use the declared semantic skills,",
                "write evidence and the bounded result to the exact output/result",
                "pointers, and choose only an `outcome` listed by the request:",
                "",
                "```json",
                '{"schema_version":1,"status":"complete",',
                '"outcome":"<allowed-outcome>",',
                '"output_sha256":"<sha256-of-output-file>",',
                '"head_sha":"<current-git-head>"}',
                "```",
                "",
                "Publish the request-bound callback through:",
                "",
                f"`python3 {request['vault_root']}/scripts/"
                "pipeline-step-submit.py "
                f"--worktree {request['worktree']}`",
                "",
                "Stop after submission. The harness owns transitions, bounded loops,",
                "accepted receipts, verification, review, recovery, and the next",
                "prompt in this same session. Never repeat an accepted visit.",
                "Treat `.task-*`, `.wiki-*`, and `.task-pipeline/**` as runtime",
                "transport: never stage or commit them. Commit only exact product",
                "files required by the approved plan.",
                "",
                "Approved custom definition: "
                f"`{compiled.definition_sha256}`.",
                "",
            )
        )
        marker = "## Harness completion"
        if marker not in body:
            raise DispatchError(
                "dispatch prompt completion marker is unavailable"
            )
        body = body.replace(marker, phase_contract + "\n" + marker, 1)
    elif execution_pipeline_for_request(request) == "engineering/fix":
        policy = request["completion_policy"]
        phase_contract = "\n".join(
            (
                "## Typed engineering/fix phases",
                "",
                "This is one persistent executor session controlled by the harness.",
                "For this prompt, execute only the exact phase in",
                "`.task-pipeline-step-request.json`. Write its evidence and bounded",
                "result to the request's exact `output_pointer` and `result_pointer`,",
                "using this exact result shape:",
                "",
                "```json",
                '{"schema_version":1,"status":"complete",'
                '"output_sha256":"<sha256-of-output-file>",'
                '"head_sha":"<current-git-head>"}',
                "```",
                "",
                "Only `reproduce` may use `status=cannot-reproduce`.",
                "then publish one callback through:",
                "",
                f"`python3 {request['vault_root']}/scripts/"
                "pipeline-step-submit.py "
                f"--worktree {request['worktree']}`",
                "",
                "Stop after submission. Do not begin another phase until the harness",
                "sends its next prompt in this same session. The coordinator owns",
                "accepted receipts and chains the next exact input. On",
                "`cannot-reproduce`, publish that typed outcome and remain paused.",
                "After a restart, obey the first missing phase from the current",
                "request; never repeat an accepted phase.",
                "Treat `.task-*`, `.wiki-*`, and `.task-pipeline/**` as runtime",
                "transport: never stage or commit them. Commit only exact product",
                "files required by the approved plan.",
                "",
                f"Selected completion_policy={policy}; "
                f"total_pass_limit={COMPLETION_PASS_LIMITS[policy]}.",
                "",
            )
        )
        marker = "## Harness completion"
        if marker not in body:
            raise DispatchError(
                "dispatch prompt completion marker is unavailable"
            )
        body = body.replace(marker, phase_contract + "\n" + marker, 1)
    if "<!-- BRANCH" in body or "<description from user" in body:
        raise DispatchError("dispatch prompt rendering left control placeholders")
    return body.rstrip() + "\n"


def review_policy(
    request: dict[str, Any], config: dict[str, Any]
) -> ReviewPolicy:
    """Resolve and freeze the deterministic task-side review preset."""

    approved = request.get("_approved_review")
    if isinstance(approved, ReviewPolicy):
        return approved
    raw = request["review"]
    mode = raw["mode"] or config["review_mode"]
    if mode not in REVIEW_MODES:
        raise DispatchError("review mode must be simple, deep, or skip")
    overrides = (
        raw["cross_model"],
        raw["runtime"],
        raw["model"],
        raw["effort"],
    )
    if mode == "skip" and any(overrides):
        raise DispatchError("skip review cannot carry expert overrides")
    verification = load_profiles(
        request["vault_root"] / "config" / "verification-profiles.toml"
    )["scoped"]
    if mode != "skip":
        routing = load_config(request["vault_root"])
        try:
            resolve(
                routing,
                "review",
                session=request["session_route"],
                explicit_runtime=raw["runtime"],
                explicit_model=raw["model"],
                explicit_effort=raw["effort"],
                same_model=not raw["cross_model"],
                review_profile=mode,
            )
        except RoutingError as exc:
            raise DispatchError(f"invalid review override: {exc}") from exc
    return ReviewPolicy(
        depth="deep" if mode == "deep" else "simple",
        cross_model=raw["cross_model"],
        enabled=mode != "skip",
        runtime=raw["runtime"],
        model=raw["model"],
        effort=raw["effort"],
        verification_profile=verification.name,
        verification_profile_sha256=verification.sha256,
    )


def resolved_routes(request: dict[str, Any], *, persist: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    approved_session = request.get("_approved_session_route")
    approved_effective = request.get("_approved_effective_route")
    if isinstance(approved_session, dict) and isinstance(approved_effective, dict):
        return dict(approved_session), dict(approved_effective)
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


def ensure_task_git_excludes(worktree: Path) -> None:
    """Keep dispatch-owned runtime bindings out of product Git status."""

    raw_path = run_command(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=worktree,
        label="task Git exclude path",
    ).stdout.strip()
    if not raw_path:
        raise DispatchError("task Git exclude path is empty")
    exclude = Path(raw_path)
    if not exclude.is_absolute():
        exclude = worktree / exclude
    if exclude.is_symlink() or (exclude.exists() and not exclude.is_file()):
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


def create_worktree(request: dict[str, Any]) -> None:
    try:
        GitAdapter(request["target_repo"]).create_worktree(
            request["worktree"],
            request["branch"],
            request["base_branch"],
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
        "approved_plan_sha256": plan_hash,
        "outcome_contract_sha256": outcome_hash,
        "interaction_policy": "unattended",
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
    links = ", ".join(f"[[{item['title']}]]" for item in request["wiki_context"]) or "none"
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
