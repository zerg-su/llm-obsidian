#!/usr/bin/env python3
"""Pure reviewer callback-submit recovery state/decision matrix."""

from __future__ import annotations

import sys
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.callback_submit_recovery import (  # noqa: E402
    ArtifactEvidence,
    CallbackSubmitEvidence,
    CallbackSubmitPolicy,
    callback_submit_binding_sha256,
    classify_callback_prompt,
    classify_callback_submit,
)
from harness.runtime_callback_io import (  # noqa: E402
    observe_review_artifact,
    submit_stable_review_input,
)
from harness.liveness import (  # noqa: E402
    LivenessController,
    LivenessEvidence,
    LivenessPolicy,
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
    "recovery binding excludes artifact races but includes exact generation",
    callback_submit_binding_sha256(idle())
    == callback_submit_binding_sha256(
        idle(callback_artifact=artifact("stable", "c" * 64))
    )
    and callback_submit_binding_sha256(idle())
    != callback_submit_binding_sha256(idle(expected_generation=4)),
)
check(
    "screen classifier recognizes only exact provider idle prompts",
    classify_callback_prompt("claude", "review complete\n❯") == "idle-prompt"
    and classify_callback_prompt("codex", "review complete\n›") == "idle-prompt"
    and classify_callback_prompt("claude", "working") == "active"
    and classify_callback_prompt(
        "claude", "1. Allow\n2. Deny\nEnter", interactive=True
    )
    == "unknown"
    and classify_callback_prompt(
        "claude", "trust dialog", interactive=True, recognized=True
    )
    == "permission",
)

with tempfile.TemporaryDirectory(prefix="callback-submit-reservation.") as raw:
    controller = LivenessController(Path(raw) / "liveness")
    controller.observe(
        LivenessEvidence(
            observed_at=0,
            process_status="alive",
            operation_revision=1,
            operation_state="awaiting-callback",
        ),
        LivenessPolicy.default(),
    )
    binding = "9" * 64

    def reserve() -> bool:
        return controller.reserve_callback_submit(binding)

    with ThreadPoolExecutor(max_workers=2) as pool:
        reservations = list(pool.map(lambda _index: reserve(), range(2)))
    reserved = controller.current_state()
    check(
        "concurrent reconcile reserves the shared nudge exactly once",
        sum(reservations) == 1
        and reserved is not None
        and reserved.nudge_count == 1
        and reserved.callback_submit_binding == binding
        and reserved.callback_submit_status == "reserved",
        (reservations, reserved),
    )
    check(
        "reservation replay is an idempotent no-op",
        not controller.reserve_callback_submit(binding),
    )
    controller.mark_callback_submit_sent(binding)
    controller.mark_callback_submit_sent(binding)
    sent = controller.current_state()
    check(
        "same write-ahead receipt becomes sent idempotently",
        sent is not None
        and sent.nudge_count == 1
        and sent.callback_submit_binding == binding
        and sent.callback_submit_status == "sent",
        sent,
    )
    receipt = json.loads(
        (controller.root / "receipts" / f"callback-submit-{binding}.json").read_text(
            encoding="utf-8"
        )
    )
    check(
        "generation receipt is content-free and sent",
        set(receipt)
        == {
            "schema_version",
            "binding_sha256",
            "nudge_count",
            "status",
        }
        and receipt["status"] == "sent",
        receipt,
    )

with tempfile.TemporaryDirectory(prefix="callback-input-fast-path.") as raw:
    root = Path(raw)
    state_dir = root / "callbacks" / "openai-holistic"
    worktree = root / "product"
    state_dir.mkdir(parents=True)
    worktree.mkdir()
    review_input = state_dir / ".review-input.json"
    callback = state_dir / ".review-callback.json"
    meta = {
        "schema_version": 1,
        "transport": "review-round",
        "operation_id": "review-round-1",
        "run_id": "review-run-1",
        "review_id": "review-parent-1",
        "parent_session_operation_id": "review-parent-1",
        "axis": "openai-holistic",
        "verification_iteration": 0,
        "verification_profile": {"name": "scoped", "sha256": "b" * 64},
        "worktree": str(worktree),
    }
    result = {
        "schema_version": 1,
        "axis": "openai-holistic",
        "verdict": "approve",
        "verification_iteration": 0,
        "findings": [],
    }
    (state_dir / ".review-meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    review_input.write_text(json.dumps(result), encoding="utf-8")
    first, digest, reads = observe_review_artifact(review_input, "", 0)
    second, digest, reads = observe_review_artifact(review_input, digest, reads)
    check(
        "typed input requires two stable bounded reads",
        first.state == "unstable"
        and second.state == "stable"
        and reads == 2,
        (first, second, reads),
    )
    submitted = submit_stable_review_input(
        vault_root=ROOT,
        worktree=worktree,
        callback_path=callback,
    )
    check(
        "stable typed input publishes through the authoritative validator",
        submitted.returncode == 0
        and callback.is_file()
        and not review_input.exists(),
        submitted,
    )
    duplicate, _digest, _reads = observe_review_artifact(callback, "", 0)
    duplicate, _digest, _reads = observe_review_artifact(
        callback, _digest, _reads
    )
    replay = submit_stable_review_input(
        vault_root=ROOT,
        worktree=worktree,
        callback_path=callback,
    )
    check(
        "existing callback fast path needs no repeated submit",
        duplicate.state == "stable" and replay.returncode == 0,
        replay,
    )
    callback.unlink()
    review_input.symlink_to(state_dir / "missing.json")
    symlink, _digest, _reads = observe_review_artifact(review_input, "", 0)
    check("symlink input is typed invalid evidence", symlink.state == "symlink")
    review_input.unlink()
    review_input.write_bytes(b"x" * 70_001)
    oversize, _digest, _reads = observe_review_artifact(review_input, "", 0)
    check(
        "oversize input is typed invalid evidence",
        oversize.state == "oversize",
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
        "reserved effect is ambiguous and never replayed",
        {"recovery_status": "reserved", "nudge_count": 1},
        "attention",
        "attention-required",
        "callback-submit-effect-uncertain",
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
        "non-awaiting parent cannot start submit recovery",
        {"operation_state": "running"},
        "attention",
        "attention-required",
        "callback-submit-state-invalid",
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
        and not result.model_effect,
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
