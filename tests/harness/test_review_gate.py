#!/usr/bin/env python3
"""Production-seam regressions for the automatic review gate."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import re
import shlex
import subprocess
import tempfile
import uuid
from contextlib import redirect_stdout
from dataclasses import dataclass, replace
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT / "scripts"))

from harness.callbacks import CallbackBroker
from harness import cli as harness_cli
from harness.adapters.claude import ClaudeDriver
from harness.contracts import (
    AttentionReason,
    CallbackEnvelope,
    EffectOutcome,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
    to_dict,
)
from harness.state_machine import TERMINAL
from harness.store import OperationStore
from harness.verification import load_profiles
from harness.runtime_worker import _pipeline_verify_identity
from harness.runtime_session_contracts import (
    RuntimeCheckpointEvidenceMissing,
    RuntimeSessionError,
    continuation_effect_id,
)
from harness.review_program import ReviewBoundaryInput
from harness.review_program_authority import trusted_review_receipt
from harness.custom_pipelines import (
    CustomPipelinePolicy,
    ExplicitPipelineApproval,
    FrozenPipelineStore,
    compile_custom_spec,
    freeze_custom_pipeline,
    parse_pipeline_spec,
    render_custom_approval,
)
from harness.pipeline_builtins import builtin_registry, compiled_builtin
from harness.workflows.review import (
    ReviewContext,
    ReviewFinding,
    ReviewOperationRequest,
    ReviewResult,
    review_round_envelope,
    review_round_payload,
    start_review,
)
from harness.workflows.review_gate import (
    ReviewGateController,
    ReviewPreset,
    ReviewScopeBoundary,
    authorize_task_finalization,
    review_context_sha256,
)
from harness.workflows.review_gate_contracts import _result_from_payload
from review_resolution import (
    FindingResolution,
    ReviewResolutionEvidence,
    review_transport_identity_sha256,
)
from review_contract import axis_finding_id
from outcome_contract import extract_from_bytes


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def quiesce_operations(store: OperationStore, task_id: str) -> None:
    """Release one test review using the same exact terminal procedure."""

    for record in store.list(task_id):
        if record.state in TERMINAL:
            continue
        store.transition(task_id, record.spec.operation_id, "cancelling")
        store.transition(task_id, record.spec.operation_id, "exiting")
        store.transition(task_id, record.spec.operation_id, "cancelled")
        cancelled = store.read(task_id, record.spec.operation_id)
        if cancelled.resources != OwnedResources():
            store.save(
                replace(
                    cancelled,
                    resources=OwnedResources(),
                    revision=cancelled.revision + 1,
                ),
                expected_revision=cancelled.revision,
            )


def write_trusted_current_approval(
    gate_root: Path,
    operation_id: str,
    head_sha: str,
    *,
    reviewed_head_sha: str = "",
    valid_resolution_proof: bool = True,
) -> None:
    """Complete a current-review fixture with authority-verifiable evidence."""

    state_path = gate_root / "review-gate.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    context = state["context"]
    context["head_sha"] = head_sha
    run_id = "trusted-current-run"
    axes = (
        "anthropic-holistic",
        "openai-holistic",
    )
    terminal_iteration = (
        1
        if reviewed_head_sha
        and reviewed_head_sha != head_sha
        and valid_resolution_proof
        else 0
    )
    aggregate_axes = []
    final_results = {}
    for axis in axes:
        short = axis
        pointer = f"final-{short}.json"
        (gate_root / pointer).write_text(
            json.dumps(
                {
                    "axis": axis,
                    "findings": [],
                    "verdict": "approve",
                    "verification_iteration": terminal_iteration,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        final_results[axis] = pointer
        aggregate_axes.append(
            {
                "axis": axis,
                "findings": [],
                "verdict": "approve",
                "verification_iteration": terminal_iteration,
            }
        )
    payload = {
        "schema_version": 1,
        "operation_id": operation_id,
        "run_id": run_id,
        "mode": "deep",
        "head_sha": head_sha,
        "verification_profile": {
            "name": context["verification_profile"],
            "sha256": context["verification_profile_sha256"],
        },
        "verdict": "approve",
        "axes": aggregate_axes,
        "verification_gaps": [],
        "notes_for_executor": [],
        "residual_risks": [],
    }
    payload_bytes = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    callback = {
        "schema_version": 1,
        "callback_id": f"review-{payload_sha256[:24]}",
        "operation_id": operation_id,
        "run_id": run_id,
        "kind": "review",
        "payload": payload,
        "payload_sha256": payload_sha256,
    }
    callback_bytes = (json.dumps(callback, sort_keys=True) + "\n").encode()
    (gate_root / ".review-callback.json").write_bytes(callback_bytes)
    state["status"] = "approved"
    state["final_results"] = final_results
    state["evidence"] = {
        "operation_id": operation_id,
        "run_id": run_id,
        "pointer": ".review-callback.json",
        "sha256": hashlib.sha256(callback_bytes).hexdigest(),
    }
    if reviewed_head_sha and reviewed_head_sha != head_sha:
        resolution_entries = []
        resolution_pointers = {}
        resolution_root = gate_root / operation_id
        resolution_root.mkdir(parents=True, exist_ok=True)
        product_root = Path(state["product_root"])
        fix_delta = subprocess.run(
            [
                "git",
                "-C",
                str(product_root),
                "diff",
                "--binary",
                "--no-ext-diff",
                reviewed_head_sha,
                head_sha,
                "--",
            ],
            check=True,
            capture_output=True,
        ).stdout
        for axis in axes:
            short = axis
            pointer = f"{operation_id}/resolution-{short}-0.json"
            raw_finding_id = "authority-terminal-head-rebind"
            finding_id = axis_finding_id(axis, raw_finding_id)
            material_ids = (
                [finding_id]
                if valid_resolution_proof and axis == "openai-holistic"
                else []
            )
            resolutions = (
                [
                    {
                        "disposition": "applied",
                        "finding_id": finding_id,
                        "follow_up": "",
                        "rationale": "The exact changed HEAD was verified in the same review boundary.",
                    }
                ]
                if material_ids
                else []
            )
            raw = (
                json.dumps(
                    {
                        "axis": axis,
                        "fix_delta_sha256": (
                            hashlib.sha256(fix_delta).hexdigest()
                            if valid_resolution_proof
                            else "4" * 64
                        ),
                        "operation_id": operation_id,
                        "previous_finding_ids": material_ids,
                        "resolutions": resolutions,
                        "resolved_head_sha": head_sha,
                        "reviewed_head_sha": reviewed_head_sha,
                        "schema_version": 1,
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode()
            (gate_root / pointer).write_bytes(raw)
            resolution_entries.append(
                {"pointer": pointer, "sha256": hashlib.sha256(raw).hexdigest()}
            )
            resolution_pointers[f"{axis}:0"] = pointer
            if valid_resolution_proof:
                (resolution_root / f"round-{short}-0.json").write_text(
                    json.dumps(
                        {
                            "axis": axis,
                            "findings": (
                                [
                                    {
                                        "axis": axis,
                                        "evidence": "The implementation HEAD changed after the initial round.",
                                        "file": "product.py",
                                        "finding_id": raw_finding_id,
                                        "line": 1,
                                        "recommendation": "Resolve and verify the exact changed HEAD.",
                                        "severity": "important",
                                        "summary": "The terminal HEAD needs verified resolution authority.",
                                    }
                                ]
                                if material_ids
                                else []
                            ),
                            "verdict": (
                                "changes-requested" if material_ids else "approve"
                            ),
                            "verification_iteration": 0,
                        },
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        state["resolution_evidence"] = resolution_pointers
        (gate_root / ".review-meta.json").write_text(
            json.dumps(
                {
                    "head_sha": head_sha,
                    "operation_id": operation_id,
                    "resolution_evidence": resolution_entries,
                    "review_boundary_input_sha256": context[
                        "boundary_input_sha256"
                    ],
                    "schema_version": 1,
                    "worktree": state["product_root"],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if valid_resolution_proof:
            store = OperationStore(product_root / ".vault-meta/harness")
            route = RuntimeRoute(
                "codex",
                "gpt-5.6-terra",
                "medium",
                "reviewer-callback",
                "6" * 64,
            )
            proof_lanes = []
            for axis in axes:
                short = axis
                parent_id = f"{operation_id[:96]}-proof-{short}"
                lane_id = hashlib.sha256(
                    f"{parent_id}:lane".encode()
                ).hexdigest()[:32]
                parent_run_id = hashlib.sha256(
                    f"{parent_id}:run".encode()
                ).hexdigest()[:32]
                parent_spec = OperationSpec(
                    parent_id,
                    hashlib.sha256(
                        f"{parent_id}:parent".encode()
                    ).hexdigest(),
                    "simple-review-holistic",
                    operation_id,
                    route,
                    "packets/review/manifest.json",
                    "scoped",
                )
                store.create(
                    parent_spec,
                    lane_id=lane_id,
                    run_id=parent_run_id,
                )
                for parent_state in (
                    "preflight",
                    "starting",
                    "running",
                    "awaiting-callback",
                ):
                    store.transition(operation_id, parent_id, parent_state)
                for iteration, result_path in (
                    (0, resolution_root / f"round-{short}-0.json"),
                    (terminal_iteration, gate_root / f"final-{short}.json"),
                ):
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    role = f"round-{iteration}"
                    suffix = (
                        f"-round-{hashlib.sha256(role.encode()).hexdigest()[:8]}"
                    )
                    child_id = f"{parent_id[: 128 - len(suffix)]}{suffix}"
                    child_key = hashlib.sha256(
                        (
                            f"{parent_spec.idempotency_key}:{axis}:{role}:"
                            f"{child_id}"
                        ).encode()
                    ).hexdigest()
                    child_run_id = hashlib.sha256(
                        f"{child_key}:run".encode()
                    ).hexdigest()[:32]
                    child_spec = OperationSpec(
                        child_id,
                        child_key,
                        "review-round",
                        operation_id,
                        route,
                        "packets/review/manifest.json",
                        "scoped",
                    )
                    store.create(
                        child_spec,
                        lane_id=lane_id,
                        run_id=child_run_id,
                    )
                    for child_state in (
                        "preflight",
                        "starting",
                        "running",
                        "awaiting-callback",
                    ):
                        store.transition(operation_id, child_id, child_state)
                    payload = review_round_payload(
                        parent_id, _result_from_payload(result)
                    )
                    encoded = json.dumps(
                        payload, sort_keys=True, separators=(",", ":")
                    ).encode()
                    digest = hashlib.sha256(encoded).hexdigest()
                    CallbackBroker(store, operation_id).accept(
                        CallbackEnvelope(
                            f"review-{digest[:24]}",
                            child_id,
                            child_run_id,
                            "review",
                            payload,
                            digest,
                        )
                    )
                    child = store.read(operation_id, child_id)
                    if child.state == "verifying":
                        store.transition(
                            operation_id, child_id, "finalizing"
                        )
                    store.transition(operation_id, child_id, "exiting")
                    store.transition(operation_id, child_id, "complete")
                store.transition(operation_id, parent_id, "finalizing")
                store.transition(operation_id, parent_id, "exiting")
                store.transition(operation_id, parent_id, "complete")
                proof_lanes.append(
                    {
                        "axis": axis,
                        "checkpoint": f"checkpoint-{short}",
                        "lane_id": lane_id,
                        "operation_id": parent_id,
                        "run_id": parent_run_id,
                        "state": "complete",
                        "surface_id": "",
                        "verification_iteration": terminal_iteration,
                    }
                )
            state["lanes"] = proof_lanes
    state_path.write_text(
        json.dumps(state, sort_keys=True) + "\n", encoding="utf-8"
    )


regression_failures: list[str] = []


def resolution_evidence(
    operation_id: str,
    axis: str,
    reviewed_head: str,
    resolved_head: str,
    *finding_ids: str,
) -> ReviewResolutionEvidence:
    rows = {
        finding_id: FindingResolution(
            finding_id,
            "applied",
            "The fix and its regression check are present on the resolved HEAD.",
        )
        for finding_id in finding_ids
    }
    return ReviewResolutionEvidence(
        operation_id,
        axis,
        reviewed_head,
        resolved_head,
        hashlib.sha256(b"bounded fix delta").hexdigest(),
        tuple(finding_ids),
        rows,
    )


def resolution_transport_identity(
    controller: ReviewGateController,
) -> str:
    state = controller.read()
    persisted = str(
        state.get("resolution_transport_identity_sha256") or ""
    )
    if persisted:
        return persisted
    awaiting = state["awaiting_resolution"]
    review_operation_ids = {
        str(boundary["review_operation_id"])
        for boundary in awaiting.values()
    }
    assert len(review_operation_ids) == 1
    callbacks = [
        {
            "axis": axis,
            "round_operation_id": boundary["round_operation_id"],
            "round_run_id": boundary["round_run_id"],
            "callback_id": boundary["callback_id"],
            "callback_sha256": boundary["callback_sha256"],
        }
        for axis, boundary in sorted(awaiting.items())
    ]
    return review_transport_identity_sha256(
        next(iter(review_operation_ids)), callbacks
    )


simple_preset = ReviewPreset.from_flags()
deep_cross = ReviewPreset.from_flags(deep=True, cross_model=True)
override = ReviewPreset.from_flags(model="opus", effort="high")
disabled = ReviewPreset.from_flags(no_review=True)
check(
    "review presets are deterministic",
    simple_preset.depth == "simple"
    and simple_preset.max_verify_iterations == 1
    and deep_cross.depth == "deep"
    and deep_cross.cross_model
    and deep_cross.max_verify_iterations == 2
    and override.model == "opus"
    and override.effort == "high"
    and not disabled.enabled,
)
try:
    ReviewPreset.from_flags(deep=True, no_review=True)
except ValueError:
    check("no-review cannot hide an enabled preset", True)
else:
    check("no-review cannot hide an enabled preset", False)


@dataclass(frozen=True)
class FakeSessionResult:
    record: object
    checkpoint: str
    action: str = ""
    process_status: str = ""
    surface_status: str = ""
    checkpoint_sha256: str = ""


class FakeRuntime:
    def __init__(self, store: OperationStore) -> None:
        self.store = store
        self.started: list[object] = []
        self.continued: list[tuple[str, str, str, str]] = []
        self.registered: list[tuple[str, str, str, str, str]] = []
        self.rearmed: list[tuple[str, str]] = []
        self.exits: list[tuple[str, str]] = []
        self.cleanups: list[tuple[str, str]] = []
        self.cleanup_attention = False
        self.cleanup_terminate_once = False

    def start(self, request: object, *, on_surface_opened=None) -> FakeSessionResult:
        self.started.append(request)
        record = self.store.create(
            request.spec, lane_id=request.lane_id, run_id=request.run_id
        )
        if not record.resources.surface_id:
            record = replace(
                record,
                resources=replace(
                    record.resources,
                    surface_id=(
                        f"{len(self.started):08d}-AAAA-4AAA-8AAA-"
                        f"{len(self.started):012d}"
                    ),
                ),
            )
            self.store.save(record, expected_revision=record.revision)
        result = FakeSessionResult(record, "checkpoint-live")
        if on_surface_opened is not None:
            on_surface_opened(result)
        return result

    def status(self, owner_id: str, operation_id: str) -> object:
        return FakeSessionResult(
            self.store.read(owner_id, operation_id),
            "checkpoint-live",
            "observed",
            "alive",
            "alive",
        )

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
        return FakeSessionResult(
            self.store.read(owner_id, operation_id), checkpoint
        )

    def register_callback_target(
        self,
        owner_id: str,
        parent_operation_id: str,
        child_operation_id: str,
        child_run_id: str,
        callback_pointer: str,
    ) -> None:
        self.registered.append(
            (
                owner_id,
                parent_operation_id,
                child_operation_id,
                child_run_id,
                callback_pointer,
            )
        )

    def rearm_callback_timeout(
        self, owner_id: str, operation_id: str
    ) -> FakeSessionResult:
        self.rearmed.append((owner_id, operation_id))
        record = self.store.rearm_callback_timeout(
            owner_id,
            operation_id,
            deadline_at=10**20,
        )
        return FakeSessionResult(record, "checkpoint-live")

    def accept_callback(self, envelope: object) -> object:
        matches = [
            path.parents[1].name
            for path in (
                self.store.root / "owners"
            ).glob(
                f"*/operations/{envelope.operation_id}.json"
            )
        ]
        if len(matches) != 1:
            raise AssertionError("fake runtime callback owner is ambiguous")
        return CallbackBroker(self.store, matches[0]).accept(envelope)

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
        if self.cleanup_terminate_once:
            self.cleanup_terminate_once = False
            return FakeSessionResult(
                self.store.read(owner_id, operation_id),
                "",
                "terminate-orphan",
            )
        if self.cleanup_attention:
            self.store.transition(
                owner_id,
                operation_id,
                "attention-required",
                reason=AttentionReason.CLEANUP_INCOMPLETE,
            )
        else:
            self.store.transition(owner_id, operation_id, "complete")
        completed = self.store.read(owner_id, operation_id)
        if completed.state == "complete" and completed.resources != OwnedResources():
            self.store.save(
                replace(
                    completed,
                    resources=OwnedResources(),
                    revision=completed.revision + 1,
                ),
                expected_revision=completed.revision,
            )
        return self.store.read(owner_id, operation_id)


class EffectRecordingRuntime(FakeRuntime):
    """Mirror the real runtime's durable continuation effect receipt."""

    def continue_session(
        self,
        owner_id: str,
        operation_id: str,
        checkpoint: str,
        prompt_pointer: str,
    ) -> FakeSessionResult:
        request = next(
            item
            for item in self.started
            if item.spec.operation_id == operation_id
        )
        prompt = (request.cwd / prompt_pointer).read_text(encoding="utf-8")
        current = self.store.read(owner_id, operation_id)
        self.store.save(
            replace(
                current,
                effect_id=continuation_effect_id(prompt),
                effect_outcome=EffectOutcome.SUCCEEDED,
                revision=current.revision + 1,
            ),
            expected_revision=current.revision,
        )
        return super().continue_session(
            owner_id, operation_id, checkpoint, prompt_pointer
        )


