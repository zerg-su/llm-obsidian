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


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import (  # noqa: E402
    EffectOutcome,
    OperationRecord,
    OwnedResources,
    RuntimeRoute,
)
from harness.pipeline_builtins import compiled_builtin  # noqa: E402
from harness.state_machine import TERMINAL  # noqa: E402
from harness.store import OperationStore, StoreError  # noqa: E402
from harness.verification import load_profiles  # noqa: E402
from harness.workflows.review import (  # noqa: E402
    ReviewOperationRequest,
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
from task_review_mechanism_recovery import (  # noqa: E402
    recover_task_review_for_mechanism,
)
from task_review_legacy_rounds import RecoveryRoundStore  # noqa: E402
from task_review_resolution_evidence import (  # noqa: E402
    approved_summary_resolution,
)
from task_review_shared import TaskReviewError  # noqa: E402
from task_escalation_records import (  # noqa: E402
    append_raise,
    append_resolution,
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


def write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
    )


def build_fixture(
    base: Path,
    *,
    gate_status: str = "attention-required",
    legacy_round_specs: bool = False,
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
    preset = ReviewPreset.from_flags()
    request = ReviewOperationRequest(
        preset.request(task_id, selected_provider="openai"),
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
    run = gate.begin(
        dispatch_operation_id=task_id,
        request=request,
        origin_surface=str(meta["task_surface"]),
        cwd=runtime_root,
        product_root=product,
        prompt_pointer="prompts/review.md",
        callback_root="callbacks",
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
