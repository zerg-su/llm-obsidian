#!/usr/bin/env python3
"""Reviewer effects require one exact ProviderEvent authority."""

from __future__ import annotations

import sys
import tempfile
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
from harness.liveness import (  # noqa: E402
    LivenessEvidence,
    LivenessPolicy,
    LivenessState,
    observe_liveness,
)
from harness.provider_events import ProviderEvent, ProviderEventIdentity  # noqa: E402
from harness.runtime_session_delivery import DeliveryController  # noqa: E402


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


reviewer_policy = LivenessPolicy.reviewer()
base = LivenessEvidence(
    observed_at=0,
    process_status="alive",
    operation_revision=1,
    operation_state="awaiting-callback",
    screen_sha256="a" * 64,
    prompt_state="non-interactive",
)
state = LivenessState.start(base)
for label, evidence in (
    ("elapsed deadline", replace(base, observed_at=3600)),
    ("dead provider", replace(base, observed_at=60, process_status="dead")),
    ("unknown provider", replace(base, observed_at=60, process_status="unknown")),
):
    decision, current = observe_liveness(state, evidence, reviewer_policy)
    check(
        f"{label} has zero positive reviewer effect",
        not decision.model_call
        and decision.action in {"observe", "suspected-idle", "attention-required"}
        and current.nudge_count == 0
        and current.restart_count == 0,
    )

missing = ArtifactEvidence()
idle = CallbackSubmitEvidence(
    observed_at=1000,
    generation_progress_at=0,
    callback_deadline_at=2000,
    operation_id="review-operation",
    run_id="review-run",
    lane_id="openai-holistic",
    generation=1,
    expected_operation_id="review-operation",
    expected_run_id="review-run",
    expected_lane_id="openai-holistic",
    expected_generation=1,
    target_sha256="b" * 64,
    expected_target_sha256="b" * 64,
    operation_state="awaiting-callback",
    process_status="alive",
    surface_status="alive",
    prompt_class="idle-prompt",
    stable_idle_observations=20,
    input_artifact=missing,
    callback_artifact=missing,
    receipt_artifact=missing,
)
idle_decision = classify_callback_submit(idle, CallbackSubmitPolicy.default())
check(
    "stable screen and elapsed time cannot authorize callback submit",
    idle_decision.action == "none" and not idle_decision.model_effect,
)

identity = ProviderEventIdentity(
    owner_id="review-owner",
    operation_id="review-operation",
    run_id="review-run",
    generation=1,
    provider_session_id="provider-session",
    process_identity="c" * 64,
    source_id="provider-source",
    workspace_id="workspace",
    surface_id="surface",
)
with tempfile.TemporaryDirectory(prefix="event-only-review.") as raw:
    controller = DeliveryController(
        Path(raw),
        profile="interactive",
        identity=identity,
        idempotency_key="input-effect",
    )
    assert controller.decide().action == "send"
    controller.record_send_outcome("input-effect", "accepted")
    assert controller.decide(
        event=ProviderEvent(
            "provider-started", identity, 1
        )
    ).action == "wait"
    assert controller.decide(
        event=ProviderEvent(
            "input-accepted", identity, 2, effect_id="input-effect"
        )
    ).action == "wait"
    stopped = controller.decide(
        event=ProviderEvent("turn-stopped", identity, 3)
    )
    check(
        "turn-stopped is telemetry-only until a production adapter exists",
        stopped.action == "attention"
        and stopped.reason == "callback-submit-unsupported"
        and not stopped.effect_id
        and controller.current_state().callback_submits == 0,
    )

check(
    "reviewer runtime contains no restart authority",
    "def restart_for_liveness" not in (
        ROOT / "scripts/harness/runtime_worker_liveness.py"
    ).read_text(encoding="utf-8"),
)

print("\nAll event-only reviewer effect tests passed.")
