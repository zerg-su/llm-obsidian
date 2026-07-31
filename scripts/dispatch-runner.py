#!/usr/bin/env python3
"""Deterministic post-approval runner for one dispatch task split.

The coordinator still owns natural-language parsing, context selection, and the
single user approval. This runner owns route capture, worktree creation,
prompt/metadata rendering, and the dispatch log entry. The generic provider
runtime owns cmux and provider lifecycle mechanics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
from lifecycle_telemetry import (  # noqa: E402
    emit_compiled_pipeline_event,
    emit_lifecycle_event,
)
from harness.contracts import (  # noqa: E402
    ContractError as HarnessContractError,
    RuntimeRoute,
)
from harness.git_ops import GitAdapter, GitError  # noqa: E402
from harness.pipeline_builtins import (  # noqa: E402
    EXECUTABLE_BUILTINS,
    builtin_registry,
    compiled_builtin,
)
from harness.custom_pipelines import (  # noqa: E402
    CustomPipelinePolicy,
    ExplicitPipelineApproval,
    FrozenCustomPipeline,
    PipelineSpec,
    compile_custom_spec,
    freeze_custom_pipeline,
    parse_pipeline_spec,
    render_custom_approval,
)
from harness.pipelines import render_contract  # noqa: E402
from harness.verification import load_profiles  # noqa: E402
from harness.runtime_sessions import (  # noqa: E402
    RuntimeSessionError,
    RuntimeSessionManager,
    RuntimeSessionResult,
)
from harness.store import OperationStore  # noqa: E402
from harness.workflows.dispatch import (  # noqa: E402
    DispatchRequest as HarnessDispatchRequest,
    ReviewPolicy,
    start_dispatch,
)


TASK_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
RUNTIMES = {"claude", "codex"}
REVIEW_MODES = {"simple", "deep", "skip"}
COMPLETION_PASS_LIMITS = {"attention": 2, "autonomous": 3}
SUMMARY_TYPES = {"session", "decision", "runbook", "incident", "service-update", "repo-touch"}
RUN_STATES = {"preparing", "launched", "failed"}
COORDINATOR_ACTION = "return-to-idle-without-polling"
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


class DispatchError(ValueError):
    pass


def custom_authoring_enabled(vault_root: Path) -> bool:
    path = vault_root / "config" / "harness.toml"
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DispatchError("custom pipeline policy is unavailable") from exc
    features = value.get("features")
    enabled = (
        features.get("custom_pipeline_authoring")
        if isinstance(features, dict)
        else None
    )
    if not isinstance(enabled, bool):
        raise DispatchError("custom pipeline authoring switch is invalid")
    return enabled


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
    legacy_review_mode = str(raw.get("review_mode") or "").strip()
    raw_review = raw.get("review")
    if raw_review is not None and legacy_review_mode:
        raise DispatchError(
            "review and review_mode cannot both be present"
        )
    if raw_review is None:
        raw_review = {"mode": legacy_review_mode}
    if not isinstance(raw_review, dict):
        raise DispatchError("review must be an object")
    unknown_review = set(raw_review) - {
        "mode", "cross_model", "runtime", "model", "effort"
    }
    if unknown_review:
        raise DispatchError(
            "unknown review keys: " + ", ".join(sorted(unknown_review))
        )
    review_mode = str(raw_review.get("mode") or "").strip()
    if custom_pipeline_spec is not None:
        if not review_mode:
            review_mode = parsed_custom.review_mode
        elif review_mode != parsed_custom.review_mode:
            raise DispatchError(
                "review.mode differs from the custom pipeline contract"
            )
    if review_mode and review_mode not in REVIEW_MODES:
        raise DispatchError("review.mode must be simple, deep, or skip")
    cross_model = raw_review.get("cross_model", False)
    if not isinstance(cross_model, bool):
        raise DispatchError("review.cross_model must be boolean")
    review_runtime = str(raw_review.get("runtime") or "").strip()
    review_model = str(raw_review.get("model") or "").strip()
    review_effort = str(raw_review.get("effort") or "").strip()
    if review_runtime and review_runtime not in RUNTIMES:
        raise DispatchError("review.runtime must be claude or codex")
    if any(
        "\0" in value or len(value) > maximum
        for value, maximum in (
            (review_model, 128),
            (review_effort, 20),
        )
    ):
        raise DispatchError("review model/effort override is invalid")
    if review_mode == "skip" and any(
        (cross_model, review_runtime, review_model, review_effort)
    ):
        raise DispatchError("skip review cannot carry expert overrides")
    context = raw.get("wiki_context") or []
    if not isinstance(context, list) or len(context) > 5:
        raise DispatchError("wiki_context must contain at most five entries")
    normalized_context: list[dict[str, str]] = []
    context_source_paths = [plan_file]
    for item in context:
        if not isinstance(item, dict):
            raise DispatchError("wiki_context entries must be objects")
        title = require_string(item.get("title"), "wiki_context.title", maximum=200)
        summary = require_string(item.get("summary"), "wiki_context.summary", maximum=500)
        matches = list((vault_root / "wiki").rglob(f"{title}.md"))
        if len(matches) != 1:
            raise DispatchError(f"wiki context target must exist exactly once: {title}")
        normalized_context.append({"title": title, "summary": summary})
        context_source_paths.append(matches[0])
    if parsed_custom is not None:
        allowed_context: dict[str, int] = {}
        for source_path in context_source_paths:
            raw_source = source_path.read_bytes()
            allowed_context[hashlib.sha256(raw_source).hexdigest()] = len(raw_source)
        for pointer in parsed_custom.context_pointers:
            source_size = allowed_context.get(pointer.content_sha256)
            if source_size is None:
                raise DispatchError(
                    "custom context pointer is not in the approved context packet"
                )
            if source_size > pointer.byte_limit:
                raise DispatchError(
                    "custom context pointer exceeds its declared byte limit"
                )
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
        "placement": placement,
        "pipeline": pipeline,
        "custom_pipeline_spec": custom_pipeline_spec,
        "completion_policy": completion_policy,
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
        "review": {
            "mode": review_mode,
            "cross_model": cross_model,
            "runtime": review_runtime,
            "model": review_model,
            "effort": review_effort,
        },
    }


def custom_pipeline_for_request(
    request: dict[str, Any],
) -> FrozenCustomPipeline | None:
    path = request.get("custom_pipeline_spec")
    if request.get("pipeline") != "custom":
        return None
    if not isinstance(path, Path):
        raise DispatchError("custom pipeline spec is unavailable")
    try:
        spec = parse_pipeline_spec(path.read_text(encoding="utf-8"))
        policy = CustomPipelinePolicy.default()
        compiled = compile_custom_spec(
            spec,
            builtin_registry(),
            policy=policy,
            capabilities=("route:resolved",),
        )
        card = render_custom_approval(spec, compiled, policy=policy)
        approval = ExplicitPipelineApproval.for_card(
            definition_sha256=compiled.definition_sha256,
            approval_card=card,
            actor="user",
            decision="approve",
        )
        return freeze_custom_pipeline(spec, compiled, approval, card)
    except (HarnessContractError, OSError, ValueError) as exc:
        raise DispatchError(f"custom pipeline changed after validation: {exc}") from exc


def compiled_pipeline_for_request(request: dict[str, Any]):
    custom = custom_pipeline_for_request(request)
    return custom.compiled if custom is not None else compiled_builtin(request["pipeline"])


def execution_pipeline_for_request(request: dict[str, Any]) -> str:
    custom = custom_pipeline_for_request(request)
    return custom.spec.baseline_pipeline if custom is not None else request["pipeline"]


def task_pipeline_policy(request: dict[str, Any]) -> dict[str, object]:
    """Render honest task metadata without exposing the raw custom spec."""

    policy: dict[str, object] = {
        "name": request["pipeline"],
        "definition_sha256": compiled_pipeline_for_request(
            request
        ).definition_sha256,
        "completion_policy": request["completion_policy"],
        "total_pass_limit": COMPLETION_PASS_LIMITS[
            request["completion_policy"]
        ],
    }
    if request["pipeline"] == "custom":
        policy.update(
            {
                "source": "custom",
                "baseline": execution_pipeline_for_request(request),
            }
        )
    return policy


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
        "<absolute path to wiki/plans/<file>.md>": str(request["plan_file"]),
    }
    for old, new in replacements.items():
        body = body.replace(old, new)
    if request["placement"] == "workspace":
        body = body.replace("the left wiki split", "the coordinator workspace")
    if request["pipeline"] == "custom":
        custom = custom_pipeline_for_request(request)
        if custom is None:
            raise DispatchError("custom pipeline contract is unavailable")
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
                "",
                "Approved custom definition: "
                f"`{custom.definition_sha256}`.",
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
    plan_hash = sha256_file(request["plan_file"])
    review = review_policy(request, config)
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
            "auto_resolve_severities": ["warning", "nit"],
            "escalate_severities": ["blocking"],
        },
        "reap_policy": {
            "mode": "final",
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


def run_state_path(vault_root: Path, request_id: str) -> Path:
    return vault_root / ".vault-meta" / "dispatch-runs" / f"{request_id}.json"


def harness_request(
    request: dict[str, Any],
    config: dict[str, Any],
    effective: dict[str, Any],
) -> HarnessDispatchRequest:
    try:
        context_manifest = request["plan_file"].relative_to(
            request["vault_root"]
        ).as_posix()
    except ValueError as exc:
        raise DispatchError("approved plan escaped the coordinator vault") from exc
    route = RuntimeRoute(
        effective["runtime"],
        effective["model"],
        effective["effort"],
        "executor",
        effective["config_sha256"],
    )
    review = review_policy(request, config)
    return HarnessDispatchRequest(
        task_id=request["request_id"],
        owner_id=request["request_id"],
        plan_sha256=sha256_file(request["plan_file"]),
        context_manifest=context_manifest,
        route=route,
        placement=request["placement"],
        review=review,
        pipeline_name=request["pipeline"],
        completion_policy=request["completion_policy"],
        custom_pipeline=custom_pipeline_for_request(request),
    )


def lifecycle_contract(
    review: ReviewPolicy | None = None,
    pipeline_name: str = "lifecycle/default",
    completion_policy: str = "attention",
) -> dict[str, str]:
    """Compile the lifecycle summary shown before dispatch approval."""

    if pipeline_name not in EXECUTABLE_BUILTINS:
        raise DispatchError("pipeline must name an executable pipeline")
    compiled = compiled_builtin(pipeline_name)
    definition = compiled.definition
    review = review or ReviewPolicy(
        verification_profile="scoped",
        verification_profile_sha256="0" * 64,
    )
    return {
        "pipeline": (
            f"{definition.pipeline_id}/{definition.profile}@{definition.version}"
        ),
        "definition_sha256": compiled.definition_sha256,
        "summary": render_contract(
            compiled,
            completion_policy=completion_policy,
            review_mode=review.mode,
            max_verify_iterations=review.max_verify_iterations,
            verification_profile=review.verification_profile or "unbound",
        ),
    }


def lifecycle_contract_for_request(
    request: dict[str, Any],
    review: ReviewPolicy,
) -> dict[str, str]:
    custom = custom_pipeline_for_request(request)
    if custom is None:
        return lifecycle_contract(
            review,
            request["pipeline"],
            request["completion_policy"],
        )
    policy = CustomPipelinePolicy.default()
    definition = custom.compiled.definition
    return {
        "pipeline": f"custom/{definition.profile}@{definition.version}",
        "definition_sha256": custom.definition_sha256,
        "summary": (
            render_custom_approval(
                custom.spec,
                custom.compiled,
                policy=policy,
            )
            + render_contract(
                custom.compiled,
                completion_policy=request["completion_policy"],
                review_mode=review.mode,
                max_verify_iterations=review.max_verify_iterations,
                verification_profile=(
                    review.verification_profile or "unbound"
                ),
            )
        ),
    }


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
        result = state["result"]
        identity = result.get("harness")
        if (
            not isinstance(identity, dict)
            or identity.get("owner_id") != request_id
            or identity.get("operation_id") != request_id
            or not isinstance(identity.get("run_id"), str)
        ):
            raise DispatchError("launched dispatch result lacks exact harness identity")
        try:
            record = OperationStore(
                vault.resolve() / ".vault-meta" / "harness"
            ).read(request_id, request_id)
        except (RuntimeError, ValueError) as exc:
            raise DispatchError(
                f"launched dispatch identity is unavailable: {exc}"
            ) from exc
        if (
            record.run_id != identity["run_id"]
            or record.lane_id != identity.get("lane_id")
        ):
            raise DispatchError("launched dispatch harness identity drifted")
        return {**result, "idempotent": True}
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


def _child_identity(result: RuntimeSessionResult) -> dict[str, str]:
    surface = result.record.resources.surface_id
    if not surface:
        raise DispatchError("provider runtime returned no exact task surface")
    return {
        "surface": surface,
        "surface_ref": result.surface_ref,
        "workspace": result.workspace_id,
        "workspace_ref": result.workspace_ref,
        "window": result.window_id,
        "window_ref": result.window_ref,
    }


def start(
    request: dict[str, Any],
    spec_sha256: str,
    *,
    runtime_manager: RuntimeSessionManager | None = None,
) -> dict[str, Any]:
    state_path = run_state_path(request["vault_root"], request["request_id"])
    stage = "harness-preflight"
    stage_started = time.monotonic()
    run_started = stage_started
    try:
        state_path, prior = begin_run(request, spec_sha256)
        if prior is not None:
            return prior
        config = load_dispatch_config(request["vault_root"], request["target_repo"])
        session_preview, effective_preview = resolved_routes(request, persist=False)
        session, effective = resolved_routes(request)
        for field in ("runtime", "model", "effort", "config_sha256"):
            if session.get(field) != session_preview.get(field):
                raise DispatchError(f"captured session route drifted at {field}")
            if effective.get(field) != effective_preview.get(field):
                raise DispatchError(f"effective route drifted at {field}")
        lifecycle_request = harness_request(request, config, effective)

        stage = "worktree"
        stage_started = time.monotonic()
        create_worktree(request)
        identity = initialize_task(request)
        atomic_text(
            request["worktree"] / ".task-prompt.md",
            render_task_prompt(request, config),
        )
        emit_lifecycle_event(
            request["worktree"],
            "dispatch-runner-stage",
            actor=stage,
            counts={
                "duration_ms": round(
                    (time.monotonic() - stage_started) * 1000
                )
            },
            vault_root=request["vault_root"],
        )

        stage = "runtime-sync"
        stage_started = time.monotonic()
        sync_codex_profile(request, config, effective)
        emit_lifecycle_event(
            request["worktree"],
            "dispatch-runner-stage",
            actor=stage,
            counts={
                "duration_ms": round(
                    (time.monotonic() - stage_started) * 1000
                )
            },
            vault_root=request["vault_root"],
        )

        runtime = runtime_manager or RuntimeSessionManager.for_root(
            request["vault_root"],
            store_root=request["vault_root"] / ".vault-meta" / "harness",
        )
        prepared: dict[str, str] = {}

        def prepare_surface(opened: RuntimeSessionResult) -> None:
            child = _child_identity(opened)
            write_task_files(
                request,
                config,
                session,
                effective,
                identity,
                {
                    "surface_id": request["origin_surface"],
                    "surface_ref": "",
                },
                child,
            )
            prepared.update(child)

        stage = "provider-runtime"
        stage_started = time.monotonic()
        initial_head_sha = ""
        if (
            execution_pipeline_for_request(request) == "engineering/fix"
            or request["pipeline"] == "custom"
        ):
            initial_head_sha = run_command(
                ["git", "rev-parse", "HEAD"],
                cwd=request["worktree"],
                label="pipeline initial HEAD",
            ).stdout.strip()
        launched = start_dispatch(
            lifecycle_request,
            runtime,
            origin_surface=request["origin_surface"],
            cwd=request["worktree"],
            initial_head_sha=initial_head_sha,
            on_surface_opened=prepare_surface,
        )
        if not prepared:
            raise DispatchError(
                "provider runtime started without preparing the task contract"
            )
        compiled_pipeline = compiled_pipeline_for_request(request)
        emit_compiled_pipeline_event(
            request["worktree"],
            event="dispatch",
            pipeline_id=compiled_pipeline.definition.pipeline_id,
            pipeline_version=compiled_pipeline.definition.version,
            profile=compiled_pipeline.definition.profile,
            compiler_outcome=(
                "custom-compiled"
                if request["pipeline"] == "custom"
                else "compiled"
            ),
            definition_sha=compiled_pipeline.definition_sha256,
            primitive_count=(
                len(compiled_pipeline.definition.steps)
                + len(compiled_pipeline.definition.control_primitives)
            ),
            loop_iteration=0,
            vault_root=request["vault_root"],
        )
        emit_lifecycle_event(
            request["worktree"],
            "dispatch-runner-stage",
            actor=stage,
            counts={
                "duration_ms": round(
                    (time.monotonic() - stage_started) * 1000
                )
            },
            vault_root=request["vault_root"],
        )

        stage = "log"
        stage_started = time.monotonic()
        log_status = "ok"
        try:
            dispatch_log(request, effective, prepared)
        except DispatchError:
            log_status = "degraded"
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
            "task_surface": prepared["surface"],
            "task_surface_ref": prepared["surface_ref"],
            "origin_surface": request["origin_surface"],
            "placement": request["placement"],
            "task_workspace": prepared["workspace"],
            "task_workspace_ref": prepared["workspace_ref"],
            "task_window": prepared["window"],
            "task_window_ref": prepared["window_ref"],
            "log_status": log_status,
            "coordinator_action": COORDINATOR_ACTION,
            "setup_duration_ms": round(
                (time.monotonic() - run_started) * 1000
            ),
            "idempotent": False,
            "harness": {
                "owner_id": launched.record.spec.owner_id,
                "operation_id": launched.record.spec.operation_id,
                "lane_id": launched.record.lane_id,
                "run_id": launched.record.run_id,
            },
        }
        atomic_json(
            state_path,
            {
                "schema_version": 1,
                "request_id": request["request_id"],
                "request_sha256": spec_sha256,
                "task_name": request["task_name"],
                "status": "launched",
                "worktree": str(request["worktree"]),
                "result": result,
                "updated_at": utc_now(),
            },
        )
        emit_lifecycle_event(
            request["worktree"],
            "dispatch-runner-stage",
            actor=stage,
            counts={
                "duration_ms": round(
                    (time.monotonic() - stage_started) * 1000
                )
            },
            status=log_status,
            vault_root=request["vault_root"],
        )
        return result
    except (
        DispatchError,
        RoutingError,
        RuntimeSessionError,
        RuntimeError,
        OSError,
        ValueError,
    ) as exc:
        if state_path.is_file():
            current = read_object(state_path)
            if current.get("status") == "preparing":
                mark_failed(state_path, stage, str(exc))
        if request["worktree"].is_dir():
            emit_lifecycle_event(
                request["worktree"],
                "dispatch-runner-stage",
                actor=stage,
                counts={
                    "duration_ms": round(
                        (time.monotonic() - stage_started) * 1000
                    )
                },
                status="error",
                vault_root=request["vault_root"],
            )
        raise DispatchError(
            f"{stage} failed for request {request['request_id']}; "
            f"no retry was attempted: {exc}"
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
            review = review_policy(request, config)
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
                "placement": request["placement"],
                "pipeline": lifecycle_contract_for_request(request, review),
                "review": {
                    "mode": review.mode,
                    "cross_model": review.cross_model,
                    "runtime": review.runtime,
                    "model": review.model,
                    "effort": review.effort,
                    "max_verify_iterations": (
                        review.max_verify_iterations
                    ),
                    "verification_profile": (
                        review.verification_profile
                    ),
                    "verification_profile_sha256": (
                        review.verification_profile_sha256
                    ),
                },
            }, sort_keys=True))
            return 0
        print(json.dumps(start(request, spec_sha256), ensure_ascii=False, sort_keys=True))
        return 0
    except (
        DispatchError,
        RoutingError,
        ContractError,
        RuntimeSessionError,
        RuntimeError,
        OSError,
        ValueError,
    ) as exc:
        die(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
