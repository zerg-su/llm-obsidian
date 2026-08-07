"""Bounded review recovery and fresh-boundary restart orchestration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping

from ..contracts import OwnedResources, to_dict
from ..review_attempt import (
    ReviewAttempt,
    ReviewAttemptError,
    ReviewAttemptIdentity,
    ReviewAttemptTerminalResult,
)
from ..state_machine import TERMINAL
from ..store import StoreError
from .review import (
    ReviewContext,
    ReviewLaneSession,
    ReviewOperationRequest,
    ReviewResult,
    ReviewRound,
    ReviewSessionRequest,
    accept_review_round,
    review_round_envelope,
    review_session_specs,
    verify_review_lane,
)
from .review_gate_attempt import compile_review_attempt_identity
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

    def reopen_zero_lane_preflight_attempt(
        self,
        *,
        dispatch_operation_id: str,
        identity: ReviewAttemptIdentity,
        request: ReviewOperationRequest,
        product_root: Path,
        callback_paths: Mapping[str, Path],
    ) -> ReviewAttempt:
        """Replace one proven effect-free preflight failure exactly once."""

        product_root = product_root.expanduser().resolve()
        compiled = compile_review_attempt_identity(
            request=request,
            finalization_lineage_id=identity.finalization_lineage_id,
            cycle=identity.cycle,
            plan_sha256=identity.plan_sha256,
            outcome_sha256=identity.outcome_sha256,
        )
        if compiled != identity:
            raise ReviewAttemptError(
                "zero-lane replacement identity changed"
            )
        replacement, pending_state = self._attempt_initial_state(
            dispatch_operation_id=dispatch_operation_id,
            request=request,
            product_root=product_root,
            identity=identity,
        )
        with self._locked():
            current = _read_json(self.state_path)
            raw_attempt = current.get("attempt")
            if not isinstance(raw_attempt, Mapping):
                raise ReviewAttemptError(
                    "zero-lane recovery requires an exact attempt"
                )
            existing = ReviewAttempt.from_mapping(raw_attempt)
            if existing.identity == identity:
                if existing.status != "pending" or current != pending_state:
                    raise ReviewAttemptError(
                        "zero-lane replacement projection changed"
                    )
                return existing
            terminal = existing.terminal
            if (
                existing.status != "terminal"
                or terminal is None
                or terminal.result
                != ReviewAttemptTerminalResult.ATTENTION_REQUIRED
                or terminal.lane_results
                or current.get("status") != "attention-required"
                or current.get("active_review_operation_id")
                != existing.identity.attempt_id
                or current.get("dispatch_operation_id")
                != dispatch_operation_id
                or current.get("product_root") != str(product_root)
                or identity.cycle != existing.identity.cycle + 1
                or identity.finalization_lineage_id
                != existing.identity.finalization_lineage_id
                or identity.plan_sha256 != existing.identity.plan_sha256
                or identity.outcome_sha256 != existing.identity.outcome_sha256
                or current.get("lanes") != []
                or any(
                    current.get(field) not in ({}, None)
                    for field in (
                        "round_results",
                        "final_results",
                        "evidence",
                        "resolution_evidence",
                        "continuation_effects",
                        "review_notification_evidence",
                        "awaiting_resolution",
                        "finalizing_recovery",
                    )
                )
            ):
                raise ReviewAttemptError(
                    "review attempt is not an effect-free preflight failure"
                )
            old_axes = {lane.axis for lane in existing.identity.lanes}
            new_axes = {lane.axis for lane in identity.lanes}
            if set(callback_paths) != old_axes or old_axes != new_axes:
                raise ReviewAttemptError(
                    "zero-lane callback topology changed"
                )
            for path in callback_paths.values():
                if path.exists() or path.is_symlink():
                    raise ReviewAttemptError(
                        "zero-lane recovery found a callback artifact"
                    )
            for lane in existing.identity.lanes:
                try:
                    self.round_store.read(lane.owner_id, lane.operation_id)
                except StoreError:
                    continue
                raise ReviewAttemptError(
                    "zero-lane recovery found a reviewer operation"
                )
            self._archive_attempt_state(
                current, cycle=existing.identity.cycle
            )
            _atomic_json(self.state_path, pending_state)
        return replacement

    def _superseded_cleanup_receipts(
        self,
        previous: ReviewGateRun,
        replacement: ReviewGateRun,
        authorization: Mapping[str, object],
    ) -> tuple[Path, ...]:
        """Publish exact cleanup authority only after replacement startup."""

        store_root = Path(self.round_store.root).resolve()
        source_authorization_path = (
            self.root / str(authorization.get("pointer") or "")
        ).resolve()
        source_authorization = _read_json(source_authorization_path)
        authorization_path = (
            store_root
            / "review-supersession-authorizations"
            / (
                previous.execution.request.policy.operation_id
                + ".json"
            )
        )
        if authorization_path.exists():
            if _read_json(authorization_path) != source_authorization:
                raise ValueError(
                    "review supersession authorization changed"
                )
        else:
            _atomic_json(authorization_path, source_authorization)
        authorization_pointer = authorization_path.relative_to(
            store_root
        ).as_posix()
        authorization_sha256 = hashlib.sha256(
            authorization_path.read_bytes()
        ).hexdigest()
        replacement_by_axis = {
            lane.axis: lane for lane in replacement.execution.lanes
        }
        receipts: list[Path] = []
        for old_lane in previous.execution.lanes:
            new_lane = replacement_by_axis.get(old_lane.axis)
            if new_lane is None:
                raise ValueError("fresh review replacement lane is missing")
            old_record = self.round_store.read(
                old_lane.owner_id, old_lane.operation_id
            )
            new_record = self.round_store.read(
                new_lane.owner_id, new_lane.operation_id
            )
            old_sha256 = hashlib.sha256(
                json.dumps(
                    to_dict(old_record),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            receipt = {
                "schema_version": 1,
                "status": "authorized",
                "superseded_owner_id": old_lane.owner_id,
                "superseded_review_operation_id": (
                    previous.execution.request.policy.operation_id
                ),
                "superseded_operation_id": old_lane.operation_id,
                "superseded_run_id": old_record.run_id,
                "superseded_record_sha256": old_sha256,
                "replacement_owner_id": new_lane.owner_id,
                "replacement_review_operation_id": (
                    replacement.execution.request.policy.operation_id
                ),
                "replacement_operation_id": new_lane.operation_id,
                "replacement_run_id": new_record.run_id,
                "store_sha256": hashlib.sha256(
                    str(store_root).encode()
                ).hexdigest(),
                "authorization_pointer": authorization_pointer,
                "authorization_sha256": str(
                    authorization_sha256
                ),
            }
            path = (
                self.root
                / "superseded-review-cleanup"
                / f"{old_lane.operation_id}.json"
            )
            if path.exists():
                if _read_json(path) != receipt:
                    raise ValueError(
                        "superseded review cleanup receipt changed"
                    )
            else:
                _atomic_json(path, receipt)
            receipts.append(path)
        return tuple(receipts)

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
