#!/usr/bin/env python3
"""Public-seam tests for the unified review workflow."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from review_contract import ReviewContractError, validate_review
from harness.callbacks import CallbackBroker
from harness.contracts import AttentionReason, CapabilityReport, RuntimeRoute
from harness.runtime_sessions import RuntimeSessionRequest
from harness.store import OperationStore
from harness.workflows import review as review_facade
from harness.workflows import review_contracts, review_results
from harness.workflows.review import (
    ReviewContext,
    ReviewExecution,
    ReviewLaneIdentity,
    ReviewLaneSession,
    ReviewOperationRequest,
    ReviewRequest,
    ReviewResult,
    ReviewSessionRequest,
    ReviewFinding,
    aggregate,
    aggregate_review_evidence,
    accept_review_round,
    enqueue,
    operation_spec,
    prepare_review_round,
    review_round_envelope,
    review_evidence_envelope,
    review_session_specs,
    start_review,
    verify_review_lane,
    verify_session,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


check(
    "review uses the generic runtime session request contract",
    ReviewSessionRequest is RuntimeSessionRequest,
)
check(
    "review facade preserves contract type identity",
    review_facade.ReviewContext is review_contracts.ReviewContext,
)
check(
    "review facade preserves result type identity",
    review_facade.ReviewResult is review_results.ReviewResult,
)
check(
    "review facade preserves aggregate callable identity",
    review_facade.aggregate is review_results.aggregate,
)


simple_review = {
    "schema_version": 1,
    "operation_id": "review-1",
    "run_id": "run-1",
    "mode": "simple",
    "head_sha": "a" * 40,
    "verification_profile": {"name": "scoped", "sha256": "b" * 64},
    "verdict": "approve",
    "axes": [
        {
            "axis": "openai-holistic",
            "verdict": "approve",
            "verification_iteration": 0,
            "findings": [],
        }
    ],
    "verification_gaps": [],
    "notes_for_executor": [],
    "residual_risks": [],
}
validated = validate_review(simple_review)
check(
    "one schema accepts simple review evidence",
    validated["operation_id"] == "review-1"
    and validated["axes"][0]["axis"] == "openai-holistic"
    and validated["verification_profile"]["name"] == "scoped",
)

material = {
    "finding_id": "F-1",
    "severity": "important",
    "file": "scripts/review_contract.py",
    "line": 1,
    "summary": "approval hides a material defect",
    "evidence": "the failing path is reachable",
    "recommendation": "request changes until it is fixed",
}
invalid_reviews = (
    (
        "operation identity is a bounded callback identifier",
        {**simple_review, "operation_id": "../review"},
    ),
    (
        "approve with material findings is rejected",
        {
            **simple_review,
            "axes": [{**simple_review["axes"][0], "findings": [material]}],
        },
    ),
    (
        "simple verification budget is enforced",
        {
            **simple_review,
            "axes": [{**simple_review["axes"][0], "verification_iteration": 2}],
        },
    ),
    (
        "top-level verdict must match independent axes",
        {
            **simple_review,
            "verdict": "changes-requested",
        },
    ),
    (
        "legacy severity vocabulary is rejected",
        {
            **simple_review,
            "verdict": "changes-requested",
            "axes": [
                {
                    **simple_review["axes"][0],
                    "verdict": "changes-requested",
                    "findings": [{**material, "severity": "warning"}],
                }
            ],
        },
    ),
    (
        "finding paths are repository-relative POSIX paths",
        {
            **simple_review,
            "verdict": "changes-requested",
            "axes": [
                {
                    **simple_review["axes"][0],
                    "verdict": "changes-requested",
                    "findings": [{**material, "file": "scripts\\review.py"}],
                }
            ],
        },
    ),
)
for label, candidate in invalid_reviews:
    try:
        validate_review(candidate)
    except ReviewContractError:
        check(label, True)
    else:
        check(label, False)

deep_review = {
    **simple_review,
    "operation_id": "review-deep",
    "mode": "deep",
    "axes": [
        {
            "axis": "anthropic-holistic",
            "verdict": "approve",
            "verification_iteration": 2,
            "findings": [],
        },
        {
            "axis": "openai-holistic",
            "verdict": "approve",
            "verification_iteration": 1,
            "findings": [],
        },
    ],
}
check(
    "deep review preserves both independently budgeted axes",
    [row["axis"] for row in validate_review(deep_review)["axes"]]
    == ["anthropic-holistic", "openai-holistic"],
)
try:
    aggregate(
        ReviewRequest(
            "bounded", max_verify_iterations=1, selected_provider="openai"
        ),
        {
            "openai-holistic": ReviewResult(
                "openai-holistic", "approve", verification_iteration=2
            )
        },
    )
except ValueError:
    check("workflow aggregation rejects over-budget verification", True)
else:
    check("workflow aggregation rejects over-budget verification", False)
lane = ReviewLaneIdentity("openai-holistic", "lane-1", "surface-1")
verify_session(lane, lane)
try:
    verify_session(
        lane, ReviewLaneIdentity("openai-holistic", "lane-1", "surface-2")
    )
except ValueError:
    check("verification reuses exact axis lane and surface", True)
else:
    check("verification reuses exact axis lane and surface", False)
published_schema = json.loads(
    (ROOT / "schemas/review-v1.schema.json").read_text(encoding="utf-8")
)
check(
    "published schema exposes the same evidence and severity vocabulary",
    {"operation_id", "head_sha", "verification_profile", "axes"}
    <= set(published_schema["required"])
    and set(published_schema["$defs"]["finding"]["properties"]["severity"]["enum"])
    == {"critical", "important", "minor"},
)

with tempfile.TemporaryDirectory(prefix="review-workflow.") as raw:
    root = Path(raw)
    manifest = root / "packets/review/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"schema_version": 1, "operation_id": "review-queued"}),
        encoding="utf-8",
    )
    route = RuntimeRoute(
        "claude", "fable", "xhigh", "reviewer-readonly", "c" * 64
    )
    policy = ReviewRequest(
        "review-queued",
        depth="deep",
        cross_model=True,
        max_verify_iterations=2,
    )
    context = ReviewContext(
        manifest="packets/review/manifest.json",
        head_sha="d" * 40,
        verification_profile="scoped",
        verification_profile_sha256="e" * 64,
    )
    request = ReviewOperationRequest(policy, "owner-1", route, context)
    spec = operation_spec(request)
    check(
        "context-ready review builds one reviewer-readonly OperationSpec",
        spec.kind == "simple-review-holistic"
        and spec.context_manifest == context.manifest
        and spec.verification_profile == "scoped"
        and spec.route.profile == "reviewer-readonly",
    )
    store = OperationStore(root / "state")
    record = enqueue(request, store)
    check(
        "review public seam persists through OperationStore",
        record == store.read("owner-1", "review-queued")
        and record.state == "created"
        and bool(record.lane_id)
        and bool(record.run_id),
    )
@dataclass(frozen=True)
class FakeSessionResult:
    record: object
    checkpoint: str


class FakeReviewRuntime:
    def __init__(self, store: OperationStore) -> None:
        self.store = store
        self.started: list[object] = []
        self.preflighted: list[tuple[tuple[RuntimeRoute, Path, str], ...]] = []
        self.incompatible_runtimes: set[str] = set()
        self.continued: list[tuple[str, str, str, str]] = []
        self.callbacks: list[object] = []
        self.exits: list[tuple[str, str]] = []
        self.cleanups: list[tuple[str, str]] = []
        self.prepared: list[str] = []
        self.registered: list[tuple[str, str, str, str, str]] = []

    def preflight_routes(
        self,
        requests: tuple[tuple[RuntimeRoute, Path, str], ...],
    ) -> tuple[CapabilityReport, ...]:
        self.preflighted.append(requests)
        return tuple(
            CapabilityReport(
                route,
                route.runtime not in self.incompatible_runtimes,
                ("provider:profile-valid",),
                (
                    None
                    if route.runtime not in self.incompatible_runtimes
                    else AttentionReason.CAPABILITY_MISMATCH
                ),
            )
            for route, _callback_dir, _origin_surface in requests
        )

    def start(
        self,
        request: object,
        *,
        on_surface_opened: object = None,
        admit_provider_start: object = None,
    ) -> FakeSessionResult:
        if callable(admit_provider_start):
            admit_provider_start()
        self.started.append(request)
        record = self.store.create(
            request.spec,
            lane_id=request.lane_id,
            run_id=request.run_id,
        )
        record = replace(
            record,
            resources=replace(
                record.resources,
                surface_id=(
                    "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAA"
                    + str(len(self.started))
                ),
            ),
        )
        self.store.save(record, expected_revision=0)
        result = FakeSessionResult(record, f"checkpoint-{len(self.started)}")
        if on_surface_opened is not None:
            on_surface_opened(result)
        return result

    def continue_session(
        self,
        owner_id: str,
        operation_id: str,
        checkpoint: str,
        prompt_pointer: str,
    ) -> FakeSessionResult:
        self.continued.append(
            (owner_id, operation_id, checkpoint, prompt_pointer)
        )
        record = self.store.read(owner_id, operation_id)
        return FakeSessionResult(record, checkpoint)

    def accept_callback(self, envelope: object) -> object:
        self.callbacks.append(envelope)
        return CallbackBroker(self.store, "owner-1").accept(envelope)

    def register_callback_target(
        self,
        owner_id: str,
        parent_operation_id: str,
        child_operation_id: str,
        child_run_id: str,
        callback_pointer: str,
    ) -> None:
        child = self.store.read(owner_id, child_operation_id)
        if (
            child.run_id != child_run_id
            or child.lane_id
            != self.store.read(owner_id, parent_operation_id).lane_id
        ):
            raise AssertionError("callback target does not match parent lane")
        self.registered.append(
            (
                owner_id,
                parent_operation_id,
                child_operation_id,
                child_run_id,
                callback_pointer,
            )
        )

    def request_exit(self, owner_id: str, operation_id: str) -> None:
        self.exits.append((owner_id, operation_id))
        record = self.store.read(owner_id, operation_id)
        if record.state in {"complete", "failed", "cancelled"}:
            return record
        if record.state in {
            "created",
            "preflight",
            "starting",
            "attention-required",
        }:
            self.store.transition(
                owner_id, operation_id, "cancelling"
            )
        elif record.state != "finalizing":
            self.store.transition(
                owner_id, operation_id, "finalizing"
            )
        self.store.transition(owner_id, operation_id, "exiting")
        return self.store.read(owner_id, operation_id)

    def cleanup(self, owner_id: str, operation_id: str) -> None:
        self.cleanups.append((owner_id, operation_id))
        self.store.transition(owner_id, operation_id, "complete")
        return self.store.read(owner_id, operation_id)

    def status(self, owner_id: str, operation_id: str) -> object:
        return self.store.read(owner_id, operation_id)


with tempfile.TemporaryDirectory(prefix="review-runtime.") as raw:
    runtime_root = Path(raw)
    scratch = runtime_root / "scratch"
    scratch.mkdir()
    runtime = FakeReviewRuntime(OperationStore(runtime_root / "state"))
    context = ReviewContext(
        manifest="packets/review/manifest.json",
        head_sha="d" * 40,
        verification_profile="scoped",
        verification_profile_sha256="e" * 64,
    )
    runtime_route = RuntimeRoute(
        "claude", "fable", "xhigh", "reviewer-callback", "c" * 64
    )
    simple_request = ReviewOperationRequest(
        ReviewRequest("review-simple", selected_provider="openai"),
        "owner-1",
        runtime_route,
        context,
        lane_ids={"openai-holistic": "composition-lane"},
    )
    simple = start_review(
        simple_request,
        runtime,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=ROOT,
        prompt_pointer="packets/review/handoff.md",
        callback_root="callbacks/review-simple",
        round_store=runtime.store,
        prepare_lane=lambda axis, _request, _result, _round: (
            runtime.prepared.append(axis)
        ),
    )
    check(
        "simple public start opens one persistent holistic runtime lane",
        isinstance(simple, ReviewExecution)
        and [lane.axis for lane in simple.lanes] == ["openai-holistic"]
        and len(runtime.started) == 1
        and runtime.started[0].spec.operation_id.startswith(
            "review-simple-holistic-"
        )
        and runtime.started[0].spec.operation_id != "review-simple"
        and runtime.started[0].lane_id == "composition-lane"
        and runtime.started[0].placement == "workspace"
        and runtime.started[0].callback_pointer
        == "callbacks/review-simple/openai-holistic/.review-callback.json"
        and runtime.prepared == ["openai-holistic"],
    )
    original = simple.lanes[0]
    first_round = prepare_review_round(runtime.store, original)
    material_result = ReviewResult(
        "openai-holistic",
        "changes-requested",
        (
            ReviewFinding(
                "F-runtime-1",
                "openai-holistic",
                "important",
                "same-session verification is required",
                "the initial review found a material issue",
            ),
        ),
        verification_iteration=0,
    )
    first_envelope = review_round_envelope(first_round, material_result)
    accept_review_round(
        runtime, runtime.store, original, first_round, first_envelope
    )
    child_record = runtime.store.read("owner-1", first_round.operation_id)
    check(
        "initial callback uses a terminal one-shot child in the parent lane",
        first_round.operation_id != original.operation_id
        and child_record.spec.parent_operation_id == original.operation_id
        and first_round.lane_id == original.lane_id
        and first_round.run_id != original.run_id
        and not child_record.resources.surface_id
        and child_record.state == "complete"
        and bool(child_record.accepted_callback_id)
        and not runtime.exits,
    )
    verified = verify_review_lane(
        runtime,
        original,
        prompt_pointer="packets/review/verify-1.md",
        callback_pointer=(
            "callbacks/review-simple/openai-holistic/.review-callback.json"
        ),
        round_store=runtime.store,
    )
    verification_round = prepare_review_round(runtime.store, verified)
    check(
        "simple verification registers a new receipt and continues exact session",
        isinstance(verified, ReviewLaneSession)
        and verified.surface_id == original.surface_id
        and verified.verification_iteration == 1
        and verification_round.operation_id != first_round.operation_id
        and len(runtime.started) == 1
        and runtime.continued
        == [
            (
                "owner-1",
                original.operation_id,
                original.checkpoint,
                "packets/review/verify-1.md",
            )
        ],
    )
    exhausted = verify_review_lane(
        runtime,
        verified,
        prompt_pointer="packets/review/verify-2.md",
        callback_pointer=(
            "callbacks/review-simple/openai-holistic/.review-callback.json"
        ),
        round_store=runtime.store,
    )
    check(
        "simple verification exhaustion persists attention without another prompt",
        exhausted.state == "attention-required"
        and runtime.store.read("owner-1", verified.operation_id).state
        == "attention-required"
        and len(runtime.continued) == 1,
    )

    deep_request = ReviewOperationRequest(
        ReviewRequest("review-deep-start", depth="deep", max_verify_iterations=2),
        "owner-1",
        route,
        context,
        axis_routes={
            "anthropic-holistic": runtime_route,
            "openai-holistic": RuntimeRoute(
                "codex",
                "gpt-5.6-sol",
                "xhigh",
                "reviewer-callback",
                "c" * 64,
            ),
        },
    )
    deep = start_review(
        deep_request,
        runtime,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=ROOT,
        prompt_pointer="packets/review/handoff.md",
        callback_root="callbacks/review-deep",
        round_store=runtime.store,
    )
    deep_operations = [lane.operation_id for lane in deep.lanes]
    replay_identities = review_session_specs(deep_request)
    check(
        "deep public start opens two independent runtime lanes",
        [lane.axis for lane in deep.lanes]
        == ["anthropic-holistic", "openai-holistic"]
        and len(set(deep_operations)) == 2
        and len({lane.surface_id for lane in deep.lanes}) == 2
        and len(
            {request.callback_pointer for request in runtime.started[-2:]}
        )
        == 2
        and [request.spec.kind for request in runtime.started[-2:]]
        == ["simple-review-holistic", "simple-review-holistic"]
        and [request.spec.route.runtime for request in runtime.started[-2:]]
        == ["claude", "codex"]
        and [request.placement for request in runtime.started[-2:]]
        == ["workspace", "workspace"]
        and [
            (item.spec.operation_id, item.lane_id, item.run_id)
            for item in replay_identities
        ]
        == [
            (lane.operation_id, lane.lane_id, lane.run_id)
            for lane in deep.lanes
        ]
        and len(runtime.started) == 3,
    )
    completed_spec = runtime.store.read(
        "owner-1", deep.lanes[0].operation_id
    )
    runtime.store.save(
        replace(
            completed_spec,
            state="complete",
            resources=replace(completed_spec.resources, surface_id=""),
            revision=completed_spec.revision + 1,
        ),
        expected_revision=completed_spec.revision,
    )
    partial_replay = start_review(
        deep_request,
        runtime,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=ROOT,
        prompt_pointer="packets/review/handoff.md",
        callback_root="callbacks/review-deep",
        round_store=runtime.store,
    )
    check(
        "partial deep replay reconstructs complete and live parents without launch",
        len(runtime.started) == 3
        and partial_replay.lanes[0].state == "complete"
        and partial_replay.lanes[0].surface_id == ""
        and partial_replay.lanes[1].surface_id
        == deep.lanes[1].surface_id,
    )
    deep_results = {
        axis: ReviewResult(axis, "approve")
        for axis in deep_request.policy.axes
    }
    aggregate_evidence = aggregate_review_evidence(deep, deep_results)
    aggregate_envelope = review_evidence_envelope(deep, deep_results)
    check(
        "deep lanes aggregate without reranking into canonical archive evidence",
        validate_review(aggregate_evidence)["verdict"] == "approve"
        and [
            row["axis"] for row in aggregate_envelope.payload["axes"]
        ]
        == list(deep_request.policy.axes)
        and aggregate_envelope.operation_id == "review-deep-start",
    )
    full_request = ReviewOperationRequest(
        ReviewRequest("review-full-start", depth="full", max_verify_iterations=2),
        "owner-1",
        route,
        context,
        axis_routes={
            "anthropic-intent": runtime_route,
            "anthropic-engineering": runtime_route,
            "openai-intent": deep_request.axis_routes["openai-holistic"],
            "openai-engineering": deep_request.axis_routes["openai-holistic"],
        },
    )
    full = start_review(
        full_request,
        runtime,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=ROOT,
        prompt_pointer="packets/review/handoff.md",
        callback_root="callbacks/review-full",
        round_store=runtime.store,
    )
    check(
        "full public start keeps four real independent specialist states",
        [lane.axis for lane in full.lanes]
        == [
            "anthropic-intent",
            "anthropic-engineering",
            "openai-intent",
            "openai-engineering",
        ]
        and len({lane.operation_id for lane in full.lanes}) == 4
        and len({lane.surface_id for lane in full.lanes}) == 4
        and [request.spec.kind for request in runtime.started[-4:]]
        == [
            "deep-review-spec",
            "deep-review-correctness",
            "deep-review-spec",
            "deep-review-correctness",
        ]
        and [request.spec.route.runtime for request in runtime.started[-4:]]
        == ["claude", "claude", "codex", "codex"],
    )
    started_before_full_replay = len(runtime.started)
    runtime.incompatible_runtimes.add("codex")
    replayed_full = start_review(
        full_request,
        runtime,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=ROOT,
        prompt_pointer="packets/review/handoff.md",
        callback_root="callbacks/review-full",
        round_store=runtime.store,
    )
    check(
        "full replay trusts already-owned exact lanes without new provider preflight",
        len(runtime.started) == started_before_full_replay
        and [lane.operation_id for lane in replayed_full.lanes]
        == [lane.operation_id for lane in full.lanes]
        and runtime.preflighted[-1] == (),
    )
    runtime.incompatible_runtimes.clear()
    blocked_runtime = FakeReviewRuntime(
        OperationStore(runtime_root / "blocked-full-state")
    )
    blocked_runtime.incompatible_runtimes.add("codex")
    try:
        start_review(
            full_request,
            blocked_runtime,
            origin_surface="11111111-1111-4111-8111-111111111111",
            cwd=scratch,
            product_root=ROOT,
            prompt_pointer="packets/review/handoff.md",
            callback_root="callbacks/review-full-blocked",
            round_store=blocked_runtime.store,
        )
    except Exception as exc:
        blocked_full_error = str(exc)
    else:
        blocked_full_error = ""
    check(
        "full preflights every provider before the first durable or external effect",
        "Deep" in blocked_full_error
        and len(blocked_runtime.preflighted) == 1
        and [
            route.runtime
            for route, _callback_dir, _origin_surface
            in blocked_runtime.preflighted[0]
        ]
        == ["claude", "claude", "codex", "codex"]
        and blocked_runtime.started == []
        and blocked_runtime.store.list("owner-1") == [],
    )
    full_results = {
        axis: ReviewResult(axis, "approve") for axis in full_request.policy.axes
    }
    full_results["openai-engineering"] = ReviewResult(
        "openai-engineering",
        "changes-requested",
        (
            ReviewFinding(
                "F-full-openai-engineering",
                "openai-engineering",
                "important",
                "one specialist found a material defect",
                "the defect remains independently attributable",
            ),
        ),
    )
    full_aggregate = aggregate(full_request.policy, full_results)
    check(
        "one material full-lane finding blocks aggregate approval without voting",
        full_aggregate["verdict"] == "changes-requested"
        and [row["axis"] for row in full_aggregate["axes"]]
        == list(full_request.policy.axes)
        and full_aggregate["axes"][-1]["findings"][0]["finding_id"]
        == "openai-engineering:F-full-openai-engineering",
    )
    check(
        "verification callback gets a distinct receipt without owning resources",
        verification_round.operation_id != first_round.operation_id
        and verification_round.lane_id == original.lane_id
        and verification_round.verification_iteration == 1,
    )
    approved = ReviewResult(
        "openai-holistic", "approve", verification_iteration=1
    )
    approved_envelope = review_round_envelope(verification_round, approved)
    accept_review_round(
        runtime,
        runtime.store,
        verified,
        verification_round,
        approved_envelope,
    )
    check(
        "terminal approval exits and cleans only the parent reviewer session",
        runtime.exits == [("owner-1", original.operation_id)]
        and runtime.cleanups == [("owner-1", original.operation_id)]
        and [item.operation_id for item in runtime.callbacks]
        == [first_round.operation_id, verification_round.operation_id],
    )

with tempfile.TemporaryDirectory(prefix="review-runner.") as raw:
    root = Path(raw)
    manifest = root / "packets/review/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"schema_version": 1, "operation_id": "review-cli"}),
        encoding="utf-8",
    )
    store_path = root / "state"
    runner_runtime = FakeReviewRuntime(OperationStore(store_path))
    module_spec = importlib.util.spec_from_file_location(
        "review_runner_module", ROOT / "scripts/review-runner.py"
    )
    if module_spec is None or module_spec.loader is None:
        raise AssertionError("review runner module is unavailable")
    review_runner = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(review_runner)
    callback_root = "callbacks"
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = review_runner.main(
            [
                "--operation-id",
                "review-cli",
                "--owner-id",
                "owner-1",
                "--store",
                str(store_path),
                "--state-dir",
                str(root / "review-data"),
                "--context-root",
                str(root),
                "--context-manifest",
                "packets/review/manifest.json",
                "--origin-surface",
                "11111111-1111-4111-8111-111111111111",
                "--prompt-pointer",
                "skills/review/SKILL.md",
                "--callback-root",
                callback_root,
                "--runtime-root",
                str(root / "runtime"),
                "--session-runtime",
                "codex",
                "--session-model",
                "gpt-5.6-sol",
                "--session-effort",
                "high",
            ],
            runtime_manager=runner_runtime,
        )
    receipt = json.loads(output.getvalue())
    round_meta = json.loads(
        (
            root
            / "runtime/callbacks/openai-holistic/.review-meta.json"
        ).read_text(encoding="utf-8")
    )
    gate_state = json.loads(
        (root / "review-data/review-gate.json").read_text(
            encoding="utf-8"
        )
    )
    check(
        "public review runner starts the durable automatic gate and typed callback",
        exit_code == 0
        and len(runner_runtime.started) == 1
        and gate_state["status"] == "reviewing"
        and gate_state["policy"]["depth"] == "simple"
        and receipt["lanes"][0]["surface_id"]
        == "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAA1"
        and round_meta["transport"] == "review-round"
        and round_meta["parent_session_operation_id"].startswith(
            "review-cli-holistic-"
        )
        and receipt["lanes"][0]["round_operation_id"]
        == round_meta["operation_id"],
    )

print("review vertical tests passed")
