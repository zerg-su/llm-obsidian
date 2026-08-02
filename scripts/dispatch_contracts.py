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


class DispatchError(ValueError):
    """The dispatch request violates its pre-effect contract."""


def _approval_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(
        path.with_suffix(".lock"),
        flags,
        0o600,
    )
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(descriptor)
        raise DispatchError("custom approval lock is not owner-only")
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


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


def custom_pipeline_for_request(
    request: dict[str, Any],
) -> FrozenCustomPipeline | None:
    """Freeze a custom contract only from explicit bound approval evidence."""

    if request.get("pipeline") != "custom":
        return None
    approval = request.get("_custom_approval")
    if not isinstance(approval, ExplicitPipelineApproval):
        raise DispatchError("custom pipeline requires exact approval evidence")
    spec, compiled, policy, card = custom_contract_for_request(request)
    try:
        return freeze_custom_pipeline(spec, compiled, approval, card)
    except (HarnessContractError, OSError, ValueError) as exc:
        raise DispatchError(f"custom pipeline changed after approval: {exc}") from exc


def custom_contract_for_request(
    request: dict[str, Any],
) -> tuple[PipelineSpec, CompiledPipeline, CustomPipelinePolicy, str]:
    """Compile the effect-free custom contract for preview or approval."""

    frozen = request.get("_approved_custom_contract")
    if isinstance(frozen, tuple) and len(frozen) == 4:
        return frozen
    path = request.get("custom_pipeline_spec")
    if request.get("pipeline") != "custom":
        raise DispatchError("custom pipeline contract requires pipeline=custom")
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
        return spec, compiled, policy, card
    except (HarnessContractError, OSError, ValueError) as exc:
        raise DispatchError(f"custom pipeline changed after validation: {exc}") from exc


def custom_approval_card_for_request(request: dict[str, Any]) -> str:
    """Render model-declared plus unavoidable inherited harness authority."""

    _spec, _compiled, _policy, base = custom_contract_for_request(request)
    return base + "\n".join(
        (
            "Inherited harness permissions: cmux-target:policy-only",
            "Inherited harness side effects: cmux-surface:policy-only",
            "Coordinator target: "
            f"surface={request['origin_surface']}; "
            f"session={request['origin_session']}; "
            f"placement={request['placement']}",
            "",
        )
    )


def custom_approval_challenge(
    request: dict[str, Any],
    *,
    request_sha256: str,
    effective: dict[str, Any],
    review: ReviewPolicy,
    prompt: str,
) -> dict[str, Any]:
    """Bind one exact pre-effect validation result to later approval."""

    spec, compiled, _policy, _base_card = custom_contract_for_request(request)
    card = custom_approval_card_for_request(request)
    path = request["custom_pipeline_spec"]
    payload = {
        "schema_version": 1,
        "request_id": request["request_id"],
        "request_sha256": request_sha256,
        "custom_spec_sha256": sha256_file(path),
        "pipeline_spec_sha256": hashlib.sha256(
            json.dumps(
                pipeline_spec_payload(spec),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "definition_sha256": compiled.definition_sha256,
        "approval_card_sha256": hashlib.sha256(card.encode()).hexdigest(),
        "plan_sha256": sha256_file(request["plan_file"]),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "coordinator": {
            "origin_surface": request["origin_surface"],
            "origin_session": request["origin_session"],
            "placement": request["placement"],
        },
        "route": effective,
        "review": {
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
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {**payload, "challenge_sha256": digest}


def custom_approval_path(request: dict[str, Any]) -> Path:
    return (
        request["vault_root"]
        / ".vault-meta"
        / "dispatch-approval-challenges"
        / f"{request['request_id']}.json"
    )


def custom_approval_plan_path(request: dict[str, Any]) -> Path:
    return custom_approval_path(request).with_suffix(".plan.md")


def approved_plan_file(request: dict[str, Any]) -> Path:
    value = request.get("_approved_plan_file")
    return value if isinstance(value, Path) else request["plan_file"]


def approved_plan_sha256(request: dict[str, Any]) -> str:
    value = request.get("_approved_plan_sha256")
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return sha256_file(request["plan_file"])


def approved_outcome_contract_sha256(request: dict[str, Any]) -> str:
    try:
        return extract_from_bytes(approved_plan_file(request).read_bytes()).sha256
    except (OSError, OutcomeContractError) as exc:
        raise DispatchError(f"approved plan Outcome Contract is invalid: {exc}") from exc


def _review_snapshot(review: ReviewPolicy) -> dict[str, Any]:
    return {
        "mode": review.mode,
        "cross_model": review.cross_model,
        "runtime": review.runtime,
        "model": review.model,
        "effort": review.effort,
        "max_verify_iterations": review.max_verify_iterations,
        "verification_profile": review.verification_profile,
        "verification_profile_sha256": review.verification_profile_sha256,
    }


def _review_from_snapshot(value: dict[str, Any]) -> ReviewPolicy:
    return ReviewPolicy(
        depth="deep" if value["mode"] == "deep" else "simple",
        cross_model=value["cross_model"],
        enabled=value["mode"] != "skip",
        runtime=value["runtime"],
        model=value["model"],
        effort=value["effort"],
        verification_profile=value["verification_profile"],
        verification_profile_sha256=value["verification_profile_sha256"],
    )


def custom_approval_snapshot(
    request: dict[str, Any],
    challenge: dict[str, Any],
    *,
    session: dict[str, Any],
    effective: dict[str, Any],
    review: ReviewPolicy,
    prompt: str,
) -> dict[str, Any]:
    spec, _compiled, _policy, _card = custom_contract_for_request(request)
    return {
        "schema_version": 1,
        "pipeline_spec": pipeline_spec_payload(spec),
        "approval_card": custom_approval_card_for_request(request),
        "prompt": prompt,
        "plan_sha256": challenge["plan_sha256"],
        "session": session,
        "effective": effective,
        "review": _review_snapshot(review),
    }


def persist_custom_approval_challenge(
    request: dict[str, Any],
    challenge: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    path = custom_approval_path(request)
    ensure_owned_dir(path.parent)
    plan_path = custom_approval_plan_path(request)
    plan_text = request["plan_file"].read_text(encoding="utf-8")
    if hashlib.sha256(plan_text.encode()).hexdigest() != challenge["plan_sha256"]:
        raise DispatchError("custom approval plan changed during validation")
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (
            path.is_symlink()
            or not path.is_file()
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise DispatchError("custom approval challenge is not owner-only")
        existing = read_object(path)
        if (
            existing.get("challenge") != challenge
            or existing.get("snapshot") != snapshot
            or not plan_path.is_file()
            or sha256_file(plan_path) != challenge["plan_sha256"]
        ):
            raise DispatchError(
                "custom approval challenge changed; use a fresh request_id"
            )
        return
    atomic_text(plan_path, plan_text)
    exclusive_json(
        path,
        {
            "schema_version": 1,
            "request_id": request["request_id"],
            "status": "pending",
            "decision": "",
            "actor": "",
            "approval_token_sha256": "",
            "challenge": challenge,
            "snapshot": snapshot,
        },
    )


def compiled_pipeline_for_request(request: dict[str, Any]):
    if request.get("pipeline") == "custom":
        return custom_contract_for_request(request)[1]
    return compiled_builtin(request["pipeline"])


def execution_pipeline_for_request(request: dict[str, Any]) -> str:
    if request.get("pipeline") == "custom":
        return custom_contract_for_request(request)[0].baseline_pipeline
    return request["pipeline"]


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
