#!/usr/bin/env python3
"""Causal fixtures and gate wiring for one exact-HEAD review attempt."""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.callbacks import CallbackBroker  # noqa: E402
from harness.contracts import OwnedResources, RuntimeRoute  # noqa: E402
from harness.review_attempt import ReviewAttemptError  # noqa: E402
from harness.review_program import ReviewBoundaryInput  # noqa: E402
from harness.review_program_resolution import (  # noqa: E402
    resolved_terminal_head,
)
from harness.store import OperationStore  # noqa: E402
from harness.workflows.review import (  # noqa: E402
    ReviewContext,
    ReviewFinding,
    ReviewOperationRequest,
    ReviewResult,
)
from harness.workflows.review_gate import (  # noqa: E402
    ReviewGateController,
    ReviewPreset,
)
from task_review_flow import _run_exact_head_review  # noqa: E402
from task_review_resolution_flow import (  # noqa: E402
    legacy_cross_head_resume_disabled,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


@dataclass(frozen=True)
class SessionResult:
    record: object
    checkpoint: str
    action: str = ""
    process_status: str = ""
    surface_status: str = ""
    checkpoint_sha256: str = ""


class FakeRuntime:
    """Count every provider-facing action around the real operation store."""

    def __init__(self, store: OperationStore) -> None:
        self.store = store
        self.started = 0
        self.accepted = 0
        self.continued = 0
        self.rearmed = 0

    def start(self, request: object, *, on_surface_opened=None) -> SessionResult:
        self.started += 1
        record = self.store.create(
            request.spec, lane_id=request.lane_id, run_id=request.run_id
        )
        if not record.resources.surface_id:
            record = replace(
                record,
                resources=replace(
                    record.resources,
                    surface_id=f"surface-{self.started}",
                ),
                revision=record.revision + 1,
            )
            self.store.save(record, expected_revision=record.revision - 1)
        result = SessionResult(record, "checkpoint-1")
        if on_surface_opened is not None:
            on_surface_opened(result)
        return result

    def register_callback_target(self, *_args: object) -> None:
        return None

    def accept_callback(self, envelope: object) -> object:
        self.accepted += 1
        return CallbackBroker(self.store, "task-1").accept(envelope)

    def request_exit(self, owner_id: str, operation_id: str) -> object:
        record = self.store.read(owner_id, operation_id)
        if record.state not in {"complete", "failed", "cancelled"}:
            if record.state in {
                "created",
                "preflight",
                "starting",
                "attention-required",
            }:
                self.store.transition(owner_id, operation_id, "cancelling")
            elif record.state != "finalizing":
                self.store.transition(owner_id, operation_id, "finalizing")
            self.store.transition(owner_id, operation_id, "exiting")
        return self.store.read(owner_id, operation_id)

    def cleanup(self, owner_id: str, operation_id: str) -> object:
        record = self.store.read(owner_id, operation_id)
        if record.state == "exiting":
            self.store.transition(owner_id, operation_id, "complete")
        completed = self.store.read(owner_id, operation_id)
        if completed.resources != OwnedResources():
            self.store.save(
                replace(
                    completed,
                    resources=OwnedResources(),
                    revision=completed.revision + 1,
                ),
                expected_revision=completed.revision,
            )
        return self.store.read(owner_id, operation_id)

    def continue_session(self, *_args: object) -> object:
        self.continued += 1
        raise AssertionError("exact-HEAD attempts cannot continue a session")

    def rearm_callback_timeout(self, *_args: object) -> object:
        self.rearmed += 1
        raise AssertionError("exact-HEAD attempts cannot rearm a session")


class FailingStartRuntime(FakeRuntime):
    def start(self, request: object, *, on_surface_opened=None) -> SessionResult:
        self.started += 1
        raise RuntimeError("provider start failed")


BOUNDARY = ReviewBoundaryInput(
    purpose="implementation",
    outcome_contract_sha256="2" * 64,
    plan_sha256="1" * 64,
    product_head_sha="a" * 40,
    verification_evidence_sha256="3" * 64,
    verification_evidence_path="docs/verification.md",
)


def request(head: str) -> ReviewOperationRequest:
    context = ReviewContext(
        manifest="packets/review/manifest.json",
        head_sha=head,
        verification_profile="scoped",
        verification_profile_sha256="8" * 64,
        purpose="implementation",
        boundary_input_sha256=BOUNDARY.input_sha256,
    )
    route = RuntimeRoute(
        "codex",
        "gpt-5.6-sol",
        "xhigh",
        "reviewer-callback",
        "7" * 64,
    )
    policy = ReviewPreset.from_flags(
        deep=True, runtime="codex", model="sol", effort="xhigh"
    ).request(
        "review-1",
        purpose="implementation",
        selected_provider="openai",
    )
    return ReviewOperationRequest(policy, "task-1", route, context)


fixture_root = ROOT / "tests/fixtures/v2.6.5"
d264 = json.loads(
    (fixture_root / "d264-73-terminal-parent-child.json").read_text()
)
stale = json.loads(
    (fixture_root / "stale-exiting-resource-gone.json").read_text()
)
check(
    "D-264-73 fixture reproduces child-after-terminal-parent causality",
    [row["fact"] for row in d264["interleaving"]]
    == ["parent-terminal", "head-changed", "verification-child-prepared"]
    and d264["interleaving"][-1]["verification_iteration"] == 1
    and "R1-cross-head-continuity" in d264["root_classes"],
)
check(
    "stale-exiting fixture separates physical loss from durable close",
    stale["interleaving"][-1]["operation_state"] == "exiting"
    and stale["interleaving"][-1]["resource_closed_event"] is False
    and stale["expected_root_class"] in stale["root_classes"],
)
exact_flow_source = inspect.getsource(_run_exact_head_review)
check(
    "new task flow has a zero call graph to cross-HEAD continuation",
    all(
        forbidden not in exact_flow_source
        for forbidden in (
            "_continue_resolution",
            "_preload_resolution_bundle",
            "_finalizing_resubmit_recovery",
            "continue_after_resolution",
            "rebind",
            "recovery",
        )
    )
    and legacy_cross_head_resume_disabled()["provider_effect_allowed"] is False,
)

with tempfile.TemporaryDirectory(prefix="exact-head-attempt.") as raw:
    base = Path(raw)
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    gate = ReviewGateController(base / "gate", runtime, store)
    initial = request("a" * 40)
    run = gate.begin_attempt(
        dispatch_operation_id="task-1",
        finalization_lineage_id="task-1",
        cycle=1,
        plan_sha256="1" * 64,
        outcome_sha256="2" * 64,
        request=initial,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=base,
        product_root=ROOT,
        prompt_pointer="prompts/review.md",
        callback_root="callbacks/review-1",
    )
    initial_operation_ids = {
        record.spec.operation_id for record in store.list("task-1")
    }
    started_before_stale = runtime.started
    try:
        gate.begin_attempt(
            dispatch_operation_id="task-1",
            finalization_lineage_id="task-1",
            cycle=1,
            plan_sha256="1" * 64,
            outcome_sha256="2" * 64,
            request=request("b" * 40),
            origin_surface="11111111-1111-4111-8111-111111111111",
            cwd=base,
            product_root=ROOT,
            prompt_pointer="prompts/review.md",
            callback_root="callbacks/review-1",
        )
    except ReviewAttemptError:
        pass
    else:
        raise AssertionError("changed HEAD reused the attempt")
    check(
        "changed HEAD is rejected before another provider start",
        runtime.started == started_before_stale,
    )

    lane = run.execution.lanes[0]
    round_ = run.rounds[lane.axis]
    try:
        gate.complete_attempt_round(
            run,
            lane,
            round_,
            ReviewResult(lane.axis, "approve", (), 1),
        )
    except ReviewAttemptError:
        pass
    else:
        raise AssertionError("verification iteration entered the attempt")
    check(
        "verification iteration is rejected before callback/provider effects",
        runtime.accepted == 0 and runtime.continued == 0,
    )

    finding = ReviewFinding(
        "F-1",
        lane.axis,
        "important",
        "cross-HEAD continuation is unsafe",
        "the causal fixture prepares a child after terminal parent state",
    )
    first_decision = gate.complete_attempt_round(
        run,
        lane,
        round_,
        ReviewResult(lane.axis, "changes-requested", (finding,), 0),
    )
    second_lane = run.execution.lanes[1]
    decision = gate.complete_attempt_round(
        run,
        second_lane,
        run.rounds[second_lane.axis],
        ReviewResult(second_lane.axis, "approve", (), 0),
    )
    state = gate.read()
    check(
        "material findings terminalize the exact attempt without a child",
        first_decision.action == "awaiting-axes"
        and decision.action == "changes-requested"
        and state["status"] == "changes-requested"
        and state["attempt"]["terminal"]["result"] == "changes-requested"
        and {
            record.spec.operation_id for record in store.list("task-1")
        }
        == initial_operation_ids
        and "awaiting_resolution" not in state
        and "continuation_effects" not in state
        and runtime.continued == 0
        and runtime.rearmed == 0,
    )
    check(
        "review-program authority keeps the attempt on its original HEAD",
        resolved_terminal_head(
            ROOT, gate.root, state, BOUNDARY, "review-1"
        )
        == "a" * 40,
    )
    try:
        resolved_terminal_head(
            ROOT,
            gate.root,
            state,
            replace(BOUNDARY, product_head_sha="b" * 40),
            "review-1",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("review-program accepted a cross-HEAD attempt")
    check("review-program rejects cross-HEAD attempt authority", True)

with tempfile.TemporaryDirectory(prefix="failed-exact-head-attempt.") as raw:
    base = Path(raw)
    store = OperationStore(base / "store")
    runtime = FailingStartRuntime(store)
    gate = ReviewGateController(base / "gate", runtime, store)
    try:
        gate.begin_attempt(
            dispatch_operation_id="task-1",
            finalization_lineage_id="task-1",
            cycle=1,
            plan_sha256="1" * 64,
            outcome_sha256="2" * 64,
            request=request("a" * 40),
            origin_surface="11111111-1111-4111-8111-111111111111",
            cwd=base,
            product_root=ROOT,
            prompt_pointer="prompts/review.md",
            callback_root="callbacks/review-1",
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("provider start failure was hidden")
    failed_state = gate.read()
    started_before_replay = runtime.started
    try:
        gate.begin_attempt(
            dispatch_operation_id="task-1",
            finalization_lineage_id="task-1",
            cycle=1,
            plan_sha256="1" * 64,
            outcome_sha256="2" * 64,
            request=request("a" * 40),
            origin_surface="11111111-1111-4111-8111-111111111111",
            cwd=base,
            product_root=ROOT,
            prompt_pointer="prompts/review.md",
            callback_root="callbacks/review-1",
        )
    except ReviewAttemptError:
        pass
    else:
        raise AssertionError("failed attempt was rearmed")
    check(
        "provider start failure terminalizes once without replay",
        failed_state["status"] == "attention-required"
        and failed_state["attempt"]["status"] == "terminal"
        and failed_state["attempt"]["terminal"]["result"]
        == "attention-required"
        and runtime.started == started_before_replay,
    )

print("\nAll exact-HEAD ReviewAttempt gate tests passed.")
