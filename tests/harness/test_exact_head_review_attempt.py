#!/usr/bin/env python3
"""Causal fixtures and gate wiring for one exact-HEAD review attempt."""

from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.callbacks import CallbackBroker  # noqa: E402
from harness import cli as harness_cli  # noqa: E402
from harness.contracts import (  # noqa: E402
    AttentionReason,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
    to_dict,
)
from harness.review_attempt import (  # noqa: E402
    LEGACY_CROSS_HEAD_RESUME_DISABLED,
    ReviewAttemptError,
)
from harness.finalization_ledger import predecessor_bound_attempt_id  # noqa: E402
from harness.runtime_session_contracts import (  # noqa: E402
    RuntimeCheckpointEvidenceMissing,
)
from harness.review_program import (  # noqa: E402
    ReviewBoundaryInput,
    compile_review_program,
    reconcile_review_program,
)
from harness.review_program_authority import trusted_review_receipt  # noqa: E402
from harness.review_program_resolution import (  # noqa: E402
    resolved_terminal_head,
)
from harness.runtime_worker_review_bridge import (  # noqa: E402
    publish_review_resolution_transport,
)
from harness.store import OperationStore  # noqa: E402
from harness.verification import load_profiles  # noqa: E402
from harness.workflows.review import (  # noqa: E402
    ReviewContext,
    ReviewFinding,
    ReviewLaneSession,
    ReviewOperationRequest,
    ReviewResult,
    prepare_review_round,
    review_round_envelope,
)
from harness.workflows.review_gate import (  # noqa: E402
    ReviewGateController,
    ReviewPreset,
    authorize_task_finalization,
)
from task_review_flow import (  # noqa: E402
    EXACT_HEAD_REVIEW_PROTOCOL,
    _run_exact_head_review,
    _run_review,
)
from approved_plan_snapshot import bind_approved_plan_snapshot  # noqa: E402
from outcome_contract import extract_from_bytes  # noqa: E402
from review_resolution import review_transport_identity_sha256  # noqa: E402
from task_plan_authority import record_plan_amendment  # noqa: E402
from task_review_transport import _write_round_meta  # noqa: E402


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def write_scoped_verification(product: Path, summary: Path, head: str) -> None:
    profile = load_profiles(product / "config/verification-profiles.toml")["scoped"]
    output_root = product / ".vault-meta/review-evidence"
    output_root.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, _command in enumerate(profile.commands, start=1):
        output = output_root / f"scoped-{index}.log"
        payload = f"scoped output {index}\n".encode()
        output.write_bytes(payload)
        rows.append(
            {
                "command_id": f"scoped-{index}",
                "cwd": ".",
                "exit_code": 0,
                "finished_at": "2026-08-07T00:00:01Z",
                "head_sha": head,
                "output_bytes": len(payload),
                "output_pointer": output.relative_to(product).as_posix(),
                "output_sha256": hashlib.sha256(payload).hexdigest(),
                "profile": profile.name,
                "profile_sha256": profile.sha256,
                "schema_version": 2,
                "started_at": "2026-08-07T00:00:00Z",
            }
        )
    summary.write_text(json.dumps(rows, sort_keys=True) + "\n", encoding="utf-8")


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

    def __init__(
        self, store: OperationStore, owner_id: str = "task-1"
    ) -> None:
        self.store = store
        self.owner_id = owner_id
        self.started = 0
        self.accepted = 0
        self.continued = 0
        self.rearmed = 0
        self.checkpoint = "checkpoint-1"

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

    def status(self, owner_id: str, operation_id: str) -> SessionResult:
        return SessionResult(
            self.store.read(owner_id, operation_id), self.checkpoint
        )

    def hydrate_durable_checkpoint(
        self, owner_id: str, operation_id: str, _lane_id: str
    ) -> SessionResult:
        if not self.checkpoint:
            raise RuntimeCheckpointEvidenceMissing(
                "the exact reviewer checkpoint was never materialized"
            )
        return SessionResult(
            self.store.read(owner_id, operation_id),
            self.checkpoint,
            checkpoint_sha256=hashlib.sha256(
                self.checkpoint.encode()
            ).hexdigest(),
        )

    def accept_callback(self, envelope: object) -> object:
        self.accepted += 1
        return CallbackBroker(self.store, self.owner_id).accept(envelope)

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


class FakeCmux:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.keys: list[tuple[str, str]] = []

    def send(self, surface_id: str, message: str) -> None:
        self.messages.append((surface_id, message))

    def send_key(self, surface_id: str, key: str) -> None:
        self.keys.append((surface_id, key))


BOUNDARY = ReviewBoundaryInput(
    purpose="implementation",
    outcome_contract_sha256="2" * 64,
    plan_sha256="1" * 64,
    product_head_sha="a" * 40,
    verification_evidence_sha256="3" * 64,
    verification_evidence_path="docs/verification.md",
)


def request(
    head: str,
    *,
    operation_id: str = "review-1",
    owner_id: str = "task-1",
    boundary_input_sha256: str = BOUNDARY.input_sha256,
) -> ReviewOperationRequest:
    context = ReviewContext(
        manifest="packets/review/manifest.json",
        head_sha=head,
        verification_profile="scoped",
        verification_profile_sha256="8" * 64,
        purpose="implementation",
        boundary_input_sha256=boundary_input_sha256,
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
        operation_id,
        purpose="implementation",
        selected_provider="openai",
    )
    return ReviewOperationRequest(policy, owner_id, route, context)


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


def advance(
    store: OperationStore,
    owner_id: str,
    operation_id: str,
    *states: str,
) -> None:
    for state in states:
        store.transition(owner_id, operation_id, state)


