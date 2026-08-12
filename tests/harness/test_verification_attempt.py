#!/usr/bin/env python3
"""Immutable bounded pipeline verification attempt contracts."""

from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.verification_attempt import (  # noqa: E402
    VerificationAttempt,
    VerificationAttemptError,
    build_verification_escalation,
    resolve_verification_escalation,
    verification_resolution_authorizes,
)


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


attempt_0 = VerificationAttempt(
    parent_operation_id="parent-operation",
    profile="scoped",
    profile_sha256="a" * 64,
    exact_head_sha="b" * 40,
    attempt_index=0,
)
attempt_1 = attempt_0.same_head_retry()

check(
    "same-HEAD retry preserves authority and advances only the bounded index",
    attempt_1.parent_operation_id == attempt_0.parent_operation_id
    and attempt_1.profile == attempt_0.profile
    and attempt_1.profile_sha256 == attempt_0.profile_sha256
    and attempt_1.exact_head_sha == attempt_0.exact_head_sha
    and attempt_1.attempt_index == 1
    and attempt_1.sha256 != attempt_0.sha256,
    (attempt_0, attempt_1),
)
check(
    "changed HEAD starts a distinct attempt-zero lineage",
    attempt_1.changed_head("c" * 40).attempt_index == 0
    and attempt_1.changed_head("c" * 40).exact_head_sha == "c" * 40,
)
escalation = build_verification_escalation(attempt_0, "verify-operation")
resolution = resolve_verification_escalation(
    escalation,
    action="authorize-one-same-head-retry",
    evidence_note="Transient local command execution failed before product effect.",
)
check(
    "typed mechanism-flake resolution binds exact identities without copied prose",
    verification_resolution_authorizes(
        resolution, attempt_0, "verify-operation"
    )
    and resolution["decision"] == "authorize-attempt-1"
    and resolution["action"] == "authorize-one-same-head-retry",
    resolution,
)
copied_wrong = dict(resolution)
copied_wrong["exact_head_sha"] = "c" * 40
check(
    "typed verification resolution rejects one copied identity token",
    not verification_resolution_authorizes(
        copied_wrong, attempt_0, "verify-operation"
    ),
)

for label, action in (
    ("attempt values are immutable", lambda: setattr(attempt_0, "attempt_index", 1)),
    ("a second same-HEAD retry is rejected", attempt_1.same_head_retry),
    (
        "changed-HEAD path requires a different exact HEAD",
        lambda: attempt_0.changed_head(attempt_0.exact_head_sha),
    ),
    (
        "attempt index cannot exceed the code-owned ceiling",
        lambda: VerificationAttempt(
            "parent-operation", "scoped", "a" * 64, "b" * 40, 2
        ),
    ),
):
    try:
        action()
    except (FrozenInstanceError, VerificationAttemptError):
        pass
    else:
        raise AssertionError(label)
    print(f"OK   {label}")

round_trip = VerificationAttempt.from_dict(attempt_1.as_dict())
check(
    "attempt serialization is exact and hash-stable",
    round_trip == attempt_1 and round_trip.sha256 == attempt_1.sha256,
    round_trip,
)

print("OK   VerificationAttempt is immutable, exact, and bounded")
