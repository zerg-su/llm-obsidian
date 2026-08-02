"""Bounded review recovery and resolution transitions."""

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
from review_contract import MATERIAL_SEVERITIES
from review_resolution import (
    ResolutionError,
    ReviewResolutionEvidence,
    review_transport_identity_sha256,
)


class ReviewGateRecoveryMixin:
    """Own fresh-boundary, resolution, and same-session recovery policy."""

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
        recovery_path = (self.root / recovery_pointer).resolve()
        try:
            recovery_path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(
                "review recovery evidence escapes the gate"
            ) from exc
        child = self.round_store.read(
            round_.owner_id, round_.operation_id
        )
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
            or run.execution.request.policy.axes != ("holistic",)
            or lane.axis != "holistic"
            or round_.axis != "holistic"
            or result.verdict != "approve"
            or child.state not in {"finalizing", "complete"}
            or child.resources != OwnedResources()
            or child.pending_effect
            or child.accepted_callback_id != expected.callback_id
            or child.accepted_callback_kind != expected.kind
            or child.accepted_callback_sha256
            != expected.payload_sha256
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
            raise ValueError(
                "finalizing review recovery identity is invalid"
            )
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

    def authorize_fresh_boundary(
        self,
        run: ReviewGateRun,
        *,
        boundary: ReviewScopeBoundary,
        authorization_pointer: str,
        authorization_sha256: str,
    ) -> None:
        """Bind a coordinator-owned authorization to one exact context change."""

        state = self.read()
        pointer = Path(authorization_pointer)
        authorization_path = (self.root / pointer).resolve()
        try:
            authorization_path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(
                "fresh review authorization escapes the gate"
            ) from exc
        expected_keys = {
            "schema_version",
            "operation_id",
            "kind",
            "previous_context_sha256",
            "next_context_sha256",
            "reason",
            "authorization_provenance",
            "verification_operation_id",
            "verification_receipt_sha256",
            "status",
        }
        authorization = (
            _read_json(authorization_path)
            if authorization_path.is_file()
            and not authorization_path.is_symlink()
            else None
        )
        identity = {
            "pointer": pointer.as_posix(),
            "sha256": authorization_sha256,
            "status": "authorized",
        }
        if (
            state.get("status")
            not in {
                "attention-required",
                "recovery-verification-required",
                "fresh-boundary-authorized",
            }
            or not isinstance(authorization, dict)
            or set(authorization) != expected_keys
            or authorization.get("schema_version") != 1
            or authorization.get("operation_id")
            != run.execution.request.policy.operation_id
            or authorization.get("kind") != boundary.kind
            or authorization.get("previous_context_sha256")
            != boundary.previous_context_sha256
            or authorization.get("next_context_sha256")
            != boundary.next_context_sha256
            or authorization.get("reason") != boundary.reason
            or not str(
                authorization.get("authorization_provenance") or ""
            )
            in {"coordinator-approved", "pipeline-verification"}
            or not str(
                authorization.get("verification_operation_id") or ""
            ).strip()
            or not SHA256.fullmatch(
                str(authorization.get("verification_receipt_sha256") or "")
            )
            or authorization.get("status") != "authorized"
            or not SHA256.fullmatch(authorization_sha256)
            or hashlib.sha256(authorization_path.read_bytes()).hexdigest()
            != authorization_sha256
            or (
                state.get("status") == "fresh-boundary-authorized"
                and state.get("fresh_boundary_authorization") != identity
            )
        ):
            raise ValueError("fresh review authorization is invalid")
        if state.get("status") != "fresh-boundary-authorized":
            self._replace(
                status="fresh-boundary-authorized",
                fresh_boundary_authorization=identity,
            )

    def authorize_fresh_summary_boundary(
        self,
        run: ReviewGateRun,
        *,
        boundary: ReviewScopeBoundary,
        context: ReviewContext,
        authorization_pointer: str,
        authorization_sha256: str,
    ) -> None:
        """Authorize one fresh review when only approved summary context changed."""

        state = self.read()
        previous = run.execution.request.context
        pointer = Path(authorization_pointer)
        authorization_path = (self.root / pointer).resolve()
        try:
            authorization_path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(
                "fresh summary authorization escapes the gate"
            ) from exc
        authorization = (
            _read_json(authorization_path)
            if authorization_path.is_file()
            and not authorization_path.is_symlink()
            else None
        )
        expected_authorization = {
            "schema_version",
            "operation_id",
            "kind",
            "previous_context_sha256",
            "next_context_sha256",
            "reason",
            "authorization_provenance",
            "verification_operation_id",
            "verification_receipt_sha256",
            "status",
        }
        identity = {
            "pointer": pointer.as_posix(),
            "sha256": authorization_sha256,
            "status": "authorized",
        }
        results = self._final_results(run.execution)
        summary_only = (
            previous.head_sha == context.head_sha
            and previous.verification_profile
            == context.verification_profile
            and previous.verification_profile_sha256
            == context.verification_profile_sha256
            and bool(previous.implementer_summary_sha256)
            and bool(context.implementer_summary_sha256)
            and previous.implementer_summary_sha256
            != context.implementer_summary_sha256
            and previous.manifest != context.manifest
        )
        lanes_quiescent = True
        for lane in run.execution.lanes:
            round_ = run.rounds.get(lane.axis)
            if round_ is None:
                lanes_quiescent = False
                break
            parent = self.round_store.read(lane.owner_id, lane.operation_id)
            child = self.round_store.read(round_.owner_id, round_.operation_id)
            if (
                parent.state not in TERMINAL
                or child.state not in TERMINAL
                or parent.resources != OwnedResources()
                or child.resources != OwnedResources()
                or parent.pending_effect
                or child.pending_effect
            ):
                lanes_quiescent = False
                break
        if (
            state.get("status") != "approved"
            or state.get("fresh_reevaluation_used") is True
            or state.get("context") != self._context(previous)
            or boundary.kind != "context"
            or boundary.previous_context_sha256
            != review_context_sha256(previous)
            or boundary.next_context_sha256
            != review_context_sha256(context)
            or not summary_only
            or results is None
            or any(result.verdict != "approve" for result in results.values())
            or not lanes_quiescent
            or not isinstance(authorization, dict)
            or set(authorization) != expected_authorization
            or authorization.get("schema_version") != 1
            or authorization.get("operation_id")
            != run.execution.request.policy.operation_id
            or authorization.get("kind") != boundary.kind
            or authorization.get("previous_context_sha256")
            != boundary.previous_context_sha256
            or authorization.get("next_context_sha256")
            != boundary.next_context_sha256
            or authorization.get("reason") != boundary.reason
            or authorization.get("authorization_provenance")
            != "coordinator-approved"
            or not str(
                authorization.get("verification_operation_id") or ""
            ).strip()
            or not SHA256.fullmatch(
                str(authorization.get("verification_receipt_sha256") or "")
            )
            or authorization.get("status") != "authorized"
            or not SHA256.fullmatch(authorization_sha256)
            or hashlib.sha256(authorization_path.read_bytes()).hexdigest()
            != authorization_sha256
        ):
            raise ValueError(
                "fresh summary review authorization is invalid"
            )
        history = state.get("prior_approved_boundaries")
        if history not in (None, []):
            raise ValueError(
                "fresh summary review boundary is already recorded"
            )
        prior = {
            "active_review_operation_id": state.get(
                "active_review_operation_id"
            ),
            "context": state.get("context"),
            "evidence": state.get("evidence"),
            "final_results": state.get("final_results"),
            "resolution_evidence": state.get("resolution_evidence"),
            "resolution_transport_identity_sha256": state.get(
                "resolution_transport_identity_sha256", ""
            ),
        }
        self._replace(
            status="fresh-boundary-authorized",
            fresh_boundary_authorization=identity,
            fresh_summary_boundary={
                "previous_context_sha256": boundary.previous_context_sha256,
                "next_context_sha256": boundary.next_context_sha256,
            },
            prior_approved_boundaries=[prior],
        )

    def defer_round_for_resolution(
        self,
        run: ReviewGateRun,
        lane: ReviewLaneSession,
        round_: ReviewRound,
        result: ReviewResult,
    ) -> ReviewGateDecision:
        """Accept one deep-axis receipt but keep its parent alive for new HEAD."""

        if self._parent_callback_timed_out(lane):
            self._replace(status="attention-required")
            return ReviewGateDecision("attention-required", lane, round_)
        envelope = review_round_envelope(round_, result)
        payload = envelope.payload
        if (
            round_.parent_operation_id != lane.operation_id
            or round_.owner_id != lane.owner_id
            or round_.lane_id != lane.lane_id
            or result.axis != lane.axis
            or result.verification_iteration
            != lane.verification_iteration
            or payload.get("parent_session_operation_id")
            != lane.operation_id
        ):
            raise ValueError(
                "deferred review callback does not match its parent lane"
            )
        self.runtime.accept_callback(envelope)
        child = self.round_store.read(
            round_.owner_id, round_.operation_id
        )
        if child.state not in TERMINAL:
            if child.state != "finalizing":
                self.round_store.transition(
                    child.spec.owner_id,
                    child.spec.operation_id,
                    "finalizing",
                )
            self.round_store.transition(
                child.spec.owner_id, child.spec.operation_id, "exiting"
            )
            self.round_store.transition(
                child.spec.owner_id, child.spec.operation_id, "complete"
            )
        pointer = self._persist_result(
            run.execution.request.policy.operation_id,
            result,
            final=False,
        )
        state = self.read()
        rounds = dict(state.get("round_results") or {})
        rounds[result.axis] = pointer
        if result.verdict == "blocked":
            self._replace(round_results=rounds)
            self._mark_attention(run.execution.lanes)
            return ReviewGateDecision("attention-required", lane, round_)
        awaiting = dict(state.get("awaiting_resolution") or {})
        material_finding_ids = [
            finding.finding_id
            for finding in result.findings
            if finding.severity in MATERIAL_SEVERITIES
        ]
        awaiting[result.axis] = {
            "pointer": pointer,
            "reviewed_head_sha": (
                run.execution.request.context.head_sha
            ),
            "review_operation_id": (
                run.execution.request.policy.operation_id
            ),
            "round_operation_id": round_.operation_id,
            "round_run_id": round_.run_id,
            "callback_id": envelope.callback_id,
            "callback_sha256": envelope.payload_sha256,
            "material_finding_ids": material_finding_ids,
        }
        self._replace(
            status="awaiting-resolution",
            round_results=rounds,
            awaiting_resolution=awaiting,
            resolution_transport_identity_sha256="",
            lanes=[self._lane(item) for item in run.execution.lanes],
        )
        return ReviewGateDecision(
            "awaiting-resolution", lane, round_
        )

    def continue_after_resolution(
        self,
        run: ReviewGateRun,
        lane: ReviewLaneSession,
        *,
        context: ReviewContext,
        resolution: ReviewResolutionEvidence,
        review_identity_sha256: str,
        verification_prompt_pointer: str,
        callback_pointer: str,
        prepare_round: (
            Callable[[ReviewLaneSession, ReviewRound], None] | None
        ) = None,
    ) -> ReviewGateDecision:
        """Continue the exact reviewer only after the executor changed HEAD."""

        state = self.read()
        awaiting = state.get("awaiting_resolution")
        if (
            state.get("status") != "awaiting-resolution"
            or not isinstance(awaiting, dict)
            or lane.axis not in awaiting
        ):
            raise ValueError("review lane is not awaiting a resolution")
        boundary = awaiting[lane.axis]
        if not isinstance(boundary, dict):
            raise ValueError("review resolution boundary is invalid")
        previous_head = str(
            boundary.get("reviewed_head_sha") or ""
        )
        previous = run.execution.request.context
        pointer = str(boundary.get("pointer") or "")
        result_path = (self.root / pointer).resolve()
        if (
            not pointer
            or self.root not in result_path.parents
            or not result_path.is_file()
            or result_path.is_symlink()
        ):
            raise ValueError("review resolution finding evidence is unavailable")
        previous_result = _result_from_payload(_read_json(result_path))
        material_ids = tuple(
            finding.finding_id
            for finding in previous_result.findings
            if finding.severity in MATERIAL_SEVERITIES
        )
        expected_resolution_operation_id = (
            run.execution.request.policy.operation_id
        )
        if state.get("fresh_reevaluation_used") is True:
            expected_resolution_operation_id = str(
                state.get("resolution_operation_id")
                or state.get("dispatch_operation_id")
                or ""
            )
        expected_review_identity_sha256 = str(
            state.get("resolution_transport_identity_sha256") or ""
        )
        if not expected_review_identity_sha256:
            review_operation_ids: set[str] = set()
            review_callbacks: list[dict[str, object]] = []
            for axis in sorted(awaiting):
                raw_boundary = awaiting[axis]
                if not isinstance(raw_boundary, dict):
                    raise ValueError(
                        "review resolution boundary identity is invalid"
                    )
                review_operation_ids.add(
                    str(raw_boundary.get("review_operation_id") or "")
                )
                review_callbacks.append(
                    {
                        "axis": axis,
                        "round_operation_id": str(
                            raw_boundary.get("round_operation_id") or ""
                        ),
                        "round_run_id": str(
                            raw_boundary.get("round_run_id") or ""
                        ),
                        "callback_id": str(
                            raw_boundary.get("callback_id") or ""
                        ),
                        "callback_sha256": str(
                            raw_boundary.get("callback_sha256") or ""
                        ),
                    }
                )
            if len(review_operation_ids) != 1:
                raise ValueError(
                    "review resolution operation identity is invalid"
                )
            try:
                expected_review_identity_sha256 = (
                    review_transport_identity_sha256(
                        next(iter(review_operation_ids)),
                        review_callbacks,
                    )
                )
            except ResolutionError as exc:
                raise ValueError(
                    "review resolution callback identity is invalid"
                ) from exc
        if (
            context.head_sha == previous_head
            or context.verification_profile
            != previous.verification_profile
            or context.verification_profile_sha256
            != previous.verification_profile_sha256
        ):
            raise ValueError(
                "review verification requires a new HEAD under the same profile"
            )
        if (
            resolution.operation_id
            != expected_resolution_operation_id
            or review_identity_sha256
            != expected_review_identity_sha256
            or resolution.axis != lane.axis
            or resolution.reviewed_head_sha != previous_head
            or resolution.resolved_head_sha != context.head_sha
            or resolution.previous_finding_ids != material_ids
        ):
            raise ValueError(
                "review resolution evidence does not cover the exact material findings"
            )
        resolution_pointer = self._persist_resolution(
            run.execution.request.policy.operation_id,
            resolution,
            verification_iteration=lane.verification_iteration,
        )
        captured: list[ReviewRound] = []

        def prepared(
            next_lane: ReviewLaneSession, next_round: ReviewRound
        ) -> None:
            captured.append(next_round)
            if prepare_round is not None:
                prepare_round(next_lane, next_round)

        continued = verify_review_lane(
            self.runtime,
            lane,
            prompt_pointer=verification_prompt_pointer,
            callback_pointer=callback_pointer,
            round_store=self.round_store,
            prepare_round=prepared,
        )
        if continued.state == "attention-required":
            self._replace(status="attention-required")
            return ReviewGateDecision("attention-required", continued)
        if not captured:
            raise ValueError(
                "review verification created no callback round"
            )
        remaining = dict(awaiting)
        remaining.pop(lane.axis, None)
        raw_lanes = state.get("lanes")
        if not isinstance(raw_lanes, list):
            raise ValueError("stored review lanes are unavailable")
        lanes = []
        for item in raw_lanes:
            if not isinstance(item, dict):
                raise ValueError("stored review lane is invalid")
            lanes.append(
                self._lane(continued)
                if item.get("axis") == lane.axis
                else item
            )
        self._replace(
            status=(
                "verifying"
                if not remaining
                else "awaiting-resolution"
            ),
            context=self._context(context),
            awaiting_resolution=remaining,
            resolution_evidence={
                **dict(state.get("resolution_evidence") or {}),
                f"{lane.axis}:{lane.verification_iteration}": resolution_pointer,
            },
            resolution_transport_identity_sha256=(
                expected_review_identity_sha256
            ),
            lanes=lanes,
        )
        return ReviewGateDecision("verify", continued, captured[0])

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
            raise ValueError(
                "fresh review verification budget cannot expand"
            )
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
            or boundary.next_context_sha256
            != review_context_sha256(context)
        ):
            raise ValueError(
                "fresh review boundary does not match the context transition"
            )
        role = (
            f"fresh:{boundary.kind}:"
            f"{boundary.next_context_sha256}"
        )
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
            axis_name = (
                "standards"
                if identity.axis
                == "standards-correctness-architecture-security"
                else identity.axis
            )
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

