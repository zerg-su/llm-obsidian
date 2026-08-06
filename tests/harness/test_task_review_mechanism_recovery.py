#!/usr/bin/env python3
"""Exact-authorization and quiescence regressions for review recovery."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import (  # noqa: E402
    AttentionReason,
    EffectOutcome,
    OperationRecord,
    OwnedResources,
    RuntimeRoute,
    to_dict,
)
from harness.callbacks import CallbackBroker  # noqa: E402
from harness.pipeline_builtins import compiled_builtin  # noqa: E402
from harness.state_machine import TERMINAL  # noqa: E402
from harness.store import OperationStore, StoreError  # noqa: E402
from harness.runtime_session_checkpoint import DurableCleanupOwnership  # noqa: E402
from harness.runtime_session_contracts import RuntimeSessionError  # noqa: E402
from harness.verification import load_profiles  # noqa: E402
from harness.workflows.review import (  # noqa: E402
    ReviewFinding,
    ReviewOperationRequest,
    ReviewResult,
    review_round_envelope,
)
from harness.workflows.review_gate import (  # noqa: E402
    ReviewGateController,
    ReviewPreset,
)
from task_review_context import (  # noqa: E402
    _context,
    _gate_root,
    _runtime_root,
)
from task_review_drift_contract import (  # noqa: E402
    _authorization_chain_boundary_is_valid,
    _compile_authorization_chain_compatibility,
    authorized_post_verification_review_drive,
    authorized_post_fresh_publication_sync,
    authorized_drift_quarantine,
    authorized_signal_free_retirement,
    authorized_supported_close_retirement,
)
from task_review_mechanism_recovery import (  # noqa: E402
    _authorized_accepted_callback_head,
    recover_task_review_for_mechanism,
)
from task_review_legacy_rounds import RecoveryRoundStore  # noqa: E402
from task_review_resolution_evidence import (  # noqa: E402
    approved_summary_resolution,
)
from task_review_shared import TaskReviewError  # noqa: E402
from task_escalation_records import (  # noqa: E402
    DecisionRecord,
    append_raise,
    append_resolution,
    load_chain,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def expect_error(label: str, action: object, message: str) -> None:
    try:
        action()
    except TaskReviewError as exc:
        check(label, message in str(exc))
    else:
        raise AssertionError(label)


@dataclass(frozen=True)
class SessionResult:
    record: OperationRecord
    checkpoint: str
    action: str = "observed"
    process_status: str = "alive"
    surface_status: str = "alive"


class FakeRuntime:
    """Only the provider transport is fake; gate and store state remain real."""

    def __init__(self, store: OperationStore) -> None:
        self.store = store
        self.started: list[object] = []
        self.registered: list[tuple[str, str, str, str, str]] = []
        self.exit_requests: list[str] = []
        self.cleanup_calls: list[str] = []
        self.fail_cleanup_for: set[str] = set()
        self.cleanup_ownership: dict[str, DurableCleanupOwnership] = {}
        self.cleanup_ownership_errors: dict[str, RuntimeSessionError] = {}
        self.ownership_probes: list[str] = []

    def start(self, request: object, *, on_surface_opened=None) -> SessionResult:
        self.started.append(request)
        record = self.store.create(
            request.spec,
            lane_id=request.lane_id,
            run_id=request.run_id,
        )
        if not record.resources.surface_id:
            updated = replace(
                record,
                resources=replace(
                    record.resources,
                    surface_id=(
                        f"{len(self.started):08d}-aaaa-4aaa-8aaa-"
                        f"{len(self.started):012d}"
                    ),
                ),
                revision=record.revision + 1,
            )
            self.store.save(updated, expected_revision=record.revision)
            record = updated
        result = SessionResult(record, "checkpoint-live")
        if on_surface_opened is not None:
            on_surface_opened(result)
        return result

    def status(self, owner_id: str, operation_id: str) -> SessionResult:
        return SessionResult(
            self.store.read(owner_id, operation_id), "checkpoint-live"
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

    def cleanup_superseded_review(self, receipt_path: Path) -> SessionResult:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        record = self.store.read(
            receipt["superseded_owner_id"],
            receipt["superseded_operation_id"],
        )
        if record.state not in TERMINAL:
            raise AssertionError(
                "mechanism fixture expected a quiescent superseded review"
            )
        return SessionResult(record, "", action="terminal")

    def request_exit(self, owner_id: str, operation_id: str) -> SessionResult:
        record = self.store.read(owner_id, operation_id)
        if record.state in TERMINAL:
            return SessionResult(record, "", action="terminal")
        self.exit_requests.append(operation_id)
        if record.state == "attention-required":
            self.store.transition(owner_id, operation_id, "cancelling")
        elif record.state != "finalizing":
            self.store.transition(owner_id, operation_id, "finalizing")
        self.store.transition(owner_id, operation_id, "exiting")
        record = self.store.read(owner_id, operation_id)
        return SessionResult(record, "", action="exit-requested")

    def cleanup(self, owner_id: str, operation_id: str) -> SessionResult:
        record = self.store.read(owner_id, operation_id)
        if record.state in TERMINAL:
            return SessionResult(record, "", action="terminal")
        self.cleanup_calls.append(operation_id)
        if operation_id in self.fail_cleanup_for:
            self.store.transition(
                owner_id,
                operation_id,
                "attention-required",
                reason=AttentionReason.CALLBACK_TIMEOUT,
            )
            return SessionResult(
                self.store.read(owner_id, operation_id),
                "",
                action="attention-required",
            )
        if record.state != "exiting":
            raise AssertionError("quarantine cleanup requires exiting ownership")
        if record.resources != OwnedResources():
            record = replace(
                record,
                resources=OwnedResources(),
                revision=record.revision + 1,
            )
            self.store.save(record, expected_revision=record.revision - 1)
        self.store.transition(owner_id, operation_id, "complete")
        record = self.store.read(owner_id, operation_id)
        return SessionResult(record, "", action="cleaned")

    def prove_durable_cleanup_ownership(
        self, owner_id: str, operation_id: str
    ) -> DurableCleanupOwnership:
        self.ownership_probes.append(operation_id)
        error = self.cleanup_ownership_errors.get(operation_id)
        if error is not None:
            raise error
        try:
            return self.cleanup_ownership[operation_id]
        except KeyError as exc:
            raise RuntimeSessionError("ownership evidence is unavailable") from exc


class LegacyRoundStore:
    """Create pre-parent-identity round records for recovery coverage."""

    def __init__(self, store: OperationStore) -> None:
        self.store = store

    def __getattr__(self, name: str) -> object:
        return getattr(self.store, name)

    def create(
        self, spec: object, *, lane_id: str, run_id: str
    ) -> OperationRecord:
        stored_spec = (
            replace(spec, parent_operation_id="")
            if spec.kind == "review-round"
            else spec
        )
        return self.store.create(
            stored_spec, lane_id=lane_id, run_id=run_id
        )


class RejectingRoundStore:
    """Force the compatibility path while retaining the original error."""

    def __init__(
        self,
        existing: OperationRecord | None,
        original: StoreError,
        *,
        read_error: StoreError | None = None,
    ) -> None:
        self.existing = existing
        self.original = original
        self.read_error = read_error
        self.create_calls = 0
        self.read_calls = 0

    def create(self, *_args: object, **_kwargs: object) -> OperationRecord:
        self.create_calls += 1
        raise self.original

    def read(self, *_args: object, **_kwargs: object) -> OperationRecord:
        self.read_calls += 1
        if self.read_error is not None:
            raise self.read_error
        assert self.existing is not None
        return self.existing


@dataclass(frozen=True)
class RecoveryFixture:
    vault: Path
    product: Path
    task_id: str
    store: OperationStore
    runtime: FakeRuntime
    gate: ReviewGateController
    parent_id: str
    child_id: str
    attention_path: Path
    exact_attention: dict[str, object]
    attention_pointer: bytes
    lane_round_ids: tuple[tuple[str, str, str], ...]


def write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
    )


def build_fixture(
    base: Path,
    *,
    gate_status: str = "attention-required",
    legacy_round_specs: bool = False,
    exact_attempt: bool = False,
    deep: bool = False,
    review_operation_id: str | None = None,
) -> RecoveryFixture:
    vault = base / "vault"
    product = base / "product"
    plan = vault / "wiki/plans/approved.md"
    (vault / "skills/review").mkdir(parents=True)
    (vault / "scripts").mkdir()
    (vault / "config").mkdir()
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "# Approved task\n\nExercise bounded review recovery.\n",
        encoding="utf-8",
    )
    (vault / "skills/review/SKILL.md").write_text(
        "# Review\n\nInspect the exact ContextPacket and product HEAD.\n",
        encoding="utf-8",
    )
    (vault / "config/verification-profiles.toml").write_bytes(
        (ROOT / "config/verification-profiles.toml").read_bytes()
    )
    product.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=product, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "review@example.invalid"],
        cwd=product,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Review Recovery Test"],
        cwd=product,
        check=True,
    )
    (product / "product.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.py"], cwd=product, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "fixture"],
        cwd=product,
        check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=product,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    task_id = str(uuid.uuid4())
    profile_sha = load_profiles(
        vault / "config/verification-profiles.toml"
    )["scoped"].sha256
    meta: dict[str, object] = {
        "version": 3,
        "project_id": str(uuid.uuid4()),
        "task_id": task_id,
        "task_name": "mechanism recovery",
        "executor_runtime": "codex",
        "origin_session": "session-1",
        "task_surface": "22222222-2222-4222-8222-222222222222",
        "worktree": str(product.resolve()),
        "vault_root": str(vault.resolve()),
        "plan_file": str(plan.resolve()),
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
                "source": "unit-test",
            }
        },
        "review_policy": {
            "mode": "deep" if deep else "simple",
            "cross_model": False,
            "runtime": "",
            "model": "",
            "effort": "",
            "max_verify_iterations": 2 if deep else 1,
            "verification_profile": "scoped",
            "verification_profile_sha256": profile_sha,
            "auto_resolve_severities": ["warning", "nit"],
            "escalate_severities": ["blocking"],
        },
        "reap_policy": {
            "mode": "final",
            "auto_file": True,
            "allowed_types": ["session"],
            "title": "mechanism recovery",
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
    write_json(product / ".task-meta.json", meta)
    runtime_root = _runtime_root(vault, task_id)
    context, _manifest = _context(
        meta, vault, product, runtime_root, task_id
    )
    store = OperationStore(vault / ".vault-meta/harness")
    runtime = FakeRuntime(store)
    gate_store = LegacyRoundStore(store) if legacy_round_specs else store
    gate = ReviewGateController(
        _gate_root(vault, task_id), runtime, gate_store
    )
    preset = ReviewPreset.from_flags(
        deep=deep,
        runtime="codex" if deep else "",
        model="sol" if deep else "",
        effort="xhigh" if deep else "",
    )
    request = ReviewOperationRequest(
        preset.request(
            review_operation_id or task_id,
            selected_provider="openai",
        ),
        task_id,
        RuntimeRoute(
            "codex",
            "gpt-5.6-sol",
            "high",
            "reviewer-callback",
            "f" * 64,
        ),
        context,
    )
    start = {
        "dispatch_operation_id": task_id,
        "request": request,
        "origin_surface": str(meta["task_surface"]),
        "cwd": runtime_root,
        "product_root": product,
        "prompt_pointer": "prompts/review.md",
        "callback_root": "callbacks",
    }
    run = (
        gate.begin_attempt(
            finalization_lineage_id=task_id,
            cycle=1,
            plan_sha256=str(meta["approved_plan_sha256"]),
            outcome_sha256="3" * 64,
            **start,
        )
        if exact_attempt
        else gate.begin(**start)
    )
    lane = run.execution.lanes[0]
    round_ = run.rounds[lane.axis]
    state_updates: dict[str, object] = {
        "status": gate_status,
        "final_results": {},
    }
    if gate_status == "awaiting-resolution":
        state_updates["resolution_evidence"] = {
            "openai-holistic:0": "persisted-resolution.json"
        }
        write_json(
            gate.root / "persisted-resolution.json",
            {"evidence": "historical-resolution"},
        )
    gate._replace(**state_updates)
    raised_attention = {
        "version": 1,
        "id": "mechanism-recovery-1",
        "status": "pending",
        "task_name": "mechanism recovery",
        "category": "mechanism-failure",
        "reason": "The repository-owned review mechanism failed",
        "question": "Authorize one bounded fresh review boundary?",
        "worktree": str(product.resolve()),
        "task_surface": str(meta["task_surface"]),
        "raised_at": "2026-08-04T12:00:00Z",
    }
    attention_path = product / ".task-needs-attention.json"
    append_raise(product, raised_attention)
    resolved_attention = append_resolution(
        product,
        "authorize-one-bounded-fresh-context-review-boundary-for-"
        f"{head[:7]}",
        resolved_at="2026-08-04T12:01:00Z",
    )
    return RecoveryFixture(
        vault,
        product,
        task_id,
        store,
        runtime,
        gate,
        lane.operation_id,
        round_.operation_id,
        attention_path,
        resolved_attention.payload,
        attention_path.read_bytes(),
        tuple(
            (lane.axis, lane.operation_id, run.rounds[lane.axis].operation_id)
            for lane in run.execution.lanes
        ),
    )


def terminalize(store: OperationStore, task_id: str, operation_id: str) -> None:
    record = store.read(task_id, operation_id)
    if record.state not in TERMINAL:
        store.transition(task_id, operation_id, "cancelling")
        store.transition(task_id, operation_id, "exiting")
        store.transition(task_id, operation_id, "cancelled")
    record = store.read(task_id, operation_id)
    if record.resources != OwnedResources():
        store.save(
            replace(
                record,
                resources=OwnedResources(),
                revision=record.revision + 1,
            ),
            expected_revision=record.revision,
        )


def replace_record(
    fixture: RecoveryFixture,
    operation_id: str,
    **updates: object,
) -> None:
    record = fixture.store.read(fixture.task_id, operation_id)
    fixture.store.save(
        replace(record, **updates, revision=record.revision + 1),
        expected_revision=record.revision,
    )


def append_mechanism_decision(
    fixture: RecoveryFixture,
    escalation_id: str,
    decision: str,
    *,
    task_surface: str | None = None,
) -> None:
    meta = json.loads(
        (fixture.product / ".task-meta.json").read_text(encoding="utf-8")
    )
    append_raise(
        fixture.product,
        {
            "version": 1,
            "id": escalation_id,
            "status": "pending",
            "task_name": "mechanism recovery",
            "category": "mechanism-failure",
            "reason": "The same exact callback ingestion boundary is paused",
            "question": "Authorize the bounded repository-owned repair?",
            "worktree": str(fixture.product.resolve()),
            "task_surface": task_surface or str(meta["task_surface"]),
            "raised_at": "2026-08-05T12:00:00Z",
        },
    )
    append_resolution(
        fixture.product,
        decision,
        resolved_at="2026-08-05T12:01:00Z",
    )


def accepted_callback_chain(
    fixture: RecoveryFixture,
    *,
    duplicate_anchor: bool = False,
    latest_surface: str | None = None,
) -> str:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=fixture.product,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    original = (
        "Classified as an eligible repository-owned callback-ingestion "
        "mechanism failure. Authorize one narrow reversible repair in the "
        "task worktree with a regression reproducing two exact accepted "
        "review callbacks stuck in verifying without .task-review.json. "
        "Preserve and ingest the existing callback identities and findings; "
        "do not relaunch reviewers, repeat provider/model/review effects, or "
        "hand-edit the canonical store. If the live root worker generation "
        "is stale, the harness may close and restart only that exact owned "
        "generation within its existing model-restart budget, then retry "
        f"callback ingestion once from clean HEAD {head}."
    )
    append_mechanism_decision(fixture, "accepted-callback-anchor", original)
    if duplicate_anchor:
        append_mechanism_decision(
            fixture, "accepted-callback-anchor-duplicate", original
        )
    ordering = (
        "Classified as the same eligible repository-owned callback-ingestion "
        "mechanism failure. Authorize a narrow regression-backed ordering "
        "repair and exactly one additional ingestion retry using the same "
        "two accepted callback identities."
    )
    append_mechanism_decision(fixture, "accepted-callback-ordering", ordering)
    chained = (
        "Classified as the same eligible repository-owned callback-ingestion "
        "authorization-chain mechanism failure. Authorize a narrow "
        "regression-backed repair that accepts the latest resolved "
        "same-failure escalation only when its exact previous chain reaches "
        f"the resolved authorization containing reviewed HEAD {head}, the "
        "review gate/attempt and two accepted callback identities are "
        "unchanged, every intervening record digest and previous pointer "
        "validates, and the current product HEAD is a clean descendant."
    )
    append_mechanism_decision(
        fixture,
        "accepted-callback-chain",
        chained,
        task_surface=latest_surface,
    )
    return head


@dataclass(frozen=True)
class DriftFixture:
    recovery: RecoveryFixture
    reviewed_head: str
    authorized_base_head: str
    accepted: dict[str, tuple[str, str]]
    artifacts: dict[str, tuple[str, str]]
    accepted_payloads: dict[str, dict[str, object]]


SIGNAL_FREE_DECISION = (
    "Classified as an eligible repository-owned stale OS-ownership drift "
    "mechanism failure. Authorize one narrow code-owned, signal-free recovery "
    "path with regression tests for an exact retained review parent. The path "
    "must send no OS/cmux/provider signal and preserve all archived evidence. "
    "It must reject any actually live exact match, any PID/PGID or supervisor "
    "reuse, ambiguous ownership, unrelated ownership, partial identity evidence, "
    "missing archive, archive tampering, or gate/progress drift. Only after "
    "read-only inventory proves no exact owned live resource may lifecycle APIs "
    "clear the stale projection. Preserve authorization for exactly one fresh "
    "single-model Codex/Sol deep review on the resulting exact clean HEAD."
)


def supported_close_decision(parents: tuple[str, str]) -> str:
    return (
        "Classified as an eligible repository-owned supported-close "
        "terminal-state compatibility mechanism failure. Authorize one narrow "
        "code-owned lifecycle repair with focused restart, crash-idempotency, "
        "zero-signal, and zero-callback/provider-replay regressions. The repair "
        "may consume only the exact retained parent identities "
        f"{parents[0]} and {parents[1]} when each is terminal cancelled, has "
        "empty OwnedResources, no pending effect, and a matching succeeded "
        "request-exit receipt produced by the supported harness close path. It "
        "must preserve the immutable archive and both callback identities, "
        "atomically project the corresponding retained round children "
        "terminal/resource-free, keep all signal and replay counters zero, and "
        "reject any live, ambiguous, partial, non-terminal, receipt-mismatched, "
        "archive-drifted, or unrelated state. After focused and configured gates "
        "pass on a new clean descendant HEAD, resume the existing "
        "drift-quarantine progress and the previously authorized exactly one "
        "fresh single-model Codex/Sol deep review."
    )


def fresh_operation_binding_decision(
    review_operation_id: str, dispatch_operation_id: str
) -> str:
    return (
        "Classified as an eligible repository-owned fresh-boundary operation-ID "
        "binding mechanism failure. Authorize one narrow local reversible TDD "
        "repair that binds fresh-boundary authorization to the active review "
        f"operation_id {review_operation_id} while preserving the dispatch "
        f"operation_id {dispatch_operation_id} only as provenance. Add focused "
        "crash, restart, idempotency, exact-identity, zero-signal, and "
        "zero-callback/provider-replay regressions. Preserve the completed "
        "quarantine archive, parent cleanup receipts, and terminal child-round "
        "receipts exactly; do not repeat cleanup, ingest or reinterpret old "
        "callbacks, manually edit gate/store state, or launch more than the one "
        "previously authorized fresh Codex/Sol review."
    )


POST_VERIFICATION_DECISION = (
    "Classified as an eligible repository-owned post-verification review-drive "
    "mechanism failure. Authorize read-only diagnosis of the exact "
    "review-drive-failed boundary and one narrow local reversible TDD repair "
    "limited to crash/restart synchronization between the completed "
    "scoped-verification receipt, the pending fresh-review marker, and the exact "
    "tracked live dispatch provider. Preserve the completed quarantine archive, "
    "terminal retained-parent and retained-round receipts, scoped-verification "
    "receipt, and existing provider ownership exactly. Add focused "
    "exact-identity, live-ownership, crash, restart, idempotency, "
    "zero-callback-replay, zero-provider-replay, and at-most-one-fresh-review "
    "regressions. Do not manually edit gate/store state, signal or close the "
    "exact live provider, repeat cleanup or verification, ingest old callbacks, "
    "relaunch the dispatch provider, or launch more than the one previously "
    "authorized fresh Codex/Sol review. After the repair and relevant configured "
    "gates are green on a clean descendant, resume once through the supported "
    "facade so the existing tracked provider continues from the completed "
    "verification boundary; fail closed and re-escalate before any effect if "
    "exact identity or ownership cannot be proven."
)


EXACT_LIVE_STATUS_DECISION = (
    "Classified as an eligible repository-owned exact-live ownership "
    "status-adapter mechanism failure. The absent-owner replacement path is "
    "prohibited for this boundary. Authorize one narrow local reversible TDD "
    "repair to the read-only Darwin process/pid status adapter: when "
    "zero-signal liveness probing returns EPERM/unknown, report alive only if "
    "libproc proves the exact persisted PID/PGID identity unchanged, "
    "proc_bsdinfo reports a running non-zombie process, the provider parent PID "
    "is the exact persisted supervisor PID, and both identities match the "
    "existing ownership receipt. Preserve current behavior on non-Darwin "
    "platforms and for ordinary successful zero-signal probes. Add focused "
    "EPERM+exact-live, PID/PGID reuse, wrong parent, stopped/zombie/exited/absent, "
    "permission-without-libproc-proof, and no-signal regressions; no signal may "
    "be sent and no lifecycle/store/gate/provider/callback/review effect may "
    "occur during diagnosis or tests. Run relevant configured harness, "
    "coverage, quality, and exact-HEAD release gates and commit a clean "
    "descendant. Then, if the same exact provider and supervisor still prove "
    "alive and all previous continuation/quarantine/verification receipts "
    "remain unchanged, authorize exactly one new supported post-verification "
    "recovery attempt on that same live dispatch executor toward the one "
    "previously authorized fresh Codex/Sol review. Do not create a replacement "
    "generation, restart or relaunch any provider/reviewer, repeat verification/"
    "cleanup, ingest old callbacks, manually edit state, or touch the surface. "
    "Fail closed and re-escalate before any effect on identity/status drift."
)


POST_FRESH_PUBLICATION_DECISION = (
    "Classified as an eligible repository-owned post-fresh-publication "
    "synchronization mechanism failure. Authorize one narrow regression-backed, "
    "crash-idempotent repair that validates the prepared continuation receipt, "
    "exact fresh gate identity, fresh_reevaluation_used=true, and the "
    "already-created Codex/Sol parent/round identities with succeeded "
    "start-provider effects; then atomically complete only the missing gate/"
    "progress/final-marker synchronization and resume callback waiting for "
    "those existing fresh lanes. Preserve quarantine/archive/retained receipts "
    "and all existing provider effects. Do not retry the consumed resume, "
    "relaunch or replace any reviewer/provider, repeat verification, replay "
    "callbacks/provider effects, signal processes, touch cmux surfaces, manually "
    "edit gate/store, or start more than the already-launched fresh review. Fail "
    "closed on any identity, receipt, resource, or effect drift."
)


FRESH_CHILD_PROGRESS_DECISION = (
    "Classified as an eligible repository-owned fresh child-round "
    "lifecycle-progress synchronization failure. Authorize one narrow "
    "regression-backed repair: accept only the same exact fresh child-round "
    "identities after monotonic advancement from awaiting-callback into "
    "verifying, finalizing, or terminal resource-free completion, while "
    "validating their persisted callback/provider/effect chain, unchanged "
    "parent identities and succeeded start-provider effects, zero replay "
    "counters, and no state regression. Preserve the prepared continuation, "
    "quarantine archive, and all receipts. After a clean exact-HEAD gate, "
    "authorize exactly one additional supported reconcile attempt. Do not "
    "relaunch or replace reviewers/providers, replay callbacks or effects, "
    "signal processes, touch cmux manually, edit gate/store by hand, or create "
    "any additional fresh lane."
)


AUTHORIZATION_COMPATIBILITY_DECISION = (
    "Classified as an eligible repository-owned typed authorization-compiler "
    "compatibility failure. Authorize one narrow regression-backed compiler "
    "repair that accepts only the exact latest fresh child-round lifecycle-"
    "progress resolution, proves and preserves its complete chain to the prior "
    "post-fresh-publication authorization and the still-unused one-additional-"
    "reconcile grant, and rejects missing, reordered, ambiguous, or broadened "
    "decisions. After clean relevant and exact-HEAD gates, permit exactly the "
    "still-unused single supported reconcile. No reviewer/provider relaunch or "
    "replacement, callback/effect replay, signals, cmux/manual gate-store "
    "mutation, additional fresh lane, push, publish, tag, release, or reap."
)


TIMING_GATE_DECISION = (
    "Classified as an eligible repository-owned Stop-hook timing-gate "
    "measurement failure. Authorize one narrow regression-backed local repair "
    "of the benchmark/test mechanism that keeps the 1.000s product SLO, uses "
    "deterministic warm-up and monotonic/jitter-resistant sampling to "
    "distinguish scheduler noise from sustained hook regression, and proves an "
    "injected real delay still fails. Do not merely raise the threshold or "
    "weaken the production contract. After clean focused/full/coverage/quality "
    "and fresh exact-HEAD release-final gates, permit the still-unused single "
    "supported reconcile. Preserve all prior evidence and prohibitions; no "
    "reviewer/provider relaunch, callback/effect replay, signals, cmux/manual "
    "store edits, push, publish, tag, release, or reap."
)


AUTHORIZATION_CHAIN_DECISION = (
    "Classified as an eligible repository-owned authorization-chain compiler "
    "compatibility failure. Authorize one narrow regression-backed exact-chain "
    "extension that accepts only the immediate immutable sequence resolution-"
    "4331f351b941ba742fa44496cd53f8d8 -> resolution-"
    "439263b4acf7db64fbba42861eaa7b08 -> raise "
    "2f0718a4-fe30-4f97-97a6-1c5faa3fccd6 -> this resolution, proving "
    "every record digest, predecessor link, unchanged gate identity, clean "
    "descendant ancestry, the already-passed release-final receipt "
    "26500000-0000-4000-8000-000005afd184, and the literal still-unused "
    "single-reconcile grant. Reject missing, reordered, duplicated, ambiguous, "
    "broadened, or unrelated records. Add focused negative/positive "
    "regressions, then run clean full/coverage/quality and a fresh exact-HEAD "
    "release-final profile before consuming exactly one supported reconcile. "
    "Preserve all historical evidence and prior prohibitions: no reviewer/"
    "provider relaunch, callback/effect replay, signals, cmux or manual store "
    "edits, push, publish, tag, release, or reap. Escalate on any further "
    "identity, ownership, or lifecycle drift."
)


def absent_ownership(index: int) -> DurableCleanupOwnership:
    return DurableCleanupOwnership(
        "dead",
        "dead",
        "missing",
        "missing",
        f"workspace-{index}",
        f"window-{index}",
    )


def prepare_signal_free_fixture(
    base: Path, *, review_operation_id: str | None = None
) -> DriftFixture:
    drift = prepare_drift_fixture(
        base, review_operation_id=review_operation_id
    )
    fixture = drift.recovery
    parents = [parent for _axis, parent, _child in fixture.lane_round_ids]
    for index, operation_id in enumerate(parents, start=1):
        record = fixture.store.read(fixture.task_id, operation_id)
        replace_record(
            fixture,
            operation_id,
            resources=OwnedResources(
                surface_id=record.resources.surface_id,
                process_group=4100 + index,
                supervisor_pid=5100 + index,
                process_identity=hashlib.sha256(
                    f"process-{index}".encode()
                ).hexdigest(),
                supervisor_identity=hashlib.sha256(
                    f"supervisor-{index}".encode()
                ).hexdigest(),
            ),
        )
        fixture.runtime.cleanup_ownership[operation_id] = absent_ownership(index)
    fixture.runtime.fail_cleanup_for.add(parents[0])
    expect_error(
        "signal-free fixture preserves prepared archive after old cleanup failure",
        lambda: recover_task_review_for_mechanism(
            fixture.product, runtime_manager=fixture.runtime
        ),
        "resource cleanup is incomplete",
    )
    fixture.runtime.fail_cleanup_for.clear()
    append_mechanism_decision(
        fixture,
        "signal-free-stale-ownership",
        SIGNAL_FREE_DECISION,
    )
    return drift


def apply_supported_close_receipt(
    fixture: RecoveryFixture, operation_id: str
) -> None:
    record = fixture.store.read(fixture.task_id, operation_id)
    if record.state not in {"finalizing", "cancelling"}:
        fixture.store.transition(fixture.task_id, operation_id, "cancelling")
    fixture.store.begin_effect(
        fixture.task_id, operation_id, "request-exit"
    )
    fixture.store.resolve_effect(
        fixture.task_id, operation_id, EffectOutcome.SUCCEEDED
    )
    fixture.store.transition(fixture.task_id, operation_id, "exiting")
    record = fixture.store.read(fixture.task_id, operation_id)
    fixture.store.save(
        replace(
            record,
            resources=OwnedResources(),
            revision=record.revision + 1,
        ),
        expected_revision=record.revision,
    )
    fixture.store.transition(fixture.task_id, operation_id, "cancelled")


def prepare_supported_close_fixture(base: Path) -> DriftFixture:
    drift = prepare_signal_free_fixture(
        base, review_operation_id=str(uuid.uuid4())
    )
    fixture = drift.recovery
    meta = json.loads(
        (fixture.product / ".task-meta.json").read_text(encoding="utf-8")
    )
    append_raise(
        fixture.product,
        {
            "version": 1,
            "id": "diagnostic-external-scope",
            "status": "pending",
            "task_name": "mechanism recovery",
            "category": "external-effect",
            "reason": "A diagnostic-only ownership boundary was classified",
            "question": "Confirm that no provider effect was authorized",
            "worktree": str(fixture.product.resolve()),
            "task_surface": str(meta["task_surface"]),
            "raised_at": "2026-08-05T12:02:00Z",
        },
    )
    append_resolution(
        fixture.product,
        "The diagnostic boundary authorized no retained-review mutation.",
        resolved_at="2026-08-05T12:03:00Z",
    )
    append_mechanism_decision(
        fixture,
        "signal-free-after-scope-boundary",
        SIGNAL_FREE_DECISION,
    )
    parents = tuple(
        parent for _axis, parent, _child in fixture.lane_round_ids
    )
    assert len(parents) == 2
    for operation_id in parents:
        apply_supported_close_receipt(fixture, operation_id)
    append_mechanism_decision(
        fixture,
        "supported-close-terminal-state",
        supported_close_decision(parents),
    )
    return drift


def prepare_drift_fixture(
    base: Path, *, review_operation_id: str | None = None
) -> DriftFixture:
    fixture = build_fixture(
        base,
        gate_status="reviewing",
        exact_attempt=True,
        deep=True,
        review_operation_id=review_operation_id,
    )
    run = fixture.gate.rehydrate_attempt()
    reviewed_head = run.execution.request.context.head_sha
    accepted: dict[str, tuple[str, str]] = {}
    artifacts: dict[str, tuple[str, str]] = {}
    accepted_payloads: dict[str, dict[str, object]] = {}
    runtime_root = _runtime_root(fixture.vault, fixture.task_id)
    for lane in run.execution.lanes:
        parent = fixture.store.read(fixture.task_id, lane.operation_id)
        for state in ("preflight", "starting", "running", "awaiting-callback"):
            if parent.state == state:
                continue
            fixture.store.transition(fixture.task_id, lane.operation_id, state)
            parent = fixture.store.read(fixture.task_id, lane.operation_id)
        round_ = run.rounds[lane.axis]
        accepted_result = ReviewResult(
            lane.axis,
            "changes-requested",
            (
                ReviewFinding(
                    f"{lane.axis}-accepted",
                    lane.axis,
                    "important",
                    "accepted retained finding",
                    "verify the clean descendant",
                ),
            ),
            0,
        )
        accepted_envelope = review_round_envelope(round_, accepted_result)
        CallbackBroker(fixture.store, fixture.task_id).accept(accepted_envelope)
        accepted[lane.axis] = (
            accepted_envelope.callback_id,
            accepted_envelope.payload_sha256,
        )
        accepted_payloads[lane.axis] = to_dict(accepted_envelope)
        receipt_path = (
            fixture.store.root
            / "owners"
            / fixture.task_id
            / "runtime"
            / lane.operation_id
            / "callback-receipt.json"
        )
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(
            receipt_path,
            {
                "schema_version": 1,
                "status": "accepted",
                "operation_id": round_.operation_id,
                "run_id": round_.run_id,
                "callback_id": accepted_envelope.callback_id,
                "payload_sha256": accepted_envelope.payload_sha256,
                "generation": 2,
            },
        )
        artifact_envelope = accepted_envelope
        if lane.axis.endswith("engineering"):
            artifact_envelope = review_round_envelope(
                round_,
                ReviewResult(
                    lane.axis,
                    "changes-requested",
                    (
                        ReviewFinding(
                            f"{lane.axis}-artifact",
                            lane.axis,
                            "important",
                            "later retained finding",
                            "do not reinterpret this artifact",
                        ),
                    ),
                    0,
                ),
            )
        callback_path = runtime_root / "callbacks" / lane.axis / ".review-callback.json"
        callback_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(callback_path, to_dict(artifact_envelope))
        artifacts[lane.axis] = (
            artifact_envelope.callback_id,
            artifact_envelope.payload_sha256,
        )

    (fixture.product / "product.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.py"], cwd=fixture.product, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "clean descendant"],
        cwd=fixture.product,
        check=True,
    )
    authorized_base_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=fixture.product,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    engineering = next(
        axis for axis in accepted if axis.endswith("engineering")
    )
    accepted_id, accepted_sha = accepted[engineering]
    artifact_id, artifact_sha = artifacts[engineering]
    append_mechanism_decision(
        fixture,
        "drift-quarantine-anchor",
        (
            "Classified as an eligible repository-owned stale-review-attempt "
            "mechanism failure. Authorize one code-owned fail-closed retirement "
            f"and quarantine of the complete retained {reviewed_head[:7]} review "
            "attempt without ingesting, reconstructing, rebinding, replaying, or "
            "reinterpreting either engineering callback. Preserve as immutable "
            f"evidence both the accepted receipt identity {accepted_id} with "
            f"payload digest {accepted_sha} and the mismatching callback-file "
            f"identity {artifact_id} with payload digest {artifact_sha}, together "
            "with the matching intent identity and all original receipts."
        ),
    )
    append_mechanism_decision(
        fixture,
        "drift-quarantine-patch",
        (
            "Choose boundary A. Classified as an eligible repository-owned "
            "stale-review quarantine mechanism gap. Authorize one narrow "
            "code-owned mechanism patch with regression tests that adds a typed, "
            "fail-closed drift-evidence quarantine transition, and no callback may "
            "be ingested, reconstructed, rebound, replayed, or reinterpreted. It "
            "must reject matching callbacks, ambiguous identity, live unrelated "
            "ownership, missing receipts, partial cleanup, or any attempt to reuse "
            "old effects. Commit the patch and tests to a new clean product HEAD "
            "descended from "
            f"{authorized_base_head}, then launch exactly one fresh single-model "
            "Codex/Sol deep review."
        ),
    )
    return DriftFixture(
        fixture,
        reviewed_head,
        authorized_base_head,
        accepted,
        artifacts,
        accepted_payloads,
    )


def check_legacy_round_rejection(
    label: str,
    *,
    requested_spec: object,
    existing: OperationRecord | None,
    lane_id: str,
    run_id: str,
    read_error: StoreError | None = None,
) -> None:
    original = StoreError(f"original {label}")
    store = RejectingRoundStore(existing, original, read_error=read_error)
    adapter = RecoveryRoundStore(store)  # type: ignore[arg-type]
    try:
        adapter.create(requested_spec, lane_id=lane_id, run_id=run_id)
    except StoreError as exc:
        check(
            f"legacy round adapter preserves original error for {label}",
            exc is original and store.create_calls == 1 and store.read_calls == 1,
        )
    else:
        raise AssertionError(f"legacy round adapter accepted {label}")


with tempfile.TemporaryDirectory(prefix="accepted-callback-chain.") as raw:
    fixture = build_fixture(Path(raw))
    reviewed_head = accepted_callback_chain(fixture)
    latest = load_chain(fixture.product)[-1]
    check(
        "same-failure authorization follows one exact validated chain",
        _authorized_accepted_callback_head(
            latest.payload, fixture.product.resolve()
        )
        == reviewed_head,
    )

    chain = load_chain(fixture.product)
    anchor = next(
        record
        for record in chain
        if str(record.payload.get("decision") or "").startswith(
            "Classified as an eligible repository-owned callback-ingestion"
        )
    )
    check(
        "literal-SHA accepted-callback authorization remains valid",
        _authorized_accepted_callback_head(
            anchor.payload, fixture.product.resolve()
        )
        == reviewed_head,
    )
    branch = json.loads(chain[-2].path.read_text(encoding="utf-8"))
    branch["record_id"] = "accepted-callback-branch"
    branch["payload"]["id"] = "accepted-callback-branch"
    branch["previous"] = {
        "record_id": anchor.record_id,
        "record_sha256": anchor.sha256,
    }
    write_json(
        fixture.product
        / ".task-escalation-records/accepted-callback-branch.json",
        branch,
    )
    check(
        "same-failure authorization rejects a branched predecessor",
        not _authorized_accepted_callback_head(
            latest.payload, fixture.product.resolve()
        ),
    )


with tempfile.TemporaryDirectory(
    prefix="accepted-callback-chain-missing."
) as raw:
    fixture = build_fixture(Path(raw))
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=fixture.product,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    append_mechanism_decision(
        fixture,
        "accepted-callback-chain-only",
        (
            "Classified as the same eligible repository-owned "
            "callback-ingestion authorization-chain mechanism failure. "
            "Authorize the exact previous authorization containing reviewed "
            f"HEAD {head}."
        ),
    )
    latest = load_chain(fixture.product)[-1]
    check(
        "same-failure authorization rejects a missing original grant",
        not _authorized_accepted_callback_head(
            latest.payload, fixture.product.resolve()
        ),
    )


with tempfile.TemporaryDirectory(
    prefix="accepted-callback-chain-ambiguous."
) as raw:
    fixture = build_fixture(Path(raw))
    accepted_callback_chain(fixture, duplicate_anchor=True)
    latest = load_chain(fixture.product)[-1]
    check(
        "same-failure authorization rejects ambiguous original grants",
        not _authorized_accepted_callback_head(
            latest.payload, fixture.product.resolve()
        ),
    )


with tempfile.TemporaryDirectory(
    prefix="accepted-callback-chain-scope."
) as raw:
    fixture = build_fixture(Path(raw))
    accepted_callback_chain(
        fixture,
        latest_surface="99999999-9999-4999-8999-999999999999",
    )
    latest = load_chain(fixture.product)[-1]
    check(
        "same-failure authorization rejects scope drift",
        not _authorized_accepted_callback_head(
            latest.payload, fixture.product.resolve()
        ),
    )


with tempfile.TemporaryDirectory(prefix="drift-quarantine-lifecycle.") as raw:
    drift = prepare_drift_fixture(Path(raw))
    fixture = drift.recovery
    started_before = len(fixture.runtime.started)
    recovered = recover_task_review_for_mechanism(
        fixture.product,
        runtime_manager=fixture.runtime,
    )
    state = fixture.gate.read()
    quarantine = state["drift_quarantine"]
    evidence_path = fixture.gate.root / quarantine["evidence_pointer"]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    old_records = [
        fixture.store.read(fixture.task_id, operation_id)
        for _axis, parent_id, child_id in fixture.lane_round_ids
        for operation_id in (parent_id, child_id)
    ]
    check(
        "drift quarantine lifecycle closes exact old ownership before one fresh review",
        recovered["status"] == "reviewing"
        and state["fresh_reevaluation_used"] is True
        and state["policy"]["max_verify_iterations"] == 0
        and state["attempt"]["status"] == "terminal"
        and state["attempt"]["terminal"]["result"] == "attention-required"
        and all(record.state == "complete" for record in old_records)
        and all(record.resources == OwnedResources() for record in old_records)
        and set(fixture.runtime.exit_requests)
        == {parent for _axis, parent, _child in fixture.lane_round_ids}
        and len(fixture.runtime.started) == started_before + 2,
    )
    relations = {
        row["axis"]: row["identity_relation"] for row in evidence["lanes"]
    }
    check(
        "drift quarantine preserves one mismatch and matching intent as immutable bytes",
        sorted(relations.values()) == ["drift", "match"]
        and relations["openai-engineering"] == "drift"
        and all(
            (fixture.gate.root / row["accepted_receipt_pointer"]).is_file()
            and (fixture.gate.root / row["callback_artifact_pointer"]).is_file()
            for row in evidence["lanes"]
        ),
    )
    started_after = len(fixture.runtime.started)
    exits_after = tuple(fixture.runtime.exit_requests)
    evidence_before = evidence_path.read_bytes()
    replay = recover_task_review_for_mechanism(
        fixture.product,
        runtime_manager=fixture.runtime,
    )
    check(
        "drift quarantine replay is idempotent with zero provider or callback replay",
        replay["status"] == "reviewing"
        and len(fixture.runtime.started) == started_after
        and tuple(fixture.runtime.exit_requests) == exits_after
        and evidence_path.read_bytes() == evidence_before,
    )


with tempfile.TemporaryDirectory(prefix="drift-quarantine-crash.") as raw:
    drift = prepare_drift_fixture(Path(raw))
    fixture = drift.recovery
    crash_events: list[str] = []

    def crash_after_first_cleanup(event: str) -> None:
        crash_events.append(event)
        if event.startswith("drift-quarantine-parent-cleaned:"):
            raise RuntimeError("simulated quarantine crash")

    try:
        recover_task_review_for_mechanism(
            fixture.product,
            runtime_manager=fixture.runtime,
            quarantine_fault_observer=crash_after_first_cleanup,
        )
    except RuntimeError as exc:
        check(
            "drift quarantine crash occurs only after one durable exact cleanup",
            str(exc) == "simulated quarantine crash"
            and len(fixture.runtime.exit_requests) == 1
            and len(fixture.runtime.started) == 2,
        )
    else:
        raise AssertionError("drift quarantine crash failpoint did not fire")
    recovered = recover_task_review_for_mechanism(
        fixture.product,
        runtime_manager=fixture.runtime,
    )
    check(
        "drift quarantine restart converges without repeating cleaned ownership",
        recovered["status"] == "reviewing"
        and len(fixture.runtime.exit_requests) == 2
        and len(set(fixture.runtime.exit_requests)) == 2
        and len(fixture.runtime.started) == 4,
    )


with tempfile.TemporaryDirectory(prefix="drift-quarantine-head-drift.") as raw:
    drift = prepare_drift_fixture(Path(raw))
    fixture = drift.recovery

    def crash_after_archive(event: str) -> None:
        if event == "drift-quarantine-prepared":
            raise RuntimeError("simulated quarantine archive crash")

    try:
        recover_task_review_for_mechanism(
            fixture.product,
            runtime_manager=fixture.runtime,
            quarantine_fault_observer=crash_after_archive,
        )
    except RuntimeError as exc:
        check(
            "drift quarantine archives evidence before its first cleanup effect",
            str(exc) == "simulated quarantine archive crash"
            and not fixture.runtime.exit_requests,
        )
    else:
        raise AssertionError("drift quarantine archive failpoint did not fire")
    (fixture.product / "product.py").write_text("VALUE = 3\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.py"], cwd=fixture.product, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "unexpected later descendant"],
        cwd=fixture.product,
        check=True,
    )
    expect_error(
        "drift quarantine restart rejects replacement HEAD drift",
        lambda: recover_task_review_for_mechanism(
            fixture.product,
            runtime_manager=fixture.runtime,
        ),
        "replacement HEAD changed",
    )
    check(
        "replacement HEAD drift rejection has zero cleanup or provider effect",
        not fixture.runtime.exit_requests and len(fixture.runtime.started) == 2,
    )


with tempfile.TemporaryDirectory(prefix="drift-quarantine-tamper.") as raw:
    drift = prepare_drift_fixture(Path(raw))
    fixture = drift.recovery
    try:
        recover_task_review_for_mechanism(
            fixture.product,
            runtime_manager=fixture.runtime,
            quarantine_fault_observer=crash_after_archive,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("drift quarantine archive failpoint did not fire")
    authorization = authorized_drift_quarantine(
        load_chain(fixture.product)[-1], fixture.product.resolve()
    )
    assert authorization is not None
    evidence_path = (
        fixture.gate.root
        / "drift-quarantine"
        / authorization.authorization_record_id
        / "evidence.json"
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    drift_row = next(
        row for row in evidence["lanes"] if row["identity_relation"] == "drift"
    )
    drift_row["accepted_callback_id"] = "review-tampered-identity"
    write_json(evidence_path, evidence)
    expect_error(
        "drift quarantine restart rejects tampered archived identities",
        lambda: recover_task_review_for_mechanism(
            fixture.product,
            runtime_manager=fixture.runtime,
        ),
        "evidence identity changed",
    )
    check(
        "archive tamper rejection precedes cleanup and provider effects",
        not fixture.runtime.exit_requests and len(fixture.runtime.started) == 2,
    )


with tempfile.TemporaryDirectory(prefix="drift-quarantine-matching.") as raw:
    drift = prepare_drift_fixture(Path(raw))
    fixture = drift.recovery
    engineering = "openai-engineering"
    write_json(
        _runtime_root(fixture.vault, fixture.task_id)
        / "callbacks"
        / engineering
        / ".review-callback.json",
        drift.accepted_payloads[engineering],
    )
    expect_error(
        "drift quarantine rejects matching retained callbacks",
        lambda: recover_task_review_for_mechanism(
            fixture.product,
            runtime_manager=fixture.runtime,
        ),
        "requires one exact callback identity drift",
    )
    check(
        "matching callback rejection has zero evidence cleanup or provider effect",
        not (fixture.gate.root / "drift-quarantine").exists()
        and not fixture.runtime.exit_requests
        and len(fixture.runtime.started) == 2,
    )


with tempfile.TemporaryDirectory(prefix="drift-quarantine-receipt.") as raw:
    drift = prepare_drift_fixture(Path(raw))
    fixture = drift.recovery
    engineering_parent = next(
        parent
        for axis, parent, _child in fixture.lane_round_ids
        if axis == "openai-engineering"
    )
    receipt = (
        fixture.store.root
        / "owners"
        / fixture.task_id
        / "runtime"
        / engineering_parent
        / "callback-receipt.json"
    )
    receipt.unlink()
    expect_error(
        "drift quarantine rejects a missing accepted receipt",
        lambda: recover_task_review_for_mechanism(
            fixture.product,
            runtime_manager=fixture.runtime,
        ),
        "accepted callback receipt is unavailable",
    )
    check(
        "missing receipt rejection has zero evidence cleanup or provider effect",
        not (fixture.gate.root / "drift-quarantine").exists()
        and not fixture.runtime.exit_requests
        and len(fixture.runtime.started) == 2,
    )


with tempfile.TemporaryDirectory(prefix="drift-quarantine-unrelated.") as raw:
    drift = prepare_drift_fixture(Path(raw))
    fixture = drift.recovery
    parent = fixture.store.read(
        fixture.task_id, fixture.lane_round_ids[0][1]
    )
    fixture.store.create(
        replace(
            parent.spec,
            operation_id="unrelated-review-owner",
            idempotency_key="unrelated-review-owner-key",
        ),
        lane_id="unrelated-lane",
        run_id="unrelated-run",
    )
    expect_error(
        "drift quarantine rejects live unrelated ownership",
        lambda: recover_task_review_for_mechanism(
            fixture.product,
            runtime_manager=fixture.runtime,
        ),
        "unrelated live ownership",
    )
    check(
        "unrelated ownership rejection has zero evidence cleanup or provider effect",
        not (fixture.gate.root / "drift-quarantine").exists()
        and not fixture.runtime.exit_requests
        and len(fixture.runtime.started) == 2,
    )


with tempfile.TemporaryDirectory(prefix="drift-quarantine-partial.") as raw:
    drift = prepare_drift_fixture(Path(raw))
    fixture = drift.recovery
    blocked_parent = next(
        parent
        for axis, parent, _child in fixture.lane_round_ids
        if axis == "openai-engineering"
    )
    fixture.runtime.fail_cleanup_for.add(blocked_parent)
    expect_error(
        "drift quarantine fails closed on partial resource cleanup",
        lambda: recover_task_review_for_mechanism(
            fixture.product,
            runtime_manager=fixture.runtime,
        ),
        "resource cleanup is incomplete",
    )
    check(
        "partial cleanup never starts the fresh provider review",
        len(fixture.runtime.started) == 2
        and fixture.gate.read().get("fresh_reevaluation_used") is not True,
    )


with tempfile.TemporaryDirectory(prefix="signal-free-lifecycle.") as raw:
    drift = prepare_signal_free_fixture(Path(raw))
    fixture = drift.recovery
    latest = load_chain(fixture.product)[-1]
    authorization = authorized_signal_free_retirement(
        latest, fixture.product.resolve()
    )
    assert authorization is not None
    archive = (
        fixture.gate.root
        / "drift-quarantine"
        / authorization.drift.authorization_record_id
        / "evidence.json"
    )
    archive_before = archive.read_bytes()
    exits_before = tuple(fixture.runtime.exit_requests)
    cleanup_before = tuple(fixture.runtime.cleanup_calls)
    started_before = len(fixture.runtime.started)
    accepted_before = {
        child: (
            fixture.store.read(fixture.task_id, child).accepted_callback_id,
            fixture.store.read(fixture.task_id, child).accepted_callback_sha256,
        )
        for _axis, _parent, child in fixture.lane_round_ids
    }
    recovered = recover_task_review_for_mechanism(
        fixture.product, runtime_manager=fixture.runtime
    )
    state = fixture.gate.read()
    progress = json.loads(
        (
            archive.parent / "progress.json"
        ).read_text(encoding="utf-8")
    )
    old_records = [
        fixture.store.read(fixture.task_id, operation_id)
        for _axis, parent, child in fixture.lane_round_ids
        for operation_id in (parent, child)
    ]
    check(
        "signal-free lifecycle atomically retires proven-absent parents and starts one fresh boundary",
        recovered["status"] == "reviewing"
        and state["fresh_reevaluation_used"] is True
        and state["policy"]["max_verify_iterations"] == 0
        and all(record.state == "complete" for record in old_records)
        and all(record.resources == OwnedResources() for record in old_records)
        and len(fixture.runtime.started) == started_before + 2
        and progress["schema_version"] == 2
        and progress["status"] == "fresh-review-started"
        and len(progress["retirement_receipts"]) == 2,
    )
    accepted_after = {
        child: (
            fixture.store.read(fixture.task_id, child).accepted_callback_id,
            fixture.store.read(fixture.task_id, child).accepted_callback_sha256,
        )
        for _axis, _parent, child in fixture.lane_round_ids
    }
    check(
        "signal-free lifecycle preserves archive and has zero signal or callback replay",
        archive.read_bytes() == archive_before
        and tuple(fixture.runtime.exit_requests) == exits_before
        and tuple(fixture.runtime.cleanup_calls) == cleanup_before
        and accepted_after == accepted_before
        and all(
            fixture.runtime.ownership_probes.count(parent) == 2
            for _axis, parent, _child in fixture.lane_round_ids
        ),
    )
    probes_after = tuple(fixture.runtime.ownership_probes)
    started_after = len(fixture.runtime.started)
    replay = recover_task_review_for_mechanism(
        fixture.product, runtime_manager=fixture.runtime
    )
    check(
        "signal-free recovery replay is idempotent with zero provider replay",
        replay["status"] == "reviewing"
        and len(fixture.runtime.started) == started_after
        and tuple(fixture.runtime.ownership_probes) == probes_after
        and tuple(fixture.runtime.exit_requests) == exits_before,
    )


with tempfile.TemporaryDirectory(prefix="supported-close-lifecycle.") as raw:
    drift = prepare_supported_close_fixture(Path(raw))
    fixture = drift.recovery
    latest = load_chain(fixture.product)[-1]
    authorization = authorized_supported_close_retirement(
        latest, fixture.product.resolve()
    )
    assert authorization is not None
    archive = (
        fixture.gate.root
        / "drift-quarantine"
        / authorization.signal_free.drift.authorization_record_id
        / "evidence.json"
    )
    archive_before = archive.read_bytes()
    effects_before = (
        tuple(fixture.runtime.exit_requests),
        tuple(fixture.runtime.cleanup_calls),
        tuple(fixture.runtime.ownership_probes),
    )
    retained_review_operation_id = fixture.gate.read()[
        "active_review_operation_id"
    ]
    started_before = len(fixture.runtime.started)
    accepted_before = {
        child: (
            fixture.store.read(fixture.task_id, child).accepted_callback_id,
            fixture.store.read(fixture.task_id, child).accepted_callback_sha256,
        )
        for _axis, _parent, child in fixture.lane_round_ids
    }
    recovered = recover_task_review_for_mechanism(
        fixture.product, runtime_manager=fixture.runtime
    )
    gate_state = fixture.gate.read()
    authorization_identity = gate_state["fresh_boundary_authorization"]
    assert isinstance(authorization_identity, dict)
    boundary_authorization = json.loads(
        (fixture.gate.root / str(authorization_identity["pointer"])).read_text(
            encoding="utf-8"
        )
    )
    progress = json.loads(
        (archive.parent / "progress.json").read_text(encoding="utf-8")
    )
    parents = [
        fixture.store.read(fixture.task_id, parent)
        for _axis, parent, _child in fixture.lane_round_ids
    ]
    children = [
        fixture.store.read(fixture.task_id, child)
        for _axis, _parent, child in fixture.lane_round_ids
    ]
    check(
        "supported-close recovery consumes only exact cancelled resource-free parents",
        recovered["status"] == "reviewing"
        and all(parent.state == "cancelled" for parent in parents)
        and all(parent.resources == OwnedResources() for parent in parents)
        and all(parent.pending_effect == "" for parent in parents)
        and all(parent.effect_id == "request-exit" for parent in parents)
        and all(
            parent.effect_outcome == EffectOutcome.SUCCEEDED
            for parent in parents
        )
        and all(child.state == "complete" for child in children)
        and all(child.resources == OwnedResources() for child in children)
        and progress["schema_version"] == 2
        and progress["status"] == "fresh-review-started"
        and set(progress["retirement_receipts"])
        == set(authorization.parent_operation_ids)
        and len(fixture.runtime.started) == started_before + 2
        and retained_review_operation_id != fixture.task_id
        and boundary_authorization["schema_version"] == 2
        and boundary_authorization["operation_id"]
        == retained_review_operation_id
        and boundary_authorization["dispatch_operation_id"] == fixture.task_id,
    )
    accepted_after = {
        child: (
            fixture.store.read(fixture.task_id, child).accepted_callback_id,
            fixture.store.read(fixture.task_id, child).accepted_callback_sha256,
        )
        for _axis, _parent, child in fixture.lane_round_ids
    }
    check(
        "supported-close recovery preserves archive and has zero signal or replay",
        archive.read_bytes() == archive_before
        and effects_before
        == (
            tuple(fixture.runtime.exit_requests),
            tuple(fixture.runtime.cleanup_calls),
            tuple(fixture.runtime.ownership_probes),
        )
        and accepted_after == accepted_before,
    )
    started_after = len(fixture.runtime.started)
    child_revisions = tuple(child.revision for child in children)
    replay = recover_task_review_for_mechanism(
        fixture.product, runtime_manager=fixture.runtime
    )
    check(
        "supported-close recovery is idempotent with zero provider replay",
        replay["status"] == "reviewing"
        and len(fixture.runtime.started) == started_after
        and tuple(
            fixture.store.read(fixture.task_id, child).revision
            for _axis, _parent, child in fixture.lane_round_ids
        )
        == child_revisions
        and effects_before
        == (
            tuple(fixture.runtime.exit_requests),
            tuple(fixture.runtime.cleanup_calls),
            tuple(fixture.runtime.ownership_probes),
        ),
    )


with tempfile.TemporaryDirectory(prefix="post-verification-authorization.") as raw:
    drift = prepare_supported_close_fixture(Path(raw))
    fixture = drift.recovery
    started_before = len(fixture.runtime.started)

    def stop_at_quarantined_boundary(event: str) -> None:
        if event == "drift-quarantine-complete":
            raise RuntimeError("retain the completed quarantine boundary")

    try:
        recover_task_review_for_mechanism(
            fixture.product,
            runtime_manager=fixture.runtime,
            quarantine_fault_observer=stop_at_quarantined_boundary,
        )
    except RuntimeError as exc:
        check(
            "post-verification fixture stops after quarantine and before review",
            str(exc) == "retain the completed quarantine boundary"
            and fixture.gate.read()["status"] == "attention-required"
            and len(fixture.runtime.started) == started_before,
        )
    else:
        raise AssertionError("post-verification quarantine failpoint did not fire")
    retained_review_operation_id = fixture.gate.read()[
        "active_review_operation_id"
    ]
    append_mechanism_decision(
        fixture,
        "fresh-operation-binding",
        fresh_operation_binding_decision(
            retained_review_operation_id, fixture.task_id
        ),
    )
    append_mechanism_decision(
        fixture,
        "post-verification-review-drive",
        POST_VERIFICATION_DECISION,
    )
    post_verification = load_chain(fixture.product)[-1]
    append_mechanism_decision(
        fixture,
        "exact-live-status-adapter",
        EXACT_LIVE_STATUS_DECISION,
    )
    exact_live = load_chain(fixture.product)[-1]
    authorization = authorized_post_verification_review_drive(
        exact_live, fixture.product.resolve()
    )
    check(
        "post-verification authorization preserves the exact historical chain",
        authorization is not None
        and authorization.active_review_operation_id
        == retained_review_operation_id
        and authorization.dispatch_operation_id == fixture.task_id
        and authorization.authorization_record_id == exact_live.record_id
        and authorization.authorization_record_id
        != post_verification.record_id,
    )
    recovered = recover_task_review_for_mechanism(
        fixture.product, runtime_manager=fixture.runtime
    )
    started_after = len(fixture.runtime.started)
    replay = recover_task_review_for_mechanism(
        fixture.product, runtime_manager=fixture.runtime
    )
    check(
        "post-verification recovery launches the authorized fresh review at most once",
        recovered["status"] == "reviewing"
        and replay["status"] == "reviewing"
        and started_after == started_before + 2
        and len(fixture.runtime.started) == started_after,
    )
    append_mechanism_decision(
        fixture,
        "post-fresh-publication-sync",
        POST_FRESH_PUBLICATION_DECISION,
    )
    latest = load_chain(fixture.product)[-1]
    sync_authorization = authorized_post_fresh_publication_sync(
        latest, fixture.product.resolve()
    )
    check(
        "post-fresh synchronization authorization preserves the consumed continuation grant",
        sync_authorization is not None
        and sync_authorization.continuation.authorization_record_id
        == exact_live.record_id
        and sync_authorization.authorization_record_id == latest.record_id,
    )
    append_mechanism_decision(
        fixture,
        "fresh-child-round-lifecycle-progress",
        FRESH_CHILD_PROGRESS_DECISION,
    )
    progress_resolution = load_chain(fixture.product)[-1]
    append_mechanism_decision(
        fixture,
        "authorization-compiler-compatibility",
        AUTHORIZATION_COMPATIBILITY_DECISION,
    )
    latest = load_chain(fixture.product)[-1]
    progress_authorization = authorized_post_fresh_publication_sync(
        latest, fixture.product.resolve()
    )
    check(
        "fresh progress compiler preserves the exact prior grant and latest authorization",
        progress_authorization is not None
        and progress_authorization.continuation.authorization_record_id
        == exact_live.record_id
        and progress_authorization.authorization_record_id == latest.record_id
        and progress_authorization.authorization_record_id
        != progress_resolution.record_id,
    )


def prepare_post_fresh_compiler_fixture(base: Path) -> RecoveryFixture:
    drift = prepare_supported_close_fixture(base)
    fixture = drift.recovery
    retained_review_operation_id = fixture.gate.read()[
        "active_review_operation_id"
    ]
    append_mechanism_decision(
        fixture,
        "fresh-operation-binding",
        fresh_operation_binding_decision(
            retained_review_operation_id, fixture.task_id
        ),
    )
    append_mechanism_decision(
        fixture,
        "post-verification-review-drive",
        POST_VERIFICATION_DECISION,
    )
    append_mechanism_decision(
        fixture,
        "exact-live-status-adapter",
        EXACT_LIVE_STATUS_DECISION,
    )
    append_mechanism_decision(
        fixture,
        "post-fresh-publication-sync",
        POST_FRESH_PUBLICATION_DECISION,
    )
    return fixture


for rejected_label, append_invalid_chain in (
    (
        "missing progress decision",
        lambda fixture: append_mechanism_decision(
            fixture,
            "authorization-compiler-compatibility",
            AUTHORIZATION_COMPATIBILITY_DECISION,
        ),
    ),
    (
        "reordered progress decision",
        lambda fixture: (
            append_mechanism_decision(
                fixture,
                "authorization-compiler-before-progress",
                AUTHORIZATION_COMPATIBILITY_DECISION,
            ),
            append_mechanism_decision(
                fixture,
                "fresh-child-round-lifecycle-progress",
                FRESH_CHILD_PROGRESS_DECISION,
            ),
            append_mechanism_decision(
                fixture,
                "authorization-compiler-compatibility",
                AUTHORIZATION_COMPATIBILITY_DECISION,
            ),
        ),
    ),
    (
        "ambiguous progress decision",
        lambda fixture: (
            append_mechanism_decision(
                fixture,
                "fresh-child-round-lifecycle-progress-one",
                FRESH_CHILD_PROGRESS_DECISION,
            ),
            append_mechanism_decision(
                fixture,
                "fresh-child-round-lifecycle-progress-two",
                FRESH_CHILD_PROGRESS_DECISION,
            ),
            append_mechanism_decision(
                fixture,
                "authorization-compiler-compatibility",
                AUTHORIZATION_COMPATIBILITY_DECISION,
            ),
        ),
    ),
    (
        "broadened compatibility decision",
        lambda fixture: (
            append_mechanism_decision(
                fixture,
                "fresh-child-round-lifecycle-progress",
                FRESH_CHILD_PROGRESS_DECISION,
            ),
            append_mechanism_decision(
                fixture,
                "authorization-compiler-compatibility",
                AUTHORIZATION_COMPATIBILITY_DECISION
                + " Authorize one additional provider relaunch.",
            ),
        ),
    ),
):
    with tempfile.TemporaryDirectory(
        prefix=f"post-fresh-compiler-{rejected_label.split()[0]}."
    ) as raw:
        fixture = prepare_post_fresh_compiler_fixture(Path(raw))
        append_invalid_chain(fixture)
        latest = load_chain(fixture.product)[-1]
        check(
            f"fresh progress compiler rejects {rejected_label}",
            authorized_post_fresh_publication_sync(
                latest, fixture.product.resolve()
            )
            is None,
        )


def exact_authorization_chain(
    worktree: Path,
) -> list[DecisionRecord]:
    scope = {
        "category": "mechanism-failure",
        "worktree": str(worktree),
        "task_name": "mechanism recovery",
        "task_surface": "surface-exact",
    }
    rows = (
        (
            "resolution-4331f351b941ba742fa44496cd53f8d8",
            "resolution",
            "44676c9221624c9f0027d954886a9926313de37bf5bbdeb1d11f914fbc4b29bc",
            "ab22dd67-791d-4ef1-8f9e-9eab13c87252",
            "e892ccfccf8b348f6e22008475b883f3fe243d48deb7ab2d13ad2acca682b785",
            AUTHORIZATION_COMPATIBILITY_DECISION,
        ),
        (
            "bd012db9-c598-406e-98e0-d5d502e24106",
            "raise",
            "4086ecaaf68c1ecc068c86d6964756f69b492fa11152d988476b76a954b40d99",
            "resolution-4331f351b941ba742fa44496cd53f8d8",
            "44676c9221624c9f0027d954886a9926313de37bf5bbdeb1d11f914fbc4b29bc",
            "",
        ),
        (
            "resolution-439263b4acf7db64fbba42861eaa7b08",
            "resolution",
            "d5e08d66d75cd274f8a2dc240a0e687ffab60e0be9dab4157d88d6abc1a1d480",
            "bd012db9-c598-406e-98e0-d5d502e24106",
            "4086ecaaf68c1ecc068c86d6964756f69b492fa11152d988476b76a954b40d99",
            TIMING_GATE_DECISION,
        ),
        (
            "2f0718a4-fe30-4f97-97a6-1c5faa3fccd6",
            "raise",
            "98d4e69168e0a7b4cc3c68b815c74425eb5ce8f544432894e29b5a375383e079",
            "resolution-439263b4acf7db64fbba42861eaa7b08",
            "d5e08d66d75cd274f8a2dc240a0e687ffab60e0be9dab4157d88d6abc1a1d480",
            "",
        ),
        (
            "resolution-b28b1e20822edf26a9c4ffa399abf305",
            "resolution",
            "0f4c34f780989a8921741390b872aa0d5b0b0ecfb2fbcc7ba4f33a8520074ce9",
            "2f0718a4-fe30-4f97-97a6-1c5faa3fccd6",
            "98d4e69168e0a7b4cc3c68b815c74425eb5ce8f544432894e29b5a375383e079",
            AUTHORIZATION_CHAIN_DECISION,
        ),
    )
    return [
        DecisionRecord(
            record_id,
            record_type,
            {
                **scope,
                "status": "resolved" if record_type == "resolution" else "pending",
                "decision": decision,
            },
            digest,
            worktree / f"{record_id}.json",
            False,
            previous_id,
            previous_sha,
        )
        for (
            record_id,
            record_type,
            digest,
            previous_id,
            previous_sha,
            decision,
        ) in rows
    ]


with tempfile.TemporaryDirectory(prefix="authorization-chain-exact.") as raw:
    product = Path(raw).resolve()
    chain = exact_authorization_chain(product)
    continuation = SimpleNamespace(dispatch_operation_id="dispatch-exact")
    prior = SimpleNamespace(continuation=continuation)
    compiled = _compile_authorization_chain_compatibility(
        chain, len(chain) - 1, product, prior
    )
    check(
        "authorization chain compiler accepts only the exact immutable tail",
        compiled is not None
        and compiled.continuation is continuation
        and compiled.authorization_record_id == chain[-1].record_id
        and compiled.authorization_record_sha256 == chain[-1].sha256,
    )
    mutations = {
        "missing record": chain[:-1],
        "reordered record": [chain[0], chain[2], chain[1], *chain[3:]],
        "duplicated record": [chain[0], chain[0], *chain[1:]],
        "ambiguous resolution": [
            replace(
                chain[-1],
                record_id="resolution-ambiguous-chain",
                sha256="2" * 64,
                previous_record_id="resolution-before-chain",
                previous_record_sha256="3" * 64,
            ),
            *chain,
        ],
        "wrong digest": [*chain[:-1], replace(chain[-1], sha256="0" * 64)],
        "broken predecessor": [
            *chain[:-1],
            replace(chain[-1], previous_record_sha256="1" * 64),
        ],
        "broadened decision": [
            *chain[:-1],
            replace(
                chain[-1],
                payload={
                    **chain[-1].payload,
                    "decision": AUTHORIZATION_CHAIN_DECISION
                    + " Permit one provider retry.",
                },
            ),
        ],
        "unrelated scope": [
            chain[0],
            replace(
                chain[1],
                payload={**chain[1].payload, "task_surface": "surface-other"},
            ),
            *chain[2:],
        ],
    }
    for label, candidate in mutations.items():
        check(
            f"authorization chain compiler rejects {label}",
            _compile_authorization_chain_compatibility(
                candidate, len(candidate) - 1, product, prior
            )
            is None,
        )


with tempfile.TemporaryDirectory(prefix="authorization-chain-boundary.") as raw:
    base = Path(raw)
    product = base / "product"
    vault = base / "vault"
    product.mkdir()
    (vault / "wiki").mkdir(parents=True)
    (vault / "scripts").mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=product, check=True)
    subprocess.run(
        ["git", "config", "user.email", "review@example.invalid"],
        cwd=product,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Authorization Test"],
        cwd=product,
        check=True,
    )
    task_id = "ad97826c-0651-4014-a113-72518e6fceea"
    write_json(
        product / ".task-meta.json",
        {
            "task_id": task_id,
            "worktree": str(product.resolve()),
            "vault_root": str(vault.resolve()),
        },
    )
    receipt_path = product / "evidence/receipt.json"
    receipt_path.parent.mkdir()
    receipt_path.write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=product, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "release receipt"], cwd=product, check=True)
    release_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=product, text=True, capture_output=True, check=True
    ).stdout.strip()
    release_tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=product, text=True, capture_output=True, check=True
    ).stdout.strip()
    (product / "repair.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=product, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "clean descendant"], cwd=product, check=True)
    gate_root = (
        vault
        / ".vault-meta/harness/review-data"
        / task_id
        / task_id
    )
    gate_root.mkdir(parents=True)
    gate = {
        "schema_version": 1,
        "owner_id": task_id,
        "dispatch_operation_id": task_id,
        "active_review_operation_id": (
            "b63552c9-60c2-54b3-99c9-093525a65c44-fresh-8016a8aa"
        ),
        "status": "attention-required",
        "fresh_reevaluation_used": True,
    }
    write_json(gate_root / "review-gate.json", gate)
    gate_sha = hashlib.sha256((gate_root / "review-gate.json").read_bytes()).hexdigest()
    runtime_root = vault / ".vault-meta/harness/owners" / task_id / "runtime" / task_id
    runtime_root.mkdir(parents=True)
    authorization = SimpleNamespace(
        continuation=SimpleNamespace(dispatch_operation_id=task_id)
    )
    receipt = {
        "attempt_id": "attempt-exact",
        "profile": "release-final",
        "profile_sha256": (
            "804158ff362f6683f29d6a76f858fa8f02912051f6fe31ad2fb39270c2e56067"
        ),
        "subject_head_sha": release_head,
        "subject_tree_sha": release_tree,
        "execution_relation": "release-candidate",
        "status": "passed",
    }
    overrides = {
        "_RECONCILE_GATE_SHA256": gate_sha,
        "_RELEASE_FINAL_ATTEMPT_ID": "attempt-exact",
        "_RELEASE_FINAL_HEAD": release_head,
        "_RELEASE_FINAL_TREE": release_tree,
        "_RELEASE_FINAL_RECEIPT": Path("evidence/receipt.json"),
    }
    with patch.multiple("task_review_authorization_boundary", **overrides):
        verifier = lambda _path: receipt
        check(
            "authorization boundary proves clean ancestry, gate, receipt, and unused reconcile",
            _authorization_chain_boundary_is_valid(
                product.resolve(), authorization, receipt_verifier=verifier
            ),
        )
        (product / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        check(
            "authorization boundary rejects a dirty descendant",
            not _authorization_chain_boundary_is_valid(
                product.resolve(), authorization, receipt_verifier=verifier
            ),
        )
        (product / "dirty.txt").unlink()
        sync = runtime_root / "post-fresh-publication-sync.json"
        sync.write_text("{}\n", encoding="utf-8")
        check(
            "authorization boundary rejects a consumed reconcile",
            not _authorization_chain_boundary_is_valid(
                product.resolve(), authorization, receipt_verifier=verifier
            ),
        )
        sync.unlink()
        check(
            "authorization boundary rejects receipt identity drift",
            not _authorization_chain_boundary_is_valid(
                product.resolve(),
                authorization,
                receipt_verifier=lambda _path: {**receipt, "attempt_id": "other"},
            ),
        )
        write_json(gate_root / "review-gate.json", {**gate, "status": "reviewing"})
        check(
            "authorization boundary rejects gate identity drift",
            not _authorization_chain_boundary_is_valid(
                product.resolve(), authorization, receipt_verifier=verifier
            ),
        )


with tempfile.TemporaryDirectory(prefix="supported-close-crash.") as raw:
    drift = prepare_supported_close_fixture(Path(raw))
    fixture = drift.recovery
    started_before = len(fixture.runtime.started)

    def crash_after_supported_close_projection(event: str) -> None:
        if event.startswith("supported-close-round-completed:"):
            raise RuntimeError("simulated supported-close projection crash")

    try:
        recover_task_review_for_mechanism(
            fixture.product,
            runtime_manager=fixture.runtime,
            quarantine_fault_observer=crash_after_supported_close_projection,
        )
    except RuntimeError as exc:
        completed = [
            child
            for _axis, _parent, child in fixture.lane_round_ids
            if fixture.store.read(fixture.task_id, child).state == "complete"
        ]
        check(
            "supported-close crash follows one atomic child projection",
            str(exc) == "simulated supported-close projection crash"
            and len(completed) == 1
            and len(fixture.runtime.started) == started_before,
        )
    else:
        raise AssertionError("supported-close projection failpoint did not fire")
    completed_child = completed[0]
    completed_revision = fixture.store.read(
        fixture.task_id, completed_child
    ).revision
    recovered = recover_task_review_for_mechanism(
        fixture.product, runtime_manager=fixture.runtime
    )
    check(
        "supported-close crash restart converges without repeating projection",
        recovered["status"] == "reviewing"
        and fixture.store.read(fixture.task_id, completed_child).revision
        == completed_revision
        and all(
            fixture.store.read(fixture.task_id, child).state == "complete"
            for _axis, _parent, child in fixture.lane_round_ids
        )
        and fixture.runtime.ownership_probes == []
        and len(fixture.runtime.started) == started_before + 2,
    )


for rejected_label, mutate, expected in (
    (
        "non-terminal parent",
        lambda fixture, parent, child, root: replace_record(
            fixture,
            parent,
            state="exiting",
            resources=OwnedResources(
                surface_id="22222222-2222-4222-8222-222222222222"
            ),
        ),
        "supported-close parent receipt changed",
    ),
    (
        "pending exit effect",
        lambda fixture, parent, child, root: replace_record(
            fixture,
            parent,
            pending_effect="request-exit",
            effect_outcome=EffectOutcome.PENDING,
        ),
        "supported-close parent receipt changed",
    ),
    (
        "partial live resources",
        lambda fixture, parent, child, root: replace_record(
            fixture,
            parent,
            resources=OwnedResources(
                surface_id="11111111-1111-4111-8111-111111111111"
            ),
        ),
        "supported-close parent receipt changed",
    ),
    (
        "round callback mismatch",
        lambda fixture, parent, child, root: replace_record(
            fixture,
            child,
            accepted_callback_id="review-mismatched",
            accepted_callback_sha256="a" * 64,
        ),
        "supported-close round identity changed",
    ),
    (
        "archive drift",
        lambda fixture, parent, child, root: write_json(
            root / "evidence.json",
            {
                **json.loads(
                    (root / "evidence.json").read_text(encoding="utf-8")
                ),
                "status": "tampered",
            },
        ),
        "evidence identity changed",
    ),
):
    with tempfile.TemporaryDirectory(prefix="supported-close-reject.") as raw:
        drift = prepare_supported_close_fixture(Path(raw))
        fixture = drift.recovery
        parent = fixture.lane_round_ids[0][1]
        child = fixture.lane_round_ids[0][2]
        authorization = authorized_supported_close_retirement(
            load_chain(fixture.product)[-1], fixture.product.resolve()
        )
        assert authorization is not None
        root = (
            fixture.gate.root
            / "drift-quarantine"
            / authorization.signal_free.drift.authorization_record_id
        )
        mutate(fixture, parent, child, root)
        effects_before = (
            tuple(fixture.runtime.exit_requests),
            tuple(fixture.runtime.cleanup_calls),
            len(fixture.runtime.started),
        )
        expect_error(
            f"supported-close recovery rejects {rejected_label}",
            lambda: recover_task_review_for_mechanism(
                fixture.product, runtime_manager=fixture.runtime
            ),
            expected,
        )
        check(
            f"{rejected_label} rejection has zero signal and zero replay",
            effects_before
            == (
                tuple(fixture.runtime.exit_requests),
                tuple(fixture.runtime.cleanup_calls),
                len(fixture.runtime.started),
            ),
        )


with tempfile.TemporaryDirectory(prefix="signal-free-live.") as raw:
    drift = prepare_signal_free_fixture(Path(raw))
    fixture = drift.recovery
    parent = fixture.lane_round_ids[0][1]
    fixture.runtime.cleanup_ownership[parent] = DurableCleanupOwnership(
        "alive", "alive", "missing", "missing", "workspace-live", "window-live"
    )
    exits_before = tuple(fixture.runtime.exit_requests)
    cleanup_before = tuple(fixture.runtime.cleanup_calls)
    started_before = len(fixture.runtime.started)
    expect_error(
        "signal-free recovery rejects an actually live exact match",
        lambda: recover_task_review_for_mechanism(
            fixture.product, runtime_manager=fixture.runtime
        ),
        "exact live resource",
    )
    check(
        "exact-live rejection sends zero signal and starts zero provider",
        tuple(fixture.runtime.exit_requests) == exits_before
        and tuple(fixture.runtime.cleanup_calls) == cleanup_before
        and len(fixture.runtime.started) == started_before,
    )


for reuse_label, reuse_message in (
    ("PID/PGID reuse", "durable cleanup process identity was reused"),
    ("supervisor reuse", "durable cleanup supervisor identity changed"),
):
    with tempfile.TemporaryDirectory(prefix="signal-free-reuse.") as raw:
        drift = prepare_signal_free_fixture(Path(raw))
        fixture = drift.recovery
        parent = fixture.lane_round_ids[0][1]
        fixture.runtime.cleanup_ownership_errors[parent] = RuntimeSessionError(
            reuse_message
        )
        effects_before = (
            tuple(fixture.runtime.exit_requests),
            tuple(fixture.runtime.cleanup_calls),
            len(fixture.runtime.started),
        )
        expect_error(
            f"signal-free recovery rejects {reuse_label}",
            lambda: recover_task_review_for_mechanism(
                fixture.product, runtime_manager=fixture.runtime
            ),
            "ownership inventory is ambiguous",
        )
        check(
            f"{reuse_label} rejection has zero signal and zero provider replay",
            effects_before
            == (
                tuple(fixture.runtime.exit_requests),
                tuple(fixture.runtime.cleanup_calls),
                len(fixture.runtime.started),
            ),
        )


with tempfile.TemporaryDirectory(prefix="signal-free-partial.") as raw:
    drift = prepare_signal_free_fixture(Path(raw))
    fixture = drift.recovery
    parent = fixture.lane_round_ids[0][1]
    fixture.runtime.cleanup_ownership[parent] = DurableCleanupOwnership(
        "dead", "unknown", "missing", "missing", "workspace-partial", "window-partial"
    )
    expect_error(
        "signal-free recovery rejects partial identity evidence",
        lambda: recover_task_review_for_mechanism(
            fixture.product, runtime_manager=fixture.runtime
        ),
        "ownership inventory is ambiguous",
    )


with tempfile.TemporaryDirectory(prefix="signal-free-crash.") as raw:
    drift = prepare_signal_free_fixture(Path(raw))
    fixture = drift.recovery
    exits_before = tuple(fixture.runtime.exit_requests)
    started_before = len(fixture.runtime.started)

    def crash_after_signal_free_retirement(event: str) -> None:
        if event.startswith("signal-free-parent-retired:"):
            raise RuntimeError("simulated atomic retirement crash")

    try:
        recover_task_review_for_mechanism(
            fixture.product,
            runtime_manager=fixture.runtime,
            quarantine_fault_observer=crash_after_signal_free_retirement,
        )
    except RuntimeError as exc:
        check(
            "signal-free crash occurs after one atomic terminal resource projection",
            str(exc) == "simulated atomic retirement crash"
            and sum(
                fixture.store.read(fixture.task_id, parent).state == "complete"
                for _axis, parent, _child in fixture.lane_round_ids
            )
            == 1
            and len(fixture.runtime.started) == started_before,
        )
    else:
        raise AssertionError("signal-free retirement failpoint did not fire")
    first_parent = fixture.lane_round_ids[0][1]
    first_probe_count = fixture.runtime.ownership_probes.count(first_parent)
    recovered = recover_task_review_for_mechanism(
        fixture.product, runtime_manager=fixture.runtime
    )
    check(
        "signal-free crash restart resumes prepared progress without repeating retirement proof",
        recovered["status"] == "reviewing"
        and fixture.runtime.ownership_probes.count(first_parent) == first_probe_count
        and tuple(fixture.runtime.exit_requests) == exits_before
        and len(fixture.runtime.started) == started_before + 2,
    )


for drift_label, mutate, expected in (
    (
        "missing archive",
        lambda fixture, root: (root / "evidence.json").unlink(),
        "archive is unavailable",
    ),
    (
        "archive tampering",
        lambda fixture, root: write_json(
            root / "evidence.json",
            {
                **json.loads((root / "evidence.json").read_text(encoding="utf-8")),
                "status": "tampered",
            },
        ),
        "evidence identity changed",
    ),
    (
        "progress drift",
        lambda fixture, root: write_json(
            root / "progress.json",
            {
                **json.loads((root / "progress.json").read_text(encoding="utf-8")),
                "cleaned_parents": ["unrelated-parent"],
            },
        ),
        "progress identity changed",
    ),
    (
        "gate drift",
        lambda fixture, root: write_json(
            fixture.gate.state_path,
            {**fixture.gate.read(), "status": "attention-required"},
        ),
        "gate drifted",
    ),
):
    with tempfile.TemporaryDirectory(prefix="signal-free-drift.") as raw:
        drift = prepare_signal_free_fixture(Path(raw))
        fixture = drift.recovery
        authorization = authorized_signal_free_retirement(
            load_chain(fixture.product)[-1], fixture.product.resolve()
        )
        assert authorization is not None
        root = (
            fixture.gate.root
            / "drift-quarantine"
            / authorization.drift.authorization_record_id
        )
        mutate(fixture, root)
        effects_before = (
            tuple(fixture.runtime.exit_requests),
            tuple(fixture.runtime.cleanup_calls),
            len(fixture.runtime.started),
        )
        expect_error(
            f"signal-free recovery rejects {drift_label}",
            lambda: recover_task_review_for_mechanism(
                fixture.product, runtime_manager=fixture.runtime
            ),
            expected,
        )
        check(
            f"{drift_label} rejection precedes signals and provider replay",
            effects_before
            == (
                tuple(fixture.runtime.exit_requests),
                tuple(fixture.runtime.cleanup_calls),
                len(fixture.runtime.started),
            ),
        )


with tempfile.TemporaryDirectory(prefix="signal-free-unrelated.") as raw:
    drift = prepare_signal_free_fixture(Path(raw))
    fixture = drift.recovery
    parent = fixture.store.read(fixture.task_id, fixture.lane_round_ids[0][1])
    fixture.store.create(
        replace(
            parent.spec,
            operation_id="late-unrelated-review-owner",
            idempotency_key="late-unrelated-review-owner-key",
        ),
        lane_id="late-unrelated-lane",
        run_id="late-unrelated-run",
    )
    expect_error(
        "signal-free recovery rechecks and rejects unrelated ownership",
        lambda: recover_task_review_for_mechanism(
            fixture.product, runtime_manager=fixture.runtime
        ),
        "unrelated live ownership",
    )


with tempfile.TemporaryDirectory(prefix="legacy-round-guards.") as raw:
    fixture = build_fixture(
        Path(raw), gate_status="awaiting-resolution", legacy_round_specs=True
    )
    terminalize(fixture.store, fixture.task_id, fixture.child_id)
    stored = fixture.store.read(fixture.task_id, fixture.child_id)
    requested = replace(
        stored.spec,
        parent_operation_id=fixture.parent_id,
    )
    original_record = stored

    success_error = StoreError("compatibility candidate")
    success_store = RejectingRoundStore(stored, success_error)
    recovered = RecoveryRoundStore(success_store).create(
        requested, lane_id=stored.lane_id, run_id=stored.run_id
    )
    check(
        "legacy round adapter rehydrates exact terminal identity in memory only",
        recovered.spec == requested
        and stored == original_record
        and success_store.create_calls == 1
        and success_store.read_calls == 1,
    )

    guard_cases = (
        (
            "wrong kind",
            replace(requested, kind="verification"),
            stored,
            stored.lane_id,
            stored.run_id,
        ),
        (
            "empty parent identity",
            replace(requested, parent_operation_id=""),
            stored,
            stored.lane_id,
            stored.run_id,
        ),
        (
            "additional specification drift",
            requested,
            replace(
                stored,
                spec=replace(stored.spec, context_manifest="drift.json"),
            ),
            stored.lane_id,
            stored.run_id,
        ),
        (
            "lane mismatch",
            requested,
            stored,
            "different-lane",
            stored.run_id,
        ),
        (
            "run mismatch",
            requested,
            stored,
            stored.lane_id,
            "different-run",
        ),
        (
            "nonterminal state",
            requested,
            replace(stored, state="created"),
            stored.lane_id,
            stored.run_id,
        ),
        (
            "owned resources",
            requested,
            replace(
                stored,
                resources=OwnedResources(surface_id="legacy-surface"),
            ),
            stored.lane_id,
            stored.run_id,
        ),
        (
            "pending effect",
            requested,
            replace(
                stored,
                pending_effect="callback-write",
                effect_id="callback-write",
                effect_outcome=EffectOutcome.PENDING,
            ),
            stored.lane_id,
            stored.run_id,
        ),
    )
    for label, spec, existing, lane_id, run_id in guard_cases:
        check_legacy_round_rejection(
            label,
            requested_spec=spec,
            existing=existing,
            lane_id=lane_id,
            run_id=run_id,
        )

    check_legacy_round_rejection(
        "unreadable existing record",
        requested_spec=requested,
        existing=None,
        lane_id=stored.lane_id,
        run_id=stored.run_id,
        read_error=StoreError("unreadable existing record"),
    )


with tempfile.TemporaryDirectory(prefix="summary-resolution-evidence.") as raw:
    fixture = build_fixture(Path(raw))
    current_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=fixture.product,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    evidence_name = "summary-resolution.json"
    write_json(
        fixture.gate.root / evidence_name,
        {
            "schema_version": 1,
            "operation_id": fixture.task_id,
            "axis": "openai-holistic",
            "reviewed_head_sha": "a" * 40,
            "resolved_head_sha": current_head,
            "fix_delta_sha256": "b" * 64,
            "previous_finding_ids": ["finding-1"],
            "resolutions": [
                {
                    "finding_id": "finding-1",
                    "disposition": "applied",
                    "rationale": "The exact summary boundary was repaired.",
                    "follow_up": "",
                }
            ],
        },
    )
    resolution = approved_summary_resolution(
        gate=fixture.gate,
        state={"resolution_evidence": {"openai-holistic:0": evidence_name}},
        task_id=fixture.task_id,
        simple_axis="openai-holistic",
        current_head=current_head,
    )
    check(
        "approved summary resolution binds one safe relative evidence pointer",
        resolution.operation_id == fixture.task_id
        and resolution.axis == "openai-holistic"
        and resolution.resolved_head_sha == current_head,
    )

    expect_error(
        "approved summary resolution rejects ambiguous evidence",
        lambda: approved_summary_resolution(
            gate=fixture.gate,
            state={"resolution_evidence": {}},
            task_id=fixture.task_id,
            simple_axis="openai-holistic",
            current_head=current_head,
        ),
        "resolution boundary is invalid",
    )


with tempfile.TemporaryDirectory(prefix="mechanism-recovery-unit.") as raw:
    fixture = build_fixture(Path(raw))

    authorization_mutations = (
        ("status", "open"),
        ("category", "runtime-contract"),
        ("worktree", str(Path(raw) / "another-product")),
        ("decision", "approve repair"),
        (
            "decision",
            "authorize-one-bounded-fresh-context-review-boundary-for-deadbee",
        ),
    )
    for field, wrong_value in authorization_mutations:
        changed = {**fixture.exact_attention, field: wrong_value}
        write_json(fixture.attention_path, changed)
        expect_error(
            f"mechanism recovery rejects authorization drift in {field}",
            lambda: recover_task_review_for_mechanism(
                fixture.product, runtime_manager=fixture.runtime
            ),
            "lacks exact coordinator authorization",
        )
    fixture.attention_path.write_bytes(fixture.attention_pointer)

    expect_error(
        "mechanism recovery rejects a live review boundary",
        lambda: recover_task_review_for_mechanism(
            fixture.product, runtime_manager=fixture.runtime
        ),
        "still has live review ownership",
    )

    for operation_id in (fixture.parent_id, fixture.child_id):
        terminalize(fixture.store, fixture.task_id, operation_id)

    resource_mutations = (
        (
            fixture.parent_id,
            {"resources": OwnedResources(surface_id="surface:91")},
            "parent resource",
        ),
        (
            fixture.child_id,
            {"resources": OwnedResources(process_group=8123)},
            "child resource",
        ),
        (
            fixture.parent_id,
            {
                "pending_effect": "open-surface",
                "effect_id": "open-surface",
                "effect_outcome": EffectOutcome.PENDING,
            },
            "parent pending effect",
        ),
        (
            fixture.child_id,
            {
                "pending_effect": "callback-write",
                "effect_id": "callback-write",
                "effect_outcome": EffectOutcome.PENDING,
            },
            "child pending effect",
        ),
    )
    for operation_id, mutation, label in resource_mutations:
        replace_record(fixture, operation_id, **mutation)
        expect_error(
            f"mechanism recovery rejects {label}",
            lambda: recover_task_review_for_mechanism(
                fixture.product, runtime_manager=fixture.runtime
            ),
            "still has live review ownership",
        )
        reset = {
            "resources": OwnedResources(),
            "pending_effect": "",
            "effect_id": "",
            "effect_outcome": EffectOutcome.NONE,
        }
        replace_record(fixture, operation_id, **reset)

    started_before = len(fixture.runtime.started)
    recovered = recover_task_review_for_mechanism(
        fixture.product, runtime_manager=fixture.runtime
    )
    recovered_state = fixture.gate.read()
    check(
        "exact quiescent authorization launches one bounded fresh review",
        recovered["status"] == "reviewing"
        and len(fixture.runtime.started) == started_before + 1
        and recovered_state["status"] == "reviewing"
        and recovered_state["fresh_reevaluation_used"] is True
        and recovered_state["policy"]["max_verify_iterations"] == 0,
    )

    replay = recover_task_review_for_mechanism(
        fixture.product, runtime_manager=fixture.runtime
    )
    check(
        "exact mechanism recovery replay does not launch another provider",
        replay["status"] == "reviewing"
        and replay["lanes"] == recovered["lanes"]
        and len(fixture.runtime.started) == started_before + 1,
    )


with tempfile.TemporaryDirectory(
    prefix="mechanism-recovery-awaiting-resolution."
) as raw:
    fixture = build_fixture(
        Path(raw),
        gate_status="awaiting-resolution",
        legacy_round_specs=True,
    )
    state_before = fixture.gate.read()
    started_before = len(fixture.runtime.started)
    terminalize(fixture.store, fixture.task_id, fixture.child_id)
    expect_error(
        "awaiting-resolution recovery rejects live retained ownership",
        lambda: recover_task_review_for_mechanism(
            fixture.product, runtime_manager=fixture.runtime
        ),
        "still has live review ownership",
    )
    check(
        "rejected awaiting-resolution recovery has zero state/provider effect",
        fixture.gate.read() == state_before
        and len(fixture.runtime.started) == started_before,
    )

    for operation_id in (fixture.parent_id, fixture.child_id):
        terminalize(fixture.store, fixture.task_id, operation_id)

    recovered = recover_task_review_for_mechanism(
        fixture.product, runtime_manager=fixture.runtime
    )
    recovered_state = fixture.gate.read()
    persisted_resolution = fixture.gate.root / "persisted-resolution.json"
    check(
        "quiescent awaiting-resolution enters one fresh review boundary",
        recovered["status"] == "reviewing"
        and len(fixture.runtime.started) == started_before + 1
        and recovered_state["status"] == "reviewing"
        and recovered_state["fresh_reevaluation_used"] is True
        and recovered_state["policy"]["max_verify_iterations"] == 0
        and json.loads(persisted_resolution.read_text(encoding="utf-8"))
        == {"evidence": "historical-resolution"},
    )

    replay = recover_task_review_for_mechanism(
        fixture.product, runtime_manager=fixture.runtime
    )
    check(
        "awaiting-resolution recovery replay does not duplicate provider",
        replay["status"] == "reviewing"
        and replay["lanes"] == recovered["lanes"]
        and len(fixture.runtime.started) == started_before + 1,
    )


print("\nAll task review mechanism recovery unit tests passed.")
