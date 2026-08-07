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
    _atomic_json,
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

    def reconcile_superseded_review_cleanup(self) -> tuple[object, ...]:
        """Retry every durable superseded-review cleanup receipt."""

        root = self.root / "superseded-review-cleanup"
        if not root.is_dir() or root.is_symlink():
            return ()
        results: list[object] = []
        for path in sorted(root.glob("*.json")):
            if path.name.endswith("-result.json"):
                continue
            results.append(self.runtime.cleanup_superseded_review(path))
        return tuple(results)

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
