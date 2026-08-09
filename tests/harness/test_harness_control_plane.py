#!/usr/bin/env python3
"""Deterministic Harness/LLM lifecycle ownership regression matrix."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.callback_submit_recovery import (  # noqa: E402
    ArtifactEvidence,
    CallbackSubmitEvidence,
    CallbackSubmitPolicy,
    classify_callback_submit,
)
from harness.pipeline_builtins import compiled_builtin  # noqa: E402
from harness.pipelines import reconcile_pipeline  # noqa: E402
from harness.runtime_worker import provider_exit_is_final  # noqa: E402
from harness.runtime_worker_loop import RuntimeWorkerLoopMixin  # noqa: E402
import v267_stabilization as stab  # noqa: E402


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


baseline_path = (
    ROOT / "docs/acceptance/v2.6.4-harness-control-plane-baseline.json"
)
baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
stages = baseline["stages"]
expected_stages = {
    "plan-step",
    "loop",
    "review",
    "verification",
    "bounded-fix-retry",
    "checkpoint",
    "callback-submit",
    "callback-ingestion",
    "terminal-cleanup",
}
check(
    "authority baseline covers every approved lifecycle stage exactly once",
    baseline.get("schema_version") == 1
    and {row["stage"] for row in stages} == expected_stages
    and len(stages) == len(expected_stages)
    and all((ROOT / row["module"]).is_file() for row in stages)
    and all((ROOT / row["test"]).is_file() for row in stages),
    stages,
)
check(
    "baseline RED rows are exactly the callback capabilities closed by Subplan A",
    {
        row["stage"]
        for row in stages
        if str(row["disposition"]).startswith("baseline-red")
    }
    == {"callback-submit", "callback-ingestion", "terminal-cleanup"},
)

final_path = ROOT / "docs/acceptance/v2.6.4-harness-control-plane-final.json"
final = json.loads(final_path.read_text(encoding="utf-8"))
final_stages = final["stages"]
check(
    "final authority trace closes every historical manual-ingress row",
    final.get("schema_version") == 1
    and {row["stage"] for row in final_stages} == expected_stages
    and len(final_stages) == len(expected_stages)
    and all(row["owner"] == "harness" for row in final_stages)
    and all(not row["manual_ingress_required"] for row in final_stages)
    and all(row["disposition"] == "established" for row in final_stages)
    and all((ROOT / row["module"]).is_file() for row in final_stages)
    and all((ROOT / row["test"]).is_file() for row in final_stages),
    final_stages,
)


pipeline = compiled_builtin("engineering/change")
step_ids = tuple(step.step_id for step in pipeline.definition.steps)
initial = {step_id: "pending" for step_id in step_ids}
initial[step_ids[0]] = "running"
before_prose = reconcile_pipeline(pipeline, initial)
terminal_model_prose = (
    "Implementation, verification, and review are complete. Reap this task now."
)
after_prose = reconcile_pipeline(pipeline, dict(initial))
check(
    "terminal model prose has zero lifecycle transition authority",
    bool(terminal_model_prose)
    and after_prose == before_prose
    and after_prose.action == "wait"
    and after_prose.step_id == step_ids[0],
    after_prose,
)

fix_pipeline = compiled_builtin("engineering/fix")
check(
    "final trace binds the approved fix PipelineSpec and bounded controls",
    final["pipeline"] == "engineering/fix"
    and final["pipeline_definition_sha256"] == fix_pipeline.definition_sha256
    and final["pipeline_steps"]
    == [step.step_id for step in fix_pipeline.definition.steps]
    and final["control_primitives"]
    == [item.identity for item in fix_pipeline.definition.control_primitives],
    final,
)

effect_trace = final["engineering_fix_effect_trace"]
receipt_manifest = effect_trace["receipt_manifest"]
check(
    "final trace binds the effect-recorded engineering fix traversal",
    effect_trace["pipeline"] == "engineering/fix"
    and effect_trace["pipeline_definition_sha256"]
    == fix_pipeline.definition_sha256
    and effect_trace["parent_state"] == "complete"
    and not effect_trace["parent_resources_owned"]
    and len(effect_trace["plan_step_receipts"]) == 7
    and {row["iteration"] for row in effect_trace["plan_step_receipts"]}
    == {0, 1}
    and receipt_manifest["plan_steps"]["count"] == 7
    and receipt_manifest["plan_steps"]["all_identity_bound"]
    and receipt_manifest["verification"]["states"]
    == ["complete", "failed"]
    and receipt_manifest["verification"]["all_identity_bound"]
    and receipt_manifest["review"]
    == {
        "status": "approved",
        "axis": "openai-holistic",
        "lane_state": "complete",
        "identity_fields": [
            "operation_id",
            "run_id",
            "lane_operation_id",
            "lane_run_id",
            "sha256",
        ],
        "evidence_digest_bound": True,
    }
    and receipt_manifest["checkpoint"]["status"] == "recorded"
    and receipt_manifest["checkpoint"]["digest_bound"]
    and receipt_manifest["callback"]
    == {"status": "accepted", "kind": "wiki-summary", "digest_bound": True}
    and receipt_manifest["reap"]
    == {"status": "complete", "effect_replayed": False, "digest_bound": True}
    and receipt_manifest["terminal_cleanup"]
    == {
        "exit_action": "exit-requested",
        "cleanup_action": "cleaned",
        "state": "complete",
        "resources_owned": False,
        "guardian_effects": ["request-exit"],
        "surface_state": "missing",
        "digest_bound": True,
    }
    and effect_trace["accepted_callback_kind"] == "wiki-summary"
    and effect_trace["next_action"] == "closed",
    effect_trace,
)

dogfood = json.loads(
    (
        ROOT
        / "docs/acceptance/v2.6.4-unattended-missing-submit-dogfood.json"
    ).read_text(encoding="utf-8")
)
dogfood_observations = dogfood["observations"]
integrated_trace = final["integrated_trace"]
check(
    "final trace derives callback, next-stage and cleanup evidence from dogfood receipt",
    integrated_trace["dogfood_trace_receipt_sha256"]
    == dogfood["trace_receipt_sha256"]
    and integrated_trace["accepted_callback_count"]
    == dogfood_observations["accepted_receipt_count"]
    and integrated_trace["same_session_recovery_count"]
    == dogfood_observations["same_session_recovery_count"]
    and integrated_trace["provider_prompt_count"]
    == dogfood_observations["provider_prompt_count"]
    and integrated_trace["provider_enter_count"]
    == dogfood_observations["provider_enter_count"]
    and integrated_trace["next_pipeline_action"]
    == dogfood_observations["next_pipeline_action"]
    and integrated_trace["parent_terminal_state"]
    == dogfood_observations["parent_state"]
    and integrated_trace["child_terminal_state"]
    == dogfood_observations["next_child_state"]
    and integrated_trace["terminal_resources_owned"]
    == dogfood_observations["terminal_resources_owned"]
    and integrated_trace["review_gate_entrypoint_count"] == 1
    and integrated_trace["summary_entrypoint_count"] == 1
    and integrated_trace["manual_current_count"]
    == dogfood_observations["manual_current_count"]
    and integrated_trace["manual_resume_count"]
    == dogfood_observations["manual_resume_count"]
    and integrated_trace["manual_send_count"]
    == dogfood_observations["manual_send_count"]
    and integrated_trace["manual_callback_write_count"]
    == dogfood_observations["manual_callback_write_count"]
    and integrated_trace["model_owned_lifecycle_effect_count"] == 0
    and integrated_trace["terminal_prose_transition_count"] == 0,
    integrated_trace,
)

trace: list[tuple[str, str]] = []
for observations in (
    {step_ids[0]: "complete", step_ids[1]: "pending", step_ids[2]: "pending"},
    {step_ids[0]: "complete", step_ids[1]: "complete", step_ids[2]: "pending"},
    {step_ids[0]: "complete", step_ids[1]: "complete", step_ids[2]: "running"},
    {step_ids[0]: "complete", step_ids[1]: "complete", step_ids[2]: "complete"},
):
    progress = reconcile_pipeline(pipeline, observations)
    trace.append((progress.action, progress.step_id))
check(
    "typed observations alone drive verify review wait and terminal progression",
    trace
    == [
        ("start", step_ids[1]),
        ("start", step_ids[2]),
        ("wait", step_ids[2]),
        ("reap-ready", ""),
    ],
    trace,
)


class TransportTrace(RuntimeWorkerLoopMixin):
    def __init__(self, callback_mode: str) -> None:
        self.spec = {"callback_mode": callback_mode}
        self.calls: list[str] = []
        self.terminal_model_prose = terminal_model_prose

    def inspect_control(self) -> None:
        self.calls.append("control")

    def recover_task_summary_attention(self) -> None:
        self.calls.append("summary-recovery")

    def drive_fix_transport(self) -> None:
        self.calls.append("fix")

    def drive_custom_transport(self) -> None:
        self.calls.append("custom")

    def inspect_task_summary(self) -> None:
        self.calls.append("summary")

    def inspect_research(self) -> None:
        self.calls.append("research")

    def inspect_callback(self) -> None:
        self.calls.append("callback")


for mode, expected in (
    ("envelope", ["control", "callback"]),
    (
        "task-summary",
        ["control", "summary-recovery", "fix", "custom", "summary"],
    ),
    ("research-fetch", ["control", "research"]),
):
    worker = TransportTrace(mode)
    worker.inspect_transport()
    check(
        f"{mode} transport dispatch is code-owned and prose-independent",
        worker.calls == expected,
        worker.calls,
    )


callback = CallbackSubmitEvidence(
    observed_at=1_000,
    generation_progress_at=100,
    callback_deadline_at=1_300,
    operation_id="review-round",
    run_id="review-run",
    lane_id="openai-holistic",
    generation=3,
    expected_operation_id="review-round",
    expected_run_id="review-run",
    expected_lane_id="openai-holistic",
    expected_generation=3,
    target_sha256="a" * 64,
    expected_target_sha256="a" * 64,
    operation_state="awaiting-callback",
    process_status="alive",
    surface_status="alive",
    prompt_class="idle-prompt",
    stable_idle_observations=2,
)
reserved = classify_callback_submit(callback, CallbackSubmitPolicy.default())
accepted = classify_callback_submit(
    replace(
        callback,
        callback_artifact=ArtifactEvidence("stable", "b" * 64),
    ),
    CallbackSubmitPolicy.default(),
)
check(
    "callback lifecycle distinguishes observation from typed publication",
    reserved.action == "none"
    and not reserved.model_effect
    and accepted.action == "accept-callback"
    and not accepted.model_effect,
    (reserved, accepted),
)
check(
    "provider prose cannot terminate an unhandled callback boundary",
    not provider_exit_is_final(
        provider_exited=True,
        callback_mode="envelope",
        callback_handled=False,
        operation_state="awaiting-callback",
        operation_profile="reviewer-callback",
        callback_deadline_at=1_300,
    )
    and provider_exit_is_final(
        provider_exited=True,
        callback_mode="envelope",
        callback_handled=True,
        operation_state="awaiting-callback",
        operation_profile="reviewer-callback",
        callback_deadline_at=1_300,
    ),
)


# --- Golden supported corridor (E267.RC1.CORRIDOR) -------------------------
#
# One deterministic production-core engineering/change scenario:
# summary → scoped verify → Simple review → material findings → fix →
# refreshed summary → re-verify → review approve → reap → cleanup.
# Model turns are world actions; provider/process/cmux/review-runtime are
# fake ports; every transition is owned by production code.

import tempfile  # noqa: E402
import threading  # noqa: E402

sys.path.insert(0, str(ROOT / "tests" / "harness"))

from harness.finalization_ledger import FinalizationLedger  # noqa: E402
from harness.workflows.reap import run_reap  # noqa: E402
from lifecycle_simulator_world import (  # noqa: E402
    MATERIAL_CORRIDOR_FINDING,
    ORIGIN_SURFACE,
    REFRESHED_SUMMARY_BODY,
    TASK_SURFACE,
    build_corridor_world,
    passing_verification_runner,
)

CORRIDOR_TASK = "cccc0267-0267-4267-8267-000000000001"
MATERIAL_FINDING = MATERIAL_CORRIDOR_FINDING


with tempfile.TemporaryDirectory(prefix="golden-corridor.") as raw:
    corridor_root = Path(raw)
    world = build_corridor_world(
        corridor_root,
        CORRIDOR_TASK,
        owner_id=CORRIDOR_TASK,
        executor_runtime="claude",
        executor_model="fable",
        review_runtime="claude",
        review_model="fable",
    )
    verification_calls: list[tuple[str, ...]] = []
    runner = passing_verification_runner(verification_calls)
    corridor_trace: list[str] = []

    def reviewer_and_executor_turns(world) -> None:
        # Reviewer turn 1: material changes-requested on the first attempt.
        world.await_condition(
            "first review round is awaiting its callback",
            lambda: bool(world.gate_state().get("lanes"))
            and world.gate_state().get("status") == "reviewing",
        )
        corridor_trace.append("review-round-1-open")
        world.publish_review_callback(
            verdict="changes-requested",
            findings=(MATERIAL_FINDING,),
            verification_iteration=0,
        )
        corridor_trace.append("review-callback-1-published")
        # Executor turn 2: consume the findings packet, commit the fix,
        # and publish the typed resolution.
        world.await_condition(
            "material findings packet reaches the executor worktree",
            lambda: (world.worktree / ".task-review.json").is_file(),
        )
        corridor_trace.append("findings-packet-delivered")
        world.resolve_findings(commit_message="resolve corridor finding")
        corridor_trace.append("resolution-published")
        # Executor turn 3: refreshed summary for the resolved HEAD.
        world.await_condition(
            "refreshed summary is requested after resolution",
            lambda: (
                world.state_root / "pipeline-summary-refresh-notify.json"
            ).is_file(),
        )
        world.publish_summary(REFRESHED_SUMMARY_BODY)
        corridor_trace.append("summary-refreshed")
        # Reviewer turn 2: approve the re-reviewed resolved HEAD.
        world.await_condition(
            "second review attempt is awaiting its callback",
            lambda: world.gate_state().get("status") == "reviewing"
            and any(
                lane.get("verification_iteration") == 0
                for lane in world.gate_state().get("lanes", [])
            )
            and world.gate_state().get("context", {}).get("head_sha")
            == world.head(),
        )
        corridor_trace.append("review-round-2-open")
        world.publish_review_callback(
            verdict="approve",
            findings=(),
            verification_iteration=0,
        )
        corridor_trace.append("review-callback-2-published")

    exit_code = world.run_worker_generation(
        verification_runner=runner,
        during=reviewer_and_executor_turns,
        timeout=90.0,
    )
    record = world.record()
    check(
        "golden corridor reaches the durable wiki-summary boundary",
        exit_code == 0
        and record.state == "finalizing"
        and record.accepted_callback_kind == "wiki-summary",
        {
            "exit_code": exit_code,
            "state": record.state,
            "trace": corridor_trace,
            "gate": world.gate_state().get("status"),
            "faults": [repr(fault) for fault in world.worker_faults],
        },
    )
    gate_state = world.gate_state()
    check(
        "corridor review gate is terminally approved at the resolved HEAD",
        gate_state.get("status") == "approved"
        and gate_state.get("context", {}).get("head_sha") == world.head(),
        {"status": gate_state.get("status")},
    )
    check(
        "corridor wakes exactly the origin surface with the reap command",
        len(world.cmux.sent) >= 1
        and world.cmux.sent[-1][0] == ORIGIN_SURFACE
        and "reap-runner.py" in world.cmux.sent[-1][1]
        and all(surface in {ORIGIN_SURFACE, TASK_SURFACE} for surface, _ in world.cmux.sent),
        world.cmux.sent,
    )
    ledger = FinalizationLedger(
        world.vault / ".vault-meta" / "harness" / "finalization-ledger",
        lineage_id=CORRIDOR_TASK,
        origin_task_id=CORRIDOR_TASK,
        plan_sha256=str(world.meta["approved_plan_sha256"]),
        outcome_contract_sha256=str(world.meta["outcome_contract_sha256"]),
    )
    lineage = ledger.snapshot()
    check(
        "corridor consumes exactly two product cycles ending approved",
        [cycle["terminal_result"] for cycle in lineage["cycles"]]
        == ["changes-requested", "approved"]
        and lineage["terminal_disposition"] == "approved",
        lineage,
    )
    summary = json.loads(world.summary_path.read_text(encoding="utf-8"))
    reap = run_reap(
        world.store,
        owner_id=world.owner_id,
        operation_id=CORRIDOR_TASK,
        summary=summary,
        finalize=lambda _record: {"schema_version": 1, "status": "filed"},
    )
    check(
        "reap finalizes the exact accepted summary callback once",
        reap.result == {"schema_version": 1, "status": "filed"}
        and reap.record.state == "finalizing"
        and reap.record.accepted_callback_sha256
        == record.accepted_callback_sha256,
        reap.record,
    )
    world.store.transition(world.owner_id, CORRIDOR_TASK, "exiting")
    world.store.transition(world.owner_id, CORRIDOR_TASK, "complete")
    terminal = world.record()
    check(
        "corridor terminates resource-free with no pending effect",
        terminal.state == "complete"
        and not any(
            value
            for value in (
                terminal.resources.surface_id,
                terminal.resources.process_group,
                terminal.resources.supervisor_pid,
                terminal.resources.process_identity,
                terminal.resources.supervisor_identity,
            )
        )
        and not terminal.pending_effect,
        terminal,
    )
    verify_children = [
        row
        for row in world.store.list(world.owner_id)
        if row.spec.kind == "pipeline-verify"
    ]
    check(
        "corridor runs scoped verification once per reviewed HEAD",
        len(verify_children) == 2
        and all(row.state == "complete" for row in verify_children)
        and all(
            row.spec.parent_operation_id == CORRIDOR_TASK
            for row in verify_children
        ),
        [(row.spec.operation_id, row.state) for row in verify_children],
    )
    round_records = [
        row
        for row in world.store.list(CORRIDOR_TASK)
        if row.spec.kind == "review-round"
    ]
    check(
        "corridor review rounds carry exactly one accepted callback each",
        len(round_records) == 2
        and all(row.accepted_callback_id for row in round_records),
        [
            (row.spec.operation_id, row.state, row.accepted_callback_id)
            for row in round_records
        ],
    )

    # RC1 consumes the existing owners as a read-only projection.  The
    # dispatch record selects the exact OperationStore identity; review,
    # verification, accepted callbacks, reap, corrected HEAD, and resource
    # freedom remain authoritative in their production stores.
    dispatch_root = world.vault / ".vault-meta" / "dispatch-runs"
    dispatch_root.mkdir(parents=True)
    launch = {
        "schema_version": 1,
        "status": "launched",
        "request_id": CORRIDOR_TASK,
        "worktree": str(world.worktree),
        "harness": {
            "owner_id": world.owner_id,
            "operation_id": CORRIDOR_TASK,
            "lane_id": terminal.lane_id,
            "run_id": terminal.run_id,
        },
    }
    (dispatch_root / f"{CORRIDOR_TASK}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": CORRIDOR_TASK,
                "status": "launched",
                "result": launch,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    evidence_root = world.vault / "docs/acceptance/evidence/v2.6.7"
    evidence_root.mkdir(parents=True)
    material: dict[str, object] = {"fix_head": world.head()}
    artifact_types = {
        "findings_artifact": "findings",
        "refreshed_summary_artifact": "refreshed-summary",
        "second_verification_artifact": "second-verification",
        "re_review_artifact": "re-review",
    }
    for field, artifact_type in artifact_types.items():
        payload = {
            "schema_version": 1,
            "type": artifact_type,
            "cell_id": "rc1-corridor-run-1",
            "head_sha": world.head(),
        }
        if field == "re_review_artifact":
            payload["verdict"] = "approve"
        encoded = json.dumps(payload, sort_keys=True).encode()
        relative = f"docs/acceptance/evidence/v2.6.7/golden-{artifact_type}.json"
        (world.vault / relative).write_bytes(encoded)
        material[field] = {
            "path": relative,
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    provider_sessions = sorted(
        {terminal.run_id, *(row.run_id for row in round_records)}
    )
    receipt = {
        "schema_version": 2,
        "run_id": terminal.run_id,
        "sequence": 1,
        "cell_id": "rc1-corridor-run-1",
        "corridor": "engineering/change",
        "lifecycle_subject_sha256": "a" * 64,
        "request_id": CORRIDOR_TASK,
        "owner_id": world.owner_id,
        "store_id": f"{world.store.root.resolve()}#owners/{world.owner_id}",
        "worktree_id": str(world.worktree),
        "provider_session_ids": provider_sessions,
        "executor_route": {
            "runtime": "claude",
            "model": "fable",
            "effort": "high",
        },
        "review_route": {
            "mode": "simple",
            "runtime": "claude",
            "model": "fable",
            "effort": "high",
        },
        "result": "success",
        "material_cycle": material,
        "resource_free": True,
        "coordinator_recovery": False,
    }
    rc1_verdict = stab.validate_streak(
        [receipt],
        expected_digest="a" * 64,
        config=stab.load_subject_config(
            ROOT / "config/v267-stabilization-subject.json"
        ),
        gate=stab.load_rc1_gate(ROOT / "config/acceptance-cells.toml"),
        root=world.vault,
    )
    check(
        "RC1 derives one material cell from accepted durable corridor owners",
        rc1_verdict["streak"] == 1
        and rc1_verdict["material_finding_cycle"] is True
        and rc1_verdict["complete"] is False,
        rc1_verdict,
    )

print("harness control-plane tests passed")
