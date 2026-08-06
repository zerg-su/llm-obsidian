"""Exact dual-provenance policy for the retained fresh-review boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from task_escalation_records import DecisionRecord


COORDINATOR_PROVENANCE_OPERATION_ID = "75ff063d-d388-46a7-915d-0eed20392da4"
COORDINATOR_PROVENANCE_RECORD_ID = "resolution-c18780860b35bf087f1ab9c5c44d9b67"
COORDINATOR_PROVENANCE_SHA256 = (
    "19b7353968b7b7ee91043a604f10a4f7471b99a6b268a643a2f24cf41285aa4b"
)
SCOPED_VERIFICATION_OPERATION_ID = (
    "ad97826c-0651-4014-a113-72518e6fceea-verify-23e835bbe5984523"
)
SCOPED_VERIFICATION_RECEIPT_SHA256 = (
    "c2037564a8a77f384fe012df4aa009882a124ad1297ab9f218cd90106278638b"
)
FRESH_BOUNDARY_AUTHORIZATION_FIELDS = frozenset(
    {
        "schema_version",
        "operation_id",
        "dispatch_operation_id",
        "kind",
        "previous_context_sha256",
        "next_context_sha256",
        "reason",
        "authorization_provenance",
        "verification_operation_id",
        "verification_receipt_sha256",
        "status",
    }
)
ZERO_EFFECT_FIELDS = (
    "os_signals_sent",
    "cmux_signals_sent",
    "callback_effects_replayed",
    "provider_effects_replayed",
)
FRESH_BOUNDARY_PROVENANCE_PREFIX = (
    "Classified as an eligible repository-owned fresh-boundary authorization "
    "provenance compatibility failure."
)
_FRESH_BOUNDARY_PROVENANCE_DECISION = (
    f"{FRESH_BOUNDARY_PROVENANCE_PREFIX} Authorize one narrow regression-backed "
    "fail-closed repair: preserve the immutable fresh-boundary artifact byte-"
    "for-byte; validate its exact coordinator escalation provenance "
    "verification_operation_id 75ff063d-d388-46a7-915d-0eed20392da4 and "
    "verification_receipt_sha256 "
    "19b7353968b7b7ee91043a604f10a4f7471b99a6b268a643a2f24cf41285aa4b; "
    "separately validate the exact bound scoped-verification operation "
    "ad97826c-0651-4014-a113-72518e6fceea-verify-23e835bbe5984523 and receipt "
    "digest c2037564a8a77f384fe012df4aa009882a124ad1297ab9f218cd90106278638b "
    "through the existing continuation/dispatch/gate identity chain; and accept "
    "only when both provenance layers, record/file digests, operation, dispatch, "
    "context, kind, reason, unchanged gate/progress state, clean descendant "
    "ancestry, and zero replay/signal counters are exact and unambiguous. Reject "
    "any missing, rewritten, mismatched, duplicated, unrelated, or broadened "
    "provenance. Add focused positive/negative and idempotency regressions, run "
    "clean focused/full/coverage/quality plus a fresh exact-HEAD release-final "
    "gate, then authorize exactly one replacement supported reconcile because "
    "the consumed attempt failed before post-fresh-publication-sync receipt or "
    "any lifecycle/provider/callback/reviewer effect. Preserve all historical "
    "evidence and prior prohibitions: no reviewer/provider relaunch, callback/"
    "effect replay, signals, cmux or manual store/gate edits, push, publish, tag, "
    "release, or reap. Escalate on any further identity, ownership, or lifecycle "
    "drift."
)
_EXACT_PROVENANCE_TAIL = (
    (
        "resolution-b28b1e20822edf26a9c4ffa399abf305",
        "resolution",
        "0f4c34f780989a8921741390b872aa0d5b0b0ecfb2fbcc7ba4f33a8520074ce9",
        "2f0718a4-fe30-4f97-97a6-1c5faa3fccd6",
        "98d4e69168e0a7b4cc3c68b815c74425eb5ce8f544432894e29b5a375383e079",
    ),
    (
        "50949589-9803-4b88-9e2c-e86e89da73a9",
        "raise",
        "a3aeac897b3db2c29f5a8d82bf9a140a689ad7924dffcfc113977388c5e95e26",
        "resolution-b28b1e20822edf26a9c4ffa399abf305",
        "0f4c34f780989a8921741390b872aa0d5b0b0ecfb2fbcc7ba4f33a8520074ce9",
    ),
    (
        "resolution-133515ccf480a8d5266eb125582f7f5b",
        "resolution",
        "85416c36635bb3d4cfac65de0463d83b96c61744b32a825a97c6306dc0744479",
        "50949589-9803-4b88-9e2c-e86e89da73a9",
        "a3aeac897b3db2c29f5a8d82bf9a140a689ad7924dffcfc113977388c5e95e26",
    ),
)


def coordinator_provenance_is_exact(
    authorization: Mapping[str, object], continuation: Mapping[str, object]
) -> bool:
    """Bind immutable coordinator provenance to its scoped verification."""

    return bool(
        set(authorization) == FRESH_BOUNDARY_AUTHORIZATION_FIELDS
        and authorization.get("authorization_provenance")
        == "coordinator-approved"
        and authorization.get("verification_operation_id")
        == COORDINATOR_PROVENANCE_OPERATION_ID
        and authorization.get("verification_receipt_sha256")
        == COORDINATOR_PROVENANCE_SHA256
        and continuation.get("authorization_record_id")
        == COORDINATOR_PROVENANCE_RECORD_ID
        and continuation.get("authorization_record_sha256")
        == COORDINATOR_PROVENANCE_SHA256
        and continuation.get("source_verification_operation_id")
        == SCOPED_VERIFICATION_OPERATION_ID
        and continuation.get("source_verification_receipt_sha256")
        == SCOPED_VERIFICATION_RECEIPT_SHA256
        and all(continuation.get(field) == 0 for field in ZERO_EFFECT_FIELDS)
    )


def provenance_tail_is_exact(
    chain: list[DecisionRecord], index: int, worktree: Path
) -> bool:
    """Accept only the immediate three-record replacement-reconcile grant."""

    if index < 2:
        return False
    tail = chain[index - 2 : index + 1]
    expected_ids = tuple(row[0] for row in _EXACT_PROVENANCE_TAIL)
    if tuple(record.record_id for record in tail) != expected_ids or any(
        sum(record.record_id == record_id for record in chain) != 1
        for record_id in expected_ids
    ):
        return False
    for record, expected in zip(tail, _EXACT_PROVENANCE_TAIL, strict=True):
        if (
            (
                record.record_id,
                record.record_type,
                record.sha256,
                record.previous_record_id,
                record.previous_record_sha256,
            )
            != expected
        ):
            return False
    latest = tail[-1]
    scope = {
        key: latest.payload.get(key)
        for key in ("category", "worktree", "task_name", "task_surface")
    }
    return bool(
        latest.payload.get("status") == "resolved"
        and latest.payload.get("category") == "mechanism-failure"
        and str(latest.payload.get("worktree") or "") == str(worktree)
        and str(latest.payload.get("decision") or "")
        == _FRESH_BOUNDARY_PROVENANCE_DECISION
        and all(
            {
                key: record.payload.get(key)
                for key in ("category", "worktree", "task_name", "task_surface")
            }
            == scope
            for record in tail
        )
        and [
            row_index
            for row_index, record in enumerate(chain)
            if record.record_type == "resolution"
            and str(record.payload.get("decision") or "").startswith(
                FRESH_BOUNDARY_PROVENANCE_PREFIX
            )
        ]
        == [index]
    )
