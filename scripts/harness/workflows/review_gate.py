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
from review_contract import MATERIAL_SEVERITIES, MODES, VERIFY_BUDGETS, validate_review
from review_resolution import (
    ResolutionError,
    ReviewResolutionEvidence,
    review_transport_identity_sha256,
)

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
from .review_gate_decisions import ReviewGateDecisionMixin
from .review_gate_recovery import ReviewGateRecoveryMixin
from .review_gate_state import ReviewGateStateMixin


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


class ReviewGateController(
    ReviewGateRecoveryMixin,
    ReviewGateDecisionMixin,
    ReviewGateStateMixin,
):
    """Stable facade for durable review policy and bounded recovery."""

    def __init__(
        self,
        root: Path,
        runtime: ReviewRuntimePort,
        round_store: ReviewRoundStore,
    ):
        self.root = root.expanduser().resolve()
        self.runtime = runtime
        self.round_store = round_store

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
    if set(raw) != {
        "schema_version",
        "callback_id",
        "operation_id",
        "run_id",
        "kind",
        "payload",
        "payload_sha256",
    }:
        raise ValueError("review callback envelope has invalid fields")
    envelope = CallbackEnvelope(
        callback_id=raw.get("callback_id", ""),
        operation_id=raw.get("operation_id", ""),
        run_id=raw.get("run_id", ""),
        kind=raw.get("kind", ""),
        payload=raw.get("payload", {}),
        payload_sha256=raw.get("payload_sha256", ""),
        schema_version=raw.get("schema_version", 0),
    )
    if (
        envelope.kind != "review"
        or evidence.get("operation_id") != envelope.operation_id
        or evidence.get("run_id") != envelope.run_id
    ):
        raise ValueError("approved review callback identity is invalid")
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
    policy = state.get("policy")
    mode = str(review.get("mode") or "")
    if (
        not isinstance(policy, dict)
        or policy.get("depth") != mode
        or mode not in MODES
    ):
        raise ValueError("approved review mode does not match its gate policy")
    raw_lanes = state.get("lanes")
    if not isinstance(raw_lanes, list) or not raw_lanes:
        raise ValueError("approved review lane identity is unavailable")
    expected_axes = tuple(
        str(lane.get("axis") or "")
        for lane in raw_lanes
        if isinstance(lane, dict)
    )
    pointers = state.get("final_results")
    if (
        len(expected_axes) != len(raw_lanes)
        or not isinstance(pointers, dict)
        or set(pointers) != set(expected_axes)
    ):
        raise ValueError("approved review final axes are incomplete")
    axes = {
        str(item.get("axis") or ""): item
        for item in review.get("axes", [])
        if isinstance(item, dict)
    }
    for axis in expected_axes:
        result_path = (root / str(pointers[axis])).resolve()
        try:
            result_path.relative_to(root)
        except ValueError as exc:
            raise ValueError("review result pointer escapes the gate") from exc
        if not result_path.is_file() or result_path.is_symlink():
            raise ValueError("approved review result is unavailable")
        result = _result_payload(_result_from_payload(_read_json(result_path)))
        aggregate_axis = dict(axes.get(axis) or {})
        aggregate_axis["findings"] = [
            {**finding, "axis": axis}
            for finding in aggregate_axis.get("findings", [])
            if isinstance(finding, dict)
        ]
        if result != aggregate_axis:
            raise ValueError("approved review result disagrees with its callback")
    return ReviewGateAuthorization(True, False, review)
