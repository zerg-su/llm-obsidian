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
import secrets
import stat
import subprocess
import sys
import time
import uuid
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
from outcome_contract import OutcomeContractError, extract_from_bytes  # noqa: E402
from lifecycle_telemetry import (  # noqa: E402
    emit_compiled_pipeline_event,
    emit_lifecycle_event,
)
from harness.contracts import (  # noqa: E402
    ContractError as HarnessContractError,
    RuntimeRoute,
)
from harness.context import ContextBuilder, outcome_contract_input  # noqa: E402
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
    pipeline_spec_payload,
    render_custom_approval,
)
from harness.pipelines import CompiledPipeline, render_contract  # noqa: E402
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
from dispatch_contracts import (  # noqa: E402
    COMPLETION_PASS_LIMITS,
    COORDINATOR_ACTION,
    DEFAULT_DISPATCH,
    REVIEW_MODES,
    RUN_STATES,
    TASK_LOCAL_GIT_EXCLUDES,
    DispatchError,
    _approval_lock,
    _review_from_snapshot,
    absolute_dir,
    approved_outcome_contract_sha256,
    approved_plan_file,
    approved_plan_sha256,
    atomic_json,
    atomic_text,
    compiled_pipeline_for_request,
    custom_approval_card_for_request,
    custom_approval_challenge,
    custom_approval_path,
    custom_approval_plan_path,
    custom_approval_snapshot,
    custom_contract_for_request,
    custom_pipeline_for_request,
    ensure_owned_dir,
    exclusive_json,
    execution_pipeline_for_request,
    persist_custom_approval_challenge,
    read_object,
    require_string,
    sha256_file,
    task_pipeline_policy,
    utc_now,
    validate_request,
)
from dispatch_setup import (  # noqa: E402
    create_worktree,
    dispatch_log,
    ensure_task_git_excludes,
    extract_prompt_body,
    initialize_task,
    keep_plan_branch,
    load_dispatch_config,
    materialize_current_context,
    render_task_prompt,
    resolved_routes,
    review_policy,
    run_state_path,
    run_command,
    sync_codex_profile,
    write_task_files,
)


HOST_APPROVAL_PROGRAM = Path("/usr/bin/osascript")




def die(message: str, code: int = 3) -> NoReturn:
    print(f"dispatch-runner: {message}", file=sys.stderr)
    raise SystemExit(code)






















































