"""Bounded review recovery and fresh-boundary restart orchestration."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping

from ..contracts import OwnedResources
from ..state_machine import TERMINAL
from .review import (
    ReviewContext,
    ReviewLaneSession,
    ReviewResult,
    ReviewRound,
    ReviewSessionRequest,
    accept_review_round,
    review_round_envelope,
    review_session_specs,
    verify_review_lane,
)
from .review_gate_contracts import (
    SHA256,
    ReviewGateDecision,
    ReviewGateRun,
    ReviewScopeBoundary,
    _read_json,
    _result_from_payload,
    review_context_sha256,
)
from .review_gate_fresh_boundary import ReviewGateFreshBoundaryMixin
from .review_gate_resolution import ReviewGateResolutionMixin
from review_contract import MATERIAL_SEVERITIES, review_axis_responsibility
from review_resolution import (
    ResolutionError,
    ReviewResolutionEvidence,
    review_transport_identity_sha256,
)


class ReviewGateRecoveryMixin(
    ReviewGateFreshBoundaryMixin,
    ReviewGateResolutionMixin,
):
    """Stable recovery mixin combining exact recovery policy seams."""

    def stage_finalizing_reverification(
        self,
        run: ReviewGateRun,
        lane: ReviewLaneSession,
        round_: ReviewRound,
        result: ReviewResult,
        *,
        recovery_pointer: str,
        recovery_sha256: str,
    ) -> ReviewGateDecision:
        """Consume one accepted old-HEAD result without approving a new HEAD."""

        state = self.read()
        axes = run.execution.request.policy.axes
        simple_axis = axes[0] if len(axes) == 1 else ""
        recovery_path = (self.root / recovery_pointer).resolve()
        try:
            recovery_path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(
                "review recovery evidence escapes the gate"
            ) from exc
        child = self.round_store.read(round_.owner_id, round_.operation_id)
        expected = review_round_envelope(round_, result)
        recovery_identity = {
            "pointer": recovery_pointer,
            "sha256": recovery_sha256,
            "status": "staged",
        }
        stored_recovery = state.get("finalizing_recovery")
        if (
            state.get("status")
            not in {
                "verifying",
                "recovery-verification-required",
                "fresh-boundary-authorized",
            }
            or run.execution.request.policy.depth != "simple"
            or not simple_axis
            or review_axis_responsibility(simple_axis) != "holistic"
            or lane.axis != simple_axis
            or round_.axis != simple_axis
            or result.verdict != "approve"
            or child.state not in {"finalizing", "complete"}
            or child.resources != OwnedResources()
            or child.pending_effect
            or child.accepted_callback_id != expected.callback_id
            or child.accepted_callback_kind != expected.kind
            or child.accepted_callback_sha256 != expected.payload_sha256
            or not recovery_path.is_file()
            or recovery_path.is_symlink()
            or not SHA256.fullmatch(recovery_sha256)
            or hashlib.sha256(recovery_path.read_bytes()).hexdigest()
            != recovery_sha256
            or (
                stored_recovery is not None
                and stored_recovery != recovery_identity
            )
        ):
            raise ValueError("finalizing review recovery identity is invalid")
        if state.get("status") == "verifying":
            cleanup = accept_review_round(
                self.runtime,
                self.round_store,
                lane,
                round_,
                review_round_envelope(round_, result),
            )
            if cleanup is None or cleanup.state != "complete":
                self._mark_attention(run.execution.lanes)
                return ReviewGateDecision("attention-required", lane, round_)
            pointer = self._persist_result(
                run.execution.request.policy.operation_id,
                result,
                final=False,
            )
            self._replace(
                status="recovery-verification-required",
                finalizing_recovery=recovery_identity,
                round_results={result.axis: pointer},
                final_results={},
                evidence={},
            )
        return ReviewGateDecision("verifying", lane, round_)

    def restart_for_boundary(
        self,
        run: ReviewGateRun,
        *,
        boundary: ReviewScopeBoundary,
        context: ReviewContext,
        origin_surface: str,
        cwd: Path,
        product_root: Path,
        prompt_pointer: str,
        callback_root: str,
        callback_wake: str = "",
        prompt_pointers: Mapping[str, str] | None = None,
        max_verify_iterations: int | None = None,
        prepare_lane: (
            Callable[[str, object, object, ReviewRound], None] | None
        ) = None,
    ) -> ReviewGateRun | None:
        exact_product = product_root.expanduser().resolve()

        def invalidate_executor_transport() -> None:
            current = self.read()
            summary_boundary = current.get("fresh_summary_boundary")
            preserve_resolution = (
                isinstance(summary_boundary, dict)
                and summary_boundary.get("previous_context_sha256")
                == boundary.previous_context_sha256
                and summary_boundary.get("next_context_sha256")
                == boundary.next_context_sha256
            )
            paths = (exact_product / ".task-review.json",)
            if not preserve_resolution:
                paths += (exact_product / ".task-review-resolution.json",)
            if any(
                path.is_symlink()
                or path.exists()
                and not path.is_file()
                for path in paths
            ):
                raise ValueError("fresh review executor transport is invalid")
            for path in paths:
                path.unlink(missing_ok=True)

        if (
            max_verify_iterations is not None
            and (
                type(max_verify_iterations) is not int
                or max_verify_iterations < 0
                or max_verify_iterations
                > run.execution.request.policy.max_verify_iterations
            )
        ):
            raise ValueError("fresh review verification budget cannot expand")
        state = self.read()
        if (
            state.get("fresh_reevaluation_used") is True
            and state.get("status")
            in {"fresh-reevaluation", "reviewing", "verifying"}
            and state.get("fresh_boundary")
            == {
                "kind": boundary.kind,
                "previous_context_sha256": (
                    boundary.previous_context_sha256
                ),
                "next_context_sha256": boundary.next_context_sha256,
                "reason": boundary.reason,
            }
        ):
            replay = self.rehydrate()
            if (
                max_verify_iterations is not None
                and replay.execution.request.policy.max_verify_iterations
                != max_verify_iterations
            ):
                raise ValueError(
                    "fresh review verification budget changed across replay"
                )
            invalidate_executor_transport()
            return replay
        if state.get("fresh_reevaluation_used") is True:
            self._mark_attention(run.execution.lanes)
            return None
        authorization = state.get("fresh_boundary_authorization")
        if (
            state.get("status") != "fresh-boundary-authorized"
            or not isinstance(authorization, dict)
            or authorization.get("status") != "authorized"
        ):
            raise ValueError(
                "fresh review requires coordinator-owned authorization"
            )
        self.authorize_fresh_boundary(
            run,
            boundary=boundary,
            authorization_pointer=str(authorization.get("pointer") or ""),
            authorization_sha256=str(authorization.get("sha256") or ""),
        )
        if (
            boundary.previous_context_sha256
            != review_context_sha256(run.execution.request.context)
            or boundary.next_context_sha256 != review_context_sha256(context)
        ):
            raise ValueError(
                "fresh review boundary does not match the context transition"
            )
        role = f"fresh:{boundary.kind}:{boundary.next_context_sha256}"
        suffix = f"-fresh-{hashlib.sha256(role.encode()).hexdigest()[:8]}"
        parent = run.execution.request.policy.operation_id
        operation_id = f"{parent[:128-len(suffix)]}{suffix}"
        request = replace(
            run.execution.request,
            policy=replace(
                run.execution.request.policy,
                operation_id=operation_id,
                max_verify_iterations=(
                    run.execution.request.policy.max_verify_iterations
                    if max_verify_iterations is None
                    else max_verify_iterations
                ),
            ),
            context=context,
            lane_ids=None,
        )
        if prompt_pointers is not None and set(prompt_pointers) != set(
            request.policy.axes
        ):
            raise ValueError(
                "review prompt pointers must cover every exact axis"
            )
        if any(
            request.route_for(axis).profile != "reviewer-callback"
            for axis in request.policy.axes
        ):
            raise ValueError(
                "provider review sessions require the reviewer-callback profile"
            )
        exact_cwd = cwd.expanduser().resolve()
        for identity in review_session_specs(request):
            axis_name = identity.axis
            ReviewSessionRequest(
                spec=identity.spec,
                lane_id=identity.lane_id,
                run_id=identity.run_id,
                origin_surface=origin_surface,
                cwd=exact_cwd,
                product_root=exact_product,
                prompt_pointer=(
                    prompt_pointer
                    if prompt_pointers is None
                    else prompt_pointers[identity.axis]
                ),
                placement="workspace",
                callback_pointer=(
                    f"{callback_root.rstrip('/')}/{axis_name}/"
                    ".review-callback.json"
                ),
                callback_wake=callback_wake,
            )
        invalidate_executor_transport()
        self._replace(
            status="fresh-reevaluation",
            fresh_reevaluation_used=True,
            resolution_operation_id=(
                run.execution.request.policy.operation_id
            ),
            policy={
                **dict(state.get("policy") or {}),
                "max_verify_iterations": (
                    request.policy.max_verify_iterations
                ),
            },
            fresh_boundary={
                "kind": boundary.kind,
                "previous_context_sha256": (
                    boundary.previous_context_sha256
                ),
                "next_context_sha256": boundary.next_context_sha256,
                "reason": boundary.reason,
            },
            active_review_operation_id=operation_id,
            context=self._context(context),
            round_results={},
            final_results={},
            awaiting_resolution={},
            resolution_evidence={},
            resolution_transport_identity_sha256="",
            evidence={},
        )
        try:
            return self._start_execution(
                request=request,
                origin_surface=origin_surface,
                cwd=cwd,
                product_root=product_root,
                prompt_pointer=prompt_pointer,
                callback_root=callback_root,
                callback_wake=callback_wake,
                prompt_pointers=prompt_pointers,
                prepare_lane=prepare_lane,
            )
        except Exception:
            self._mark_attention(run.execution.lanes)
            raise
