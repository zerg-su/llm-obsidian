"""Write-ahead receipts for exact same-session review continuations."""

from __future__ import annotations

import re
from dataclasses import replace

from ..contracts import EffectOutcome, OwnedResources
from .review import ReviewLaneSession, ReviewRound
from review_resolution import ReviewResolutionEvidence


EFFECT_ID = re.compile(r"continue-[0-9a-f]{32}\Z")


class ReviewGateContinuationMixin:
    """Persist and reconcile one provider effect per lane and iteration."""

    @staticmethod
    def _continuation_key(lane: ReviewLaneSession) -> str:
        return f"{lane.axis}:{lane.verification_iteration}"

    @staticmethod
    def _continuation_receipt(
        lane: ReviewLaneSession,
        round_: ReviewRound,
        resolution: ReviewResolutionEvidence,
        effect_id: str,
        state: str,
    ) -> dict[str, object]:
        if not EFFECT_ID.fullmatch(effect_id):
            raise ValueError("review continuation effect identity is invalid")
        if state not in {"prepared", "succeeded"}:
            raise ValueError("review continuation receipt state is invalid")
        route = lane.spec.route
        return {
            "schema_version": 1,
            "axis": lane.axis,
            "owner_id": lane.owner_id,
            "parent_operation_id": lane.operation_id,
            "parent_run_id": lane.run_id,
            "lane_id": lane.lane_id,
            "surface_id": lane.surface_id,
            "checkpoint": lane.checkpoint,
            "runtime": route.runtime,
            "model": route.model,
            "routing_sha256": route.routing_sha256,
            "reviewed_head_sha": resolution.reviewed_head_sha,
            "resolved_head_sha": resolution.resolved_head_sha,
            "verification_iteration": lane.verification_iteration + 1,
            "round_operation_id": round_.operation_id,
            "round_run_id": round_.run_id,
            "continuation_effect_id": effect_id,
            "state": state,
        }

    def _resource_free_unpublished_child(
        self, lane: ReviewLaneSession, round_: ReviewRound
    ) -> None:
        child = self.round_store.read(lane.owner_id, round_.operation_id)
        if (
            child.run_id != round_.run_id
            or child.lane_id != lane.lane_id
            or child.state != "awaiting-callback"
            or child.resources != OwnedResources()
            or child.accepted_callback_id
            or child.accepted_callback_sha256
        ):
            raise ValueError(
                "review continuation child is not resource-free and unpublished"
            )

    def _receipt_state(
        self,
        lane: ReviewLaneSession,
        round_: ReviewRound,
        resolution: ReviewResolutionEvidence,
        effect_id: str,
    ) -> str:
        state = self.read()
        raw = state.get("continuation_effects")
        receipts = dict(raw) if isinstance(raw, dict) else {}
        key = self._continuation_key(lane)
        current = receipts.get(key)
        if current is None:
            return ""
        if not isinstance(current, dict):
            raise ValueError("review continuation receipt is invalid")
        expected = self._continuation_receipt(
            lane, round_, resolution, effect_id, str(current.get("state") or "")
        )
        if current != expected:
            raise ValueError("review continuation receipt identity changed")
        return str(current["state"])

    def _persist_continuation_receipt(
        self,
        lane: ReviewLaneSession,
        round_: ReviewRound,
        resolution: ReviewResolutionEvidence,
        effect_id: str,
        state: str,
    ) -> None:
        receipt = self._continuation_receipt(
            lane, round_, resolution, effect_id, state
        )
        current = self.read()
        raw = current.get("continuation_effects")
        receipts = dict(raw) if isinstance(raw, dict) else {}
        key = self._continuation_key(lane)
        existing = receipts.get(key)
        if existing is not None:
            if not isinstance(existing, dict):
                raise ValueError("review continuation receipt is invalid")
            comparable = {**receipt, "state": existing.get("state")}
            if existing != comparable:
                raise ValueError("review continuation receipt identity changed")
            if existing.get("state") == "succeeded":
                return
        receipts[key] = receipt
        self._replace(continuation_effects=receipts)

    def _effect_succeeded(
        self, lane: ReviewLaneSession, effect_id: str
    ) -> bool:
        parent = self.round_store.read(lane.owner_id, lane.operation_id)
        return bool(
            parent.spec == lane.spec
            and parent.lane_id == lane.lane_id
            and parent.run_id == lane.run_id
            and parent.resources.surface_id == lane.surface_id
            and parent.effect_id == effect_id
            and parent.effect_outcome == EffectOutcome.SUCCEEDED
        )

    def _reconcile_continuation_receipt(
        self,
        lane: ReviewLaneSession,
        round_: ReviewRound,
        resolution: ReviewResolutionEvidence,
        effect_id: str,
    ) -> ReviewLaneSession | None:
        state = self._receipt_state(
            lane, round_, resolution, effect_id
        )
        if state == "succeeded" or (
            state == "prepared" and self._effect_succeeded(lane, effect_id)
        ):
            self._resource_free_unpublished_child(lane, round_)
            self._persist_continuation_receipt(
                lane, round_, resolution, effect_id, "succeeded"
            )
            parent = self.round_store.read(lane.owner_id, lane.operation_id)
            return replace(
                lane,
                verification_iteration=lane.verification_iteration + 1,
                state=parent.state,
            )
        return None

    def backfill_succeeded_continuation_receipt(
        self,
        lane: ReviewLaneSession,
        round_: ReviewRound,
        resolution: ReviewResolutionEvidence,
        effect_id: str = "",
    ) -> None:
        """Bind a pre-upgrade successful effect without replaying it."""

        prior_lane = replace(
            lane,
            verification_iteration=lane.verification_iteration - 1,
        )
        if prior_lane.verification_iteration < 0:
            raise ValueError("review continuation iteration is invalid")
        self._resource_free_unpublished_child(prior_lane, round_)
        parent = self.round_store.read(
            prior_lane.owner_id, prior_lane.operation_id
        )
        durable_effect_id = parent.effect_id
        if effect_id and effect_id != durable_effect_id:
            raise ValueError("review continuation effect identity changed")
        effect_id = durable_effect_id
        if not self._effect_succeeded(prior_lane, effect_id):
            raise ValueError("review continuation effect is not durable")
        self._persist_continuation_receipt(
            prior_lane, round_, resolution, effect_id, "succeeded"
        )
