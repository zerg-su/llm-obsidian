"""Routing and sandbox safety policy for the legacy cmux supervisor."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from model_routing import (
    RoutingError,
    load_config as load_routing_config,
    resolve as resolve_model_route,
    session_from_meta,
)
from task_contract import ContractError, normalize, read_json as read_task_json
from cmux_agent_support import (
    CODEX_EFFORTS,
    ROUTING_CONFIG as _ROUTING_CONFIG,
    SupervisorError,
    codex_automation_service_tier_config,
    codex_effort_config,
    resolved_git_common_dir,
    task_codex_config_values,
    validated_cmux_socket_path,
)
from cmux_supervisor_contracts import (
    CLAUDE_EFFORTS,
    CLAUDE_REVIEW_TOOL_SURFACE,
    CODEX_FORBIDDEN_OPTIONS,
    SCRIPT_DIR,
    claude_review_allowed_tools,
    read_json,
    task_dcg_config,
    validate_trusted_runtime_path,
)
from cmux_supervisor_review import primary_coordinator_review


def option_value(argv: list[str], flag: str) -> str | None:
    values = option_values(argv, flag)
    if not values:
        return None
    if len(values) != 1:
        raise SupervisorError(f"agent command must contain at most one {flag}")
    return values[0]


def option_values(argv: list[str], flag: str) -> list[str]:
    positions = [index for index, item in enumerate(argv) if item == flag or item.startswith(f"{flag}=")]
    values: list[str] = []
    for index in positions:
        if argv[index] != flag or index + 1 >= len(argv):
            raise SupervisorError(f"agent command must pass {flag} as a separate option")
        values.append(argv[index + 1])
    return values


def require_option(argv: list[str], flag: str, expected: str) -> None:
    if option_value(argv, flag) != expected:
        raise SupervisorError(f"agent command must pin {flag} {expected}")


def reviewer_codex_config_values(effort: str = "") -> list[str]:
    values = [
        codex_automation_service_tier_config(),
        'web_search="disabled"',
        "sandbox_workspace_write.network_access=true",
        "features.network_proxy.enabled=true",
        'features.network_proxy.domains={ "localhost" = "allow", "127.0.0.1" = "allow", "::1" = "allow" }',
        "features.network_proxy.unix_sockets={}",
        "features.network_proxy.allow_local_binding=true",
        "features.network_proxy.allow_upstream_proxy=false",
        "features.network_proxy.dangerously_allow_all_unix_sockets=false",
        "features.network_proxy.dangerously_allow_non_loopback_proxy=false",
        "features.network_proxy.enable_socks5=false",
        "features.network_proxy.enable_socks5_udp=false",
    ]
    if effort:
        values.append(codex_effort_config(effort))
    return values


def append_task_codex_network_policy(argv: list[str], cmux_socket: Path, effort: str) -> None:
    for value in task_codex_config_values(cmux_socket, effort):
        argv.extend(["-c", value])


def validate_reviewer_safety(
    argv: list[str],
    runtime: str,
    reviewer_model: str,
    reviewer_effort: str = "",
    worktree: Path | None = None,
    base_branch: str = "",
) -> None:
    require_option(argv, "--model", reviewer_model)
    if runtime == "codex":
        require_option(argv, "-s", "workspace-write")
        require_option(argv, "-a", "never")
        require_option(argv, "--disable", "hooks")
        if option_values(argv, "-c") != reviewer_codex_config_values(reviewer_effort):
            raise SupervisorError("Codex reviewer command has an unexpected network policy")
        if "--add-dir" in argv:
            raise SupervisorError("Codex reviewer command must not request additional writable roots")
        if any(item in CODEX_FORBIDDEN_OPTIONS for item in argv) or "danger-full-access" in argv:
            raise SupervisorError("Codex reviewer command weakens the isolated scratch boundary")
        return

    require_option(argv, "--permission-mode", "dontAsk")
    require_option(argv, "--tools", CLAUDE_REVIEW_TOOL_SURFACE)
    if reviewer_effort:
        require_option(argv, "--effort", reviewer_effort)
    if "--dangerously-skip-permissions" in argv:
        raise SupervisorError("Claude reviewer command bypasses permissions")
    allowed_positions = [index for index, item in enumerate(argv) if item == "--allowedTools"]
    model_positions = [index for index, item in enumerate(argv) if item == "--model"]
    if len(allowed_positions) != 1 or len(model_positions) != 1:
        raise SupervisorError("Claude reviewer command must pin allowed tools and model")
    if worktree is None:
        raise SupervisorError("Claude reviewer command is missing its exact product worktree")
    expected_allowed_tools = claude_review_allowed_tools(
        worktree,
        base_branch=base_branch,
    )
    allowed_index, model_index = allowed_positions[0], model_positions[0]
    if allowed_index >= model_index:
        raise SupervisorError("Claude reviewer allowed tools are malformed")
    allowed_end = allowed_index + 1 + len(expected_allowed_tools)
    if tuple(argv[allowed_index + 1:allowed_end]) != expected_allowed_tools:
        raise SupervisorError("Claude reviewer command has an unexpected permission allowlist")
    extras = argv[allowed_end:model_index]
    while extras:
        if len(extras) < 2 or extras[0] not in {"--add-dir", "--resume"}:
            raise SupervisorError("Claude reviewer command has unexpected pre-model options")
        extras = extras[2:]


def validate_task_safety(
    argv: list[str],
    runtime: str,
    interaction_policy: str,
    git_common_dir: Path | None = None,
    cmux_socket: Path | None = None,
    model: str = "",
    effort: str = "high",
    task_session_dir: Path | None = None,
) -> None:
    require_option(argv, "--model", model)
    if runtime == "codex":
        if any(item in CODEX_FORBIDDEN_OPTIONS for item in argv) or "danger-full-access" in argv:
            raise SupervisorError("Codex task command weakens the approved sandbox")
        if interaction_policy == "unattended":
            if git_common_dir is None or cmux_socket is None:
                raise SupervisorError("Codex unattended task is missing an approved runtime root")
            expected_roots = [str(git_common_dir)]
            if task_session_dir is not None:
                expected_roots.append(str(task_session_dir))
            if option_values(argv, "--add-dir") != expected_roots:
                raise SupervisorError("Codex task command has unexpected writable roots")
            require_option(argv, "-a", "never")
            require_option(argv, "-s", "workspace-write")
            if option_values(argv, "-c") != task_codex_config_values(cmux_socket, effort):
                raise SupervisorError("Codex task command has an unexpected network policy")
        elif any(option_value(argv, flag) is not None for flag in ("-a", "-s", "--add-dir")):
            raise SupervisorError("interactive Codex task command has unexpected approval overrides")
        elif option_values(argv, "-c") != [
            codex_automation_service_tier_config(), codex_effort_config(effort)
        ]:
            raise SupervisorError("interactive Codex task command has unexpected config overrides")
        return
    require_option(argv, "--permission-mode", "auto")
    require_option(argv, "--effort", effort)
    if "--dangerously-skip-permissions" in argv or "--allowedTools" in argv:
        raise SupervisorError("Claude task command has unexpected permission overrides")


def expected_codex_home(meta: dict[str, Any]) -> str | None:
    raw = str(meta.get("codex_home") or "").strip()
    return str(Path(raw).expanduser().resolve()) if raw else None


def resolved_task_model_route(worktree: Path, meta: dict[str, Any], runtime: str) -> dict[str, Any]:
    """Resolve new routing envelopes while preserving concrete legacy metadata."""
    config_root = worktree if (worktree / "config/model-routing.toml").is_file() else _ROUTING_CONFIG.root
    try:
        config = load_routing_config(config_root)
        routing = meta.get("routing")
        effective = routing.get("effective") if isinstance(routing, dict) else None
        if isinstance(effective, dict):
            route = {
                "runtime": str(effective.get("runtime") or ""),
                "model": str(effective.get("model") or ""),
                "effort": str(effective.get("effort") or ""),
                "source": effective.get("source") or ["metadata-envelope"],
                "config_sha256": str(effective.get("config_sha256") or config.fingerprint),
            }
            if route["runtime"] != runtime or not route["model"]:
                raise RoutingError("task routing envelope disagrees with executor runtime")
            allowed_efforts = CODEX_EFFORTS if runtime == "codex" else CLAUDE_EFFORTS
            if route["effort"] not in allowed_efforts:
                raise RoutingError("task routing envelope has invalid effort")
            registered = config.data["model_registry"].get(route["model"])
            sources = route["source"] if isinstance(route["source"], list) else []
            if registered not in {None, runtime}:
                raise RoutingError("task routing envelope model/provider mismatch")
            if registered is None and not {"explicit-model", "explicit-runtime"} <= set(sources):
                raise RoutingError("unregistered task model requires explicit model and runtime sources")
            return route
        session = session_from_meta(meta)
        explicit_model = str(meta.get("model") or "").strip()
        explicit_effort = str(meta.get("effort") or "").strip()
        if session is not None:
            return resolve_model_route(
                config,
                "dispatch",
                session=session,
                explicit_runtime=runtime if runtime != session["runtime"] else "",
                explicit_model=explicit_model,
                explicit_effort=explicit_effort,
            )
        # v1/v2 metadata created before the routing envelope treats concrete
        # fields as explicit overrides and otherwise uses the central default.
        default = config.runtime_default(runtime)
        if explicit_model:
            registered = config.data["model_registry"].get(explicit_model)
            if registered not in {None, runtime}:
                raise RoutingError("legacy task metadata model/provider mismatch")
        default["model"] = explicit_model or default["model"]
        default["effort"] = explicit_effort or default["effort"]
        allowed_efforts = CODEX_EFFORTS if runtime == "codex" else CLAUDE_EFFORTS
        if default["effort"] not in allowed_efforts:
            raise RoutingError("legacy task metadata has invalid effort")
        default.update({"source": ["legacy-metadata" if explicit_model or explicit_effort else "tracked-default"], "config_sha256": config.fingerprint})
        return default
    except RoutingError as exc:
        raise SupervisorError(str(exc)) from exc


def validated_task_git_common_dir(worktree: Path, meta: dict[str, Any]) -> Path:
    common = resolved_git_common_dir(worktree)
    target_raw = str(meta.get("target_repo") or "").strip()
    if not target_raw:
        return common
    target = Path(target_raw).expanduser().resolve()
    if not target.is_dir() or resolved_git_common_dir(target) != common:
        raise SupervisorError("task worktree does not belong to target_repo")
    return common


def validated_task_session_dir(meta: dict[str, Any]) -> Path | None:
    """Return the sole coordinator registry subtree owned by a v3 task."""
    if meta.get("version") not in {3, 4}:
        return None
    vault = Path(str(meta.get("vault_root") or "")).expanduser().resolve()
    project_id = str(meta.get("project_id") or "").strip()
    task_id = str(meta.get("task_id") or "").strip()
    root = (vault / ".vault-meta" / "task-sessions").resolve()
    task_dir = (root / "projects" / project_id / "tasks" / task_id).resolve()
    try:
        task_dir.relative_to(root)
    except ValueError as exc:
        raise SupervisorError("task registry root escapes the coordinator registry") from exc
    if not task_dir.is_dir() or task_dir.stat().st_uid != os.getuid():
        raise SupervisorError("exact task registry root is missing or not owned by the current user")
    return task_dir


def validated_review_runtime(worktree: Path, meta: dict[str, Any]) -> Path:
    raw = str(meta.get("review_runtime_dir") or "").strip()
    if not raw:
        raise SupervisorError("Codex review metadata is missing review_runtime_dir")
    runtime = Path(raw).expanduser().resolve()
    if not runtime.is_dir():
        raise SupervisorError("Codex review runtime directory does not exist")
    operation_raw = str(meta.get("operation_dir") or "").strip()
    operation_dir = Path(operation_raw).expanduser().resolve() if operation_raw else None
    persistent = operation_dir is not None and runtime == operation_dir.parents[1] / "runtime"
    dry_run = str(meta.get("review_surface") or "") == "00000000-0000-0000-0000-000000000000"
    if persistent:
        expected_location = operation_dir.name == str(meta.get("operation_id") or "")
    elif dry_run:
        expected_location = runtime.parent == worktree.parent and runtime.name.startswith(".review-runtime-")
    else:
        root = (SCRIPT_DIR.parent / ".vault-meta" / "review-runtimes").resolve()
        expected_location = (
            runtime.parent == root
            and runtime.name.startswith("llm-review-")
        )
    if not expected_location:
        raise SupervisorError("Codex review runtime is not a generated scratch directory")
    try:
        runtime.relative_to(worktree)
    except ValueError:
        pass
    else:
        # A coordinator review targets the canonical vault itself, so its
        # sanctioned scratch root is inside the reviewed checkout by design.
        # Permit only that exact canonical-vault case; linked task worktrees and
        # every other in-worktree runtime remain rejected.
        coordinator_vault = (
            not dry_run and worktree.resolve() == SCRIPT_DIR.parent.resolve()
        )
        if not coordinator_vault:
            raise SupervisorError("Codex review runtime must be outside the product worktree")
    try:
        worktree.relative_to(runtime)
    except ValueError:
        pass
    else:
        raise SupervisorError("Codex review runtime must not contain the product worktree")
    stat = runtime.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise SupervisorError("Codex review runtime must be owner-only")
    if any(runtime.iterdir()) and not persistent:
        raise SupervisorError("Codex review runtime must be empty before launch")
    return runtime


def validate_routing(
    worktree: Path, state_dir: Path, kind: str, surface: str, spec: dict[str, Any]
) -> None:
    source_meta = (
        read_json(state_dir / ".review-meta.json")
        if kind == "reviewer"
        else read_task_json(worktree / ".task-meta.json")
    )
    coordinator_review = (
        kind == "reviewer"
        and primary_coordinator_review(worktree, state_dir, source_meta)
    )
    task_meta = (
        {"version": 1, "vault_root": str(worktree)}
        if coordinator_review
        else read_task_json(worktree / ".task-meta.json")
    )
    try:
        task_policy = normalize(task_meta)
    except ContractError as exc:
        raise SupervisorError(str(exc)) from exc
    if kind == "task":
        source_meta = task_meta
        expected_surface = str(task_meta.get("task_surface") or "")
        expected_runtime = str(task_meta.get("executor_runtime") or task_meta.get("runtime") or "")
    else:
        expected_surface = str(source_meta.get("review_surface") or "")
        expected_runtime = str(source_meta.get("reviewer_runtime") or "")
    if surface != expected_surface or not surface:
        raise SupervisorError(f"{kind} supervisor surface does not match metadata")
    if spec["runtime"] != expected_runtime:
        raise SupervisorError(f"{kind} supervisor runtime does not match metadata")
    expected_env: dict[str, str] = {}
    expected_env["LLM_OBSIDIAN_PROJECT_ROOT"] = str(
        Path(str(task_meta.get("vault_root") or "")).expanduser().resolve()
    )
    expected_env["LLM_OBSIDIAN_SESSION_ROLE"] = kind
    if kind == "task" and task_policy["interaction_policy"] == "unattended":
        expected_env["DCG_CONFIG"] = str(task_dcg_config(task_meta))
    if spec["runtime"] == "codex":
        expected_cwd = (
            validated_review_runtime(worktree, source_meta)
            if kind == "reviewer"
            else worktree
        )
        require_option(spec["argv"], "--cd", str(expected_cwd))
        expected_home = expected_codex_home(source_meta)
        if expected_home is not None:
            expected_env["CODEX_HOME"] = expected_home
        expected_tmp = str(expected_cwd) if kind == "reviewer" else None
        if expected_tmp is not None:
            expected_env["TMPDIR"] = expected_tmp
        expected_socket = (
            validated_cmux_socket_path()
            if kind == "task" and task_policy["interaction_policy"] == "unattended"
            else None
        )
        if expected_socket is not None:
            expected_env["CMUX_SOCKET_PATH"] = str(expected_socket)
        actual_env = dict(spec["env"])
        runtime_path = actual_env.pop("PATH", "")
        validate_trusted_runtime_path(runtime_path, spec["runtime"])
        if actual_env != expected_env:
            raise SupervisorError("Codex supervisor environment does not match the approved runtime")
    else:
        actual_env = dict(spec["env"])
        runtime_path = actual_env.pop("PATH", "")
        validate_trusted_runtime_path(runtime_path, spec["runtime"])
        if actual_env != expected_env:
            raise SupervisorError("Claude supervisor environment does not match the approved runtime")
        if kind == "reviewer" and str(source_meta.get("review_runtime_dir") or "").strip():
            require_option(spec["argv"], "--add-dir", str(worktree))
    if kind == "reviewer":
        validate_reviewer_safety(
            spec["argv"],
            spec["runtime"],
            str(source_meta.get("reviewer_model") or ""),
            str(source_meta.get("reviewer_effort") or ""),
            worktree,
            str(source_meta.get("base_branch") or ""),
        )
    else:
        task_route = resolved_task_model_route(worktree, source_meta, spec["runtime"])
        git_common_dir = (
            validated_task_git_common_dir(worktree, source_meta)
            if spec["runtime"] == "codex" and task_policy["interaction_policy"] == "unattended"
            else None
        )
        cmux_socket = (
            validated_cmux_socket_path()
            if spec["runtime"] == "codex" and task_policy["interaction_policy"] == "unattended"
            else None
        )
        task_session_dir = (
            validated_task_session_dir(source_meta)
            if spec["runtime"] == "codex" and task_policy["interaction_policy"] == "unattended"
            else None
        )
        validate_task_safety(
            spec["argv"],
            spec["runtime"],
            task_policy["interaction_policy"],
            git_common_dir,
            cmux_socket,
            str(task_route["model"]),
            str(task_route["effort"]),
            task_session_dir=task_session_dir,
        )
