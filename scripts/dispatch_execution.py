"""Execute one approved dispatch through the harness runtime."""

from __future__ import annotations

import time
from typing import Any

from lifecycle_telemetry import emit_compiled_pipeline_event, emit_lifecycle_event
from model_routing import RoutingError
from harness.runtime_sessions import (
    RuntimeSessionError,
    RuntimeSessionManager,
    RuntimeSessionResult,
)
from harness.workflows.dispatch import start_dispatch
from harness.dashboard_facade import DashboardLaunchReceipt, launch_facade_dashboard
from dispatch_contracts import (
    COORDINATOR_ACTION,
    DispatchError,
    atomic_json,
    atomic_text,
    read_object,
    utc_now,
)
from dispatch_custom_contracts import (
    compiled_pipeline_for_request,
    execution_pipeline_for_request,
)
from dispatch_lifecycle import (
    _child_identity,
    begin_run,
    harness_request,
    mark_failed,
)
from dispatch_setup import (
    load_dispatch_config,
    render_task_prompt,
    resolved_routes,
    run_state_path,
)
from dispatch_workspace import (
    create_worktree,
    dispatch_log,
    initialize_task,
    run_command,
    sync_codex_profile,
    write_task_files,
)
from approved_plan_snapshot import (
    PlanSnapshotError,
    bind_approved_plan_snapshot,
)


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
        stage = "plan-snapshot"
        stage_started = time.monotonic()
        request = bind_approved_plan_snapshot(request)
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
        dashboard_receipt: dict[str, DashboardLaunchReceipt] = {}

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
            dashboard_receipt["root"] = launch_facade_dashboard(
                vault=request["vault_root"],
                store=request["vault_root"] / ".vault-meta" / "harness",
                caller_surface=child["surface"],
                facade="dispatch",
                request_id=identity["task_id"],
                root_operation_id=identity["task_id"],
            )

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
            "observer": dashboard_receipt["root"].__dict__,
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
        PlanSnapshotError,
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
