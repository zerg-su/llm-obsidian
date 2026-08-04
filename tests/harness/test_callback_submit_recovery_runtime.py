#!/usr/bin/env python3
"""Frozen v2.6.3 missing-submit incident and runtime recovery contract."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.liveness import (  # noqa: E402
    LivenessEvidence,
    LivenessPolicy,
    LivenessState,
    observe_liveness,
)
from harness.callback_submit_recovery import (  # noqa: E402
    CallbackSubmitEvidence,
    classify_callback_submit,
)
from harness.contracts import OperationSpec, RuntimeRoute  # noqa: E402
from harness.runtime_worker_control import RuntimeWorkerControlMixin  # noqa: E402
from harness.store import OperationStore  # noqa: E402
from harness.supervisor import OperationSupervisor  # noqa: E402


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


class FastPathWorker(RuntimeWorkerControlMixin):
    pass


with tempfile.TemporaryDirectory(prefix="review-input-runtime.") as raw:
    root = Path(raw)
    scratch = root / "scratch"
    product = root / "product"
    state_root = root / "worker-state"
    scratch.mkdir()
    product.mkdir()
    state_root.mkdir()
    callback_dir = scratch / "callbacks" / "openai-holistic"
    callback_dir.mkdir(parents=True)
    callback_path = callback_dir / ".review-callback.json"
    input_path = callback_dir / ".review-input.json"
    registration = state_root / "callback-target.json"
    registration.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation": 3,
                "operation_id": "review-round-1",
                "run_id": "review-round-run-1",
                "callback_pointer": str(callback_path),
            }
        ),
        encoding="utf-8",
    )
    (callback_dir / ".review-meta.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "transport": "review-round",
                "operation_id": "review-round-1",
                "run_id": "review-round-run-1",
                "review_id": "review-parent-1",
                "parent_session_operation_id": "review-parent-1",
                "axis": "openai-holistic",
                "verification_iteration": 0,
                "verification_profile": {
                    "name": "scoped",
                    "sha256": "d" * 64,
                },
                "worktree": str(product),
            }
        ),
        encoding="utf-8",
    )
    input_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "axis": "openai-holistic",
                "verdict": "approve",
                "verification_iteration": 0,
                "findings": [],
            }
        ),
        encoding="utf-8",
    )
    route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "high", "reviewer-callback", "e" * 64
    )
    store = OperationStore(root / "store")
    parent = OperationSpec(
        "review-parent-1",
        "review-parent-key-1",
        "review-session",
        "review-owner-1",
        route,
        "packets/review.json",
        "scoped",
    )
    child = OperationSpec(
        "review-round-1",
        "review-round-key-1",
        "review-round",
        "review-owner-1",
        route,
        "packets/review.json",
        "scoped",
    )
    store.create(parent, lane_id="openai-holistic", run_id="review-parent-run-1")
    OperationSupervisor(store, "review-owner-1", "review-parent-1").configure_budget(
        attempt_limit=1,
        model_restart_limit=0,
        time_budget_seconds=3600,
        token_limit=100,
        now=time.time(),
    )
    store.create(
        child, lane_id="openai-holistic", run_id="review-round-run-1"
    )
    for operation_id in ("review-parent-1", "review-round-1"):
        for state in ("preflight", "starting", "running", "awaiting-callback"):
            store.transition("review-owner-1", operation_id, state)
    worker = FastPathWorker()
    worker.spec_path = state_root / "launch.json"
    worker.spec = {
        "owner_id": "review-owner-1",
        "operation_id": "review-parent-1",
        "run_id": "review-parent-run-1",
        "cwd": scratch.resolve(),
        "product_root": product.resolve(),
        "callback_registration": registration,
    }
    worker.store = store
    worker.trusted_vault = ROOT
    worker.active_target = None
    worker.last_digest = ""
    worker.stable_reads = 0
    worker.review_input_digest = ""
    worker.review_input_stable_reads = 0
    worker.callback_handled = False
    worker.registration_invalid = False
    worker.cmux_adapter = object()
    worker.inspect_callback()
    worker.inspect_callback()
    worker.inspect_callback()
    accepted = store.read("review-owner-1", "review-round-1")
    check(
        "runtime consumes stable typed input and accepts its callback without model input",
        accepted.state == "finalizing"
        and bool(accepted.accepted_callback_id)
        and (state_root / "callback-receipt.json").is_file()
        and not input_path.exists(),
        {
            "record": accepted,
            "callback_exists": callback_path.exists(),
            "input_exists": input_path.exists(),
            "callback_error": (
                (state_root / "callback-error.json").read_text(encoding="utf-8")
                if (state_root / "callback-error.json").is_file()
                else ""
            ),
            "callback_receipt": (
                (state_root / "callback-receipt.json").read_text(encoding="utf-8")
                if (state_root / "callback-receipt.json").is_file()
                else ""
            ),
        },
    )

# RED until the reviewer-specific generation-bound recovery policy is wired.
recovery = classify_callback_submit(
    CallbackSubmitEvidence(
        observed_at=incident["second_observed_at"],
        generation_progress_at=incident["first_observed_at"],
        callback_deadline_at=(
            incident["second_observed_at"]
            + incident["deadline_remaining_seconds"]
        ),
        operation_id=incident["operation_id"],
        run_id=incident["run_id"],
        lane_id=incident["lane_id"],
        generation=incident["generation"],
        expected_operation_id=incident["operation_id"],
        expected_run_id=incident["run_id"],
        expected_lane_id=incident["lane_id"],
        expected_generation=incident["generation"],
        target_sha256=incident["target_sha256"],
        expected_target_sha256=incident["target_sha256"],
        operation_state=incident["operation_state"],
        process_status=incident["process_status"],
        surface_status="alive",
        prompt_class="idle-prompt",
        stable_idle_observations=2,
        nudge_count=incident["nudge_count"],
        restart_count=incident["restart_count"],
    )
)
check(
    "idle current reviewer generation reserves one submit-only recovery",
    recovery.action == fixture["required_recovery"]["action"],
    recovery,
)
