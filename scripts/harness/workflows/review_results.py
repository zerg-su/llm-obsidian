"""Typed review findings, aggregation, evidence, and lane verification."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Callable, Mapping, Protocol

from ..contracts import (
    AttentionReason,
    CallbackEnvelope,
    OperationRecord,
    OperationSpec,
    RuntimeRoute,
)
from ..runtime_sessions import RuntimeSessionRequest
from ..state_machine import TERMINAL
from review_contract import (
    MATERIAL_SEVERITIES,
    SEVERITIES,
    VERIFY_BUDGETS,
    ReviewContractError,
    axis_finding_id,
    require_unique_finding_ids,
    validate_finding,
    review_axis_responsibility,
)


from .review_contracts import ReviewRequest, ReviewSessionIdentity


@dataclass(frozen=True)
class ReviewFinding:
    finding_id: str
    axis: str
    severity: str
    summary: str
    evidence: str
    file: str = "unknown"
    line: int | None = None
    recommendation: str = "Resolve this finding before approval."

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError("review severity must be critical, important, or minor")
        if not all(
            (
                self.finding_id,
                self.axis,
                self.summary,
                self.evidence,
                self.file,
                self.recommendation,
            )
        ):
            raise ValueError("review finding fields are required")
        if self.line is not None and (
            not isinstance(self.line, int)
            or isinstance(self.line, bool)
            or self.line < 1
        ):
            raise ValueError("review finding line must be positive or null")


@dataclass(frozen=True)
class ReviewResult:
    axis: str
    verdict: str
    findings: tuple[ReviewFinding, ...] = ()
    verification_iteration: int = 0

    def __post_init__(self) -> None:
        if self.verdict not in {"approve", "changes-requested", "blocked"}:
            raise ValueError("invalid review verdict")
        if any(row.axis != self.axis for row in self.findings):
            raise ValueError("review findings cannot cross axes")
        if (
            self.verdict == "approve"
            and any(
                finding.severity in MATERIAL_SEVERITIES
                for finding in self.findings
            )
        ):
            raise ValueError("review axis cannot approve with material findings")
        if (
            not isinstance(self.verification_iteration, int)
            or isinstance(self.verification_iteration, bool)
            or self.verification_iteration < 0
        ):
            raise ValueError("verification iteration must be a non-negative integer")


def aggregate(request: ReviewRequest, results: Mapping[str, ReviewResult]) -> dict[str, object]:
    if set(results) != set(request.axes):
        raise ValueError("review aggregation requires every independent axis")
    ordered = tuple(results[axis] for axis in request.axes)
    if any(
        row.verification_iteration > request.max_verify_iterations for row in ordered
    ):
        raise ValueError("review result exceeds the verification iteration budget")
    canonical_findings: dict[str, list[dict[str, object]]] = {}
    try:
        for raw_row in ordered:
            row = namespace_review_result(request, raw_row)
            canonical_findings[row.axis] = [
                validate_finding(
                    {
                        "finding_id": finding.finding_id,
                        "severity": finding.severity,
                        "file": finding.file,
                        "line": finding.line,
                        "summary": finding.summary,
                        "evidence": finding.evidence,
                        "recommendation": finding.recommendation,
                    },
                    f"review result {row.axis} findings[{index}]",
                )
                for index, finding in enumerate(row.findings)
            ]
        require_unique_finding_ids(
            (
                finding["finding_id"]
                for findings in canonical_findings.values()
                for finding in findings
            ),
            "review finding_id values across axes",
        )
    except ReviewContractError as exc:
        raise ValueError(f"review findings are invalid: {exc}") from exc
    verdict = (
        "blocked" if any(row.verdict == "blocked" for row in ordered)
        else "changes-requested" if any(row.verdict == "changes-requested" for row in ordered)
        else "approve"
    )
    return {
        "verdict": verdict,
        "axes": [
            {
                "axis": row.axis,
                "verdict": row.verdict,
                "findings": canonical_findings[row.axis],
                "verification_iteration": row.verification_iteration,
            }
            for row in ordered
        ],
    }


def namespace_review_result(
    request: ReviewRequest, result: ReviewResult
) -> ReviewResult:
    """Return aggregate-safe finding IDs without changing callback evidence."""

    if len(request.axes) == 1 or not result.findings:
        return result
    prefix = f"{result.axis}:"
    if result.verification_iteration == 0 and any(
        finding.finding_id.startswith(prefix) for finding in result.findings
    ):
        raise ValueError(
            "initial multi-lane finding_id uses the reserved aggregate prefix"
        )
    qualified = replace(
        result,
        findings=tuple(
            replace(
                finding,
                finding_id=axis_finding_id(result.axis, finding.finding_id),
            )
            for finding in result.findings
        ),
    )
    try:
        require_unique_finding_ids(
            (finding.finding_id for finding in qualified.findings),
            f"review result {result.axis} qualified finding IDs",
        )
    except ReviewContractError as exc:
        raise ValueError(str(exc)) from exc
    return qualified


def aggregate_review_evidence(
    execution: ReviewExecution,
    results: Mapping[str, ReviewResult],
    *,
    verification_gaps: tuple[str, ...] = (),
    notes_for_executor: tuple[str, ...] = (),
    residual_risks: tuple[str, ...] = (),
) -> dict[str, object]:
    """Build the canonical review-v1 evidence after every lane has completed."""

    combined = aggregate(execution.request.policy, results)
    run_id = hashlib.sha256(
        (
            execution.request.policy.operation_id
            + ":"
            + ":".join(lane.run_id for lane in execution.lanes)
        ).encode()
    ).hexdigest()[:32]
    context = execution.request.context
    return {
        "schema_version": 1,
        "operation_id": execution.request.policy.operation_id,
        "run_id": run_id,
        "mode": execution.request.policy.depth,
        "head_sha": context.head_sha,
        "verification_profile": {
            "name": context.verification_profile,
            "sha256": context.verification_profile_sha256,
        },
        "verdict": combined["verdict"],
        "axes": combined["axes"],
        "verification_gaps": list(verification_gaps),
        "notes_for_executor": list(notes_for_executor),
        "residual_risks": list(residual_risks),
    }


def review_evidence_envelope(
    execution: ReviewExecution,
    results: Mapping[str, ReviewResult],
    **notes: tuple[str, ...],
) -> CallbackEnvelope:
    """Wrap final aggregate evidence for the existing archive transport."""

    payload = aggregate_review_evidence(execution, results, **notes)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return CallbackEnvelope(
        callback_id=f"review-{digest[:24]}",
        operation_id=str(payload["operation_id"]),
        run_id=str(payload["run_id"]),
        kind="review",
        payload=payload,
        payload_sha256=digest,
    )


def verify_lane(original_surface: str, verification_surface: str) -> None:
    if original_surface != verification_surface:
        raise ValueError("same-session verification cannot open a second surface")


@dataclass(frozen=True)
class ReviewLaneIdentity:
    axis: str
    lane_id: str
    surface_id: str

    def __post_init__(self) -> None:
        try:
            review_axis_responsibility(self.axis)
        except ValueError as exc:
            raise ValueError("invalid review axis") from exc
        if not self.lane_id or not self.surface_id:
            raise ValueError("review lane and surface identity are required")


def verify_session(
    original: ReviewLaneIdentity, verification: ReviewLaneIdentity
) -> None:
    """Fail closed unless verification reuses the exact axis/lane/surface."""

    if original != verification:
        raise ValueError(
            "same-session verification must reuse the exact axis, lane, and surface"
        )


def resolution_required(result: ReviewResult) -> bool:
    return result.verdict == "changes-requested" and any(
        finding.severity in MATERIAL_SEVERITIES for finding in result.findings
    )
