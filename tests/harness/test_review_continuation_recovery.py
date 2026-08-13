#!/usr/bin/env python3
"""Pure policy regressions for the two RC6.4 recovery classes."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.review_continuation_recovery import (  # noqa: E402
    AcceptedCallback,
    RecoveryDisposition,
    RecoveryReason,
    RecoverySnapshot,
    classify_review_continuation,
)


FIXTURES = Path(__file__).with_name("fixtures") / "review-continuation"


def check(label: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def fixture(name: str) -> RecoverySnapshot:
    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return RecoverySnapshot.from_mapping(raw)


drive = fixture("review-drive-failed-after-reverify.json")
drive_decision = classify_review_continuation(drive)
check(
    "captured review-drive failure is eligible only after changed-HEAD verification",
    drive_decision.disposition is RecoveryDisposition.REVIEW_DRIVE_REARM
    and drive_decision.reason is RecoveryReason.ELIGIBLE
    and drive_decision.receipt is not None
    and drive_decision.receipt.identity.recovery_class == "review-drive"
    and drive_decision.receipt.identity.root_revision == 13
    and drive_decision.receipt.identity.attempt_id
    == "cf7d4579-0ffd-5191-bca0-ab95241b677a",
    drive_decision,
)

callback = fixture("accepted-callback-pending-ingestion.json")
callback_decision = classify_review_continuation(callback)
check(
    "captured accepted callback is eligible for gate ingestion without replay",
    callback_decision.disposition
    is RecoveryDisposition.ACCEPTED_CALLBACK_INGEST
    and callback_decision.reason is RecoveryReason.ELIGIBLE
    and callback_decision.receipt is not None
    and callback_decision.receipt.identity.callback_id
    == "review-0e2f254b321ab0d29439bc82"
    and callback_decision.receipt.identity.authority_sha256
    == "0e2f254b321ab0d29439bc826429aab2f5e103e978c1f223f3953b086007ffd4",
    callback_decision,
)


def refused(label: str, snapshot: RecoverySnapshot, reason: RecoveryReason) -> None:
    decision = classify_review_continuation(snapshot)
    check(
        label,
        decision.disposition is RecoveryDisposition.REFUSE
        and decision.reason is reason
        and decision.receipt is None,
        decision,
    )


refused(
    "a pending effect cannot be replayed",
    replace(drive, root=replace(drive.root, pending_effect="provider-input")),
    RecoveryReason.PENDING_EFFECT,
)
refused(
    "a missing resolution has no continuation authority",
    replace(drive, resolution=None),
    RecoveryReason.RESOLUTION_MISSING,
)
refused(
    "an unchanged HEAD is not a post-resolution continuation",
    replace(drive, current_head=drive.resolution.reviewed_head),
    RecoveryReason.HEAD_UNCHANGED,
)
refused(
    "stale verification evidence fails closed",
    replace(
        drive,
        verification=replace(drive.verification, head=drive.resolution.reviewed_head),
    ),
    RecoveryReason.VERIFICATION_IDENTITY_MISMATCH,
)
refused(
    "an exhausted finalization ceiling performs no recovery",
    replace(drive, attempt=replace(drive.attempt, cycle=5, max_cycles=5)),
    RecoveryReason.REVIEW_CEILING_EXHAUSTED,
)

accepted = callback.accepted_callbacks[0]
refused(
    "a wrong callback run cannot be ingested",
    replace(
        callback,
        accepted_callbacks=(replace(accepted, run_id="wrong-run"),),
    ),
    RecoveryReason.CALLBACK_IDENTITY_MISMATCH,
)
refused(
    "conflicting accepted callbacks are ambiguous",
    replace(
        callback,
        accepted_callbacks=(
            accepted,
            replace(
                accepted,
                callback_id="review-conflicting",
                payload_sha256="f" * 64,
            ),
        ),
    ),
    RecoveryReason.CALLBACK_AMBIGUOUS,
)
refused(
    "a callback already consumed by the gate never retries",
    replace(callback, consumed_callback_ids=frozenset({accepted.callback_id})),
    RecoveryReason.CALLBACK_ALREADY_CONSUMED,
)
refused(
    "provider or callback replay requirements fail closed",
    replace(callback, effect_requires_replay=True),
    RecoveryReason.EFFECT_REPLAY_REQUIRED,
)

live = replace(
    callback,
    root=replace(
        callback.root,
        state="awaiting-callback",
        resume_state="",
    ),
    accepted_callbacks=(),
    lanes=(
        replace(
            callback.lanes[0],
            round_state="running",
            ready_identity_exact=True,
            process_alive=True,
        ),
    ),
)
live_decision = classify_review_continuation(live)
check(
    "the same authority recognizes an exact durable review in progress",
    live_decision.disposition is RecoveryDisposition.REVIEW_IN_PROGRESS
    and live_decision.reason is RecoveryReason.REVIEW_ACTIVE
    and live_decision.receipt is None,
    live_decision,
)

for status in (
    "wiki-summary-invalid",
    "callback-wake-effect-uncertain",
    "pipeline-verification-effect-uncertain",
):
    refused(
        f"{status} retains restart-only semantics",
        replace(drive, attention_status=status),
        RecoveryReason.ATTENTION_NOT_RECOVERABLE,
    )

malformed = replace(
    callback,
    accepted_callbacks=(
        AcceptedCallback(
            **{
                **accepted.__dict__,
                "payload_sha256": "not-a-digest",
            }
        ),
    ),
)
refused(
    "malformed callback authority cannot create a receipt",
    malformed,
    RecoveryReason.MALFORMED_EVIDENCE,
)

print("review continuation recovery policy tests passed")
