"""Dispatch ingress, custom approval, and compiled-policy contracts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from outcome_contract import OutcomeContractError, extract_from_bytes
from harness.contracts import ContractError as HarnessContractError
from harness.custom_pipelines import (
    CustomPipelinePolicy,
    ExplicitPipelineApproval,
    PipelineSpec,
    compile_custom_spec,
    freeze_custom_pipeline,
    parse_pipeline_spec,
    pipeline_spec_payload,
    render_custom_approval,
)
from harness.pipeline_builtins import EXECUTABLE_BUILTINS, builtin_registry, compiled_builtin
from harness.workflows.dispatch import ReviewPolicy
from dispatch_io import (
    DispatchError,
    _approval_lock,
    absolute_dir,
    absolute_file,
    atomic_json,
    atomic_text,
    custom_authoring_enabled,
    ensure_owned_dir,
    exclusive_json,
    read_object,
    require_string,
    sha256_file,
    utc_now,
)


TASK_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
RUNTIMES = {"claude", "codex"}
REVIEW_MODES = {"simple", "deep", "skip"}
COMPLETION_PASS_LIMITS = {"attention": 2, "autonomous": 3}
SUMMARY_TYPES = {"session", "decision", "runbook", "incident", "service-update", "repo-touch"}
RUN_STATES = {"preparing", "launched", "failed"}
COORDINATOR_ACTION = "return-to-idle-without-polling"
TASK_LOCAL_GIT_EXCLUDES = (".task-origin-session",)
DEFAULT_DISPATCH = {
    "codex_home": "",
    "profile": "",
    "reap_skill": "$llm-obsidian:reap",
    "review_skill": "$llm-obsidian:review",
    "interaction_policy": "unattended",
    "review_mode": "simple",
    "max_verify_iterations": 2,
    "auto_close_surfaces": True,
    "default_reap_type": "session",
    "watchdog_enabled": True,
    "watchdog_poll_seconds": 30,
    "watchdog_warn_after_seconds": 900,
    "watchdog_alert_after_seconds": 1200,
}




























def _validated_routes_and_review(
    raw: dict[str, Any],
    custom_pipeline_spec: Path | None,
    parsed_custom: PipelineSpec | None,
) -> dict[str, Any]:
    origin_session = require_string(
        raw.get("origin_session"), "origin_session", maximum=128
    )
    session_route = raw.get("session_route")
    if not isinstance(session_route, dict):
        raise DispatchError("session_route must be an object")
    session = {
        field: require_string(
            session_route.get(field),
            f"session_route.{field}",
            maximum=limit,
        )
        for field, limit in (
            ("runtime", 10),
            ("model", 200),
            ("effort", 20),
            ("source", 100),
        )
    }
    if session["runtime"] not in RUNTIMES or session["source"] == "tracked-default":
        raise DispatchError("session_route must be host-confirmed for claude or codex")

    raw_executor = raw.get("executor") or {}
    if not isinstance(raw_executor, dict):
        raise DispatchError("executor must be an object")
    executor = {
        field: str(raw_executor.get(field) or "").strip()
        for field in ("runtime", "model", "effort")
    }
    if executor["runtime"] and executor["runtime"] not in RUNTIMES:
        raise DispatchError("executor.runtime must be claude or codex")
    if any(
        "\0" in executor[field] or len(executor[field]) > 200
        for field in ("model", "effort")
    ):
        raise DispatchError("executor model/effort override is invalid")

    legacy_mode = str(raw.get("review_mode") or "").strip()
    raw_review = raw.get("review")
    if raw_review is not None and legacy_mode:
        raise DispatchError("review and review_mode cannot both be present")
    if raw_review is None:
        raw_review = {"mode": legacy_mode}
    if not isinstance(raw_review, dict):
        raise DispatchError("review must be an object")
    unknown = set(raw_review) - {"mode", "cross_model", "runtime", "model", "effort"}
    if unknown:
        raise DispatchError("unknown review keys: " + ", ".join(sorted(unknown)))
    review = {
        "mode": str(raw_review.get("mode") or "").strip(),
        "cross_model": raw_review.get("cross_model", False),
        "runtime": str(raw_review.get("runtime") or "").strip(),
        "model": str(raw_review.get("model") or "").strip(),
        "effort": str(raw_review.get("effort") or "").strip(),
    }
    if custom_pipeline_spec is not None:
        assert parsed_custom is not None
        if not review["mode"]:
            review["mode"] = parsed_custom.review_mode
        elif review["mode"] != parsed_custom.review_mode:
            raise DispatchError("review.mode differs from the custom pipeline contract")
    if review["mode"] and review["mode"] not in REVIEW_MODES:
        raise DispatchError("review.mode must be simple, deep, or skip")
    if not isinstance(review["cross_model"], bool):
        raise DispatchError("review.cross_model must be boolean")
    if review["runtime"] and review["runtime"] not in RUNTIMES:
        raise DispatchError("review.runtime must be claude or codex")
    if any(
        "\0" in review[field] or len(review[field]) > limit
        for field, limit in (("model", 128), ("effort", 20))
    ):
        raise DispatchError("review model/effort override is invalid")
    if review["mode"] == "skip" and any(
        (review["cross_model"], review["runtime"], review["model"], review["effort"])
    ):
        raise DispatchError("skip review cannot carry expert overrides")
    return {
        "origin_session": origin_session,
        "session_route": session,
        "executor": executor,
        "review": review,
    }


def _validated_context(
    raw: dict[str, Any],
    vault_root: Path,
    plan_file: Path,
    parsed_custom: PipelineSpec | None,
) -> list[dict[str, str]]:
    context = raw.get("wiki_context") or []
    if not isinstance(context, list) or len(context) > 5:
        raise DispatchError("wiki_context must contain at most five entries")
    normalized: list[dict[str, str]] = []
    source_paths = [plan_file]
    for item in context:
        if not isinstance(item, dict):
            raise DispatchError("wiki_context entries must be objects")
        title = require_string(item.get("title"), "wiki_context.title", maximum=200)
        summary = require_string(item.get("summary"), "wiki_context.summary", maximum=500)
        matches = list((vault_root / "wiki").rglob(f"{title}.md"))
        if len(matches) != 1:
            raise DispatchError(f"wiki context target must exist exactly once: {title}")
        normalized.append({"title": title, "summary": summary})
        source_paths.append(matches[0])
    if parsed_custom is not None:
        allowed: dict[str, int] = {}
        for source_path in source_paths:
            source = source_path.read_bytes()
            allowed[hashlib.sha256(source).hexdigest()] = len(source)
        for pointer in parsed_custom.context_pointers:
            source_size = allowed.get(pointer.content_sha256)
            if source_size is None:
                raise DispatchError(
                    "custom context pointer is not in the approved context packet"
                )
            if source_size > pointer.byte_limit:
                raise DispatchError(
                    "custom context pointer exceeds its declared byte limit"
                )
    return normalized


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
    try:
        outcome_contract_sha256 = extract_from_bytes(plan_file.read_bytes()).sha256
    except (OSError, OutcomeContractError) as exc:
        raise DispatchError(f"approved plan Outcome Contract is invalid: {exc}") from exc
    worktree = absolute_dir(raw.get("worktree"), "worktree", must_exist=False)
    if worktree.exists():
        raise DispatchError(f"worktree already exists: {worktree}")
    branch = require_string(raw.get("branch"), "branch", maximum=200)
    if branch != f"task/{task_name}":
        raise DispatchError("new dispatch branch must equal task/<task_name>")
    base_branch = require_string(raw.get("base_branch"), "base_branch", maximum=300)
    origin_surface = require_string(raw.get("origin_surface"), "origin_surface", maximum=100)
    placement = str(raw.get("placement") or "split").strip()
    if placement not in {"split", "workspace"}:
        raise DispatchError("placement must be split or workspace")
    pipeline = str(raw.get("pipeline") or "lifecycle/default").strip()
    if pipeline not in {*EXECUTABLE_BUILTINS, "custom"}:
        raise DispatchError("pipeline must name an executable pipeline")
    custom_pipeline_spec: Path | None = None
    parsed_custom: PipelineSpec | None = None
    if pipeline == "custom":
        if not custom_authoring_enabled(vault_root):
            raise DispatchError("custom pipeline authoring is disabled")
        custom_pipeline_spec = absolute_file(
            raw.get("custom_pipeline_spec"),
            "custom_pipeline_spec",
        )
        try:
            custom_pipeline_spec.relative_to(
                vault_root / ".vault-meta" / "dispatch-requests"
            )
        except ValueError as exc:
            raise DispatchError(
                "custom_pipeline_spec must be under the coordinator request scratch"
            ) from exc
        try:
            parsed_custom = parse_pipeline_spec(
                custom_pipeline_spec.read_text(encoding="utf-8")
            )
            compile_custom_spec(
                parsed_custom,
                builtin_registry(),
                policy=CustomPipelinePolicy.default(),
                capabilities=("route:resolved",),
            )
        except (HarnessContractError, OSError, ValueError) as exc:
            raise DispatchError(f"custom pipeline is invalid: {exc}") from exc
    elif raw.get("custom_pipeline_spec") is not None:
        raise DispatchError(
            "custom_pipeline_spec requires pipeline=custom"
        )
    completion_policy = str(
        raw.get("completion_policy") or "attention"
    ).strip()
    if completion_policy not in COMPLETION_PASS_LIMITS:
        raise DispatchError(
            "completion_policy must be attention or autonomous"
        )
    execution_pipeline = (
        parsed_custom.baseline_pipeline
        if custom_pipeline_spec is not None
        else pipeline
    )
    custom_has_loop = (
        parsed_custom is not None
        and any(
            item.primitive_id == "bounded_loop"
            for item in parsed_custom.controls
        )
    )
    if (
        execution_pipeline != "engineering/fix"
        and not custom_has_loop
        and completion_policy != "attention"
    ):
        raise DispatchError(
            "autonomous completion_policy requires a code-bounded loop"
        )
    if (
        custom_pipeline_spec is not None
        and completion_policy != parsed_custom.completion_policy
    ):
        raise DispatchError(
            "completion_policy differs from the custom pipeline contract"
        )
    routes = _validated_routes_and_review(raw, custom_pipeline_spec, parsed_custom)
    normalized_context = _validated_context(raw, vault_root, plan_file, parsed_custom)
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
    unknown_reap = set(reap) - {"type", "title", "plan_mode"}
    if unknown_reap:
        raise DispatchError(
            "unknown reap keys: " + ", ".join(sorted(unknown_reap))
        )
    reap_type = require_string(reap.get("type"), "reap.type", maximum=50)
    if reap_type not in SUMMARY_TYPES:
        raise DispatchError("reap.type is not supported")
    reap_title = require_string(reap.get("title"), "reap.title", maximum=200)
    reap_plan_mode = str(reap.get("plan_mode") or "final")
    if reap_plan_mode not in {"final", "shared"}:
        raise DispatchError("reap.plan_mode must be final or shared")
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
        "outcome_contract_sha256": outcome_contract_sha256,
        "origin_surface": origin_surface,
        "placement": placement,
        "pipeline": pipeline,
        "custom_pipeline_spec": custom_pipeline_spec,
        "completion_policy": completion_policy,
        **routes,
        "wiki_context": normalized_context,
        "suggested_agents": normalized_agents,
        "reap": {
            "type": reap_type,
            "title": reap_title,
            "plan_mode": reap_plan_mode,
        },
    }
