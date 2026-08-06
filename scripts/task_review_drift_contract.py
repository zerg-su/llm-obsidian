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
_FRESH_OPERATION_BINDING_PREFIX = (
    "Classified as an eligible repository-owned fresh-boundary operation-ID "
    "binding mechanism failure."
)
_POST_VERIFICATION_PREFIX = (
    "Classified as an eligible repository-owned post-verification review-drive "
    "mechanism failure."
)
_EXACT_LIVE_STATUS_PREFIX = (
    "Classified as an eligible repository-owned exact-live ownership "
    "status-adapter mechanism failure."
)
_POST_FRESH_PUBLICATION_PREFIX = (
    "Classified as an eligible repository-owned post-fresh-publication "
    "synchronization mechanism failure."
)
_FRESH_CHILD_PROGRESS_PREFIX = (
    "Classified as an eligible repository-owned fresh child-round "
    "lifecycle-progress synchronization failure."
)
_AUTHORIZATION_COMPATIBILITY_PREFIX = (
    "Classified as an eligible repository-owned typed authorization-compiler "
    "compatibility failure."
)
_FRESH_CHILD_PROGRESS_DECISION = (
    f"{_FRESH_CHILD_PROGRESS_PREFIX} Authorize one narrow regression-backed "
    "repair: accept only the same exact fresh child-round identities after "
    "monotonic advancement from awaiting-callback into verifying, finalizing, "
    "or terminal resource-free completion, while validating their persisted "
    "callback/provider/effect chain, unchanged parent identities and succeeded "
    "start-provider effects, zero replay counters, and no state regression. "
    "Preserve the prepared continuation, quarantine archive, and all receipts. "
    "After a clean exact-HEAD gate, authorize exactly one additional supported "
    "reconcile attempt. Do not relaunch or replace reviewers/providers, replay "
    "callbacks or effects, signal processes, touch cmux manually, edit gate/"
    "store by hand, or create any additional fresh lane."
)
_AUTHORIZATION_COMPATIBILITY_DECISION = (
    f"{_AUTHORIZATION_COMPATIBILITY_PREFIX} Authorize one narrow regression-"
    "backed compiler repair that accepts only the exact latest fresh child-"
    "round lifecycle-progress resolution, proves and preserves its complete "
    "chain to the prior post-fresh-publication authorization and the still-"
    "unused one-additional-reconcile grant, and rejects missing, reordered, "
    "ambiguous, or broadened decisions. After clean relevant and exact-HEAD "
    "gates, permit exactly the still-unused single supported reconcile. No "
    "reviewer/provider relaunch or replacement, callback/effect replay, "
    "signals, cmux/manual gate-store mutation, additional fresh lane, push, "
    "publish, tag, release, or reap."
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


@dataclass(frozen=True)
class PostVerificationReviewDriveAuthorization:
    supported_close: SupportedCloseRetirementAuthorization
    active_review_operation_id: str
    dispatch_operation_id: str
    fresh_binding_record_id: str
    fresh_binding_record_sha256: str
    authorization_record_id: str
    authorization_record_sha256: str


@dataclass(frozen=True)
class PostFreshPublicationSyncAuthorization:
    continuation: PostVerificationReviewDriveAuthorization
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


def _compile_supported_close_authorization(
    chain: list[DecisionRecord], index: int, worktree: Path
) -> SupportedCloseRetirementAuthorization | None:
    latest = chain[index]
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
    signal_indices = [
        signal_index
        for signal_index, record in enumerate(chain[:index])
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
        for record in chain[signal_index : index + 1]
    ):
        return None
    return SupportedCloseRetirementAuthorization(
        signal_free,
        (parent_match.group(1), parent_match.group(2)),
        latest.record_id,
        latest.sha256,
    )


def authorized_supported_close_retirement(
    latest: DecisionRecord, worktree: Path
) -> SupportedCloseRetirementAuthorization | None:
    """Compile one exact terminal supported-close compatibility grant."""

    try:
        chain = list(load_chain(worktree))
    except EscalationRecordError:
        return None
    if not chain or chain[-1].sha256 != latest.sha256:
        return None
    return _compile_supported_close_authorization(
        chain, len(chain) - 1, worktree
    )


def _compile_post_verification_review_drive(
    chain: list[DecisionRecord], index: int, worktree: Path
) -> PostVerificationReviewDriveAuthorization | None:
    latest = chain[index]
    attention = latest.payload
    decision = str(attention.get("decision") or "")
    required = (
        "crash/restart synchronization between the completed scoped-verification receipt, the pending fresh-review marker, and the exact tracked live dispatch provider",
        "Preserve the completed quarantine archive, terminal retained-parent and retained-round receipts, scoped-verification receipt, and existing provider ownership exactly",
        "zero-callback-replay, zero-provider-replay, and at-most-one-fresh-review regressions",
        "Do not manually edit gate/store state, signal or close the exact live provider, repeat cleanup or verification, ingest old callbacks, relaunch the dispatch provider",
        "resume once through the supported facade so the existing tracked provider continues from the completed verification boundary",
        "fail closed and re-escalate before any effect if exact identity or ownership cannot be proven",
    )
    if (
        latest.record_type != "resolution"
        or attention.get("status") != "resolved"
        or attention.get("category") != "mechanism-failure"
        or str(attention.get("worktree") or "") != str(worktree)
        or not decision.startswith(_POST_VERIFICATION_PREFIX)
        or any(fragment not in decision for fragment in required)
    ):
        return None
    supported_indices = [
        row_index
        for row_index, record in enumerate(chain[:index])
        if record.record_type == "resolution"
        and str(record.payload.get("decision") or "").startswith(
            _SUPPORTED_CLOSE_PREFIX
        )
    ]
    if not supported_indices:
        return None
    supported_index = supported_indices[-1]
    supported = _compile_supported_close_authorization(
        chain, supported_index, worktree
    )
    fresh_rows: list[tuple[int, DecisionRecord, re.Match[str]]] = []
    for row_index, record in enumerate(
        chain[supported_index + 1 : index], supported_index + 1
    ):
        record_decision = str(record.payload.get("decision") or "")
        if record.record_type != "resolution" or not record_decision.startswith(
            _FRESH_OPERATION_BINDING_PREFIX
        ):
            continue
        match = re.search(
            r"active review operation_id "
            r"([A-Za-z0-9][A-Za-z0-9._:-]{0,127}) while preserving the dispatch "
            r"operation_id ([A-Za-z0-9][A-Za-z0-9._:-]{0,127}) only as provenance",
            record_decision,
        )
        if match is not None:
            fresh_rows.append((row_index, record, match))
    if supported is None or len(fresh_rows) != 1:
        return None
    fresh_index, fresh, match = fresh_rows[0]
    active_review_id, dispatch_id = match.groups()
    if active_review_id == dispatch_id:
        return None
    fresh_decision = str(fresh.payload.get("decision") or "")
    fresh_required = (
        "zero-callback/provider-replay regressions",
        "Preserve the completed quarantine archive, parent cleanup receipts, and terminal child-round receipts exactly",
        "one previously authorized fresh Codex/Sol review",
    )
    scope = _decision_scope(attention)
    if (
        any(fragment not in fresh_decision for fragment in fresh_required)
        or any(
            record.record_type not in {"raise", "resolution"}
            or _decision_scope(record.payload) != scope
            for record in chain[supported_index : index + 1]
        )
        or fresh_index >= index
    ):
        return None
    return PostVerificationReviewDriveAuthorization(
        supported,
        active_review_id,
        dispatch_id,
        fresh.record_id,
        fresh.sha256,
        latest.record_id,
        latest.sha256,
    )


def _exact_live_status_decision_is_valid(
    latest: DecisionRecord, worktree: Path
) -> bool:
    attention = latest.payload
    decision = str(attention.get("decision") or "")
    required = (
        "The absent-owner replacement path is prohibited for this boundary",
        "one narrow local reversible TDD repair to the read-only Darwin process/pid status adapter",
        "when zero-signal liveness probing returns EPERM/unknown, report alive only if libproc proves the exact persisted PID/PGID identity unchanged",
        "proc_bsdinfo reports a running non-zombie process",
        "provider parent PID is the exact persisted supervisor PID",
        "both identities match the existing ownership receipt",
        "Preserve current behavior on non-Darwin platforms and for ordinary successful zero-signal probes",
        "no signal may be sent and no lifecycle/store/gate/provider/callback/review effect may occur during diagnosis or tests",
        "exactly one new supported post-verification recovery attempt on that same live dispatch executor",
        "Do not create a replacement generation, restart or relaunch any provider/reviewer",
        "Fail closed and re-escalate before any effect on identity/status drift",
    )
    return not (
        latest.record_type != "resolution"
        or attention.get("status") != "resolved"
        or attention.get("category") != "mechanism-failure"
        or str(attention.get("worktree") or "") != str(worktree)
        or not decision.startswith(_EXACT_LIVE_STATUS_PREFIX)
        or any(fragment not in decision for fragment in required)
    )


def _compile_authorized_post_verification_review_drive(
    chain: list[DecisionRecord], index: int, worktree: Path
) -> PostVerificationReviewDriveAuthorization | None:
    latest = chain[index]
    decision = str(latest.payload.get("decision") or "")
    if decision.startswith(_POST_VERIFICATION_PREFIX):
        return _compile_post_verification_review_drive(chain, index, worktree)
    if not _exact_live_status_decision_is_valid(latest, worktree):
        return None
    post_indices = [
        row_index
        for row_index, record in enumerate(chain[:index])
        if record.record_type == "resolution"
        and str(record.payload.get("decision") or "").startswith(
            _POST_VERIFICATION_PREFIX
        )
    ]
    if len(post_indices) != 1:
        return None
    post_index = post_indices[0]
    prior = _compile_post_verification_review_drive(
        chain, post_index, worktree
    )
    scope = _decision_scope(latest.payload)
    if prior is None or any(
        record.record_type not in {"raise", "resolution"}
        or _decision_scope(record.payload) != scope
        for record in chain[post_index : index + 1]
    ):
        return None
    return PostVerificationReviewDriveAuthorization(
        prior.supported_close,
        prior.active_review_operation_id,
        prior.dispatch_operation_id,
        prior.fresh_binding_record_id,
        prior.fresh_binding_record_sha256,
        latest.record_id,
        latest.sha256,
    )


def authorized_post_verification_review_drive(
    latest: DecisionRecord, worktree: Path
) -> PostVerificationReviewDriveAuthorization | None:
    """Compile the exact live-provider continuation grant and its provenance."""

    try:
        chain = list(load_chain(worktree))
    except EscalationRecordError:
        return None
    if not chain or chain[-1].sha256 != latest.sha256:
        return None
    return _compile_authorized_post_verification_review_drive(
        chain, len(chain) - 1, worktree
    )


def _compile_post_fresh_publication_sync(
    chain: list[DecisionRecord], index: int, worktree: Path
) -> PostFreshPublicationSyncAuthorization | None:
    latest = chain[index]
    decision = str(latest.payload.get("decision") or "")
    required = (
        "validates the prepared continuation receipt, exact fresh gate identity, fresh_reevaluation_used=true",
        "already-created Codex/Sol parent/round identities with succeeded start-provider effects",
        "complete only the missing gate/progress/final-marker synchronization and resume callback waiting for those existing fresh lanes",
        "Preserve quarantine/archive/retained receipts and all existing provider effects",
        "Do not retry the consumed resume, relaunch or replace any reviewer/provider, repeat verification, replay callbacks/provider effects, signal processes, touch cmux surfaces, manually edit gate/store",
        "Fail closed on any identity, receipt, resource, or effect drift",
    )
    attention = latest.payload
    if (
        latest.record_type != "resolution"
        or attention.get("status") != "resolved"
        or attention.get("category") != "mechanism-failure"
        or str(attention.get("worktree") or "") != str(worktree)
        or not decision.startswith(_POST_FRESH_PUBLICATION_PREFIX)
        or any(fragment not in decision for fragment in required)
    ):
        return None
    exact_live_indices = [
        row_index
        for row_index, record in enumerate(chain[:index])
        if record.record_type == "resolution"
        and str(record.payload.get("decision") or "").startswith(
            _EXACT_LIVE_STATUS_PREFIX
        )
    ]
    if len(exact_live_indices) != 1:
        return None
    exact_live_index = exact_live_indices[0]
    continuation = _compile_authorized_post_verification_review_drive(
        chain, exact_live_index, worktree
    )
    scope = _decision_scope(attention)
    if continuation is None or any(
        record.record_type not in {"raise", "resolution"}
        or _decision_scope(record.payload) != scope
        for record in chain[exact_live_index : index + 1]
    ):
        return None
    return PostFreshPublicationSyncAuthorization(
        continuation,
        latest.record_id,
        latest.sha256,
    )


def _compile_fresh_progress_compatibility(
    chain: list[DecisionRecord], index: int, worktree: Path
) -> PostFreshPublicationSyncAuthorization | None:
    """Compile only the exact two-step extension of the publication grant."""

    latest = chain[index]
    attention = latest.payload
    decision = str(attention.get("decision") or "")
    if (
        index < 4
        or latest.record_type != "resolution"
        or attention.get("status") != "resolved"
        or attention.get("category") != "mechanism-failure"
        or str(attention.get("worktree") or "") != str(worktree)
        or decision != _AUTHORIZATION_COMPATIBILITY_DECISION
    ):
        return None

    post_index = index - 4
    progress_raise = chain[index - 3]
    progress = chain[index - 2]
    compatibility_raise = chain[index - 1]
    if (
        chain[post_index].record_type != "resolution"
        or progress_raise.record_type != "raise"
        or progress.record_type != "resolution"
        or str(progress.payload.get("decision") or "")
        != _FRESH_CHILD_PROGRESS_DECISION
        or compatibility_raise.record_type != "raise"
    ):
        return None

    post_indices = [
        row_index
        for row_index, record in enumerate(chain[:index])
        if record.record_type == "resolution"
        and str(record.payload.get("decision") or "").startswith(
            _POST_FRESH_PUBLICATION_PREFIX
        )
    ]
    progress_indices = [
        row_index
        for row_index, record in enumerate(chain[:index])
        if record.record_type == "resolution"
        and str(record.payload.get("decision") or "").startswith(
            _FRESH_CHILD_PROGRESS_PREFIX
        )
    ]
    compatibility_indices = [
        row_index
        for row_index, record in enumerate(chain[: index + 1])
        if record.record_type == "resolution"
        and str(record.payload.get("decision") or "").startswith(
            _AUTHORIZATION_COMPATIBILITY_PREFIX
        )
    ]
    if (
        post_indices != [post_index]
        or progress_indices != [index - 2]
        or compatibility_indices != [index]
    ):
        return None

    prior = _compile_post_fresh_publication_sync(
        chain, post_index, worktree
    )
    scope = _decision_scope(attention)
    if prior is None or any(
        _decision_scope(record.payload) != scope
        for record in chain[post_index : index + 1]
    ):
        return None
    return PostFreshPublicationSyncAuthorization(
        prior.continuation,
        latest.record_id,
        latest.sha256,
    )


def authorized_post_fresh_publication_sync(
    latest: DecisionRecord, worktree: Path
) -> PostFreshPublicationSyncAuthorization | None:
    """Compile the one grant to finish a partially published fresh review."""

    try:
        chain = list(load_chain(worktree))
    except EscalationRecordError:
        return None
    if not chain or chain[-1].sha256 != latest.sha256:
        return None
    index = len(chain) - 1
    decision = str(latest.payload.get("decision") or "")
    if decision.startswith(_POST_FRESH_PUBLICATION_PREFIX):
        return _compile_post_fresh_publication_sync(
            chain, index, worktree
        )
    if decision.startswith(_AUTHORIZATION_COMPATIBILITY_PREFIX):
        return _compile_fresh_progress_compatibility(
            chain, index, worktree
        )
    return None
