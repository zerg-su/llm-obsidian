"""Durable automatic review gate between task completion and final reap."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from ..contracts import (
    AttentionReason,
    CallbackEnvelope,
    OperationRecord,
    OwnedResources,
    to_dict,
)
from ..state_machine import TERMINAL
from .review import (
    ReviewContext,
    ReviewExecution,
    ReviewFinding,
    ReviewLaneSession,
    ReviewOperationRequest,
    ReviewRequest,
    ReviewResult,
    ReviewRound,
    ReviewRoundStore,
    ReviewRuntimePort,
    ReviewSessionRequest,
    accept_review_round,
    finish_review_lane,
    prepare_review_round,
    review_evidence_envelope,
    review_round_envelope,
    review_session_specs,
    start_review,
    verify_review_lane,
)
from review_contract import MATERIAL_SEVERITIES, VERIFY_BUDGETS, validate_review
from review_resolution import (
    ResolutionError,
    ReviewResolutionEvidence,
    review_transport_identity_sha256,
)


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
EFFORTS = {
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
}

from .review_gate_contracts import (
    ReviewGateAuthorization,
    ReviewGateDecision,
    ReviewGateRun,
    ReviewPreset,
    ReviewScopeBoundary,
    _atomic_json,
    _read_json,
    _result_from_payload,
    _result_payload,
    review_context_sha256,
)
from .review_gate_recovery import ReviewGateRecoveryMixin


class ReviewGateController(ReviewGateRecoveryMixin):
    """Own review policy, rounds, durable approval, and bounded recovery."""

    def __init__(
        self,
        root: Path,
        runtime: ReviewRuntimePort,
        round_store: ReviewRoundStore,
    ):
        self.root = root.expanduser().resolve()
        self.runtime = runtime
        self.round_store = round_store

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
            raise ValueError(
                "only an evidence-free bound review may resume"
            )
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
        return {
            "axis": lane.axis,
            "operation_id": lane.operation_id,
            "lane_id": lane.lane_id,
            "run_id": lane.run_id,
            "surface_id": lane.surface_id,
            "checkpoint": lane.checkpoint,
            "verification_iteration": lane.verification_iteration,
            "state": lane.state,
        }

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

    def _start_execution(
        self,
        *,
        request: ReviewOperationRequest,
        origin_surface: str,
        cwd: Path,
        product_root: Path,
        prompt_pointer: str,
        callback_root: str,
        callback_wake: str = "",
        prompt_pointers: Mapping[str, str] | None = None,
        prepare_lane: (
            Callable[[str, object, object, ReviewRound], None] | None
        ) = None,
    ) -> ReviewGateRun:
        captured: dict[str, ReviewRound] = {}

        def prepared(
            axis: str,
            session_request: object,
            result: object,
            round_: ReviewRound,
        ) -> None:
            captured[axis] = round_
            if prepare_lane is not None:
                prepare_lane(axis, session_request, result, round_)

        execution = start_review(
            request,
            self.runtime,
            origin_surface=origin_surface,
            cwd=cwd,
            product_root=product_root,
            prompt_pointer=prompt_pointer,
            callback_root=callback_root,
            callback_wake=callback_wake,
            round_store=self.round_store,
            prompt_pointers=prompt_pointers,
            prepare_lane=prepared,
        )
        for lane in execution.lanes:
            if lane.axis not in captured:
                captured[lane.axis] = prepare_review_round(
                    self.round_store, lane
                )
        self._replace(
            status="reviewing",
            active_review_operation_id=request.policy.operation_id,
            context=self._context(request.context),
            lanes=[self._lane(lane) for lane in execution.lanes],
        )
        return ReviewGateRun(execution, captured)

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
        review = ReviewRequest(
            operation_id=str(
                state.get("active_review_operation_id") or ""
            ),
            depth=str(policy.get("depth") or ""),
            cross_model=bool(policy.get("cross_model")),
            runtime=str(policy.get("runtime") or ""),
            model=str(policy.get("model") or ""),
            effort=str(policy.get("effort") or ""),
            max_verify_iterations=int(
                policy.get("max_verify_iterations", -1)
            ),
            purpose=str(policy.get("purpose") or "implementation"),
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
        for raw_lane in raw_lanes:
            if not isinstance(raw_lane, dict):
                raise ValueError("stored review lane is invalid")
            axis = str(raw_lane.get("axis") or "")
            operation_id = str(raw_lane.get("operation_id") or "")
            record = self.round_store.read(owner_id, operation_id)
            observed: object = record
            checkpoint = str(raw_lane.get("checkpoint") or "")
            if record.state not in TERMINAL:
                observed = self.runtime.status(owner_id, operation_id)
                runtime_checkpoint = str(
                    getattr(observed, "checkpoint", "") or ""
                )
                if runtime_checkpoint:
                    checkpoint = runtime_checkpoint
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
            )
            lanes.append(lane)
            routes[axis] = record.spec.route
            rounds[axis] = prepare_review_round(
                self.round_store, lane
            )
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

    def begin(
        self,
        *,
        dispatch_operation_id: str,
        request: ReviewOperationRequest,
        origin_surface: str,
        cwd: Path,
        product_root: Path,
        prompt_pointer: str,
        callback_root: str,
        callback_wake: str = "",
        prompt_pointers: Mapping[str, str] | None = None,
        prepare_lane: (
            Callable[[str, object, object, ReviewRound], None] | None
        ) = None,
    ) -> ReviewGateRun:
        product_root = product_root.expanduser().resolve()
        self._initialize(
            dispatch_operation_id=dispatch_operation_id,
            request=request,
            product_root=product_root,
        )
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

    @classmethod
    def skip(
        cls,
        root: Path,
        *,
        dispatch_operation_id: str,
        owner_id: str,
        preset: ReviewPreset,
        context: ReviewContext,
        product_root: Path,
    ) -> None:
        if preset.enabled:
            raise ValueError("only an explicit no-review preset may skip")
        root = root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        state = {
            "schema_version": 1,
            "dispatch_operation_id": dispatch_operation_id,
            "owner_id": owner_id,
            "status": "skipped",
            "policy": cls._policy(preset),
            "product_root": str(product_root.expanduser().resolve()),
            "active_review_operation_id": "",
            "context": cls._context(context),
            "fresh_reevaluation_used": False,
            "lanes": [],
            "round_results": {},
            "final_results": {},
            "resolution_evidence": {},
            "evidence": {},
        }
        path = root / "review-gate.json"
        if path.exists() and _read_json(path) != state:
            raise ValueError("no-review gate identity changed across replay")
        _atomic_json(path, state)

    def _persist_result(
        self, operation_id: str, result: ReviewResult, *, final: bool
    ) -> str:
        axis = (
            "standards"
            if result.axis
            == "standards-correctness-architecture-security"
            else result.axis
        )
        directory = self.root / operation_id
        name = (
            f"final-{axis}.json"
            if final
            else f"round-{axis}-{result.verification_iteration}.json"
        )
        path = directory / name
        payload = _result_payload(result)
        if path.exists() and _read_json(path) != payload:
            raise ValueError("review result changed across replay")
        _atomic_json(path, payload)
        return path.relative_to(self.root).as_posix()

    def _persist_resolution(
        self,
        operation_id: str,
        resolution: ReviewResolutionEvidence,
        *,
        verification_iteration: int,
    ) -> str:
        axis = (
            "standards"
            if resolution.axis
            == "standards-correctness-architecture-security"
            else resolution.axis
        )
        path = (
            self.root
            / operation_id
            / f"resolution-{axis}-{verification_iteration}.json"
        )
        payload = resolution.payload()
        if path.exists() and _read_json(path) != payload:
            raise ValueError("review resolution evidence changed across replay")
        _atomic_json(path, payload)
        return path.relative_to(self.root).as_posix()

    def _mark_attention(self, lanes: tuple[ReviewLaneSession, ...]) -> None:
        for lane in lanes:
            try:
                record = self.round_store.read(
                    lane.owner_id, lane.operation_id
                )
                if (
                    isinstance(record, OperationRecord)
                    and record.state not in TERMINAL
                    and record.state != "attention-required"
                ):
                    self.round_store.transition(
                        lane.owner_id,
                        lane.operation_id,
                        "attention-required",
                        reason=AttentionReason.RETRY_EXHAUSTED,
                    )
            except Exception:
                pass
        self._replace(status="attention-required")

    def _parent_callback_timed_out(
        self, lane: ReviewLaneSession
    ) -> bool:
        observed = self.runtime.status(
            lane.owner_id, lane.operation_id
        )
        parent = (
            observed
            if isinstance(observed, OperationRecord)
            else getattr(observed, "record", None)
        )
        return (
            isinstance(parent, OperationRecord)
            and parent.state == "attention-required"
            and parent.attention_reason
            == AttentionReason.CALLBACK_TIMEOUT
        )

    def _final_results(
        self, execution: ReviewExecution
    ) -> dict[str, ReviewResult] | None:
        state = self.read()
        pointers = state.get("final_results")
        if not isinstance(pointers, dict) or set(pointers) != set(
            execution.request.policy.axes
        ):
            return None
        return {
            axis: _result_from_payload(
                _read_json(self.root / str(pointers[axis]))
            )
            for axis in execution.request.policy.axes
        }

    def _approve(
        self, execution: ReviewExecution
    ) -> Path | None:
        results = self._final_results(execution)
        if results is None:
            return None
        envelope = review_evidence_envelope(execution, results)
        callback_path = self.root / ".review-callback.json"
        _atomic_json(callback_path, to_dict(envelope))
        context = execution.request.context
        _atomic_json(
            self.root / ".review-meta.json",
            {
                "schema_version": 1,
                "operation_id": envelope.operation_id,
                "review_id": execution.request.policy.operation_id,
                "run_id": envelope.run_id,
                "review_mode": execution.request.policy.depth,
                "review_purpose": context.purpose,
                "review_boundary_input_sha256": (
                    context.boundary_input_sha256
                ),
                "worktree": str(self.read()["product_root"]),
                "task_name": execution.request.policy.operation_id,
                "head_sha": context.head_sha,
                "verification_profile": {
                    "name": context.verification_profile,
                    "sha256": context.verification_profile_sha256,
                },
                "resolution_evidence": [
                    {
                        "pointer": str(pointer),
                        "sha256": hashlib.sha256(
                            (self.root / str(pointer)).read_bytes()
                        ).hexdigest(),
                    }
                    for pointer in dict(
                        self.read().get("resolution_evidence") or {}
                    ).values()
                ],
            },
        )
        digest = hashlib.sha256(callback_path.read_bytes()).hexdigest()
        self._replace(
            status="approved",
            context=self._context(context),
            evidence={
                "pointer": callback_path.relative_to(self.root).as_posix(),
                "sha256": digest,
                "operation_id": envelope.operation_id,
                "run_id": envelope.run_id,
            },
        )
        return callback_path

    def complete_round(
        self,
        run: ReviewGateRun,
        lane: ReviewLaneSession,
        round_: ReviewRound,
        result: ReviewResult,
    ) -> ReviewGateDecision:
        if self._parent_callback_timed_out(lane):
            self._replace(status="attention-required")
            return ReviewGateDecision("attention-required", lane, round_)
        envelope = review_round_envelope(round_, result)
        cleanup = accept_review_round(
            self.runtime,
            self.round_store,
            lane,
            round_,
            envelope,
        )
        pointer = self._persist_result(
            run.execution.request.policy.operation_id,
            result,
            final=False,
        )
        state = self.read()
        rounds = dict(state.get("round_results") or {})
        rounds[result.axis] = pointer
        self._replace(round_results=rounds)

        material = tuple(
            finding
            for finding in result.findings
            if finding.severity in MATERIAL_SEVERITIES
        )
        if (
            run.execution.request.policy.purpose == "release"
            and result.verdict != "approve"
        ):
            cleanup = finish_review_lane(self.runtime, lane)
            if cleanup is None or cleanup.state != "complete":
                self._mark_attention(run.execution.lanes)
                return ReviewGateDecision("attention-required", lane)
            stopped = dict(state.get("stopped_results") or {})
            stopped[result.axis] = pointer
            self._replace(
                status="stopped",
                stopped_results=stopped,
                lanes=[self._lane(item) for item in run.execution.lanes],
            )
            return ReviewGateDecision("stopped", lane, round_)
        if result.verdict == "blocked":
            self._mark_attention(run.execution.lanes)
            return ReviewGateDecision("attention-required", lane)
        if result.verdict == "changes-requested" and material:
            awaiting = dict(
                state.get("awaiting_resolution") or {}
            )
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
                "material_finding_ids": [
                    finding.finding_id for finding in material
                ],
            }
            self._replace(
                status="awaiting-resolution",
                awaiting_resolution=awaiting,
                resolution_transport_identity_sha256="",
                lanes=[
                    self._lane(item)
                    for item in run.execution.lanes
                ],
            )
            return ReviewGateDecision(
                "awaiting-resolution", lane, round_
            )
        if result.verdict == "changes-requested" and not result.findings:
            self._mark_attention(run.execution.lanes)
            return ReviewGateDecision("attention-required", lane)

        if result.verdict != "approve":
            cleanup = finish_review_lane(self.runtime, lane)
        if cleanup is None or cleanup.state != "complete":
            self._mark_attention(run.execution.lanes)
            return ReviewGateDecision("attention-required", lane)

        final = (
            result
            if result.verdict == "approve"
            else ReviewResult(
                result.axis,
                "approve",
                result.findings,
                result.verification_iteration,
            )
        )
        final_pointer = self._persist_result(
            run.execution.request.policy.operation_id,
            final,
            final=True,
        )
        state = self.read()
        finals = dict(state.get("final_results") or {})
        finals[final.axis] = final_pointer
        self._replace(final_results=finals)
        evidence = self._approve(run.execution)
        if evidence is None:
            status = str(self.read().get("status") or "")
            if status == "attention-required":
                return ReviewGateDecision("attention-required", lane)
            if status == "awaiting-resolution":
                return ReviewGateDecision(
                    "awaiting-resolution", lane, round_
                )
            self._replace(status="reviewing")
            return ReviewGateDecision("awaiting-axes", lane)
        return ReviewGateDecision(
            "approved", lane, evidence_path=evidence
        )


def authorize_task_finalization(
    root: Path,
    *,
    dispatch_operation_id: str,
    expected_head_sha: str,
    expected_profile: str,
    expected_profile_sha256: str,
    expected_summary_sha256: str = "",
) -> ReviewGateAuthorization:
    """Fail closed unless current approved evidence or explicit skip is durable."""

    root = root.expanduser().resolve()
    state = _read_json(root / "review-gate.json")
    if (
        state.get("schema_version") != 1
        or state.get("dispatch_operation_id") != dispatch_operation_id
    ):
        raise ValueError("review gate does not match the dispatch operation")
    context = state.get("context")
    if not isinstance(context, dict) or (
        context.get("head_sha") != expected_head_sha
        or context.get("verification_profile") != expected_profile
        or context.get("verification_profile_sha256")
        != expected_profile_sha256
        or (
            expected_summary_sha256
            and context.get("implementer_summary_sha256")
            != expected_summary_sha256
        )
    ):
        raise ValueError("review gate evidence is stale")
    policy = state.get("policy")
    if (
        state.get("status") == "skipped"
        and isinstance(policy, dict)
        and policy.get("enabled") is False
    ):
        return ReviewGateAuthorization(False, True, {})
    if state.get("status") != "approved":
        raise ValueError("task finalization requires an approved review gate")
    evidence = state.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("approved review gate has no evidence pointer")
    path = (root / str(evidence.get("pointer") or "")).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("review evidence pointer escapes the gate") from exc
    if (
        not path.is_file()
        or path.is_symlink()
        or hashlib.sha256(path.read_bytes()).hexdigest()
        != evidence.get("sha256")
    ):
        raise ValueError("approved review evidence is unavailable")
    raw = _read_json(path)
    envelope = CallbackEnvelope(
        callback_id=raw.get("callback_id", ""),
        operation_id=raw.get("operation_id", ""),
        run_id=raw.get("run_id", ""),
        kind=raw.get("kind", ""),
        payload=raw.get("payload", {}),
        payload_sha256=raw.get("payload_sha256", ""),
        schema_version=raw.get("schema_version", 0),
    )
    review = validate_review(
        dict(envelope.payload),
        expected_operation_id=envelope.operation_id,
        expected_run_id=envelope.run_id,
        expected_head_sha=expected_head_sha,
        expected_profile=expected_profile,
        expected_profile_sha256=expected_profile_sha256,
    )
    if review["verdict"] != "approve":
        raise ValueError("only approved review evidence unlocks finalization")
    return ReviewGateAuthorization(True, False, review)
