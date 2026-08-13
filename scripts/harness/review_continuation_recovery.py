"""Pure RC6.4 policy for two bounded review continuation recoveries.

Runtime adapters collect durable records and pass an immutable snapshot here.
This module performs no I/O and owns the eligibility decision plus the
canonical identity used by the exactly-once recovery receipt.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Mapping


_HEAD_RE = re.compile(r"[0-9a-f]{40,64}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_LIVE_ROUND_STATES = frozenset(
    {
        "created",
        "preflight",
        "starting",
        "running",
        "awaiting-callback",
        "verifying",
        "finalizing",
        "exiting",
    }
)


class RecoveryDisposition(str, Enum):
    REVIEW_IN_PROGRESS = "review-in-progress"
    REVIEW_DRIVE_REARM = "review-drive-rearm"
    ACCEPTED_CALLBACK_INGEST = "accepted-callback-ingest"
    REFUSE = "refuse"


class RecoveryReason(str, Enum):
    ELIGIBLE = "eligible"
    REVIEW_ACTIVE = "review-active"
    ATTENTION_NOT_RECOVERABLE = "attention-not-recoverable"
    PENDING_EFFECT = "pending-effect"
    EFFECT_REPLAY_REQUIRED = "effect-replay-required"
    MALFORMED_EVIDENCE = "malformed-evidence"
    GATE_STATE_MISMATCH = "gate-state-mismatch"
    RESOLUTION_MISSING = "resolution-missing"
    RESOLUTION_IDENTITY_MISMATCH = "resolution-identity-mismatch"
    HEAD_UNCHANGED = "head-unchanged"
    VERIFICATION_IDENTITY_MISMATCH = "verification-identity-mismatch"
    REVIEW_CEILING_EXHAUSTED = "review-ceiling-exhausted"
    CALLBACK_MISSING = "callback-missing"
    CALLBACK_AMBIGUOUS = "callback-ambiguous"
    CALLBACK_ALREADY_CONSUMED = "callback-already-consumed"
    CALLBACK_IDENTITY_MISMATCH = "callback-identity-mismatch"


@dataclass(frozen=True)
class RootSnapshot:
    owner_id: str
    operation_id: str
    run_id: str
    revision: int
    state: str
    resume_state: str = ""
    pending_effect: str = ""


@dataclass(frozen=True)
class AttemptSnapshot:
    attempt_id: str
    status: str
    exact_head: str
    cycle: int
    max_cycles: int


@dataclass(frozen=True)
class GateSnapshot:
    status: str
    sha256: str
    context_head: str


@dataclass(frozen=True)
class ResolutionSnapshot:
    reviewed_head: str
    current_head: str
    sha256: str


@dataclass(frozen=True)
class VerificationSnapshot:
    status: str
    head: str
    receipt_sha256: str


@dataclass(frozen=True)
class ReviewLane:
    axis: str
    operation_id: str
    run_id: str
    lane_id: str
    state: str
    round_operation_id: str
    round_run_id: str
    round_state: str
    launch_in_progress: bool = False
    ready_identity_exact: bool = False
    process_alive: bool = False


@dataclass(frozen=True)
class AcceptedCallback:
    attempt_id: str
    axis: str
    callback_id: str
    kind: str
    lane_id: str
    operation_id: str
    parent_operation_id: str
    payload_sha256: str
    run_id: str


@dataclass(frozen=True)
class RecoveryIdentity:
    recovery_class: str
    owner_id: str
    root_operation_id: str
    root_run_id: str
    root_revision: int
    attempt_id: str
    gate_sha256: str
    authority_sha256: str
    lane_id: str = ""
    round_operation_id: str = ""
    round_run_id: str = ""
    callback_id: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "RecoveryIdentity":
        try:
            return cls(
                recovery_class=str(raw["recovery_class"]),
                owner_id=str(raw["owner_id"]),
                root_operation_id=str(raw["root_operation_id"]),
                root_run_id=str(raw["root_run_id"]),
                root_revision=_integer(raw["root_revision"]),
                attempt_id=str(raw["attempt_id"]),
                gate_sha256=str(raw["gate_sha256"]),
                authority_sha256=str(raw["authority_sha256"]),
                lane_id=str(raw.get("lane_id") or ""),
                round_operation_id=str(raw.get("round_operation_id") or ""),
                round_run_id=str(raw.get("round_run_id") or ""),
                callback_id=str(raw.get("callback_id") or ""),
            )
        except KeyError as exc:
            raise ValueError("recovery identity is incomplete") from exc

    @property
    def sha256(self) -> str:
        encoded = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RecoveryReceipt:
    identity: RecoveryIdentity
    identity_sha256: str
    status: str = "prepared"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "RecoveryReceipt":
        identity_raw = raw.get("identity")
        if not isinstance(identity_raw, Mapping):
            raise ValueError("recovery receipt identity is unavailable")
        identity = RecoveryIdentity.from_mapping(identity_raw)
        receipt = cls(
            identity=identity,
            identity_sha256=str(raw.get("identity_sha256") or ""),
            status=str(raw.get("status") or ""),
        )
        if (
            receipt.status not in {"prepared", "finalized"}
            or receipt.identity_sha256 != identity.sha256
        ):
            raise ValueError("recovery receipt identity changed")
        return receipt

    def payload(
        self,
        *,
        status: str | None = None,
        outcome: str = "",
        reason: str = "",
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": 1,
            "status": status or self.status,
            "identity": asdict(self.identity),
            "identity_sha256": self.identity_sha256,
        }
        if outcome:
            value["outcome"] = outcome
        if reason:
            value["reason"] = reason
        return value


@dataclass(frozen=True)
class RecoveryDecision:
    disposition: RecoveryDisposition
    reason: RecoveryReason
    receipt: RecoveryReceipt | None = None


@dataclass(frozen=True)
class RecoverySnapshot:
    recovery_class: str
    root: RootSnapshot
    gate: GateSnapshot
    attempt: AttemptSnapshot
    current_head: str
    attention_status: str = ""
    resolution: ResolutionSnapshot | None = None
    verification: VerificationSnapshot | None = None
    lanes: tuple[ReviewLane, ...] = ()
    accepted_callbacks: tuple[AcceptedCallback, ...] = ()
    consumed_callback_ids: frozenset[str] = frozenset()
    effect_requires_replay: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "RecoverySnapshot":
        root = _object(raw.get("root"))
        gate = _object(raw.get("gate"))
        attempt = _object(raw.get("attempt"))
        resolution_raw = raw.get("resolution")
        verification_raw = raw.get("verification")
        return cls(
            recovery_class=str(raw.get("recovery_class") or ""),
            attention_status=str(raw.get("attention_status") or ""),
            current_head=str(raw.get("current_head") or ""),
            effect_requires_replay=raw.get("effect_requires_replay") is True,
            root=RootSnapshot(
                owner_id=str(root.get("owner_id") or ""),
                operation_id=str(root.get("operation_id") or ""),
                run_id=str(root.get("run_id") or ""),
                revision=_integer(root.get("revision")),
                state=str(root.get("state") or ""),
                resume_state=str(root.get("resume_state") or ""),
                pending_effect=str(root.get("pending_effect") or ""),
            ),
            gate=GateSnapshot(
                status=str(gate.get("status") or ""),
                sha256=str(gate.get("sha256") or ""),
                context_head=str(gate.get("context_head") or ""),
            ),
            attempt=AttemptSnapshot(
                attempt_id=str(attempt.get("attempt_id") or ""),
                status=str(attempt.get("status") or ""),
                exact_head=str(attempt.get("exact_head") or ""),
                cycle=_integer(attempt.get("cycle")),
                max_cycles=_integer(attempt.get("max_cycles")),
            ),
            resolution=(
                _resolution(_object(resolution_raw))
                if isinstance(resolution_raw, Mapping)
                else None
            ),
            verification=(
                _verification(_object(verification_raw))
                if isinstance(verification_raw, Mapping)
                else None
            ),
            lanes=tuple(
                _lane(_object(item))
                for item in _sequence(raw.get("lanes"))
                if isinstance(item, Mapping)
            ),
            accepted_callbacks=tuple(
                _callback(_object(item))
                for item in _sequence(raw.get("accepted_callbacks"))
                if isinstance(item, Mapping)
            ),
            consumed_callback_ids=frozenset(
                str(item)
                for item in _sequence(raw.get("consumed_callback_ids"))
                if isinstance(item, str)
            ),
        )


def _object(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> tuple[object, ...]:
    return tuple(value) if isinstance(value, list) else ()


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _resolution(raw: Mapping[str, object]) -> ResolutionSnapshot:
    return ResolutionSnapshot(
        reviewed_head=str(raw.get("reviewed_head") or ""),
        current_head=str(raw.get("current_head") or ""),
        sha256=str(raw.get("sha256") or ""),
    )


def _verification(raw: Mapping[str, object]) -> VerificationSnapshot:
    return VerificationSnapshot(
        status=str(raw.get("status") or ""),
        head=str(raw.get("head") or ""),
        receipt_sha256=str(raw.get("receipt_sha256") or ""),
    )


def _lane(raw: Mapping[str, object]) -> ReviewLane:
    return ReviewLane(
        axis=str(raw.get("axis") or ""),
        operation_id=str(raw.get("operation_id") or ""),
        run_id=str(raw.get("run_id") or ""),
        lane_id=str(raw.get("lane_id") or ""),
        state=str(raw.get("state") or ""),
        round_operation_id=str(raw.get("round_operation_id") or ""),
        round_run_id=str(raw.get("round_run_id") or ""),
        round_state=str(raw.get("round_state") or ""),
        launch_in_progress=raw.get("launch_in_progress") is True,
        ready_identity_exact=raw.get("ready_identity_exact") is True,
        process_alive=raw.get("process_alive") is True,
    )


def _callback(raw: Mapping[str, object]) -> AcceptedCallback:
    return AcceptedCallback(
        attempt_id=str(raw.get("attempt_id") or ""),
        axis=str(raw.get("axis") or ""),
        callback_id=str(raw.get("callback_id") or ""),
        kind=str(raw.get("kind") or ""),
        lane_id=str(raw.get("lane_id") or ""),
        operation_id=str(raw.get("operation_id") or ""),
        parent_operation_id=str(raw.get("parent_operation_id") or ""),
        payload_sha256=str(raw.get("payload_sha256") or ""),
        run_id=str(raw.get("run_id") or ""),
    )


def _refuse(reason: RecoveryReason) -> RecoveryDecision:
    return RecoveryDecision(RecoveryDisposition.REFUSE, reason)


def _receipt(identity: RecoveryIdentity) -> RecoveryReceipt:
    return RecoveryReceipt(identity, identity.sha256)


def _common_evidence_valid(snapshot: RecoverySnapshot) -> bool:
    return (
        snapshot.recovery_class in {"review-drive", "accepted-callback"}
        and bool(snapshot.root.owner_id)
        and bool(snapshot.root.operation_id)
        and bool(snapshot.root.run_id)
        and snapshot.root.revision >= 0
        and bool(snapshot.attempt.attempt_id)
        and snapshot.attempt.cycle > 0
        and snapshot.attempt.max_cycles > 0
        and bool(_HEAD_RE.fullmatch(snapshot.current_head))
        and bool(_HEAD_RE.fullmatch(snapshot.attempt.exact_head))
        and bool(_HEAD_RE.fullmatch(snapshot.gate.context_head))
        and bool(_SHA256_RE.fullmatch(snapshot.gate.sha256))
    )


def _live_review_evidence_valid(snapshot: RecoverySnapshot) -> bool:
    """Validate the narrower wait-only authority of an active reviewer.

    The pre-launch window predates a finalization attempt identity and does
    not require a root transition.  Its decision can only suppress a duplicate
    drive; it cannot create a recovery receipt or mutate lifecycle state.
    """

    return (
        snapshot.recovery_class in {"review-drive", "accepted-callback"}
        and bool(_HEAD_RE.fullmatch(snapshot.current_head))
        and bool(_HEAD_RE.fullmatch(snapshot.attempt.exact_head))
        and bool(_HEAD_RE.fullmatch(snapshot.gate.context_head))
        and bool(_SHA256_RE.fullmatch(snapshot.gate.sha256))
    )


def _live_review(snapshot: RecoverySnapshot) -> bool:
    return (
        snapshot.root.state in {"running", "awaiting-callback", "verifying"}
        and snapshot.gate.status in {"reviewing", "verifying"}
        and snapshot.attempt.status == "awaiting-callback"
        and snapshot.attempt.exact_head == snapshot.current_head
        and snapshot.gate.context_head == snapshot.current_head
        and not snapshot.accepted_callbacks
        and bool(snapshot.lanes)
        and all(
            lane.operation_id
            and lane.run_id
            and lane.lane_id
            and (
                (
                    lane.launch_in_progress
                    and lane.state in {"preflight", "starting", "running"}
                    and lane.ready_identity_exact
                )
                or (
                    lane.round_operation_id
                    and lane.round_run_id
                    and lane.state in {"awaiting-callback", "finalizing"}
                    and lane.round_state in _LIVE_ROUND_STATES
                    and lane.ready_identity_exact
                    and lane.process_alive
                )
            )
            for lane in snapshot.lanes
        )
    )


def classify_review_continuation(
    snapshot: RecoverySnapshot,
) -> RecoveryDecision:
    """Return one closed decision from immutable durable observations."""

    if snapshot.root.pending_effect:
        return _refuse(RecoveryReason.PENDING_EFFECT)
    if snapshot.effect_requires_replay:
        return _refuse(RecoveryReason.EFFECT_REPLAY_REQUIRED)
    if _live_review_evidence_valid(snapshot) and _live_review(snapshot):
        return RecoveryDecision(
            RecoveryDisposition.REVIEW_IN_PROGRESS,
            RecoveryReason.REVIEW_ACTIVE,
        )
    if not _common_evidence_valid(snapshot):
        return _refuse(RecoveryReason.MALFORMED_EVIDENCE)
    if snapshot.recovery_class == "review-drive":
        return _classify_review_drive(snapshot)
    return _classify_accepted_callback(snapshot)


def _classify_review_drive(snapshot: RecoverySnapshot) -> RecoveryDecision:
    if (
        snapshot.root.state != "attention-required"
        or snapshot.root.resume_state != "awaiting-callback"
        or snapshot.attention_status != "review-drive-failed"
    ):
        return _refuse(RecoveryReason.ATTENTION_NOT_RECOVERABLE)
    if (
        snapshot.gate.status != "changes-requested"
        or snapshot.attempt.status != "terminal"
    ):
        return _refuse(RecoveryReason.GATE_STATE_MISMATCH)
    if snapshot.attempt.cycle >= snapshot.attempt.max_cycles:
        return _refuse(RecoveryReason.REVIEW_CEILING_EXHAUSTED)
    resolution = snapshot.resolution
    if resolution is None:
        return _refuse(RecoveryReason.RESOLUTION_MISSING)
    if snapshot.current_head == resolution.reviewed_head:
        return _refuse(RecoveryReason.HEAD_UNCHANGED)
    if (
        not _HEAD_RE.fullmatch(resolution.reviewed_head)
        or not _HEAD_RE.fullmatch(resolution.current_head)
        or not _SHA256_RE.fullmatch(resolution.sha256)
        or snapshot.attempt.exact_head != resolution.reviewed_head
        or snapshot.gate.context_head != resolution.reviewed_head
        or snapshot.current_head != resolution.current_head
    ):
        return _refuse(RecoveryReason.RESOLUTION_IDENTITY_MISMATCH)
    verification = snapshot.verification
    if (
        verification is None
        or verification.status != "complete"
        or verification.head != snapshot.current_head
        or not _SHA256_RE.fullmatch(verification.receipt_sha256)
    ):
        return _refuse(RecoveryReason.VERIFICATION_IDENTITY_MISMATCH)
    identity = RecoveryIdentity(
        recovery_class="review-drive",
        owner_id=snapshot.root.owner_id,
        root_operation_id=snapshot.root.operation_id,
        root_run_id=snapshot.root.run_id,
        root_revision=snapshot.root.revision,
        attempt_id=snapshot.attempt.attempt_id,
        gate_sha256=snapshot.gate.sha256,
        authority_sha256=verification.receipt_sha256,
    )
    return RecoveryDecision(
        RecoveryDisposition.REVIEW_DRIVE_REARM,
        RecoveryReason.ELIGIBLE,
        _receipt(identity),
    )


def _classify_accepted_callback(
    snapshot: RecoverySnapshot,
) -> RecoveryDecision:
    if (
        snapshot.gate.status not in {"reviewing", "verifying"}
        or snapshot.attempt.status != "awaiting-callback"
        or snapshot.attempt.exact_head != snapshot.current_head
        or snapshot.gate.context_head != snapshot.current_head
    ):
        return _refuse(RecoveryReason.GATE_STATE_MISMATCH)
    if not snapshot.accepted_callbacks:
        return _refuse(RecoveryReason.CALLBACK_MISSING)
    if len(snapshot.accepted_callbacks) != 1:
        return _refuse(RecoveryReason.CALLBACK_AMBIGUOUS)
    callback = snapshot.accepted_callbacks[0]
    if not _SHA256_RE.fullmatch(callback.payload_sha256):
        return _refuse(RecoveryReason.MALFORMED_EVIDENCE)
    if callback.callback_id in snapshot.consumed_callback_ids:
        return _refuse(RecoveryReason.CALLBACK_ALREADY_CONSUMED)
    matching_lanes = [
        lane
        for lane in snapshot.lanes
        if lane.axis == callback.axis
        and lane.operation_id == callback.parent_operation_id
        and lane.lane_id == callback.lane_id
        and lane.round_operation_id == callback.operation_id
        and lane.round_run_id == callback.run_id
    ]
    if (
        callback.kind != "review"
        or callback.attempt_id != snapshot.attempt.attempt_id
        or len(matching_lanes) != 1
    ):
        return _refuse(RecoveryReason.CALLBACK_IDENTITY_MISMATCH)
    lane = matching_lanes[0]
    if (
        lane.state not in {"awaiting-callback", "finalizing"}
        or lane.round_state not in _LIVE_ROUND_STATES
        or not lane.ready_identity_exact
    ):
        return _refuse(RecoveryReason.CALLBACK_IDENTITY_MISMATCH)
    identity = RecoveryIdentity(
        recovery_class="accepted-callback",
        owner_id=snapshot.root.owner_id,
        root_operation_id=snapshot.root.operation_id,
        root_run_id=snapshot.root.run_id,
        root_revision=snapshot.root.revision,
        attempt_id=snapshot.attempt.attempt_id,
        gate_sha256=snapshot.gate.sha256,
        authority_sha256=callback.payload_sha256,
        lane_id=callback.lane_id,
        round_operation_id=callback.operation_id,
        round_run_id=callback.run_id,
        callback_id=callback.callback_id,
    )
    return RecoveryDecision(
        RecoveryDisposition.ACCEPTED_CALLBACK_INGEST,
        RecoveryReason.ELIGIBLE,
        _receipt(identity),
    )