def host_custom_approval_decision(challenge: dict[str, Any]) -> str:
    """Ask through a host-owned macOS dialog; stdin/argv cannot approve."""

    if sys.platform != "darwin" or not HOST_APPROVAL_PROGRAM.is_file():
        raise DispatchError(
            "custom approval requires the macOS host confirmation dialog"
        )
    script = """
on run argv
  set challengeDigest to item 1 of argv
  set messageText to "Approve exact custom pipeline challenge?" & return & return & challengeDigest
  set answer to display dialog messageText with title "LLM Obsidian" buttons {"Reject", "Revise", "Approve"} default button "Revise"
  return button returned of answer
end run
""".strip()
    try:
        result = subprocess.run(
            [
                str(HOST_APPROVAL_PROGRAM),
                "-e",
                script,
                challenge["challenge_sha256"],
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DispatchError(f"host approval dialog failed: {exc}") from exc
    if result.returncode != 0:
        raise DispatchError("host approval dialog did not return a decision")
    decision = result.stdout.strip().lower()
    if decision not in {"approve", "reject", "revise"}:
        raise DispatchError("host approval dialog returned an invalid decision")
    return decision


def record_custom_approval_decision(
    request: dict[str, Any],
    challenge: dict[str, Any],
    challenge_sha256: str,
    *,
    host_decision: Any = host_custom_approval_decision,
) -> dict[str, Any]:
    """Persist a decision produced only by the host confirmation boundary."""

    if challenge_sha256 != challenge["challenge_sha256"]:
        raise DispatchError("custom approval challenge digest does not match")
    path = custom_approval_path(request)
    if path.is_symlink() or not path.is_file():
        raise DispatchError("custom pipeline must be validated before decision")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise DispatchError("custom approval challenge is not owner-only")
    lock = _approval_lock(path)
    try:
        record = read_object(path)
        if record.get("challenge") != challenge:
            raise DispatchError(
                "custom approval challenge no longer matches validation"
            )
        if record.get("status") != "pending":
            raise DispatchError("custom approval decision is already durable")
        decision = host_decision(challenge)
        if decision not in {"approve", "reject", "revise"}:
            raise DispatchError("host approval decision is invalid")
        token = secrets.token_hex(32) if decision == "approve" else ""
        record.update(
            {
                "status": "approved" if decision == "approve" else decision,
                "decision": decision,
                "actor": "host-user-dialog",
                "approval_token_sha256": (
                    hashlib.sha256(token.encode()).hexdigest() if token else ""
                ),
            }
        )
        atomic_json(path, record)
    finally:
        os.close(lock)
    result = {
        "schema_version": 1,
        "request_id": request["request_id"],
        "status": record["status"],
        "decision": decision,
    }
    if token:
        result["approval_token"] = token
    return result


def authorize_custom_request(
    request: dict[str, Any],
    request_sha256: str,
    approval_token: str,
) -> dict[str, Any]:
    """Consume the host decision while installing its immutable snapshot."""

    if not re.fullmatch(r"[0-9a-f]{64}", approval_token):
        raise DispatchError("custom start requires --approval-token")
    path = custom_approval_path(request)
    if path.is_symlink() or not path.is_file():
        raise DispatchError("custom pipeline must be validated before start")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise DispatchError("custom approval challenge is not owner-only")
    lock = _approval_lock(path)
    try:
        persisted = read_object(path)
        challenge = persisted.get("challenge")
        snapshot = persisted.get("snapshot")
        if not isinstance(challenge, dict) or not isinstance(snapshot, dict):
            raise DispatchError("custom approval snapshot is unavailable")
        if challenge.get("request_sha256") != request_sha256:
            raise DispatchError("custom approval request bytes changed")
        coordinator = challenge.get("coordinator")
        if coordinator != {
            "origin_surface": request["origin_surface"],
            "origin_session": request["origin_session"],
            "placement": request["placement"],
        }:
            raise DispatchError("custom approval coordinator identity changed")
        if (
            persisted.get("status") != "approved"
            or persisted.get("decision") != "approve"
            or persisted.get("actor") != "host-user-dialog"
        ):
            raise DispatchError("custom pipeline has no approved decision receipt")
        if persisted.get("approval_token_sha256") != hashlib.sha256(
            approval_token.encode()
        ).hexdigest():
            raise DispatchError("custom approval token does not match")
        plan_path = custom_approval_plan_path(request)
        plan_info = plan_path.stat() if plan_path.exists() else None
        if (
            plan_path.is_symlink()
            or not plan_path.is_file()
            or plan_info is None
            or plan_info.st_uid != os.getuid()
            or stat.S_IMODE(plan_info.st_mode) & 0o077
            or sha256_file(plan_path) != challenge.get("plan_sha256")
        ):
            raise DispatchError("approved plan snapshot is unavailable")
        try:
            spec = parse_pipeline_spec(
                json.dumps(snapshot["pipeline_spec"], sort_keys=True)
            )
            policy = CustomPipelinePolicy.default()
            compiled = compile_custom_spec(
                spec,
                builtin_registry(),
                policy=policy,
                capabilities=("route:resolved",),
            )
        except (KeyError, HarnessContractError, ValueError) as exc:
            raise DispatchError("approved custom snapshot is invalid") from exc
        card = render_custom_approval(spec, compiled, policy=policy) + "\n".join(
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
        prompt = snapshot.get("prompt")
        if (
            compiled.definition_sha256 != challenge.get("definition_sha256")
            or hashlib.sha256(
                json.dumps(
                    pipeline_spec_payload(spec),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            != challenge.get("pipeline_spec_sha256")
            or hashlib.sha256(card.encode()).hexdigest()
            != challenge.get("approval_card_sha256")
            or card != snapshot.get("approval_card")
            or not isinstance(prompt, str)
            or hashlib.sha256(prompt.encode()).hexdigest()
            != challenge.get("prompt_sha256")
            or snapshot.get("plan_sha256") != challenge.get("plan_sha256")
            or snapshot.get("effective") != challenge.get("route")
            or snapshot.get("review") != challenge.get("review")
        ):
            raise DispatchError("approved custom snapshot no longer matches")
        expected_session = {
            "schema_version": 1,
            "session_id": request["origin_session"],
            **request["session_route"],
            "config_sha256": snapshot["effective"]["config_sha256"],
        }
        if snapshot.get("session") != expected_session:
            raise DispatchError("approved custom session snapshot changed")
        review = _review_from_snapshot(snapshot["review"])
        persisted["status"] = "consumed"
        atomic_json(path, persisted)
    finally:
        os.close(lock)
    approved = dict(request)
    approved["_custom_approval"] = ExplicitPipelineApproval.for_card(
        definition_sha256=compiled.definition_sha256,
        approval_card=card,
        actor="host-user-dialog",
        decision="approve",
    )
    approved["_approved_custom_contract"] = (spec, compiled, policy, card)
    approved["_approved_plan_file"] = plan_path
    approved["_approved_plan_sha256"] = challenge["plan_sha256"]
    approved["_approved_prompt"] = prompt
    approved["_approved_session_route"] = snapshot["session"]
    approved["_approved_effective_route"] = snapshot["effective"]
    approved["_approved_review"] = review
    return approved




































def harness_request(
    request: dict[str, Any],
    config: dict[str, Any],
    effective: dict[str, Any],
) -> HarnessDispatchRequest:
    outcome_digest = approved_outcome_contract_sha256(request)
    packet_root = (
        request["vault_root"]
        / ".vault-meta"
        / "harness"
        / "context-packets"
    )
    manifest = ContextBuilder(packet_root).build(
        request["request_id"],
        (
            outcome_contract_input(
                approved_plan_file(request),
                expected_sha256=outcome_digest,
            ),
        ),
        metadata={
            "task_id": request["request_id"],
            "approved_plan_sha256": approved_plan_sha256(request),
            "outcome_contract_sha256": outcome_digest,
        },
    )
    context_manifest = (
        packet_root / manifest.packet_id / "manifest.json"
    ).relative_to(request["vault_root"]).as_posix()
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
        plan_sha256=approved_plan_sha256(request),
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
    if request.get("pipeline") != "custom":
        return lifecycle_contract(
            review,
            request["pipeline"],
            request["completion_policy"],
        )
    spec, compiled, policy, _base_card = custom_contract_for_request(request)
    card = custom_approval_card_for_request(request)
    definition = compiled.definition
    return {
        "pipeline": f"custom/{definition.profile}@{definition.version}",
        "definition_sha256": compiled.definition_sha256,
        "summary": (
            card
            + render_contract(
                compiled,
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

        stage = "runtime-sync"
        stage_started = time.monotonic()
        sync_codex_profile(request, config, effective)

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
    approve = sub.add_parser("approve")
    approve.add_argument("--spec", type=Path, required=True)
    approve.add_argument("--challenge-sha256", required=True)
    launch = sub.add_parser("start")
    launch.add_argument("--spec", type=Path, required=True)
    launch.add_argument("--approval-token", default="")
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
        if args.command in {"validate", "approve"}:
            config = load_dispatch_config(request["vault_root"], request["target_repo"])
            session, effective = resolved_routes(request, persist=False)
            review = review_policy(request, config)
            prompt_request = request
            if request["pipeline"] == "custom":
                prompt_request = dict(request)
                prompt_request["_approved_plan_file"] = (
                    custom_approval_plan_path(request)
                )
            prompt = render_task_prompt(prompt_request, config)
            result = {
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
            }
            challenge = None
            if request["pipeline"] == "custom":
                challenge = custom_approval_challenge(
                    request,
                    request_sha256=spec_sha256,
                    effective=effective,
                    review=review,
                    prompt=prompt,
                )
                if args.command == "validate":
                    persist_custom_approval_challenge(
                        request,
                        challenge,
                        custom_approval_snapshot(
                            request,
                            challenge,
                            session=session,
                            effective=effective,
                            review=review,
                            prompt=prompt,
                        ),
                    )
                result["challenge_sha256"] = challenge["challenge_sha256"]
            if args.command == "approve":
                if challenge is None:
                    raise DispatchError(
                        "approve is available only for custom pipelines"
                    )
                result = record_custom_approval_decision(
                    request,
                    challenge,
                    args.challenge_sha256,
                )
            print(json.dumps(result, sort_keys=True))
            return 0
        if request["pipeline"] == "custom":
            request = authorize_custom_request(
                request,
                spec_sha256,
                args.approval_token,
            )
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
