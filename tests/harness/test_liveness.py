#!/usr/bin/env python3
"""Deterministic callback-liveness recovery ladder."""

from __future__ import annotations

import sys
import tempfile
import hashlib
import json
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
from harness.runtime_worker import (  # noqa: E402
    _current_callback_receipt_sha256,
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

with tempfile.TemporaryDirectory(prefix="liveness-receipt.") as raw:
    runtime_root = Path(raw)
    receipt_path = runtime_root / "callback-receipt.json"
    target_path = runtime_root / "callback-target.json"
    receipt = {
        "schema_version": 1,
        "generation": 2,
        "operation_id": "review-round-initial",
        "run_id": "verification-run",
        "callback_id": "callback-initial",
        "payload_sha256": "a" * 64,
        "status": "accepted",
    }
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    target_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation": 3,
                "operation_id": "review-round-verification",
                "run_id": "verification-run",
                "callback_pointer": "callbacks/verification.json",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    check(
        "receipt from an earlier review generation is not liveness progress",
        _current_callback_receipt_sha256(runtime_root) == "",
    )
    receipt["generation"] = 3
    receipt["operation_id"] = "review-round-verification"
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    check(
        "receipt matching the current callback target is liveness progress",
        _current_callback_receipt_sha256(runtime_root)
        == hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
    )
    receipt["status"] = "duplicate"
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    check(
        "exact duplicate receipt reconstructed after broker acceptance is progress",
        _current_callback_receipt_sha256(runtime_root)
        == hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
    )
    receipt["run_id"] = "wrong-run"
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    check(
        "duplicate receipt with the wrong run is not liveness progress",
        _current_callback_receipt_sha256(runtime_root) == "",
    )

with tempfile.TemporaryDirectory(prefix="callback-submit-liveness.") as raw:
    controller = LivenessController(Path(raw))
    controller.observe(base, policy)
    binding = "d" * 64
    check(
        "callback submit reserves the shared nudge budget exactly once",
        controller.reserve_callback_submit(binding) is True
        and controller.reserve_callback_submit(binding) is False,
    )
    controller.mark_callback_submit_sent(binding)
    controller.mark_callback_submit_sent(binding)
    sent = controller.current_state()
    check(
        "callback submit sent state is durable and idempotent",
        sent is not None
        and sent.callback_submit_binding == binding
        and sent.callback_submit_status == "sent"
        and sent.nudge_count == 1,
    )
    accepted_callback_receipt_sha256 = "9" * 64
    controller.retire_callback_submit_after_acceptance(
        binding,
        accepted_callback_receipt_sha256,
    )
    retired = controller.current_state()
    accepted_receipt = json.loads(
        (
            Path(raw)
            / "receipts"
            / f"callback-submit-{binding}.json"
        ).read_text(encoding="utf-8")
    )
    check(
        "accepted callback retires only its exact submit binding",
        retired is not None
        and retired.callback_submit_binding == ""
        and retired.callback_submit_status == ""
        and retired.nudge_count == 1
        and accepted_receipt["status"] == "accepted"
        and accepted_receipt["accepted_callback_receipt_sha256"]
        == accepted_callback_receipt_sha256,
    )
    try:
        controller.retire_callback_submit_after_acceptance(
            binding,
            accepted_callback_receipt_sha256,
        )
    except Exception as exc:
        check(
            "retired callback generation cannot be accepted twice",
            "acceptance identity changed" in str(exc),
        )
    else:
        check("retired callback generation cannot be accepted twice", False)
    check(
        "a different callback generation cannot reuse the reservation",
        controller.reserve_callback_submit("e" * 64) is False,
    )

with tempfile.TemporaryDirectory(prefix="callback-submit-corrupt.") as raw:
    controller = LivenessController(Path(raw))
    controller.observe(base, policy)
    binding = "8" * 64
    controller.reserve_callback_submit(binding)
    controller.mark_callback_submit_sent(binding)
    (
        Path(raw) / "receipts" / f"callback-submit-{binding}.json"
    ).write_text("not-json\n", encoding="utf-8")
    try:
        controller.retire_callback_submit_after_acceptance(
            binding,
            "7" * 64,
        )
    except Exception as exc:
        check(
            "malformed callback submit receipt fails closed",
            "receipt is invalid" in str(exc),
        )
    else:
        check("malformed callback submit receipt fails closed", False)

with tempfile.TemporaryDirectory(prefix="callback-submit-uncertain.") as raw:
    controller = LivenessController(Path(raw))
    controller.observe(base, policy)
    binding = "f" * 64
    check(
        "callback submit uncertainty starts from an exact reservation",
        controller.reserve_callback_submit(binding) is True,
    )
    controller.mark_callback_submit_uncertain(binding)
    controller.mark_callback_submit_uncertain(binding)
    uncertain = controller.current_state()
    check(
        "uncertain callback transport fails closed and remains idempotent",
        uncertain is not None
        and uncertain.callback_submit_status == "uncertain"
        and uncertain.nudge_count == 1,
    )

with tempfile.TemporaryDirectory(prefix="callback-submit-invalid.") as raw:
    controller = LivenessController(Path(raw))
    try:
        controller.reserve_callback_submit("a" * 64)
    except Exception as exc:
        check(
            "callback submit cannot reserve without liveness state",
            "no liveness state" in str(exc),
        )
    else:
        check("callback submit cannot reserve without liveness state", False)

print("\nAll liveness tests passed.")
