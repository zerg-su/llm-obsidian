"""Exact coordinator authorization for stale review drift quarantine."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from task_escalation_records import DecisionRecord, EscalationRecordError, load_chain


_ANCHOR_PREFIX = (
    "Classified as an eligible repository-owned stale-review-attempt "
    "mechanism failure."
)
_PATCH_PREFIX = (
    "Choose boundary A. Classified as an eligible repository-owned "
    "stale-review quarantine mechanism gap."
)
_SIGNAL_FREE_PREFIX = (
    "Classified as an eligible repository-owned stale OS-ownership drift "
    "mechanism failure."
)
_SUPPORTED_CLOSE_PREFIX = (
    "Classified as an eligible repository-owned supported-close "
    "terminal-state compatibility mechanism failure."
)


@dataclass(frozen=True)
class DriftQuarantineAuthorization:
    accepted_callback_id: str
    accepted_callback_sha256: str
    artifact_callback_id: str
    artifact_callback_sha256: str
    descendant_base_head: str
    anchor_record_id: str
    anchor_record_sha256: str
    authorization_record_id: str
    authorization_record_sha256: str


@dataclass(frozen=True)
class SignalFreeRetirementAuthorization:
    drift: DriftQuarantineAuthorization
    authorization_record_id: str
    authorization_record_sha256: str


@dataclass(frozen=True)
class SupportedCloseRetirementAuthorization:
    signal_free: SignalFreeRetirementAuthorization
    parent_operation_ids: tuple[str, str]
    authorization_record_id: str
    authorization_record_sha256: str


def _decision_scope(payload: dict[str, Any]) -> dict[str, object]:
    return {
        key: payload.get(key)
        for key in ("category", "worktree", "task_name", "task_surface")
    }


def _anchor_identities(decision: str) -> tuple[str, str, str, str] | None:
    match = re.search(
        r"accepted receipt identity "
        r"([A-Za-z0-9][A-Za-z0-9._:-]{0,127}) with payload digest "
        r"([0-9a-f]{64}) and the mismatching callback-file identity "
        r"([A-Za-z0-9][A-Za-z0-9._:-]{0,127}) with payload digest "
        r"([0-9a-f]{64})",
        decision,
    )
    if match is None:
        return None
    identities = match.groups()
    if identities[0] == identities[2] or identities[1] == identities[3]:
        return None
    return identities


def _compile_drift_authorization(
    chain: list[DecisionRecord], index: int, worktree: Path
) -> DriftQuarantineAuthorization | None:
    latest = chain[index]
    attention = latest.payload
    decision = str(attention.get("decision") or "")
    base_match = re.search(
        r"new clean product HEAD descended from ([0-9a-f]{40,64})",
        decision,
    )
    if (
        latest.record_type != "resolution"
        or attention.get("status") != "resolved"
        or attention.get("category") != "mechanism-failure"
        or str(attention.get("worktree") or "") != str(worktree)
        or not decision.startswith(_PATCH_PREFIX)
        or "typed, fail-closed drift-evidence quarantine transition"
        not in decision
        or "no callback may be ingested, reconstructed, rebound, replayed, or reinterpreted"
        not in decision
        or "reject matching callbacks, ambiguous identity, live unrelated ownership, missing receipts, partial cleanup, or any attempt to reuse old effects"
        not in decision
        or "launch exactly one fresh single-model Codex/Sol deep review"
        not in decision
        or base_match is None
    ):
        return None
    anchors = [
        (index, record, _anchor_identities(str(record.payload.get("decision") or "")))
        for index, record in enumerate(chain[:index])
        if record.record_type == "resolution"
        and str(record.payload.get("decision") or "").startswith(_ANCHOR_PREFIX)
    ]
    if len(anchors) != 1 or anchors[0][2] is None:
        return None
    anchor_index, anchor, identities = anchors[0]
    assert identities is not None
    scope = _decision_scope(attention)
    if any(
        record.record_type not in {"raise", "resolution"}
        or _decision_scope(record.payload) != scope
        for record in chain[anchor_index : index + 1]
    ):
        return None
    return DriftQuarantineAuthorization(
        *identities,
        base_match.group(1),
        anchor.record_id,
        anchor.sha256,
        latest.record_id,
        latest.sha256,
    )


def authorized_drift_quarantine(
    latest: DecisionRecord, worktree: Path
) -> DriftQuarantineAuthorization | None:
    """Compile one exact coordinator chain into drift-quarantine authority."""

    try:
        chain = load_chain(worktree)
    except EscalationRecordError:
        return None
    if not chain or chain[-1].sha256 != latest.sha256:
        return None
    return _compile_drift_authorization(chain, len(chain) - 1, worktree)


def _compile_signal_free_authorization(
    chain: list[DecisionRecord], index: int, worktree: Path
) -> SignalFreeRetirementAuthorization | None:
    latest = chain[index]
    if not _signal_free_decision_is_valid(latest, worktree):
        return None
    attention = latest.payload
    patch_indices = [
        patch_index
        for patch_index, record in enumerate(chain[:index])
        if record.record_type == "resolution"
        and str(record.payload.get("decision") or "").startswith(_PATCH_PREFIX)
    ]
    if len(patch_indices) != 1:
        return None
    drift = _compile_drift_authorization(chain, patch_indices[0], worktree)
    if drift is None:
        return None
    scope = _decision_scope(attention)
    if any(
        record.record_type not in {"raise", "resolution"}
        or _decision_scope(record.payload) != scope
        for record in chain[patch_indices[0] : index + 1]
    ):
        return None
    return SignalFreeRetirementAuthorization(
        drift,
        latest.record_id,
        latest.sha256,
    )


def _signal_free_decision_is_valid(
    latest: DecisionRecord, worktree: Path
) -> bool:
    attention = latest.payload
    decision = str(attention.get("decision") or "")
    required = (
        "one narrow code-owned, signal-free recovery path",
        "must send no OS/cmux/provider signal",
        "Only after read-only inventory proves no exact owned live resource",
        "reject any actually live exact match, any PID/PGID or supervisor reuse, ambiguous ownership, unrelated ownership, partial identity evidence, missing archive, archive tampering, or gate/progress drift",
        "exactly one fresh single-model Codex/Sol deep review",
    )
    return not (
        latest.record_type != "resolution"
        or attention.get("status") != "resolved"
        or attention.get("category") != "mechanism-failure"
        or str(attention.get("worktree") or "") != str(worktree)
        or not decision.startswith(_SIGNAL_FREE_PREFIX)
        or any(fragment not in decision for fragment in required)
    )


def authorized_signal_free_retirement(
    latest: DecisionRecord, worktree: Path
) -> SignalFreeRetirementAuthorization | None:
    """Compile the exact follow-up grant for signal-free stale retirement."""

    try:
        chain = list(load_chain(worktree))
    except EscalationRecordError:
        return None
    if not chain or chain[-1].sha256 != latest.sha256:
        return None
    return _compile_signal_free_authorization(
        chain, len(chain) - 1, worktree
    )


def authorized_supported_close_retirement(
    latest: DecisionRecord, worktree: Path
) -> SupportedCloseRetirementAuthorization | None:
    """Compile one exact terminal supported-close compatibility grant."""

    attention = latest.payload
    decision = str(attention.get("decision") or "")
    required = (
        "one narrow code-owned lifecycle repair",
        "when each is terminal cancelled, has empty OwnedResources, no pending effect, and a matching succeeded request-exit receipt produced by the supported harness close path",
        "atomically project the corresponding retained round children terminal/resource-free",
        "keep all signal and replay counters zero",
        "reject any live, ambiguous, partial, non-terminal, receipt-mismatched, archive-drifted, or unrelated state",
        "previously authorized exactly one fresh single-model Codex/Sol deep review",
    )
    parent_match = re.search(
        r"exact retained parent identities "
        r"([A-Za-z0-9][A-Za-z0-9._:-]{0,127}) and "
        r"([A-Za-z0-9][A-Za-z0-9._:-]{0,127}) when each",
        decision,
    )
    if (
        latest.record_type != "resolution"
        or attention.get("status") != "resolved"
        or attention.get("category") != "mechanism-failure"
        or str(attention.get("worktree") or "") != str(worktree)
        or not decision.startswith(_SUPPORTED_CLOSE_PREFIX)
        or any(fragment not in decision for fragment in required)
        or parent_match is None
        or parent_match.group(1) == parent_match.group(2)
    ):
        return None
    try:
        chain = list(load_chain(worktree))
    except EscalationRecordError:
        return None
    if not chain or chain[-1].sha256 != latest.sha256:
        return None
    signal_indices = [
        index
        for index, record in enumerate(chain[:-1])
        if record.record_type == "resolution"
        and str(record.payload.get("decision") or "").startswith(
            _SIGNAL_FREE_PREFIX
        )
    ]
    if not signal_indices:
        return None
    signal_index = signal_indices[-1]
    signal_free = _compile_signal_free_authorization(
        chain, signal_index, worktree
    )
    if signal_free is None:
        patch_indices = [
            index
            for index, record in enumerate(chain[:signal_index])
            if record.record_type == "resolution"
            and str(record.payload.get("decision") or "").startswith(
                _PATCH_PREFIX
            )
        ]
        signal_record = chain[signal_index]
        prior = chain[signal_index - 1] if signal_index else None
        drift = (
            _compile_drift_authorization(
                chain, patch_indices[0], worktree
            )
            if len(patch_indices) == 1
            else None
        )
        if (
            drift is None
            or prior is None
            or prior.record_type != "raise"
            or not _signal_free_decision_is_valid(signal_record, worktree)
            or signal_record.previous_record_id != prior.record_id
            or signal_record.previous_record_sha256 != prior.sha256
            or _decision_scope(signal_record.payload)
            != _decision_scope(prior.payload)
            or signal_record.payload.get("id") != prior.payload.get("id")
        ):
            return None
        signal_free = SignalFreeRetirementAuthorization(
            drift,
            signal_record.record_id,
            signal_record.sha256,
        )
    scope = _decision_scope(attention)
    if any(
        record.record_type not in {"raise", "resolution"}
        or _decision_scope(record.payload) != scope
        for record in chain[signal_index :]
    ):
        return None
    return SupportedCloseRetirementAuthorization(
        signal_free,
        (parent_match.group(1), parent_match.group(2)),
        latest.record_id,
        latest.sha256,
    )
