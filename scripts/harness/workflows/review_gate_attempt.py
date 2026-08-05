"""Exact-HEAD ReviewAttempt compilation and one-round gate decisions."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping

from ..review_attempt import (
    EXACT_HEAD_REVIEW_PROTOCOL,
    ReviewAttempt,
    ReviewAttemptError,
    ReviewAttemptIdentity,
    ReviewAttemptLaneIdentity,
    ReviewAttemptLaneResult,
    ReviewAttemptPolicy,
    ReviewAttemptTerminal,
    ReviewAttemptTerminalResult,
)
from .review import (
    ReviewLaneSession,
    ReviewOperationRequest,
    ReviewResult,
    ReviewRound,
    accept_review_round,
    finish_review_lane,
    review_round_envelope,
    review_session_specs,
)
from .review_gate_contracts import (
    ReviewGateDecision,
    ReviewGateRun,
    ReviewPreset,
    _atomic_json,
    _read_json,
    _result_from_payload,
)
from .review_results import namespace_review_result
from review_contract import MATERIAL_SEVERITIES


def compile_review_attempt_identity(
    *,
    request: ReviewOperationRequest,
    finalization_lineage_id: str,
    cycle: int,
    plan_sha256: str,
    outcome_sha256: str,
) -> ReviewAttemptIdentity:
    """Freeze policy, exact HEAD, routes, and deterministic lanes pre-effect."""

    policy = request.policy
    attempt_policy = ReviewAttemptPolicy(
        depth=policy.depth,
        cross_model=policy.cross_model,
        runtime=policy.runtime,
        model=policy.model,
        effort=policy.effort,
        max_verify_iterations=policy.max_verify_iterations,
        purpose=policy.purpose,
        selected_provider=policy.selected_provider,
    )
    lanes = tuple(
        ReviewAttemptLaneIdentity(
            axis=identity.axis,
            owner_id=identity.spec.owner_id,
            operation_id=identity.spec.operation_id,
            lane_id=identity.lane_id,
            run_id=identity.run_id,
            runtime=identity.spec.route.runtime,
            model=identity.spec.route.model,
            effort=identity.spec.route.effort,
            profile=identity.spec.route.profile,
            routing_sha256=identity.spec.route.routing_sha256,
        )
        for identity in review_session_specs(request)
    )
    return ReviewAttemptIdentity(
        attempt_id=policy.operation_id,
        finalization_lineage_id=finalization_lineage_id,
        cycle=cycle,
        plan_sha256=plan_sha256,
        outcome_sha256=outcome_sha256,
        exact_head_sha=request.context.head_sha,
        policy=attempt_policy,
        lanes=lanes,
    )


class ReviewGateAttemptMixin:
    """Own the new one-HEAD path without invoking legacy continuation code."""

    def _attempt(self) -> ReviewAttempt:
        raw = self.read().get("attempt")
        if not isinstance(raw, Mapping):
            raise ReviewAttemptError(
                "legacy-cross-head-resume-disabled"
            )
        return ReviewAttempt.from_mapping(raw)

    @staticmethod
    def _attempt_lane(
        identity: ReviewAttemptIdentity, lane: ReviewLaneSession
    ) -> ReviewAttemptLaneIdentity:
        matches = tuple(
            item for item in identity.lanes if item.axis == lane.axis
        )
        if len(matches) != 1:
            raise ReviewAttemptError("review attempt lane is not frozen")
        expected = matches[0]
        route = lane.spec.route
        observed = ReviewAttemptLaneIdentity(
            axis=lane.axis,
            owner_id=lane.owner_id,
            operation_id=lane.operation_id,
            lane_id=lane.lane_id,
            run_id=lane.run_id,
            runtime=route.runtime,
            model=route.model,
            effort=route.effort,
            profile=route.profile,
            routing_sha256=route.routing_sha256,
            verification_iteration=lane.verification_iteration,
        )
        if observed != expected:
            raise ReviewAttemptError("review attempt lane identity changed")
        return expected

    def _initialize_attempt(
        self,
        *,
        dispatch_operation_id: str,
        request: ReviewOperationRequest,
        product_root: Path,
        identity: ReviewAttemptIdentity,
    ) -> ReviewAttempt:
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
            raise ReviewAttemptError(
                "automatic review must use the deterministic preset budget"
            )
        attempt = ReviewAttempt.pending(identity)
        initial = {
            "schema_version": 1,
            "execution_protocol": EXACT_HEAD_REVIEW_PROTOCOL,
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
            "lanes": [],
            "round_results": {},
            "final_results": {},
            "evidence": {},
            "attempt": attempt.payload(),
        }
        with self._locked():
            if self.state_path.exists():
                current = _read_json(self.state_path)
                raw_attempt = current.get("attempt")
                if not isinstance(raw_attempt, Mapping):
                    raise ReviewAttemptError(
                        "legacy-cross-head-resume-disabled"
                    )
                existing = ReviewAttempt.from_mapping(raw_attempt)
                if existing.status == "terminal" and identity.cycle == (
                    existing.identity.cycle + 1
                ):
                    if (
                        existing.terminal is None
                        or existing.terminal.result
                        != ReviewAttemptTerminalResult.CHANGES_REQUESTED
                        or existing.identity.finalization_lineage_id
                        != identity.finalization_lineage_id
                        or existing.identity.plan_sha256 != identity.plan_sha256
                        or existing.identity.outcome_sha256 != identity.outcome_sha256
                        or existing.identity.exact_head_sha == identity.exact_head_sha
                    ):
                        raise ReviewAttemptError(
                            "next review attempt lacks a changed-HEAD resolution boundary"
                        )
                    archive = self.root / "attempts" / (
                        f"cycle-{existing.identity.cycle}.json"
                    )
                    archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    archive.parent.chmod(0o700)
                    encoded = (
                        json.dumps(
                            current,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode()
                    try:
                        descriptor = os.open(
                            archive,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                        )
                    except FileExistsError:
                        if archive.read_bytes() != encoded:
                            raise ReviewAttemptError(
                                "archived review attempt changed"
                            ) from None
                    else:
                        with os.fdopen(descriptor, "wb") as handle:
                            handle.write(encoded)
                            handle.flush()
                            os.fsync(handle.fileno())
                    _atomic_json(self.state_path, initial)
                    return attempt
                existing.assert_identity(identity)
                if existing.status != "pending":
                    raise ReviewAttemptError(
                        "review attempt cannot start or rearm twice"
                    )
                if (
                    current.get("status") != "pending"
                    or current.get("lanes") != []
                    or current.get("round_results") != {}
                    or current.get("final_results") != {}
                    or current.get("evidence") != {}
                ):
                    raise ReviewAttemptError(
                        "pending review attempt gate contains execution state"
                    )
                for field in (
                    "schema_version",
                    "execution_protocol",
                    "dispatch_operation_id",
                    "owner_id",
                    "policy",
                    "product_root",
                    "active_review_operation_id",
                    "context",
                ):
                    if current.get(field) != initial[field]:
                        raise ReviewAttemptError(
                            "review attempt gate identity changed"
                        )
                return existing
            _atomic_json(self.state_path, initial)
        return attempt

    def begin_attempt(
        self,
        *,
        dispatch_operation_id: str,
        finalization_lineage_id: str,
        cycle: int,
        plan_sha256: str,
        outcome_sha256: str,
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
        """Start exactly one immutable attempt after all identity checks."""

        product_root = product_root.expanduser().resolve()
        identity = compile_review_attempt_identity(
            request=request,
            finalization_lineage_id=finalization_lineage_id,
            cycle=cycle,
            plan_sha256=plan_sha256,
            outcome_sha256=outcome_sha256,
        )
        attempt = self._initialize_attempt(
            dispatch_operation_id=dispatch_operation_id,
            request=request,
            product_root=product_root,
            identity=identity,
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
            attempt=attempt,
        )

    def rehydrate_attempt(self) -> ReviewGateRun:
        """Rehydrate iteration zero without resurrecting a reviewer effect."""

        attempt = self._attempt()
        state = self.read()
        raw_lanes = state.get("lanes")
        if (
            attempt.status not in {"awaiting-callback", "terminal"}
            or not isinstance(raw_lanes, list)
            or not raw_lanes
        ):
            raise ReviewAttemptError("review attempt cannot be rehydrated")
        checkpointless_axes: set[str] = set()
        for raw_lane in raw_lanes:
            if (
                not isinstance(raw_lane, dict)
                or raw_lane.get("verification_iteration") != 0
            ):
                raise ReviewAttemptError(
                    "review attempt checkpoint cannot be resurrected"
                )
            if (
                attempt.status == "awaiting-callback"
                and not str(raw_lane.get("checkpoint") or "")
            ):
                checkpointless_axes.add(str(raw_lane.get("axis") or ""))
        run = self.rehydrate()
        if run.execution.request.context.head_sha != attempt.identity.exact_head_sha:
            raise ReviewAttemptError("review attempt cannot bind a changed HEAD")
        for lane in run.execution.lanes:
            self._attempt_lane(attempt.identity, lane)
            if lane.axis not in checkpointless_axes:
                continue
            round_ = run.rounds[lane.axis]
            accepted = self.round_store.read(
                round_.owner_id, round_.operation_id
            )
            if (
                accepted.spec != round_.spec
                or accepted.lane_id != lane.lane_id
                or accepted.run_id != round_.run_id
                or accepted.state
                not in {"verifying", "finalizing", "exiting", "complete"}
                or accepted.accepted_callback_kind != "review"
                or not accepted.accepted_callback_id
                or re.fullmatch(
                    r"[0-9a-f]{64}", accepted.accepted_callback_sha256
                )
                is None
            ):
                raise ReviewAttemptError(
                    "review attempt checkpoint cannot be resurrected"
                )
        return run

    @staticmethod
    def _lane_terminal_verdict(result: ReviewResult) -> str:
        material = any(
            finding.severity in MATERIAL_SEVERITIES
            for finding in result.findings
        )
        if result.verdict == "changes-requested" and material:
            return "changes-requested"
        if result.verdict == "blocked":
            return "blocked"
        if result.verdict == "approve" or (
            result.verdict == "changes-requested" and result.findings
        ):
            return "approve"
        raise ReviewAttemptError(
            "review attempt result cannot produce a terminal verdict"
        )

    def _attempt_lane_results(
        self,
        run: ReviewGateRun,
        pointers: Mapping[str, object],
    ) -> tuple[ReviewAttemptLaneResult, ...]:
        results: list[ReviewAttemptLaneResult] = []
        for axis in run.execution.request.policy.axes:
            pointer = pointers.get(axis)
            if not isinstance(pointer, str):
                raise ReviewAttemptError(
                    "review attempt terminal lanes are incomplete"
                )
            path = (self.root / pointer).resolve()
            if self.root not in path.parents or not path.is_file() or path.is_symlink():
                raise ReviewAttemptError(
                    "review attempt terminal result is unavailable"
                )
            result = _result_from_payload(_read_json(path))
            if result.axis != axis or result.verification_iteration != 0:
                raise ReviewAttemptError(
                    "review attempt terminal result identity changed"
                )
            results.append(
                ReviewAttemptLaneResult(
                    axis=axis,
                    verdict=self._lane_terminal_verdict(result),
                    result_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    finding_ids=tuple(
                        finding.finding_id for finding in result.findings
                    ),
                )
            )
        return tuple(results)

    @staticmethod
    def _terminal_result(
        lanes: tuple[ReviewAttemptLaneResult, ...]
    ) -> ReviewAttemptTerminalResult:
        verdicts = {lane.verdict for lane in lanes}
        if "blocked" in verdicts:
            return ReviewAttemptTerminalResult.BLOCKED
        if "changes-requested" in verdicts:
            return ReviewAttemptTerminalResult.CHANGES_REQUESTED
        return ReviewAttemptTerminalResult.APPROVED

    def _attempt_resolution_boundaries(
        self,
        run: ReviewGateRun,
        pointers: Mapping[str, object],
    ) -> dict[str, object]:
        """Bind exact-attempt findings to their already accepted callbacks."""

        boundaries: dict[str, object] = {}
        material_count = 0
        for lane in run.execution.lanes:
            pointer = pointers.get(lane.axis)
            if not isinstance(pointer, str) or not pointer:
                raise ReviewAttemptError(
                    "review attempt resolution result is unavailable"
                )
            path = (self.root / pointer).resolve()
            if self.root not in path.parents or not path.is_file() or path.is_symlink():
                raise ReviewAttemptError(
                    "review attempt resolution result is unavailable"
                )
            result = _result_from_payload(_read_json(path))
            qualified = namespace_review_result(
                run.execution.request.policy, result
            )
            material_ids = [
                finding.finding_id
                for finding in qualified.findings
                if finding.severity in MATERIAL_SEVERITIES
            ]
            material_count += len(material_ids)
            round_ = run.rounds[lane.axis]
            envelope = review_round_envelope(round_, result)
            accepted = self.round_store.read(
                round_.owner_id, round_.operation_id
            )
            if (
                accepted.spec != round_.spec
                or accepted.lane_id != round_.lane_id
                or accepted.run_id != round_.run_id
                or accepted.state != "complete"
                or accepted.pending_effect
                or accepted.accepted_callback_id != envelope.callback_id
                or accepted.accepted_callback_kind != envelope.kind
                or accepted.accepted_callback_sha256
                != envelope.payload_sha256
            ):
                raise ReviewAttemptError(
                    "review attempt resolution callback identity changed"
                )
            boundaries[lane.axis] = {
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
                "material_finding_ids": material_ids,
            }
        if material_count == 0:
            raise ReviewAttemptError(
                "changes-requested attempt has no material resolution boundary"
            )
        return boundaries

    def complete_attempt_round(
        self,
        run: ReviewGateRun,
        lane: ReviewLaneSession,
        round_: ReviewRound,
        result: ReviewResult,
    ) -> ReviewGateDecision:
        """Accept one initial callback and terminalize after all lanes finish."""

        attempt = self._attempt()
        attempt.assert_identity(attempt.identity)
        if attempt.status != "awaiting-callback":
            raise ReviewAttemptError(
                "review attempt is not accepting an initial callback"
            )
        self._attempt_lane(attempt.identity, lane)
        if (
            run.execution.request.context.head_sha
            != attempt.identity.exact_head_sha
            or result.verification_iteration != 0
            or round_.verification_iteration != 0
        ):
            raise ReviewAttemptError(
                "review attempt rejects changed HEAD or verification iteration"
            )
        terminal_verdict = self._lane_terminal_verdict(result)
        envelope = review_round_envelope(round_, result)
        cleanup = accept_review_round(
            self.runtime, self.round_store, lane, round_, envelope
        )
        if cleanup is None:
            cleanup = finish_review_lane(self.runtime, lane)
        pointer = self._persist_result(
            run.execution.request.policy.operation_id,
            result,
            final=False,
        )
        if cleanup.state != "complete":
            terminal = ReviewAttemptTerminal(
                ReviewAttemptTerminalResult.ATTENTION_REQUIRED,
                attempt.identity.exact_head_sha,
                (),
            )
            finished = attempt.finish(attempt.identity, terminal)
            self._replace(
                status="attention-required", attempt=finished.payload()
            )
            return ReviewGateDecision("attention-required", lane, round_)
        state = self.read()
        stored_lanes = []
        for item in state.get("lanes", []):
            if not isinstance(item, dict):
                raise ReviewAttemptError("review attempt lanes are invalid")
            stored_lanes.append(
                self._lane(
                    replace(
                        lane,
                        state="complete",
                        surface_id="",
                        checkpoint="",
                        checkpoint_sha256="",
                    )
                )
                if item.get("axis") == lane.axis
                else item
            )
        normalized = replace(result, verdict=terminal_verdict)
        final_pointer = self._persist_result(
            run.execution.request.policy.operation_id,
            normalized,
            final=True,
        )
        rounds = dict(state.get("round_results") or {})
        rounds[result.axis] = pointer
        finals = dict(state.get("final_results") or {})
        finals[result.axis] = final_pointer
        self._replace(
            status="reviewing",
            lanes=stored_lanes,
            round_results=rounds,
            final_results=finals,
        )
        state = self.read()
        pointers = state.get("final_results")
        if not isinstance(pointers, dict) or set(pointers) != set(
            run.execution.request.policy.axes
        ):
            return ReviewGateDecision("awaiting-axes", lane, round_)
        lane_results = self._attempt_lane_results(run, pointers)
        terminal_result = self._terminal_result(lane_results)
        terminal = ReviewAttemptTerminal(
            terminal_result,
            attempt.identity.exact_head_sha,
            lane_results,
        )
        if terminal_result == ReviewAttemptTerminalResult.APPROVED:
            prepared = self._approval_artifacts(run.execution)
            if prepared is None:
                terminal = ReviewAttemptTerminal(
                    ReviewAttemptTerminalResult.ATTENTION_REQUIRED,
                    attempt.identity.exact_head_sha,
                    lane_results,
                )
                terminal_result = ReviewAttemptTerminalResult.ATTENTION_REQUIRED
                evidence_path = None
                evidence: dict[str, object] = {}
            else:
                evidence_path, evidence = prepared
        else:
            evidence_path = None
            evidence = {}
        finished = attempt.finish(attempt.identity, terminal)
        updates: dict[str, object] = {
            "status": terminal_result.value,
            "attempt": finished.payload(),
        }
        if terminal_result == ReviewAttemptTerminalResult.APPROVED:
            updates.update(
                context=self._context(run.execution.request.context),
                evidence=evidence,
            )
        elif terminal_result == ReviewAttemptTerminalResult.CHANGES_REQUESTED:
            updates["awaiting_resolution"] = (
                self._attempt_resolution_boundaries(run, rounds)
            )
        self._replace(**updates)
        return ReviewGateDecision(
            terminal_result.value,
            lane,
            round_,
            evidence_path=evidence_path,
        )


__all__ = ("ReviewGateAttemptMixin", "compile_review_attempt_identity")
