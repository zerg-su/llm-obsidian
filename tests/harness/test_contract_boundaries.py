#!/usr/bin/env python3
"""Cheap fail-closed coverage for persisted harness contract boundaries."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import (
    AttentionReason,
    CallbackEnvelope,
    CapabilityReport,
    ContextPacketManifest,
    ContractError,
    EffectOutcome,
    OperationRecord,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
    TransitionResult,
    VerificationEvidence,
    operation_record_from_dict,
    to_dict,
)


def rejected(label: str, call: object) -> None:
    try:
        call()  # type: ignore[operator]
    except ContractError:
        print(f"OK   {label}")
    else:
        raise AssertionError(label)


DIGEST = "a" * 64
ROUTE = RuntimeRoute("codex", "gpt-5.6-sol", "high", "executor", DIGEST)
SPEC = OperationSpec(
    "contract-operation",
    "contract-key",
    "dispatch",
    "contract-owner",
    ROUTE,
    "packets/contract.json",
    "scoped",
)
RECORD = OperationRecord(SPEC, "running", 0, "contract-lane", "contract-run")

rejected(
    "bounded identifiers reject empty values",
    lambda: RuntimeRoute("codex", "", "high", "executor", DIGEST),
)
rejected(
    "owner-relative paths reject empty values",
    lambda: replace(SPEC, context_manifest=""),
)
rejected(
    "callback payload rejects non-JSON values",
    lambda: CallbackEnvelope(
        "callback-json",
        SPEC.operation_id,
        RECORD.run_id,
        "result",
        {"invalid": {"set"}},
        DIGEST,
    ),
)

for label, call in (
    (
        "packet metadata rejects negative byte counts",
        lambda: ContextPacketManifest("packet", SPEC.operation_id, (), DIGEST, -1),
    ),
    (
        "packet manifests reject duplicate normalized files",
        lambda: ContextPacketManifest(
            "packet", SPEC.operation_id, ("a.md", "a.md"), DIGEST, 1
        ),
    ),
    (
        "owned resources reject negative process identifiers",
        lambda: OwnedResources(process_group=-1),
    ),
    (
        "process identity requires a live process group",
        lambda: OwnedResources(process_identity=DIGEST),
    ),
    (
        "supervisor identity requires a live supervisor pid",
        lambda: OwnedResources(supervisor_identity=DIGEST),
    ),
    (
        "operation records reject negative revisions",
        lambda: replace(RECORD, revision=-1),
    ),
    (
        "operation records reject invalid persisted budgets",
        lambda: replace(RECORD, attempt_limit=0),
    ),
    (
        "resume state is limited to attention-required",
        lambda: replace(RECORD, resume_state="running"),
    ),
    (
        "effect identity cannot exist without an outcome",
        lambda: replace(RECORD, effect_id="effect"),
    ),
    (
        "pending effect identity must agree",
        lambda: replace(
            RECORD,
            pending_effect="effect-a",
            effect_id="effect-b",
            effect_outcome=EffectOutcome.PENDING,
        ),
    ),
    (
        "resolved effect cannot remain pending",
        lambda: replace(
            RECORD,
            pending_effect="effect",
            effect_id="effect",
            effect_outcome=EffectOutcome.SUCCEEDED,
        ),
    ),
    (
        "accepted callback identity must be complete",
        lambda: replace(RECORD, accepted_callback_id="callback"),
    ),
    (
        "compatible capability reports cannot carry a failure reason",
        lambda: CapabilityReport(
            ROUTE, True, (), AttentionReason.CAPABILITY_MISMATCH
        ),
    ),
    (
        "incompatible capability reports require a reason",
        lambda: CapabilityReport(ROUTE, False, ()),
    ),
    (
        "callback envelopes reject unknown schema versions",
        lambda: CallbackEnvelope(
            "callback-schema",
            SPEC.operation_id,
            RECORD.run_id,
            "result",
            {},
            hashlib.sha256(b"{}").hexdigest(),
            schema_version=2,
        ),
    ),
):
    rejected(label, call)

large_payload = {"value": "x" * CallbackEnvelope.MAX_PAYLOAD_BYTES}
large_encoded = json.dumps(
    large_payload, sort_keys=True, separators=(",", ":")
).encode()
rejected(
    "callback envelopes enforce the payload size cap",
    lambda: CallbackEnvelope(
        "callback-large",
        SPEC.operation_id,
        RECORD.run_id,
        "result",
        large_payload,
        hashlib.sha256(large_encoded).hexdigest(),
    ),
)
rejected(
    "callback envelopes bind the canonical payload digest",
    lambda: CallbackEnvelope(
        "callback-digest",
        SPEC.operation_id,
        RECORD.run_id,
        "result",
        {},
        DIGEST,
    ),
)

valid_evidence = VerificationEvidence(
    "scoped",
    DIGEST,
    "b" * 40,
    "verify-command",
    ".",
    0,
    "2026-08-02T00:00:00Z",
    "2026-08-02T00:00:01Z",
    "outputs/verify.txt",
)
for label, call in (
    (
        "verification evidence requires an exact Git object id",
        lambda: replace(valid_evidence, head_sha="short"),
    ),
    (
        "verification evidence requires an integer exit code",
        lambda: replace(valid_evidence, exit_code="0"),
    ),
    (
        "verification evidence requires both timestamps",
        lambda: replace(valid_evidence, finished_at=""),
    ),
    (
        "transition results reject negative revisions",
        lambda: TransitionResult(
            SPEC.operation_id, "running", "finalizing", -1, True
        ),
    ),
    (
        "stable serialization rejects non-contract values",
        lambda: to_dict({"not": "a dataclass"}),
    ),
):
    rejected(label, call)

legacy_pending = operation_record_from_dict(
    {
        **to_dict(RECORD),
        "pending_effect": "legacy-effect",
        "effect_id": "",
        "effect_outcome": "",
    }
)
assert (
    legacy_pending.effect_id == "legacy-effect"
    and legacy_pending.effect_outcome == EffectOutcome.PENDING
)
print("OK   legacy pending effects receive the bounded derived outcome")

print("\nAll contract boundary tests passed.")