class CheckpointRecoveryRuntime(FakeRuntime):
    def __init__(self, store: OperationStore) -> None:
        super().__init__(store)
        self.hydrations: list[tuple[str, str, str]] = []
        self.continue_attempts = 0
        self.provider_prompt_effects = 0
        self.fail_first_continuation = False

    def status(self, owner_id: str, operation_id: str) -> object:
        return FakeSessionResult(
            self.store.read(owner_id, operation_id),
            "",
            "attention-required",
            "alive",
            "alive",
        )

    def hydrate_durable_checkpoint(
        self, owner_id: str, operation_id: str, lane_id: str
    ) -> FakeSessionResult:
        record = self.store.read(owner_id, operation_id)
        if record.lane_id != lane_id:
            raise ValueError("checkpoint lane changed")
        self.hydrations.append((owner_id, operation_id, lane_id))
        return FakeSessionResult(
            record,
            "checkpoint-owned",
            action="checkpoint-hydrated",
            checkpoint_sha256="9" * 64,
        )

    def continue_session(
        self,
        owner_id: str,
        operation_id: str,
        checkpoint: str,
        prompt_pointer: str,
    ) -> FakeSessionResult:
        self.continue_attempts += 1
        if self.fail_first_continuation and self.continue_attempts == 1:
            raise ValueError("injected pre-effect checkpoint interruption")
        self.provider_prompt_effects += 1
        return super().continue_session(
            owner_id, operation_id, checkpoint, prompt_pointer
        )


class AcceptedRoundWithoutCheckpointRuntime(FakeRuntime):
    def __init__(self, store: OperationStore) -> None:
        super().__init__(store)
        self.hydration_attempts = 0

    def status(self, owner_id: str, operation_id: str) -> object:
        return FakeSessionResult(
            self.store.read(owner_id, operation_id),
            "",
            "observed",
            "alive",
            "alive",
        )

    def hydrate_durable_checkpoint(
        self, owner_id: str, operation_id: str, lane_id: str
    ) -> FakeSessionResult:
        self.hydration_attempts += 1
        raise RuntimeCheckpointEvidenceMissing(
            "durable checkpoint evidence is unavailable"
        )


class PartialFullRecoveryRuntime(FakeRuntime):
    def __init__(self, store: OperationStore) -> None:
        super().__init__(store)
        self.effect_ids: dict[str, str] = {}
        self.provider_effects: dict[str, int] = {}
        self.timeout_once: set[str] = set()

    def continue_session(
        self,
        owner_id: str,
        operation_id: str,
        checkpoint: str,
        prompt_pointer: str,
    ) -> FakeSessionResult:
        if operation_id in self.timeout_once:
            self.timeout_once.remove(operation_id)
            current = self.store.read(owner_id, operation_id)
            self.store.save(
                replace(
                    current,
                    deadline_at=1.0,
                    revision=current.revision + 1,
                ),
                expected_revision=current.revision,
            )
            self.store.transition(
                owner_id,
                operation_id,
                "attention-required",
                reason=AttentionReason.CALLBACK_TIMEOUT,
            )
            raise ValueError("injected callback timeout before prompt effect")
        self.provider_effects[operation_id] = (
            self.provider_effects.get(operation_id, 0) + 1
        )
        current = self.store.read(owner_id, operation_id)
        updated = replace(
            current,
            effect_id=self.effect_ids[operation_id],
            effect_outcome=EffectOutcome.SUCCEEDED,
            revision=current.revision + 1,
        )
        self.store.save(updated, expected_revision=current.revision)
        return super().continue_session(
            owner_id, operation_id, checkpoint, prompt_pointer
        )

def request_for(
    operation_id: str,
    *,
    depth: str = "simple",
    context: ReviewContext,
) -> ReviewOperationRequest:
    primary = RuntimeRoute(
        "claude",
        "fable",
        "xhigh" if depth == "deep" else "high",
        "reviewer-callback",
        "a" * 64,
    )
    preset = ReviewPreset.from_flags(
        deep=depth == "deep", full=depth == "full"
    )
    max_verify_iterations = (
        0
        if context.purpose == "release"
        else min(preset.max_verify_iterations, 1)
        if context.purpose == "intent"
        else preset.max_verify_iterations
    )
    policy = preset.request(
        operation_id,
        purpose=context.purpose,
        max_verify_iterations=max_verify_iterations,
        selected_provider=(
            "" if depth in {"deep", "full"} else "anthropic"
        ),
    )
    axes = (
        {
            "anthropic-holistic": primary,
            "openai-holistic": RuntimeRoute(
                "codex",
                "gpt-5.6-sol",
                "xhigh",
                "reviewer-callback",
                "b" * 64,
            ),
        }
        if depth == "deep"
        else {
            "anthropic-intent": primary,
            "anthropic-engineering": primary,
            "openai-intent": RuntimeRoute(
                "codex",
                "gpt-5.6-sol",
                "xhigh",
                "reviewer-callback",
                "b" * 64,
            ),
            "openai-engineering": RuntimeRoute(
                "codex",
                "gpt-5.6-sol",
                "xhigh",
                "reviewer-callback",
                "b" * 64,
            ),
        }
        if depth == "full"
        else None
    )
    return ReviewOperationRequest(
        policy, "owner-1", primary, context, axis_routes=axes
    )


def begin(
    controller: ReviewGateController,
    request: ReviewOperationRequest,
    scratch: Path,
) -> object:
    return controller.begin(
        dispatch_operation_id="dispatch-1",
        request=request,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=ROOT,
        prompt_pointer="prompts/review.md",
        callback_root=f"callbacks/{request.policy.operation_id}",
    )


context = ReviewContext(
    manifest="packets/review/manifest.json",
    head_sha="c" * 40,
    verification_profile="scoped",
    verification_profile_sha256="d" * 64,
)

with tempfile.TemporaryDirectory(prefix="release-review-gate.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    release_context = replace(
        context,
        purpose="release",
        boundary_input_sha256="9" * 64,
    )
    run = begin(
        controller,
        request_for("review-release-stop", context=release_context),
        scratch,
    )
    lane = run.execution.lanes[0]
    stopped = controller.complete_round(
        run,
        lane,
        run.rounds["anthropic-holistic"],
        ReviewResult(
            "anthropic-holistic",
            "changes-requested",
            (
                ReviewFinding(
                    "F-release-stop",
                    "anthropic-holistic",
                    "important",
                    "release evidence is incomplete",
                    "the required evidence map has a material gap",
                ),
            ),
        ),
    )
    check(
        "release finding stops the boundary without a fix loop",
        stopped.action == "stopped"
        and controller.read()["status"] == "stopped"
        and controller.read().get("awaiting_resolution") in ({}, None)
        and run.execution.request.policy.max_verify_iterations == 0,
    )

