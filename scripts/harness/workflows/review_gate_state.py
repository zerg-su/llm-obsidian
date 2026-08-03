"""Persisted review-gate state and exact runtime rehydration."""

from __future__ import annotations

import contextlib
import fcntl
import re
from pathlib import Path

from ..contracts import OperationRecord
from ..runtime_session_contracts import (
    RuntimeCheckpointEvidenceMissing,
    RuntimeSessionError,
)
from ..state_machine import TERMINAL
from ..store import StoreError
from .review import (
    ReviewContext,
    ReviewExecution,
    ReviewLaneSession,
    ReviewOperationRequest,
    ReviewRequest,
    ReviewRound,
    ReviewRoundStore,
    prepare_review_round,
    review_round_spec,
)
from .review_gate_contracts import (
    ReviewGateRun,
    ReviewPreset,
    _atomic_json,
    _read_json,
    review_context_sha256,
)
from review_contract import (
    review_axis_provider,
    review_axis_responsibility,
    review_provider_runtime,
)


def _round_without_checkpoint(
    round_store: ReviewRoundStore,
    *,
    review: ReviewRequest,
    raw_lane: dict[str, object],
    record: OperationRecord,
    axis: str,
    owner_id: str,
    unavailable: RuntimeSessionError,
    allow_pending: bool,
) -> ReviewRound:
    """Bind an exact existing child while its checkpoint is unavailable."""

    lane = ReviewLaneSession(
        axis=axis,
        owner_id=owner_id,
        operation_id=record.spec.operation_id,
        lane_id=record.lane_id,
        run_id=record.run_id,
        surface_id=record.resources.surface_id,
        checkpoint="",
        spec=record.spec,
        verification_iteration=int(
            raw_lane.get("verification_iteration", -1)
        ),
        max_verify_iterations=review.max_verify_iterations,
        state=record.state,
        checkpoint_sha256="",
    )
    spec = review_round_spec(lane)
    try:
        accepted_record = round_store.read(spec.owner_id, spec.operation_id)
    except StoreError as exc:
        raise unavailable from exc
    accepted_round = ReviewRound(
        parent_operation_id=lane.operation_id,
        operation_id=spec.operation_id,
        owner_id=spec.owner_id,
        lane_id=accepted_record.lane_id,
        run_id=accepted_record.run_id,
        axis=lane.axis,
        verification_iteration=lane.verification_iteration,
        spec=spec,
    )
    valid_receipt = (
        accepted_record.spec == spec
        and accepted_record.lane_id == lane.lane_id
        and accepted_record.state
        in {"verifying", "finalizing", "exiting", "complete"}
        and accepted_record.accepted_callback_kind == "review"
        and bool(accepted_record.accepted_callback_id)
        and re.fullmatch(
            r"[0-9a-f]{64}", accepted_record.accepted_callback_sha256
        )
        is not None
    )
    valid_pending = (
        allow_pending
        and accepted_record.state == "awaiting-callback"
        and not accepted_record.accepted_callback_id
        and not accepted_record.accepted_callback_kind
        and not accepted_record.accepted_callback_sha256
    )
    if not valid_receipt and not valid_pending:
        raise unavailable
    return accepted_round


def _rehydrate_checkpoint(
    runtime: object,
    round_store: ReviewRoundStore,
    *,
    review: ReviewRequest,
    raw_lane: dict[str, object],
    record: OperationRecord,
    axis: str,
    owner_id: str,
    runtime_checkpoint: str,
    allow_pending: bool,
) -> tuple[str, str, ReviewRound | None, bool]:
    try:
        recovered = runtime.hydrate_durable_checkpoint(
            owner_id, record.spec.operation_id, record.lane_id
        )
    except RuntimeCheckpointEvidenceMissing as exc:
        accepted_round = _round_without_checkpoint(
            round_store,
            review=review,
            raw_lane=raw_lane,
            record=record,
            axis=axis,
            owner_id=owner_id,
            unavailable=exc,
            allow_pending=allow_pending,
        )
        return "", "", accepted_round, False
    recovered_record = getattr(recovered, "record", None)
    checkpoint = str(getattr(recovered, "checkpoint", "") or "")
    checkpoint_sha256 = str(
        getattr(recovered, "checkpoint_sha256", "") or ""
    )
    if (
        not isinstance(recovered_record, OperationRecord)
        or recovered_record != record
        or not checkpoint
        or re.fullmatch(r"[0-9a-f]{64}", checkpoint_sha256) is None
        or (runtime_checkpoint and runtime_checkpoint != checkpoint)
    ):
        raise ValueError("durable review checkpoint identity changed")
    return checkpoint, checkpoint_sha256, None, True


