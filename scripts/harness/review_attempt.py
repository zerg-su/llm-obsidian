"""Immutable identity and one-shot terminal state for one exact-HEAD review."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields
from enum import Enum
from typing import Mapping

from review_contract import compile_review_axes


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
GIT_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
ATTEMPT_STATES = {"pending", "running", "awaiting-callback", "terminal"}
LANE_VERDICTS = {"approve", "changes-requested", "blocked"}
EXACT_HEAD_REVIEW_PROTOCOL = "exact-head-attempt-v1"


class ReviewAttemptError(ValueError):
    """Raised before an immutable review-attempt invariant can be crossed."""


class ReviewAttemptTerminalResult(str, Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes-requested"
    BLOCKED = "blocked"
    ATTENTION_REQUIRED = "attention-required"


def _identifier(value: str, label: str, *, optional: bool = False) -> None:
    if optional and value == "":
        return
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise ReviewAttemptError(f"{label} must be a bounded identifier")


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise ReviewAttemptError(f"{label} must be a lowercase sha256")


def _exact_fields(raw: Mapping[str, object], value: type[object], label: str) -> None:
    expected = {field.name for field in fields(value)}
    if set(raw) != expected:
        raise ReviewAttemptError(f"{label} fields are not exact")


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ReviewAttemptPolicy:
    """Provider-neutral policy frozen before any provider session starts."""

    depth: str
    cross_model: bool
    runtime: str
    model: str
    effort: str
    max_verify_iterations: int
    purpose: str
    selected_provider: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        expected_budget = (
            0
            if self.purpose == "release"
            else min(
                {"simple": 1, "deep": 2, "full": 2}.get(self.depth, -1),
                1 if self.purpose == "intent" else 2,
            )
        )
        if (
            self.schema_version != 1
            or self.depth not in {"simple", "deep", "full"}
            or type(self.cross_model) is not bool
            or self.runtime not in {"", "claude", "codex"}
            or isinstance(self.max_verify_iterations, bool)
            or not isinstance(self.max_verify_iterations, int)
            or not 0 <= self.max_verify_iterations <= 2
            or self.max_verify_iterations != expected_budget
            or self.purpose not in {"intent", "implementation", "release"}
            or self.selected_provider not in {"", "anthropic", "openai"}
        ):
            raise ReviewAttemptError("review attempt policy is invalid")
        _identifier(self.model, "review model", optional=True)
        _identifier(self.effort, "review effort", optional=True)

    def payload(self) -> dict[str, object]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @property
    def sha256(self) -> str:
        return _canonical_sha256(self.payload())

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ReviewAttemptPolicy":
        _exact_fields(raw, cls, "review attempt policy")
        try:
            return cls(**raw)
        except TypeError as exc:
            raise ReviewAttemptError("review attempt policy is invalid") from exc


@dataclass(frozen=True)
class ReviewAttemptLaneIdentity:
    """Deterministic lane identity computed before the runtime is invoked."""

    axis: str
    owner_id: str
    operation_id: str
    lane_id: str
    run_id: str
    runtime: str
    model: str
    effort: str
    profile: str
    routing_sha256: str
    verification_iteration: int = 0
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or self.runtime not in {"claude", "codex"}
            or self.verification_iteration != 0
        ):
            raise ReviewAttemptError(
                "review attempt lanes permit only the initial exact-HEAD round"
            )
        for value, label in (
            (self.axis, "review axis"),
            (self.owner_id, "review owner"),
            (self.operation_id, "review operation"),
            (self.lane_id, "review lane"),
            (self.run_id, "review run"),
            (self.model, "review route model"),
            (self.effort, "review route effort"),
            (self.profile, "review route profile"),
        ):
            _identifier(value, label)
        _sha256(self.routing_sha256, "review route digest")

    def payload(self) -> dict[str, object]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, object]
    ) -> "ReviewAttemptLaneIdentity":
        _exact_fields(raw, cls, "review attempt lane identity")
        try:
            return cls(**raw)
        except TypeError as exc:
            raise ReviewAttemptError("review attempt lane identity is invalid") from exc


@dataclass(frozen=True)
class ReviewAttemptIdentity:
    """The complete immutable truth for one review cycle and one Git HEAD."""

    attempt_id: str
    finalization_lineage_id: str
    cycle: int
    plan_sha256: str
    outcome_sha256: str
    exact_head_sha: str
    policy: ReviewAttemptPolicy
    lanes: tuple[ReviewAttemptLaneIdentity, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or isinstance(self.cycle, bool)
            or not isinstance(self.cycle, int)
            or self.cycle < 1
            or not isinstance(self.policy, ReviewAttemptPolicy)
            or not isinstance(self.lanes, tuple)
            or not self.lanes
            or any(not isinstance(lane, ReviewAttemptLaneIdentity) for lane in self.lanes)
        ):
            raise ReviewAttemptError("review attempt identity is invalid")
        _identifier(self.attempt_id, "review attempt")
        _identifier(self.finalization_lineage_id, "finalization lineage")
        _sha256(self.plan_sha256, "review plan digest")
        _sha256(self.outcome_sha256, "review Outcome Contract digest")
        if GIT_SHA.fullmatch(self.exact_head_sha) is None:
            raise ReviewAttemptError("review attempt HEAD must be an exact Git object id")
        for label, values in (
            ("axis", tuple(lane.axis for lane in self.lanes)),
            ("operation", tuple(lane.operation_id for lane in self.lanes)),
            ("lane", tuple(lane.lane_id for lane in self.lanes)),
            ("run", tuple(lane.run_id for lane in self.lanes)),
        ):
            if len(set(values)) != len(values):
                raise ReviewAttemptError(f"review attempt {label} identity is duplicated")
        try:
            expected_axes = compile_review_axes(
                self.policy.depth,
                selected_provider=self.policy.selected_provider,
            )
        except ValueError as exc:
            raise ReviewAttemptError("review attempt policy topology is invalid") from exc
        if tuple(lane.axis for lane in self.lanes) != expected_axes:
            raise ReviewAttemptError("review attempt lane topology changed")

    def payload(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "finalization_lineage_id": self.finalization_lineage_id,
            "cycle": self.cycle,
            "plan_sha256": self.plan_sha256,
            "outcome_sha256": self.outcome_sha256,
            "exact_head_sha": self.exact_head_sha,
            "policy": self.policy.payload(),
            "lanes": [lane.payload() for lane in self.lanes],
            "schema_version": self.schema_version,
        }

    @property
    def sha256(self) -> str:
        return _canonical_sha256(self.payload())

    @property
    def policy_sha256(self) -> str:
        return self.policy.sha256

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ReviewAttemptIdentity":
        _exact_fields(raw, cls, "review attempt identity")
        policy = raw.get("policy")
        lanes = raw.get("lanes")
        if not isinstance(policy, Mapping) or not isinstance(lanes, list):
            raise ReviewAttemptError("review attempt identity is invalid")
        if any(not isinstance(item, Mapping) for item in lanes):
            raise ReviewAttemptError("review attempt identity is invalid")
        try:
            return cls(
                attempt_id=raw["attempt_id"],
                finalization_lineage_id=raw["finalization_lineage_id"],
                cycle=raw["cycle"],
                plan_sha256=raw["plan_sha256"],
                outcome_sha256=raw["outcome_sha256"],
                exact_head_sha=raw["exact_head_sha"],
                policy=ReviewAttemptPolicy.from_mapping(policy),
                lanes=tuple(
                    ReviewAttemptLaneIdentity.from_mapping(item)
                    for item in lanes
                ),
                schema_version=raw["schema_version"],
            )
        except (KeyError, TypeError) as exc:
            raise ReviewAttemptError("review attempt identity is invalid") from exc


@dataclass(frozen=True)
class ReviewAttemptLaneResult:
    axis: str
    verdict: str
    result_sha256: str
    finding_ids: tuple[str, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or self.verdict not in LANE_VERDICTS
            or not isinstance(self.finding_ids, tuple)
        ):
            raise ReviewAttemptError("review attempt lane result is invalid")
        _identifier(self.axis, "review result axis")
        _sha256(self.result_sha256, "review result digest")
        for finding_id in self.finding_ids:
            _identifier(finding_id, "review finding")
        if len(set(self.finding_ids)) != len(self.finding_ids):
            raise ReviewAttemptError("review finding identity is duplicated")

    def payload(self) -> dict[str, object]:
        return {
            "axis": self.axis,
            "verdict": self.verdict,
            "result_sha256": self.result_sha256,
            "finding_ids": list(self.finding_ids),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ReviewAttemptLaneResult":
        _exact_fields(raw, cls, "review attempt lane result")
        finding_ids = raw.get("finding_ids")
        if not isinstance(finding_ids, list):
            raise ReviewAttemptError("review attempt lane result is invalid")
        try:
            return cls(
                axis=raw["axis"],
                verdict=raw["verdict"],
                result_sha256=raw["result_sha256"],
                finding_ids=tuple(finding_ids),
                schema_version=raw["schema_version"],
            )
        except (KeyError, TypeError) as exc:
            raise ReviewAttemptError("review attempt lane result is invalid") from exc


@dataclass(frozen=True)
class ReviewAttemptTerminal:
    result: ReviewAttemptTerminalResult
    exact_head_sha: str
    lane_results: tuple[ReviewAttemptLaneResult, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.result, str):
            try:
                object.__setattr__(self, "result", ReviewAttemptTerminalResult(self.result))
            except ValueError as exc:
                raise ReviewAttemptError("review attempt terminal result is invalid") from exc
        if (
            self.schema_version != 1
            or not isinstance(self.result, ReviewAttemptTerminalResult)
            or GIT_SHA.fullmatch(self.exact_head_sha) is None
            or not isinstance(self.lane_results, tuple)
            or any(not isinstance(item, ReviewAttemptLaneResult) for item in self.lane_results)
        ):
            raise ReviewAttemptError("review attempt terminal result is invalid")

    def validate_for(self, identity: ReviewAttemptIdentity) -> None:
        axes = tuple(item.axis for item in self.lane_results)
        expected_axes = tuple(lane.axis for lane in identity.lanes)
        if self.exact_head_sha != identity.exact_head_sha:
            raise ReviewAttemptError("review attempt terminal HEAD changed")
        if self.result != ReviewAttemptTerminalResult.ATTENTION_REQUIRED and axes != expected_axes:
            raise ReviewAttemptError("review attempt terminal lanes are incomplete or reordered")
        if len(set(axes)) != len(axes) or any(axis not in expected_axes for axis in axes):
            raise ReviewAttemptError("review attempt terminal lane identity is invalid")
        verdicts = {item.verdict for item in self.lane_results}
        if self.result == ReviewAttemptTerminalResult.APPROVED and verdicts != {"approve"}:
            raise ReviewAttemptError("approved attempt contains a non-approval lane")
        if self.result == ReviewAttemptTerminalResult.CHANGES_REQUESTED and (
            "changes-requested" not in verdicts or "blocked" in verdicts
        ):
            raise ReviewAttemptError("changes-requested attempt has inconsistent lanes")
        if self.result == ReviewAttemptTerminalResult.BLOCKED and "blocked" not in verdicts:
            raise ReviewAttemptError("blocked attempt has no blocked lane")

    def payload(self) -> dict[str, object]:
        return {
            "result": self.result.value,
            "exact_head_sha": self.exact_head_sha,
            "lane_results": [item.payload() for item in self.lane_results],
            "schema_version": self.schema_version,
        }

    @property
    def sha256(self) -> str:
        return _canonical_sha256(self.payload())

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ReviewAttemptTerminal":
        _exact_fields(raw, cls, "review attempt terminal result")
        lane_results = raw.get("lane_results")
        if not isinstance(lane_results, list):
            raise ReviewAttemptError("review attempt terminal result is invalid")
        if any(not isinstance(item, Mapping) for item in lane_results):
            raise ReviewAttemptError("review attempt terminal result is invalid")
        try:
            return cls(
                result=ReviewAttemptTerminalResult(raw["result"]),
                exact_head_sha=raw["exact_head_sha"],
                lane_results=tuple(
                    ReviewAttemptLaneResult.from_mapping(item)
                    for item in lane_results
                ),
                schema_version=raw["schema_version"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewAttemptError("review attempt terminal result is invalid") from exc


@dataclass(frozen=True)
class ReviewAttempt:
    identity: ReviewAttemptIdentity
    status: str
    terminal: ReviewAttemptTerminal | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or not isinstance(self.identity, ReviewAttemptIdentity)
            or self.status not in ATTEMPT_STATES
            or (self.status == "terminal") != (self.terminal is not None)
        ):
            raise ReviewAttemptError("review attempt state is invalid")
        if self.terminal is not None:
            self.terminal.validate_for(self.identity)

    @classmethod
    def pending(cls, identity: ReviewAttemptIdentity) -> "ReviewAttempt":
        return cls(identity, "pending")

    def assert_identity(self, candidate: ReviewAttemptIdentity) -> None:
        if candidate != self.identity:
            if candidate.exact_head_sha != self.identity.exact_head_sha:
                raise ReviewAttemptError("review attempt cannot bind a changed HEAD")
            raise ReviewAttemptError("review attempt identity changed")

    def start(self, candidate: ReviewAttemptIdentity) -> "ReviewAttempt":
        self.assert_identity(candidate)
        if self.status != "pending":
            raise ReviewAttemptError("review attempt cannot start twice")
        return ReviewAttempt(self.identity, "running")

    def await_callback(self, candidate: ReviewAttemptIdentity) -> "ReviewAttempt":
        self.assert_identity(candidate)
        if self.status != "running":
            raise ReviewAttemptError("review attempt cannot enter callback wait")
        return ReviewAttempt(self.identity, "awaiting-callback")

    def finish(
        self,
        candidate: ReviewAttemptIdentity,
        terminal: ReviewAttemptTerminal,
    ) -> "ReviewAttempt":
        self.assert_identity(candidate)
        if self.status != "awaiting-callback":
            raise ReviewAttemptError("review attempt terminal result is one-shot")
        terminal.validate_for(self.identity)
        return ReviewAttempt(self.identity, "terminal", terminal)

    def fail_attention(
        self,
        candidate: ReviewAttemptIdentity,
        lane_results: tuple[ReviewAttemptLaneResult, ...] = (),
    ) -> "ReviewAttempt":
        """Terminalize an ambiguous or failed attempt without permitting replay."""

        self.assert_identity(candidate)
        if self.status == "terminal":
            raise ReviewAttemptError("review attempt terminal result is one-shot")
        terminal = ReviewAttemptTerminal(
            ReviewAttemptTerminalResult.ATTENTION_REQUIRED,
            self.identity.exact_head_sha,
            lane_results,
        )
        terminal.validate_for(self.identity)
        return ReviewAttempt(self.identity, "terminal", terminal)

    def rearm(self, candidate: ReviewAttemptIdentity) -> "ReviewAttempt":
        self.assert_identity(candidate)
        raise ReviewAttemptError("review attempt cannot be rearmed")

    def payload(self) -> dict[str, object]:
        return {
            "identity": self.identity.payload(),
            "status": self.status,
            "terminal": None if self.terminal is None else self.terminal.payload(),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ReviewAttempt":
        _exact_fields(raw, cls, "review attempt")
        identity = raw.get("identity")
        terminal = raw.get("terminal")
        if not isinstance(identity, Mapping) or terminal is not None and not isinstance(terminal, Mapping):
            raise ReviewAttemptError("review attempt state is invalid")
        try:
            return cls(
                identity=ReviewAttemptIdentity.from_mapping(identity),
                status=raw["status"],
                terminal=(
                    None
                    if terminal is None
                    else ReviewAttemptTerminal.from_mapping(terminal)
                ),
                schema_version=raw["schema_version"],
            )
        except (KeyError, TypeError) as exc:
            raise ReviewAttemptError("review attempt state is invalid") from exc


@dataclass(frozen=True)
class LegacyCrossHeadResumeDisposition:
    status: str = "legacy-cross-head-resume-disabled"
    allowed_actions: tuple[str, ...] = ("inspect", "archive", "cleanup")
    provider_effect_allowed: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or self.status != "legacy-cross-head-resume-disabled"
            or self.allowed_actions != ("inspect", "archive", "cleanup")
            or self.provider_effect_allowed is not False
        ):
            raise ReviewAttemptError(
                "legacy cross-HEAD resume disposition is invalid"
            )

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "allowed_actions": list(self.allowed_actions),
            "provider_effect_allowed": self.provider_effect_allowed,
        }


LEGACY_CROSS_HEAD_RESUME_DISABLED = LegacyCrossHeadResumeDisposition()


__all__ = (
    "EXACT_HEAD_REVIEW_PROTOCOL",
    "LEGACY_CROSS_HEAD_RESUME_DISABLED",
    "LegacyCrossHeadResumeDisposition",
    "ReviewAttempt",
    "ReviewAttemptError",
    "ReviewAttemptIdentity",
    "ReviewAttemptLaneIdentity",
    "ReviewAttemptLaneResult",
    "ReviewAttemptPolicy",
    "ReviewAttemptTerminal",
    "ReviewAttemptTerminalResult",
)