with tempfile.TemporaryDirectory(prefix="causal-review-fixtures.") as raw:
    base = Path(raw)
    store = OperationStore(base / "store")
    route = RuntimeRoute(
        "codex",
        "gpt-5.6-sol",
        "xhigh",
        "reviewer-callback",
        "7" * 64,
    )
    parent_spec = OperationSpec(
        operation_id="legacy-parent",
        idempotency_key="legacy-parent-key",
        kind="deep-review-correctness",
        owner_id="legacy-owner",
        route=route,
        context_manifest="packets/legacy/manifest.json",
        verification_profile="scoped",
    )
    parent = store.create(
        parent_spec, lane_id="legacy-lane", run_id="legacy-run"
    )
    advance(
        store,
        parent.spec.owner_id,
        parent.spec.operation_id,
        "preflight",
        "starting",
        "running",
        "awaiting-callback",
    )
    store.transition(
        parent.spec.owner_id,
        parent.spec.operation_id,
        "attention-required",
        reason=AttentionReason.CALLBACK_TIMEOUT,
    )
    terminal_parent = store.read(
        parent.spec.owner_id, parent.spec.operation_id
    )
    legacy_lane = ReviewLaneSession(
        axis="openai-engineering",
        owner_id=parent.spec.owner_id,
        operation_id=parent.spec.operation_id,
        lane_id=parent.lane_id,
        run_id=parent.run_id,
        surface_id="legacy-surface",
        checkpoint="legacy-checkpoint",
        spec=parent.spec,
        verification_iteration=1,
        max_verify_iterations=2,
        state=terminal_parent.state,
    )
    legacy_child = prepare_review_round(store, legacy_lane)
    child_record = store.read(
        legacy_child.owner_id, legacy_child.operation_id
    )
    replayed_d264 = [
        {
            "fact": "parent-terminal",
            "parent_state": terminal_parent.state,
            "attention_reason": terminal_parent.attention_reason.value,
        },
        {"fact": "head-changed", "head": d264["resolved_head"]},
        {
            "fact": "verification-child-prepared",
            "child_state": child_record.state,
            "verification_iteration": legacy_child.verification_iteration,
        },
    ]
    check(
        "D-264-73 executable fixture creates the historical store interleaving",
        replayed_d264
        == [
            {
                "fact": item["fact"],
                **(
                    {
                        "parent_state": item["parent_state"],
                        "attention_reason": item["attention_reason"],
                    }
                    if item["fact"] == "parent-terminal"
                    else {"head": item["head"]}
                    if item["fact"] == "head-changed"
                    else {
                        "child_state": item["child_state"],
                        "verification_iteration": item[
                            "verification_iteration"
                        ],
                    }
                ),
            }
            for item in d264["interleaving"]
        ],
    )

    stale_parent_spec = replace(
        parent_spec,
        operation_id="stale-parent",
        idempotency_key="stale-parent-key",
        owner_id="stale-owner",
    )
    stale_parent = store.create(
        stale_parent_spec, lane_id="stale-lane", run_id="stale-parent-run"
    )
    advance(
        store,
        stale_parent.spec.owner_id,
        stale_parent.spec.operation_id,
        "preflight",
        "starting",
        "running",
        "awaiting-callback",
    )
    exiting_spec = replace(
        stale_parent_spec,
        operation_id="stale-child",
        idempotency_key="stale-child-key",
        kind="review-round",
        parent_operation_id=stale_parent.spec.operation_id,
    )
    exiting = store.create(
        exiting_spec, lane_id="stale-lane", run_id="stale-child-run"
    )
    advance(
        store,
        exiting.spec.owner_id,
        exiting.spec.operation_id,
        "preflight",
        "starting",
        "running",
        "finalizing",
        "exiting",
    )
    stale_record = store.read(exiting.spec.owner_id, exiting.spec.operation_id)
    live_parent = store.read(
        stale_parent.spec.owner_id, stale_parent.spec.operation_id
    )
    check(
        "stale-exiting executable fixture preserves resource-free nonterminal state",
        stale_record.state == stale["interleaving"][-1]["operation_state"]
        and stale_record.resources == OwnedResources()
        and live_parent.state == stale["interleaving"][-1]["parent_state"]
        and stale_record.state not in {"complete", "failed", "cancelled"},
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
    and LEGACY_CROSS_HEAD_RESUME_DISABLED.provider_effect_allowed is False,
)

