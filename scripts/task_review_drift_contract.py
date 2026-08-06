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


def authorized_drift_quarantine(
    latest: DecisionRecord, worktree: Path
) -> DriftQuarantineAuthorization | None:
    """Compile one exact coordinator chain into drift-quarantine authority."""

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
    try:
        chain = load_chain(worktree)
    except EscalationRecordError:
        return None
    if not chain or chain[-1].sha256 != latest.sha256:
        return None
    anchors = [
        (index, record, _anchor_identities(str(record.payload.get("decision") or "")))
        for index, record in enumerate(chain[:-1])
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
        for record in chain[anchor_index:]
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
