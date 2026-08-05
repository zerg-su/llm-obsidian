#!/usr/bin/env python3
"""Exact review-drive attention rearm is atomic, bound, and effect-free."""

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


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import (  # noqa: E402
    AttentionReason,
    EffectOutcome,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
    to_dict,
)
from harness.liveness import (  # noqa: E402
    LivenessController,
    LivenessEvidence,
    LivenessPolicy,
)
from harness.review_drive_rearm import (  # noqa: E402
    ReviewDriveRearmError,
    rearm_review_drive,
)
from harness.runtime_worker_custom import RuntimeWorkerCustomMixin  # noqa: E402
from harness.runtime_worker_loop import RuntimeWorkerLoopMixin  # noqa: E402
from harness.runtime_worker_summary import RuntimeWorkerSummaryMixin  # noqa: E402
from harness.store import OperationStore  # noqa: E402
from outcome_contract import extract_from_bytes  # noqa: E402
from review_resolution import review_transport_identity_sha256  # noqa: E402


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()


def transition_to_waiting(store: OperationStore, owner: str, operation: str) -> None:
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(owner, operation, state)


class ProductionWorkerTick(
    RuntimeWorkerLoopMixin,
    RuntimeWorkerSummaryMixin,
    RuntimeWorkerCustomMixin,
):
    """Exercise the real task-summary recovery phase without provider adapters."""

    def __init__(self, data: dict[str, object], attention_revision: int) -> None:
        self.store = data["store"]
        self.spec_path = data["runtime_root"] / "runtime.json"
        self.spec = {
            "callback_mode": "task-summary",
            "owner_id": data["task_id"],
            "operation_id": data["task_id"],
        }
        self.callback_handled = True
        self.summary_attention_revision = attention_revision
        self.summary_digest = "stale"
        self.summary_stable_reads = 7
        self.summary_inspections = 0
        self.loaded_marker = object()
        self.pipeline = SimpleNamespace(definition_sha256="d" * 64)
        self.next_liveness_probe = float("inf")
        self.next_prompt_probe = float("inf")
        self.next_checkpoint_probe = float("inf")
        self.checkpoint = "retained-review-checkpoint"

    def inspect_control(self) -> None:
        return None

    def drive_fix_transport(self) -> None:
        return None

    def drive_custom_transport(self) -> None:
        return None

    def inspect_task_summary(self) -> None:
        self.summary_inspections += 1
        self.loaded_marker = self.load_review_marker()


