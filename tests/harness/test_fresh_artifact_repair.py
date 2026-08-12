#!/usr/bin/env python3
"""One restart-safe XHigh fresh artifact-only repair session."""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.artifact_repair import ContractArtifactOwner  # noqa: E402
from harness.adapters.claude import ClaudeDriver  # noqa: E402
from harness.adapters.codex import CodexDriver, REVIEWER_CONFIG  # noqa: E402
from harness.adapters.process import ProcessAdapter  # noqa: E402
from harness.contracts import (  # noqa: E402
    AttentionReason,
    CanonicalContractTemplate,
    ContractFamily,
    OperationRecord,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.fresh_artifact_repair import (  # noqa: E402
    FreshArtifactRepair,
    FreshRepairEffectUncertain,
    FreshRepairExhausted,
    FreshRepairInvalid,
    ProviderAvailability,
    select_fresh_repair_route,
)
from harness.runtime_worker_control import RuntimeWorkerControlMixin  # noqa: E402
from harness.runtime_worker_summary import RuntimeWorkerSummaryMixin  # noqa: E402
import harness.runtime_worker_summary as summary_runtime  # noqa: E402
from model_routing_config import load_config  # noqa: E402


failures: list[str] = []


def check(label: str, condition: bool) -> None:
    if condition:
        print(f"OK   {label}")
    else:
        print(f"FAIL {label}")
        failures.append(label)


class FakeManager:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def start(self, request: object) -> object:
        self.requests.append(request)
        spec = request.spec
        record = OperationRecord(
            spec,
            "running",
            1,
            request.lane_id,
            request.run_id,
            OwnedResources(surface_id="repair-surface"),
            attempt=1,
            attempt_limit=1,
            model_restart_limit=0,
        )
        return type("Started", (), {"record": record})()


config = load_config(ROOT)
prior = RuntimeRoute(
    "codex", "gpt-5.6-sol", "high", "executor", config.fingerprint
)
opposite = select_fresh_repair_route(config, prior)
check(
    "fresh repair prefers the opposite provider at XHigh",
    opposite.runtime == "claude"
    and opposite.effort == "xhigh"
    and opposite.profile == "artifact-repair",
)
try:
    select_fresh_repair_route(config, prior, same_provider=True)
except ValueError:
    no_unproven_fallback = True
else:
    no_unproven_fallback = False
fallback = select_fresh_repair_route(
    config,
    prior,
    same_provider=True,
    opposite_availability=ProviderAvailability(
        "claude", "unavailable", "9" * 64
    ),
)
check(
    "same-provider fallback requires durable opposite-provider unavailability",
    no_unproven_fallback
    and fallback.runtime == "codex"
    and fallback.effort == "xhigh",
)

with tempfile.TemporaryDirectory(prefix="fresh-artifact-repair.") as raw:
    base = Path(raw)
    worktree = base / "product"
    state = base / "store" / "runtime" / "root"
    worktree.mkdir()
    state.mkdir(parents=True)
    target = worktree / ".task-summary.json"
    template = CanonicalContractTemplate.create(
        ContractFamily.TASK_SUMMARY,
        attempt_id="root",
        target_pointer=".task-summary.json",
        value={
            "schema_version": 2,
            "type": "repo-touch",
            "session": "session-1",
            "title": "",
            "body": "",
            "outcome_disposition": "",
            "outcome_evidence_ids": [],
            "residual_gap_pointers": [],
        },
        code_owned_fields={"schema_version", "type", "session"},
        model_owned_fields={
            "title",
            "body",
            "outcome_disposition",
            "outcome_evidence_ids",
            "residual_gap_pointers",
        },
    )
    owner = ContractArtifactOwner.publish(
        state_root=state,
        worktree=worktree,
        template=template,
        actual_target=target,
    )
    owner.restore_template()
    root_spec = OperationSpec(
        operation_id="root",
        idempotency_key="root-key",
        kind="dispatch",
        owner_id="root",
        route=prior,
        context_manifest="packets/root/manifest.json",
        verification_profile="scoped",
        root_operation_id="root",
    )
    parent = OperationRecord(
        root_spec, "running", 1, "root-lane", "root-run", OwnedResources()
    )
    repair = FreshArtifactRepair.reserve(
        owner=owner,
        parent=parent,
        invalid_sha256="1" * 64,
        route=opposite,
        origin_surface="11111111-1111-4111-8111-111111111111",
    )
    product_before = {
        path.relative_to(worktree).as_posix(): path.read_bytes()
        for path in worktree.rglob("*")
        if path.is_file()
    }
    manager = FakeManager()
    started = repair.start(manager)
    replay = repair.start(manager)
    request = manager.requests[0]
    claude_route = RuntimeRoute(
        "claude", opposite.model, "xhigh", "artifact-repair", config.fingerprint
    )
    codex_route = RuntimeRoute(
        "codex", prior.model, "xhigh", "artifact-repair", config.fingerprint
    )
    claude_command = ClaudeDriver(Path("/usr/bin/claude")).command(
        claude_route,
        callback_pointer=request.cwd / request.callback_pointer,
        session_root=request.cwd,
    )
    codex_command = CodexDriver(Path("/usr/bin/codex")).command(
        codex_route,
        callback_pointer=request.cwd / request.callback_pointer,
        session_root=request.cwd,
    )
    launch = ProcessAdapter().prepare_surface_launch(
        argv=claude_command,
        cwd=request.cwd,
        state_root=state / "launch-state",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=request.cwd / request.callback_pointer,
        product_root=None,
        store_root=state,
        owner_id="root",
        operation_id=request.spec.operation_id,
        run_id=request.run_id,
        surface_id="22222222-2222-4222-8222-222222222222",
        runtime="claude",
        callback_mode="artifact-repair",
        origin_surface="11111111-1111-4111-8111-111111111111",
        initial_input_pointer=request.cwd / request.prompt_pointer,
    )
    scratch_files = sorted(
        path.relative_to(request.cwd).as_posix()
        for path in request.cwd.rglob("*")
        if path.is_file()
    )
    fresh_session_ok = (
        started.status == "started"
        and replay.status == "already-started"
        and len(manager.requests) == 1
        and request.cwd != worktree
        and not request.cwd.is_relative_to(worktree)
        and scratch_files == ["prompt.md", "template.json"]
        and request.attempt_limit == 1
        and request.model_restart_limit == 0
        and request.product_root is None
        and request.spec.kind == "artifact-repair"
        and request.spec.parent_operation_id == "root"
        and request.spec.root_operation_id == "root"
        and request.spec.route.profile == "artifact-repair"
        and "Bash" not in claude_command
        and "--add-dir" not in claude_command
        and "--strict-mcp-config" in claude_command
        and "--strict-config" in codex_command
        and "--add-dir" not in codex_command
        and all(value in codex_command for value in REVIEWER_CONFIG)
        and json.loads(launch.spec_path.read_text())["product_root"] == ""
    )
    check(
        "fresh session is artifact-only with zero replay budget",
        fresh_session_ok,
    )
    product_after = {
        path.relative_to(worktree).as_posix(): path.read_bytes()
        for path in worktree.rglob("*")
        if path.is_file()
    }
    check(
        "launch cannot mutate repository files or durable lifecycle authority",
        product_after == product_before
        and parent == OperationRecord(
            root_spec,
            "running",
            1,
            "root-lane",
            "root-run",
            OwnedResources(),
        ),
    )

    try:
        FreshArtifactRepair.reserve(
            owner=owner,
            parent=parent,
            invalid_sha256="2" * 64,
            route=opposite,
            origin_surface="11111111-1111-4111-8111-111111111111",
        )
    except FreshRepairExhausted:
        one_only = True
    else:
        one_only = False
    check("one family attempt cannot reserve a second fresh repair", one_only)

    callback = request.cwd / request.callback_pointer
    artifact = {
        **owner.template_value,
        "title": "Bounded repair",
        "body": "Artifact-only correction.",
        "outcome_disposition": "partially-achieved",
        "residual_gap_pointers": ["plan.md"],
    }
    payload = {
        "schema_version": 1,
        "family": "task-summary",
        "repair_id": repair.repair_id,
        "artifact": artifact,
    }
    import hashlib

    payload_sha = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    callback.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "callback_id": f"result-{payload_sha[:24]}",
                "operation_id": request.spec.operation_id,
                "run_id": request.run_id,
                "kind": "result",
                "payload": payload,
                "payload_sha256": payload_sha,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    receipt = repair.accept(
        lambda value: value
        if value.get("title") == "Bounded repair"
        else (_ for _ in ()).throw(ValueError("invalid"))
    )
    check(
        "accepted repair emits only content-free identity and digest evidence",
        receipt.status == "self-healed"
        and receipt.family == "task-summary"
        and receipt.stage == "fresh-context"
        and json.loads(target.read_text(encoding="utf-8"))["title"]
        == "Bounded repair"
        and "Bounded repair" not in json.dumps(receipt.__dict__),
    )

with tempfile.TemporaryDirectory(prefix="fresh-artifact-invalid.") as raw:
    base = Path(raw)
    tree = base / "product"
    state = base / "state"
    tree.mkdir()
    state.mkdir()
    invalid_target = tree / ".task-summary.json"
    invalid_owner = ContractArtifactOwner.publish(
        state_root=state,
        worktree=tree,
        template=template,
        actual_target=invalid_target,
    )
    invalid_owner.restore_template()
    invalid = FreshArtifactRepair.reserve(
        owner=invalid_owner,
        parent=parent,
        invalid_sha256="5" * 64,
        route=opposite,
        origin_surface="11111111-1111-4111-8111-111111111111",
    )
    invalid_manager = FakeManager()
    invalid.start(invalid_manager)
    bad_callback = invalid.scratch / ".artifact-repair-callback.json"
    bad_callback.write_text("{}\n", encoding="utf-8")
    try:
        invalid.accept(lambda value: value)
    except FreshRepairInvalid:
        invalid_rejected = True
    else:
        invalid_rejected = False
    try:
        invalid.start(FakeManager())
    except FreshRepairInvalid:
        no_second_effect = True
    else:
        no_second_effect = False
    failure = json.loads((invalid.root / "failed.json").read_text())
    restored = json.loads(invalid_target.read_text())
    check(
        "invalid fresh output converges terminally without another provider effect",
        invalid_rejected
        and no_second_effect
        and failure["status"] == "invalid"
        and set(failure) == {
            "status", "family", "stage", "repair_id", "input_sha256",
            "output_sha256", "route_sha256",
        }
        and restored == invalid_owner.template_value,
    )

with tempfile.TemporaryDirectory(prefix="fresh-artifact-reconcile.") as raw:
    base = Path(raw)
    for family in (
        ContractFamily.TASK_SUMMARY,
        ContractFamily.PIPELINE_STEP_RESULT,
    ):
        family_root = base / family.value
        worktree = family_root / "product"
        state = family_root / "state"
        worktree.mkdir(parents=True)
        state.mkdir(parents=True)
        attempt_id = f"reconcile-{family.value}"
        if family is ContractFamily.TASK_SUMMARY:
            pointer = ".task-summary.json"
            template_value = {
                "schema_version": 2,
                "type": "repo-touch",
                "session": "session-1",
                "title": "",
                "body": "",
            }
            code_fields = {"schema_version", "type", "session"}
        else:
            pointer = ".task-pipeline-step-result.json"
            template_value = {
                "schema_version": 1,
                "output_sha256": "8" * 64,
                "head_sha": "9" * 40,
                "status": "",
                "outcome": "",
            }
            code_fields = {"schema_version", "output_sha256", "head_sha"}
        target = worktree / pointer
        family_template = CanonicalContractTemplate.create(
            family,
            attempt_id=attempt_id,
            target_pointer=pointer,
            value=template_value,
            code_owned_fields=code_fields,
            model_owned_fields=set(template_value) - code_fields,
        )
        family_owner = ContractArtifactOwner.publish(
            state_root=state,
            worktree=worktree,
            template=family_template,
            actual_target=target,
        )
        family_owner.restore_template()
        family_spec = OperationSpec(
            operation_id=attempt_id,
            idempotency_key=f"key-{attempt_id}",
            kind="dispatch",
            owner_id=attempt_id,
            route=prior,
            context_manifest="packets/root/manifest.json",
            verification_profile="scoped",
            root_operation_id=attempt_id,
        )
        family_parent = OperationRecord(
            family_spec,
            "running",
            1,
            f"lane-{family.value}",
            f"run-{family.value}",
        )
        family_repair = FreshArtifactRepair.reserve(
            owner=family_owner,
            parent=family_parent,
            invalid_sha256="6" * 64,
            route=opposite,
            origin_surface="11111111-1111-4111-8111-111111111111",
        )
        family_manager = FakeManager()
        family_repair.start(family_manager)
        family_request = family_manager.requests[0]
        artifact = dict(template_value)
        if family is ContractFamily.TASK_SUMMARY:
            artifact.update({"title": "Repaired", "body": "Ready"})
        else:
            artifact.update({"status": "complete", "outcome": "ok"})
        payload = {
            "schema_version": 1,
            "family": family.value,
            "repair_id": family_repair.repair_id,
            "artifact": artifact,
        }
        payload_sha = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        callback_id = f"result-{payload_sha[:24]}"
        (family_repair.scratch / family_request.callback_pointer).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "callback_id": callback_id,
                    "operation_id": family_request.spec.operation_id,
                    "run_id": family_request.run_id,
                    "kind": "result",
                    "payload": payload,
                    "payload_sha256": payload_sha,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        accepted_child = OperationRecord(
            family_request.spec,
            "verifying",
            4,
            family_request.lane_id,
            family_request.run_id,
            attempt=1,
            attempt_limit=1,
            model_restart_limit=0,
            accepted_callback_id=callback_id,
            accepted_callback_kind="result",
            accepted_callback_sha256=payload_sha,
        )
        class AcceptedStore:
            def read(self, _owner_id: str, _operation_id: str) -> OperationRecord:
                return accepted_child

        if family is ContractFamily.TASK_SUMMARY:
            (worktree / ".task-meta.json").write_text("{}\n", encoding="utf-8")
            published: list[dict[str, object]] = []

            class SummaryWorker(RuntimeWorkerSummaryMixin):
                def __init__(self) -> None:
                    self.task_summary_artifact_owner = family_owner
                    self.store = AcceptedStore()
                    self.spec = {"owner_id": attempt_id, "cwd": worktree}
                    self.summary_digest = ""
                    self.summary_stable_reads = 0

                def summary_is_stable(self, _raw: bytes) -> bool:
                    return True

                def load_summary_contract(self, _raw: bytes) -> dict[str, object]:
                    return artifact

                def build_summary_pipeline_state(
                    self, _raw: bytes, *, summary: dict[str, object]
                ) -> object:
                    return type("State", (), {"summary": summary})()

                def advance_compiled_pipeline(self, _state: object) -> bool:
                    return True

                def publish_summary_callback(self, value: dict[str, object]) -> None:
                    published.append(value)

                def summary_attention(self, *_args: object, **_kwargs: object) -> None:
                    raise AssertionError("valid fresh summary reached attention")

            original_validate_summary = summary_runtime.validate_summary_for_task
            summary_runtime.validate_summary_for_task = lambda value, *_args, **_kwargs: value
            try:
                summary_worker = SummaryWorker()
                summary_worker.finish_task_summary(target.read_bytes())
                summary_worker.finish_task_summary(target.read_bytes())
            finally:
                summary_runtime.validate_summary_for_task = original_validate_summary
            production_transition = len(published) == 1
        else:
            class PipelineWorker(RuntimeWorkerControlMixin):
                def __init__(self) -> None:
                    self.pipeline_step_artifact_owner = family_owner
                    self.store = AcceptedStore()
                    self.spec = {"owner_id": attempt_id}
                    self.fix_result_digest = "prior"
                    self.fix_result_stable_reads = 2
                    self.custom_result_digest = "prior"
                    self.custom_result_stable_reads = 2

                def summary_attention(self, *_args: object, **_kwargs: object) -> None:
                    raise AssertionError("valid fresh result reached attention")

            pipeline_worker = PipelineWorker()
            production_transition = (
                pipeline_worker.adopt_fresh_pipeline_step_result() is True
                and pipeline_worker.adopt_fresh_pipeline_step_result() is False
            )
        check(
            f"{family.value} production consumer adopts once then submits normally",
            production_transition,
        )

        failure_attempt = f"failure-{family.value}"
        failure_worktree = family_root / "failure-product"
        failure_worktree.mkdir()
        failure_target = failure_worktree / pointer
        failure_template = CanonicalContractTemplate.create(
            family,
            attempt_id=failure_attempt,
            target_pointer=pointer,
            value=template_value,
            code_owned_fields=code_fields,
            model_owned_fields=set(template_value) - code_fields,
        )
        failure_owner = ContractArtifactOwner.publish(
            state_root=state / "failure",
            worktree=failure_worktree,
            template=failure_template,
            actual_target=failure_target,
        )
        failure_owner.restore_template()
        failure_spec = replace(
            family_spec,
            operation_id=failure_attempt,
            idempotency_key=f"key-{failure_attempt}",
            owner_id=failure_attempt,
            root_operation_id=failure_attempt,
        )
        failure_parent = OperationRecord(
            failure_spec,
            "running",
            1,
            f"failure-lane-{family.value}",
            f"failure-run-{family.value}",
        )
        failed_repair = FreshArtifactRepair.reserve(
            owner=failure_owner,
            parent=failure_parent,
            invalid_sha256="7" * 64,
            route=opposite,
            origin_surface="11111111-1111-4111-8111-111111111111",
        )
        failed_manager = FakeManager()
        failed_repair.start(failed_manager)
        failed_request = failed_manager.requests[0]
        failed_child = OperationRecord(
            failed_request.spec,
            "attention-required",
            4,
            failed_request.lane_id,
            failed_request.run_id,
            attempt=1,
            attempt_limit=1,
            model_restart_limit=0,
            attention_reason=AttentionReason.CALLBACK_INVALID,
        )
        try:
            failed_repair.reconcile(failed_child, lambda value: value)
        except FreshRepairInvalid:
            propagated = True
        else:
            propagated = False
        failure_receipt = json.loads(
            (failed_repair.root / "failed.json").read_text()
        )
        class FailedStore:
            def read(self, _owner_id: str, _operation_id: str) -> OperationRecord:
                return failed_child

        terminal_attention: list[tuple[str, object]] = []
        if family is ContractFamily.TASK_SUMMARY:
            (failure_worktree / ".task-meta.json").write_text("{}\n", encoding="utf-8")

            class FailedSummaryWorker(RuntimeWorkerSummaryMixin):
                def __init__(self) -> None:
                    self.task_summary_artifact_owner = failure_owner
                    self.store = FailedStore()
                    self.spec = {
                        "owner_id": failure_attempt,
                        "cwd": failure_worktree,
                    }

                def summary_attention(self, status: str, reason: object = None) -> None:
                    terminal_attention.append((status, reason))

            FailedSummaryWorker().finish_task_summary(failure_target.read_bytes())
        else:
            class FailedPipelineWorker(RuntimeWorkerControlMixin):
                def __init__(self) -> None:
                    self.pipeline_step_artifact_owner = failure_owner
                    self.store = FailedStore()
                    self.spec = {"owner_id": failure_attempt}

                def summary_attention(self, status: str, reason: object = None) -> None:
                    terminal_attention.append((status, reason))

            FailedPipelineWorker().adopt_fresh_pipeline_step_result()
        check(
            f"{family.value} production consumer terminalizes child failure once",
            propagated
            and failure_receipt["stage"] == "callback-invalid"
            and json.loads(failure_target.read_text())
            == failure_owner.template_value
            and terminal_attention
            and terminal_attention[-1][1] is AttentionReason.RETRY_EXHAUSTED,
        )

        substitution_attempt = f"substitution-{family.value}"
        substitution_worktree = family_root / "substitution-product"
        substitution_worktree.mkdir()
        substitution_target = substitution_worktree / pointer
        substitution_template = CanonicalContractTemplate.create(
            family,
            attempt_id=substitution_attempt,
            target_pointer=pointer,
            value=template_value,
            code_owned_fields=code_fields,
            model_owned_fields=set(template_value) - code_fields,
        )
        substitution_owner = ContractArtifactOwner.publish(
            state_root=state / "substitution",
            worktree=substitution_worktree,
            template=substitution_template,
            actual_target=substitution_target,
        )
        substitution_owner.restore_template()
        substitution_spec = replace(
            family_spec,
            operation_id=substitution_attempt,
            idempotency_key=f"key-{substitution_attempt}",
            owner_id=substitution_attempt,
            root_operation_id=substitution_attempt,
        )
        substitution_parent = OperationRecord(
            substitution_spec,
            "running",
            1,
            f"substitution-lane-{family.value}",
            f"substitution-run-{family.value}",
        )
        substitution = FreshArtifactRepair.reserve(
            owner=substitution_owner,
            parent=substitution_parent,
            invalid_sha256="a" * 64,
            route=opposite,
            origin_surface="11111111-1111-4111-8111-111111111111",
        )
        substitution_manager = FakeManager()
        substitution.start(substitution_manager)
        substitution_request = substitution_manager.requests[0]

        def callback_value(replacement: str) -> tuple[dict[str, object], str]:
            changed_artifact = dict(artifact)
            if family is ContractFamily.TASK_SUMMARY:
                changed_artifact["title"] = replacement
            else:
                changed_artifact["outcome"] = replacement
            changed_payload = {
                "schema_version": 1,
                "family": family.value,
                "repair_id": substitution.repair_id,
                "artifact": changed_artifact,
            }
            changed_sha = hashlib.sha256(
                json.dumps(
                    changed_payload, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            return (
                {
                    "schema_version": 1,
                    "callback_id": f"result-{changed_sha[:24]}",
                    "operation_id": substitution_request.spec.operation_id,
                    "run_id": substitution_request.run_id,
                    "kind": "result",
                    "payload": changed_payload,
                    "payload_sha256": changed_sha,
                },
                changed_sha,
            )

        accepted_value, accepted_sha = callback_value("accepted-a")
        substituted_value, _substituted_sha = callback_value("substituted-b")
        substitution_callback = (
            substitution.scratch / substitution_request.callback_pointer
        )
        substitution_callback.write_text(
            json.dumps(accepted_value, sort_keys=True) + "\n", encoding="utf-8"
        )
        substitution_child = OperationRecord(
            substitution_request.spec,
            "verifying",
            4,
            substitution_request.lane_id,
            substitution_request.run_id,
            attempt=1,
            attempt_limit=1,
            model_restart_limit=0,
            accepted_callback_id=str(accepted_value["callback_id"]),
            accepted_callback_kind="result",
            accepted_callback_sha256=accepted_sha,
        )
        substitution_callback.write_text(
            json.dumps(substituted_value, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            substitution.reconcile(substitution_child, lambda value: value)
        except FreshRepairInvalid:
            substitution_rejected = True
        else:
            substitution_rejected = False
        substitution_failure = json.loads(
            (substitution.root / "failed.json").read_text()
        )
        substitution_failure_bytes = (
            substitution.root / "failed.json"
        ).read_bytes()
        restarted_substitution = FreshArtifactRepair.load(
            owner=substitution_owner
        )
        try:
            restarted_substitution.reconcile(
                substitution_child, lambda value: value
            )
        except FreshRepairInvalid:
            restart_rejected = True
        else:
            restart_rejected = False
        check(
            f"{family.value} rejects callback substitution across restart",
            substitution_rejected
            and restart_rejected
            and substitution_failure["stage"] == "accepted-callback-mismatch"
            and (substitution.root / "failed.json").read_bytes()
            == substitution_failure_bytes
            and json.loads(substitution_target.read_text())
            == substitution_owner.template_value,
        )

with tempfile.TemporaryDirectory(prefix="fresh-artifact-crash.") as raw:
    base = Path(raw)
    tree = base / "product"
    state = base / "state"
    tree.mkdir()
    state.mkdir()
    target = tree / ".task-summary.json"
    owner = ContractArtifactOwner.publish(
        state_root=state,
        worktree=tree,
        template=template,
        actual_target=target,
    )
    owner.restore_template()
    crash = FreshArtifactRepair.reserve(
        owner=owner,
        parent=parent,
        invalid_sha256="3" * 64,
        route=opposite,
        origin_surface="11111111-1111-4111-8111-111111111111",
    )
    crash.fault_observer = lambda boundary: (
        (_ for _ in ()).throw(RuntimeError("crash"))
        if boundary == "fresh-effect-reserved"
        else None
    )
    try:
        crash.start(FakeManager())
    except RuntimeError:
        pass
    reloaded = FreshArtifactRepair.load(owner=owner)
    try:
        reloaded.start(FakeManager())
    except FreshRepairEffectUncertain:
        fail_closed = True
    else:
        fail_closed = False
    check("restart never replays an uncertain provider effect", fail_closed)

try:
    FreshArtifactRepair.reserve(
        owner=owner,
        parent=parent,
        invalid_sha256="4" * 64,
        route=opposite,
        origin_surface="11111111-1111-4111-8111-111111111111",
        family=ContractFamily.VERIFICATION_ESCALATION,
    )
except ValueError:
    forbidden = True
else:
    forbidden = False
check("code-owned and unregistered fresh targets fail closed", forbidden)

if failures:
    raise SystemExit(f"{len(failures)} fresh artifact repair test(s) failed")
print("All fresh artifact repair tests passed.")
