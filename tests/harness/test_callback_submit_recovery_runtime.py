#!/usr/bin/env python3
"""Frozen missing-submit incident and integrated reviewer recovery seams."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.callback_submit_recovery import (  # noqa: E402
    CallbackSubmitEvidence,
    CallbackSubmitPolicy,
    classify_callback_submit,
)
from harness.contracts import (  # noqa: E402
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.liveness import (  # noqa: E402
    LivenessController,
    LivenessEvidence,
    LivenessPolicy,
    LivenessState,
    observe_liveness,
)
from harness.runtime_worker_control import RuntimeWorkerControlMixin  # noqa: E402
from harness.runtime_worker_liveness import RuntimeWorkerLivenessMixin  # noqa: E402
from harness.store import OperationStore  # noqa: E402
from harness.supervisor import OperationSupervisor  # noqa: E402


SURFACE = "11111111-1111-1111-1111-111111111111"


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


fixture = json.loads(
    (
        ROOT / "tests/harness/fixtures/v2.6.3-missing-review-submit.json"
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


class RecoveryWorker(RuntimeWorkerLivenessMixin):
    def inspect_callback(self) -> None:
        raise AssertionError("missing-artifact recovery must not ingest a callback")


with tempfile.TemporaryDirectory(prefix="review-input-runtime.") as raw:
    root = Path(raw)
    scratch = (root / "scratch").resolve()
    product = (root / "product").resolve()
    state_root = (root / "worker-state").resolve()
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
    store.create(child, lane_id="openai-holistic", run_id="review-round-run-1")
    for operation_id in ("review-parent-1", "review-round-1"):
        for state in ("preflight", "starting", "running", "awaiting-callback"):
            store.transition("review-owner-1", operation_id, state)
    worker = FastPathWorker()
    worker.spec_path = state_root / "launch.json"
    worker.spec = {
        "owner_id": "review-owner-1",
        "operation_id": "review-parent-1",
        "run_id": "review-parent-run-1",
        "cwd": scratch,
        "product_root": product,
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
        accepted,
    )


with tempfile.TemporaryDirectory(prefix="review-submit-nudge-runtime.") as raw:
    root = Path(raw)
    scratch = (root / "scratch").resolve()
    product = (root / "product").resolve()
    state_root = (root / "worker-state").resolve()
    scratch.mkdir()
    product.mkdir()
    state_root.mkdir()
    callback_dir = scratch / "callbacks" / "openai-holistic"
    callback_dir.mkdir(parents=True)
    callback_path = callback_dir / ".review-callback.json"
    registration = state_root / "callback-target.json"
    registration.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation": 3,
                "operation_id": "review-round-3",
                "run_id": "review-round-run-3",
                "callback_pointer": str(callback_path),
            }
        ),
        encoding="utf-8",
    )
    now = time.time()
    os.utime(registration, (now - 60, now - 60))
    route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "high", "reviewer-callback", "f" * 64
    )
    store = OperationStore(root / "store")
    parent = OperationSpec(
        "review-parent-3",
        "review-parent-key-3",
        "review-session",
        "review-owner-3",
        route,
        "packets/review.json",
        "scoped",
    )
    store.create(parent, lane_id="openai-holistic", run_id="review-parent-run-3")
    supervisor = OperationSupervisor(store, "review-owner-3", "review-parent-3")
    supervisor.configure_budget(
        attempt_limit=1,
        model_restart_limit=0,
        time_budget_seconds=300,
        token_limit=100,
        now=now,
    )
    for state in ("preflight", "starting"):
        store.transition("review-owner-3", "review-parent-3", state)
    supervisor.bind_resources(
        OwnedResources(
            SURFACE,
            321,
            322,
            "1" * 64,
            "2" * 64,
        )
    )
    for state in ("running", "awaiting-callback"):
        store.transition("review-owner-3", "review-parent-3", state)

    class Process:
        def process_status(self, process_group: int, identity: str) -> str:
            check(
                "recovery process probe uses exact ownership",
                process_group == 321 and identity == "1" * 64,
            )
            return "alive"

    class Cmux:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str]] = []
            self.keys: list[tuple[str, str]] = []

        def status(self, surface_id: str) -> str:
            check("recovery surface probe uses exact ownership", surface_id == SURFACE)
            return "alive"

        def send(self, surface_id: str, message: str) -> None:
            self.sent.append((surface_id, message))

        def send_key(self, surface_id: str, key: str) -> None:
            self.keys.append((surface_id, key))

    cmux = Cmux()
    worker = RecoveryWorker()
    worker.spec_path = state_root / "launch.json"
    worker.spec = {
        "owner_id": "review-owner-3",
        "operation_id": "review-parent-3",
        "run_id": "review-parent-run-3",
        "cwd": scratch,
        "product_root": product,
        "callback_registration": registration,
        "surface_id": SURFACE,
        "runtime": "codex",
    }
    worker.store = store
    worker.process = Process()
    worker.handle = SimpleNamespace(process_group=321, process_identity="1" * 64)
    worker.provider_exited = False
    worker.cmux_adapter = cmux
    worker.clock = lambda: now
    worker.liveness_policy = LivenessPolicy.default()
    worker.callback_submit_policy = CallbackSubmitPolicy(30, 60, 1)
    worker.liveness_controller = LivenessController(state_root / "liveness")
    worker.latest_callback_prompt_class = "idle-prompt"
    worker.callback_idle_observations = 0
    worker.callback_prompt_observations = 0
    worker.callback_generation_identity = ""
    worker.callback_generation_progress_at = 0.0
    worker.callback_recovery_input_digest = ""
    worker.callback_recovery_input_reads = 0
    worker.callback_recovery_digest = ""
    worker.callback_recovery_reads = 0
    worker.trusted_vault = ROOT
    worker.inspect_liveness()
    worker.inspect_liveness()
    worker.inspect_liveness()
    recovery_state = worker.liveness_controller.current_state()
    check(
        "runtime reserves and sends one same-session submit-only nudge",
        len(cmux.sent) == 1
        and cmux.keys == [(SURFACE, "Enter")]
        and ".review-input.json" in cmux.sent[0][1]
        and "review_submit.py" in cmux.sent[0][1]
        and recovery_state is not None
        and recovery_state.nudge_count == 1
        and recovery_state.callback_submit_status == "sent",
        (cmux.sent, cmux.keys, recovery_state),
    )


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
