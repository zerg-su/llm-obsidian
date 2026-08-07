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
import shutil
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
    CapabilityReport,
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
from harness.workflows.review_gate_attempt import (
    compile_review_attempt_identity,
)
from review_resolution import (
    FindingResolution,
    ReviewResolutionEvidence,
    review_transport_identity_sha256,
)
from review_contract import axis_finding_id
from outcome_contract import extract_from_bytes
from task_review_current import _current_review_artifact_root
from task_review_identity import _zero_effect_attention_is_quiescent


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


with tempfile.TemporaryDirectory(prefix="current-zero-effect-attention.") as raw:
    zero_effect_vault = Path(raw)
    zero_effect_task = "11111111-1111-4111-8111-111111111111"
    zero_effect_gate = {
        "status": "attention-required",
        "lanes": [],
        "round_results": {},
        "final_results": {},
        "attempt": {
            "status": "terminal",
            "terminal": {
                "result": "attention-required",
                "lane_results": [],
            },
        },
    }
    check(
        "current review supersedes an exact zero-row pre-provider attention gate",
        _zero_effect_attention_is_quiescent(
            zero_effect_vault, zero_effect_task, zero_effect_gate
        ),
    )
    zero_effect_store = OperationStore(
        zero_effect_vault / ".vault-meta/harness"
    )
    zero_effect_operation = "22222222-2222-4222-8222-222222222222"
    zero_effect_store.create(
        OperationSpec(
            zero_effect_operation,
            "a" * 64,
            "simple-review-holistic",
            zero_effect_task,
            RuntimeRoute(
                "claude",
                "claude-opus-5",
                "xhigh",
                "reviewer-callback",
                "b" * 64,
            ),
            "packets/review/manifest.json",
            "scoped",
        ),
        lane_id="c" * 32,
        run_id="d" * 32,
    )
    check(
        "current review keeps any persisted operation row fail closed",
        not _zero_effect_attention_is_quiescent(
            zero_effect_vault, zero_effect_task, zero_effect_gate
        ),
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
        self.superseded_cleanups: list[Path] = []
        self.cleanup_attention = False
        self.cleanup_terminate_once = False

    def preflight_routes(
        self,
        requests: tuple[tuple[RuntimeRoute, Path, str], ...],
    ) -> tuple[CapabilityReport, ...]:
        return tuple(
            CapabilityReport(route, True, ("provider:profile-valid",))
            for route, _callback_dir, _origin_surface in requests
        )

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

    def cleanup_superseded_review(self, receipt_path: Path) -> object:
        self.superseded_cleanups.append(receipt_path)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        owner_id = receipt["superseded_owner_id"]
        operation_id = receipt["superseded_operation_id"]
        self.request_exit(owner_id, operation_id)
        return self.cleanup(owner_id, operation_id)


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
    profile_path = product / "config/verification-profiles.toml"
    profile_path.parent.mkdir(exist_ok=True)
    profile_path.write_bytes((ROOT / "config/verification-profiles.toml").read_bytes())
    write_scoped_verification(product, verification_path, reviewed_head)
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

for depth in ("deep", "full"):
    with tempfile.TemporaryDirectory(
        prefix=f"review-gate-{depth}-minor-finalization."
    ) as raw:
        base = Path(raw)
        scratch = base / "scratch"
        scratch.mkdir()
        store = OperationStore(base / "store")
        runtime = FakeRuntime(store)
        controller = ReviewGateController(base / "gate", runtime, store)
        operation_id = f"review-{depth}-minor-finalization"
        run = begin(
            controller,
            request_for(operation_id, depth=depth, context=context),
            scratch,
        )
        decision = None
        for lane in run.execution.lanes:
            decision = controller.complete_round(
                run,
                lane,
                run.rounds[lane.axis],
                ReviewResult(
                    lane.axis,
                    "approve",
                    (
                        ReviewFinding(
                            "F-minor",
                            lane.axis,
                            "minor",
                            "non-blocking independent note",
                            "the lane retains one attributed minor finding",
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
        check(
            f"{depth} minor findings preserve callback bytes and authorize",
            decision is not None
            and decision.action == "approved"
            and authorization.approved
            and {
                finding["finding_id"]
                for axis in authorization.evidence["axes"]
                for finding in axis["findings"]
            }
            == {
                axis_finding_id(lane.axis, "F-minor")
                for lane in run.execution.lanes
            },
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

module_spec = importlib.util.spec_from_file_location(
    "task_review_runner_module", ROOT / "scripts/task-review-runner.py"
)
assert module_spec is not None and module_spec.loader is not None
task_review_runner = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(task_review_runner)

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
    write_scoped_verification(
        product,
        boundary_artifacts["verification"],
        initial_current_head,
    )
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

    current_store = OperationStore(product / ".vault-meta/harness")
    current_runtime = EffectRecordingRuntime(current_store)
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

    def zero_effect_checkout(name: str) -> Path:
        checkout = base / name
        shutil.copytree(
            product,
            checkout,
            ignore=shutil.ignore_patterns(
                ".git", ".vault-meta", "__pycache__", "*.pyc"
            ),
        )
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "review@example.invalid"],
            cwd=checkout,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Review Gate Test"],
            cwd=checkout,
            check=True,
        )
        subprocess.run(["git", "add", "."], cwd=checkout, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        return checkout

    class PreStoreFailureRuntime(EffectRecordingRuntime):
        def start(self, request: object, *, on_surface_opened=None) -> FakeSessionResult:
            self.started.append(request)
            raise RuntimeError("pre-provider fixture failure")

    try:
        started = task_review_runner.run_current_review(
            product,
            purpose="implementation",
            boundary_input_file=current_boundary_path,
            plan_file=review_plan,
            origin_surface="33333333-3333-4333-8333-333333333333",
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
            and " --plan "
            not in current_runtime.started[0].callback_wake
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

        retry_product = zero_effect_checkout("zero-effect-retry")
        retry_store = OperationStore(retry_product / ".vault-meta/harness")
        failed_runtime = PreStoreFailureRuntime(retry_store)
        try:
            task_review_runner.run_current_review(
                retry_product,
                origin_surface="33333333-3333-4333-8333-333333333333",
                scratch_root=base / "zero-effect-retry-scratch",
                runtime_manager=failed_runtime,
            )
        except RuntimeError as exc:
            assert str(exc) == "pre-provider fixture failure"
        else:
            raise AssertionError("pre-provider fixture failure was swallowed")
        first_active = json.loads(
            (
                retry_product
                / ".vault-meta/harness/current-review/active.json"
            ).read_text(encoding="utf-8")
        )
        first_task_id = first_active["task_id"]
        first_gate = json.loads(
            (
                retry_product
                / ".vault-meta/harness/review-data"
                / first_task_id
                / first_task_id
                / "review-gate.json"
            ).read_text(encoding="utf-8")
        )
        check(
            "writer emits an exact zero-effect attention gate",
            first_gate["status"] == "attention-required"
            and first_gate["lanes"] == []
            and first_gate["round_results"] == {}
            and first_gate["final_results"] == {}
            and not retry_store.list(first_task_id),
        )
        retry_runtime = EffectRecordingRuntime(retry_store)
        retried = task_review_runner.run_current_review(
            retry_product,
            origin_surface="33333333-3333-4333-8333-333333333333",
            scratch_root=base / "zero-effect-retry-scratch",
            runtime_manager=retry_runtime,
        )
        check(
            "durable current-review interface supersedes a zero-effect failure",
            retried["status"] == "reviewing"
            and retried["task_id"] != first_task_id
            and len(retry_runtime.started) == 1,
        )

        retained_product = zero_effect_checkout("zero-effect-retained-row")
        retained_store = OperationStore(retained_product / ".vault-meta/harness")
        try:
            task_review_runner.run_current_review(
                retained_product,
                origin_surface="33333333-3333-4333-8333-333333333333",
                scratch_root=base / "zero-effect-retained-row-scratch",
                runtime_manager=PreStoreFailureRuntime(retained_store),
            )
        except RuntimeError as exc:
            assert str(exc) == "pre-provider fixture failure"
        else:
            raise AssertionError("pre-provider fixture failure was swallowed")
        retained_task_id = json.loads(
            (
                retained_product
                / ".vault-meta/harness/current-review/active.json"
            ).read_text(encoding="utf-8")
        )["task_id"]
        retained_store.create(
            OperationSpec(
                "22222222-2222-4222-8222-222222222222",
                "a" * 64,
                "simple-review-holistic",
                retained_task_id,
                RuntimeRoute(
                    "claude",
                    "claude-opus-5",
                    "xhigh",
                    "reviewer-callback",
                    "b" * 64,
                ),
                "packets/review/manifest.json",
                "scoped",
            ),
            lane_id="c" * 32,
            run_id="d" * 32,
        )
        retained_runtime = EffectRecordingRuntime(retained_store)
        try:
            retained_result = task_review_runner.run_current_review(
                retained_product,
                origin_surface="33333333-3333-4333-8333-333333333333",
                scratch_root=base / "zero-effect-retained-row-scratch",
                runtime_manager=retained_runtime,
            )
        except task_review_runner.TaskReviewError:
            retained_result = None
        retained_active = json.loads(
            (
                retained_product
                / ".vault-meta/harness/current-review/active.json"
            ).read_text(encoding="utf-8")
        )
        check(
            "durable current-review interface keeps any operation row fail closed",
            retained_active["task_id"] == retained_task_id
            and not retained_runtime.started
            and (
                retained_result is None
                or retained_result["task_id"] == retained_task_id
            ),
        )
    finally:
        for name, value in old_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

with tempfile.TemporaryDirectory(prefix="current-release-artifacts.") as raw:
    base = Path(raw)

    def release_fixture(
        name: str,
        *,
        outcome_path: str = "outcome-evidence.json",
        deviations_path: str = "accepted-deviations.json",
        symlink_outcome_dir: bool = False,
        symlink_deviations: bool = False,
    ) -> tuple[Path, Path, Path, Path]:
        product = base / name / "checkout"
        evidence_root = base / name / "evidence"
        scratch = base / name / "scratch"
        subprocess.run(
            ["git", "clone", "--quiet", "--no-local", str(ROOT), str(product)],
            check=True,
        )
        evidence_root.mkdir(parents=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=product,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        plan = evidence_root / "approved-plan.md"
        plan.write_text(
            """# Release plan

```json
{"schema_version":1,"desired_outcome":"Bind one external release evidence root.","success_evidence":[{"evidence_id":"release-root","observable":"The exact external artifacts enter review."}],"non_goals":["No release effect."]}
```
""",
            encoding="utf-8",
        )
        outcome = evidence_root / outcome_path
        if symlink_outcome_dir:
            foreign_root = base / name / "foreign"
            foreign_root.mkdir(parents=True)
            (foreign_root / outcome.name).write_text(
                '{"release-root":"established"}\n', encoding="utf-8"
            )
            outcome.parent.symlink_to(foreign_root, target_is_directory=True)
        else:
            outcome.parent.mkdir(parents=True, exist_ok=True)
            outcome.write_text(
                '{"release-root":"established"}\n', encoding="utf-8"
            )
        deviations_target = evidence_root / "real-deviations.json"
        deviations_target.write_text('{"deviations":[]}\n', encoding="utf-8")
        deviations = evidence_root / deviations_path
        deviations.parent.mkdir(parents=True, exist_ok=True)
        if symlink_deviations:
            deviations.symlink_to(deviations_target)
        else:
            deviations.write_bytes(deviations_target.read_bytes())
        boundary = ReviewBoundaryInput(
            purpose="release",
            outcome_contract_sha256=extract_from_bytes(plan.read_bytes()).sha256,
            plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
            integration_head_sha=head,
            outcome_evidence_map_sha256=hashlib.sha256(outcome.read_bytes()).hexdigest(),
            outcome_evidence_map_path=outcome_path,
            accepted_deviations_sha256=hashlib.sha256(deviations.read_bytes()).hexdigest(),
            accepted_deviations_path=deviations_path,
        )
        boundary_path = evidence_root / "review-boundary.json"
        boundary_path.write_text(
            json.dumps(boundary.payload(), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return product, evidence_root, boundary_path, scratch

    def start_release(
        fixture: tuple[Path, Path, Path, Path],
    ) -> tuple[dict[str, object], EffectRecordingRuntime]:
        product, evidence_root, boundary_path, scratch = fixture
        runtime = EffectRecordingRuntime(
            OperationStore(product / ".vault-meta/harness")
        )
        started = task_review_runner.run_current_review(
            product,
            purpose="release",
            boundary_input_file=boundary_path,
            artifact_root=evidence_root,
            plan_file=evidence_root / "approved-plan.md",
            origin_surface="33333333-3333-4333-8333-333333333333",
            scratch_root=scratch,
            runtime_manager=runtime,
        )
        return started, runtime

    def release_rejected(
        fixture: tuple[Path, Path, Path, Path],
    ) -> bool:
        try:
            start_release(fixture)
        except task_review_runner.TaskReviewError:
            return True
        return False

    saved_route = {
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
        valid = release_fixture("valid")
        product, evidence_root, _boundary_path, _scratch = valid
        started, runtime = start_release(valid)
        active = json.loads(
            (
                product
                / ".vault-meta/harness/current-review/active.json"
            ).read_text(encoding="utf-8")
        )
        check(
            "current release review binds one external artifact root",
            started["status"] == "reviewing"
            and active["review_artifact_root"] == str(evidence_root.resolve())
            and len(runtime.started) == 1
            and f"--artifact-root {evidence_root.resolve()}"
            in runtime.started[0].callback_wake,
        )
        wake_argv = shlex.split(
            runtime.started[0].callback_wake.split(
                "Run this exact command: ", 1
            )[1]
        )
        wake_args = task_review_runner.parser().parse_args(wake_argv[2:])
        resumed = task_review_runner.run_current_review(
            wake_args.worktree,
            deep=wake_args.deep,
            full=wake_args.full,
            cross_model=wake_args.cross_model,
            runtime=wake_args.runtime,
            model=wake_args.model,
            effort=wake_args.effort,
            no_review=wake_args.no_review,
            purpose=wake_args.purpose,
            boundary_input_file=wake_args.boundary_input,
            artifact_root=wake_args.artifact_root,
            plan_file=wake_args.plan,
            scratch_root=valid[3],
            runtime_manager=runtime,
        )
        check(
            "release callback wake is an idempotent active-review resume",
            resumed["status"] == "reviewing"
            and wake_args.boundary_input.resolve()
            == (evidence_root / "review-boundary.json").resolve()
            and wake_args.plan.resolve()
            == (evidence_root / "approved-plan.md").resolve(),
        )
        for ancestor in (product.parent, Path("/")):
            try:
                _current_review_artifact_root(
                    product,
                    purpose="release",
                    boundary_input_file=product / "AGENTS.md",
                    plan_file=product / "README.md",
                    artifact_root=ancestor,
                )
            except task_review_runner.TaskReviewError:
                rejected = True
            else:
                rejected = False
            check(
                f"current release review rejects checkout ancestor {ancestor}",
                rejected,
            )
        replacement_root = base / "valid" / "replacement-evidence"
        shutil.copytree(evidence_root, replacement_root)
        check(
            "current release review rejects artifact-root drift on resume",
            release_rejected(
                (
                    product,
                    replacement_root,
                    replacement_root / "review-boundary.json",
                    valid[3],
                )
            ),
        )

        check(
            "current release review rejects an out-of-root outcome artifact",
            release_rejected(
                release_fixture(
                    "escaped",
                    outcome_path="outcome-link/outcome-evidence.json",
                    symlink_outcome_dir=True,
                )
            ),
        )

        check(
            "current release review rejects a symlinked deviations artifact",
            release_rejected(
                release_fixture("linked", symlink_deviations=True)
            ),
        )
    finally:
        for name, value in saved_route.items():
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

with tempfile.TemporaryDirectory(prefix="review-iteration-facade.") as raw:
    base = Path(raw)
    product = base / "product"
    scratch = base / "scratch"
    for directory in (
        product / "wiki",
        product / "skills/review",
        product / "scripts/harness",
        product / "config",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (product / "skills/review/SKILL.md").write_text(
        "# Review\n\nInspect the exact ContextPacket and product HEAD.\n",
        encoding="utf-8",
    )
    (product / "scripts/harness/review_submit.py").write_text(
        "# test fixture\n", encoding="utf-8"
    )
    for name in ("model-routing.toml", "verification-profiles.toml"):
        (product / "config" / name).write_bytes((ROOT / "config" / name).read_bytes())
    plan = product / "wiki/plan.md"
    plan.write_text(
        """# Iteration barrier plan

```json
{"schema_version":1,"desired_outcome":"Persist the exact newer Deep review iteration.","success_evidence":[{"evidence_id":"iteration-two","observable":"Both verification callbacks are accepted once."}],"non_goals":["Changing review topology."]}
```
""",
        encoding="utf-8",
    )
    evidence = product / "wiki/verification.md"
    evidence.write_text("# Verification\n\nExact fixture evidence.\n", encoding="utf-8")
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
    reviewed_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=product,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    write_scoped_verification(product, evidence, reviewed_head)
    boundary = ReviewBoundaryInput(
        purpose="implementation",
        outcome_contract_sha256=extract_from_bytes(plan.read_bytes()).sha256,
        plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
        product_head_sha=reviewed_head,
        verification_evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(),
        verification_evidence_path="wiki/verification.md",
    )
    boundary_path = base / "boundary.json"
    boundary_path.write_text(
        json.dumps(boundary.payload(), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    store = OperationStore(product / ".vault-meta/harness")
    runtime = EffectRecordingRuntime(store)
    saved_route = {
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
        started = task_review_runner.run_current_review(
            product,
            deep=True,
            runtime="codex",
            model="sol",
            effort="high",
            purpose="implementation",
            boundary_input_file=boundary_path,
            plan_file=plan,
            origin_surface="33333333-3333-4333-8333-333333333333",
            scratch_root=scratch,
            runtime_manager=runtime,
        )
        gate_root = (
            product
            / ".vault-meta/harness/review-data"
            / started["task_id"]
            / started["task_id"]
        )
        for lane in started["lanes"]:
            axis = str(lane["axis"])
            active = task_review_runner.load_active_round(
                gate_root, store, runtime, axis=axis
            )
            finding_id = f"iteration-{axis}"
            callback = review_round_envelope(
                active.round,
                ReviewResult(
                    axis,
                    "changes-requested",
                    (
                        ReviewFinding(
                            finding_id,
                            axis,
                            "important",
                            "advance the exact verification iteration",
                            "the initial fixture HEAD still needs its bounded fix",
                            file="product.py",
                        ),
                    ),
                    0,
                ),
            )
            Path(lane["callback_path"]).write_text(
                json.dumps(to_dict(callback), sort_keys=True) + "\n",
                encoding="utf-8",
            )
        awaiting = task_review_runner.run_current_review(
            product,
            deep=True,
            runtime="codex",
            model="sol",
            effort="high",
            purpose="implementation",
            boundary_input_file=boundary_path,
            plan_file=plan,
            scratch_root=scratch,
            runtime_manager=runtime,
        )
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
        boundary_path.write_text(
            json.dumps(
                replace(boundary, product_head_sha=resolved_head).payload(),
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        gate = ReviewGateController(gate_root, runtime, store)
        verifying = task_review_runner.run_current_review(
            product,
            deep=True,
            runtime="codex",
            model="sol",
            effort="high",
            purpose="implementation",
            boundary_input_file=boundary_path,
            plan_file=plan,
            scratch_root=scratch,
            runtime_manager=runtime,
        )
        gate_root = (
            product
            / ".vault-meta/harness/review-data"
            / verifying["task_id"]
            / verifying["task_id"]
        )
        gate = ReviewGateController(gate_root, runtime, store)
        for lane in verifying["lanes"]:
            axis = str(lane["axis"])
            active = task_review_runner.load_active_round(
                gate_root, store, runtime, axis=axis
            )
            callback = review_round_envelope(
                active.round, ReviewResult(axis, "approve", (), 0)
            )
            Path(lane["callback_path"]).write_text(
                json.dumps(to_dict(callback), sort_keys=True) + "\n",
                encoding="utf-8",
            )
        approved = task_review_runner.run_current_review(
            product,
            deep=True,
            runtime="codex",
            model="sol",
            effort="high",
            purpose="implementation",
            boundary_input_file=boundary_path,
            plan_file=plan,
            scratch_root=scratch,
            runtime_manager=runtime,
        )
        final_state = gate.read()
        final_iterations = {
            json.loads((gate_root / pointer).read_text(encoding="utf-8"))[
                "verification_iteration"
            ]
            for pointer in final_state["final_results"].values()
        }
        check(
            "Deep facade seals the terminal HEAD and starts one fresh attempt",
            awaiting["status"] == "changes-requested"
            and verifying["status"] == "reviewing"
            and approved["status"] == "approved"
            and final_iterations == {0}
            and len(final_state["final_results"]) == 2
            and len(runtime.started) == 4
            and not runtime.continued,
        )
    finally:
        for name, value in saved_route.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


with tempfile.TemporaryDirectory(prefix="review-zero-lane-preflight.") as raw:
    base = Path(raw)
    scratch = base / "scratch"
    scratch.mkdir()
    store = OperationStore(base / "store")

    class PreflightFailureRuntime(FakeRuntime):
        fail_once = True

        def start(self, request: object, *, on_surface_opened=None) -> object:
            if self.fail_once:
                self.fail_once = False
                raise RuntimeSessionError("runtime preflight failed")
            return super().start(
                request, on_surface_opened=on_surface_opened
            )

    failed_runtime = PreflightFailureRuntime(store)
    gate = ReviewGateController(base / "gate", failed_runtime, store)
    failed_request = request_for(
        "review-zero-lane-cycle-1", context=context
    )
    try:
        gate.begin_attempt(
            dispatch_operation_id="review-zero-lane-dispatch",
            finalization_lineage_id="review-zero-lane-lineage",
            cycle=1,
            plan_sha256="1" * 64,
            outcome_sha256="2" * 64,
            request=failed_request,
            origin_surface="11111111-1111-4111-8111-111111111111",
            cwd=scratch,
            product_root=ROOT,
            prompt_pointer="prompts/review.md",
            callback_root="callbacks/review-zero-lane",
        )
    except RuntimeSessionError:
        pass
    else:
        raise AssertionError("preflight fixture did not fail")
    failed_state = gate.read()
    replacement = request_for(
        "review-zero-lane-cycle-2", context=context
    )
    replacement_identity = compile_review_attempt_identity(
        request=replacement,
        finalization_lineage_id="review-zero-lane-lineage",
        cycle=2,
        plan_sha256="1" * 64,
        outcome_sha256="2" * 64,
    )
    reopened = gate._initialize_attempt(
        dispatch_operation_id="review-zero-lane-dispatch",
        identity=replacement_identity,
        request=replacement,
        product_root=ROOT,
    )
    replayed = gate._initialize_attempt(
        dispatch_operation_id="review-zero-lane-dispatch",
        identity=replacement_identity,
        request=replacement,
        product_root=ROOT,
    )
    recovered_state = gate.read()
    check(
        "terminal zero-lane preflight recovery installs one replay-safe pending attempt",
        failed_state["status"] == "attention-required"
        and failed_state["lanes"] == []
        and failed_state["round_results"] == {}
        and failed_state["final_results"] == {}
        and not store.list("owner-1")
        and reopened == replayed
        and reopened.status == "pending"
        and recovered_state["status"] == "pending"
        and recovered_state["attempt"]["identity"]["cycle"] == 2
        and not failed_runtime.started,
    )
    recovered_run = gate.begin_attempt(
        dispatch_operation_id="review-zero-lane-dispatch",
        finalization_lineage_id="review-zero-lane-lineage",
        cycle=2,
        plan_sha256="1" * 64,
        outcome_sha256="2" * 64,
        request=replacement,
        origin_surface="11111111-1111-4111-8111-111111111111",
        cwd=scratch,
        product_root=ROOT,
        prompt_pointer="prompts/review.md",
        callback_root="callbacks/review-zero-lane",
    )
    check(
        "recovered zero-lane preflight starts exactly one fresh bound lane",
        len(recovered_run.execution.lanes) == 1
        and len(failed_runtime.started) == 1
        and gate.read()["status"] == "reviewing"
        and gate.read()["attempt"]["status"] == "awaiting-callback",
    )


if regression_failures:
    raise AssertionError(
        "RED review recovery regressions: "
        + "; ".join(regression_failures)
    )

print("\nAll review gate tests passed.")
