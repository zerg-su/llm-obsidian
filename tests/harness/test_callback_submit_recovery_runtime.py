#!/usr/bin/env python3
"""Frozen v2.6.3 missing-submit incident and runtime recovery contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.liveness import (  # noqa: E402
    LivenessEvidence,
    LivenessPolicy,
    LivenessState,
    observe_liveness,
)


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


fixture = json.loads(
    (
        ROOT
        / "tests/harness/fixtures/v2.6.3-missing-review-submit.json"
    ).read_text(encoding="utf-8")
)
incident = fixture["incident"]
policy = LivenessPolicy.default()
first = LivenessEvidence(
    observed_at=incident["first_observed_at"],
    process_status=incident["process_status"],
    operation_revision=incident["operation_revision"],
    operation_state=incident["operation_state"],
    screen_sha256=incident["first_screen_sha256"],
    prompt_state=incident["prompt_state"],
)
previous = LivenessState.start(first)
second = LivenessEvidence(
    observed_at=incident["second_observed_at"],
    process_status=incident["process_status"],
    operation_revision=incident["operation_revision"],
    operation_state=incident["operation_state"],
    screen_sha256=incident["second_screen_sha256"],
    prompt_state=incident["prompt_state"],
)
decision, current = observe_liveness(previous, second, policy)

check(
    "v2.6.3 fixture preserves the screen-churn suppression guard",
    decision.action == fixture["v2_6_3_observed"]["liveness_action"]
    and current.last_progress_at == incident["second_observed_at"],
    (decision, current),
)

# RED until the reviewer-specific generation-bound recovery policy is wired.
check(
    "idle current reviewer generation reserves one submit-only recovery",
    decision.action == fixture["required_recovery"]["action"],
    decision,
)
