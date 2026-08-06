#!/usr/bin/env python3
"""Pure immutable exact-HEAD ReviewAttempt contracts."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.review_attempt import (  # noqa: E402
    LEGACY_CROSS_HEAD_RESUME_DISABLED,
    ReviewAttempt,
    ReviewAttemptError,
    ReviewAttemptIdentity,
    ReviewAttemptLaneIdentity,
    ReviewAttemptLaneResult,
    ReviewAttemptPolicy,
    ReviewAttemptTerminal,
    ReviewAttemptTerminalResult,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def rejected(label: str, callback) -> None:
    try:
        callback()
    except ReviewAttemptError:
        print(f"OK   {label}")
        return
    raise AssertionError(label)


policy = ReviewAttemptPolicy(
    depth="deep",
    cross_model=False,
    runtime="codex",
    model="sol",
    effort="xhigh",
    max_verify_iterations=2,
    purpose="implementation",
    selected_provider="openai",
)
lanes = (
    ReviewAttemptLaneIdentity(
        axis="openai-intent",
        owner_id="task-1",
        operation_id="review-1-intent",
        lane_id="lane-intent",
        run_id="run-intent",
        runtime="codex",
        model="gpt-5.6-sol",
        effort="xhigh",
        profile="reviewer-callback",
        routing_sha256="1" * 64,
    ),
    ReviewAttemptLaneIdentity(
        axis="openai-engineering",
        owner_id="task-1",
        operation_id="review-1-engineering",
        lane_id="lane-engineering",
        run_id="run-engineering",
        runtime="codex",
        model="gpt-5.6-sol",
        effort="xhigh",
        profile="reviewer-callback",
        routing_sha256="1" * 64,
    ),
)
identity = ReviewAttemptIdentity(
    attempt_id="review-1",
    finalization_lineage_id="task-1",
    cycle=1,
    plan_sha256="2" * 64,
    outcome_sha256="3" * 64,
    exact_head_sha="a" * 40,
    policy=policy,
    lanes=lanes,
)

attempt = ReviewAttempt.pending(identity)
check(
    "attempt identity round-trips without mutable aliases",
    ReviewAttempt.from_mapping(attempt.payload()) == attempt
    and attempt.identity.policy_sha256 == policy.sha256,
)

malformed_identity = identity.payload()
malformed_identity["lanes"].append("not-a-lane")
rejected(
    "identity parsing rejects a non-object lane without dropping it",
    lambda: ReviewAttemptIdentity.from_mapping(malformed_identity),
)
rejected(
    "attempt policy rejects a mutable verification budget",
    lambda: replace(policy, max_verify_iterations=1),
)
attempt = attempt.start(identity)
attempt = attempt.await_callback(identity)
check(
    "attempt follows the one-way pre-terminal state graph",
    attempt.status == "awaiting-callback" and attempt.terminal is None,
)

terminal = ReviewAttemptTerminal(
    result=ReviewAttemptTerminalResult.CHANGES_REQUESTED,
    exact_head_sha=identity.exact_head_sha,
    lane_results=(
        ReviewAttemptLaneResult(
            "openai-intent", "approve", "4" * 64, ()
        ),
        ReviewAttemptLaneResult(
            "openai-engineering",
            "changes-requested",
            "5" * 64,
            ("F-1",),
        ),
    ),
)
malformed_terminal = terminal.payload()
malformed_terminal["lane_results"].append("not-a-result")
rejected(
    "terminal parsing rejects a non-object result without dropping it",
    lambda: ReviewAttemptTerminal.from_mapping(malformed_terminal),
)
finished = attempt.finish(identity, terminal)
check(
    "changes-requested is a terminal attempt result",
    finished.status == "terminal"
    and finished.terminal == terminal
    and ReviewAttempt.from_mapping(finished.payload()) == finished,
)

rejected(
    "a terminal attempt cannot be written twice",
    lambda: finished.finish(identity, terminal),
)
rejected(
    "a terminal attempt cannot be rearmed",
    lambda: finished.rearm(identity),
)
rejected(
    "an attempt cannot bind a changed HEAD",
    lambda: attempt.assert_identity(
        replace(identity, exact_head_sha="b" * 40)
    ),
)
rejected(
    "verification iterations are absent from an attempt",
    lambda: replace(lanes[0], verification_iteration=1),
)
rejected(
    "a terminal result must cover every frozen lane exactly once",
    lambda: ReviewAttemptTerminal(
        result=ReviewAttemptTerminalResult.APPROVED,
        exact_head_sha=identity.exact_head_sha,
        lane_results=(
            ReviewAttemptLaneResult(
                "openai-intent", "approve", "4" * 64, ()
            ),
        ),
    ).validate_for(identity),
)
check(
    "historical resume is typed, read-only, and provider-effect free",
    LEGACY_CROSS_HEAD_RESUME_DISABLED.payload()
    == {
        "schema_version": 1,
        "status": "legacy-cross-head-resume-disabled",
        "allowed_actions": ["inspect", "archive", "cleanup"],
        "provider_effect_allowed": False,
    },
)

print("\nAll immutable ReviewAttempt tests passed.")
