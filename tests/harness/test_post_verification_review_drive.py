#!/usr/bin/env python3
"""Post-verification review-drive continuation is exact and replay-free."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import (  # noqa: E402
    AttentionReason,
    CallbackEnvelope,
    EffectOutcome,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.callbacks import CallbackBroker  # noqa: E402
from harness import cli as harness_cli  # noqa: E402
from harness.liveness import (  # noqa: E402
    LivenessController,
    LivenessEvidence,
    LivenessPolicy,
)
from harness.post_verification_review_drive import (  # noqa: E402
    PostVerificationReviewDriveError,
    continued_verification_receipt,
    post_verification_review_marker,
    synchronize_post_verification_review_drive,
)
from task_review_post_fresh_publication import (  # noqa: E402
    synchronize_post_fresh_publication,
)
from task_review_drift_contract import (  # noqa: E402
    DriftQuarantineAuthorization,
    PostFreshPublicationSyncAuthorization,
    PostVerificationReviewDriveAuthorization,
    SignalFreeRetirementAuthorization,
    SupportedCloseRetirementAuthorization,
)
from review_contract import review_parent_kind  # noqa: E402
from harness.runtime_worker import (  # noqa: E402
    _pipeline_verify_effect_id,
    _pipeline_verify_identity,
)
from harness.runtime_worker_summary import RuntimeWorkerSummaryMixin  # noqa: E402
from harness.store import OperationStore  # noqa: E402
from harness.verification import load_profiles  # noqa: E402
from harness.verification_attempt import VerificationAttempt  # noqa: E402


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hash(root: Path, *, exclude: tuple[str, ...] = ()) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in exclude:
            continue
        digest.update(relative.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()


def advance(
    store: OperationStore, owner: str, operation: str, states: tuple[str, ...]
) -> None:
    for state in states:
        store.transition(owner, operation, state)


class ExactProcess:
    def __init__(self, resources: OwnedResources) -> None:
        self.resources = resources
        self.process_state = "alive"
        self.supervisor_state = "alive"
        self.probes: list[tuple[str, int, str]] = []
        self.signals: list[str] = []

    def process_status(self, process_group: int, identity: str) -> str:
        self.probes.append(("process", process_group, identity))
        if (
            process_group != self.resources.process_group
            or identity != self.resources.process_identity
        ):
            return "unknown"
        return self.process_state

    def pid_status(self, pid: int, identity: str) -> str:
        self.probes.append(("supervisor", pid, identity))
        if (
            pid != self.resources.supervisor_pid
            or identity != self.resources.supervisor_identity
        ):
            return "unknown"
        return self.supervisor_state

    def request_guardian_signal(self, *_args: object, **_kwargs: object) -> None:
        self.signals.append("guardian")
        raise AssertionError("post-verification continuation cannot signal")

    def request_exit(self, *_args: object, **_kwargs: object) -> None:
        self.signals.append("exit")
        raise AssertionError("post-verification continuation cannot signal")


class MixedDarwinProcess(ExactProcess):
    def process_status(self, process_group: int, identity: str) -> str:
        super().process_status(process_group, identity)
        return "unknown"

    def pid_status(self, pid: int, identity: str) -> str:
        super().pid_status(pid, identity)
        return "unknown"

    def exact_statuses(
        self,
        process_group: int,
        process_identity: str,
        supervisor_pid: int,
        supervisor_identity: str,
    ) -> tuple[str, str]:
        assert (
            process_group,
            process_identity,
            supervisor_pid,
            supervisor_identity,
        ) == (
            self.resources.process_group,
            self.resources.process_identity,
            self.resources.supervisor_pid,
            self.resources.supervisor_identity,
        )
        return "alive", "alive"


class ExactCmux:
    def __init__(self, surface_id: str) -> None:
        self.surface_id = surface_id
        self.state = "alive"
        self.probes: list[str] = []
        self.closes: list[str] = []

    def status(self, surface_id: str) -> str:
        self.probes.append(surface_id)
        if surface_id != self.surface_id:
            return "unknown"
        return self.state

    def close_exact(self, surface_id: str) -> None:
        self.closes.append(surface_id)
        raise AssertionError("post-verification continuation cannot close")


class RecoveryEffect:
    def __init__(self, data: dict[str, object]) -> None:
        self.data = data
        self.calls = 0
        self.provider_starts = 0

    def __call__(self) -> dict[str, object]:
        self.calls += 1
        gate_path = self.data["gate_path"]
        assert isinstance(gate_path, Path)
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        if gate.get("fresh_reevaluation_used") is not True:
            self.provider_starts += 2
            gate.update(
                {
                    "status": "reviewing",
                    "fresh_reevaluation_used": True,
                    "fresh_boundary": {
                        "kind": "context",
                        "reason": "authorized post-verification continuation",
                        "next_context_sha256": "e" * 64,
                    },
                    "fresh_boundary_authorization": {
                        "pointer": "fresh-boundary.json",
                        "sha256": "f" * 64,
                        "status": "authorized",
                    },
                    "context": {
                        **gate["context"],
                        "head_sha": self.data["target_head"],
                    },
                }
            )
            write_json(gate_path, gate)
            progress_path = self.data["progress_path"]
            assert isinstance(progress_path, Path)
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            progress["status"] = "fresh-review-started"
            write_json(progress_path, progress)
        return {"status": "reviewing"}


def fixture(root: Path) -> dict[str, object]:
    vault = root / "vault"
    product = root / "product"
    (vault / "wiki").mkdir(parents=True)
    (vault / "config").mkdir()
    (vault / "config" / "verification-profiles.toml").write_bytes(
        (ROOT / "config" / "verification-profiles.toml").read_bytes()
    )
    product.mkdir()
    git(product, "init", "-q", "-b", "task/post-verification")
    git(product, "config", "user.email", "continuation@example.invalid")
    git(product, "config", "user.name", "Continuation Test")
    (product / ".gitignore").write_text(".task-*.json\n", encoding="utf-8")
    (product / "product.txt").write_text("verified\n", encoding="utf-8")
    git(product, "add", ".gitignore", "product.txt")
    git(product, "commit", "-q", "-m", "verified source")
    source_head = git(product, "rev-parse", "HEAD")

    task_id = str(uuid.uuid4())
    review_id = str(uuid.uuid4())
    definition = "d" * 64
    profile_sha = load_profiles(
        vault / "config" / "verification-profiles.toml"
    )["scoped"].sha256
    write_json(
        product / ".task-meta.json",
        {
            "version": 4,
            "task_id": task_id,
            "task_name": "post verification continuation",
            "origin_session": "session-continuation",
            "executor_runtime": "codex",
            "interaction_policy": "unattended",
            "pipeline_policy": {
                "name": "lifecycle/default",
                "definition_sha256": definition,
                "completion_policy": "attention",
                "total_pass_limit": 2,
            },
            "review_policy": {
                "mode": "deep",
                "cross_model": False,
                "runtime": "codex",
                "model": "sol",
                "effort": "xhigh",
                "max_verify_iterations": 2,
                "verification_profile": "scoped",
                "verification_profile_sha256": profile_sha,
            },
            "vault_root": str(vault.resolve()),
            "worktree": str(product.resolve()),
            "branch": "task/post-verification",
            "task_surface": "11111111-1111-4111-8111-111111111111",
        },
    )
    write_json(
        product / ".task-summary.json",
        {
            "schema_version": 2,
            "type": "repo-touch",
            "title": "post verification continuation",
            "session": "session-continuation",
            "body": "The verified source is ready for its fresh review.",
            "outcome_disposition": "achieved",
            "outcome_evidence_ids": ["verified"],
            "residual_gap_pointers": [],
        },
    )

    store = OperationStore(vault / ".vault-meta" / "harness")
    route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "xhigh", "executor", "a" * 64
    )
    root_spec = OperationSpec(
        task_id,
        "dispatch-idempotency",
        "dispatch",
        task_id,
        route,
        "packets/root/manifest.json",
        "scoped",
        contract_sha256=definition,
    )
    store.create(root_spec, lane_id="root-lane", run_id="root-run")
    advance(
        store,
        task_id,
        task_id,
        ("preflight", "starting", "running", "awaiting-callback"),
    )
    resources = OwnedResources(
        surface_id="11111111-1111-4111-8111-111111111111",
        process_group=4101,
        supervisor_pid=4102,
        process_identity="1" * 64,
        supervisor_identity="2" * 64,
    )
    record = store.read(task_id, task_id)
    store.save(
        replace(record, resources=resources, revision=record.revision + 1),
        expected_revision=record.revision,
    )
    store.begin_effect(task_id, task_id, "start-provider")
    store.resolve_effect(task_id, task_id, EffectOutcome.SUCCEEDED)
    store.transition(
        task_id,
        task_id,
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )

    retained: list[tuple[str, str]] = []
    for index, axis in enumerate(("openai-engineering", "openai-intent"), 1):
        parent_id = f"{review_id}-{axis}"
        child_id = f"{parent_id}-round"
        parent_spec = OperationSpec(
            parent_id,
            f"parent-{index}",
            "deep-review-correctness" if index == 1 else "deep-review-spec",
            task_id,
            replace(route, profile="reviewer-callback"),
            "packets/review/manifest.json",
            "scoped",
        )
        store.create(parent_spec, lane_id=f"lane-{index}", run_id=f"parent-run-{index}")
        advance(
            store,
            task_id,
            parent_id,
            ("preflight", "starting", "running", "awaiting-callback"),
        )
        store.begin_effect(task_id, parent_id, "request-exit")
        store.resolve_effect(task_id, parent_id, EffectOutcome.SUCCEEDED)
        advance(store, task_id, parent_id, ("cancelling", "exiting", "cancelled"))
        child_spec = OperationSpec(
            child_id,
            f"child-{index}",
            "review-round",
            task_id,
            replace(route, profile="reviewer-ephemeral"),
            "packets/review/manifest.json",
            "scoped",
            parent_operation_id=parent_id,
        )
        store.create(child_spec, lane_id=f"lane-{index}", run_id=f"child-run-{index}")
        advance(
            store,
            task_id,
            child_id,
            ("preflight", "starting", "running", "awaiting-callback"),
        )
        child = store.read(task_id, child_id)
        store.save(
            replace(
                child,
                accepted_callback_id=f"review-{index}",
                accepted_callback_kind="review",
                accepted_callback_sha256=str(index) * 64,
                revision=child.revision + 1,
            ),
            expected_revision=child.revision,
        )
        advance(store, task_id, child_id, ("finalizing", "exiting", "complete"))
        retained.append((parent_id, child_id))

    gate_root = store.root / "review-data" / task_id / task_id
    archive_root = gate_root / "drift-quarantine" / "resolution-quarantine"
    write_json(
        archive_root / "evidence.json",
        {
            "schema_version": 1,
            "status": "quarantined-evidence",
            "operation_id": task_id,
            "review_operation_id": review_id,
            "authorization_record_id": "resolution-quarantine",
            "authorization_record_sha256": "b" * 64,
            "anchor_record_id": "resolution-anchor",
            "anchor_record_sha256": "3" * 64,
            "callback_effects_replayed": 0,
            "provider_effects_replayed": 0,
        },
    )
    for index, (_parent, _child) in enumerate(retained, 1):
        write_json(
            archive_root / "artifacts" / f"callback-{index}.json",
            {
                "schema_version": 1,
                "callback_id": f"review-{index}",
                "payload_sha256": str(index) * 64,
            },
        )
        write_json(
            archive_root / "supported-close" / f"parent-{index}.json",
            {"schema_version": 1, "status": "supported-close-consumed"},
        )
    evidence_sha = sha256(archive_root / "evidence.json")
    progress_path = archive_root / "progress.json"
    write_json(
        progress_path,
        {
            "schema_version": 2,
            "status": "quarantined",
            "evidence_sha256": evidence_sha,
            "cleaned_parents": [parent for parent, _child in retained],
            "terminal_rounds": [child for _parent, child in retained],
            "retirement_receipts": {
                parent: hashlib.sha256(parent.encode()).hexdigest()
                for parent, _child in retained
            },
        },
    )
    gate_path = gate_root / "review-gate.json"
    write_json(
        gate_path,
        {
            "schema_version": 1,
            "dispatch_operation_id": task_id,
            "owner_id": task_id,
            "active_review_operation_id": review_id,
            "status": "attention-required",
            "fresh_reevaluation_used": False,
            "context": {
                "head_sha": source_head,
                "manifest": "packets/old/manifest.json",
                "verification_profile": "scoped",
                "verification_profile_sha256": profile_sha,
            },
            "policy": {
                "enabled": True,
                "depth": "deep",
                "runtime": "codex",
                "model": "gpt-5.6-sol",
                "effort": "xhigh",
                "cross_model": False,
                "max_verify_iterations": 2,
                "purpose": "implementation",
            },
            "attempt": {
                "schema_version": 1,
                "status": "terminal",
                "identity": {
                    "schema_version": 1,
                    "attempt_id": review_id,
                    "exact_head_sha": source_head,
                },
                "terminal": {
                    "schema_version": 1,
                    "result": "attention-required",
                    "exact_head_sha": source_head,
                    "lane_results": [],
                },
            },
            "drift_quarantine": {
                "status": "quarantined",
                "evidence_pointer": (
                    archive_root / "evidence.json"
                ).relative_to(gate_root).as_posix(),
                "evidence_sha256": evidence_sha,
                "authorization_record_id": "resolution-quarantine",
                "authorization_record_sha256": "b" * 64,
            },
            "lanes": [],
            "round_results": {},
            "final_results": {},
            "evidence": {},
        },
    )

    runtime_root = store.root / "owners" / task_id / "runtime" / task_id
    write_json(
        runtime_root / "session.json",
        {
            "schema_version": 1,
            "operation_id": task_id,
            "run_id": "root-run",
            "cwd": str(product.resolve()),
            "product_root": str(product.resolve()),
            "callback_mode": "task-summary",
            "time_budget_seconds": 1800,
            "placement": "workspace",
        },
    )
    write_json(
        runtime_root / "launch.json",
        {
            "schema_version": 1,
            "owner_id": task_id,
            "operation_id": task_id,
            "run_id": "root-run",
            "cwd": str(product.resolve()),
            "product_root": str(product.resolve()),
            "surface_id": resources.surface_id,
            "callback_mode": "task-summary",
        },
    )
    write_json(
        runtime_root / "ready.json",
        {
            "schema_version": 1,
            "status": "ready",
            "pid": resources.process_group,
            "process_group": resources.process_group,
            "process_identity": resources.process_identity,
            "supervisor_pid": resources.supervisor_pid,
            "supervisor_identity": resources.supervisor_identity,
        },
    )
    provider_identity = {
        "schema_version": 1,
        "owner_id": task_id,
        "operation_id": task_id,
        "run_id": "root-run",
        "generation": 1,
        "provider_session_id": "root-run",
        "process_identity": resources.process_identity,
        "surface_id": resources.surface_id,
        "workspace_id": "workspace-test",
        "source_id": f"process:{resources.process_identity}",
    }
    events = runtime_root / "provider-events" / "generation-1"
    write_json(
        events / "events" / "0001.json",
        {
            "schema_version": 1,
            "sequence": 1,
            "kind": "provider-started",
            "effect_id": "",
            "reason": "",
            "result_sha256": "",
            "exit_code": None,
            "identity": provider_identity,
        },
    )
    write_json(
        events / "events" / "0002.json",
        {
            "schema_version": 1,
            "sequence": 2,
            "kind": "input-accepted",
            "effect_id": "c" * 64,
            "reason": "",
            "result_sha256": "",
            "exit_code": None,
            "identity": provider_identity,
        },
    )
    write_json(
        events / "delivery" / "delivery-state.json",
        {
            "schema_version": 1,
            "identity": provider_identity,
            "send_status": "accepted",
            "send_attempts": 1,
            "callback_submits": 0,
            "attention_reason": "",
            "cursor": {
                "schema_version": 1,
                "identity": provider_identity,
                "last_sequence": 2,
                "provider_started": True,
                "input_accepted": True,
                "result_published": False,
                "process_exited": False,
                "resource_closed": False,
                "event_gap": False,
                "turn_stops": 0,
                "profile": "interactive",
            },
            "profile": "interactive",
            "idempotency_key": "c" * 64,
        },
    )
    write_json(
        runtime_root / "callback-error.json",
        {"schema_version": 1, "status": "review-drive-failed"},
    )

    input_sha = hashlib.sha256(
        json.dumps(
            {
                "definition_sha256": definition,
                "head_sha": source_head,
                "profile_sha256": profile_sha,
                "schema_version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    child_spec, verify_lane, verify_run = _pipeline_verify_identity(
        root_spec,
        definition_sha256=definition,
        input_sha256=input_sha,
        profile="scoped",
    )
    store.create(child_spec, lane_id=verify_lane, run_id=verify_run)
    advance(
        store,
        task_id,
        child_spec.operation_id,
        ("preflight", "starting", "running", "verifying"),
    )
    effect_id = _pipeline_verify_effect_id(input_sha)
    store.begin_effect(task_id, child_spec.operation_id, effect_id)
    store.resolve_effect(task_id, child_spec.operation_id, EffectOutcome.SUCCEEDED)
    advance(
        store,
        task_id,
        child_spec.operation_id,
        ("finalizing", "exiting", "complete"),
    )
    attempt = VerificationAttempt(task_id, "scoped", profile_sha, source_head, 0)
    evidence: list[dict[str, object]] = []
    for index in range(1, 4):
        output = (
            runtime_root
            / "pipeline-verification"
            / child_spec.operation_id
            / "evidence"
            / f"scoped-{index}.log"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("ok\n", encoding="utf-8")
        evidence.append(
            {
                "profile": "scoped",
                "profile_sha256": profile_sha,
                "head_sha": source_head,
                "command_id": f"scoped-{index}",
                "cwd": ".",
                "exit_code": 0,
                "started_at": str(index),
                "finished_at": str(index + 1),
                "output_pointer": output.relative_to(runtime_root).as_posix(),
            }
        )
    verification = {
        "schema_version": 2,
        "operation_id": child_spec.operation_id,
        "parent_operation_id": task_id,
        "lane_id": verify_lane,
        "run_id": verify_run,
        "definition_sha256": definition,
        "step_id": "verify",
        "head_sha": source_head,
        "input_sha256": input_sha,
        "profile": "scoped",
        "profile_sha256": profile_sha,
        "effect_id": effect_id,
        "status": "complete",
        "evidence": evidence,
        "verification_attempt": attempt.as_dict(),
        "verification_attempt_sha256": attempt.sha256,
    }
    verification_path = (
        runtime_root
        / "pipeline-verification"
        / child_spec.operation_id
        / "receipt.json"
    )
    write_json(verification_path, verification)
    write_json(runtime_root / "pipeline-step-verify.json", verification)

    drive = hashlib.sha256()
    drive.update(gate_path.read_bytes())
    drive.update(source_head.encode())
    write_json(
        runtime_root / "pipeline-review-start.json",
        {
            "schema_version": 1,
            "operation_id": task_id,
            "definition_sha256": definition,
            "drive_sha256": drive.hexdigest(),
            "status": "pending",
        },
    )

    summary_sha = sha256(product / ".task-summary.json")
    current = store.read(task_id, task_id)
    LivenessController(runtime_root / "liveness").observe(
        LivenessEvidence(
            observed_at=10_000.0,
            process_status="alive",
            prompt_state="non-interactive",
            operation_revision=current.revision,
            operation_state=current.state,
            screen_sha256="9" * 64,
            typed_result_sha256=summary_sha,
        ),
        LivenessPolicy.default(),
    )

    (product / "mechanism.txt").write_text(
        "post-verification continuation repair\n", encoding="utf-8"
    )
    git(product, "add", "mechanism.txt")
    git(product, "commit", "-q", "-m", "mechanism continuation")
    target_head = git(product, "rev-parse", "HEAD")
    return {
        "vault": vault,
        "product": product,
        "store": store,
        "task_id": task_id,
        "review_id": review_id,
        "definition": definition,
        "source_head": source_head,
        "target_head": target_head,
        "runtime_root": runtime_root,
        "gate_path": gate_path,
        "archive_root": archive_root,
        "progress_path": progress_path,
        "resources": resources,
        "verification": verification,
        "verification_path": verification_path,
        "retained": retained,
    }


def synchronize(
    data: dict[str, object],
    *,
    now: float,
    fault=None,
    process: ExactProcess | None = None,
    authorization_record_id: str = "resolution-post-verification",
    authorization_record_sha256: str = "a" * 64,
) -> tuple[dict[str, object], ExactProcess, ExactCmux, RecoveryEffect]:
    resources = data["resources"]
    assert isinstance(resources, OwnedResources)
    process = process or ExactProcess(resources)
    cmux = ExactCmux(resources.surface_id)
    recovery = RecoveryEffect(data)
    receipt = synchronize_post_verification_review_drive(
        data["product"],
        store=data["store"],
        operation_id=str(data["task_id"]),
        active_review_operation_id=str(data["review_id"]),
        authorization_record_id=authorization_record_id,
        authorization_record_sha256=authorization_record_sha256,
        process_adapter=process,
        cmux_adapter=cmux,
        recover_review=recovery,
        now=now,
        _fault_hook=fault,
    )
    return receipt, process, cmux, recovery


def post_fresh_authorization(
    data: dict[str, object],
    *,
    authorization_record_id: str = "resolution-post-verification",
    authorization_record_sha256: str = "a" * 64,
) -> PostFreshPublicationSyncAuthorization:
    drift = DriftQuarantineAuthorization(
        "review-accepted",
        "4" * 64,
        "review-artifact",
        "5" * 64,
        str(data["source_head"]),
        "resolution-anchor",
        "3" * 64,
        "resolution-quarantine",
        "b" * 64,
    )
    signal = SignalFreeRetirementAuthorization(
        drift, "resolution-signal", "6" * 64
    )
    parents = tuple(parent for parent, _child in data["retained"])
    assert len(parents) == 2
    supported = SupportedCloseRetirementAuthorization(
        signal,
        (str(parents[0]), str(parents[1])),
        "resolution-supported-close",
        "7" * 64,
    )
    continuation = PostVerificationReviewDriveAuthorization(
        supported,
        str(data["review_id"]),
        str(data["task_id"]),
        "resolution-fresh-binding",
        "8" * 64,
        authorization_record_id,
        authorization_record_sha256,
    )
    return PostFreshPublicationSyncAuthorization(
        continuation,
        "resolution-post-fresh-publication",
        "c" * 64,
    )


def prepare_partial_fresh_publication(
    data: dict[str, object],
    *,
    coordinator_provenance: dict[str, str] | None = None,
) -> PostFreshPublicationSyncAuthorization:
    def stop_prepared(stage: str) -> None:
        if stage == "prepared":
            raise RuntimeError("retain prepared continuation")

    try:
        synchronize(
            data,
            now=40_000.0,
            fault=stop_prepared,
            authorization_record_id=(
                coordinator_provenance["record_id"]
                if coordinator_provenance
                else "resolution-post-verification"
            ),
            authorization_record_sha256=(
                coordinator_provenance["record_sha256"]
                if coordinator_provenance
                else "a" * 64
            ),
        )
    except RuntimeError as exc:
        assert str(exc) == "retain prepared continuation"
    else:
        raise AssertionError("prepared continuation failpoint did not fire")

    store = data["store"]
    assert isinstance(store, OperationStore)
    task_id = str(data["task_id"])
    review_id = str(data["review_id"])
    product = data["product"]
    gate_path = data["gate_path"]
    assert isinstance(product, Path) and isinstance(gate_path, Path)
    previous_context = "f" * 64
    next_context = "e" * 64
    role = f"fresh:context:{next_context}"
    suffix = f"-fresh-{hashlib.sha256(role.encode()).hexdigest()[:8]}"
    fresh_id = f"{review_id[:128-len(suffix)]}{suffix}"
    authorization_path = gate_path.parent / "fresh-boundary.json"
    authorization_payload = {
            "schema_version": 2,
            "operation_id": review_id,
            "dispatch_operation_id": task_id,
            "kind": "context",
            "previous_context_sha256": previous_context,
            "next_context_sha256": next_context,
            "reason": "authorized post-verification continuation",
            "authorization_provenance": (
                "coordinator-approved"
                if coordinator_provenance
                else "pipeline-verification"
            ),
            "verification_operation_id": (
                coordinator_provenance["operation_id"]
                if coordinator_provenance
                else data["verification"]["operation_id"]
            ),
            "verification_receipt_sha256": (
                coordinator_provenance["record_sha256"]
                if coordinator_provenance
                else sha256(data["verification_path"])
            ),
            "status": "authorized",
        }
    write_json(authorization_path, authorization_payload)
    route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "xhigh", "reviewer-callback", "a" * 64
    )
    lanes: list[dict[str, object]] = []
    for index, axis in enumerate(("openai-intent", "openai-engineering"), 1):
        parent_id = f"{fresh_id}-{axis}"
        child_id = f"{parent_id}-round"
        parent_spec = OperationSpec(
            parent_id,
            hashlib.sha256(f"{parent_id}:parent".encode()).hexdigest(),
            review_parent_kind(axis),
            task_id,
            route,
            f"packets/fresh/{axis}.json",
            "scoped",
        )
        lane_id = hashlib.sha256(f"{parent_id}:lane".encode()).hexdigest()[:32]
        parent_run = hashlib.sha256(f"{parent_id}:run".encode()).hexdigest()[:32]
        store.create(parent_spec, lane_id=lane_id, run_id=parent_run)
        advance(store, task_id, parent_id, ("preflight", "starting"))
        resources = OwnedResources(
            surface_id=f"{index + 1}" * 8
            + "-2222-4222-8222-"
            + f"{index + 1}" * 12,
            process_group=5100 + index,
            supervisor_pid=5200 + index,
            process_identity=str(index + 5) * 64,
            supervisor_identity=str(index + 7) * 64,
        )
        parent = store.read(task_id, parent_id)
        store.save(
            replace(parent, resources=resources, revision=parent.revision + 1),
            expected_revision=parent.revision,
        )
        store.begin_effect(task_id, parent_id, "start-provider")
        store.resolve_effect(task_id, parent_id, EffectOutcome.SUCCEEDED)
        advance(store, task_id, parent_id, ("running", "awaiting-callback"))
        child_spec = OperationSpec(
            child_id,
            hashlib.sha256(f"{child_id}:round".encode()).hexdigest(),
            "review-round",
            task_id,
            route,
            f"packets/fresh/{axis}.json",
            "scoped",
            parent_operation_id=parent_id,
        )
        child_run = hashlib.sha256(f"{child_id}:run".encode()).hexdigest()[:32]
        store.create(child_spec, lane_id=lane_id, run_id=child_run)
        advance(
            store,
            task_id,
            child_id,
            ("preflight", "starting", "running", "awaiting-callback"),
        )
        runtime_root = store.root / "owners" / task_id / "runtime" / parent_id
        write_json(
            runtime_root / "session.json",
            {
                "schema_version": 1,
                "operation_id": parent_id,
                "run_id": parent_run,
                "cwd": str(product.resolve()),
                "product_root": str(product.resolve()),
                "callback_mode": "envelope",
                "placement": "workspace",
            },
        )
        write_json(
            runtime_root / "launch.json",
            {
                "schema_version": 1,
                "owner_id": task_id,
                "operation_id": parent_id,
                "run_id": parent_run,
                "cwd": str(product.resolve()),
                "product_root": str(product.resolve()),
                "surface_id": resources.surface_id,
                "runtime": "codex",
                "callback_mode": "envelope",
                "reviewer_sandbox": True,
            },
        )
        write_json(
            runtime_root / "ready.json",
            {
                "schema_version": 1,
                "status": "ready",
                "pid": resources.process_group,
                "process_group": resources.process_group,
                "process_identity": resources.process_identity,
                "supervisor_pid": resources.supervisor_pid,
                "supervisor_identity": resources.supervisor_identity,
            },
        )
        write_json(
            runtime_root / "callback-target.json",
            {
                "schema_version": 1,
                "generation": 2,
                "operation_id": child_id,
                "run_id": child_run,
                "callback_pointer": f"callbacks/{axis}/.review-callback.json",
            },
        )
        identity = {
            "schema_version": 1,
            "owner_id": task_id,
            "operation_id": parent_id,
            "run_id": parent_run,
            "generation": 2,
            "provider_session_id": parent_run,
            "process_identity": resources.process_identity,
            "surface_id": resources.surface_id,
            "workspace_id": f"workspace-{index}",
            "source_id": f"process:{resources.process_identity}",
        }
        generation = runtime_root / "provider-events" / "generation-2"
        for sequence, kind in ((1, "provider-started"), (2, "input-accepted")):
            write_json(
                generation / "events" / f"{sequence:04d}.json",
                {
                    "schema_version": 1,
                    "sequence": sequence,
                    "kind": kind,
                    "effect_id": "" if sequence == 1 else "d" * 64,
                    "reason": "",
                    "result_sha256": "",
                    "exit_code": None,
                    "identity": identity,
                },
            )
        write_json(
            generation / "delivery" / "delivery-state.json",
            {
                "schema_version": 1,
                "identity": identity,
                "send_status": "accepted",
                "send_attempts": 1,
                "callback_submits": 0,
                "attention_reason": "",
                "cursor": {
                    "schema_version": 1,
                    "identity": identity,
                    "last_sequence": 2,
                    "provider_started": True,
                    "input_accepted": True,
                    "result_published": False,
                    "process_exited": False,
                    "resource_closed": False,
                    "event_gap": False,
                    "turn_stops": 0,
                    "profile": "interactive",
                },
                "profile": "interactive",
                "idempotency_key": "d" * 64,
            },
        )
        lanes.append(
            {
                "axis": axis,
                "operation_id": parent_id,
                "lane_id": lane_id,
                "run_id": parent_run,
                "surface_id": resources.surface_id,
                "checkpoint": "",
                "verification_iteration": 0,
                "state": "awaiting-callback",
            }
        )
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    gate.update(
        {
            "status": "attention-required",
            "fresh_reevaluation_used": True,
            "resolution_operation_id": review_id,
            "active_review_operation_id": fresh_id,
            "policy": {
                **gate["policy"],
                "depth": "deep",
                "runtime": "codex",
                "model": "gpt-5.6-sol",
                "effort": "xhigh",
                "cross_model": False,
                "max_verify_iterations": 0,
            },
            "context": {
                **gate["context"],
                "head_sha": data["target_head"],
                "sha256": next_context,
            },
            "fresh_boundary": {
                "kind": "context",
                "previous_context_sha256": previous_context,
                "next_context_sha256": next_context,
                "reason": "authorized post-verification continuation",
            },
            "fresh_boundary_authorization": {
                "pointer": authorization_path.name,
                "sha256": sha256(authorization_path),
                "status": "authorized",
            },
            "lanes": lanes,
        }
    )
    write_json(gate_path, gate)
    return post_fresh_authorization(
        data,
        authorization_record_id=(
            coordinator_provenance["record_id"]
            if coordinator_provenance
            else "resolution-post-verification"
        ),
        authorization_record_sha256=(
            coordinator_provenance["record_sha256"]
            if coordinator_provenance
            else "a" * 64
        ),
    )


def advance_fresh_round(
    data: dict[str, object], lane_index: int, target_state: str
) -> tuple[str, str]:
    """Persist the real callback/provider chain for one monotonic fresh round."""

    if target_state not in {"verifying", "finalizing", "complete"}:
        raise AssertionError(f"unsupported fresh round test state: {target_state}")
    store = data["store"]
    gate_path = data["gate_path"]
    assert isinstance(store, OperationStore) and isinstance(gate_path, Path)
    task_id = str(data["task_id"])
    lane = json.loads(gate_path.read_text(encoding="utf-8"))["lanes"][lane_index]
    parent_id = str(lane["operation_id"])
    child = next(
        record
        for record in store.list(task_id)
        if record.spec.parent_operation_id == parent_id
        and record.spec.kind == "review-round"
    )
    verdict = "changes-requested" if target_state == "verifying" else "approve"
    payload = {
        "parent_session_operation_id": parent_id,
        "verdict": verdict,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload_sha = hashlib.sha256(encoded).hexdigest()
    callback_id = f"review-{payload_sha[:24]}"
    CallbackBroker(store, task_id).accept(
        CallbackEnvelope(
            callback_id,
            child.spec.operation_id,
            child.run_id,
            "review",
            payload,
            payload_sha,
        ),
        deadline_operation_id=parent_id,
    )
    if target_state == "complete":
        for state in ("exiting", "complete"):
            store.transition(task_id, child.spec.operation_id, state)

    runtime_root = store.root / "owners" / task_id / "runtime" / parent_id
    write_json(
        runtime_root / "callback-receipt.json",
        {
            "schema_version": 1,
            "status": "accepted",
            "operation_id": child.spec.operation_id,
            "run_id": child.run_id,
            "callback_id": callback_id,
            "payload_sha256": payload_sha,
            "generation": 2,
        },
    )
    generation = runtime_root / "provider-events" / "generation-2"
    identity = json.loads(
        (generation / "events" / "0001.json").read_text(encoding="utf-8")
    )["identity"]
    write_json(
        generation / "events" / "0003.json",
        {
            "schema_version": 1,
            "sequence": 3,
            "kind": "turn-stopped",
            "effect_id": "",
            "reason": "",
            "result_sha256": "",
            "exit_code": None,
            "identity": identity,
        },
    )
    write_json(
        generation / "events" / "0004.json",
        {
            "schema_version": 1,
            "sequence": 4,
            "kind": "result-published",
            "effect_id": "",
            "reason": "",
            "result_sha256": hashlib.sha256(
                f"{child.spec.operation_id}:result".encode()
            ).hexdigest(),
            "exit_code": None,
            "identity": identity,
        },
    )
    delivery_path = generation / "delivery" / "delivery-state.json"
    delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
    delivery["callback_submits"] = 1
    delivery["cursor"].update(
        {
            "last_sequence": 4,
            "result_published": True,
            "turn_stops": 1,
        }
    )
    write_json(delivery_path, delivery)
    return parent_id, child.spec.operation_id


def reject(label: str, data: dict[str, object], mutate) -> None:
    mutate(data)
    resources = data["resources"]
    assert isinstance(resources, OwnedResources)
    process = ExactProcess(resources)
    cmux = ExactCmux(resources.surface_id)
    recovery = RecoveryEffect(data)
    try:
        synchronize_post_verification_review_drive(
            data["product"],
            store=data["store"],
            operation_id=str(data["task_id"]),
            active_review_operation_id=str(data["review_id"]),
            authorization_record_id="resolution-post-verification",
            authorization_record_sha256="a" * 64,
            process_adapter=process,
            cmux_adapter=cmux,
            recover_review=recovery,
            now=30_000.0,
        )
    except PostVerificationReviewDriveError:
        check(
            label,
            recovery.provider_starts == 0
            and not process.signals
            and not cmux.closes,
        )
    else:
        raise AssertionError(f"{label}: invalid boundary was accepted")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="post-verification-drive-") as raw:
        data = fixture(Path(raw) / "happy")
        store = data["store"]
        assert isinstance(store, OperationStore)
        runtime_root = data["runtime_root"]
        archive_root = data["archive_root"]
        assert isinstance(runtime_root, Path)
        assert isinstance(archive_root, Path)
        archive_before = tree_hash(archive_root, exclude=("progress.json",))
        provider_before = tree_hash(runtime_root / "provider-events")
        retained_before = {
            operation: sha256(
                store.root
                / "owners"
                / str(data["task_id"])
                / "operations"
                / f"{operation}.json"
            )
            for pair in data["retained"]
            for operation in pair
        }
        receipt, process, cmux, recovery = synchronize(data, now=20_000.0)
        record = store.read(str(data["task_id"]), str(data["task_id"]))
        live = LivenessController(runtime_root / "liveness").current_state()
        check(
            "exact live continuation applies one fresh review after completed verification",
            receipt["status"] == "applied"
            and receipt["source_verification_head_sha"] == data["source_head"]
            and receipt["target_head_sha"] == data["target_head"]
            and record.state == "awaiting-callback"
            and live is not None
            and live.operation_state == "awaiting-callback"
            and live.operation_revision == record.revision
            and record.deadline_at == 21_800.0
            and recovery.provider_starts == 2,
            (receipt, record, live, recovery.provider_starts),
        )
        check(
            "continuation preserves archive, retained receipts, and dispatch provider",
            tree_hash(archive_root, exclude=("progress.json",)) == archive_before
            and tree_hash(runtime_root / "provider-events") == provider_before
            and retained_before
            == {
                operation: sha256(
                    store.root
                    / "owners"
                    / str(data["task_id"])
                    / "operations"
                    / f"{operation}.json"
                )
                for pair in data["retained"]
                for operation in pair
            }
            and not process.signals
            and not cmux.closes,
        )
        mixed_data = fixture(Path(raw) / "mixed-darwin")
        mixed_resources = mixed_data["resources"]
        assert isinstance(mixed_resources, OwnedResources)
        mixed_receipt, mixed_process, _mixed_cmux, mixed_recovery = synchronize(
            mixed_data,
            now=20_050.0,
            process=MixedDarwinProcess(mixed_resources),
        )
        check(
            "post-verification recovery accepts only the paired exact-live fallback",
            mixed_receipt["status"] == "applied"
            and mixed_recovery.provider_starts == 2
            and not mixed_process.signals,
            (mixed_receipt, mixed_recovery.provider_starts),
        )
        continued = continued_verification_receipt(
            runtime_root,
            operation_id=str(data["task_id"]),
            current_head=str(data["target_head"]),
            controller_receipt=data["verification"],
        )
        marker = post_verification_review_marker(
            runtime_root,
            operation_id=str(data["task_id"]),
            definition_sha256=str(data["definition"]),
        )
        check(
            "applied receipt reuses only the bound completion and selects a fresh marker",
            continued == data["verification"]
            and marker is not None
            and marker["status"] == "started"
            and marker["path"].name
            == "pipeline-review-post-verification-start.json",
            (continued, marker),
        )
        marker_worker = RuntimeWorkerSummaryMixin()
        marker_worker.spec_path = runtime_root / "runtime.json"
        marker_worker.spec = {"operation_id": str(data["task_id"])}
        marker_worker.pipeline = SimpleNamespace(
            definition_sha256=str(data["definition"])
        )
        loaded_marker = marker_worker.load_review_marker()
        check(
            "production worker selects the started fresh marker without changing the failed marker",
            loaded_marker is not None
            and loaded_marker["status"] == "started"
            and marker_worker.marker_path.name
            == "pipeline-review-post-verification-start.json",
            (loaded_marker, marker_worker.marker_path),
        )

        class ReviewAdvanceProbe(RuntimeWorkerSummaryMixin):
            def __init__(self) -> None:
                self.review = SimpleNamespace(
                    status="reviewing",
                    gate_root=Path(data["gate_path"]).parent,
                )
                self.spec = {
                    "operation_id": str(data["task_id"]),
                    "cwd": data["product"],
                }
                self.trusted_vault = data["vault"]
                self.drive_calls = 0

            def drive_review(self) -> bool:
                self.drive_calls += 1
                return True

            def review_gate_state(self) -> dict[str, object]:
                return json.loads(Path(data["gate_path"]).read_text(encoding="utf-8"))

            def review_drive_sha256(self) -> str:
                assert loaded_marker is not None
                return str(loaded_marker["drive_sha256"])

        advance_probe = ReviewAdvanceProbe()
        advanced = advance_probe.advance_review_boundary(
            SimpleNamespace(marker=loaded_marker), True
        )
        check(
            "started fresh marker prevents a second review drive",
            advanced and advance_probe.drive_calls == 0,
            advance_probe.drive_calls,
        )

        class VerificationCarryProbe(RuntimeWorkerSummaryMixin):
            def __init__(self) -> None:
                self.spec_path = runtime_root / "runtime.json"
                self.spec = {"operation_id": str(data["task_id"])}
                steps = (
                    SimpleNamespace(primitive_id="model_step"),
                    SimpleNamespace(primitive_id="verify"),
                    SimpleNamespace(primitive_id="review"),
                )
                self.pipeline = SimpleNamespace(
                    definition_sha256=str(data["definition"]),
                    definition=SimpleNamespace(steps=steps),
                )
                self.operation = SimpleNamespace(
                    spec=SimpleNamespace(contract_sha256=str(data["definition"]))
                )
                self.is_custom_pipeline = False
                self.verification_head = str(data["target_head"])
                self.resolve_calls = 0

            def load_summary_contract(self, _raw: bytes) -> dict[str, object]:
                return {"schema_version": 2}

            def load_review_marker(self) -> dict[str, object]:
                return {"status": "pending"}

            def bind_verification_contract(self, _step: object) -> None:
                self.verification_head = str(data["target_head"])

            def controller_verification_receipt(self) -> dict[str, object]:
                return data["verification"]

            def handle_prior_failed_verification(self, _previous: object) -> bool:
                return True

            def resolve_current_verification(self, _step: object):
                self.resolve_calls += 1
                return None, False

        carry_probe = VerificationCarryProbe()
        pipeline_state = carry_probe.build_summary_pipeline_state(b"{}")
        check(
            "production summary path does not repeat completed scoped verification",
            pipeline_state is not None
            and pipeline_state.existing_verification == data["verification"]
            and carry_probe.resolve_calls == 0,
            (pipeline_state, carry_probe.resolve_calls),
        )
        revision = record.revision
        replay, _process2, _cmux2, replay_recovery = synchronize(
            data, now=20_100.0
        )
        check(
            "continuation replay is idempotent and starts at most one fresh review",
            replay == receipt
            and store.read(str(data["task_id"]), str(data["task_id"])).revision
            == revision
            and replay_recovery.provider_starts == 0,
            (replay, replay_recovery.provider_starts),
        )

    with tempfile.TemporaryDirectory(prefix="post-verification-crash-") as raw:
        data = fixture(Path(raw) / "operation")
        stages: list[str] = []

        def crash(stage: str) -> None:
            stages.append(stage)
            if stage == "operation-written":
                raise RuntimeError("simulated operation publication crash")

        try:
            synchronize(data, now=22_000.0, fault=crash)
        except RuntimeError as exc:
            check(
                "crash is injected after exact operation publication",
                str(exc) == "simulated operation publication crash",
            )
        else:
            raise AssertionError("operation crash did not fire")
        store = data["store"]
        runtime_root = data["runtime_root"]
        assert isinstance(store, OperationStore)
        assert isinstance(runtime_root, Path)
        partial = store.read(str(data["task_id"]), str(data["task_id"]))
        partial_live = LivenessController(runtime_root / "liveness").current_state()
        check(
            "write-ahead transition retains the recoverable split publication",
            partial.state == "awaiting-callback"
            and partial_live is not None
            and partial_live.operation_state == "attention-required",
            (partial, partial_live, stages),
        )
        recovered, _process, _cmux, replay_recovery = synchronize(
            data, now=23_000.0
        )
        recovered_record = store.read(
            str(data["task_id"]), str(data["task_id"])
        )
        recovered_live = LivenessController(runtime_root / "liveness").current_state()
        check(
            "crash restart converges without a second transition or provider replay",
            recovered["status"] == "applied"
            and recovered_record.revision == partial.revision
            and recovered_live is not None
            and recovered_live.operation_revision == partial.revision
            and replay_recovery.provider_starts == 0,
            (recovered, recovered_record, recovered_live),
        )

    with tempfile.TemporaryDirectory(prefix="post-verification-prepare-") as raw:
        data = fixture(Path(raw) / "prepared")

        def crash_prepared(stage: str) -> None:
            if stage == "prepared":
                raise RuntimeError("simulated prepared crash")

        try:
            synchronize(data, now=24_000.0, fault=crash_prepared)
        except RuntimeError:
            pass
        else:
            raise AssertionError("prepared crash did not fire")
        store = data["store"]
        assert isinstance(store, OperationStore)
        check(
            "prepared crash occurs before any fresh review effect",
            store.read(str(data["task_id"]), str(data["task_id"])).state
            == "attention-required",
        )
        recovered, _process, _cmux, recovery = synchronize(
            data, now=24_000.0
        )
        check(
            "prepared restart launches the one authorized fresh review once",
            recovered["status"] == "applied" and recovery.provider_starts == 2,
            (recovered, recovery.provider_starts),
        )

    with tempfile.TemporaryDirectory(prefix="post-fresh-publication-") as raw:
        data = fixture(Path(raw) / "happy")
        authorization = prepare_partial_fresh_publication(data)
        store = data["store"]
        runtime_root = data["runtime_root"]
        archive_root = data["archive_root"]
        assert isinstance(store, OperationStore)
        assert isinstance(runtime_root, Path) and isinstance(archive_root, Path)
        product = data["product"]
        assert isinstance(product, Path)
        (product / "publication-repair.txt").write_text(
            "typed post-fresh repair\n", encoding="utf-8"
        )
        git(product, "add", "publication-repair.txt")
        git(product, "commit", "-q", "-m", "post-fresh mechanism repair")
        repair_head = git(product, "rev-parse", "HEAD")
        repair_tree = git(product, "rev-parse", "HEAD^{tree}")
        provider_roots = [
            store.root
            / "owners"
            / str(data["task_id"])
            / "runtime"
            / str(lane["operation_id"])
            / "provider-events"
            for lane in json.loads(
                Path(data["gate_path"]).read_text(encoding="utf-8")
            )["lanes"]
        ]
        provider_before = [tree_hash(root) for root in provider_roots]
        archive_before = tree_hash(archive_root, exclude=("progress.json",))
        receipt = synchronize_post_fresh_publication(
            data["product"],
            store=store,
            operation_id=str(data["task_id"]),
            authorization=authorization,
            now=41_000.0,
        )
        gate = json.loads(Path(data["gate_path"]).read_text(encoding="utf-8"))
        progress = json.loads(
            Path(data["progress_path"]).read_text(encoding="utf-8")
        )
        root_record = store.read(str(data["task_id"]), str(data["task_id"]))
        check(
            "post-fresh synchronization resumes callback waiting for existing lanes",
            receipt["status"] == "applied"
            and receipt["reviews_started"] == 0
            and receipt["repair_head_sha"] == repair_head
            and receipt["repair_tree_sha"] == repair_tree
            and receipt["repair_head_sha"] != data["target_head"]
            and gate["status"] == "reviewing"
            and gate["fresh_reevaluation_used"] is True
            and progress["status"] == "fresh-review-started"
            and root_record.state == "awaiting-callback"
            and (runtime_root / "pipeline-review-post-verification-start.json").is_file(),
            (receipt, gate, progress, root_record),
        )
        check(
            "post-fresh synchronization preserves archive and provider effects",
            archive_before
            == tree_hash(archive_root, exclude=("progress.json",))
            and provider_before == [tree_hash(root) for root in provider_roots],
        )
        revision = root_record.revision
        replay = synchronize_post_fresh_publication(
            data["product"],
            store=store,
            operation_id=str(data["task_id"]),
            authorization=authorization,
            now=42_000.0,
        )
        check(
            "post-fresh synchronization replay is idempotent and starts no review",
            replay == receipt
            and store.read(str(data["task_id"]), str(data["task_id"])).revision
            == revision
            and replay["provider_effects_replayed"] == 0
            and replay["callback_effects_replayed"] == 0
            and replay["reviews_started"] == 0,
            replay,
        )

    coordinator = {
        "operation_id": "coordinator-provenance-exact",
        "record_id": "resolution-coordinator-provenance",
        "record_sha256": "9" * 64,
    }

    def provenance_overrides(data: dict[str, object]) -> dict[str, object]:
        verification = data["verification"]
        assert isinstance(verification, dict)
        return {
            "COORDINATOR_PROVENANCE_OPERATION_ID": coordinator["operation_id"],
            "COORDINATOR_PROVENANCE_RECORD_ID": coordinator["record_id"],
            "COORDINATOR_PROVENANCE_SHA256": coordinator["record_sha256"],
            "SCOPED_VERIFICATION_OPERATION_ID": verification["operation_id"],
            "SCOPED_VERIFICATION_RECEIPT_SHA256": sha256(
                Path(data["verification_path"])
            ),
        }

    with tempfile.TemporaryDirectory(prefix="post-fresh-dual-provenance-") as raw:
        data = fixture(Path(raw) / "happy")
        authorization = prepare_partial_fresh_publication(
            data, coordinator_provenance=coordinator
        )
        gate = json.loads(Path(data["gate_path"]).read_text(encoding="utf-8"))
        artifact_path = Path(data["gate_path"]).parent / gate[
            "fresh_boundary_authorization"
        ]["pointer"]
        artifact_before = artifact_path.read_bytes()
        with patch.multiple(
            "task_review_provenance_contract",
            **provenance_overrides(data),
        ):
            receipt = synchronize_post_fresh_publication(
                data["product"],
                store=data["store"],
                operation_id=str(data["task_id"]),
                authorization=authorization,
                now=42_010.0,
            )
            replay = synchronize_post_fresh_publication(
                data["product"],
                store=data["store"],
                operation_id=str(data["task_id"]),
                authorization=authorization,
                now=42_020.0,
            )
        check(
            "post-fresh synchronization preserves exact dual provenance byte-for-byte",
            receipt == replay
            and receipt["status"] == "applied"
            and artifact_path.read_bytes() == artifact_before
            and receipt["os_signals_sent"] == 0
            and receipt["cmux_signals_sent"] == 0
            and receipt["callback_effects_replayed"] == 0
            and receipt["provider_effects_replayed"] == 0
            and receipt["reviews_started"] == 0,
            receipt,
        )

    for rejected_label, mutate_artifact, override_drift in (
        (
            "missing coordinator provenance",
            lambda value: value.pop("authorization_provenance"),
            {},
        ),
        (
            "mismatched coordinator provenance",
            lambda value: value.update(
                {"verification_operation_id": "coordinator-unrelated"}
            ),
            {},
        ),
        (
            "broadened coordinator provenance",
            lambda value: value.update({"provider_relaunch": True}),
            {},
        ),
        (
            "mismatched scoped verification binding",
            lambda _value: None,
            {"SCOPED_VERIFICATION_RECEIPT_SHA256": "8" * 64},
        ),
    ):
        with tempfile.TemporaryDirectory(
            prefix=f"post-fresh-provenance-{rejected_label.split()[0]}."
        ) as raw:
            data = fixture(Path(raw) / "rejected")
            authorization = prepare_partial_fresh_publication(
                data, coordinator_provenance=coordinator
            )
            gate_path = Path(data["gate_path"])
            progress_path = Path(data["progress_path"])
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            artifact_path = gate_path.parent / gate[
                "fresh_boundary_authorization"
            ]["pointer"]
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            mutate_artifact(artifact)
            write_json(artifact_path, artifact)
            gate["fresh_boundary_authorization"]["sha256"] = sha256(artifact_path)
            write_json(gate_path, gate)
            gate_before = gate_path.read_bytes()
            progress_before = progress_path.read_bytes()
            root_before = data["store"].read(
                str(data["task_id"]), str(data["task_id"])
            )
            overrides = {**provenance_overrides(data), **override_drift}
            try:
                with patch.multiple(
                    "task_review_provenance_contract",
                    **overrides,
                ):
                    synchronize_post_fresh_publication(
                        data["product"],
                        store=data["store"],
                        operation_id=str(data["task_id"]),
                        authorization=authorization,
                        now=42_030.0,
                    )
            except PostVerificationReviewDriveError:
                check(
                    f"post-fresh synchronization rejects {rejected_label} before effect",
                    gate_path.read_bytes() == gate_before
                    and progress_path.read_bytes() == progress_before
                    and data["store"].read(
                        str(data["task_id"]), str(data["task_id"])
                    )
                    == root_before
                    and not (
                        Path(data["runtime_root"])
                        / "post-fresh-publication-sync.json"
                    ).exists(),
                )
            else:
                raise AssertionError(f"accepted {rejected_label}")

    for progressed_state in ("verifying", "finalizing", "complete"):
        with tempfile.TemporaryDirectory(
            prefix=f"post-fresh-{progressed_state}-"
        ) as raw:
            data = fixture(Path(raw) / progressed_state)
            authorization = prepare_partial_fresh_publication(data)
            store = data["store"]
            assert isinstance(store, OperationStore)
            pairs = [
                advance_fresh_round(data, index, progressed_state)
                for index in range(2)
            ]
            parent_before = {
                parent: sha256(
                    store.root
                    / "owners"
                    / str(data["task_id"])
                    / "operations"
                    / f"{parent}.json"
                )
                for parent, _child in pairs
            }
            round_before = {
                child: sha256(
                    store.root
                    / "owners"
                    / str(data["task_id"])
                    / "operations"
                    / f"{child}.json"
                )
                for _parent, child in pairs
            }
            provider_before = {
                parent: tree_hash(
                    store.root
                    / "owners"
                    / str(data["task_id"])
                    / "runtime"
                    / parent
                    / "provider-events"
                )
                for parent, _child in pairs
            }
            receipt = synchronize_post_fresh_publication(
                data["product"],
                store=store,
                operation_id=str(data["task_id"]),
                authorization=authorization,
                now=42_100.0,
            )
            check(
                f"post-fresh synchronization accepts exact monotonic {progressed_state} rounds",
                receipt["status"] == "applied"
                and {
                    str(binding["round_state"])
                    for binding in receipt["fresh_lane_bindings"]
                }
                == {progressed_state}
                and all(
                    binding["provider_phase"] == "result-published"
                    and binding["accepted_callback_id"]
                    and binding["accepted_callback_sha256"]
                    and binding["callback_receipt_sha256"]
                    for binding in receipt["fresh_lane_bindings"]
                )
                and all(receipt[key] == 0 for key in (
                    "os_signals_sent",
                    "cmux_signals_sent",
                    "callback_effects_replayed",
                    "provider_effects_replayed",
                    "reviews_started",
                )),
                receipt,
            )
            check(
                f"post-fresh {progressed_state} synchronization preserves exact parents and callback/provider effects",
                parent_before
                == {
                    parent: sha256(
                        store.root
                        / "owners"
                        / str(data["task_id"])
                        / "operations"
                        / f"{parent}.json"
                    )
                    for parent, _child in pairs
                }
                and round_before
                == {
                    child: sha256(
                        store.root
                        / "owners"
                        / str(data["task_id"])
                        / "operations"
                        / f"{child}.json"
                    )
                    for _parent, child in pairs
                }
                and provider_before
                == {
                    parent: tree_hash(
                        store.root
                        / "owners"
                        / str(data["task_id"])
                        / "runtime"
                        / parent
                        / "provider-events"
                    )
                    for parent, _child in pairs
                },
            )

    with tempfile.TemporaryDirectory(prefix="post-fresh-progress-crash-") as raw:
        data = fixture(Path(raw) / "progress")
        authorization = prepare_partial_fresh_publication(data)

        def crash_after_sync_prepared(stage: str) -> None:
            if stage == "prepared":
                raise RuntimeError("simulated pre-progress sync crash")

        try:
            synchronize_post_fresh_publication(
                data["product"],
                store=data["store"],
                operation_id=str(data["task_id"]),
                authorization=authorization,
                now=42_200.0,
                fault_observer=crash_after_sync_prepared,
            )
        except RuntimeError as exc:
            assert str(exc) == "simulated pre-progress sync crash"
        else:
            raise AssertionError("pre-progress sync crash did not fire")
        advance_fresh_round(data, 0, "verifying")
        advance_fresh_round(data, 1, "complete")
        recovered = synchronize_post_fresh_publication(
            data["product"],
            store=data["store"],
            operation_id=str(data["task_id"]),
            authorization=authorization,
            now=42_300.0,
        )
        check(
            "post-fresh prepared crash accepts only monotonic child advancement",
            recovered["status"] == "applied"
            and {binding["round_state"] for binding in recovered["fresh_lane_bindings"]}
            == {"verifying", "complete"}
            and recovered["reviews_started"] == 0
            and recovered["provider_effects_replayed"] == 0
            and recovered["callback_effects_replayed"] == 0,
            recovered,
        )

    with tempfile.TemporaryDirectory(prefix="post-fresh-progress-regression-") as raw:
        data = fixture(Path(raw) / "regression")
        authorization = prepare_partial_fresh_publication(data)
        _parent, child_id = advance_fresh_round(data, 0, "finalizing")
        advance_fresh_round(data, 1, "finalizing")

        def crash_after_advanced_prepared(stage: str) -> None:
            if stage == "prepared":
                raise RuntimeError("simulated advanced prepared crash")

        try:
            synchronize_post_fresh_publication(
                data["product"],
                store=data["store"],
                operation_id=str(data["task_id"]),
                authorization=authorization,
                now=42_400.0,
                fault_observer=crash_after_advanced_prepared,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("advanced prepared crash did not fire")
        store = data["store"]
        assert isinstance(store, OperationStore)
        regressed = store.read(str(data["task_id"]), child_id)
        store.save(
            replace(regressed, state="verifying", revision=regressed.revision + 1),
            expected_revision=regressed.revision,
        )
        try:
            synchronize_post_fresh_publication(
                data["product"],
                store=store,
                operation_id=str(data["task_id"]),
                authorization=authorization,
                now=42_500.0,
            )
        except PostVerificationReviewDriveError:
            check(
                "post-fresh prepared recovery rejects child state regression without publication",
                json.loads(Path(data["gate_path"]).read_text(encoding="utf-8"))[
                    "status"
                ]
                == "attention-required"
                and not (
                    data["runtime_root"]
                    / "pipeline-review-post-verification-start.json"
                ).exists(),
            )
        else:
            raise AssertionError("fresh child state regression was accepted")

    with tempfile.TemporaryDirectory(prefix="post-fresh-callback-drift-") as raw:
        data = fixture(Path(raw) / "callback")
        authorization = prepare_partial_fresh_publication(data)
        parent_id, _child_id = advance_fresh_round(data, 0, "verifying")
        advance_fresh_round(data, 1, "finalizing")
        receipt_path = (
            data["store"].root
            / "owners"
            / str(data["task_id"])
            / "runtime"
            / parent_id
            / "callback-receipt.json"
        )
        callback_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        callback_receipt["payload_sha256"] = "0" * 64
        write_json(receipt_path, callback_receipt)
        try:
            synchronize_post_fresh_publication(
                data["product"],
                store=data["store"],
                operation_id=str(data["task_id"]),
                authorization=authorization,
                now=42_600.0,
            )
        except PostVerificationReviewDriveError:
            check(
                "post-fresh progressed callback receipt drift rejects with zero publication",
                json.loads(Path(data["gate_path"]).read_text(encoding="utf-8"))[
                    "status"
                ]
                == "attention-required"
                and not (
                    data["runtime_root"]
                    / "post-fresh-publication-sync.json"
                ).exists(),
            )
        else:
            raise AssertionError("fresh progressed callback receipt drift was accepted")

    with tempfile.TemporaryDirectory(prefix="post-fresh-gate-crash-") as raw:
        data = fixture(Path(raw) / "gate")
        authorization = prepare_partial_fresh_publication(data)

        def crash_after_gate(stage: str) -> None:
            if stage == "gate-written":
                raise RuntimeError("simulated fresh gate publication crash")

        try:
            synchronize_post_fresh_publication(
                data["product"],
                store=data["store"],
                operation_id=str(data["task_id"]),
                authorization=authorization,
                now=43_000.0,
                fault_observer=crash_after_gate,
            )
        except RuntimeError as exc:
            check(
                "post-fresh crash retains one published gate and prepared progress",
                str(exc) == "simulated fresh gate publication crash"
                and json.loads(
                    Path(data["gate_path"]).read_text(encoding="utf-8")
                )["status"]
                == "reviewing"
                and json.loads(
                    Path(data["progress_path"]).read_text(encoding="utf-8")
                )["status"]
                == "quarantined",
            )
        else:
            raise AssertionError("fresh gate crash did not fire")
        recovered = synchronize_post_fresh_publication(
            data["product"],
            store=data["store"],
            operation_id=str(data["task_id"]),
            authorization=authorization,
            now=44_000.0,
        )
        check(
            "post-fresh gate crash restart converges without provider replay",
            recovered["status"] == "applied"
            and recovered["reviews_started"] == 0
            and recovered["provider_effects_replayed"] == 0,
            recovered,
        )

    with tempfile.TemporaryDirectory(prefix="post-fresh-transition-crash-") as raw:
        data = fixture(Path(raw) / "transition")
        authorization = prepare_partial_fresh_publication(data)

        def crash_after_continuation(stage: str) -> None:
            if stage == "continuation-applied":
                raise RuntimeError("simulated continuation publication crash")

        try:
            synchronize_post_fresh_publication(
                data["product"],
                store=data["store"],
                operation_id=str(data["task_id"]),
                authorization=authorization,
                now=45_000.0,
                fault_observer=crash_after_continuation,
            )
        except RuntimeError as exc:
            check(
                "post-fresh continuation crash retains the applied root transition",
                str(exc) == "simulated continuation publication crash"
                and data["store"].read(
                    str(data["task_id"]), str(data["task_id"])
                ).state
                == "awaiting-callback",
            )
        else:
            raise AssertionError("fresh continuation crash did not fire")
        recovered = synchronize_post_fresh_publication(
            data["product"],
            store=data["store"],
            operation_id=str(data["task_id"]),
            authorization=authorization,
            now=46_000.0,
        )
        check(
            "post-fresh continuation crash restart completes only its final receipt",
            recovered["status"] == "applied"
            and recovered["reviews_started"] == 0
            and recovered["provider_effects_replayed"] == 0,
            recovered,
        )

    with tempfile.TemporaryDirectory(prefix="post-fresh-drift-") as raw:
        data = fixture(Path(raw) / "provider")
        authorization = prepare_partial_fresh_publication(data)
        gate = json.loads(Path(data["gate_path"]).read_text(encoding="utf-8"))
        parent_id = gate["lanes"][0]["operation_id"]
        event = (
            data["store"].root
            / "owners"
            / str(data["task_id"])
            / "runtime"
            / parent_id
            / "provider-events"
            / "generation-2"
            / "events"
            / "0002.json"
        )
        changed = json.loads(event.read_text(encoding="utf-8"))
        changed["identity"]["process_identity"] = "0" * 64
        write_json(event, changed)
        try:
            synchronize_post_fresh_publication(
                data["product"],
                store=data["store"],
                operation_id=str(data["task_id"]),
                authorization=authorization,
                now=47_000.0,
            )
        except PostVerificationReviewDriveError:
            check(
                "post-fresh provider drift rejects before gate, store, or marker effect",
                json.loads(
                    Path(data["gate_path"]).read_text(encoding="utf-8")
                )["status"]
                == "attention-required"
                and data["store"].read(
                    str(data["task_id"]), str(data["task_id"])
                ).state
                == "attention-required"
                and not (
                    data["runtime_root"]
                    / "pipeline-review-post-verification-start.json"
                ).exists(),
            )
        else:
            raise AssertionError("fresh provider identity drift was accepted")

    with tempfile.TemporaryDirectory(prefix="post-fresh-reconcile-") as raw:
        data = fixture(Path(raw) / "facade")
        resources = data["resources"]
        assert isinstance(resources, OwnedResources)
        process = ExactProcess(resources)
        cmux = ExactCmux(resources.surface_id)
        original_recover = harness_cli._recover_post_fresh_publication_if_present
        original_publish = harness_cli.publish_status
        harness_cli._recover_post_fresh_publication_if_present = (
            lambda _store, _owner: True
        )

        def forbid_publish(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("post-fresh reconcile cannot touch cmux status")

        harness_cli.publish_status = forbid_publish
        try:
            code = harness_cli.main(
                [
                    "--store",
                    str(data["store"].root),
                    "--owner",
                    str(data["task_id"]),
                    "--json",
                    "reconcile",
                ],
                process_adapter=process,
                cmux_adapter=cmux,
            )
        finally:
            harness_cli._recover_post_fresh_publication_if_present = original_recover
            harness_cli.publish_status = original_publish
        check(
            "supported reconcile synchronization performs zero process/cmux probe or status effect",
            code == 0
            and process.probes == []
            and process.signals == []
            and cmux.probes == []
            and cmux.closes == [],
            (process.probes, process.signals, cmux.probes, cmux.closes),
        )

    with tempfile.TemporaryDirectory(prefix="post-verification-facade-") as raw:
        data = fixture(Path(raw) / "facade")
        resources = data["resources"]
        assert isinstance(resources, OwnedResources)
        process = ExactProcess(resources)
        cmux = ExactCmux(resources.surface_id)
        recovery = RecoveryEffect(data)
        facade_calls = 0

        def supported_facade_recovery(
            store: OperationStore,
            owner: str,
            operation_id: str,
            **_kwargs: object,
        ) -> bool:
            nonlocal facade_calls
            facade_calls += 1
            synchronize_post_verification_review_drive(
                data["product"],
                store=store,
                operation_id=operation_id,
                active_review_operation_id=str(data["review_id"]),
                authorization_record_id="resolution-post-verification",
                authorization_record_sha256="a" * 64,
                process_adapter=process,
                cmux_adapter=cmux,
                recover_review=recovery,
                now=25_000.0,
            )
            return owner == operation_id == str(data["task_id"])

        original = harness_cli._recover_post_verification_review_drive_if_present
        harness_cli._recover_post_verification_review_drive_if_present = (
            supported_facade_recovery
        )
        try:
            result = harness_cli._resume(
                data["store"],
                str(data["task_id"]),
                str(data["task_id"]),
                process_adapter=process,
                cmux_adapter=cmux,
            )
        finally:
            harness_cli._recover_post_verification_review_drive_if_present = original
        check(
            "supported resume facade consumes the synchronization exactly once",
            facade_calls == 1
            and result.previous_state == "attention-required"
            and result.state == "awaiting-callback"
            and recovery.provider_starts == 2,
            (facade_calls, result, recovery.provider_starts),
        )

    with tempfile.TemporaryDirectory(prefix="post-verification-reject-") as raw:
        base = Path(raw)
        dead = fixture(base / "dead")
        resources = dead["resources"]
        assert isinstance(resources, OwnedResources)
        process = ExactProcess(resources)
        process.process_state = "dead"
        cmux = ExactCmux(resources.surface_id)
        recovery = RecoveryEffect(dead)
        try:
            synchronize_post_verification_review_drive(
                dead["product"],
                store=dead["store"],
                operation_id=str(dead["task_id"]),
                active_review_operation_id=str(dead["review_id"]),
                authorization_record_id="resolution-post-verification",
                authorization_record_sha256="a" * 64,
                process_adapter=process,
                cmux_adapter=cmux,
                recover_review=recovery,
                now=30_000.0,
            )
        except PostVerificationReviewDriveError:
            check(
                "dead exact process rejects before the fresh review effect",
                recovery.provider_starts == 0,
            )
        else:
            raise AssertionError("dead process boundary was accepted")

        reject(
            "ready identity drift rejects before any effect",
            fixture(base / "ready"),
            lambda data: write_json(
                data["runtime_root"] / "ready.json",
                {
                    **json.loads(
                        (data["runtime_root"] / "ready.json").read_text(
                            encoding="utf-8"
                        )
                    ),
                    "process_identity": "0" * 64,
                },
            ),
        )
        reject(
            "verification receipt drift rejects before any effect",
            fixture(base / "verification"),
            lambda data: write_json(
                data["runtime_root"] / "pipeline-step-verify.json",
                {**data["verification"], "status": "failed"},
            ),
        )
        reject(
            "pending review marker drift rejects before any effect",
            fixture(base / "marker"),
            lambda data: write_json(
                data["runtime_root"] / "pipeline-review-start.json",
                {
                    **json.loads(
                        (
                            data["runtime_root"] / "pipeline-review-start.json"
                        ).read_text(encoding="utf-8")
                    ),
                    "status": "started",
                },
            ),
        )
        reject(
            "gate identity drift rejects before any effect",
            fixture(base / "gate"),
            lambda data: write_json(
                data["gate_path"],
                {
                    **json.loads(data["gate_path"].read_text(encoding="utf-8")),
                    "active_review_operation_id": str(uuid.uuid4()),
                },
            ),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
