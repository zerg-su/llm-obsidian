"""Immutable contracts and serialization helpers for the review gate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .review import (
    ReviewContext,
    ReviewExecution,
    ReviewFinding,
    ReviewLaneSession,
    ReviewRequest,
    ReviewResult,
    ReviewRound,
)
from review_contract import VERIFY_BUDGETS


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


@dataclass(frozen=True)
class ReviewPreset:
    """Deterministic public review flags before model routing."""

    enabled: bool = True
    depth: str = "simple"
    cross_model: bool = False
    runtime: str = ""
    model: str = ""
    effort: str = ""

    def __post_init__(self) -> None:
        if self.depth not in VERIFY_BUDGETS:
            raise ValueError("review preset depth must be simple or deep")
        if self.runtime and self.runtime not in {"claude", "codex"}:
            raise ValueError("review preset runtime must be claude or codex")
        if self.model and not IDENTIFIER.fullmatch(self.model):
            raise ValueError("review model override must be a registered alias")
        if self.effort and self.effort not in EFFORTS:
            raise ValueError("review effort override is invalid")
        if not self.enabled and any(
            (
                self.depth != "simple",
                self.cross_model,
                self.runtime,
                self.model,
                self.effort,
            )
        ):
            raise ValueError("no-review cannot carry review overrides")

    @property
    def max_verify_iterations(self) -> int:
        return VERIFY_BUDGETS[self.depth]

    @classmethod
    def from_flags(
        cls,
        *,
        deep: bool = False,
        cross_model: bool = False,
        runtime: str = "",
        model: str = "",
        effort: str = "",
        no_review: bool = False,
    ) -> "ReviewPreset":
        if no_review and any(
            (deep, cross_model, runtime, model, effort)
        ):
            raise ValueError("no-review cannot be combined with review flags")
        return cls(
            enabled=not no_review,
            depth="deep" if deep else "simple",
            cross_model=cross_model,
            runtime=runtime,
            model=model,
            effort=effort,
        )

    def request(self, operation_id: str) -> ReviewRequest:
        if not self.enabled:
            raise ValueError("no-review has no provider review request")
        return ReviewRequest(
            operation_id=operation_id,
            depth=self.depth,
            cross_model=self.cross_model,
            runtime=self.runtime,
            model=self.model,
            effort=self.effort,
            max_verify_iterations=self.max_verify_iterations,
        )


@dataclass(frozen=True)
class ReviewScopeBoundary:
    """Explicit evidence authorizing one fresh compact re-evaluation."""

    kind: str
    previous_context_sha256: str
    next_context_sha256: str
    reason: str

    def __post_init__(self) -> None:
        if self.kind not in {"scope", "context"}:
            raise ValueError("fresh review boundary must be scope or context")
        if (
            not SHA256.fullmatch(self.previous_context_sha256)
            or not SHA256.fullmatch(self.next_context_sha256)
            or self.previous_context_sha256 == self.next_context_sha256
        ):
            raise ValueError("fresh review boundary must prove changed context")
        if not self.reason.strip() or len(self.reason) > 500:
            raise ValueError("fresh review boundary requires a bounded reason")


@dataclass(frozen=True)
class ReviewGateRun:
    execution: ReviewExecution
    rounds: Mapping[str, ReviewRound]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "rounds", MappingProxyType(dict(self.rounds))
        )
        if set(self.rounds) != set(self.execution.request.policy.axes):
            raise ValueError("review gate must retain one round per axis")


@dataclass(frozen=True)
class ReviewGateDecision:
    action: str
    lane: ReviewLaneSession | None = None
    round: ReviewRound | None = None
    evidence_path: Path | None = None


@dataclass(frozen=True)
class ReviewGateAuthorization:
    approved: bool
    skipped: bool
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "evidence", MappingProxyType(dict(self.evidence))
        )


def review_context_sha256(context: ReviewContext) -> str:
    identity = {
        "manifest": context.manifest,
        "head_sha": context.head_sha,
        "verification_profile": context.verification_profile,
        "verification_profile_sha256": (
            context.verification_profile_sha256
        ),
    }
    if context.implementer_summary_sha256:
        identity["implementer_summary_sha256"] = (
            context.implementer_summary_sha256
        )
    raw = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"review gate state is unavailable: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("review gate state must be an object")
    return value


def _result_payload(result: ReviewResult) -> dict[str, object]:
    return {
        "axis": result.axis,
        "verdict": result.verdict,
        "verification_iteration": result.verification_iteration,
        "findings": [
            {
                "finding_id": finding.finding_id,
                "axis": finding.axis,
                "severity": finding.severity,
                "summary": finding.summary,
                "evidence": finding.evidence,
                "file": finding.file,
                "line": finding.line,
                "recommendation": finding.recommendation,
            }
            for finding in result.findings
        ],
    }


def _result_from_payload(value: object) -> ReviewResult:
    if not isinstance(value, dict):
        raise ValueError("stored review result is invalid")
    findings = tuple(
        ReviewFinding(**item)
        for item in value.get("findings", [])
        if isinstance(item, dict)
    )
    if len(findings) != len(value.get("findings", [])):
        raise ValueError("stored review findings are invalid")
    return ReviewResult(
        axis=str(value.get("axis") or ""),
        verdict=str(value.get("verdict") or ""),
        findings=findings,
        verification_iteration=value.get("verification_iteration", -1),
    )
