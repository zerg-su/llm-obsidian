#!/usr/bin/env python3
"""Typed per-finding executor resolution evidence."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from review_resolution import (  # noqa: E402
    ResolutionError,
    build_resolution_evidence,
    review_transport_identity_sha256,
    validate_resolution,
    validate_resolution_evidence,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


reviewed = "a" * 40
resolved = "b" * 40
review_callbacks = [
    {
        "axis": "openai-holistic",
        "round_operation_id": "round-operation",
        "round_run_id": "round-run",
        "callback_id": "callback-current",
        "callback_sha256": "c" * 64,
    }
]
review_identity = review_transport_identity_sha256(
    "review-operation", review_callbacks
)
raw = {
    "schema_version": 1,
    "operation_id": "review-operation",
    "review_identity_sha256": review_identity,
    "reviewed_head_sha": reviewed,
    "resolved_head_sha": resolved,
    "resolutions": [
        {
            "finding_id": "F-applied",
            "disposition": "applied",
            "rationale": "The corrected branch and regression test are on the resolved HEAD.",
            "follow_up": "",
        },
        {
            "finding_id": "F-rejected",
            "disposition": "rejected",
            "rationale": "The reported path is unreachable under the bound invariant.",
            "follow_up": "",
        },
        {
            "finding_id": "F-later",
            "disposition": "out-of-scope",
            "rationale": "The observation belongs to the explicitly excluded release tooling.",
            "follow_up": "docs/follow-ups/release-tooling.md",
        },
    ],
}
validated = validate_resolution(
    raw,
    expected_operation_id="review-operation",
    expected_reviewed_head_sha=reviewed,
    expected_resolved_head_sha=resolved,
    expected_finding_ids=("F-applied", "F-rejected", "F-later"),
    expected_review_identity_sha256=review_identity,
)
check(
    "every material finding receives one terminal typed disposition",
    tuple(item.finding_id for item in validated.resolutions)
    == ("F-applied", "F-rejected", "F-later"),
)
evidence = build_resolution_evidence(
    validated,
    axis="openai-holistic",
    fix_delta=b"diff --git a/product.py b/product.py\n",
)
payload = evidence.payload()
round_tripped = validate_resolution_evidence(payload)
check(
    "harness evidence binds finding IDs heads axis and exact fix delta",
    payload["previous_finding_ids"]
    == ["F-applied", "F-rejected", "F-later"]
    and payload["reviewed_head_sha"] == reviewed
    and payload["resolved_head_sha"] == resolved
    and len(payload["fix_delta_sha256"]) == 64
    and payload["axis"] == "openai-holistic"
    and round_tripped.payload() == payload,
)
check(
    "executor response binds the exact current review callbacks",
    validated.review_identity_sha256 == review_identity,
)


invalid_cases = []
attempted = json.loads(json.dumps(raw))
attempted["resolutions"][0]["disposition"] = "attempted"
invalid_cases.append(("attempted is not terminal", attempted))
missing_rationale = json.loads(json.dumps(raw))
missing_rationale["resolutions"][1]["rationale"] = ""
invalid_cases.append(("rejection requires rationale", missing_rationale))
missing_follow_up = json.loads(json.dumps(raw))
missing_follow_up["resolutions"][2]["follow_up"] = ""
invalid_cases.append(("out-of-scope requires durable follow-up", missing_follow_up))
applied_follow_up = json.loads(json.dumps(raw))
applied_follow_up["resolutions"][0]["follow_up"] = "docs/not-allowed.md"
invalid_cases.append(("applied cannot masquerade as follow-up", applied_follow_up))
omitted = json.loads(json.dumps(raw))
omitted["resolutions"].pop()
invalid_cases.append(("material findings cannot be omitted", omitted))
duplicate = json.loads(json.dumps(raw))
duplicate["resolutions"][2]["finding_id"] = "F-applied"
invalid_cases.append(("material findings cannot be duplicated", duplicate))
same_head = json.loads(json.dumps(raw))
same_head["resolved_head_sha"] = reviewed
invalid_cases.append(("applied evidence requires a new HEAD", same_head))
stale_review = json.loads(json.dumps(raw))
stale_review["review_identity_sha256"] = "f" * 64
invalid_cases.append(
    ("prior-boundary callback identity is rejected", stale_review)
)

for label, candidate in invalid_cases:
    try:
        validate_resolution(
            candidate,
            expected_operation_id="review-operation",
            expected_reviewed_head_sha=reviewed,
            expected_resolved_head_sha=(
                reviewed if label == "applied evidence requires a new HEAD" else resolved
            ),
            expected_finding_ids=("F-applied", "F-rejected", "F-later"),
            expected_review_identity_sha256=review_identity,
        )
    except ResolutionError:
        check(label, True)
    else:
        check(label, False)

print("\nAll review resolution tests passed.")