def fixture(root: Path, *, mechanism_fix_commits: int = 1) -> dict[str, object]:
    vault = root / "vault"
    product = root / "product"
    plan = vault / "wiki" / "plans" / "approved.md"
    plan.parent.mkdir(parents=True)
    product.mkdir()
    git(product, "init", "-b", "task/rearm")
    git(product, "config", "user.email", "rearm@example.invalid")
    git(product, "config", "user.name", "Review Rearm Test")
    (product / ".gitignore").write_text(".task-*.json\n", encoding="utf-8")
    (product / "product.txt").write_text("reviewed\n", encoding="utf-8")
    git(product, "add", ".gitignore", "product.txt")
    git(product, "commit", "-m", "reviewed")
    reviewed_head = git(product, "rev-parse", "HEAD")
    (product / "product.txt").write_text("resolved\n", encoding="utf-8")
    git(product, "add", "product.txt")
    git(product, "commit", "-m", "resolved")
    failed_drive_head = git(product, "rev-parse", "HEAD")
    for index in range(mechanism_fix_commits):
        (product / "mechanism.txt").write_text(
            f"mechanism repair {index}\n", encoding="utf-8"
        )
        git(product, "add", "mechanism.txt")
        git(product, "commit", "-m", f"mechanism repair {index}")
    resolved_head = git(product, "rev-parse", "HEAD")

    plan.write_text(
        "# Approved\n\n```json\n"
        '{"schema_version":1,"desired_outcome":"Resolve the review.",'
        '"success_evidence":[{"evidence_id":"done",'
        '"observable":"The exact review resumes."}],'
        '"non_goals":["No provider replay."]}\n```\n',
        encoding="utf-8",
    )
    task_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    definition_sha256 = "d" * 64
    profile_sha256 = "e" * 64
    meta = {
        "version": 4,
        "project_id": project_id,
        "task_id": task_id,
        "task_name": "review drive rearm",
        "origin_session": "session-rearm",
        "executor_runtime": "codex",
        "interaction_policy": "unattended",
        "pipeline_policy": {
            "name": "lifecycle/default",
            "definition_sha256": definition_sha256,
            "completion_policy": "attention",
            "total_pass_limit": 2,
        },
        "plan_file": str(plan.resolve()),
        "approved_plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "outcome_contract_sha256": extract_from_bytes(plan.read_bytes()).sha256,
        "vault_root": str(vault.resolve()),
        "review_policy": {
            "mode": "deep",
            "cross_model": False,
            "runtime": "codex",
            "model": "sol",
            "effort": "xhigh",
            "max_verify_iterations": 2,
            "verification_profile": "scoped",
            "verification_profile_sha256": profile_sha256,
        },
        "reap_policy": {
            "mode": "final",
            "auto_file": True,
            "allowed_types": ["repo-touch"],
            "title": "Review drive rearm result",
        },
        "surface_policy": {"auto_close": True, "placement": "workspace"},
        "watchdog_policy": {
            "enabled": True,
            "poll_seconds": 30,
            "warn_after_seconds": 900,
            "alert_after_seconds": 1200,
        },
        "forbidden_actions": [
            "push",
            "deploy",
            "publish",
            "delete-worktree",
            "delete-branch",
            "expand-scope",
        ],
        "task_surface": "surface-root",
        "worktree": str(product.resolve()),
        "branch": "task/rearm",
    }
    write_json(product / ".task-meta.json", meta)
    summary = {
        "schema_version": 2,
        "type": "repo-touch",
        "title": "Review drive rearm result",
        "session": "session-rearm",
        "body": f"Resolved at `{resolved_head}`.",
        "outcome_disposition": "achieved",
        "outcome_evidence_ids": ["done"],
        "residual_gap_pointers": [],
    }
    write_json(product / ".task-summary.json", summary)

    store = OperationStore(vault / ".vault-meta" / "harness")
    route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "xhigh", "executor", "a" * 64
    )
    root_spec = OperationSpec(
        task_id,
        "root-idempotency",
        "dispatch",
        task_id,
        route,
        "packets/root/manifest.json",
        "scoped",
        contract_sha256=definition_sha256,
    )
    root_record = store.create(root_spec, lane_id="root-lane", run_id="root-run")
    transition_to_waiting(store, task_id, task_id)
    root_record = store.read(task_id, task_id)
    root_record = replace(
        root_record,
        resources=OwnedResources(
            surface_id="surface-root",
            process_group=4101,
            supervisor_pid=4102,
            process_identity="1" * 64,
            supervisor_identity="2" * 64,
        ),
        deadline_at=4_100.0,
        revision=root_record.revision + 1,
    )
    store.save(root_record, expected_revision=root_record.revision - 1)
    store.begin_effect(task_id, task_id, "start-provider")
    store.resolve_effect(task_id, task_id, EffectOutcome.SUCCEEDED)
    store.transition(
        task_id,
        task_id,
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )

    lanes: list[dict[str, object]] = []
    callbacks: list[dict[str, object]] = []
    awaiting: dict[str, object] = {}
    finding_ids: list[str] = []
    parent_digests: dict[str, str] = {}
    for index, axis in enumerate(("openai-engineering", "openai-intent"), 1):
        operation_id = f"{task_id}-{axis}"
        run_id = f"run-{axis}"
        lane_id = f"lane-{axis}"
        surface_id = f"surface-{axis}"
        parent = store.create(
            OperationSpec(
                operation_id,
                f"key-{axis}",
                "deep-review-correctness" if index == 1 else "deep-review-spec",
                task_id,
                replace(route, profile="reviewer-callback"),
                "packets/review/manifest.json",
                "scoped",
            ),
            lane_id=lane_id,
            run_id=run_id,
        )
        transition_to_waiting(store, task_id, operation_id)
        parent = store.read(task_id, operation_id)
        parent = replace(
            parent,
            resources=OwnedResources(
                surface_id=surface_id,
                process_group=4200 + index,
                supervisor_pid=4300 + index,
                process_identity=str(index + 2) * 64,
                supervisor_identity=str(index + 4) * 64,
            ),
            revision=parent.revision + 1,
        )
        store.save(parent, expected_revision=parent.revision - 1)
        store.begin_effect(task_id, operation_id, "start-provider")
        store.resolve_effect(task_id, operation_id, EffectOutcome.SUCCEEDED)
        callback = {
            "axis": axis,
            "round_operation_id": f"round-{axis}",
            "round_run_id": f"round-run-{axis}",
            "callback_id": f"callback-{axis}",
            "callback_sha256": hashlib.sha256(axis.encode()).hexdigest(),
        }
        callback.update({})
        callbacks.append(callback)
        finding_id = f"{axis}:finding-{index}"
        finding_ids.append(finding_id)
        awaiting[axis] = {
            **callback,
            "material_finding_ids": [finding_id],
            "pointer": f"round-{axis}.json",
            "review_operation_id": task_id,
            "reviewed_head_sha": reviewed_head,
        }
        lanes.append(
            {
                "axis": axis,
                "checkpoint": f"checkpoint-{axis}",
                "checkpoint_sha256": hashlib.sha256(
                    f"checkpoint-{axis}".encode()
                ).hexdigest(),
                "lane_id": lane_id,
                "operation_id": operation_id,
                "run_id": run_id,
                "state": "awaiting-callback",
                "surface_id": surface_id,
                "verification_iteration": 0,
            }
        )
        parent_path = (
            store.root / "owners" / task_id / "operations" / f"{operation_id}.json"
        )
        parent_digests[axis] = hashlib.sha256(parent_path.read_bytes()).hexdigest()

    review_identity = review_transport_identity_sha256(task_id, callbacks)
    gate_root = store.root / "review-data" / task_id / task_id
    gate = {
        "schema_version": 1,
        "dispatch_operation_id": task_id,
        "owner_id": task_id,
        "status": "awaiting-resolution",
        "policy": {
            "enabled": True,
            "depth": "deep",
            "cross_model": False,
            "runtime": "codex",
            "model": "sol",
            "effort": "xhigh",
            "max_verify_iterations": 2,
            "purpose": "implementation",
        },
        "product_root": str(product.resolve()),
        "active_review_operation_id": task_id,
        "context": {
            "manifest": "packets/review/manifest.json",
            "head_sha": reviewed_head,
            "verification_profile": "scoped",
            "verification_profile_sha256": profile_sha256,
            "implementer_summary_sha256": "9" * 64,
        },
        "fresh_reevaluation_used": False,
        "lanes": lanes,
        "round_results": {axis: f"round-{axis}.json" for axis in awaiting},
        "final_results": {},
        "awaiting_resolution": awaiting,
        "resolution_evidence": {},
        "resolution_transport_identity_sha256": "",
        "continuation_effects": {},
        "evidence": {},
    }
    write_json(gate_root / "review-gate.json", gate)

    review_packet = {
        "schema_version": 1,
        "allowed_dispositions": ["applied", "out-of-scope", "rejected"],
        "findings": [
            {
                "axis": axis,
                "finding_id": finding_id,
                "severity": "important",
                "summary": finding_id,
                "evidence": "bounded evidence",
                "recommendation": "apply the fix",
                "file": "product.txt",
                "line": 1,
            }
            for axis, finding_id in zip(awaiting, finding_ids, strict=True)
        ],
        "material_finding_ids": finding_ids,
        "operation_id": task_id,
        "resolution_path": ".task-review-resolution.json",
        "review_callbacks": callbacks,
        "review_identity_sha256": review_identity,
        "review_operation_id": task_id,
        "reviewed_head_sha": reviewed_head,
    }
    write_json(product / ".task-review.json", review_packet)
    resolution = {
        "schema_version": 1,
        "operation_id": task_id,
        "review_identity_sha256": review_identity,
        "reviewed_head_sha": reviewed_head,
        "resolved_head_sha": resolved_head,
        "resolutions": [
            {
                "finding_id": finding_id,
                "disposition": "applied",
                "rationale": "The exact finding was repaired and verified.",
                "follow_up": "",
            }
            for finding_id in finding_ids
        ],
    }
    write_json(product / ".task-review-resolution.json", resolution)

    runtime_root = store.root / "owners" / task_id / "runtime" / task_id
    write_json(
        runtime_root / "session.json",
        {
            "schema_version": 1,
            "operation_id": task_id,
            "run_id": "root-run",
            "cwd": str(product.resolve()),
            "product_root": str(product.resolve()),
            "time_budget_seconds": 1800,
        },
    )
    write_json(
        runtime_root / "callback-error.json",
        {"schema_version": 1, "status": "review-drive-failed"},
    )
    notification = {
        "schema_version": 1,
        "operation_id": task_id,
        "packet_sha256": canonical_sha256(review_packet),
        "reviewed_head_sha": reviewed_head,
        "summary_sha256": "9" * 64,
        "status": "sent",
    }
    write_json(runtime_root / "pipeline-review-resolution-notify.json", notification)
    drive = hashlib.sha256()
    drive.update((gate_root / "review-gate.json").read_bytes())
    drive.update(failed_drive_head.encode())
    write_json(
        runtime_root / "pipeline-review-start.json",
        {
            "schema_version": 1,
            "operation_id": task_id,
            "definition_sha256": definition_sha256,
            "drive_sha256": drive.hexdigest(),
            "status": "pending",
        },
    )
    root_record = store.read(task_id, task_id)
    liveness = LivenessController(runtime_root / "liveness")
    liveness.observe(
        LivenessEvidence(
            observed_at=4_300.0,
            process_status="alive",
            prompt_state="non-interactive",
            operation_revision=root_record.revision,
            operation_state=root_record.state,
            screen_sha256="8" * 64,
            typed_result_sha256=hashlib.sha256(
                (product / ".task-summary.json").read_bytes()
            ).hexdigest(),
        ),
        LivenessPolicy.default(),
    )
    return {
        "vault": vault,
        "product": product,
        "store": store,
        "task_id": task_id,
        "runtime_root": runtime_root,
        "parent_digests": parent_digests,
        "lanes": lanes,
        "reviewed_head": reviewed_head,
        "failed_drive_head": failed_drive_head,
        "resolved_head": resolved_head,
        "drive_sha256": drive.hexdigest(),
    }