with tempfile.TemporaryDirectory(prefix="exact-head-attempt.") as raw:
    base = Path(raw)
    store = OperationStore(base / ".vault-meta/harness")
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
    try:
        resolved_terminal_head(
            base, gate.root, gate.read(), BOUNDARY, "review-1"
        )
    except ValueError:
        pass
    else:
        raise AssertionError("nonterminal attempt minted trusted authority")
    check("review-program rejects nonterminal attempt authority", True)
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
        and not state.get("awaiting_resolution")
        and "continuation_effects" not in state
        and runtime.continued == 0
        and runtime.rearmed == 0,
    )
    check(
        "review-program authority keeps the attempt on its original HEAD",
        resolved_terminal_head(
            base, gate.root, state, BOUNDARY, "review-1"
        )
        == "a" * 40,
    )
    try:
        resolved_terminal_head(
            base,
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


with tempfile.TemporaryDirectory(prefix="exact-head-input-rollover.") as raw:
    base = Path(raw)
    store = OperationStore(base / ".vault-meta/harness")
    runtime = FakeRuntime(store)
    gate = ReviewGateController(base / "gate", runtime, store)

    def start_with_input(
        cycle: int, head: str, operation_id: str
    ) -> object:
        active_request = request(head, operation_id=operation_id)

        def prepare_lane(
            _axis: str,
            _session_request: object,
            _result: object,
            round_: object,
        ) -> None:
            _write_round_meta(
                runtime_root=base,
                vault=base,
                worktree=ROOT,
                task_id="task-1",
                depth="deep",
                context=active_request.context,
                lane_operation_id=round_.parent_operation_id,
                round_=round_,
            )

        return gate.begin_attempt(
            dispatch_operation_id="task-1",
            finalization_lineage_id="task-1",
            cycle=cycle,
            plan_sha256="1" * 64,
            outcome_sha256="2" * 64,
            request=active_request,
            origin_surface="11111111-1111-4111-8111-111111111111",
            cwd=base,
            product_root=ROOT,
            prompt_pointer="prompts/review.md",
            callback_root="callbacks",
            prepare_lane=prepare_lane,
        )

    first = start_with_input(1, "a" * 40, "review-rollover-1")
    original_meta = {}
    original_input = {}
    for index, lane in enumerate(first.execution.lanes):
        axis_root = base / "callbacks" / lane.axis
        input_path = axis_root / ".review-input.json"
        value = {
            "schema_version": 1,
            "axis": lane.axis,
            "verdict": "changes-requested" if index == 0 else "approve",
            "verification_iteration": 0,
            "findings": [],
        }
        input_path.write_text(
            json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
        )
        original_meta[lane.axis] = (
            axis_root / ".review-meta.json"
        ).read_bytes()
        original_input[lane.axis] = input_path.read_bytes()
        gate.complete_attempt_round(
            first,
            lane,
            first.rounds[lane.axis],
            ReviewResult(
                lane.axis,
                "changes-requested" if index == 0 else "approve",
                (
                    ReviewFinding(
                        "rollover-finding",
                        lane.axis,
                        "important",
                        "retire the prior attempt scratch",
                        "the next attempt requires fresh metadata",
                    ),
                )
                if index == 0
                else (),
                0,
            ),
        )

    axes = tuple(sorted(original_input))
    paths = {
        axis: base / "callbacks" / axis / ".review-input.json"
        for axis in axes
    }
    paths[axes[0]].write_bytes(original_input[axes[1]])
    paths[axes[1]].write_bytes(original_input[axes[0]])
    starts_before = runtime.started
    try:
        start_with_input(2, "b" * 40, "review-rollover-2")
    except ReviewAttemptError:
        pass
    else:
        raise AssertionError("cross-axis review input substitution rolled over")
    check(
        "cross-axis review input substitution fails before provider start",
        runtime.started == starts_before
        and gate.read()["attempt"]["identity"]["cycle"] == 1,
    )
    for axis in axes:
        paths[axis].write_bytes(original_input[axis])

    archive = (
        gate.root
        / "attempts"
        / "attempt-review-rollover-1-review-input"
    )
    archive.mkdir(parents=True)
    duplicate = archive / f"{axes[0]}.review-input.json"
    duplicate.write_bytes(original_input[axes[0]])
    try:
        start_with_input(2, "b" * 40, "review-rollover-2")
    except ReviewAttemptError:
        pass
    else:
        raise AssertionError("same-attempt review input substitution rolled over")
    check(
        "same-attempt scratch substitution fails before provider start",
        runtime.started == starts_before,
    )
    duplicate.unlink()

    second = start_with_input(2, "b" * 40, "review-rollover-2")
    check(
        "terminal input rollover preserves prior authority and starts fresh",
        gate.read()["attempt"]["identity"]["cycle"] == 2
        and runtime.started == starts_before + 2
        and all(
            (archive / f"{axis}.review-meta.json").read_bytes()
            == original_meta[axis]
            and (archive / f"{axis}.review-input.json").read_bytes()
            == original_input[axis]
            for axis in axes
        )
        and all(
            (base / "callbacks" / lane.axis / ".review-meta.json").is_file()
            and (
                base / "callbacks" / lane.axis / ".review-input.json"
            ).is_file()
            and json.loads(
                (
                    base / "callbacks" / lane.axis / ".review-input.json"
                ).read_text(encoding="utf-8")
            )["axis"]
            == lane.axis
            for lane in second.execution.lanes
        ),
    )


with tempfile.TemporaryDirectory(
    prefix="exact-head-accepted-checkpointless."
) as raw:
    base = Path(raw)
    store = OperationStore(base / ".vault-meta/harness")
    runtime = FakeRuntime(store)
    gate = ReviewGateController(base / "gate", runtime, store)
    active = gate.begin_attempt(
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
        callback_root="callbacks/review-accepted",
    )
    state = gate.read()
    gate._replace(
        lanes=[
            {
                key: value
                for key, value in lane.items()
                if key not in {"checkpoint", "checkpoint_sha256"}
            }
            | {"checkpoint": ""}
            for lane in state["lanes"]
        ]
    )
    runtime.checkpoint = ""
    started_before = runtime.started
    try:
        gate.rehydrate_attempt()
    except (ReviewAttemptError, RuntimeCheckpointEvidenceMissing):
        pass
    else:
        raise AssertionError("checkpointless unaccepted attempt rehydrated")
    check(
        "checkpointless unaccepted attempt stays fail-closed",
        runtime.started == started_before,
    )

    results = {}
    accepted_identities = {}
    for index, lane in enumerate(active.execution.lanes):
        finding = ReviewFinding(
            f"accepted-{index}",
            lane.axis,
            "important",
            "the accepted callback must survive checkpoint loss",
            "the durable child receipt is the recovery authority",
        )
        result = ReviewResult(
            lane.axis,
            "changes-requested" if index == 0 else "approve",
            (finding,) if index == 0 else (),
            0,
        )
        round_ = active.rounds[lane.axis]
        envelope = review_round_envelope(round_, result)
        CallbackBroker(store, "task-1").accept(envelope)
        child = store.read("task-1", round_.operation_id)
        results[lane.axis] = result
        accepted_identities[lane.axis] = (
            child.accepted_callback_id,
            child.accepted_callback_sha256,
        )
    recovered = gate.rehydrate_attempt()
    check(
        "two exact accepted callbacks rehydrate without reviewer checkpoints",
        runtime.started == started_before
        and {
            lane.axis: (
                store.read("task-1", recovered.rounds[lane.axis].operation_id)
                .accepted_callback_id,
                store.read("task-1", recovered.rounds[lane.axis].operation_id)
                .accepted_callback_sha256,
            )
            for lane in recovered.execution.lanes
        }
        == accepted_identities,
    )
    missing_response = base / ".task-verification-response.json"
    check(
        "zero-lane preflight attention routes to exact code-owned recovery",
        harness_cli._review_recovery_kind(
            {
                "status": "attention-required",
                "execution_protocol": "exact-head-attempt-v1",
                "lanes": [],
                "round_results": {},
                "final_results": {},
                "evidence": {},
                "attempt": {
                    "status": "terminal",
                    "terminal": {
                        "result": "attention-required",
                        "lane_results": [],
                    },
                },
            },
            missing_response,
        )
        == "zero-lane-preflight",
    )
    check(
        "two accepted callbacks route before the legacy response precondition",
        harness_cli._review_recovery_kind(
            gate.read(), missing_response
        )
        == "accepted-exact-callbacks"
        and not missing_response.exists(),
    )
    legacy_gate = {
        "status": "verifying",
        "execution_protocol": "legacy",
    }
    check(
        "legacy recovery still requires its verification response",
        harness_cli._review_recovery_kind(
            legacy_gate, missing_response
        )
        == "",
    )
    missing_response.write_text("{}\n", encoding="utf-8")
    check(
        "legacy recovery remains eligible with its verification response",
        harness_cli._review_recovery_kind(
            legacy_gate, missing_response
        )
        == "legacy-finalizing",
    )
    missing_response.unlink()
    decisions = []
    for lane in recovered.execution.lanes:
        decisions.append(
            gate.complete_attempt_round(
                recovered,
                lane,
                recovered.rounds[lane.axis],
                results[lane.axis],
            ).action
        )
    recovered_state = gate.read()
    check(
        "accepted checkpointless callback replay terminalizes without effects",
        decisions == ["awaiting-axes", "changes-requested"]
        and recovered_state["status"] == "changes-requested"
        and recovered_state["attempt"]["terminal"]["result"]
        == "changes-requested"
        and runtime.started == started_before
        and runtime.continued == 0
        and runtime.rearmed == 0,
    )
    packet_path = base / ".task-review.json"
    check(
        "accepted callback crash prefix has no executor packet",
        not packet_path.exists(),
    )
    runtime_spec_path = base / "runtime/launch.json"
    runtime_spec_path.parent.mkdir(parents=True)
    cmux = FakeCmux()
    publish_review_resolution_transport(
        gate_state=recovered_state,
        gate_root=gate.root,
        worktree=base,
        operation_id="task-1",
        surface_id="task-surface",
        summary_sha256="d" * 64,
        runtime_spec_path=runtime_spec_path,
        cmux_adapter=cmux,
    )
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    check(
        "exact accepted callbacks materialize one typed executor packet",
        packet["reviewed_head_sha"] == "a" * 40
        and len(packet["review_callbacks"]) == 2
        and {
            row["axis"]: (
                row["callback_id"], row["callback_sha256"]
            )
            for row in packet["review_callbacks"]
        }
        == accepted_identities
        and packet["material_finding_ids"]
        == [f"{active.execution.lanes[0].axis}:accepted-0"]
        and len(cmux.messages) == 1
        and cmux.keys == [("task-surface", "Enter")]
        and runtime.started == started_before,
    )

with tempfile.TemporaryDirectory(prefix="exact-protocol-selector.") as raw:
    base = Path(raw)
    vault = base / "vault"
    product = base / "product"
    runtime_root = base / "runtime"
    (vault / "config").mkdir(parents=True)
    (vault / "skills/review").mkdir(parents=True)
    product.mkdir()
    runtime_root.mkdir()
    (vault / "config/model-routing.toml").write_bytes(
        (ROOT / "config/model-routing.toml").read_bytes()
    )
    (vault / "config/verification-profiles.toml").write_bytes(
        (ROOT / "config/verification-profiles.toml").read_bytes()
    )
    (vault / "skills/review/SKILL.md").write_text(
        "# Review\n\nInspect only the exact ContextPacket.\n",
        encoding="utf-8",
    )
    plan = vault / "approved-plan.md"
    plan.write_text(
        """# Approved exact-HEAD task

## Outcome Contract

```json
{"schema_version":1,"purpose":"Exercise the exact review path.","desired_outcome":"A schema-valid v4 policy reserves one exact attempt.","success_evidence":[{"evidence_id":"exact-review","observable":"The public flow uses the ledger."}],"non_goals":["No external effect."]}
```
""",
        encoding="utf-8",
    )
    (product / "product.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=product,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "review@example.invalid"],
        cwd=product,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Protocol Selector Test"],
        cwd=product,
        check=True,
    )
    subprocess.run(["git", "add", "product.py"], cwd=product, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial product"],
        cwd=product,
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    profile_sha = load_profiles(
        vault / "config/verification-profiles.toml"
    )["scoped"].sha256
    task_id = "11111111-1111-4111-8111-111111111111"
    outcome_sha = extract_from_bytes(plan.read_bytes()).sha256
    (vault / ".vault-meta").mkdir(exist_ok=True)
    plan_binding = bind_approved_plan_snapshot(
        {"vault_root": vault.resolve(), "plan_file": plan.resolve()}
    )
    (product / ".task-summary.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "type": "repo-touch",
                "title": "Exact selector",
                "session": "selector-session",
                "body": "The exact selector fixture is ready.",
                "outcome_disposition": "achieved",
                "outcome_evidence_ids": ["exact-review"],
                "residual_gap_pointers": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    meta = {
        "version": 4,
        "task_id": task_id,
        "task_name": "exact selector",
        "task_surface": "22222222-2222-4222-8222-222222222222",
        "worktree": str(product.resolve()),
        "vault_root": str(vault.resolve()),
        "plan_file": str(plan),
        "plan_snapshot_file": str(plan_binding["_approved_plan_file"]),
        "approved_plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "outcome_contract_sha256": outcome_sha,
        "finalization_policy": {
            "max_cycles": 5,
            "add_independent_model_after": 3,
            "execution": "ephemeral",
            "primary_route_alias": "finalization-primary",
            "independent_route_alias": "finalization-independent",
        },
        "routing": {
            "session": {
                "runtime": "codex",
                "model": "gpt-5.6-sol",
                "effort": "xhigh",
                "source": "fixture",
            }
        },
        "review_policy": {
            "mode": "simple",
            "cross_model": False,
            "runtime": "codex",
            "model": "sol",
            "effort": "xhigh",
            "max_verify_iterations": 1,
            "verification_profile": "scoped",
            "verification_profile_sha256": profile_sha,
        },
    }
    (product / ".task-meta.json").write_text(
        json.dumps(meta, sort_keys=True) + "\n", encoding="utf-8"
    )
    store = OperationStore(vault / ".vault-meta/harness")
    runtime = FakeRuntime(store, owner_id=task_id)

    def forbidden_finalizing_recovery(*_args: object, **_kwargs: object):
        raise AssertionError("exact protocol selected legacy finalizing recovery")

    skip_task_id = "33333333-3333-4333-8333-333333333333"
    skip_runtime_root = (
        vault / ".vault-meta/harness/review-runtime" / skip_task_id
    )
    skip_meta = {
        **meta,
        "task_id": skip_task_id,
        "review_policy": {
            "mode": "skip",
            "cross_model": False,
            "runtime": "",
            "model": "",
            "effort": "",
            "max_verify_iterations": 0,
            "verification_profile": "scoped",
            "verification_profile_sha256": profile_sha,
        },
    }
    skip_runtime = FakeRuntime(store, owner_id=skip_task_id)
    skipped = _run_review(
        skip_meta,
        vault,
        product,
        skip_task_id,
        skip_runtime_root,
        runtime_manager=skip_runtime,
        apply_finalizing_recovery=forbidden_finalizing_recovery,
    )
    skip_gate_root = (
        vault
        / ".vault-meta/harness/review-data"
        / skip_task_id
        / skip_task_id
    )
    skip_state = json.loads(
        (skip_gate_root / "review-gate.json").read_text(encoding="utf-8")
    )
    check(
        "exact-HEAD no-review policy terminates without a provider effect",
        skipped["status"] == "skipped"
        and skip_state["status"] == "skipped"
        and skip_state["lanes"] == []
        and skip_runtime.started == 0
        and store.list(skip_task_id) == [],
    )

    started = _run_review(
        meta,
        vault,
        product,
        task_id,
        runtime_root,
        runtime_manager=runtime,
        apply_finalizing_recovery=forbidden_finalizing_recovery,
    )
    gate_root = (
        vault
        / ".vault-meta/harness/review-data"
        / task_id
        / task_id
    )
    gate = ReviewGateController(gate_root, runtime, store)
    active = gate.rehydrate_attempt()
    lane = active.execution.lanes[0]
    round_ = active.rounds[lane.axis]
    finding = ReviewFinding(
        "F-selector",
        lane.axis,
        "important",
        "stop at the original HEAD",
        "the production selector must not open a verification child",
    )
    amended_plan = vault / "amended-plan.md"
    amended_plan.write_text(
        """# Amended exact-HEAD task

## Outcome Contract

```json
{"schema_version":1,"purpose":"Exercise the amended review path.","desired_outcome":"A schema-valid amendment starts a fresh review boundary.","success_evidence":[{"evidence_id":"exact-review","observable":"The public flow preserves the ledger and binds amended authority."}],"non_goals":["No provider replay."]}
```
""",
        encoding="utf-8",
    )
    record_plan_amendment(
        product,
        amended_plan,
        decision="approve the exact review-boundary amendment",
    )
    amended_plan_sha = hashlib.sha256(amended_plan.read_bytes()).hexdigest()
    amended_outcome_sha = extract_from_bytes(amended_plan.read_bytes()).sha256
    awaiting_amended = _run_review(
        meta,
        vault,
        product,
        task_id,
        runtime_root,
        runtime_manager=runtime,
        apply_finalizing_recovery=forbidden_finalizing_recovery,
    )
    check(
        "an amendment while awaiting callback preserves the stale lane without replay",
        awaiting_amended["status"] == "reviewing"
        and runtime.started == 1
        and gate.read()["attempt"]["identity"]["plan_sha256"]
        == meta["approved_plan_sha256"],
    )
    callback_path = Path(str(started["lanes"][0]["callback_path"]))
    callback_path.parent.mkdir(parents=True, exist_ok=True)
    callback_path.write_text(
        json.dumps(
            to_dict(review_round_envelope(
                round_,
                ReviewResult(
                    lane.axis, "changes-requested", (finding,), 0
                ),
            ))
        )
        + "\n",
        encoding="utf-8",
    )
    terminal = _run_review(
        meta,
        vault,
        product,
        task_id,
        runtime_root,
        runtime_manager=runtime,
        apply_finalizing_recovery=forbidden_finalizing_recovery,
    )
    exact_state = gate.read()
    check(
        "production selector terminalizes iteration zero without legacy effects",
        started["status"] == "reviewing"
        and terminal["status"] == "changes-requested"
        and exact_state["attempt"]["status"] == "terminal"
        and exact_state["attempt"]["terminal"]["result"]
        == "changes-requested"
        and runtime.started == 1
        and runtime.continued == 0
        and runtime.rearmed == 0
        and not exact_state.get("awaiting_resolution")
        and "continuation_effects" not in exact_state,
    )
    (product / "product.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.py"], cwd=product, check=True)
    subprocess.run(
        ["git", "commit", "-m", "resolve exact finding"],
        cwd=product,
        check=True,
        capture_output=True,
    )
    resolved_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=product, text=True
    ).strip()
    boundaries = exact_state["review_notification_evidence"]
    callbacks = [
        {
            "axis": axis,
            "round_operation_id": boundary["round_operation_id"],
            "round_run_id": boundary["round_run_id"],
            "callback_id": boundary["callback_id"],
            "callback_sha256": boundary["callback_sha256"],
        }
        for axis, boundary in sorted(boundaries.items())
    ]
    (product / ".task-review-resolution.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": task_id,
                "reviewed_head_sha": exact_state["attempt"]["identity"][
                    "exact_head_sha"
                ],
                "resolved_head_sha": resolved_head,
                "review_identity_sha256": review_transport_identity_sha256(
                    next(
                        iter(
                            {
                                boundary["review_operation_id"]
                                for boundary in boundaries.values()
                            }
                        )
                    ),
                    callbacks,
                ),
                "resolutions": [
                    {
                        "finding_id": material_finding_id,
                        "disposition": "applied",
                        "rationale": "The exact selector repair is committed.",
                        "follow_up": "",
                    }
                    for boundary in boundaries.values()
                    for material_finding_id in boundary[
                        "material_finding_ids"
                    ]
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    second = _run_review(
        meta,
        vault,
        product,
        task_id,
        runtime_root,
        runtime_manager=runtime,
        apply_finalizing_recovery=forbidden_finalizing_recovery,
    )
    second_state = gate.read()
    first_boundary = next(iter(boundaries.values()))
    archived_callback = (
        callback_path.parent
        / "accepted"
        / f"{first_boundary['callback_sha256']}.review-callback.json"
    )
    ledger = json.loads(
        (
            vault
            / ".vault-meta/harness/finalization-ledger"
            / f"{task_id}.json"
        ).read_text(encoding="utf-8")
    )
    check(
        "changed-HEAD resolution reserves the next bounded ledger cycle",
        second["status"] == "reviewing"
        and second_state["attempt"]["identity"]["cycle"] == 2
        and len(ledger["cycles"]) == 2
        and ledger["plan_sha256"] == meta["approved_plan_sha256"]
        and ledger["outcome_contract_sha256"]
        == meta["outcome_contract_sha256"]
        and ledger["cycles"][0]["terminal_result"] == "changes-requested"
        and ledger["cycles"][1]["terminal_result"] == ""
        and second_state["attempt"]["identity"]["plan_sha256"]
        == amended_plan_sha
        and second_state["attempt"]["identity"]["outcome_sha256"]
        == amended_outcome_sha
        and (
            gate.root / "attempts/cycle-1.json"
        ).is_file()
        and archived_callback.is_file()
        and not callback_path.exists()
        and json.loads(
            archived_callback.read_text(encoding="utf-8")
        )["callback_id"]
        == first_boundary["callback_id"]
        and runtime.started == 2,
    )

    def finish_current_attempt(
        started_receipt: dict[str, object], verdict: str
    ) -> dict[str, object]:
        active_run = gate.rehydrate_attempt()
        active_lane = active_run.execution.lanes[0]
        active_round = active_run.rounds[active_lane.axis]
        callback = Path(
            str(
                list(started_receipt["lanes"])[0]["callback_path"]
            )
        )
        callback.parent.mkdir(parents=True, exist_ok=True)
        callback.write_text(
            json.dumps(
                to_dict(
                    review_round_envelope(
                        active_round,
                        ReviewResult(active_lane.axis, verdict, (), 0),
                    )
                )
            )
            + "\n",
            encoding="utf-8",
        )
        return _run_review(
            meta,
            vault,
            product,
            task_id,
            runtime_root,
            runtime_manager=runtime,
            apply_finalizing_recovery=forbidden_finalizing_recovery,
        )

    def commit_changed_head(value: int, message: str) -> None:
        (product / "product.py").write_text(
            f"VALUE = {value}\n", encoding="utf-8"
        )
        subprocess.run(["git", "add", "product.py"], cwd=product, check=True)
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=product,
            check=True,
            capture_output=True,
        )

    approved_terminal = finish_current_attempt(second, "approve")
    approved_state = gate.read()
    approved_attempt_id = approved_state["attempt"]["identity"]["attempt_id"]
    approved_callback_envelope = json.loads(
        callback_path.read_text(encoding="utf-8")
    )
    approved_callback_archive = (
        callback_path.parent
        / "accepted"
        / (
            f"{approved_callback_envelope['payload_sha256']}"
            ".review-callback.json"
        )
    )
    summary_path = product / ".task-summary.json"
    changed_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    changed_summary["body"] = (
        "The exact selector fixture was refreshed after review launch."
    )
    summary_path.write_text(
        json.dumps(changed_summary, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    review_skill = vault / "skills/review/SKILL.md"
    stable_review_skill = review_skill.read_bytes()
    review_skill.write_bytes(stable_review_skill + b"\nDrifted input.\n")
    ledger_path = (
        vault
        / ".vault-meta/harness/finalization-ledger"
        / f"{task_id}.json"
    )
    ledger_before_ambiguous_context = ledger_path.read_bytes()
    starts_before_ambiguous_context = runtime.started
    try:
        _run_review(
            meta,
            vault,
            product,
            task_id,
            runtime_root,
            runtime_manager=runtime,
            apply_finalizing_recovery=forbidden_finalizing_recovery,
        )
    except ReviewAttemptError:
        pass
    else:
        raise AssertionError("ambiguous same-HEAD context drift was accepted")
    review_skill.write_bytes(stable_review_skill)
    check(
        "same-HEAD summary follow-up refuses any additional context drift",
        runtime.started == starts_before_ambiguous_context
        and ledger_path.read_bytes() == ledger_before_ambiguous_context,
    )
    failing_summary_runtime = FailingStartRuntime(
        store, owner_id=task_id
    )
    try:
        _run_review(
            meta,
            vault,
            product,
            task_id,
            runtime_root,
            runtime_manager=failing_summary_runtime,
            apply_finalizing_recovery=forbidden_finalizing_recovery,
        )
    except RuntimeError as exc:
        if str(exc) != "provider start failed":
            raise
    else:
        raise AssertionError("summary follow-up start failure was hidden")
    failed_summary_state = gate.read()
    failed_summary_attempt_id = failed_summary_state["attempt"][
        "identity"
    ]["attempt_id"]
    check(
        "summary follow-up launch failure is zero-effect cycle 3",
        failed_summary_state["status"] == "attention-required"
        and failed_summary_state["lanes"] == []
        and failed_summary_state["attempt"]["identity"]["cycle"] == 3
        and failed_summary_state["attempt"]["terminal"]["lane_results"]
        == []
        and failed_summary_attempt_id
        == predecessor_bound_attempt_id(
            lineage_id=task_id,
            predecessor_attempt_id=approved_attempt_id,
            exact_head=resolved_head,
            cycle_number=3,
        )
        and failing_summary_runtime.started == 1
        and approved_callback_archive.is_file()
        and not callback_path.exists(),
    )
    approved_callback_archive.replace(callback_path)
    summary_follow_up = _run_review(
        meta,
        vault,
        product,
        task_id,
        runtime_root,
        runtime_manager=runtime,
        apply_finalizing_recovery=forbidden_finalizing_recovery,
    )
    summary_follow_up_state = gate.read()
    expected_summary_attempt_id = predecessor_bound_attempt_id(
        lineage_id=task_id,
        predecessor_attempt_id=failed_summary_attempt_id,
        exact_head=resolved_head,
        cycle_number=3,
    )
    starts_after_summary_follow_up = runtime.started
    summary_follow_up_replay = _run_review(
        meta,
        vault,
        product,
        task_id,
        runtime_root,
        runtime_manager=runtime,
        apply_finalizing_recovery=forbidden_finalizing_recovery,
    )
    check(
        "same-HEAD summary drift reserves one predecessor-bound follow-up",
        approved_terminal["status"] == "approved"
        and summary_follow_up["status"] == "reviewing"
        and summary_follow_up_replay["status"] == "reviewing"
        and summary_follow_up_state["attempt"]["identity"]["cycle"] == 3
        and summary_follow_up_state["attempt"]["identity"]["attempt_id"]
        == expected_summary_attempt_id
        and summary_follow_up_state["context"][
            "implementer_summary_sha256"
        ]
        == hashlib.sha256(summary_path.read_bytes()).hexdigest()
        and approved_callback_archive.is_file()
        and not callback_path.exists()
        and len(
            json.loads(ledger_path.read_text(encoding="utf-8"))[
                "cycles"
            ]
        )
        == 3
        and runtime.started == starts_after_summary_follow_up == 3,
    )
    summary_approved = finish_current_attempt(summary_follow_up, "approve")
    starts_after_summary_approval = runtime.started
    unchanged_summary = _run_review(
        meta,
        vault,
        product,
        task_id,
        runtime_root,
        runtime_manager=runtime,
        apply_finalizing_recovery=forbidden_finalizing_recovery,
    )
    check(
        "unchanged reviewed summary reuses terminal approval idempotently",
        summary_approved["status"] == "approved"
        and unchanged_summary["status"] == "approved"
        and runtime.started == starts_after_summary_approval == 3,
    )
    commit_changed_head(3, "change after approval")
    after_approved = _run_review(
        meta,
        vault,
        product,
        task_id,
        runtime_root,
        runtime_manager=runtime,
        apply_finalizing_recovery=forbidden_finalizing_recovery,
    )
    check(
        "changed HEAD never reuses an approved terminal attempt",
        approved_terminal["status"] == "approved"
        and after_approved["status"] == "reviewing"
        and gate.read()["attempt"]["identity"]["cycle"] == 4
        and runtime.started == 4,
    )

    blocked_terminal = finish_current_attempt(after_approved, "blocked")
    commit_changed_head(4, "change after blocked review")
    after_blocked = _run_review(
        meta,
        vault,
        product,
        task_id,
        runtime_root,
        runtime_manager=runtime,
        apply_finalizing_recovery=forbidden_finalizing_recovery,
    )
    check(
        "changed HEAD never reuses a blocked terminal attempt",
        blocked_terminal["status"] == "blocked"
        and after_blocked["status"] == "reviewing"
        and gate.read()["attempt"]["identity"]["cycle"] == 4
        and runtime.started == 5,
    )

    original_request_exit = runtime.request_exit

    def attention_request_exit(owner_id: str, operation_id: str) -> object:
        record = store.read(owner_id, operation_id)
        if record.state != "attention-required":
            store.transition(
                owner_id,
                operation_id,
                "attention-required",
                reason=AttentionReason.ATTENTION_REQUIRED,
            )
        return store.read(owner_id, operation_id)

    runtime.request_exit = attention_request_exit
    attention_terminal = finish_current_attempt(after_blocked, "approve")
    runtime.request_exit = original_request_exit
    commit_changed_head(5, "change after attention review")
    starts_before_attention_retry = runtime.started
    try:
        _run_review(
            meta,
            vault,
            product,
            task_id,
            runtime_root,
            runtime_manager=runtime,
            apply_finalizing_recovery=forbidden_finalizing_recovery,
        )
    except ReviewAttemptError:
        pass
    else:
        raise AssertionError("cleanup-retained attempt launched a successor")
    check(
        "changed HEAD cannot replace a cleanup-retained attention attempt",
        attention_terminal["status"] == "attention-required"
        and runtime.started == starts_before_attention_retry
        and gate.read()["attempt"]["identity"]["cycle"] == 4
        and gate.read()["attempt"]["identity"]["exact_head_sha"]
        != subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=product,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip(),
    )

    legacy_task_id = "22222222-2222-4222-8222-222222222222"
    legacy_runtime = FakeRuntime(store, owner_id=legacy_task_id)
    legacy_gate_root = (
        vault
        / ".vault-meta/harness/review-data"
        / legacy_task_id
        / legacy_task_id
    )
    legacy_gate = ReviewGateController(
        legacy_gate_root, legacy_runtime, store
    )
    legacy_runtime_root = base / "legacy-runtime"
    legacy_runtime_root.mkdir()
    legacy_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    legacy_gate.begin(
        dispatch_operation_id=legacy_task_id,
        request=request(
            legacy_head,
            operation_id=legacy_task_id,
            owner_id=legacy_task_id,
        ),
        origin_surface="22222222-2222-4222-8222-222222222222",
        cwd=legacy_runtime_root,
        product_root=product,
        prompt_pointer="prompts/review.md",
        callback_root="callbacks/legacy",
    )
    legacy_starts = legacy_runtime.started
    legacy_meta = {
        **meta,
        "task_id": legacy_task_id,
        "review_policy": {
            **meta["review_policy"],
            "mode": "deep",
            "max_verify_iterations": 2,
        },
    }
    historical = _run_review(
        legacy_meta,
        vault,
        product,
        legacy_task_id,
        legacy_runtime_root,
        runtime_manager=legacy_runtime,
        apply_finalizing_recovery=forbidden_finalizing_recovery,
    )
    check(
        "production selector makes a pre-activation gate inspect-only",
        historical["status"] == "legacy-cross-head-resume-disabled"
        and historical["allowed_actions"] == ["inspect", "archive", "cleanup"]
        and historical["provider_effect_allowed"] is False
        and legacy_runtime.started == legacy_starts,
    )

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

with tempfile.TemporaryDirectory(prefix="approval-failpoint.") as raw:
    base = Path(raw)
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    gate = ReviewGateController(base / "gate", runtime, store)
    run = gate.begin_attempt(
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
    first_lane, second_lane = run.execution.lanes
    gate.complete_attempt_round(
        run,
        first_lane,
        run.rounds[first_lane.axis],
        ReviewResult(first_lane.axis, "approve", (), 0),
    )
    original_replace = gate._replace

    def crash_before_terminal_commit(**updates: object) -> dict[str, object]:
        if updates.get("status") == "approved":
            raise RuntimeError("crash before terminal attempt commit")
        return original_replace(**updates)

    gate._replace = crash_before_terminal_commit
    try:
        gate.complete_attempt_round(
            run,
            second_lane,
            run.rounds[second_lane.axis],
            ReviewResult(second_lane.axis, "approve", (), 0),
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("approval failpoint did not interrupt the commit")
    interrupted = gate.read()
    try:
        authorize_task_finalization(
            gate.root,
            dispatch_operation_id="task-1",
            expected_head_sha="a" * 40,
            expected_profile="scoped",
            expected_profile_sha256="8" * 64,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("prepared approval artifacts became authoritative")
    check(
        "approval artifacts are non-authoritative before atomic terminal commit",
        (gate.root / ".review-callback.json").is_file()
        and interrupted["status"] == "reviewing"
        and interrupted["attempt"]["status"] == "awaiting-callback"
        and interrupted["evidence"] == {},
    )

with tempfile.TemporaryDirectory(prefix="exact-attempt-program.") as raw:
    product = Path(raw) / "product"
    product.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=product,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "review@example.invalid"],
        cwd=product,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Review Attempt Test"],
        cwd=product,
        check=True,
    )
    verification_path = product / "docs/verification.md"
    verification_path.parent.mkdir()
    verification_path.write_text("scoped verification passed\n", encoding="utf-8")
    profile_path = product / "config/verification-profiles.toml"
    profile_path.parent.mkdir()
    profile_path.write_bytes((ROOT / "config/verification-profiles.toml").read_bytes())
    subprocess.run(
        ["git", "add", "docs/verification.md", "config/verification-profiles.toml"],
        cwd=product,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "verification evidence"],
        cwd=product,
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    write_scoped_verification(product, verification_path, head)
    boundary = ReviewBoundaryInput(
        purpose="implementation",
        outcome_contract_sha256="2" * 64,
        plan_sha256="1" * 64,
        product_head_sha=head,
        verification_evidence_sha256=hashlib.sha256(
            verification_path.read_bytes()
        ).hexdigest(),
        verification_evidence_path="docs/verification.md",
    )
    program = compile_review_program("small-reversible", (boundary,))

    def terminal_receipt(verdict: str, operation_id: str):
        gate_root = (
            product
            / ".vault-meta/harness/review-data"
            / operation_id
            / operation_id
        )
        store = OperationStore(product / ".vault-meta/harness")
        runtime = FakeRuntime(store, owner_id=operation_id)
        gate = ReviewGateController(gate_root, runtime, store)
        scratch = product.parent / f"scratch-{operation_id}"
        scratch.mkdir()
        run = gate.begin_attempt(
            dispatch_operation_id=operation_id,
            finalization_lineage_id="program-lineage",
            cycle=1,
            plan_sha256=boundary.plan_sha256,
            outcome_sha256=boundary.outcome_contract_sha256,
            request=request(
                head,
                operation_id=operation_id,
                owner_id=operation_id,
                boundary_input_sha256=boundary.input_sha256,
            ),
            origin_surface="11111111-1111-4111-8111-111111111111",
            cwd=scratch,
            product_root=product,
            prompt_pointer="prompts/review.md",
            callback_root=f"callbacks/{operation_id}",
        )
        first_lane, second_lane = run.execution.lanes
        findings = (
            (
                ReviewFinding(
                    "F-material",
                    first_lane.axis,
                    "important",
                    "stop this exact attempt",
                    "the initial exact-HEAD result is material",
                ),
            )
            if verdict == "changes-requested"
            else ()
        )
        gate.complete_attempt_round(
            run,
            first_lane,
            run.rounds[first_lane.axis],
            ReviewResult(first_lane.axis, verdict, findings, 0),
        )
        decision = gate.complete_attempt_round(
            run,
            second_lane,
            run.rounds[second_lane.axis],
            ReviewResult(second_lane.axis, "approve", (), 0),
        )
        receipt = trusted_review_receipt(product, boundary, operation_id)
        authorization = (
            authorize_task_finalization(
                gate.root,
                dispatch_operation_id=operation_id,
                expected_head_sha=head,
                expected_profile="scoped",
                expected_profile_sha256="8" * 64,
            )
            if verdict == "approve"
            else None
        )
        return (
            decision,
            receipt,
            reconcile_review_program(program, (receipt,)),
            authorization,
        )

    (
        material_decision,
        material_receipt,
        material_program,
        _,
    ) = terminal_receipt(
        "changes-requested", "review-material"
    )
    blocked_decision, blocked_receipt, blocked_program, _ = terminal_receipt(
        "blocked", "review-blocked"
    )
    approved_decision, approved_receipt, approved_program, approval = (
        terminal_receipt("approve", "review-approved")
    )
    check(
        "terminal attempts become digest-bound program decisions",
        material_decision.action == "changes-requested"
        and blocked_decision.action == "blocked"
        and approved_decision.action == "approved"
        and material_receipt.verdict == "stopped"
        and blocked_receipt.verdict == "stopped"
        and approved_receipt.verdict == "approved"
        and material_program.action == "stop"
        and blocked_program.action == "stop"
        and approved_program.action == "complete"
        and material_program.may_fix
        and blocked_program.may_fix
        and approval is not None
        and approval.approved,
    )

print("\nAll exact-HEAD ReviewAttempt gate tests passed.")
