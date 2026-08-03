"""Ordinary review result decisions and durable approval evidence."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..contracts import AttentionReason, OperationRecord, to_dict
from ..state_machine import TERMINAL
from .review import (
    ReviewExecution,
    ReviewLaneSession,
    ReviewResult,
    ReviewRound,
    accept_review_round,
    finish_review_lane,
    review_evidence_envelope,
    review_round_envelope,
)
from .review_gate_contracts import (
    ReviewGateDecision,
    ReviewGateRun,
    _atomic_json,
    _read_json,
    _result_from_payload,
    _result_payload,
)
from review_contract import MATERIAL_SEVERITIES
from review_resolution import ReviewResolutionEvidence


class ReviewGateDecisionMixin:
    """Own verdict transitions, result files, and terminal approval."""

    def _persist_result(
        self, operation_id: str, result: ReviewResult, *, final: bool
    ) -> str:
        axis = result.axis
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
        axis = resolution.axis
        path = (
            self.root
            / operation_id
            / f"resolution-{axis}-{verification_iteration}.json"
        )
        payload = resolution.payload()
        if path.exists() and _read_json(path) != payload:
            state = self.read()
            published = state.get("resolution_evidence")
            pointer = path.relative_to(self.root).as_posix()
            if (
                state.get("status") != "awaiting-resolution"
                or not isinstance(published, dict)
                or pointer in published.values()
            ):
                raise ValueError(
                    "review resolution evidence changed across replay"
                )
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
        observed = self.runtime.status(lane.owner_id, lane.operation_id)
        parent = (
            observed
            if isinstance(observed, OperationRecord)
            else getattr(observed, "record", None)
        )
        return (
            isinstance(parent, OperationRecord)
            and parent.state == "attention-required"
            and parent.attention_reason == AttentionReason.CALLBACK_TIMEOUT
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

    def _approve(self, execution: ReviewExecution) -> Path | None:
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
            awaiting = dict(state.get("awaiting_resolution") or {})
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
                "material_finding_ids": [
                    finding.finding_id for finding in material
                ],
            }
            self._replace(
                status="awaiting-resolution",
                awaiting_resolution=awaiting,
                resolution_transport_identity_sha256="",
                lanes=[self._lane(item) for item in run.execution.lanes],
            )
            return ReviewGateDecision("awaiting-resolution", lane, round_)
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
        return ReviewGateDecision("approved", lane, evidence_path=evidence)
