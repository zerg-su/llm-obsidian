#!/usr/bin/env python3
"""Deterministic callback-liveness recovery ladder."""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.liveness import (  # noqa: E402
    LivenessEvidence,
    LivenessController,
    LivenessPolicy,
    LivenessState,
    observe_liveness,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


policy = LivenessPolicy.default()
base = LivenessEvidence(
    observed_at=1000,
    process_status="alive",
    operation_revision=4,
    operation_state="awaiting-callback",
    screen_sha256="a" * 64,
    prompt_state="non-interactive",
)
state = LivenessState.start(base)

decision, progressed = observe_liveness(
    state,
    replace(base, observed_at=1300, screen_sha256="b" * 64),
    policy,
)
check(
    "screen progress resets the idle clock without a model call",
    decision.action == "observe" and progressed.last_progress_at == 1300,
)

idle_evidence = replace(base, observed_at=1901, screen_sha256="b" * 64)
decision, idle = observe_liveness(progressed, idle_evidence, policy)
check(
    "ten quiet minutes produce content-free suspected idle",
    decision.action == "suspected-idle" and decision.model_call is False,
)

decision, nudged = observe_liveness(
    idle,
    replace(idle_evidence, observed_at=2201),
    policy,
)
check(
    "fifteen quiet minutes allow one bounded nudge",
    decision.action == "nudge" and nudged.nudge_count == 1,
)
decision, unchanged = observe_liveness(
    nudged,
    replace(idle_evidence, observed_at=2210),
    policy,
)
check(
    "nudge budget prevents repeated token spend",
    decision.action == "suspected-idle" and unchanged.nudge_count == 1,
)

decision, restarted = observe_liveness(
    nudged,
    replace(idle_evidence, observed_at=2501),
    policy,
)
check(
    "twenty quiet minutes allow one identity-bound restart",
    decision.action == "restart" and restarted.restart_count == 1,
)
decision, exhausted = observe_liveness(
    restarted,
    replace(idle_evidence, observed_at=2600),
    policy,
)
check(
    "exhausted recovery becomes durable attention",
    decision.action == "attention-required",
)

active = replace(
    idle_evidence,
    observed_at=2501,
    prompt_state="interactive",
)
decision, _ = observe_liveness(nudged, active, policy)
check("interactive provider is never interrupted", decision.action == "observe")

result = replace(
    base,
    observed_at=1400,
    typed_result_sha256="c" * 64,
)
decision, pending = observe_liveness(state, result, policy)
check("first typed result read waits for stability", decision.action == "observe")
decision, _ = observe_liveness(
    pending,
    replace(result, observed_at=1460),
    policy,
)
check(
    "stable result without callback selects model-free reconcile",
    decision.action == "reconcile-result" and decision.model_call is False,
)

dead = replace(base, observed_at=1060, process_status="dead")
decision, _ = observe_liveness(state, dead, policy)
check("provider death is distinct from live idle", decision.action == "restart")

with tempfile.TemporaryDirectory(prefix="liveness-state.") as raw:
    controller = LivenessController(Path(raw))
    controller.observe(base, policy)
    stored_idle = controller.observe(
        replace(base, observed_at=1601),
        policy,
    )
    stored_nudge = controller.observe(
        replace(base, observed_at=1901),
        policy,
    )
    replay = controller.observe(
        replace(base, observed_at=1901),
        policy,
    )
    receipts = list((Path(raw) / "receipts").glob("*.json"))
    check(
        "durable controller records each recovery stage once",
        stored_idle.action == "suspected-idle"
        and stored_nudge.action == "nudge"
        and replay.action == "suspected-idle"
        and len(receipts) == 3,
    )
    check(
        "liveness state and receipts remain owner-only",
        (Path(raw) / "state.json").stat().st_mode & 0o077 == 0
        and all(path.stat().st_mode & 0o077 == 0 for path in receipts),
    )

print("\nAll liveness tests passed.")