with tempfile.TemporaryDirectory(prefix="review-gate.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(controller, request_for("review-auto", context=context), scratch)
    check(
        "automatic simple gate starts one persistent holistic lane",
        len(run.execution.lanes) == 1
        and run.execution.lanes[0].axis == "anthropic-holistic"
        and len(runtime.started) == 1
        and controller.read()["status"] == "reviewing",
    )
    lane = run.execution.lanes[0]
    waiting = controller.complete_round(
        run,
        lane,
        run.rounds["anthropic-holistic"],
        ReviewResult(
            "anthropic-holistic",
            "changes-requested",
            (
                ReviewFinding(
                    "F-gate-1",
                    "anthropic-holistic",
                    "important",
                    "material regression",
                    "focused test fails",
                ),
            ),
        ),
    )
    resolved_context = replace(context, head_sha="f" * 40)
    try:
        controller.continue_after_resolution(
            run,
            lane,
            context=resolved_context,
            resolution=resolution_evidence(
                "review-auto", "anthropic-holistic", context.head_sha,
                resolved_context.head_sha, "F-gate-1",
            ),
            review_identity_sha256="0" * 64,
            verification_prompt_pointer="prompts/verify.md",
            callback_pointer=(
                "callbacks/review-auto/anthropic-holistic/.review-callback.json"
            ),
        )
    except ValueError:
        pass
    else:
        regression_failures.append(
            "controller accepted a prior-boundary review identity"
        )
    check(
        "controller rejects prior-boundary review identity",
        controller.read()["status"] == "awaiting-resolution",
    )
    parent_before_timeout = store.read(lane.owner_id, lane.operation_id)
    store.save(
        replace(
            parent_before_timeout,
            deadline_at=1.0,
            revision=parent_before_timeout.revision + 1,
        ),
        expected_revision=parent_before_timeout.revision,
    )
    store.transition(
        lane.owner_id,
        lane.operation_id,
        "attention-required",
        reason=AttentionReason.CALLBACK_TIMEOUT,
    )
    orphan_resolution = (
        base / "gate" / "review-auto" / "resolution-anthropic-holistic-0.json"
    )
    orphan_resolution.parent.mkdir(parents=True, exist_ok=True)
    orphan_resolution.write_text(
        json.dumps(
            resolution_evidence(
                "review-auto",
                "anthropic-holistic",
                context.head_sha,
                "e" * 40,
                "F-gate-1",
            ).payload(),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    first = controller.continue_after_resolution(
        run,
        lane,
        context=resolved_context,
        resolution=resolution_evidence(
            "review-auto", "anthropic-holistic", context.head_sha,
            resolved_context.head_sha, "F-gate-1",
        ),
        review_identity_sha256=resolution_transport_identity(controller),
        verification_prompt_pointer="prompts/verify.md",
        callback_pointer="callbacks/review-auto/anthropic-holistic/.review-callback.json",
    )
    check(
        "material finding waits for resolution, then continues exact session",
        waiting.action == "awaiting-resolution"
        and first.action == "verify"
        and first.lane is not None
        and first.lane.operation_id == lane.operation_id
        and first.lane.surface_id == lane.surface_id
        and first.lane.verification_iteration == 1
        and len(runtime.started) == 1
        and len(runtime.continued) == 1,
    )
    check(
        "accepted material round rearms its exact timed-out parent before verification",
        store.read(lane.owner_id, lane.operation_id).state
        == "awaiting-callback"
        and store.read(lane.owner_id, lane.operation_id).deadline_at == 10**20
        and runtime.rearmed == [(lane.owner_id, lane.operation_id)],
    )
    check(
        "unpublished resolution crash residue is replaced before durable publication",
        json.loads(orphan_resolution.read_text(encoding="utf-8"))[
            "resolved_head_sha"
        ]
        == resolved_context.head_sha,
    )
    resolution_pointer = controller.read()["resolution_evidence"][
        "anthropic-holistic:0"
    ]
    check(
        "review gate persists per-finding resolution evidence before verification",
        (base / "gate" / resolution_pointer).is_file()
        and json.loads(
            (base / "gate" / resolution_pointer).read_text(encoding="utf-8")
        )["previous_finding_ids"] == ["F-gate-1"],
    )
    run = controller.rehydrate()
    first = replace(
        first,
        lane=run.execution.lanes[0],
        round=run.rounds["anthropic-holistic"],
    )
    approved = controller.complete_round(
        run,
        first.lane,
        first.round,
        ReviewResult("anthropic-holistic", "approve", verification_iteration=1),
    )
    authorization = authorize_task_finalization(
        base / "gate",
        dispatch_operation_id="dispatch-1",
        expected_head_sha=resolved_context.head_sha,
        expected_profile=context.verification_profile,
        expected_profile_sha256=context.verification_profile_sha256,
    )
    check(
        "approved evidence unlocks task summary and reap",
        approved.action == "approved"
        and authorization.approved
        and not authorization.skipped
        and authorization.evidence["verdict"] == "approve",
    )
    state_path = base / "gate" / "review-gate.json"
    approved_state = state_path.read_text(encoding="utf-8")
    missing_lanes = json.loads(approved_state)
    missing_lanes["lanes"] = []
    state_path.write_text(json.dumps(missing_lanes), encoding="utf-8")
    try:
        authorize_task_finalization(
            base / "gate",
            dispatch_operation_id="dispatch-1",
            expected_head_sha=resolved_context.head_sha,
            expected_profile=context.verification_profile,
            expected_profile_sha256=context.verification_profile_sha256,
        )
    except ValueError as exc:
        check(
            "approved finalization fails closed without durable lane identity",
            "lane identity" in str(exc),
        )
    else:
        check(
            "approved finalization fails closed without durable lane identity",
            False,
        )
    finally:
        state_path.write_text(approved_state, encoding="utf-8")
    try:
        authorize_task_finalization(
            base / "gate",
            dispatch_operation_id="dispatch-1",
            expected_head_sha="e" * 40,
            expected_profile=context.verification_profile,
            expected_profile_sha256=context.verification_profile_sha256,
        )
    except ValueError:
        check("stale review evidence never unlocks finalization", True)
    else:
        check("stale review evidence never unlocks finalization", False)

with tempfile.TemporaryDirectory(prefix="review-checkpoint-replay.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = CheckpointRecoveryRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    operation_id = "review-checkpoint-replay"
    run = begin(
        controller,
        request_for(operation_id, context=context),
        scratch,
    )
    lane = run.execution.lanes[0]
    waiting = controller.defer_round_for_resolution(
        run,
        lane,
        run.rounds[lane.axis],
        ReviewResult(
            lane.axis,
            "changes-requested",
            (
                ReviewFinding(
                    "F-checkpoint-replay",
                    lane.axis,
                    "important",
                    "checkpoint must survive coordinator restart",
                    "the provider session remains exact and live",
                ),
            ),
        ),
    )
    parent = store.read(lane.owner_id, lane.operation_id)
    store.save(
        replace(
            parent,
            deadline_at=1.0,
            revision=parent.revision + 1,
        ),
        expected_revision=parent.revision,
    )
    store.transition(
        lane.owner_id,
        lane.operation_id,
        "attention-required",
        reason=AttentionReason.CALLBACK_TIMEOUT,
    )
    state_path = base / "gate" / "review-gate.json"
    checkpointless = json.loads(state_path.read_text(encoding="utf-8"))
    checkpointless["lanes"][0]["checkpoint"] = ""
    checkpointless["lanes"][0].pop("checkpoint_sha256", None)
    state_path.write_text(
        json.dumps(checkpointless, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    recovered_run = controller.rehydrate()
    recovered_lane = recovered_run.execution.lanes[0]
    persisted_hydration = controller.read()["lanes"][0]
    controller.rehydrate()
    check(
        "empty review checkpoint hydrates once and persists its digest",
        waiting.action == "awaiting-resolution"
        and recovered_lane.checkpoint == "checkpoint-owned"
        and persisted_hydration["checkpoint"] == "checkpoint-owned"
        and persisted_hydration["checkpoint_sha256"] == "9" * 64
        and runtime.hydrations
        == [(lane.owner_id, lane.operation_id, lane.lane_id)]
        and len(runtime.started) == 1,
    )
    resolved_context = replace(context, head_sha="8" * 40)
    resolution = resolution_evidence(
        operation_id,
        lane.axis,
        context.head_sha,
        resolved_context.head_sha,
        "F-checkpoint-replay",
    )
    review_identity = resolution_transport_identity(controller)
    runtime.fail_first_continuation = True
    try:
        controller.continue_after_resolution(
            recovered_run,
            recovered_lane,
            context=resolved_context,
            resolution=resolution,
            review_identity_sha256=review_identity,
            verification_prompt_pointer="prompts/verify.md",
            callback_pointer=(
                "callbacks/review-checkpoint-replay/"
                "anthropic-holistic/.review-callback.json"
            ),
        )
    except ValueError as exc:
        check(
            "checkpoint interruption occurs before a provider effect",
            "pre-effect" in str(exc)
            and runtime.provider_prompt_effects == 0,
        )
    else:
        check("checkpoint interruption occurs before a provider effect", False)
    unpublished_child = runtime.registered[-1][2]
    child_before_replay = store.read(lane.owner_id, unpublished_child)
    replay_run = controller.rehydrate()
    replay_lane = replay_run.execution.lanes[0]
    resumed = controller.continue_after_resolution(
        replay_run,
        replay_lane,
        context=resolved_context,
        resolution=resolution,
        review_identity_sha256=review_identity,
        verification_prompt_pointer="prompts/verify.md",
        callback_pointer=(
            "callbacks/review-checkpoint-replay/"
            "anthropic-holistic/.review-callback.json"
        ),
    )
    check(
        "checkpoint recovery reuses one unpublished child and one provider effect",
        resumed.action == "verify"
        and child_before_replay.resources == OwnedResources()
        and runtime.registered[-2][2] == unpublished_child
        and runtime.registered[-1][2] == unpublished_child
        and runtime.rearmed
        == [(lane.owner_id, lane.operation_id)]
        and runtime.continue_attempts == 2
        and runtime.provider_prompt_effects == 1
        and len(runtime.started) == 1
        and len(runtime.hydrations) == 1,
    )

with tempfile.TemporaryDirectory(
    prefix="review-accepted-without-checkpoint."
) as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = AcceptedRoundWithoutCheckpointRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-accepted-without-checkpoint", context=context),
        scratch,
    )
    lane = run.execution.lanes[0]
    round_ = run.rounds[lane.axis]
    result = ReviewResult(lane.axis, "approve", (), 0)
    runtime.accept_callback(review_round_envelope(round_, result))
    accepted = store.read(round_.owner_id, round_.operation_id)
    store.save(
        replace(
            accepted,
            state="verifying",
            revision=accepted.revision + 1,
        ),
        expected_revision=accepted.revision,
    )
    state_path = base / "gate" / "review-gate.json"
    checkpointless = json.loads(state_path.read_text(encoding="utf-8"))
    checkpointless["lanes"][0]["checkpoint"] = ""
    checkpointless["lanes"][0].pop("checkpoint_sha256", None)
    state_path.write_text(
        json.dumps(checkpointless, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    recovered = controller.rehydrate()
    recovered_lane = recovered.execution.lanes[0]
    decision = controller.complete_round(
        recovered,
        recovered_lane,
        recovered.rounds[recovered_lane.axis],
        result,
    )
    check(
        "accepted terminal callback does not require a resume checkpoint",
        runtime.hydration_attempts == 1
        and recovered_lane.checkpoint == ""
        and decision.action == "approved"
        and controller.read()["status"] == "approved",
    )

with tempfile.TemporaryDirectory(
    prefix="review-missing-round-without-checkpoint."
) as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = AcceptedRoundWithoutCheckpointRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-missing-round-without-checkpoint", context=context),
        scratch,
    )
    lane = run.execution.lanes[0]
    round_ = run.rounds[lane.axis]
    round_path = (
        store.root
        / "owners"
        / round_.owner_id
        / "operations"
        / f"{round_.operation_id}.json"
    )
    round_path.unlink()
    state_path = base / "gate" / "review-gate.json"
    checkpointless = json.loads(state_path.read_text(encoding="utf-8"))
    checkpointless["lanes"][0]["checkpoint"] = ""
    checkpointless["lanes"][0].pop("checkpoint_sha256", None)
    state_path.write_text(
        json.dumps(checkpointless, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        controller.rehydrate()
    except RuntimeCheckpointEvidenceMissing:
        check(
            "missing checkpoint recovery does not create a child round",
            not round_path.exists(),
        )
    else:
        check(
            "missing checkpoint recovery does not create a child round",
            False,
        )

with tempfile.TemporaryDirectory(
    prefix="review-partial-full-without-checkpoints."
) as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = AcceptedRoundWithoutCheckpointRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for(
            "review-partial-full-without-checkpoints",
            depth="full",
            context=context,
        ),
        scratch,
    )
    accepted_lane = run.execution.lanes[2]
    accepted_round = run.rounds[accepted_lane.axis]
    result = ReviewResult(accepted_lane.axis, "approve", (), 0)
    runtime.accept_callback(review_round_envelope(accepted_round, result))
    accepted_record = store.read(
        accepted_round.owner_id, accepted_round.operation_id
    )
    store.save(
        replace(
            accepted_record,
            state="verifying",
            revision=accepted_record.revision + 1,
        ),
        expected_revision=accepted_record.revision,
    )
    state_path = base / "gate" / "review-gate.json"
    checkpointless = json.loads(state_path.read_text(encoding="utf-8"))
    for raw_lane in checkpointless["lanes"]:
        raw_lane["checkpoint"] = ""
        raw_lane.pop("checkpoint_sha256", None)
    state_path.write_text(
        json.dumps(checkpointless, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        recovered = controller.rehydrate()
    except RuntimeCheckpointEvidenceMissing:
        recovered = None
    check(
        "partial Full callback tolerates live sibling lanes without checkpoints",
        recovered is not None
        and len(recovered.execution.lanes) == 4
        and recovered.rounds[accepted_lane.axis].operation_id
        == accepted_round.operation_id,
    )

with tempfile.TemporaryDirectory(prefix="review-full-partial-progress.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = PartialFullRecoveryRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    operation_id = "review-full-partial-progress"
    run = begin(
        controller,
        request_for(operation_id, depth="full", context=context),
        scratch,
    )
    lanes = run.execution.lanes
    for index, lane in enumerate(lanes):
        controller.defer_round_for_resolution(
            run,
            lane,
            run.rounds[lane.axis],
            ReviewResult(
                lane.axis,
                "changes-requested",
                (
                    ReviewFinding(
                        f"F-full-partial-{index}",
                        lane.axis,
                        "important",
                        "retain exact partial progress",
                        "each continuation effect is independently durable",
                    ),
                ),
            ),
        )
    resolved_context = replace(context, head_sha="7" * 40)
    review_identity = resolution_transport_identity(controller)
    resolutions = {
        lane.axis: resolution_evidence(
            operation_id,
            lane.axis,
            context.head_sha,
            resolved_context.head_sha,
            axis_finding_id(lane.axis, f"F-full-partial-{index}"),
        )
        for index, lane in enumerate(lanes)
    }
    for index, lane in enumerate(lanes):
        runtime.effect_ids[lane.operation_id] = (
            "continue-" + str(index + 1) * 32
        )
    first = controller.continue_after_resolution(
        run,
        lanes[0],
        context=resolved_context,
        resolution=resolutions[lanes[0].axis],
        review_identity_sha256=review_identity,
        verification_prompt_pointer="prompts/verify-0.md",
        callback_pointer="callbacks/full/0.json",
        continuation_effect_id=runtime.effect_ids[lanes[0].operation_id],
    )
    post_first = controller.rehydrate()
    first_lane = next(
        lane for lane in post_first.execution.lanes
        if lane.axis == lanes[0].axis
    )
    controller._replace(continuation_effects={})
    for _ in range(2):
        controller.backfill_succeeded_continuation_receipt(
            first_lane,
            post_first.rounds[first_lane.axis],
            resolutions[first_lane.axis],
            runtime.effect_ids[first_lane.operation_id],
        )
    second_parent = store.read(lanes[1].owner_id, lanes[1].operation_id)
    store.save(
        replace(
            second_parent,
            deadline_at=1.0,
            revision=second_parent.revision + 1,
        ),
        expected_revision=second_parent.revision,
    )
    store.transition(
        lanes[1].owner_id,
        lanes[1].operation_id,
        "attention-required",
        reason=AttentionReason.CALLBACK_TIMEOUT,
    )
    runtime.timeout_once.add(lanes[1].operation_id)
    try:
        controller.continue_after_resolution(
            controller.rehydrate(),
            controller.rehydrate().execution.lanes[1],
            context=resolved_context,
            resolution=resolutions[lanes[1].axis],
            review_identity_sha256=review_identity,
            verification_prompt_pointer="prompts/verify-1.md",
            callback_pointer="callbacks/full/1.json",
            continuation_effect_id=(
                runtime.effect_ids[lanes[1].operation_id]
            ),
        )
    except ValueError as exc:
        check(
            "full partial progress stops lane N before its prompt effect",
            "before prompt effect" in str(exc),
        )
    else:
        check("full partial progress stops lane N before its prompt effect", False)
    prepared_state = controller.read()
    child_for_second = runtime.registered[-1][2]
    child_before_replay = store.read(lanes[1].owner_id, child_for_second)
    for index in range(1, len(lanes)):
        replay = controller.rehydrate()
        replay_lane = next(
            lane for lane in replay.execution.lanes
            if lane.axis == lanes[index].axis
        )
        controller.continue_after_resolution(
            replay,
            replay_lane,
            context=resolved_context,
            resolution=resolutions[replay_lane.axis],
            review_identity_sha256=review_identity,
            verification_prompt_pointer=f"prompts/verify-{index}.md",
            callback_pointer=f"callbacks/full/{index}.json",
            continuation_effect_id=runtime.effect_ids[
                replay_lane.operation_id
            ],
        )
    final_state = controller.read()
    receipts = final_state["continuation_effects"]
    check(
        "multi-lane replay resumes from N without replaying prior effects",
        first.action == "verify"
        and child_before_replay.resources == OwnedResources()
        and prepared_state["continuation_effects"][lanes[1].axis + ":0"][
            "state"
        ]
        == "prepared"
        and all(receipt["state"] == "succeeded" for receipt in receipts.values())
        and runtime.provider_effects
        == {lane.operation_id: 1 for lane in lanes}
        and len(runtime.started) == 4
        and sum(
            item[2] == child_for_second for item in runtime.registered
        )
        == 2,
    )

with tempfile.TemporaryDirectory(prefix="review-program-real-resolution.") as raw:
    container = Path(raw)
    product = container / "product"
    scratch = container / "scratch"
    product.mkdir()
    scratch.mkdir()
    verification_path = product / "docs/verification.json"
    verification_path.parent.mkdir()
    verification_path.write_text('{"status":"passed"}\n', encoding="utf-8")
    product_file = product / "product.py"
    product_file.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=product, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Review Gate Test"],
        cwd=product,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "review-gate@example.invalid"],
        cwd=product,
        check=True,
    )
    subprocess.run(["git", "add", "docs", "product.py"], cwd=product, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "reviewed candidate"],
        cwd=product,
        check=True,
    )
    reviewed_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    boundary = ReviewBoundaryInput(
        purpose="implementation",
        outcome_contract_sha256="a" * 64,
        plan_sha256="b" * 64,
        product_head_sha=reviewed_head,
        verification_evidence_sha256=hashlib.sha256(
            verification_path.read_bytes()
        ).hexdigest(),
        verification_evidence_path="docs/verification.json",
    )
    operation_id = "review-program-real-resolution"
    gate_root = (
        product
        / ".vault-meta/harness/review-data"
        / operation_id
        / operation_id
    )
    store = OperationStore(product / ".vault-meta/harness")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(gate_root, runtime, store)
    real_context = ReviewContext(
        manifest="packets/review/manifest.json",
        head_sha=reviewed_head,
        verification_profile="scoped",
        verification_profile_sha256="d" * 64,
        purpose="implementation",
        boundary_input_sha256=boundary.input_sha256,
    )
    request = replace(
        request_for(operation_id, context=real_context),
        owner_id=operation_id,
    )
    run = controller.begin(
        dispatch_operation_id=operation_id,
        request=request,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=product,
        prompt_pointer="prompts/review.md",
        callback_root=f"callbacks/{operation_id}",
    )
    lane = run.execution.lanes[0]
    controller.defer_round_for_resolution(
        run,
        lane,
        run.rounds["anthropic-holistic"],
        ReviewResult(
            "anthropic-holistic",
            "changes-requested",
            (
                ReviewFinding(
                    "F-real-resolution",
                    "anthropic-holistic",
                    "important",
                    "the reviewed candidate needs one exact fix",
                    "the initial production value is incorrect",
                ),
            ),
        ),
    )
    product_file.write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.py"], cwd=product, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "resolve review finding"],
        cwd=product,
        check=True,
    )
    resolved_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    fix_delta = subprocess.run(
        [
            "git",
            "diff",
            "--binary",
            "--no-ext-diff",
            reviewed_head,
            resolved_head,
            "--",
        ],
        cwd=product,
        check=True,
        capture_output=True,
    ).stdout
    controller.continue_after_resolution(
        run,
        lane,
        context=replace(real_context, head_sha=resolved_head),
        resolution=ReviewResolutionEvidence(
            operation_id,
            "anthropic-holistic",
            reviewed_head,
            resolved_head,
            hashlib.sha256(fix_delta).hexdigest(),
            ("F-real-resolution",),
            {
                "F-real-resolution": FindingResolution(
                    "F-real-resolution",
                    "applied",
                    "The exact fix is committed and ready for same-session verification.",
                )
            },
        ),
        review_identity_sha256=resolution_transport_identity(controller),
        verification_prompt_pointer="prompts/verify.md",
        callback_pointer=f"callbacks/{operation_id}/anthropic-holistic/.review-callback.json",
    )
    run = controller.rehydrate()
    terminal = controller.complete_round(
        run,
        run.execution.lanes[0],
        run.rounds["anthropic-holistic"],
        ReviewResult("anthropic-holistic", "approve", verification_iteration=1),
    )
    receipt = trusted_review_receipt(product, boundary, operation_id)
    check(
        "real controller resolution and same-session approval mint moved-HEAD authority",
        terminal.action == "approved" and receipt.verdict == "approved",
    )

with tempfile.TemporaryDirectory(prefix="review-bounded-summary.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-bounded-summary", context=context),
        scratch,
    )
    lane = run.execution.lanes[0]
    decision = controller.complete_round(
        run,
        lane,
        run.rounds["anthropic-holistic"],
        ReviewResult(
            "anthropic-holistic",
            "approve",
            (
                ReviewFinding(
                    "F-long-summary",
                    "anthropic-holistic",
                    "minor",
                    "s" * 300,
                    "full evidence remains available",
                ),
            ),
        ),
    )
    authorization = authorize_task_finalization(
        base / "gate",
        dispatch_operation_id="dispatch-1",
        expected_head_sha=context.head_sha,
        expected_profile=context.verification_profile,
        expected_profile_sha256=context.verification_profile_sha256,
    )
    bounded = authorization.evidence["axes"][0]["findings"][0]
    check(
        "canonical maximum finding summaries are preserved in final evidence",
        decision.action == "approved"
        and len(bounded["summary"]) == 300
        and bounded["summary"] == "s" * 300
        and bounded["evidence"] == "full evidence remains available",
    )

with tempfile.TemporaryDirectory(prefix="review-summary-refresh.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    product = base / "product"
    scratch.mkdir()
    product.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    reviewed_context = replace(
        context,
        manifest="packets/reviewed-summary/manifest.json",
        implementer_summary_sha256="a" * 64,
    )
    summary_request = request_for(
        "review-summary-refresh", context=reviewed_context
    )
    run = controller.begin(
        dispatch_operation_id="dispatch-1",
        request=summary_request,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=product,
        prompt_pointer="prompts/review.md",
        callback_root="callbacks/review-summary-refresh",
    )
    lane = run.execution.lanes[0]
    approved = controller.complete_round(
        run,
        lane,
        run.rounds["anthropic-holistic"],
        ReviewResult("anthropic-holistic", "approve"),
    )
    for terminal_record in store.list("owner-1"):
        if terminal_record.state in TERMINAL:
            store.save(
                replace(
                    terminal_record,
                    resources=OwnedResources(),
                    revision=terminal_record.revision + 1,
                ),
                expected_revision=terminal_record.revision,
            )
    refreshed_context = replace(
        reviewed_context,
        manifest="packets/refreshed-summary/manifest.json",
        implementer_summary_sha256="b" * 64,
    )
    boundary = ReviewScopeBoundary(
        "context",
        review_context_sha256(reviewed_context),
        review_context_sha256(refreshed_context),
        "approved summary bytes changed after resolution",
    )
    boundary_authorization = {
        "schema_version": 1,
        "operation_id": "review-summary-refresh",
        "kind": boundary.kind,
        "previous_context_sha256": boundary.previous_context_sha256,
        "next_context_sha256": boundary.next_context_sha256,
        "reason": boundary.reason,
        "authorization_provenance": "coordinator-approved",
        "verification_operation_id": "summary-refresh-test",
        "verification_receipt_sha256": "b" * 64,
        "status": "authorized",
    }
    authorization_path = controller.root / "summary-authorization.json"
    authorization_path.write_text(
        json.dumps(boundary_authorization, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (product / ".task-review.json").write_text("{}\n", encoding="utf-8")
    preserved_resolution = product / ".task-review-resolution.json"
    preserved_resolution.write_text(
        '{"schema_version":1,"preserved":true}\n',
        encoding="utf-8",
    )
    controller.authorize_fresh_summary_boundary(
        run,
        boundary=boundary,
        context=refreshed_context,
        authorization_pointer=authorization_path.name,
        authorization_sha256=hashlib.sha256(
            authorization_path.read_bytes()
        ).hexdigest(),
    )
    fresh = controller.restart_for_boundary(
        run,
        boundary=boundary,
        context=refreshed_context,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=product,
        prompt_pointer="prompts/summary-only.md",
        callback_root="callbacks/review-summary-refresh",
        max_verify_iterations=0,
    )
    summary_state = controller.read()
    check(
        "approved summary refresh launches one zero-verification boundary and preserves prior evidence",
        approved.action == "approved"
        and fresh is not None
        and len(runtime.started) == 2
        and fresh.execution.request.policy.max_verify_iterations == 0
        and summary_state["status"] == "reviewing"
        and summary_state["context"]["head_sha"] == reviewed_context.head_sha
        and summary_state["context"]["implementer_summary_sha256"] == "b" * 64
        and summary_state["fresh_reevaluation_used"] is True
        and len(summary_state["prior_approved_boundaries"]) == 1
        and summary_state["prior_approved_boundaries"][0]["context"]
        ["implementer_summary_sha256"]
        == "a" * 64
        and not (product / ".task-review.json").exists()
        and preserved_resolution.is_file()
        and json.loads(preserved_resolution.read_text(encoding="utf-8"))[
            "preserved"
        ]
        is True,
    )

with tempfile.TemporaryDirectory(prefix="review-cleanup-attention.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    runtime.cleanup_attention = True
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-cleanup-attention", context=context),
        scratch,
    )
    lane = run.execution.lanes[0]
    decision = controller.complete_round(
        run,
        lane,
        run.rounds[lane.axis],
        ReviewResult("anthropic-holistic", "approve"),
    )
    state = controller.read()
    check(
        "cleanup attention blocks approval after bounded exact-lane retries",
        decision.action == "attention-required"
        and state["status"] == "attention-required"
        and state["final_results"] == {}
        and state["evidence"] == {}
        and len(runtime.cleanups) == 3,
    )

with tempfile.TemporaryDirectory(prefix="review-callback-timeout.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-callback-timeout", context=context),
        scratch,
    )
    lane = run.execution.lanes[0]
    store.transition(
        lane.owner_id,
        lane.operation_id,
        "attention-required",
        reason=AttentionReason.CALLBACK_TIMEOUT,
    )
    decision = controller.complete_round(
        run,
        lane,
        run.rounds[lane.axis],
        ReviewResult("anthropic-holistic", "approve"),
    )
    state = controller.read()
    check(
        "late reviewer approval cannot erase durable callback timeout",
        decision.action == "attention-required"
        and state["status"] == "attention-required"
        and state["final_results"] == {}
        and state["evidence"] == {},
    )
    store.transition(
        lane.owner_id,
        lane.operation_id,
        "awaiting-callback",
    )
    controller.resume_bound_attention()
    check(
        "explicit runtime recovery rearms only the existing bound review gate",
        controller.read()["status"] == "reviewing"
        and controller.read()["lanes"][0]["operation_id"]
        == lane.operation_id,
    )

with tempfile.TemporaryDirectory(prefix="review-defer-callback-timeout.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for(
            "review-defer-callback-timeout",
            depth="deep",
            context=context,
        ),
        scratch,
    )
    lane = run.execution.lanes[0]
    store.transition(
        lane.owner_id,
        lane.operation_id,
        "attention-required",
        reason=AttentionReason.CALLBACK_TIMEOUT,
    )
    decision = controller.defer_round_for_resolution(
        run,
        lane,
        run.rounds[lane.axis],
        ReviewResult(lane.axis, "approve"),
    )
    state = controller.read()
    check(
        "deep resolution cannot mask durable callback timeout",
        decision.action == "attention-required"
        and state["status"] == "attention-required"
        and state["round_results"] == {}
        and not state.get("awaiting_resolution"),
    )

with tempfile.TemporaryDirectory(prefix="review-cleanup-terminate.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    runtime.cleanup_terminate_once = True
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-cleanup-terminate", context=context),
        scratch,
    )
    lane = run.execution.lanes[0]
    decision = controller.complete_round(
        run,
        lane,
        run.rounds[lane.axis],
        ReviewResult("anthropic-holistic", "approve"),
    )
    check(
        "orphan termination remains a bounded cleanup wait before approval",
        decision.action == "approved"
        and controller.read()["status"] == "approved"
        and len(runtime.cleanups) == 2,
    )

with tempfile.TemporaryDirectory(prefix="review-gate-budget.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    product = base / "product"
    product.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = controller.begin(
        dispatch_operation_id="review-budget",
        request=request_for("review-budget", context=context),
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=product,
        prompt_pointer="prompts/review.md",
        callback_root="callbacks/review-budget",
    )
    lane = run.execution.lanes[0]
    waiting = controller.complete_round(
        run,
        lane,
        run.rounds["anthropic-holistic"],
        ReviewResult(
            "anthropic-holistic",
            "changes-requested",
            (
                ReviewFinding(
                    "F-budget-1",
                    "anthropic-holistic",
                    "important",
                    "first defect",
                    "first failure",
                ),
            ),
        ),
    )
    first = controller.continue_after_resolution(
        run,
        lane,
        context=replace(context, head_sha="1" * 40),
        resolution=resolution_evidence(
            "review-budget", "anthropic-holistic", context.head_sha,
            "1" * 40, "F-budget-1",
        ),
        review_identity_sha256=resolution_transport_identity(controller),
        verification_prompt_pointer="prompts/verify.md",
        callback_pointer="callbacks/review-budget/anthropic-holistic/.review-callback.json",
    )
    run = controller.rehydrate()
    first = replace(
        first,
        lane=run.execution.lanes[0],
        round=run.rounds["anthropic-holistic"],
    )
    second_waiting = controller.complete_round(
        run,
        first.lane,
        first.round,
        ReviewResult(
            "anthropic-holistic",
            "changes-requested",
            (
                ReviewFinding(
                    "F-budget-2",
                    "anthropic-holistic",
                    "important",
                    "residual defect",
                    "verification still fails",
                ),
            ),
            verification_iteration=1,
        ),
    )
    exhausted = controller.continue_after_resolution(
        run,
        first.lane,
        context=replace(context, head_sha="2" * 40),
        resolution=resolution_evidence(
            "review-budget", "anthropic-holistic", "1" * 40,
            "2" * 40, "F-budget-2",
        ),
        review_identity_sha256=resolution_transport_identity(controller),
        verification_prompt_pointer="prompts/verify-2.md",
        callback_pointer="callbacks/review-budget/anthropic-holistic/.review-callback.json",
    )
    check(
        "verification exhaustion is durable attention instead of ValueError",
        second_waiting.action == "awaiting-resolution"
        and exhausted.action == "attention-required"
        and controller.read()["status"] == "attention-required"
        and store.read("owner-1", first.lane.operation_id).state
        == "attention-required",
    )
    active_context = run.execution.request.context
    new_context = replace(
        active_context, manifest="packets/review/expanded.json"
    )
    boundary = ReviewScopeBoundary(
        "context",
        review_context_sha256(active_context),
        review_context_sha256(new_context),
        "approved context packet changed",
    )
    boundary_authorization = {
        "schema_version": 1,
        "operation_id": "review-budget",
        "kind": boundary.kind,
        "previous_context_sha256": boundary.previous_context_sha256,
        "next_context_sha256": boundary.next_context_sha256,
        "reason": boundary.reason,
        "authorization_provenance": "coordinator-approved",
        "verification_operation_id": "verification-test",
        "verification_receipt_sha256": "a" * 64,
        "status": "authorized",
    }
    boundary_authorization_path = (
        controller.root / "fresh-boundary-authorization.json"
    )
    boundary_authorization_path.write_text(
        json.dumps(boundary_authorization, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    controller.authorize_fresh_boundary(
        run,
        boundary=boundary,
        authorization_pointer=boundary_authorization_path.name,
        authorization_sha256=hashlib.sha256(
            boundary_authorization_path.read_bytes()
        ).hexdigest(),
    )
    stale_packet = product / ".task-review.json"
    stale_resolution = product / ".task-review-resolution.json"
    stale_packet.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": "review-budget",
                "reviewed_head_sha": active_context.head_sha,
                "material_finding_ids": ["F-budget-2"],
                "findings": [{"finding_id": "F-budget-2"}],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    stale_resolution.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": "review-budget",
                "reviewed_head_sha": active_context.head_sha,
                "resolved_head_sha": "2" * 40,
                "resolutions": [
                    {
                        "finding_id": "F-budget-2",
                        "disposition": "applied",
                        "rationale": "Prior-boundary resolution.",
                        "follow_up": "",
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    fresh = controller.restart_for_boundary(
        run,
        boundary=boundary,
        context=new_context,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=product,
        prompt_pointer="prompts/compact.md",
        callback_root="callbacks/review-budget-fresh",
        max_verify_iterations=0,
    )
    check(
        "fresh boundary invalidates the previous executor review transport",
        not stale_packet.exists() and not stale_resolution.exists(),
    )
    repeated = controller.restart_for_boundary(
        fresh,
        boundary=boundary,
        context=new_context,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=product,
        prompt_pointer="prompts/compact.md",
        callback_root="callbacks/review-budget-fresh",
        max_verify_iterations=0,
    )
    check(
        "one explicit scope/context boundary permits one fresh compact run",
        fresh is not None
        and len(runtime.started) == 2
        and repeated is not None
        and fresh.execution.request.policy.max_verify_iterations == 0
        and fresh.execution.lanes[0].max_verify_iterations == 0
        and controller.read()["status"] == "reviewing",
    )
    assert fresh is not None
    fresh_lane = fresh.execution.lanes[0]
    fresh_result = ReviewResult(
        "anthropic-holistic",
        "changes-requested",
        (
            ReviewFinding(
                "F-fresh-1",
                "anthropic-holistic",
                "important",
                "fresh context defect",
                "fresh review found a product gap",
            ),
        ),
    )
    fresh_waiting = controller.complete_round(
        fresh,
        fresh_lane,
        fresh.rounds["anthropic-holistic"],
        fresh_result,
    )
    fresh_round = fresh.rounds["anthropic-holistic"]
    fresh_callback = review_round_envelope(fresh_round, fresh_result)
    fresh_boundary = controller.read()["awaiting_resolution"]["anthropic-holistic"]
    check(
        "fresh resolution boundary binds operation callback findings and HEAD",
        fresh_boundary["review_operation_id"]
        == fresh.execution.request.policy.operation_id
        and fresh_boundary["round_operation_id"]
        == fresh_round.operation_id
        and fresh_boundary["round_run_id"] == fresh_round.run_id
        and fresh_boundary["callback_id"] == fresh_callback.callback_id
        and fresh_boundary["callback_sha256"]
        == fresh_callback.payload_sha256
        and fresh_boundary["material_finding_ids"] == ["F-fresh-1"]
        and fresh_boundary["reviewed_head_sha"] == new_context.head_sha,
    )
    fresh_exhausted = controller.continue_after_resolution(
        fresh,
        fresh_lane,
        context=replace(new_context, head_sha="3" * 40),
        resolution=resolution_evidence(
            "review-budget",
            "anthropic-holistic",
            new_context.head_sha,
            "3" * 40,
            "F-fresh-1",
        ),
        review_identity_sha256=resolution_transport_identity(controller),
        verification_prompt_pointer="prompts/fresh-verify.md",
        callback_pointer="callbacks/review-budget-fresh/anthropic-holistic/.review-callback.json",
    )
    check(
        "fresh review resolutions remain bound to the dispatch identity",
        fresh_waiting.action == "awaiting-resolution"
        and fresh_exhausted.action == "attention-required",
    )

with tempfile.TemporaryDirectory(prefix="review-gate-deep.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-deep-gate", depth="deep", context=context),
        scratch,
    )
    first_axis = run.execution.lanes[0]
    first = controller.complete_round(
        run,
        first_axis,
        run.rounds[first_axis.axis],
        ReviewResult(first_axis.axis, "approve"),
    )
    second_axis = run.execution.lanes[1]
    second = controller.complete_round(
        run,
        second_axis,
        run.rounds[second_axis.axis],
        ReviewResult(second_axis.axis, "approve"),
    )
    check(
        "deep gate preserves independent lanes and approves only after both axes",
        len(runtime.started) == 2
        and len({lane.lane_id for lane in run.execution.lanes}) == 2
        and first.action == "awaiting-axes"
        and second.action == "approved",
    )

with tempfile.TemporaryDirectory(prefix="review-gate-deep-attention.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-deep-attention", depth="deep", context=context),
        scratch,
    )
    spec_lane, standards_lane = run.execution.lanes
    blocked = controller.complete_round(
        run,
        spec_lane,
        run.rounds[spec_lane.axis],
        ReviewResult(spec_lane.axis, "blocked"),
    )
    later = controller.complete_round(
        run,
        standards_lane,
        run.rounds[standards_lane.axis],
        ReviewResult(standards_lane.axis, "approve"),
    )
    state = controller.read()
    check(
        "later deep-axis approval cannot mask durable blocked attention",
        blocked.action == "attention-required"
        and later.action == "attention-required"
        and state["status"] == "attention-required"
        and state["evidence"] == {},
    )

with tempfile.TemporaryDirectory(prefix="review-gate-defer-blocked.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-defer-blocked", depth="deep", context=context),
        scratch,
    )
    lane = run.execution.lanes[0]
    decision = controller.defer_round_for_resolution(
        run,
        lane,
        run.rounds[lane.axis],
        ReviewResult(lane.axis, "blocked"),
    )
    state = controller.read()
    check(
        "deep defer keeps blocked verdict as durable attention",
        decision.action == "attention-required"
        and state["status"] == "attention-required"
        and not state.get("awaiting_resolution"),
    )

with tempfile.TemporaryDirectory(prefix="review-gate-skip.") as raw:
    gate = Path(raw)
    ReviewGateController.skip(
        gate,
        dispatch_operation_id="dispatch-skip",
        owner_id="owner-1",
        preset=disabled,
        context=context,
        product_root=ROOT,
    )
    skipped = authorize_task_finalization(
        gate,
        dispatch_operation_id="dispatch-skip",
        expected_head_sha=context.head_sha,
        expected_profile=context.verification_profile,
        expected_profile_sha256=context.verification_profile_sha256,
    )
    check(
        "explicit no-review is the only evidence-free finalization route",
        skipped.skipped and not skipped.approved,
    )

with tempfile.TemporaryDirectory(prefix="review-gate-deep-resolution.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")
    runtime = FakeRuntime(store)
    controller = ReviewGateController(base / "gate", runtime, store)
    run = begin(
        controller,
        request_for("review-deep-resolution", depth="deep", context=context),
        scratch,
    )
    spec_lane, standards_lane = run.execution.lanes
    controller.defer_round_for_resolution(
        run,
        spec_lane,
        run.rounds[spec_lane.axis],
        ReviewResult(spec_lane.axis, "approve"),
    )
    controller.defer_round_for_resolution(
        run,
        standards_lane,
        run.rounds[standards_lane.axis],
        ReviewResult(
            standards_lane.axis,
            "changes-requested",
            (
                ReviewFinding(
                    "F-deep-resolution",
                    standards_lane.axis,
                    "important",
                    "shared HEAD needs a fix",
                    "the correctness axis found a regression",
                ),
            ),
        ),
    )
    qualified_deep_finding = (
        f"{standards_lane.axis}:F-deep-resolution"
    )
    deep_boundary = controller.read()["awaiting_resolution"][
        standards_lane.axis
    ]
    persisted_deep_result = json.loads(
        (controller.root / deep_boundary["pointer"]).read_text(encoding="utf-8")
    )
    persisted_envelope = review_round_envelope(
        run.rounds[standards_lane.axis],
        _result_from_payload(persisted_deep_result),
    )
    persisted_child = controller.round_store.read(
        run.rounds[standards_lane.axis].owner_id,
        run.rounds[standards_lane.axis].operation_id,
    )
    check(
        "deep barrier preserves callback bytes and publishes a qualified resolution identity",
        persisted_deep_result["findings"][0]["finding_id"]
        == "F-deep-resolution"
        and deep_boundary["material_finding_ids"]
        == [qualified_deep_finding]
        and persisted_child.accepted_callback_sha256
        == persisted_envelope.payload_sha256,
    )
    resolved = replace(context, head_sha="9" * 40)
    for lane in run.execution.lanes:
        controller.continue_after_resolution(
            run,
            lane,
            context=resolved,
            resolution=resolution_evidence(
                "review-deep-resolution",
                lane.axis,
                context.head_sha,
                resolved.head_sha,
                *(
                    (qualified_deep_finding,)
                    if lane.axis == standards_lane.axis
                    else ()
                ),
            ),
            review_identity_sha256=resolution_transport_identity(controller),
            verification_prompt_pointer=f"prompts/{lane.axis}.md",
            callback_pointer=(
                f"callbacks/deep/{lane.axis}/.review-callback.json"
            ),
        )
    check(
        "deep material resolution keeps both independent parents for the new HEAD",
        len(runtime.started) == 2
        and len(runtime.continued) == 2
        and not runtime.exits
        and controller.read()["status"] == "verifying"
        and {
            row["axis"]: row["verification_iteration"]
            for row in controller.read()["lanes"]
        }
        == {
            "anthropic-holistic": 1,
            "openai-holistic": 1,
        },
    )

with tempfile.TemporaryDirectory(prefix="task-review-runner.") as raw:
    base = Path(raw)
    vault = base / "coordinator-vault"
    product = base / "generic-product-worktree"
    (vault / "wiki/plans").mkdir(parents=True)
    (vault / "skills/review").mkdir(parents=True)
    (vault / "scripts").mkdir()
    (vault / "config").mkdir()
    (vault / ".vault-meta/harness").mkdir(parents=True)
    plan = vault / "wiki/plans/approved.md"
    plan.write_text("# Approved task\n\nImplement the bounded change.\n", encoding="utf-8")
    (vault / "skills/review/SKILL.md").write_text(
        "# Review\n\nInspect the exact ContextPacket and product HEAD.\n",
        encoding="utf-8",
    )
    (vault / "config/model-routing.toml").write_bytes(
        (ROOT / "config/model-routing.toml").read_bytes()
    )
    (vault / "config/verification-profiles.toml").write_bytes(
        (ROOT / "config/verification-profiles.toml").read_bytes()
    )
    product.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "review@example.invalid"],
        cwd=product,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Review Gate Test"],
        cwd=product,
        check=True,
    )
    (product / "product.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.py"], cwd=product, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    )
    task_id = str(uuid.uuid4())
    profile_sha = load_profiles(
        vault / "config/verification-profiles.toml"
    )["scoped"].sha256
    meta = {
        "version": 3,
        "project_id": str(uuid.uuid4()),
        "task_id": task_id,
        "task_name": "automatic task review",
        "executor_runtime": "codex",
        "origin_session": "session-1",
        "task_surface": "22222222-2222-4222-8222-222222222222",
        "worktree": str(product),
        "vault_root": str(vault),
        "plan_file": str(plan),
        "approved_plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "interaction_policy": "unattended",
        "pipeline_policy": {
            "name": "lifecycle/default",
            "definition_sha256": compiled_builtin(
                "lifecycle/default"
            ).definition_sha256,
            "completion_policy": "attention",
            "total_pass_limit": 2,
        },
        "routing": {
            "session": {
                "runtime": "codex",
                "model": "gpt-5.6-sol",
                "effort": "high",
                "source": "test-session",
            }
        },
        "review_policy": {
            "mode": "simple",
            "cross_model": False,
            "runtime": "",
            "model": "",
            "effort": "",
            "max_verify_iterations": 1,
            "verification_profile": "scoped",
            "verification_profile_sha256": profile_sha,
            "auto_resolve_severities": ["warning", "nit"],
            "escalate_severities": ["blocking"],
        },
        "reap_policy": {
            "mode": "final",
            "auto_file": True,
            "allowed_types": ["session"],
            "title": "automatic task review",
        },
        "surface_policy": {"auto_close": True, "placement": "split"},
        "forbidden_actions": [
            "push",
            "deploy",
            "publish",
            "delete-worktree",
            "delete-branch",
            "expand-scope",
        ],
    }
    (product / ".task-meta.json").write_text(
        json.dumps(meta, sort_keys=True) + "\n", encoding="utf-8"
    )
    module_spec = importlib.util.spec_from_file_location(
        "task_review_runner_module", ROOT / "scripts/task-review-runner.py"
    )
    check(
        "task review runner is a public code-owned lifecycle facade",
        module_spec is not None and module_spec.loader is not None,
    )
    runner_source = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "scripts/task-review-runner.py",
            "scripts/task_review_flow.py",
            "scripts/task_review_resolution_flow.py",
        )
    )
    check(
        "deep resolution stops after the first exhausted lane",
        re.search(
            r'if decision\.action == "attention-required":\n\s+break',
            runner_source,
        )
        is not None,
    )
    task_review_runner = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(task_review_runner)
    check(
        "quiescent stale-HEAD resolution permits a fresh replacement boundary",
        task_review_runner.stale_resolution_boundary(
            "awaiting-resolution", "a" * 40, "b" * 40, True
        ),
    )
    check(
        "same-HEAD or live resolution cannot be silently superseded",
        not task_review_runner.stale_resolution_boundary(
            "awaiting-resolution", "a" * 40, "a" * 40, True
        )
        and not task_review_runner.stale_resolution_boundary(
            "awaiting-resolution", "a" * 40, "b" * 40, False
        ),
    )
    task_store = OperationStore(vault / ".vault-meta/harness")
    task_dispatch_spec = OperationSpec(
        operation_id=task_id,
        idempotency_key="task-review-dispatch",
        kind="dispatch",
        owner_id=task_id,
        route=RuntimeRoute(
            "codex",
            "gpt-5.6-sol",
            "high",
            "executor",
            "f" * 64,
        ),
        context_manifest="wiki/plans/approved.md",
        verification_profile="scoped",
        contract_sha256=meta["pipeline_policy"]["definition_sha256"],
    )
    task_store.create(
        task_dispatch_spec,
        lane_id="task-review-dispatch-lane",
        run_id="task-review-dispatch-run",
    )
    for dispatch_state in (
        "preflight",
        "starting",
        "running",
        "awaiting-callback",
    ):
        task_store.transition(task_id, task_id, dispatch_state)
    task_runtime = EffectRecordingRuntime(task_store)
    started = task_review_runner.run_task_review(
        product, runtime_manager=task_runtime
    )
    gate_root = (
        vault
        / ".vault-meta/harness/review-data"
        / task_id
        / task_id
    )
    started_state = json.loads(
        (gate_root / "review-gate.json").read_text(encoding="utf-8")
    )
    packet_manifest = (
        Path(str(started["context_manifest"]))
    )
    check(
        "v3 task start separates coordinator vault, product, and scratch ContextPacket",
        started["status"] == "reviewing"
        and started["worktree"] == str(product.resolve())
        and started["vault_root"] == str(vault.resolve())
        and len(task_runtime.started) == 1
        and task_runtime.started[0].product_root == product.resolve()
        and task_runtime.started[0].cwd != product.resolve()
        and packet_manifest.is_file()
        and product.resolve() not in packet_manifest.parents
        and started_state["dispatch_operation_id"] == task_id,
    )
    initial_lane = started["lanes"][0]
    simple_axis = str(initial_lane["axis"])
    callback_path = Path(initial_lane["callback_path"])
    submit = shlex.join(
        (
            str(Path(sys.executable).resolve()),
            str(vault.resolve() / "scripts/harness/review_submit.py"),
            "--worktree",
            str(product.resolve()),
            "--state-dir",
            str(callback_path.parent),
            "--input-file",
            str(callback_path.with_name(".review-input.json")),
        )
    )
    initial_request = task_runtime.started[0]
    prompt_text = (
        initial_request.cwd / initial_request.prompt_pointer
    ).read_text(encoding="utf-8")
    claude_command = ClaudeDriver(Path("/usr/bin/claude")).command(
        RuntimeRoute(
            "claude",
            "fable",
            "high",
            "reviewer-callback",
            "f" * 64,
        ),
        callback_pointer=callback_path,
        product_root=product.resolve(),
        session_root=initial_request.cwd,
    )
    claude_sandbox = json.loads(
        claude_command[claude_command.index("--settings") + 1]
    )
    check(
        "review prompt submit command runs inside the native Claude sandbox",
        f"`{submit}`" in prompt_text
        and "Bash" in claude_command
        and not any(item.startswith("Bash(") for item in claude_command)
        and claude_sandbox["sandbox"]["enabled"] is True
        and claude_sandbox["sandbox"]["failIfUnavailable"] is True
        and claude_sandbox["sandbox"]["allowUnsandboxedCommands"] is False
        and str(product.resolve())
        in claude_sandbox["sandbox"]["filesystem"]["denyWrite"]
        and "Read, Glob, and Grep with absolute paths" in prompt_text
        and "review-inspect.py" in prompt_text
        and "Do not run cd or copy packet files" in prompt_text,
    )
    round_ = task_review_runner.load_active_round(
        gate_root,
        task_store,
        task_runtime,
        axis=simple_axis,
    )
    callback = review_round_envelope(
        round_.round,
        ReviewResult(
            simple_axis,
            "changes-requested",
            (
                ReviewFinding(
                    "F-task-1",
                    simple_axis,
                    "important",
                    "fix the product value",
                    "VALUE still equals one",
                    file="product.py",
                ),
            ),
        ),
    )
    callback_path.write_text(
        json.dumps(to_dict(callback), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    waiting = task_review_runner.run_task_review(
        product, runtime_manager=task_runtime
    )
    check(
        "drive consumes the exact callback and persists material findings",
        waiting["status"] == "awaiting-resolution"
        and len(task_runtime.continued) == 0
        and json.loads(
            (gate_root / "review-gate.json").read_text(encoding="utf-8")
        )["status"]
        == "awaiting-resolution",
    )
    review_events = [
        json.loads(line)
        for line in (
            vault / ".vault-meta/pipeline-events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("op", "").startswith("review-")
    ]
    check(
        "production review emits content-free start callback and completion telemetry",
        [event["op"] for event in review_events]
        == ["review-round-start", "review-callback", "review-round-complete"]
        and review_events[1]["counts"]["accepted_callbacks"] == 1
        and review_events[2]["counts"]["important_findings"] == 1
        and review_events[2]["counts"]["critical_findings"] == 0
        and review_events[2]["counts"]["minor_findings"] == 0
        and review_events[2]["identifiers"]
        == {
            "axis": simple_axis,
            "reviewer_runtime": "codex",
            "terminal_status": "changes-requested",
        }
        and "F-task-1" not in json.dumps(review_events, sort_keys=True),
    )
    reviewed_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (product / "product.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.py"], cwd=product, check=True)
    subprocess.run(
        ["git", "commit", "-m", "resolve review"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    )
    resolved_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (product / ".task-review-resolution.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": task_id,
                "review_identity_sha256": resolution_transport_identity(
                    ReviewGateController(gate_root, task_runtime, task_store)
                ),
                "reviewed_head_sha": reviewed_head,
                "resolved_head_sha": resolved_head,
                "resolutions": [
                    {
                        "finding_id": "F-task-1",
                        "disposition": "applied",
                        "rationale": (
                            "The corrected product value and its commit are "
                            "present on the resolved HEAD."
                        ),
                        "follow_up": "",
                    }
                ],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    verifying = task_review_runner.run_task_review(
        product, runtime_manager=task_runtime
    )
    verification_manifest = json.loads(
        Path(str(verifying["context_manifest"])).read_text(encoding="utf-8")
    )
    verification_inputs = {
        item["name"] for item in verification_manifest["inputs"]
    }
    check(
        "restart-safe verify rehydrates and continues the same parent session",
        verifying["status"] == "verifying"
        and len(task_runtime.started) == 1
        and len(task_runtime.continued) == 1
        and task_runtime.continued[0][1] == initial_lane["operation_id"]
        and json.loads(
            (gate_root / "review-gate.json").read_text(encoding="utf-8")
        )["context"]["head_sha"]
        == subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=product,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
    )
    check(
        "same-session verification receives previous findings and bounded fix delta",
        {
            "resolution-evidence.json",
            "fix-delta.manifest.json",
            "fix-delta.part-001.patch",
        }
        <= verification_inputs,
    )

    # Recovery must consume coordinator-owned verification evidence and must
    # preserve the exact reviewer-seen ruling across a verification repair.
    finalizing = task_review_runner.load_active_round(
        gate_root,
        task_store,
        task_runtime,
        axis=simple_axis,
    )
    approved_result = ReviewResult(
        simple_axis,
        "approve",
        (),
        verification_iteration=1,
    )
    approved_envelope = review_round_envelope(
        finalizing.round,
        approved_result,
    )
    callback_path.write_text(
        json.dumps(to_dict(approved_envelope), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    task_runtime.accept_callback(approved_envelope)
    interrupted_child = task_store.read(
        task_id,
        finalizing.round.operation_id,
    )
    (product / "verification-repair.txt").write_text(
        "bounded verification repair\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "verification-repair.txt"],
        cwd=product,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "repair verification"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    )
    repaired_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    resolution_path = product / ".task-review-resolution.json"
    exact_resolution = json.loads(
        resolution_path.read_text(encoding="utf-8")
    )
    exact_resolution["resolved_head_sha"] = repaired_head
    exact_resolution_bytes = (
        json.dumps(exact_resolution, sort_keys=True) + "\n"
    ).encode()
    resolution_path.write_bytes(exact_resolution_bytes)
    (product / ".task-summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "type": "session",
                "title": "automatic task review",
                "session": "session-1",
                "body": "Preserve exact accepted review evidence during recovery.",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    verification_input_sha = hashlib.sha256(
        b"failed verification input"
    ).hexdigest()
    (
        verification_spec,
        verification_lane_id,
        verification_run_id,
    ) = _pipeline_verify_identity(
        task_dispatch_spec,
        definition_sha256=meta["pipeline_policy"]["definition_sha256"],
        input_sha256=verification_input_sha,
        profile="scoped",
    )
    verification_operation_id = verification_spec.operation_id
    verification_effect_id = (
        f"pipeline-verify-{verification_input_sha[:32]}"
    )
    task_store.create(
        verification_spec,
        lane_id=verification_lane_id,
        run_id=verification_run_id,
    )
    for verification_state in (
        "preflight",
        "starting",
        "running",
        "verifying",
    ):
        task_store.transition(
            task_id,
            verification_operation_id,
            verification_state,
        )
    task_store.transition(
        task_id,
        verification_operation_id,
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )
    verification_record = task_store.read(
        task_id, verification_operation_id
    )
    task_store.save(
        replace(
            verification_record,
            effect_id=verification_effect_id,
            effect_outcome=EffectOutcome.SUCCEEDED,
            revision=verification_record.revision + 1,
        ),
        expected_revision=verification_record.revision,
    )
    owner_runtime = (
        vault
        / ".vault-meta/harness/owners"
        / task_id
        / "runtime"
        / task_id
    )
    verification_root = (
        owner_runtime
        / "pipeline-verification"
        / verification_operation_id
    )
    evidence_path = verification_root / "evidence/scoped-1.log"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text("deterministic failed verification\n", encoding="utf-8")
    verification_receipt = {
        "schema_version": 1,
        "operation_id": verification_operation_id,
        "parent_operation_id": task_id,
        "lane_id": verification_lane_id,
        "run_id": verification_run_id,
        "definition_sha256": meta["pipeline_policy"]["definition_sha256"],
        "step_id": "verify",
        "head_sha": resolved_head,
        "input_sha256": verification_input_sha,
        "profile": "scoped",
        "profile_sha256": profile_sha,
        "effect_id": verification_effect_id,
        "status": "failed",
        "evidence": [
            {
                "command_id": "scoped-1",
                "cwd": ".",
                "exit_code": 2,
                "started_at": "1.0",
                "finished_at": "2.0",
                "head_sha": resolved_head,
                "profile": "scoped",
                "profile_sha256": profile_sha,
                "output_pointer": (
                    evidence_path.relative_to(owner_runtime).as_posix()
                ),
            }
        ],
    }
    receipt_path = verification_root / "receipt.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(verification_receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (owner_runtime / "pipeline-step-verify.json").write_text(
        json.dumps(verification_receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    packet = {
        "schema_version": 1,
        "operation_id": task_id,
        "verification_operation_id": verification_operation_id,
        "verification_lane_id": verification_lane_id,
        "verification_run_id": verification_run_id,
        "definition_sha256": meta["pipeline_policy"]["definition_sha256"],
        "step_id": "verify",
        "head_sha": resolved_head,
        "status": "attention-required",
        "reason": "verification-failed",
        "safe_boundary": "tdd-slices-complete",
        "allowed_responses": ["fix-and-resubmit", "escalate"],
        "response_pointer": ".task-verification-response.json",
        "receipt_pointer": str(receipt_path.resolve()),
        "evidence": [
            {
                "command_id": "scoped-1",
                "exit_code": 2,
                "output_pointer": str(evidence_path.resolve()),
            }
        ],
    }

    def write_resubmit(
        packet_value: dict[str, object],
    ) -> dict[str, object]:
        (product / ".task-verification.json").write_text(
            json.dumps(packet_value, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        packet_bytes = json.dumps(
            packet_value, sort_keys=True, separators=(",", ":")
        ).encode()
        response_value = {
            "schema_version": 1,
            "operation_id": task_id,
            "verification_operation_id": packet_value[
                "verification_operation_id"
            ],
            "failed_head_sha": resolved_head,
            "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
            "response": "fix-and-resubmit",
            "resubmitted_head_sha": repaired_head,
        }
        (product / ".task-verification-response.json").write_text(
            json.dumps(response_value, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return response_value

    recover_finalizing = getattr(
        task_review_runner,
        "recover_finalizing_review",
        None,
    )
    response_receipt_path = verification_root / "response-receipt.json"
    if recover_finalizing is None:
        regression_failures.extend(
            (
                "HOL-R01 durable verification receipt binding",
                "HOL-R02 exact reviewer-seen finding ruling",
                "HOL-R03 interruption-safe finalizing replay",
            )
        )
    else:
        forged_packet = {
            **packet,
            "verification_operation_id": "forged-verification-child",
            "verification_lane_id": "forged-verification-lane",
            "verification_run_id": "forged-verification-run",
            "receipt_pointer": str(
                product / "missing-forged-verification-receipt.json"
            ),
        }
        write_resubmit(forged_packet)
        try:
            recover_finalizing(product, runtime_manager=task_runtime)
        except task_review_runner.TaskReviewError:
            forged_rejected = True
        else:
            forged_rejected = False
        if (
            not forged_rejected
            or response_receipt_path.exists()
            or task_store.read(
                task_id, finalizing.round.operation_id
            ).state
            != "finalizing"
        ):
            regression_failures.append(
                "HOL-R01 durable verification receipt binding"
            )

        write_resubmit(packet)
        drifted_resolution = json.loads(
            exact_resolution_bytes.decode()
        )
        drifted_resolution["resolutions"][0].update(
            {
                "disposition": "rejected",
                "rationale": "executor-rewritten rationale after review",
                "follow_up": "",
            }
        )
        resolution_path.write_text(
            json.dumps(drifted_resolution, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            recover_finalizing(product, runtime_manager=task_runtime)
        except task_review_runner.TaskReviewError:
            ruling_rejected = True
        else:
            ruling_rejected = False
        if not ruling_rejected:
            regression_failures.append(
                "HOL-R02 exact reviewer-seen finding ruling"
            )
        resolution_path.write_bytes(exact_resolution_bytes)

        parent_record = task_store.read(task_id, task_id)
        if parent_record.state != "attention-required":
            task_store.transition(
                task_id,
                task_id,
                "attention-required",
                reason=AttentionReason.ATTENTION_REQUIRED,
            )
        session_path = (
            task_store.root
            / "owners"
            / task_id
            / "runtime"
            / task_id
            / "session.json"
        )
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation_id": task_id,
                    "run_id": task_store.read(task_id, task_id).run_id,
                    "cwd": str(product),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        cli_output = StringIO()
        if "review_runtime_manager" in inspect.signature(
            harness_cli.main
        ).parameters:
            with redirect_stdout(cli_output):
                cli_resume_rc = harness_cli.main(
                    [
                        "--store",
                        str(task_store.root),
                        "--owner",
                        task_id,
                        "--json",
                        "resume",
                        task_id,
                    ],
                    process_adapter=object(),
                    cmux_adapter=object(),
                    review_runtime_manager=task_runtime,
                )
        else:
            cli_resume_rc = 2
            try:
                recover_finalizing(
                    product, runtime_manager=task_runtime
                )
            except task_review_runner.TaskReviewError:
                pass
        staged_state = json.loads(
            (gate_root / "review-gate.json").read_text(encoding="utf-8")
        )
        staged = {
            "status": (
                "verifying"
                if staged_state.get("status")
                == "recovery-verification-required"
                else "error"
            )
        }
        if not (
            cli_resume_rc == 0
            and task_store.read(task_id, task_id).state
            == "awaiting-callback"
        ):
            regression_failures.append(
                "HOL-003 dispatch resume accepts staged recovery"
            )
        accepted_response = (
            json.loads(response_receipt_path.read_text(encoding="utf-8"))
            if response_receipt_path.is_file()
            else {}
        )
        if not (
            staged.get("status") == "verifying"
            and staged_state["context"]["head_sha"] == resolved_head
            and staged_state.get("status")
            == "recovery-verification-required"
            and staged_state.get("final_results") == {}
            and accepted_response.get("verification_operation_id")
            == verification_operation_id
            and accepted_response.get("resubmitted_head_sha")
            == repaired_head
            and task_store.read(
                task_id, finalizing.round.operation_id
            ).state
            == "complete"
        ):
            regression_failures.append(
                "HOL-001 repaired HEAD cannot inherit prior approval"
            )

        unauthorized_fresh_rejected = False
        if staged.get("status") == "verifying":
            dispatch_record = task_store.read(task_id, task_id)
            if dispatch_record.state != "attention-required":
                task_store.transition(
                    task_id,
                    task_id,
                    "attention-required",
                    reason=AttentionReason.ATTENTION_REQUIRED,
                )
            for terminal_record in task_store.list(task_id):
                if (
                    terminal_record.state in TERMINAL
                    and terminal_record.resources != OwnedResources()
                ):
                    task_store.save(
                        replace(
                            terminal_record,
                            resources=OwnedResources(),
                            revision=terminal_record.revision + 1,
                        ),
                        expected_revision=terminal_record.revision,
                    )
            try:
                task_review_runner.restart_task_review_for_boundary(
                    product,
                    kind="scope",
                    reason="caller-manufactured scope expansion",
                    runtime_manager=task_runtime,
                )
            except task_review_runner.TaskReviewError:
                unauthorized_fresh_rejected = True
        if not unauthorized_fresh_rejected:
            regression_failures.append(
                "HOL-002 coordinator-owned fresh-boundary authorization"
            )

        successful_verification_operation_id = ""
        if staged.get("status") == "verifying":
            successful_input_sha = hashlib.sha256(
                b"successful repaired-head verification"
            ).hexdigest()
            (
                successful_spec,
                successful_lane_id,
                successful_run_id,
            ) = _pipeline_verify_identity(
                task_dispatch_spec,
                definition_sha256=meta["pipeline_policy"][
                    "definition_sha256"
                ],
                input_sha256=successful_input_sha,
                profile="scoped",
            )
            successful_verification_operation_id = (
                successful_spec.operation_id
            )
            task_store.create(
                successful_spec,
                lane_id=successful_lane_id,
                run_id=successful_run_id,
            )
            for successful_state in (
                "preflight",
                "starting",
                "running",
                "verifying",
                "finalizing",
                "exiting",
                "complete",
            ):
                task_store.transition(
                    task_id,
                    successful_spec.operation_id,
                    successful_state,
                )
            successful_record = task_store.read(
                task_id, successful_spec.operation_id
            )
            successful_effect_id = (
                f"pipeline-verify-{successful_input_sha[:32]}"
            )
            task_store.save(
                replace(
                    successful_record,
                    effect_id=successful_effect_id,
                    effect_outcome=EffectOutcome.SUCCEEDED,
                    revision=successful_record.revision + 1,
                ),
                expected_revision=successful_record.revision,
            )
            successful_root = (
                owner_runtime
                / "pipeline-verification"
                / successful_spec.operation_id
            )
            successful_evidence = []
            for command_index in range(1, 4):
                successful_output = (
                    successful_root
                    / f"evidence/scoped-{command_index}.log"
                )
                successful_output.parent.mkdir(
                    parents=True, exist_ok=True
                )
                successful_output.write_text(
                    "deterministic successful verification\n",
                    encoding="utf-8",
                )
                successful_evidence.append(
                    {
                        "command_id": f"scoped-{command_index}",
                        "cwd": ".",
                        "exit_code": 0,
                        "started_at": f"{command_index}.0",
                        "finished_at": f"{command_index}.5",
                        "head_sha": repaired_head,
                        "profile": "scoped",
                        "profile_sha256": profile_sha,
                        "output_pointer": successful_output.relative_to(
                            owner_runtime
                        ).as_posix(),
                    }
                )
            successful_receipt = {
                "schema_version": 1,
                "operation_id": successful_spec.operation_id,
                "parent_operation_id": task_id,
                "lane_id": successful_lane_id,
                "run_id": successful_run_id,
                "definition_sha256": meta["pipeline_policy"][
                    "definition_sha256"
                ],
                "step_id": "verify",
                "head_sha": repaired_head,
                "input_sha256": successful_input_sha,
                "profile": "scoped",
                "profile_sha256": profile_sha,
                "effect_id": successful_effect_id,
                "status": "complete",
                "evidence": successful_evidence,
            }
            successful_receipt_path = successful_root / "receipt.json"
            successful_receipt_path.parent.mkdir(
                parents=True, exist_ok=True
            )
            successful_receipt_path.write_text(
                json.dumps(successful_receipt, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (owner_runtime / "pipeline-step-verify.json").write_text(
                json.dumps(successful_receipt, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            authorize = getattr(
                ReviewGateController, "authorize_fresh_boundary", None
            )
            if authorize is None:
                interrupted = False
                replayed = {"status": "error"}
                terminal_replay = {"status": "error"}
            else:
                original_authorize = authorize

                def interrupt_after_authorization(
                    controller: ReviewGateController,
                    *args: object,
                    **kwargs: object,
                ) -> object:
                    original_authorize(controller, *args, **kwargs)
                    raise OSError(
                        "simulated interruption after boundary authorization"
                    )

                ReviewGateController.authorize_fresh_boundary = (
                    interrupt_after_authorization
                )
                try:
                    try:
                        recover_finalizing(
                            product, runtime_manager=task_runtime
                        )
                    except task_review_runner.TaskReviewError:
                        interrupted = True
                    else:
                        interrupted = False
                finally:
                    ReviewGateController.authorize_fresh_boundary = (
                        original_authorize
                    )
                try:
                    replayed = recover_finalizing(
                        product, runtime_manager=task_runtime
                    )
                except task_review_runner.TaskReviewError as exc:
                    replayed = {"status": "error", "error": str(exc)}
                try:
                    terminal_replay = recover_finalizing(
                        product, runtime_manager=task_runtime
                    )
                except task_review_runner.TaskReviewError as exc:
                    terminal_replay = {
                        "status": "error",
                        "error": str(exc),
                    }
        else:
            interrupted = False
            replayed = {"status": "error"}
            terminal_replay = {"status": "error"}

        recovered_state = json.loads(
            (gate_root / "review-gate.json").read_text(encoding="utf-8")
        )
        authorization_pointer = recovered_state.get(
            "fresh_boundary_authorization", {}
        ).get("pointer", "")
        authorization_path = gate_root / authorization_pointer
        authorization = (
            json.loads(authorization_path.read_text(encoding="utf-8"))
            if authorization_path.is_file()
            else {}
        )
        if not (
            interrupted
            and replayed.get("status") == "reviewing"
            and terminal_replay.get("status") == "reviewing"
            and recovered_state.get("status") == "reviewing"
            and recovered_state["context"]["head_sha"] == repaired_head
            and recovered_state.get("final_results") == {}
            and authorization.get("operation_id") == task_id
            and authorization.get("kind") == "context"
            and authorization.get("authorization_provenance")
            == "pipeline-verification"
            and authorization.get("verification_operation_id")
            == successful_verification_operation_id
        ):
            regression_failures.append(
                "HOL-R03 interruption-safe finalizing replay"
            )

    custom_recovery_id = str(uuid.uuid4())
    custom_pipeline_raw = {
        "schema_version": 1,
        "spec_id": "review-recovery-extra-check",
        "version": "1.0.0",
        "intent": "engineering-change",
        "task_profile": "change",
        "baseline_pipeline": "engineering/change",
        "route_alias": "executor-default",
        "required_capabilities": ["route:resolved"],
        "input_schema": "approved-plan/v1",
        "output_schema": "reap-ready/v1",
        "steps": [
            {
                "step_id": "implement",
                "primitive_id": "model_step",
                "primitive_version": "1.0.0",
                "input_schema": "approved-plan/v1",
                "output_schema": "implementation-result/v1",
                "session_mode": "worktree",
                "semantic_skills": ["tdd"],
            },
            {
                "step_id": "verify",
                "primitive_id": "verify",
                "primitive_version": "1.0.0",
                "input_schema": "implementation-result/v1",
                "output_schema": "verified-result/v1",
                "session_mode": "verification",
                "semantic_skills": [],
            },
            {
                "step_id": "review",
                "primitive_id": "review",
                "primitive_version": "1.0.0",
                "input_schema": "verified-result/v1",
                "output_schema": "reap-ready/v1",
                "session_mode": "review",
                "semantic_skills": ["review"],
            },
        ],
        "transitions": [
            {
                "from_step": "implement",
                "outcome": "complete",
                "target": "verify",
                "max_traversals": 1,
            },
            {
                "from_step": "verify",
                "outcome": "complete",
                "target": "review",
                "max_traversals": 1,
            },
            {
                "from_step": "review",
                "outcome": "complete",
                "target": "terminal:completed",
                "max_traversals": 1,
            },
        ],
        "controls": [],
        "budget": {
            "attempt_limit": 2,
            "model_restart_limit": 1,
            "time_budget_seconds": 900,
            "token_limit": 50000,
        },
        "completion_policy": "attention",
        "requested_permissions": ["git-write", "product-worktree"],
        "requested_side_effects": ["git-write", "worktree"],
        "context_pointers": [
            {
                "pointer_id": "approved-plan",
                "content_sha256": "a" * 64,
                "byte_limit": 65536,
            }
        ],
        "verification_checks": ["diff-check"],
        "review_mode": "simple",
        "human_gates": ["initial-approval"],
        "terminal_outcomes": ["completed", "attention-required"],
    }
    custom_policy = CustomPipelinePolicy.default()
    custom_spec = parse_pipeline_spec(custom_pipeline_raw)
    custom_compiled = compile_custom_spec(
        custom_spec,
        builtin_registry(),
        policy=custom_policy,
        capabilities=("route:resolved",),
    )
    custom_card = render_custom_approval(
        custom_spec,
        custom_compiled,
        policy=custom_policy,
    )
    custom_approval = ExplicitPipelineApproval.for_card(
        definition_sha256=custom_compiled.definition_sha256,
        approval_card=custom_card,
        actor="user",
        decision="approve",
    )
    custom_frozen = freeze_custom_pipeline(
        custom_spec,
        custom_compiled,
        custom_approval,
        custom_card,
    )
    FrozenPipelineStore(
        task_store.root
        / "owners"
        / custom_recovery_id
        / "runtime"
    ).save(
        operation_id=custom_recovery_id,
        spec=custom_spec,
        frozen=custom_frozen,
        approval=custom_approval,
    )
    custom_dispatch_spec = OperationSpec(
        operation_id=custom_recovery_id,
        idempotency_key="custom-review-recovery",
        kind="dispatch",
        owner_id=custom_recovery_id,
        route=RuntimeRoute(
            "codex",
            "gpt-5.6-sol",
            "high",
            "executor",
            "f" * 64,
        ),
        context_manifest="wiki/plans/approved.md",
        verification_profile="scoped",
        contract_sha256=custom_compiled.definition_sha256,
    )
    task_store.create(
        custom_dispatch_spec,
        lane_id="custom-recovery-dispatch-lane",
        run_id="custom-recovery-dispatch-run",
    )
    custom_input_sha = hashlib.sha256(
        b"custom repaired-head verification"
    ).hexdigest()
    (
        custom_verification_spec,
        custom_verification_lane,
        custom_verification_run,
    ) = _pipeline_verify_identity(
        custom_dispatch_spec,
        definition_sha256=custom_compiled.definition_sha256,
        input_sha256=custom_input_sha,
        profile="scoped",
    )
    task_store.create(
        custom_verification_spec,
        lane_id=custom_verification_lane,
        run_id=custom_verification_run,
    )
    for custom_state in (
        "preflight",
        "starting",
        "running",
        "verifying",
        "finalizing",
        "exiting",
        "complete",
    ):
        task_store.transition(
            custom_recovery_id,
            custom_verification_spec.operation_id,
            custom_state,
        )
    custom_effect_id = f"pipeline-verify-{custom_input_sha[:32]}"
    custom_record = task_store.read(
        custom_recovery_id,
        custom_verification_spec.operation_id,
    )
    task_store.save(
        replace(
            custom_record,
            effect_id=custom_effect_id,
            effect_outcome=EffectOutcome.SUCCEEDED,
            revision=custom_record.revision + 1,
        ),
        expected_revision=custom_record.revision,
    )
    custom_owner_runtime = (
        task_store.root
        / "owners"
        / custom_recovery_id
        / "runtime"
        / custom_recovery_id
    )
    custom_verification_root = (
        custom_owner_runtime
        / "pipeline-verification"
        / custom_verification_spec.operation_id
    )
    custom_evidence = []
    custom_command_count = len(
        load_profiles(vault / "config/verification-profiles.toml")[
            "scoped"
        ].commands
    ) + 1
    for command_index in range(1, custom_command_count + 1):
        custom_output = (
            custom_verification_root
            / f"evidence/scoped-{command_index}.log"
        )
        custom_output.parent.mkdir(parents=True, exist_ok=True)
        custom_output.write_text(
            "custom pipeline verification passed\n",
            encoding="utf-8",
        )
        custom_evidence.append(
            {
                "command_id": f"scoped-{command_index}",
                "cwd": ".",
                "exit_code": 0,
                "started_at": f"{command_index}.0",
                "finished_at": f"{command_index}.5",
                "head_sha": repaired_head,
                "profile": "scoped",
                "profile_sha256": profile_sha,
                "output_pointer": custom_output.relative_to(
                    custom_owner_runtime
                ).as_posix(),
            }
        )
    custom_receipt = {
        "schema_version": 1,
        "operation_id": custom_verification_spec.operation_id,
        "parent_operation_id": custom_recovery_id,
        "lane_id": custom_verification_lane,
        "run_id": custom_verification_run,
        "definition_sha256": custom_compiled.definition_sha256,
        "step_id": "verify",
        "head_sha": repaired_head,
        "input_sha256": custom_input_sha,
        "profile": "scoped",
        "profile_sha256": profile_sha,
        "effect_id": custom_effect_id,
        "status": "complete",
        "evidence": custom_evidence,
    }
    custom_receipt_path = custom_verification_root / "receipt.json"
    custom_receipt_path.parent.mkdir(parents=True, exist_ok=True)
    custom_receipt_path.write_text(
        json.dumps(custom_receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (custom_owner_runtime / "pipeline-step-verify.json").write_text(
        json.dumps(custom_receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    custom_meta = {
        "pipeline_policy": {
            "name": "custom",
            "source": "custom",
            "baseline": "engineering/change",
            "definition_sha256": custom_compiled.definition_sha256,
        },
        "review_policy": {
            "verification_profile": "scoped",
            "verification_profile_sha256": profile_sha,
        },
    }
    try:
        custom_verified = (
            task_review_runner._durable_successful_verification(
                custom_meta,
                vault,
                task_store,
                custom_recovery_id,
                repaired_head,
            )
        )
    except task_review_runner.TaskReviewError:
        custom_verified = None
    if custom_verified != (
        custom_verification_spec.operation_id,
        hashlib.sha256(custom_receipt_path.read_bytes()).hexdigest(),
    ):
        regression_failures.append(
            "HOL-004 custom verification commands remain recoverable"
        )

    recovery_id = str(uuid.uuid4())
    recovery_meta = {**meta, "task_id": recovery_id}
    (product / ".task-meta.json").write_text(
        json.dumps(recovery_meta, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    dispatch_spec = OperationSpec(
        operation_id=recovery_id,
        idempotency_key="review-recovery-dispatch",
        kind="dispatch",
        owner_id=recovery_id,
        route=RuntimeRoute(
            "codex",
            "gpt-5.6-sol",
            "high",
            "executor",
            "f" * 64,
        ),
        context_manifest="wiki/plans/approved.md",
        verification_profile="scoped",
    )
    task_store.create(
        dispatch_spec,
        lane_id="review-recovery-lane",
        run_id="review-recovery-run",
    )
    for state in (
        "preflight",
        "starting",
        "running",
        "awaiting-callback",
    ):
        task_store.transition(recovery_id, recovery_id, state)
    task_store.transition(
        recovery_id,
        recovery_id,
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )

    class FailBeforeReviewLane(FakeRuntime):
        def __init__(self, store: OperationStore) -> None:
            super().__init__(store)
            self.fail_once = True

        def start(
            self, request: object, *, on_surface_opened=None
        ) -> FakeSessionResult:
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("simulated review drive interruption")
            return super().start(
                request, on_surface_opened=on_surface_opened
            )

    recovery_runtime = FailBeforeReviewLane(task_store)
    try:
        task_review_runner.run_task_review(
            product, runtime_manager=recovery_runtime
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("review drive interruption fixture did not fail")
    recovery_gate = ReviewGateController(
        vault
        / ".vault-meta/harness/review-data"
        / recovery_id
        / recovery_id,
        recovery_runtime,
        task_store,
    )
    recovery_gate.mark_pending_attention()
    still_paused = task_review_runner.run_task_review(
        product, runtime_manager=recovery_runtime
    )
    task_store.transition(
        recovery_id,
        recovery_id,
        "awaiting-callback",
    )
    recovered = task_review_runner.run_task_review(
        product, runtime_manager=recovery_runtime
    )
    check(
        "resumed dispatch restarts only an evidence-free pre-launch review gate",
        still_paused["status"] == "attention-required"
        and recovered["status"] == "reviewing"
        and len(recovered["lanes"]) == 1
        and recovery_gate.read()["status"] == "reviewing",
    )
    skip_id = str(uuid.uuid4())
    skip_meta = {
        **meta,
        "task_id": skip_id,
        "review_policy": {
            **meta["review_policy"],
            "mode": "skip",
            "max_verify_iterations": 0,
        },
    }
    (product / ".task-meta.json").write_text(
        json.dumps(skip_meta, sort_keys=True) + "\n", encoding="utf-8"
    )
    skipped_task = task_review_runner.run_task_review(
        product, runtime_manager=task_runtime
    )
    skip_state = json.loads(
        (
            vault
            / ".vault-meta/harness/review-data"
            / skip_id
            / skip_id
            / "review-gate.json"
        ).read_text(encoding="utf-8")
    )
    check(
        "production no-review is an explicit typed exact-HEAD skip",
        skipped_task["status"] == "skipped"
        and skip_state["status"] == "skipped"
        and skip_state["policy"]["enabled"] is False,
    )

with tempfile.TemporaryDirectory(prefix="current-review-runner.") as raw:
    base = Path(raw)
    product = base / "current-checkout"
    scratch = base / "external-scratch"
    (product / "wiki").mkdir(parents=True)
    (product / "skills/review").mkdir(parents=True)
    (product / "scripts/harness").mkdir(parents=True)
    (product / "config").mkdir()
    (product / "skills/review/SKILL.md").write_text(
        "# Review\n\nInspect the exact ContextPacket and product HEAD.\n",
        encoding="utf-8",
    )
    (product / "scripts/harness/review_submit.py").write_text(
        "# test fixture\n", encoding="utf-8"
    )
    (product / "config/model-routing.toml").write_bytes(
        (ROOT / "config/model-routing.toml").read_bytes()
    )
    (product / "config/verification-profiles.toml").write_bytes(
        (ROOT / "config/verification-profiles.toml").read_bytes()
    )
    (product / "AGENTS.md").write_text(
        "# Product instructions\n", encoding="utf-8"
    )
    review_plan = product / "wiki/review-plan.md"
    review_plan.write_text(
        """# Review plan

```json
{"schema_version":1,"desired_outcome":"Preserve the approved outcome through release review.","success_evidence":[{"evidence_id":"release-proof","observable":"Exact evidence is present in the review packet."}],"non_goals":["Changing product behavior during release review."]}
```
""",
        encoding="utf-8",
    )
    boundary_artifacts = {
        "design": product / "wiki/design.md",
        "capability-dispositions": product / "wiki/capability-dispositions.json",
        "success-evidence": product / "wiki/success-evidence.md",
        "verification": product / "wiki/verification.md",
        "outcome-evidence": product / "wiki/outcome-evidence.md",
        "accepted-deviations": product / "wiki/accepted-deviations.md",
    }
    for label, path in boundary_artifacts.items():
        path.write_text(f"# {label}\n\nExact bounded evidence.\n", encoding="utf-8")
    (product / "product.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "review@example.invalid"],
        cwd=product,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Review Gate Test"],
        cwd=product,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=product, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    )

    initial_current_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    current_boundary = ReviewBoundaryInput(
        purpose="implementation",
        outcome_contract_sha256=extract_from_bytes(
            review_plan.read_bytes()
        ).sha256,
        plan_sha256=hashlib.sha256(review_plan.read_bytes()).hexdigest(),
        product_head_sha=initial_current_head,
        verification_evidence_sha256=hashlib.sha256(
            boundary_artifacts["verification"].read_bytes()
        ).hexdigest(),
        verification_evidence_path="wiki/verification.md",
    )
    current_boundary_path = base / "implementation-boundary.json"
    current_boundary_path.write_text(
        json.dumps(current_boundary.payload(), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    class FailOnceCurrentRuntime(EffectRecordingRuntime):
        def __init__(self, store: OperationStore) -> None:
            super().__init__(store)
            self.fail_once = True

        def start(
            self, request: object, *, on_surface_opened=None
        ) -> FakeSessionResult:
            callback = request.cwd / request.callback_pointer
            if not callback.parent.is_dir():
                raise AssertionError(
                    "current review must prepare callback scratch before start"
                )
            if self.fail_once:
                self.fail_once = False
                self.store.create(
                    request.spec,
                    lane_id=request.lane_id,
                    run_id=request.run_id,
                )
                raise RuntimeError("simulated pre-launch interruption")
            return super().start(
                request, on_surface_opened=on_surface_opened
            )

    current_store = OperationStore(product / ".vault-meta/harness")
    current_runtime = FailOnceCurrentRuntime(current_store)
    old_environment = {
        name: os.environ.get(name)
        for name in (
            "LLM_OBSIDIAN_SESSION_RUNTIME",
            "LLM_OBSIDIAN_SESSION_MODEL",
            "LLM_OBSIDIAN_SESSION_EFFORT",
        )
    }
    os.environ["LLM_OBSIDIAN_SESSION_RUNTIME"] = "codex"
    os.environ["LLM_OBSIDIAN_SESSION_MODEL"] = "gpt-5.6-sol"
    os.environ["LLM_OBSIDIAN_SESSION_EFFORT"] = "high"
    try:
        try:
            task_review_runner.run_current_review(
                product,
                purpose="implementation",
                boundary_input_file=current_boundary_path,
                plan_file=review_plan,
                origin_surface="33333333-3333-4333-8333-333333333333",
                scratch_root=scratch,
                runtime_manager=current_runtime,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError(
                "current review interruption fixture did not fail"
            )
        started = task_review_runner.run_current_review(
            product,
            purpose="implementation",
            boundary_input_file=current_boundary_path,
            plan_file=review_plan,
            scratch_root=scratch,
            runtime_manager=current_runtime,
        )
        current_gate_root = (
            product
            / ".vault-meta/harness/review-data"
            / started["task_id"]
            / started["task_id"]
        )
        manifest = Path(started["context_manifest"])
        check(
            "current checkout review needs no dispatch metadata and keeps scratch external",
            started["status"] == "reviewing"
            and not (product / ".task-meta.json").exists()
            and started["worktree"] == str(product.resolve())
            and started["vault_root"] == str(product.resolve())
            and len(current_runtime.started) == 1
            and current_runtime.started[0].product_root == product.resolve()
            and current_runtime.started[0].cwd != product.resolve()
            and "Typed current-review callback is ready"
            in current_runtime.started[0].callback_wake
            and "task-review-runner.py current"
            in current_runtime.started[0].callback_wake
            and f"--worktree {product.resolve()}"
            in current_runtime.started[0].callback_wake
            and manifest.is_file()
            and product.resolve() not in manifest.parents,
        )
        check(
            "current checkout review resumes its initialized pre-launch gate",
            json.loads(
                (current_gate_root / "review-gate.json").read_text(
                    encoding="utf-8"
                )
            )["status"]
            == "reviewing",
        )
        lane = started["lanes"][0]
        current_simple_axis = str(lane["axis"])
        active = task_review_runner.load_active_round(
            current_gate_root,
            current_store,
            current_runtime,
            axis=current_simple_axis,
        )
        callback = review_round_envelope(
            active.round,
            ReviewResult(
                current_simple_axis,
                "changes-requested",
                (
                    ReviewFinding(
                        "F-current-1",
                        current_simple_axis,
                        "important",
                        "fix the current checkout",
                        "VALUE still equals one",
                        file="product.py",
                    ),
                ),
            ),
        )
        Path(lane["callback_path"]).write_text(
            json.dumps(to_dict(callback), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        waiting = task_review_runner.run_current_review(
            product,
            purpose="implementation",
            boundary_input_file=current_boundary_path,
            plan_file=review_plan,
            scratch_root=scratch,
            runtime_manager=current_runtime,
        )
        current_reviewed_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=product,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (product / "product.py").write_text("VALUE = 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "product.py"], cwd=product, check=True)
        subprocess.run(
            ["git", "commit", "-m", "resolve current review"],
            cwd=product,
            check=True,
            capture_output=True,
            text=True,
        )
        current_resolved_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=product,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (product / ".task-review-resolution.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation_id": started["task_id"],
                    "review_identity_sha256": resolution_transport_identity(
                        ReviewGateController(
                            current_gate_root,
                            current_runtime,
                            current_store,
                        )
                    ),
                    "reviewed_head_sha": current_reviewed_head,
                    "resolved_head_sha": current_resolved_head,
                    "resolutions": [
                        {
                            "finding_id": "F-current-1",
                            "disposition": "applied",
                            "rationale": (
                                "The current-checkout correction is present "
                                "on the resolved HEAD."
                            ),
                            "follow_up": "",
                        }
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        verifying = task_review_runner.run_current_review(
            product,
            purpose="implementation",
            boundary_input_file=current_boundary_path,
            plan_file=review_plan,
            scratch_root=scratch,
            runtime_manager=current_runtime,
        )
        check(
            "purpose-bound current review rebinds the exact resolution HEAD in its durable parent session",
            waiting["status"] == "awaiting-resolution"
            and verifying["status"] == "verifying"
            and verifying["task_id"] == started["task_id"]
            and len(current_runtime.started) == 1
            and len(current_runtime.continued) == 1
            and current_runtime.continued[0][1] == lane["operation_id"],
        )
        current_gate_state = json.loads(
            (current_gate_root / "review-gate.json").read_text(
                encoding="utf-8"
            )
        )
        current_gate_state["status"] = "attention-required"
        (current_gate_root / "review-gate.json").write_text(
            json.dumps(current_gate_state, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        quiesce_operations(current_store, started["task_id"])
        restarted = task_review_runner.run_current_review(
            product,
            origin_surface="33333333-3333-4333-8333-333333333333",
            scratch_root=scratch,
            runtime_manager=current_runtime,
        )
        check(
            "quiescent attention gate permits one fresh current review",
            restarted["status"] == "reviewing"
            and restarted["task_id"] != started["task_id"],
        )
        quiesce_operations(current_store, restarted["task_id"])
        restarted_gate_path = (
            product
            / ".vault-meta/harness/review-data"
            / restarted["task_id"]
            / restarted["task_id"]
            / "review-gate.json"
        )
        guarded_state = json.loads(
            restarted_gate_path.read_text(encoding="utf-8")
        )
        guarded_state["round_results"] = {
            "anthropic-holistic": f'{restarted["task_id"]}/round-anthropic-holistic-0.json'
        }
        restarted_gate_path.write_text(
            json.dumps(guarded_state, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            task_review_runner.run_current_review(
                product,
                deep=True,
                cross_model=True,
                runtime="claude",
                model="fable",
                effort="xhigh",
                origin_surface="33333333-3333-4333-8333-333333333333",
                scratch_root=scratch,
                runtime_manager=current_runtime,
            )
        except task_review_runner.TaskReviewError as exc:
            guarded = "another preset or override" in str(exc)
        else:
            guarded = False
        check(
            "quiescent review with persisted results cannot be superseded",
            guarded,
        )
        guarded_state["round_results"] = {}
        restarted_gate_path.write_text(
            json.dumps(guarded_state, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        superseded = task_review_runner.run_current_review(
            product,
            deep=True,
            cross_model=True,
            runtime="claude",
            model="fable",
            effort="xhigh",
            origin_surface="33333333-3333-4333-8333-333333333333",
            scratch_root=scratch,
            runtime_manager=current_runtime,
        )
        check(
            "resource-free result-free review permits a new current preset",
            superseded["status"] == "reviewing"
            and superseded["task_id"] != restarted["task_id"]
            and len(superseded["lanes"]) == 2,
        )
        quiesce_operations(current_store, superseded["task_id"])
        # The next assertions exercise a fresh purpose-bound release chain,
        # not authority inherited from this generic preset fixture.
        (
            product / ".vault-meta/harness/current-review/active.json"
        ).unlink()
        failed_full = task_review_runner.run_current_review(
            product,
            full=True,
            plan_file=review_plan,
            origin_surface="33333333-3333-4333-8333-333333333333",
            scratch_root=scratch,
            runtime_manager=current_runtime,
        )
        current_active_path = (
            product / ".vault-meta/harness/current-review/active.json"
        )
        failed_runtime_root = str(
            json.loads(current_active_path.read_text(encoding="utf-8"))[
                "runtime_root"
            ]
        )
        failed_started_count = len(current_runtime.started)
        live_full_replay = task_review_runner.run_current_review(
            product,
            full=True,
            plan_file=review_plan,
            scratch_root=scratch,
            runtime_manager=current_runtime,
        )
        failed_operation_ids = {
            str(lane["operation_id"]) for lane in failed_full["lanes"]
        }
        check(
            "live same-policy full review preserves task and reviewer identities",
            live_full_replay["task_id"] == failed_full["task_id"]
            and {
                str(lane["operation_id"])
                for lane in live_full_replay["lanes"]
            }
            == failed_operation_ids
            and len(current_runtime.started) == failed_started_count,
        )
        failed_gate_root = (
            product
            / ".vault-meta/harness/review-data"
            / failed_full["task_id"]
            / failed_full["task_id"]
        )
        failed_gate_path = failed_gate_root / "review-gate.json"
        failed_gate = json.loads(
            failed_gate_path.read_text(encoding="utf-8")
        )
        failed_axis = str(failed_full["lanes"][-1]["axis"])
        failed_finding_id = "openai-engineering.closed-dogfood"
        failed_result_pointer = (
            f'{failed_full["task_id"]}/round-{failed_axis}-0.json'
        )
        failed_result_path = failed_gate_root / failed_result_pointer
        failed_result_path.parent.mkdir(parents=True, exist_ok=True)
        failed_result_path.write_text(
            json.dumps(
                {
                    "axis": failed_axis,
                    "findings": [
                        {
                            "finding_id": failed_finding_id,
                            "severity": "important",
                        }
                    ],
                    "verdict": "changes-requested",
                    "verification_iteration": 0,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        failed_gate["status"] = "awaiting-resolution"
        failed_gate["round_results"] = {
            failed_axis: failed_result_pointer
        }
        failed_gate["final_results"] = {}
        failed_gate["awaiting_resolution"] = {
            failed_axis: {
                "callback_id": "review-" + "a" * 24,
                "callback_sha256": "b" * 64,
                "material_finding_ids": [failed_finding_id],
                "pointer": failed_result_pointer,
                "review_operation_id": failed_full["task_id"],
                "reviewed_head_sha": failed_gate["context"]["head_sha"],
                "round_operation_id": (
                    str(failed_full["lanes"][-1]["operation_id"])
                    + "-round-closed"
                ),
                "round_run_id": str(failed_full["lanes"][-1]["run_id"]),
            }
        }
        failed_gate["resolution_evidence"] = {}
        failed_gate["resolution_transport_identity_sha256"] = "c" * 64
        failed_gate_path.write_text(
            json.dumps(failed_gate, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        quiesce_operations(current_store, failed_full["task_id"])
        preserved_failed_gate = failed_gate_path.read_bytes()
        (product / "product.py").write_text("VALUE = 20\n", encoding="utf-8")
        subprocess.run(["git", "add", "product.py"], cwd=product, check=True)
        subprocess.run(
            ["git", "commit", "-m", "advance after closed full dogfood"],
            cwd=product,
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            fresh_full = task_review_runner.run_current_review(
                product,
                full=True,
                plan_file=review_plan,
                origin_surface="33333333-3333-4333-8333-333333333333",
                scratch_root=scratch,
                runtime_manager=current_runtime,
            )
        except task_review_runner.TaskReviewError as exc:
            fresh_full = None
            fresh_full_error = str(exc)
        else:
            fresh_full_error = ""
        fresh_operation_ids = (
            {
                str(lane["operation_id"])
                for lane in fresh_full["lanes"]
            }
            if fresh_full is not None
            else set()
        )
        fresh_runtime_root = (
            str(
                json.loads(
                    current_active_path.read_text(encoding="utf-8")
                )["runtime_root"]
            )
            if fresh_full is not None
            else ""
        )
        check(
            "closed failed full gate selects fresh task runtime and reviewer identities"
            + (f": {fresh_full_error}" if fresh_full_error else ""),
            fresh_full is not None
            and fresh_full["status"] == "reviewing"
            and fresh_full["task_id"] != failed_full["task_id"]
            and fresh_runtime_root != failed_runtime_root
            and len(fresh_operation_ids) == 4
            and fresh_operation_ids.isdisjoint(failed_operation_ids)
            and len(current_runtime.started) == failed_started_count + 4
            and all(
                sum(
                    request.spec.operation_id == operation_id
                    for request in current_runtime.started
                )
                == 1
                for operation_id in failed_operation_ids
            )
            and failed_gate_path.read_bytes() == preserved_failed_gate,
        )
        quiesce_operations(current_store, fresh_full["task_id"])
        (
            product / ".vault-meta/harness/current-review/active.json"
        ).unlink()
        release_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=product,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        release_boundary = ReviewBoundaryInput(
            purpose="release",
            outcome_contract_sha256=extract_from_bytes(
                review_plan.read_bytes()
            ).sha256,
            plan_sha256=hashlib.sha256(review_plan.read_bytes()).hexdigest(),
            integration_head_sha=release_head,
            outcome_evidence_map_sha256=hashlib.sha256(
                boundary_artifacts["outcome-evidence"].read_bytes()
            ).hexdigest(),
            outcome_evidence_map_path="wiki/outcome-evidence.md",
            accepted_deviations_sha256=hashlib.sha256(
                boundary_artifacts["accepted-deviations"].read_bytes()
            ).hexdigest(),
            accepted_deviations_path="wiki/accepted-deviations.md",
        )
        release_boundary_path = base / "release-boundary.json"
        release_boundary_path.write_text(
            json.dumps(release_boundary.payload(), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        release_started = task_review_runner.run_current_review(
            product,
            purpose="release",
            boundary_input_file=release_boundary_path,
            plan_file=review_plan,
            origin_surface="33333333-3333-4333-8333-333333333333",
            scratch_root=scratch,
            runtime_manager=current_runtime,
        )
        release_gate_root = (
            product
            / ".vault-meta/harness/review-data"
            / release_started["task_id"]
            / release_started["task_id"]
        )
        release_state = json.loads(
            (release_gate_root / "review-gate.json").read_text(
                encoding="utf-8"
            )
        )
        release_manifest = json.loads(
            Path(release_started["context_manifest"]).read_text(
                encoding="utf-8"
            )
        )
        release_wake = current_runtime.started[-1].callback_wake
        started_count = len(current_runtime.started)
        release_replay = task_review_runner.run_current_review(
            product,
            purpose="release",
            boundary_input_file=release_boundary_path,
            plan_file=review_plan,
            scratch_root=scratch,
            runtime_manager=current_runtime,
        )
        check(
            "current release review binds its manifest, purpose, and zero fix budget",
            release_started["review_purpose"] == "release"
            and release_started["review_boundary_input_sha256"]
            == release_boundary.input_sha256
            and release_state["policy"]["purpose"] == "release"
            and release_state["policy"]["max_verify_iterations"] == 0
            and any(
                item["name"] == "review-boundary-input.json"
                for item in release_manifest["inputs"]
            )
            and {
                "outcome-contract.json",
                "review-outcome-evidence",
                "review-accepted-deviations",
            }
            <= {item["name"] for item in release_manifest["inputs"]}
            and "--purpose release" in release_wake
            and "--boundary-input" in release_wake,
        )
        check(
            "purpose-bound current review replays without a duplicate provider effect",
            release_replay["task_id"] == release_started["task_id"]
            and len(current_runtime.started) == started_count,
        )
        quiesce_operations(current_store, release_started["task_id"])
        release_state["status"] = "stopped"
        release_state["round_results"] = {
            "anthropic-holistic": f'{release_started["task_id"]}/round-anthropic-holistic-0.json'
        }
        (release_gate_root / "review-gate.json").write_text(
            json.dumps(release_state, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        active_post_stop = json.loads(
            (
                product
                / ".vault-meta/harness/current-review/active.json"
            ).read_text(encoding="utf-8")
        )
        stopped_fixture_status = json.loads(
            (release_gate_root / "review-gate.json").read_text(
                encoding="utf-8"
            )
        )["status"]
        check(
            "stopped release fixture binds the active current review",
            active_post_stop["task_id"] == release_started["task_id"]
            and stopped_fixture_status == "stopped",
        )
        try:
            task_review_runner.run_current_review(
                product,
                deep=True,
                cross_model=True,
                runtime="claude",
                model="fable",
                effort="xhigh",
                purpose="release",
                boundary_input_file=release_boundary_path,
                plan_file=review_plan,
                origin_surface="33333333-3333-4333-8333-333333333333",
                scratch_root=scratch,
                runtime_manager=current_runtime,
            )
        except task_review_runner.TaskReviewError as exc:
            policy_only_guarded = "another preset or override" in str(exc)
        else:
            policy_only_guarded = False
        check(
            "stopped release review rejects a policy-only retry",
            policy_only_guarded,
        )
        same_head_implementation_path = (
            base / "same-head-implementation-boundary.json"
        )
        same_head_implementation = replace(
            current_boundary,
            product_head_sha=release_boundary.integration_head_sha,
        )
        same_head_implementation_path.write_text(
            json.dumps(same_head_implementation.payload(), sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        same_head_started_count = len(current_runtime.started)
        try:
            task_review_runner.run_current_review(
                product,
                deep=True,
                purpose="implementation",
                boundary_input_file=same_head_implementation_path,
                plan_file=review_plan,
                origin_surface="33333333-3333-4333-8333-333333333333",
                scratch_root=scratch,
                runtime_manager=current_runtime,
            )
        except task_review_runner.TaskReviewError as exc:
            same_head_implementation_guarded = (
                "another preset or override" in str(exc)
            )
        else:
            same_head_implementation_guarded = False
        check(
            "stopped release rejects a same-HEAD implementation checkpoint",
            same_head_implementation_guarded
            and len(current_runtime.started) == same_head_started_count,
        )
        (product / "product.py").write_text("VALUE = 3\n", encoding="utf-8")
        subprocess.run(["git", "add", "product.py"], cwd=product, check=True)
        subprocess.run(
            ["git", "commit", "-m", "advance after stopped release review"],
            cwd=product,
            check=True,
            capture_output=True,
            text=True,
        )
        post_stop_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=product,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        post_stop_implementation = replace(
            current_boundary,
            product_head_sha=post_stop_head,
        )
        post_stop_implementation_path = (
            base / "post-stop-implementation-boundary.json"
        )
        post_stop_implementation_path.write_text(
            json.dumps(post_stop_implementation.payload(), sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        post_stop_boundary = replace(
            release_boundary,
            integration_head_sha=post_stop_head,
        )
        post_stop_boundary_path = base / "post-stop-release-boundary.json"
        post_stop_boundary_path.write_text(
            json.dumps(post_stop_boundary.payload(), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        direct_release_started_count = len(current_runtime.started)
        try:
            task_review_runner.run_current_review(
                product,
                purpose="release",
                boundary_input_file=post_stop_boundary_path,
                plan_file=review_plan,
                origin_surface="33333333-3333-4333-8333-333333333333",
                scratch_root=scratch,
                runtime_manager=current_runtime,
            )
        except task_review_runner.TaskReviewError as exc:
            direct_release_guarded = "another preset or override" in str(exc)
        else:
            direct_release_guarded = False
        check(
            "stopped release rejects a direct changed-HEAD release retry",
            direct_release_guarded
            and len(current_runtime.started) == direct_release_started_count,
        )
        implementation_cycle = task_review_runner.run_current_review(
            product,
            deep=True,
            purpose="implementation",
            boundary_input_file=post_stop_implementation_path,
            plan_file=review_plan,
            origin_surface="33333333-3333-4333-8333-333333333333",
            scratch_root=scratch,
            runtime_manager=current_runtime,
        )
        check(
            "stopped release permits one exact implementation checkpoint",
            implementation_cycle["status"] == "reviewing"
            and implementation_cycle["task_id"]
            != release_started["task_id"],
        )
        quiesce_operations(current_store, implementation_cycle["task_id"])
        unapproved_release_started_count = len(current_runtime.started)
        try:
            task_review_runner.run_current_review(
                product,
                purpose="release",
                boundary_input_file=post_stop_boundary_path,
                plan_file=review_plan,
                origin_surface="33333333-3333-4333-8333-333333333333",
                scratch_root=scratch,
                runtime_manager=current_runtime,
            )
        except task_review_runner.TaskReviewError as exc:
            unapproved_release_guarded = (
                "another preset or override" in str(exc)
            )
        else:
            unapproved_release_guarded = False
        check(
            "release waits for exact implementation checkpoint approval",
            unapproved_release_guarded
            and len(current_runtime.started)
            == unapproved_release_started_count,
        )
        implementation_gate_root = (
            product
            / ".vault-meta/harness/review-data"
            / implementation_cycle["task_id"]
            / implementation_cycle["task_id"]
        )
        implementation_state_path = implementation_gate_root / "review-gate.json"
        implementation_state = json.loads(
            implementation_state_path.read_text(encoding="utf-8")
        )
        implementation_state["status"] = "approved"
        implementation_state["final_results"] = {"anthropic-holistic": "final-anthropic-holistic.json"}
        implementation_state_path.write_text(
            json.dumps(implementation_state, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        forged_started_count = len(current_runtime.started)
        try:
            task_review_runner.run_current_review(
                product,
                purpose="release",
                boundary_input_file=post_stop_boundary_path,
                plan_file=review_plan,
                scratch_root=scratch,
                runtime_manager=current_runtime,
            )
        except task_review_runner.TaskReviewError as exc:
            forged_guarded = "another preset or override" in str(exc)
        else:
            forged_guarded = False
        check(
            "release rejects an incomplete forged implementation approval",
            forged_guarded and len(current_runtime.started) == forged_started_count,
        )
        write_trusted_current_approval(
            implementation_gate_root,
            implementation_cycle["task_id"],
            post_stop_head,
        )
        changed_plan = product / "wiki/plans/changed-review-plan.md"
        changed_plan.parent.mkdir(parents=True, exist_ok=True)
        changed_plan.write_text("# Changed review plan\n", encoding="utf-8")
        changed_plan_boundary = replace(
            post_stop_boundary,
            plan_sha256=hashlib.sha256(changed_plan.read_bytes()).hexdigest(),
        )
        changed_plan_path = base / "changed-plan-release-boundary.json"
        changed_plan_path.write_text(
            json.dumps(changed_plan_boundary.payload(), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        changed_outcome_boundary = replace(
            post_stop_boundary,
            outcome_contract_sha256="f" * 64,
        )
        changed_outcome_path = base / "changed-outcome-release-boundary.json"
        changed_outcome_path.write_text(
            json.dumps(changed_outcome_boundary.payload(), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for label, boundary_path, plan_path in (
            ("plan", changed_plan_path, changed_plan),
            ("outcome", changed_outcome_path, review_plan),
        ):
            mismatch_started_count = len(current_runtime.started)
            try:
                task_review_runner.run_current_review(
                    product,
                    purpose="release",
                    boundary_input_file=boundary_path,
                    plan_file=plan_path,
                    scratch_root=scratch,
                    runtime_manager=current_runtime,
                )
            except task_review_runner.TaskReviewError as exc:
                mismatch_guarded = "another preset or override" in str(exc)
            else:
                mismatch_guarded = False
            check(
                f"release rejects changed {label} checkpoint identity",
                mismatch_guarded
                and len(current_runtime.started) == mismatch_started_count,
            )
        (product / "product.py").write_text("VALUE = 4\n", encoding="utf-8")
        subprocess.run(["git", "add", "product.py"], cwd=product, check=True)
        subprocess.run(
            ["git", "commit", "-m", "advance after implementation approval"],
            cwd=product,
            check=True,
            capture_output=True,
            text=True,
        )
        final_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=product,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        final_boundary = replace(post_stop_boundary, integration_head_sha=final_head)
        final_boundary_path = base / "final-release-boundary.json"
        final_boundary_path.write_text(
            json.dumps(final_boundary.payload(), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        moved_head_started_count = len(current_runtime.started)
        try:
            task_review_runner.run_current_review(
                product,
                purpose="release",
                boundary_input_file=final_boundary_path,
                plan_file=review_plan,
                scratch_root=scratch,
                runtime_manager=current_runtime,
            )
        except task_review_runner.TaskReviewError as exc:
            moved_head_guarded = "another preset or override" in str(exc)
        else:
            moved_head_guarded = False
        check(
            "release rejects a HEAD moved after implementation approval",
            moved_head_guarded
            and len(current_runtime.started) == moved_head_started_count,
        )
        write_trusted_current_approval(
            implementation_gate_root,
            implementation_cycle["task_id"],
            final_head,
            reviewed_head_sha=post_stop_head,
            valid_resolution_proof=False,
        )
        forged_resolution_started_count = len(current_runtime.started)
        try:
            task_review_runner.run_current_review(
                product,
                purpose="release",
                boundary_input_file=final_boundary_path,
                plan_file=review_plan,
                scratch_root=scratch,
                runtime_manager=current_runtime,
            )
        except task_review_runner.TaskReviewError as exc:
            forged_resolution_guarded = "another preset or override" in str(exc)
        else:
            forged_resolution_guarded = False
        check(
            "release rejects an empty post-approval resolution injection",
            forged_resolution_guarded
            and len(current_runtime.started) == forged_resolution_started_count,
        )
        write_trusted_current_approval(
            implementation_gate_root,
            implementation_cycle["task_id"],
            final_head,
            reviewed_head_sha=post_stop_head,
        )
        post_stop_review = task_review_runner.run_current_review(
            product,
            purpose="release",
            boundary_input_file=final_boundary_path,
            plan_file=review_plan,
            origin_surface="33333333-3333-4333-8333-333333333333",
            scratch_root=scratch,
            runtime_manager=current_runtime,
        )
        check(
            "resolved implementation checkpoint permits the final release boundary",
            post_stop_review["status"] == "reviewing"
            and post_stop_review["task_id"] != release_started["task_id"],
        )
        if post_stop_review is not None:
            quiesce_operations(current_store, post_stop_review["task_id"])
        post_stop_started_count = len(current_runtime.started)
        stale_boundary_path = base / "stale-release-boundary.json"
        stale_boundary_path.write_text(
            json.dumps(
                replace(
                    final_boundary,
                    accepted_deviations_sha256="f" * 64,
                ).payload(),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            task_review_runner.run_current_review(
                product,
                purpose="release",
                boundary_input_file=stale_boundary_path,
                plan_file=review_plan,
                scratch_root=scratch,
                runtime_manager=current_runtime,
            )
        except task_review_runner.TaskReviewError as exc:
            stale_rejected = "artifact digest is stale" in str(exc)
        else:
            stale_rejected = False
        check(
            "purpose-bound current review rejects stale artifact bytes before launch",
            stale_rejected
            and len(current_runtime.started) == post_stop_started_count,
        )
        intent_boundary = ReviewBoundaryInput(
            purpose="intent",
            outcome_contract_sha256=extract_from_bytes(
                review_plan.read_bytes()
            ).sha256,
            plan_sha256=hashlib.sha256(review_plan.read_bytes()).hexdigest(),
            design_sha256=hashlib.sha256(
                boundary_artifacts["design"].read_bytes()
            ).hexdigest(),
            design_path="wiki/design.md",
            capability_dispositions_sha256=hashlib.sha256(
                boundary_artifacts["capability-dispositions"].read_bytes()
            ).hexdigest(),
            capability_dispositions_path="wiki/capability-dispositions.json",
            success_evidence_map_sha256=hashlib.sha256(
                boundary_artifacts["success-evidence"].read_bytes()
            ).hexdigest(),
            success_evidence_map_path="wiki/success-evidence.md",
        )
        intent_inputs = task_review_runner._purpose_boundary_inputs(
            product.resolve(),
            review_plan,
            intent_boundary,
            pointer_root=scratch / "pointers",
        )
        intent_started_count = len(current_runtime.started)
        check(
            "current intent review materializes exact outcome evidence",
            {
                "outcome-contract.json",
                "review-design",
                "review-capability-dispositions",
                "review-success-evidence-map",
            }
            == {item.name for item in intent_inputs},
        )
        try:
            task_review_runner._purpose_boundary_inputs(
                product.resolve(),
                review_plan,
                replace(intent_boundary, plan_sha256="f" * 64),
                pointer_root=scratch / "pointers",
            )
        except task_review_runner.TaskReviewError as exc:
            stale_intent_rejected = "plan digest is stale" in str(exc)
        else:
            stale_intent_rejected = False
        check(
            "current intent review rejects stale plan bytes before launch",
            stale_intent_rejected
            and len(current_runtime.started) == intent_started_count,
        )
    finally:
        for name, value in old_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

with tempfile.TemporaryDirectory(prefix="pending-review-terminal.") as raw:
    unsafe_store = OperationStore(Path(raw) / "store")
    unsafe_context = ReviewContext(
        manifest="packets/review/manifest.json",
        head_sha="8" * 40,
        verification_profile="scoped",
        verification_profile_sha256="7" * 64,
    )
    unsafe_request = request_for(
        "pending-terminal",
        context=unsafe_context,
    )
    unsafe_identity = task_review_runner.review_session_specs(
        unsafe_request
    )[0]
    unsafe_store.create(
        unsafe_identity.spec,
        lane_id=unsafe_identity.lane_id,
        run_id=unsafe_identity.run_id,
    )
    unsafe_store.transition(
        unsafe_request.owner_id,
        unsafe_identity.spec.operation_id,
        "failed",
    )

    class PendingGateMarker:
        marked = False

        def mark_pending_attention(self) -> None:
            self.marked = True

    class UnusedRuntime:
        def status(self, owner_id: str, operation_id: str) -> object:
            raise AssertionError("terminal parent must not be probed")

    pending_gate = PendingGateMarker()
    check(
        "terminal parent cannot turn a pending gate into reviewing",
        not task_review_runner._pending_replay_is_safe(
            unsafe_request,
            unsafe_store,
            pending_gate,
            UnusedRuntime(),
        )
        and pending_gate.marked
        and unsafe_store.read(
            unsafe_request.owner_id,
            unsafe_identity.spec.operation_id,
        ).state
        == "failed",
    )

with tempfile.TemporaryDirectory(prefix="pending-review-dead.") as raw:
    dead_store = OperationStore(Path(raw) / "store")
    dead_context = ReviewContext(
        manifest="packets/review/manifest.json",
        head_sha="6" * 40,
        verification_profile="scoped",
        verification_profile_sha256="5" * 64,
    )
    dead_request = request_for("pending-dead", context=dead_context)
    dead_identity = task_review_runner.review_session_specs(dead_request)[0]
    dead_record = dead_store.create(
        dead_identity.spec,
        lane_id=dead_identity.lane_id,
        run_id=dead_identity.run_id,
    )
    dead_record = replace(
        dead_record,
        resources=replace(
            dead_record.resources,
            surface_id="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
            process_group=4321,
            supervisor_pid=4322,
            process_identity="4" * 64,
            supervisor_identity="3" * 64,
        ),
    )
    dead_store.save(dead_record, expected_revision=0)
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        dead_store.transition(
            dead_request.owner_id,
            dead_identity.spec.operation_id,
            state,
        )
    dead_record = dead_store.read(
        dead_request.owner_id, dead_identity.spec.operation_id
    )

    class DeadStatusRuntime:
        calls = 0

        def status(self, owner_id: str, operation_id: str) -> object:
            self.calls += 1
            return FakeSessionResult(
                dead_record,
                "checkpoint-dead",
                "observed",
                "dead",
                "alive",
            )

    dead_runtime = DeadStatusRuntime()
    dead_gate = PendingGateMarker()
    check(
        "dead identified parent cannot turn a pending gate into reviewing",
        not task_review_runner._pending_replay_is_safe(
            dead_request,
            dead_store,
            dead_gate,
            dead_runtime,
        )
        and dead_runtime.calls == 2
        and dead_gate.marked
        and dead_store.read(
            dead_request.owner_id,
            dead_identity.spec.operation_id,
        ).state
        == "attention-required",
    )

with tempfile.TemporaryDirectory(prefix="pending-review-retry.") as raw:
    retry_store = OperationStore(Path(raw) / "store")
    retry_context = ReviewContext(
        manifest="packets/review/manifest.json",
        head_sha="2" * 40,
        verification_profile="scoped",
        verification_profile_sha256="1" * 64,
    )
    retry_request = request_for("pending-retry", context=retry_context)
    retry_identity = task_review_runner.review_session_specs(
        retry_request
    )[0]
    retry_record = retry_store.create(
        retry_identity.spec,
        lane_id=retry_identity.lane_id,
        run_id=retry_identity.run_id,
    )
    retry_record = replace(
        retry_record,
        resources=replace(
            retry_record.resources,
            surface_id="BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB",
            process_group=5321,
            supervisor_pid=5322,
            process_identity="2" * 64,
            supervisor_identity="1" * 64,
        ),
    )
    retry_store.save(retry_record, expected_revision=0)
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        retry_store.transition(
            retry_request.owner_id,
            retry_identity.spec.operation_id,
            state,
        )

    class RetryStatusRuntime:
        calls = 0

        def status(self, owner_id: str, operation_id: str) -> object:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient read-only probe failure")
            return FakeSessionResult(
                retry_store.read(owner_id, operation_id),
                "checkpoint-retry",
                "observed",
                "alive",
                "alive",
            )

    retry_runtime = RetryStatusRuntime()
    retry_gate = PendingGateMarker()
    check(
        "pending replay retries one inconclusive read-only liveness probe",
        task_review_runner._pending_replay_is_safe(
            retry_request,
            retry_store,
            retry_gate,
            retry_runtime,
        )
        and retry_runtime.calls == 2
        and not retry_gate.marked,
    )

with tempfile.TemporaryDirectory(prefix="review-raced-parent.") as raw:
    raced_root = Path(raw)
    raced_store = OperationStore(raced_root / "store")
    raced_context = ReviewContext(
        manifest="packets/review/manifest.json",
        head_sha="9" * 40,
        verification_profile="scoped",
        verification_profile_sha256="8" * 64,
    )
    raced_request = request_for("raced-parent", context=raced_context)

    class RacedDeadRuntime(FakeRuntime):
        def start(
            self, request: object, *, on_surface_opened=None
        ) -> FakeSessionResult:
            record = self.store.create(
                request.spec,
                lane_id=request.lane_id,
                run_id=request.run_id,
            )
            record = replace(
                record,
                resources=replace(
                    record.resources,
                    surface_id="CCCCCCCC-CCCC-4CCC-8CCC-CCCCCCCCCCCC",
                    process_group=6321,
                    supervisor_pid=6322,
                    process_identity="9" * 64,
                    supervisor_identity="8" * 64,
                ),
            )
            self.store.save(record, expected_revision=0)
            for state in (
                "preflight",
                "starting",
                "running",
                "awaiting-callback",
            ):
                self.store.transition(
                    request.spec.owner_id,
                    request.spec.operation_id,
                    state,
                )
            return FakeSessionResult(
                self.store.read(
                    request.spec.owner_id, request.spec.operation_id
                ),
                "checkpoint-raced",
                "attention-required",
                "alive",
                "alive",
            )

    try:
        start_review(
            raced_request,
            RacedDeadRuntime(raced_store),
            origin_surface="44444444-4444-4444-8444-444444444444",
            cwd=raced_root,
            product_root=ROOT,
            prompt_pointer="prompts/review.md",
            callback_root="callbacks",
            round_store=raced_store,
        )
    except ValueError as exc:
        check(
            "raced supervisor-dead parent must prove resumable liveness",
            str(exc) == "stored review runtime is not live and resumable",
        )
    else:
        raise AssertionError("dead raced reviewer must not become reviewing")

with tempfile.TemporaryDirectory(prefix="fresh-review-preflight.") as raw:
    preflight_root = Path(raw)
    (preflight_root / "scratch").mkdir()
    preflight_store = OperationStore(preflight_root / "store")
    preflight_runtime = FakeRuntime(preflight_store)
    preflight_gate = ReviewGateController(
        preflight_root / "gate",
        preflight_runtime,
        preflight_store,
    )
    preflight_run = begin(
        preflight_gate,
        request_for("fresh-preflight", context=context),
        preflight_root / "scratch",
    )
    next_context = replace(
        context,
        manifest="packets/review/expanded-manifest.json",
    )
    preflight_boundary = ReviewScopeBoundary(
        "context",
        review_context_sha256(context),
        review_context_sha256(next_context),
        "approved bounded ContextPacket expansion",
    )
    preflight_authorization = {
        "schema_version": 1,
        "operation_id": "fresh-preflight",
        "kind": preflight_boundary.kind,
        "previous_context_sha256": (
            preflight_boundary.previous_context_sha256
        ),
        "next_context_sha256": preflight_boundary.next_context_sha256,
        "reason": preflight_boundary.reason,
        "authorization_provenance": "coordinator-approved",
        "verification_operation_id": "verification-preflight",
        "verification_receipt_sha256": "b" * 64,
        "status": "authorized",
    }
    preflight_authorization_path = (
        preflight_gate.root / "fresh-boundary-authorization.json"
    )
    preflight_authorization_path.write_text(
        json.dumps(preflight_authorization, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    preflight_gate._replace(status="attention-required")
    preflight_gate.authorize_fresh_boundary(
        preflight_run,
        boundary=preflight_boundary,
        authorization_pointer=preflight_authorization_path.name,
        authorization_sha256=hashlib.sha256(
            preflight_authorization_path.read_bytes()
        ).hexdigest(),
    )
    state_before_preflight = preflight_gate.read()
    try:
        preflight_gate.restart_for_boundary(
            preflight_run,
            boundary=preflight_boundary,
            context=next_context,
            origin_surface="55555555-5555-4555-8555-555555555555",
            cwd=ROOT,
            product_root=ROOT,
            prompt_pointer="prompts/compact.md",
            callback_root="callbacks/fresh-preflight",
        )
    except Exception as exc:
        isolation_rejected = (
            "isolated from product root" in str(exc)
        )
    else:
        isolation_rejected = False
    if not (
        isolation_rejected
        and preflight_gate.read() == state_before_preflight
    ):
        regression_failures.append(
            "scratch isolation preflight precedes fresh intent persistence"
        )

if regression_failures:
    raise AssertionError(
        "RED review recovery regressions: "
        + "; ".join(regression_failures)
    )

print("\nAll review gate tests passed.")
