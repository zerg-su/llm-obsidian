#!/usr/bin/env python3
"""Deterministic callback-liveness recovery ladder."""

from __future__ import annotations

import sys
import tempfile
import hashlib
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
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
import harness.liveness as liveness_module  # noqa: E402
from harness.contracts import to_dict  # noqa: E402
from harness.runtime_worker import (  # noqa: E402
    _current_callback_receipt_sha256,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


policy = LivenessPolicy.default()
callback_submit_identity = {
    "operation_id": "review-round",
    "run_id": "review-run",
    "lane_id": "openai-holistic",
    "generation": 3,
    "target_sha256": "a" * 64,
    "expected_operation_id": "review-round",
    "expected_run_id": "review-run",
    "expected_lane_id": "openai-holistic",
    "expected_generation": 3,
    "expected_target_sha256": "a" * 64,
}
callback_submit_binding = hashlib.sha256(
    json.dumps(
        callback_submit_identity,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()
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

with tempfile.TemporaryDirectory(prefix="liveness-durability.") as raw:
    events: list[str] = []
    original_fsync = liveness_module.os.fsync
    original_replace = liveness_module.os.replace

    def recording_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        events.append("dir-fsync" if stat.S_ISDIR(mode) else "file-fsync")
        original_fsync(descriptor)

    def recording_replace(source: object, target: object) -> None:
        events.append("replace")
        original_replace(source, target)

    def durable_publications(label: str, action) -> None:
        events.clear()
        action()
        replace_indexes = [
            index for index, event in enumerate(events) if event == "replace"
        ]
        check(
            label,
            bool(replace_indexes)
            and all(
                "file-fsync" in events[:index]
                and index + 1 < len(events)
                and events[index + 1] == "dir-fsync"
                for index in replace_indexes
            ),
        )

    liveness_module.os.fsync = recording_fsync
    liveness_module.os.replace = recording_replace
    try:
        durable_controller = LivenessController(Path(raw) / "liveness")
        durable_publications(
            "first liveness state publication fsyncs file and directory",
            lambda: durable_controller.observe(base, policy),
        )
        durable_publications(
            "callback reservation is durable before provider effect",
            lambda: durable_controller.reserve_callback_submit(
                callback_submit_binding, callback_submit_identity
            ),
        )
        events.append("provider-effect")
        check(
            "provider effect follows the durable reservation receipt",
            events[-2:] == ["dir-fsync", "provider-effect"],
        )
        durable_publications(
            "sent callback transition fsyncs state and receipt",
            lambda: durable_controller.mark_callback_submit_sent(
                callback_submit_binding
            ),
        )
        durable_publications(
            "accepted callback retirement fsyncs state and receipt",
            lambda: durable_controller.retire_callback_submit_after_acceptance(
                callback_submit_binding,
                "9" * 64,
                generation=3,
                operation_id="review-round",
                run_id="review-run",
                lane_id="openai-holistic",
            ),
        )

        uncertain_controller = LivenessController(Path(raw) / "uncertain")
        uncertain_controller.observe(base, policy)
        uncertain_controller.reserve_callback_submit(
            callback_submit_binding, callback_submit_identity
        )
        durable_publications(
            "uncertain callback transition fsyncs state and receipt",
            lambda: uncertain_controller.mark_callback_submit_uncertain(
                callback_submit_binding
            ),
        )
    finally:
        liveness_module.os.fsync = original_fsync
        liveness_module.os.replace = original_replace

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
    binding = callback_submit_binding
    check(
        "callback submit reserves the shared nudge budget exactly once",
        controller.reserve_callback_submit(binding, callback_submit_identity) is True
        and controller.reserve_callback_submit(binding, callback_submit_identity) is False,
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
    def retire() -> None:
        controller.retire_callback_submit_after_acceptance(
            binding,
            accepted_callback_receipt_sha256,
            generation=3,
            operation_id="review-round",
            run_id="review-run",
            lane_id="openai-holistic",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _index: retire(), range(2)))
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
    check(
        "concurrent callback retirement replays idempotently",
        controller.current_state() == retired,
    )
    check(
        "a different callback generation cannot reuse the reservation",
        controller.reserve_callback_submit(
            hashlib.sha256(
                json.dumps(
                    {**callback_submit_identity, "generation": 4, "expected_generation": 4},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            {**callback_submit_identity, "generation": 4, "expected_generation": 4},
        )
        is False,
    )

with tempfile.TemporaryDirectory(prefix="callback-submit-corrupt.") as raw:
    controller = LivenessController(Path(raw))
    controller.observe(base, policy)
    binding = callback_submit_binding
    controller.reserve_callback_submit(binding, callback_submit_identity)
    controller.mark_callback_submit_sent(binding)
    (
        Path(raw) / "receipts" / f"callback-submit-{binding}.json"
    ).write_text("not-json\n", encoding="utf-8")
    try:
        controller.retire_callback_submit_after_acceptance(
            binding,
            "7" * 64,
            generation=3,
            operation_id="review-round",
            run_id="review-run",
            lane_id="openai-holistic",
        )
    except Exception as exc:
        check(
            "malformed callback submit receipt fails closed",
            "receipt is invalid" in str(exc),
        )
    else:
        check("malformed callback submit receipt fails closed", False)

with tempfile.TemporaryDirectory(prefix="callback-submit-sent-crash.") as raw:
    controller = LivenessController(Path(raw))
    controller.observe(base, policy)
    controller.reserve_callback_submit(
        callback_submit_binding, callback_submit_identity
    )
    original_write = controller._write
    crash_injected = False

    def crash_before_sent_receipt(path: Path, value: object) -> None:
        is_sent_receipt = (
            isinstance(value, dict)
            and path.name
            == f"callback-submit-{callback_submit_binding}.json"
            and value.get("status") == "sent"
        )
        if is_sent_receipt:
            raise OSError("injected sent receipt crash")
        original_write(path, value)

    controller._write = crash_before_sent_receipt  # type: ignore[method-assign]
    try:
        controller.mark_callback_submit_sent(callback_submit_binding)
    except OSError:
        crash_injected = True
    finally:
        controller._write = original_write  # type: ignore[method-assign]
    sent_state = controller.current_state()
    reserved_receipt = json.loads(
        (
            Path(raw)
            / "receipts"
            / f"callback-submit-{callback_submit_binding}.json"
        ).read_text(encoding="utf-8")
    )
    check(
        "sent transition kill point persists the exact replay-healable phase",
        crash_injected
        and sent_state is not None
        and sent_state.callback_submit_status == "sent"
        and reserved_receipt["status"] == "reserved",
    )

with tempfile.TemporaryDirectory(prefix="callback-submit-retire-crash.") as raw:
    controller = LivenessController(Path(raw))
    controller.observe(base, policy)
    controller.reserve_callback_submit(
        callback_submit_binding, callback_submit_identity
    )
    controller.mark_callback_submit_sent(callback_submit_binding)
    original_write = controller._write
    crash_injected = False

    def crash_before_retired_state(path: Path, value: object) -> None:
        is_retired_state = isinstance(value, dict) and path.name == "state.json"
        if (
            is_retired_state
            and value.get("callback_submit_binding") == ""
            and value.get("callback_submit_status") == ""
        ):
            raise OSError("injected retirement state crash")
        original_write(path, value)

    controller._write = crash_before_retired_state  # type: ignore[method-assign]
    try:
        controller.retire_callback_submit_after_acceptance(
            callback_submit_binding,
            "6" * 64,
            generation=3,
            operation_id="review-round",
            run_id="review-run",
            lane_id="openai-holistic",
        )
    except OSError:
        crash_injected = True
    finally:
        controller._write = original_write  # type: ignore[method-assign]
    mixed_state = controller.current_state()
    mixed_receipt = json.loads(
        (
            Path(raw)
            / "receipts"
            / f"callback-submit-{callback_submit_binding}.json"
        ).read_text(encoding="utf-8")
    )
    check(
        "retirement kill point leaves one exact resumable mixed phase",
        crash_injected
        and mixed_state is not None
        and mixed_state.callback_submit_status == "sent"
        and mixed_receipt["status"] == "accepted",
    )
    controller.retire_callback_submit_after_acceptance(
        callback_submit_binding,
        "6" * 64,
        generation=3,
        operation_id="review-round",
        run_id="review-run",
        lane_id="openai-holistic",
    )
    replayed_state = controller.current_state()
    check(
        "accepted retirement replay completes without another provider effect",
        replayed_state is not None
        and replayed_state.callback_submit_binding == ""
        and replayed_state.callback_submit_status == ""
        and replayed_state.nudge_count == 1,
    )
    foreign_state = replace(
        replayed_state,
        callback_submit_binding="f" * 64,
        callback_submit_status="sent",
    )
    controller._write(  # type: ignore[attr-defined]
        Path(raw) / "state.json",
        to_dict(foreign_state),
    )
    try:
        controller.retire_callback_submit_after_acceptance(
            callback_submit_binding,
            "6" * 64,
            generation=3,
            operation_id="review-round",
            run_id="review-run",
            lane_id="openai-holistic",
        )
    except Exception as exc:
        check(
            "accepted retirement never clears a foreign live binding",
            "acceptance identity changed" in str(exc)
            and controller.current_state() == foreign_state,
        )
    else:
        check("accepted retirement never clears a foreign live binding", False)

with tempfile.TemporaryDirectory(prefix="callback-submit-identity.") as raw:
    controller = LivenessController(Path(raw))
    controller.observe(base, policy)
    invalid_identity = dict(callback_submit_identity)
    invalid_identity.pop("expected_lane_id")
    try:
        controller.reserve_callback_submit(callback_submit_binding, invalid_identity)
    except Exception as exc:
        check(
            "incomplete callback generation identity fails closed",
            "generation identity is invalid" in str(exc),
        )
    else:
        check("incomplete callback generation identity fails closed", False)
    try:
        controller.reserve_callback_submit("f" * 64, callback_submit_identity)
    except Exception as exc:
        check(
            "callback generation identity cannot change its binding",
            "binding identity changed" in str(exc),
        )
    else:
        check("callback generation identity cannot change its binding", False)
    controller.reserve_callback_submit(
        callback_submit_binding, callback_submit_identity
    )
    controller.mark_callback_submit_sent(callback_submit_binding)
    try:
        controller.retire_callback_submit_after_acceptance(
            callback_submit_binding,
            "8" * 64,
            generation=3,
            operation_id="review-round",
            run_id="wrong-run",
            lane_id="openai-holistic",
        )
    except Exception as exc:
        check(
            "accepted callback must match the reserved run identity",
            "acceptance identity changed" in str(exc),
        )
    else:
        check("accepted callback must match the reserved run identity", False)

with tempfile.TemporaryDirectory(prefix="callback-submit-no-reservation.") as raw:
    controller = LivenessController(Path(raw))
    controller.observe(base, policy)
    try:
        controller.mark_callback_submit_sent(callback_submit_binding)
    except Exception as exc:
        check(
            "callback submit cannot be marked sent without reservation",
            "reservation identity changed" in str(exc),
        )
    else:
        check("callback submit cannot be marked sent without reservation", False)
    try:
        controller.mark_callback_submit_uncertain(callback_submit_binding)
    except Exception as exc:
        check(
            "callback submit cannot be marked uncertain without reservation",
            "reservation identity changed" in str(exc),
        )
    else:
        check(
            "callback submit cannot be marked uncertain without reservation",
            False,
        )

with tempfile.TemporaryDirectory(prefix="callback-submit-uncertain.") as raw:
    controller = LivenessController(Path(raw))
    controller.observe(base, policy)
    binding = callback_submit_binding
    check(
        "callback submit uncertainty starts from an exact reservation",
        controller.reserve_callback_submit(binding, callback_submit_identity) is True,
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
        controller.reserve_callback_submit(
            callback_submit_binding, callback_submit_identity
        )
    except Exception as exc:
        check(
            "callback submit cannot reserve without liveness state",
            "no liveness state" in str(exc),
        )
    else:
        check("callback submit cannot reserve without liveness state", False)

print("\nAll liveness tests passed.")