def assert_rejected(label: str, action, expected: str = "") -> None:
    try:
        action()
    except ReviewDriveRearmError as exc:
        if expected and expected not in str(exc):
            raise AssertionError(f"{label}: {exc}") from exc
        print(f"OK   {label}")
        return
    raise AssertionError(f"{label}: rearm unexpectedly succeeded")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="review-drive-rearm-") as raw:
        base = Path(raw)
        happy = fixture(base / "happy")
        product = happy["product"]
        store = happy["store"]
        task_id = happy["task_id"]
        before_parents = dict(happy["parent_digests"])
        failed_marker_path = happy["runtime_root"] / "pipeline-review-start.json"
        failed_marker_sha256 = hashlib.sha256(failed_marker_path.read_bytes()).hexdigest()
        progress_at = 2_000_000_000.0
        receipt = rearm_review_drive(product, now=progress_at)
        record = store.read(task_id, task_id)
        live = LivenessController(happy["runtime_root"] / "liveness").current_state()
        check(
            "rearm binds the unique failed-drive ancestor and one exact revision",
            receipt["status"] == "applied"
            and receipt["failed_drive_head_sha"] == happy["failed_drive_head"]
            and receipt["drive_sha256"] == happy["drive_sha256"]
            and receipt["resolved_head_sha"] == happy["resolved_head"]
            and record.state == "awaiting-callback"
            and live is not None
            and live.operation_state == record.state
            and live.operation_revision == record.revision
            and live.last_progress_at == progress_at
            and record.deadline_at == progress_at + 1_800.0,
            (receipt, record, live),
        )
        worker = ProductionWorkerTick(happy, receipt["attention_revision"])
        worker.inspect_transport()
        worker.tick_observers()
        after_tick = store.read(task_id, task_id)
        check(
            "next production worker tick consumes the latch without attention bounce",
            after_tick.state == "awaiting-callback"
            and after_tick.revision == receipt["target_revision"]
            and not worker.callback_handled
            and worker.summary_attention_revision == -1
            and worker.summary_inspections == 1
            and worker.loaded_marker is None
            and worker.marker_path.name == "pipeline-review-rearm-start.json"
            and hashlib.sha256(failed_marker_path.read_bytes()).hexdigest()
            == failed_marker_sha256
            and json.loads(
                (happy["runtime_root"] / "callback-error.json").read_text(
                    encoding="utf-8"
                )
            )
            == {"schema_version": 1, "status": "review-drive-failed"},
            (after_tick, worker.__dict__),
        )
        decision = LivenessController(
            happy["runtime_root"] / "liveness"
        ).observe(
            LivenessEvidence(
                observed_at=progress_at + 1.0,
                process_status="alive",
                prompt_state="non-interactive",
                operation_revision=record.revision,
                operation_state=record.state,
                screen_sha256="8" * 64,
                typed_result_sha256=live.typed_result_sha256,
            ),
            LivenessPolicy.default(),
        )
        check(
            "fresh progress cannot bounce directly back to attention",
            decision.action != "attention-required",
            decision,
        )
        after_parents = {}
        for lane in happy["lanes"]:
            path = (
                store.root
                / "owners"
                / task_id
                / "operations"
                / f"{lane['operation_id']}.json"
            )
            after_parents[lane["axis"]] = hashlib.sha256(path.read_bytes()).hexdigest()
        check(
            "rearm creates zero duplicate provider or continuation effect",
            after_parents == before_parents
            and record.effect_id == "start-provider"
            and record.effect_outcome == EffectOutcome.SUCCEEDED
            and not record.pending_effect,
            (before_parents, after_parents, record),
        )
        replay_revision = record.revision
        replay = rearm_review_drive(product, now=progress_at + 2.0)
        check(
            "applied rearm is idempotent",
            replay == receipt
            and store.read(task_id, task_id).revision == replay_revision,
        )
        cli = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "task-review-rearm.py"),
                "--worktree",
                str(product),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        check(
            "code-owned CLI replays the same applied receipt",
            cli.returncode == 0 and json.loads(cli.stdout) == receipt,
            (cli.returncode, cli.stdout, cli.stderr),
        )

        no_match = fixture(base / "no-match")
        marker_path = no_match["runtime_root"] / "pipeline-review-start.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["drive_sha256"] = "0" * 64
        write_json(marker_path, marker)
        assert_rejected(
            "mismatched marker with no first-parent match fails closed",
            lambda: rearm_review_drive(no_match["product"], now=progress_at),
            "exactly one first-parent HEAD",
        )

        ambiguous = fixture(base / "ambiguous")
        assert_rejected(
            "multiple candidate matches fail closed as ambiguous",
            lambda: rearm_review_drive(
                ambiguous["product"],
                now=progress_at,
                _drive_digest=lambda _head: ambiguous["drive_sha256"],
            ),
            "exactly one first-parent HEAD",
        )

        nonancestor = fixture(base / "nonancestor")
        git(
            nonancestor["product"],
            "checkout",
            "-b",
            "side-review",
            nonancestor["reviewed_head"],
        )
        (nonancestor["product"] / "side.txt").write_text(
            "foreign reviewed boundary\n", encoding="utf-8"
        )
        git(nonancestor["product"], "add", "side.txt")
        git(nonancestor["product"], "commit", "-m", "side review")
        foreign_reviewed = git(nonancestor["product"], "rev-parse", "HEAD")
        git(nonancestor["product"], "checkout", "task/rearm")
        nonancestor_gate_path = (
            nonancestor["store"].root
            / "review-data"
            / nonancestor["task_id"]
            / nonancestor["task_id"]
            / "review-gate.json"
        )
        nonancestor_gate = json.loads(
            nonancestor_gate_path.read_text(encoding="utf-8")
        )
        nonancestor_gate["context"]["head_sha"] = foreign_reviewed
        for boundary in nonancestor_gate["awaiting_resolution"].values():
            boundary["reviewed_head_sha"] = foreign_reviewed
        write_json(nonancestor_gate_path, nonancestor_gate)
        nonancestor_review_path = nonancestor["product"] / ".task-review.json"
        nonancestor_review = json.loads(
            nonancestor_review_path.read_text(encoding="utf-8")
        )
        nonancestor_review["reviewed_head_sha"] = foreign_reviewed
        write_json(nonancestor_review_path, nonancestor_review)
        nonancestor_resolution_path = (
            nonancestor["product"] / ".task-review-resolution.json"
        )
        nonancestor_resolution = json.loads(
            nonancestor_resolution_path.read_text(encoding="utf-8")
        )
        nonancestor_resolution["reviewed_head_sha"] = foreign_reviewed
        write_json(nonancestor_resolution_path, nonancestor_resolution)
        nonancestor_notification_path = (
            nonancestor["runtime_root"]
            / "pipeline-review-resolution-notify.json"
        )
        nonancestor_notification = json.loads(
            nonancestor_notification_path.read_text(encoding="utf-8")
        )
        nonancestor_notification["reviewed_head_sha"] = foreign_reviewed
        nonancestor_notification["packet_sha256"] = canonical_sha256(
            nonancestor_review
        )
        write_json(nonancestor_notification_path, nonancestor_notification)
        assert_rejected(
            "non-ancestor reviewed HEAD fails closed before marker matching",
            lambda: rearm_review_drive(
                nonancestor["product"], now=progress_at
            ),
            "not an ancestor",
        )

        crash = fixture(base / "crash")
        crash_seen: list[str] = []

        def crash_after_operation(stage: str) -> None:
            crash_seen.append(stage)
            if stage == "operation-written":
                raise RuntimeError("simulated process crash")

        try:
            rearm_review_drive(
                crash["product"], now=7_000.0, _fault_hook=crash_after_operation
            )
        except RuntimeError as exc:
            check(
                "crash is injected after the operation publication",
                str(exc) == "simulated process crash",
            )
        else:
            raise AssertionError("crash injection did not fire")
        partial_record = crash["store"].read(crash["task_id"], crash["task_id"])
        partial_live = LivenessController(
            crash["runtime_root"] / "liveness"
        ).current_state()
        prepared = json.loads(
            (crash["runtime_root"] / "review-drive-rearm.json").read_text(
                encoding="utf-8"
            )
        )
        check(
            "write-ahead receipt contains the atomic crash boundary",
            prepared["status"] == "prepared"
            and partial_record.state == "awaiting-callback"
            and partial_live.operation_state == "attention-required",
            (prepared, partial_record, partial_live),
        )
        recovered = rearm_review_drive(crash["product"], now=8_000.0)
        recovered_record = crash["store"].read(crash["task_id"], crash["task_id"])
        recovered_live = LivenessController(
            crash["runtime_root"] / "liveness"
        ).current_state()
        check(
            "crash replay finishes the same revision without a second transition",
            recovered["status"] == "applied"
            and recovered_record.revision == partial_record.revision
            and recovered_live.operation_revision == partial_record.revision
            and recovered_live.last_progress_at == 7_000.0,
            (recovered, recovered_record, recovered_live),
        )

        stale = fixture(base / "stale")

        def crash_after_prepare(stage: str) -> None:
            if stage == "prepared":
                raise RuntimeError("prepared")

        try:
            rearm_review_drive(
                stale["product"], now=9_000.0, _fault_hook=crash_after_prepare
            )
        except RuntimeError:
            pass
        summary_path = stale["product"] / ".task-summary.json"
        changed = json.loads(summary_path.read_text(encoding="utf-8"))
        changed["body"] += " drift"
        write_json(summary_path, changed)
        assert_rejected(
            "prepared latch rejects summary digest drift",
            lambda: rearm_review_drive(stale["product"], now=9_001.0),
        )

        mismatch = fixture(base / "mismatch")
        gate_path = (
            mismatch["store"].root
            / "review-data"
            / mismatch["task_id"]
            / mismatch["task_id"]
            / "review-gate.json"
        )
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        gate["continuation_effects"] = {"openai-intent:0": {"state": "prepared"}}
        write_json(gate_path, gate)
        assert_rejected(
            "live continuation receipt rejects rearm",
            lambda: rearm_review_drive(mismatch["product"], now=10_000.0),
        )

        parent_drift = fixture(base / "parent-drift")
        lane = parent_drift["lanes"][0]
        parent_drift["store"].transition(
            parent_drift["task_id"], lane["operation_id"], "running"
        )
        assert_rejected(
            "retained parent state drift rejects rearm",
            lambda: rearm_review_drive(parent_drift["product"], now=11_000.0),
        )

        unbound = fixture(base / "unbound")
        (unbound["runtime_root"] / "pipeline-review-resolution-notify.json").unlink()
        assert_rejected(
            "missing notification binding rejects rearm",
            lambda: rearm_review_drive(unbound["product"], now=12_000.0),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