def _observed_lane_checkpoint(
    observed: object,
    record: OperationRecord,
    raw_lane: dict[str, object],
) -> tuple[str, bool]:
    """Validate one read-only status and classify live pre-checkpoint work."""

    observed_record = getattr(observed, "record", None)
    if (
        not isinstance(observed_record, OperationRecord)
        or observed_record.spec != record.spec
        or observed_record.lane_id
        != str(raw_lane.get("lane_id") or "")
        or observed_record.run_id != str(raw_lane.get("run_id") or "")
    ):
        raise ValueError("stored review lane identity changed")
    checkpoint = str(getattr(observed, "checkpoint", "") or "")
    live_without_checkpoint = (
        str(getattr(observed, "action", "") or "")
        in {"observed", "already-started"}
        and str(getattr(observed, "process_status", "") or "") == "alive"
        and str(getattr(observed, "surface_status", "") or "") == "alive"
        and not checkpoint
    )
    return checkpoint, live_without_checkpoint


class ReviewGateStateMixin:
    """Own the persisted gate document and reconstruct exact active lanes."""

    @property
    def state_path(self) -> Path:
        return self.root / "review-gate.json"

    @contextlib.contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        with (self.root / ".lock").open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read(self) -> dict[str, object]:
        with self._locked():
            state = _read_json(self.state_path)
        if state.get("schema_version") != 1:
            raise ValueError("review gate schema is invalid")
        return state

    def _replace(self, **updates: object) -> dict[str, object]:
        with self._locked():
            state = _read_json(self.state_path)
            state.update(updates)
            _atomic_json(self.state_path, state)
            return state

    def mark_pending_attention(self) -> None:
        state = self.read()
        if state.get("status") != "pending" or state.get("lanes") != []:
            raise ValueError(
                "only an unbound pending review may require attention"
            )
        self._replace(status="attention-required")

    def resume_unbound_attention(self) -> None:
        state = self.read()
        if (
            state.get("status") != "attention-required"
            or state.get("lanes") != []
            or state.get("evidence") not in ({}, None)
            or state.get("round_results") not in ({}, None)
            or state.get("final_results") not in ({}, None)
        ):
            raise ValueError(
                "only an evidence-free pre-launch review may resume as pending"
            )
        self._replace(status="pending")

    def resume_bound_attention(self) -> None:
        """Rearm an exact review after its runtime attention was resolved."""

        state = self.read()
        lanes = state.get("lanes")
        owner_id = str(state.get("owner_id") or "")
        if (
            state.get("status") != "attention-required"
            or not isinstance(lanes, list)
            or not lanes
            or not owner_id
            or state.get("evidence") not in ({}, None)
        ):
            raise ValueError("only an evidence-free bound review may resume")
        for lane in lanes:
            if not isinstance(lane, dict):
                raise ValueError("bound review lane is invalid")
            operation_id = str(lane.get("operation_id") or "")
            if not operation_id:
                raise ValueError("bound review lane has no operation")
            record = self.round_store.read(owner_id, operation_id)
            if record.state not in {"awaiting-callback", "verifying"}:
                raise ValueError(
                    "bound review runtime attention is not resolved"
                )
        status = (
            "verifying"
            if any(
                int(lane.get("verification_iteration") or 0) > 0
                for lane in lanes
            )
            else "reviewing"
        )
        self._replace(status=status)

    @staticmethod
    def _policy(
        preset: ReviewPreset,
        *,
        purpose: str = "implementation",
        max_verify_iterations: int | None = None,
    ) -> dict[str, object]:
        return {
            "enabled": preset.enabled,
            "depth": preset.depth,
            "cross_model": preset.cross_model,
            "runtime": preset.runtime,
            "model": preset.model,
            "effort": preset.effort,
            "max_verify_iterations": (
                preset.max_verify_iterations
                if max_verify_iterations is None
                else max_verify_iterations
            ),
            "purpose": purpose,
        }

    @staticmethod
    def _context(context: ReviewContext) -> dict[str, object]:
        value: dict[str, object] = {
            "manifest": context.manifest,
            "sha256": review_context_sha256(context),
            "head_sha": context.head_sha,
            "verification_profile": context.verification_profile,
            "verification_profile_sha256": (
                context.verification_profile_sha256
            ),
        }
        if context.implementer_summary_sha256:
            value["implementer_summary_sha256"] = (
                context.implementer_summary_sha256
            )
        if context.boundary_input_sha256:
            value["purpose"] = context.purpose
            value["boundary_input_sha256"] = context.boundary_input_sha256
        return value

    @staticmethod
    def _lane(lane: ReviewLaneSession) -> dict[str, object]:
        value = {
            "axis": lane.axis,
            "operation_id": lane.operation_id,
            "lane_id": lane.lane_id,
            "run_id": lane.run_id,
            "surface_id": lane.surface_id,
            "checkpoint": lane.checkpoint,
            "verification_iteration": lane.verification_iteration,
            "state": lane.state,
        }
        if lane.checkpoint_sha256:
            value["checkpoint_sha256"] = lane.checkpoint_sha256
        return value

    def _initialize(
        self,
        *,
        dispatch_operation_id: str,
        request: ReviewOperationRequest,
        product_root: Path,
    ) -> None:
        preset = ReviewPreset(
            depth=request.policy.depth,
            cross_model=request.policy.cross_model,
            runtime=request.policy.runtime,
            model=request.policy.model,
            effort=request.policy.effort,
        )
        expected_budget = (
            0
            if request.policy.purpose == "release"
            else min(
                preset.max_verify_iterations,
                1 if request.policy.purpose == "intent" else 2,
            )
        )
        if request.policy.max_verify_iterations != expected_budget:
            raise ValueError(
                "automatic review must use the deterministic preset budget"
            )
        initial = {
            "schema_version": 1,
            "dispatch_operation_id": dispatch_operation_id,
            "owner_id": request.owner_id,
            "status": "pending",
            "policy": self._policy(
                preset,
                purpose=request.policy.purpose,
                max_verify_iterations=request.policy.max_verify_iterations,
            ),
            "product_root": str(product_root),
            "active_review_operation_id": request.policy.operation_id,
            "context": self._context(request.context),
            "fresh_reevaluation_used": False,
            "lanes": [],
            "round_results": {},
            "final_results": {},
            "resolution_evidence": {},
            "continuation_effects": {},
            "evidence": {},
        }
        with self._locked():
            if self.state_path.exists():
                current = _read_json(self.state_path)
                for field in (
                    "schema_version",
                    "dispatch_operation_id",
                    "owner_id",
                    "policy",
                    "product_root",
                ):
                    if current.get(field) != initial[field]:
                        raise ValueError(
                            "review gate identity changed across replay"
                        )
                return
            _atomic_json(self.state_path, initial)

    def rehydrate(self) -> ReviewGateRun:
        """Rebuild the exact active lanes after the task process restarts."""

        state = self.read()
        policy = state.get("policy")
        context = state.get("context")
        raw_lanes = state.get("lanes")
        if (
            not isinstance(policy, dict)
            or policy.get("enabled") is not True
            or not isinstance(context, dict)
            or not isinstance(raw_lanes, list)
            or not raw_lanes
        ):
            raise ValueError("active review gate cannot be rehydrated")
        stored_axes = tuple(
            str(item.get("axis") or "")
            for item in raw_lanes
            if isinstance(item, dict)
        )
        selected_provider = ""
        if stored_axes and (
            str(policy.get("depth") or "") == "simple"
            or (
                str(policy.get("depth") or "") == "deep"
                and review_axis_responsibility(stored_axes[0]) != "holistic"
            )
        ):
            selected_provider = review_axis_provider(stored_axes[0])
        review = ReviewRequest(
            operation_id=str(state.get("active_review_operation_id") or ""),
            depth=str(policy.get("depth") or ""),
            cross_model=bool(policy.get("cross_model")),
            runtime=str(policy.get("runtime") or ""),
            model=str(policy.get("model") or ""),
            effort=str(policy.get("effort") or ""),
            max_verify_iterations=int(
                policy.get("max_verify_iterations", -1)
            ),
            purpose=str(policy.get("purpose") or "implementation"),
            selected_provider=selected_provider,
        )
        review_context = ReviewContext(
            manifest=str(context.get("manifest") or ""),
            head_sha=str(context.get("head_sha") or ""),
            verification_profile=str(
                context.get("verification_profile") or ""
            ),
            verification_profile_sha256=str(
                context.get("verification_profile_sha256") or ""
            ),
            implementer_summary_sha256=str(
                context.get("implementer_summary_sha256") or ""
            ),
            purpose=str(context.get("purpose") or "implementation"),
            boundary_input_sha256=str(
                context.get("boundary_input_sha256") or ""
            ),
        )
        lanes: list[ReviewLaneSession] = []
        routes: dict[str, object] = {}
        rounds: dict[str, ReviewRound] = {}
        owner_id = str(state.get("owner_id") or "")
        hydrated = False
        for raw_lane in raw_lanes:
            if not isinstance(raw_lane, dict):
                raise ValueError("stored review lane is invalid")
            axis = str(raw_lane.get("axis") or "")
            operation_id = str(raw_lane.get("operation_id") or "")
            record = self.round_store.read(owner_id, operation_id)
            observed: object = record
            checkpoint = str(raw_lane.get("checkpoint") or "")
            checkpoint_sha256 = str(
                raw_lane.get("checkpoint_sha256") or ""
            )
            if record.state not in TERMINAL:
                observed = self.runtime.status(owner_id, operation_id)
                (
                    runtime_checkpoint,
                    live_without_checkpoint,
                ) = _observed_lane_checkpoint(
                    observed, record, raw_lane
                )
                expected_runtime = review_provider_runtime(
                    review_axis_provider(axis)
                )
                if record.spec.route.runtime != expected_runtime:
                    raise ValueError(
                        "stored review provider route changed"
                    )
                if checkpoint:
                    if (
                        runtime_checkpoint
                        and runtime_checkpoint != checkpoint
                    ):
                        raise ValueError(
                            "stored review checkpoint identity changed"
                        )
                else:
                    (
                        recovered_checkpoint,
                        recovered_sha256,
                        accepted_round,
                        did_hydrate,
                    ) = _rehydrate_checkpoint(
                        self.runtime,
                        self.round_store,
                        review=review,
                        raw_lane=raw_lane,
                        record=record,
                        axis=axis,
                        owner_id=owner_id,
                        runtime_checkpoint=runtime_checkpoint,
                        allow_pending=live_without_checkpoint,
                    )
                    if accepted_round is not None:
                        rounds[axis] = accepted_round
                    checkpoint = recovered_checkpoint
                    checkpoint_sha256 = recovered_sha256
                    if did_hydrate:
                        hydrated = True
            observed_record = (
                observed
                if isinstance(observed, OperationRecord)
                else getattr(observed, "record", None)
            )
            if (
                not isinstance(observed_record, OperationRecord)
                or observed_record.spec != record.spec
                or observed_record.lane_id
                != str(raw_lane.get("lane_id") or "")
                or observed_record.run_id
                != str(raw_lane.get("run_id") or "")
            ):
                raise ValueError("stored review lane identity changed")
            lane = ReviewLaneSession(
                axis=axis,
                owner_id=owner_id,
                operation_id=operation_id,
                lane_id=record.lane_id,
                run_id=record.run_id,
                surface_id=record.resources.surface_id,
                checkpoint=checkpoint,
                spec=record.spec,
                verification_iteration=int(
                    raw_lane.get("verification_iteration", -1)
                ),
                max_verify_iterations=review.max_verify_iterations,
                state=record.state,
                checkpoint_sha256=checkpoint_sha256,
            )
            lanes.append(lane)
            routes[axis] = record.spec.route
            if axis not in rounds:
                rounds[axis] = prepare_review_round(
                    self.round_store, lane
                )
        if hydrated:
            persisted_lanes = [self._lane(lane) for lane in lanes]
            with self._locked():
                current = _read_json(self.state_path)
                if current.get("lanes") != raw_lanes:
                    raise ValueError(
                        "stored review lanes changed during checkpoint hydration"
                    )
                current["lanes"] = persisted_lanes
                _atomic_json(self.state_path, current)
        request = ReviewOperationRequest(
            review,
            owner_id,
            lanes[0].spec.route,
            review_context,
            axis_routes=routes,
            lane_ids={lane.axis: lane.lane_id for lane in lanes},
        )
        execution = ReviewExecution(request, tuple(lanes))
        return ReviewGateRun(execution, rounds)
