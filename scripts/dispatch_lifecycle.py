"""Dispatch ContextPacket, lifecycle contract, and durable run identity."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from harness.context import ContextBuilder, outcome_contract_input
from harness.contracts import RuntimeRoute
from harness.pipeline_builtins import EXECUTABLE_BUILTINS, compiled_builtin
from harness.pipelines import render_contract
from harness.runtime_sessions import RuntimeSessionResult
from harness.store import OperationStore
from harness.workflows.dispatch import (
    DispatchRequest as HarnessDispatchRequest,
    ReviewPolicy,
)
from dispatch_contracts import (
    DispatchError,
    atomic_json,
    ensure_owned_dir,
    exclusive_json,
    read_object,
    utc_now,
)
from dispatch_custom_contracts import (
    approved_outcome_contract_sha256,
    approved_plan_file,
    approved_plan_sha256,
    custom_approval_card_for_request,
    custom_contract_for_request,
    custom_pipeline_for_request,
)
from dispatch_setup import review_policy, run_state_path


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
