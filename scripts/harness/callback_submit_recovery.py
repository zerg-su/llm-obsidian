"""Pure generation-bound policy for missing reviewer callback submits."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
SHA256 = re.compile(r"[0-9a-f]{64}")
ARTIFACT_STATES = frozenset(
    {"missing", "unstable", "stable", "symlink", "oversize", "malformed"}
)
INVALID_ARTIFACT_STATES = frozenset({"symlink", "oversize", "malformed"})
PROMPT_CLASSES = frozenset(
    {"active", "idle-prompt", "permission", "unknown", "missing"}
)
OWNERSHIP_STATES = frozenset({"alive", "dead", "unknown", "missing"})
RECOVERY_STATES = frozenset({"none", "reserved", "sent"})
TERMINAL_OPERATION_STATES = frozenset(
    {"complete", "cancelled", "failed", "timed-out"}
)


@dataclass(frozen=True)
class ArtifactEvidence:
    """Content-free observation of one bounded reviewer artifact."""

    state: str = "missing"
    sha256: str = ""


@dataclass(frozen=True)
class CallbackSubmitPolicy:
    """Code-owned timing and shared nudge ceiling."""

    probe_seconds: int
    nudge_after_seconds: int
    max_nudges: int
    schema_version: int = 1

    @classmethod
    def default(cls) -> "CallbackSubmitPolicy":
        return cls(probe_seconds=60, nudge_after_seconds=900, max_nudges=1)

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or type(self.probe_seconds) is not int
            or self.probe_seconds < 30
            or type(self.nudge_after_seconds) is not int
            or self.nudge_after_seconds < 60
            or type(self.max_nudges) is not int
            or self.max_nudges != 1
        ):
            raise ValueError("callback submit policy exceeds code-owned bounds")

    @property
    def minimum_deadline_seconds(self) -> int:
        return self.probe_seconds * 2


@dataclass(frozen=True)
class CallbackSubmitEvidence:
    """Immutable exact-generation evidence supplied by the runtime adapter."""

    observed_at: float
    generation_progress_at: float
    callback_deadline_at: float
    operation_id: str
    run_id: str
    lane_id: str
    generation: int
    expected_operation_id: str
    expected_run_id: str
    expected_lane_id: str
    expected_generation: int
    target_sha256: str
    expected_target_sha256: str
    operation_state: str
    process_status: str
    surface_status: str
    prompt_class: str
    stable_idle_observations: int
    input_artifact: ArtifactEvidence = field(default_factory=ArtifactEvidence)
    callback_artifact: ArtifactEvidence = field(default_factory=ArtifactEvidence)
    receipt_artifact: ArtifactEvidence = field(default_factory=ArtifactEvidence)
    nudge_count: int = 0
    restart_count: int = 0
    recovery_status: str = "none"
    schema_version: int = 1


@dataclass(frozen=True)
class CallbackSubmitDecision:
    """One deterministic state/action with a content-free identity."""

    state: str
    action: str
    action_id: str
    reason: str = ""
    model_effect: bool = False
    schema_version: int = 1


def _decision(
    evidence: CallbackSubmitEvidence,
    *,
    state: str,
    action: str = "none",
    reason: str = "",
    model_effect: bool = False,
) -> CallbackSubmitDecision:
    identity = json.dumps(
        {
            "state": state,
            "action": action,
            "reason": reason,
            "operation_id": evidence.operation_id,
            "run_id": evidence.run_id,
            "lane_id": evidence.lane_id,
            "generation": evidence.generation,
            "target_sha256": evidence.target_sha256,
            "input_sha256": evidence.input_artifact.sha256,
            "callback_sha256": evidence.callback_artifact.sha256,
            "receipt_sha256": evidence.receipt_artifact.sha256,
            "recovery_status": evidence.recovery_status,
            "nudge_count": evidence.nudge_count,
            "restart_count": evidence.restart_count,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return CallbackSubmitDecision(
        state=state,
        action=action,
        action_id=hashlib.sha256(identity.encode()).hexdigest(),
        reason=reason,
        model_effect=model_effect,
    )


def _attention(
    evidence: CallbackSubmitEvidence, reason: str
) -> CallbackSubmitDecision:
    return _decision(
        evidence,
        state="attention",
        action="attention-required",
        reason=reason,
    )


def _artifact_evidence_is_well_formed(artifact: object) -> bool:
    if not isinstance(artifact, ArtifactEvidence) or artifact.state not in ARTIFACT_STATES:
        return False
    if artifact.state in {"stable", "unstable"}:
        return bool(SHA256.fullmatch(artifact.sha256))
    return not artifact.sha256


def _evidence_is_well_formed(evidence: CallbackSubmitEvidence) -> bool:
    identifiers = (
        evidence.operation_id,
        evidence.run_id,
        evidence.lane_id,
        evidence.expected_operation_id,
        evidence.expected_run_id,
        evidence.expected_lane_id,
    )
    numeric_times = (
        evidence.observed_at,
        evidence.generation_progress_at,
        evidence.callback_deadline_at,
    )
    return all((isinstance(value, str) and IDENTIFIER.fullmatch(value) for value in identifiers)) and (
        evidence.schema_version == 1
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value >= 0
            for value in numeric_times
        )
        and evidence.generation_progress_at <= evidence.observed_at
        and type(evidence.generation) is int
        and evidence.generation >= 1
        and type(evidence.expected_generation) is int
        and evidence.expected_generation >= 1
        and bool(SHA256.fullmatch(evidence.target_sha256))
        and bool(SHA256.fullmatch(evidence.expected_target_sha256))
        and isinstance(evidence.operation_state, str)
        and bool(IDENTIFIER.fullmatch(evidence.operation_state))
        and evidence.process_status in OWNERSHIP_STATES
        and evidence.surface_status in OWNERSHIP_STATES
        and evidence.prompt_class in PROMPT_CLASSES
        and type(evidence.stable_idle_observations) is int
        and evidence.stable_idle_observations >= 0
        and type(evidence.nudge_count) is int
        and evidence.nudge_count >= 0
        and type(evidence.restart_count) is int
        and evidence.restart_count >= 0
        and evidence.recovery_status in RECOVERY_STATES
        and all(
            _artifact_evidence_is_well_formed(artifact)
            for artifact in (
                evidence.input_artifact,
                evidence.callback_artifact,
                evidence.receipt_artifact,
            )
        )
    )


def classify_callback_submit(
    evidence: CallbackSubmitEvidence,
    policy: CallbackSubmitPolicy | None = None,
) -> CallbackSubmitDecision:
    """Classify one reviewer generation without performing I/O or effects."""

    policy = policy or CallbackSubmitPolicy.default()
    if not isinstance(evidence, CallbackSubmitEvidence) or not _evidence_is_well_formed(evidence):
        return _attention(evidence, "callback-submit-evidence-malformed")

    artifacts = (
        evidence.input_artifact,
        evidence.callback_artifact,
        evidence.receipt_artifact,
    )
    if any(artifact.state in INVALID_ARTIFACT_STATES for artifact in artifacts):
        return _attention(evidence, "callback-submit-artifact-invalid")

    identity = (
        evidence.operation_id,
        evidence.run_id,
        evidence.lane_id,
        evidence.generation,
        evidence.target_sha256,
    )
    expected = (
        evidence.expected_operation_id,
        evidence.expected_run_id,
        evidence.expected_lane_id,
        evidence.expected_generation,
        evidence.expected_target_sha256,
    )
    if identity != expected:
        return _attention(evidence, "callback-submit-stale-generation")

    if evidence.receipt_artifact.state == "stable":
        return _decision(evidence, state="accepted")
    if evidence.callback_artifact.state == "stable":
        return _decision(
            evidence, state="callback-ready", action="accept-callback"
        )
    if evidence.input_artifact.state == "stable":
        return _decision(
            evidence, state="typed-input-ready", action="submit-typed-input"
        )

    if evidence.operation_state in TERMINAL_OPERATION_STATES:
        return _attention(evidence, "callback-submit-terminal")
    if evidence.process_status == "dead":
        return _attention(evidence, "callback-submit-provider-unavailable")
    if evidence.process_status != "alive" or evidence.surface_status != "alive":
        return _attention(evidence, "callback-submit-ownership-lost")
    if evidence.prompt_class == "permission":
        return _attention(evidence, "callback-submit-permission")
    if evidence.prompt_class in {"unknown", "missing"}:
        return _attention(evidence, "callback-submit-evidence-unknown")
    if any(artifact.state == "unstable" for artifact in artifacts):
        return _decision(evidence, state="working")

    if evidence.recovery_status == "reserved":
        if evidence.nudge_count != policy.max_nudges:
            return _attention(evidence, "callback-submit-evidence-malformed")
        return _decision(
            evidence,
            state="recovery-reserved",
            action="send-reserved-recovery",
            model_effect=True,
        )
    if evidence.recovery_status == "sent":
        if evidence.nudge_count != policy.max_nudges:
            return _attention(evidence, "callback-submit-evidence-malformed")
        return _decision(evidence, state="recovery-sent")

    if evidence.prompt_class == "active" or evidence.stable_idle_observations < 2:
        return _decision(evidence, state="working")
    if evidence.observed_at - evidence.generation_progress_at < policy.nudge_after_seconds:
        return _decision(evidence, state="working")
    if evidence.callback_deadline_at - evidence.observed_at < policy.minimum_deadline_seconds:
        return _attention(evidence, "callback-submit-deadline-insufficient")
    if evidence.nudge_count >= policy.max_nudges:
        return _attention(evidence, "callback-submit-budget-exhausted")
    return _decision(
        evidence,
        state="idle-without-submit",
        action="reserve-submit-recovery",
    )
