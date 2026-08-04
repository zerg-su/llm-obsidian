#!/usr/bin/env python3
"""Pure reviewer callback-submit recovery state/decision matrix."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.callback_submit_recovery import (  # noqa: E402
    ArtifactEvidence,
    CallbackSubmitEvidence,
    CallbackSubmitPolicy,
    classify_callback_submit,
)


SHA = "a" * 64


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def artifact(state: str = "missing", digest: str = "") -> ArtifactEvidence:
    return ArtifactEvidence(state=state, sha256=digest)


def idle(**changes: object) -> CallbackSubmitEvidence:
    value = CallbackSubmitEvidence(
        observed_at=1000,
        generation_progress_at=100,
        callback_deadline_at=1300,
        operation_id="review-parent-1",
        run_id="review-run-1",
        lane_id="openai-holistic",
        generation=3,
        expected_operation_id="review-parent-1",
        expected_run_id="review-run-1",
        expected_lane_id="openai-holistic",
        expected_generation=3,
        target_sha256=SHA,
        expected_target_sha256=SHA,
        operation_state="awaiting-callback",
        process_status="alive",
        surface_status="alive",
        prompt_class="idle-prompt",
        stable_idle_observations=2,
        input_artifact=artifact(),
        callback_artifact=artifact(),
        receipt_artifact=artifact(),
        nudge_count=0,
        restart_count=0,
        recovery_status="none",
    )
    return replace(value, **changes)


policy = CallbackSubmitPolicy.default()


decision = classify_callback_submit(idle(), policy)
check(
    "exact current idle generation reserves one recovery",
    decision.state == "idle-without-submit"
    and decision.action == "reserve-submit-recovery"
    and not decision.model_effect
    and len(decision.action_id) == 64,
    decision,
)
check(
    "screen identity is not part of generation-bound action identity",
    decision.action_id
    == classify_callback_submit(idle(), policy).action_id,
)

for label, mutation, state, action, reason in (
    (
        "stable typed input wins without model effect",
        {"input_artifact": artifact("stable", "b" * 64)},
        "typed-input-ready",
        "submit-typed-input",
        "",
    ),
    (
        "stable callback wins without model effect",
        {"callback_artifact": artifact("stable", "c" * 64)},
        "callback-ready",
        "accept-callback",
        "",
    ),
    (
        "accepted receipt wins every recovery race",
        {
            "receipt_artifact": artifact("stable", "d" * 64),
            "recovery_status": "sent",
            "nudge_count": 1,
        },
        "accepted",
        "none",
        "",
    ),
    (
        "reserved effect is resumed without a second reservation",
        {"recovery_status": "reserved", "nudge_count": 1},
        "recovery-reserved",
        "send-reserved-recovery",
        "",
    ),
    (
        "sent effect is observed without replay",
        {"recovery_status": "sent", "nudge_count": 1},
        "recovery-sent",
        "none",
        "",
    ),
    (
        "active provider produces zero effect",
        {"prompt_class": "active"},
        "working",
        "none",
        "",
    ),
    (
        "one idle observation produces zero effect",
        {"stable_idle_observations": 1},
        "working",
        "none",
        "",
    ),
    (
        "production floor blocks an early idle prompt",
        {"generation_progress_at": 101},
        "working",
        "none",
        "",
    ),
    (
        "permission prompt fails closed",
        {"prompt_class": "permission"},
        "attention",
        "attention-required",
        "callback-submit-permission",
    ),
    (
        "unknown prompt fails closed",
        {"prompt_class": "unknown"},
        "attention",
        "attention-required",
        "callback-submit-evidence-unknown",
    ),
    (
        "lost process ownership fails closed",
        {"process_status": "unknown"},
        "attention",
        "attention-required",
        "callback-submit-ownership-lost",
    ),
    (
        "dead provider fails closed for submit recovery",
        {"process_status": "dead"},
        "attention",
        "attention-required",
        "callback-submit-provider-unavailable",
    ),
    (
        "stale generation fails closed",
        {"expected_generation": 4},
        "attention",
        "attention-required",
        "callback-submit-stale-generation",
    ),
    (
        "stale target fails closed",
        {"expected_target_sha256": "e" * 64},
        "attention",
        "attention-required",
        "callback-submit-stale-generation",
    ),
    (
        "terminal parent fails closed",
        {"operation_state": "complete"},
        "attention",
        "attention-required",
        "callback-submit-terminal",
    ),
    (
        "insufficient deadline fails closed",
        {"callback_deadline_at": 1119},
        "attention",
        "attention-required",
        "callback-submit-deadline-insufficient",
    ),
    (
        "shared generic nudge ceiling blocks submit recovery",
        {"nudge_count": 1},
        "attention",
        "attention-required",
        "callback-submit-budget-exhausted",
    ),
    (
        "symlink input fails closed",
        {"input_artifact": artifact("symlink")},
        "attention",
        "attention-required",
        "callback-submit-artifact-invalid",
    ),
    (
        "oversize callback fails closed",
        {"callback_artifact": artifact("oversize")},
        "attention",
        "attention-required",
        "callback-submit-artifact-invalid",
    ),
    (
        "unstable typed input is observed without effect",
        {"input_artifact": artifact("unstable", "f" * 64)},
        "working",
        "none",
        "",
    ),
):
    result = classify_callback_submit(idle(**mutation), policy)
    check(
        label,
        result.state == state
        and result.action == action
        and result.reason == reason
        and result.model_effect == (action == "send-reserved-recovery"),
        result,
    )

malformed = classify_callback_submit(
    idle(operation_id="not valid", target_sha256="bad"), policy
)
check(
    "malformed identity and digest fail closed",
    malformed.state == "attention"
    and malformed.reason == "callback-submit-evidence-malformed"
    and not malformed.model_effect,
    malformed,
)

test_policy = CallbackSubmitPolicy(
    probe_seconds=30,
    nudge_after_seconds=60,
    max_nudges=1,
)
check(
    "tests can inject a bounded policy without changing production defaults",
    classify_callback_submit(
        idle(observed_at=160, generation_progress_at=100, callback_deadline_at=220),
        test_policy,
    ).action
    == "reserve-submit-recovery"
    and CallbackSubmitPolicy.default().nudge_after_seconds == 900,
)
