"""Resolution handoff and exact same-session review continuation."""

from __future__ import annotations

from typing import Callable

from ..contracts import AttentionReason
from ..state_machine import TERMINAL
from .review import (
    ReviewContext,
    ReviewLaneSession,
    ReviewResult,
    ReviewRound,
    review_round_envelope,
    verify_review_lane,
)
from .review_gate_contracts import (
    ReviewGateDecision,
    ReviewGateRun,
    _read_json,
    _result_from_payload,
)
from review_contract import MATERIAL_SEVERITIES
from review_resolution import (
    ResolutionError,
    ReviewResolutionEvidence,
    review_transport_identity_sha256,
)


class ReviewGateResolutionMixin:
    """Bind material findings to resolution evidence and exact continuation."""

    def _rearm_accepted_resolution_parent(
        self,
        lane: ReviewLaneSession,
        boundary: dict[str, object],
    ) -> bool:
        """Rearm only a timed-out parent whose exact round is already accepted."""

        parent = self.round_store.read(lane.owner_id, lane.operation_id)
        if parent.state != "attention-required":
            return True
        child_operation_id = str(boundary.get("round_operation_id") or "")
        if (
            parent.attention_reason != AttentionReason.CALLBACK_TIMEOUT
            or not child_operation_id
        ):
            return False
        child = self.round_store.read(lane.owner_id, child_operation_id)
        if (
            child.state != "complete"
            or child.run_id != str(boundary.get("round_run_id") or "")
            or child.accepted_callback_id
            != str(boundary.get("callback_id") or "")
            or child.accepted_callback_sha256
            != str(boundary.get("callback_sha256") or "")
        ):
            return False
        self.round_store.transition(
            lane.owner_id,
            lane.operation_id,
            "awaiting-callback",
        )
        return True

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
            or result.verification_iteration != lane.verification_iteration
            or payload.get("parent_session_operation_id") != lane.operation_id
        ):
            raise ValueError(
                "deferred review callback does not match its parent lane"
            )
        self.runtime.accept_callback(envelope)
        child = self.round_store.read(round_.owner_id, round_.operation_id)
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
            "reviewed_head_sha": run.execution.request.context.head_sha,
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
        return ReviewGateDecision("awaiting-resolution", lane, round_)

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
        previous_head = str(boundary.get("reviewed_head_sha") or "")
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
            or context.verification_profile != previous.verification_profile
            or context.verification_profile_sha256
            != previous.verification_profile_sha256
        ):
            raise ValueError(
                "review verification requires a new HEAD under the same profile"
            )
        if (
            resolution.operation_id != expected_resolution_operation_id
            or review_identity_sha256 != expected_review_identity_sha256
            or resolution.axis != lane.axis
            or resolution.reviewed_head_sha != previous_head
            or resolution.resolved_head_sha != context.head_sha
            or resolution.previous_finding_ids != material_ids
        ):
            raise ValueError(
                "review resolution evidence does not cover the exact material findings"
            )
        if not self._rearm_accepted_resolution_parent(lane, boundary):
            self._replace(status="attention-required")
            return ReviewGateDecision("attention-required", lane)
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
            raise ValueError("review verification created no callback round")
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
                "verifying" if not remaining else "awaiting-resolution"
            ),
            context=self._context(context),
            awaiting_resolution=remaining,
            resolution_evidence={
                **dict(state.get("resolution_evidence") or {}),
                f"{lane.axis}:{lane.verification_iteration}": (
                    resolution_pointer
                ),
            },
            resolution_transport_identity_sha256=(
                expected_review_identity_sha256
            ),
            lanes=lanes,
        )
        return ReviewGateDecision("verify", continued, captured[0])
