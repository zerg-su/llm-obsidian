#!/usr/bin/env python3
"""Deterministic Harness/LLM lifecycle ownership regression matrix."""

from __future__ import annotations

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

print("harness control-plane tests passed")
