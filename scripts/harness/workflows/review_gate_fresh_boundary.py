"""Fail-closed authorization for one exact fresh review boundary."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..contracts import OwnedResources
from ..state_machine import TERMINAL
from .review import ReviewContext
from .review_gate_contracts import (
    SHA256,
    ReviewGateRun,
    ReviewScopeBoundary,
    _read_json,
    review_context_sha256,
)


class ReviewGateFreshBoundaryMixin:
    """Validate coordinator authorization before any fresh reevaluation."""

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
