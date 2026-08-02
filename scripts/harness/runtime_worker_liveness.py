"""Extracted runtime-worker responsibility mixin."""

from __future__ import annotations
from .runtime_worker import *
from .runtime_worker import (
    _atomic_json,
    _bounded_file_sha256,
    _callback_target,
    _contain_provider_start_failure,
    _current_callback_receipt_sha256,
    _envelope,
    _normalize_fetch_errors_at_provider_boundary,
    _pipeline_verify_identity,
    _research_input_provenance,
    _review_resolution_handoff_ready,
    _submit_failure_requires_attention,
)


class RuntimeWorkerLivenessMixin:

    def restart_for_liveness(self, action_id: str) -> None:
        supervisor = OperationSupervisor(
            self.store, self.spec["owner_id"], self.spec["operation_id"]
        )
        try:
            budgeted = supervisor.consume_model_restart(explicitly_permitted=True)
            old_handle = self.handle
            if not self.provider_exited:
                self.process.signal_owned_child_group(
                    old_handle.process_group,
                    old_handle.process_identity,
                    signal.SIGTERM,
                )
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    waited, _status = os.waitpid(old_handle.pid, os.WNOHANG)
                    if waited == old_handle.pid:
                        break
                    time.sleep(0.05)
                else:
                    self.process.signal_owned_child_group(
                        old_handle.process_group,
                        old_handle.process_identity,
                        signal.SIGKILL,
                    )
                    os.waitpid(old_handle.pid, 0)
            resume_command = provider_resume_argv(
                self.provider_command, str(self.spec["runtime"]), self.checkpoint
            )
            restarted = self.process.start(
                resume_command, cwd=self.spec["cwd"], env=self.provider_env
            )
            resources = budgeted.resources
            supervisor.bind_resources(
                OwnedResources(
                    surface_id=resources.surface_id or self.spec["surface_id"],
                    process_group=restarted.process_group,
                    supervisor_pid=resources.supervisor_pid or os.getpid(),
                    process_identity=restarted.process_identity,
                    supervisor_identity=resources.supervisor_identity
                    or self.supervisor_identity,
                )
            )
            self.handle = restarted
            self.provider_exited = False
            self.exit_code = 0
            self.exit_containment_failed = False
            self.write_immutable_json(
                self.spec_path.parent
                / "liveness"
                / f"provider-restart-{budgeted.model_restarts}.json",
                {
                    "schema_version": 1,
                    "action_id": action_id,
                    "operation_id": self.spec["operation_id"],
                    "run_id": self.spec["run_id"],
                    "model_restarts": budgeted.model_restarts,
                    "checkpoint": self.checkpoint,
                    "provider_argv_sha256": hashlib.sha256(
                        json.dumps(resume_command, separators=(",", ":")).encode()
                    ).hexdigest(),
                    "old_process_identity": old_handle.process_identity,
                    "new_process_identity": restarted.process_identity,
                    "status": "restarted",
                },
            )
        except (
            HarnessContractError,
            OSError,
            ProcessError,
            RuntimeWorkerError,
            StoreError,
            SupervisorError,
        ):
            try:
                current = self.store.read(
                    self.spec["owner_id"], self.spec["operation_id"]
                )
                if (
                    current.state not in TERMINAL
                    and current.state != "attention-required"
                ):
                    self.store.transition(
                        self.spec["owner_id"],
                        self.spec["operation_id"],
                        "attention-required",
                        reason=AttentionReason.ATTENTION_REQUIRED,
                    )
            except Exception:
                pass

    def inspect_liveness(self) -> None:
        try:
            record = self.store.read(self.spec["owner_id"], self.spec["operation_id"])
            process_status = (
                "dead"
                if self.provider_exited
                else self.process.process_status(
                    self.handle.process_group, self.handle.process_identity
                )
            )
            typed_result_path = self.spec["cwd"] / self.spec["task_summary_pointer"]
            if self.spec["callback_mode"] == "task-summary" and (
                self._pipeline_name == "engineering/fix"
                and (not self.fix_transport_complete)
                or (self.is_custom_pipeline and (not self.custom_transport_complete))
            ):
                try:
                    step_request = json.loads(
                        (
                            self.spec["cwd"] / ".task-pipeline-step-request.json"
                        ).read_text(encoding="utf-8")
                    )
                    typed_result_path = self.spec["cwd"] / str(
                        step_request.get("result_pointer") or ""
                    )
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pass
            typed_result_sha256 = (
                _bounded_file_sha256(typed_result_path)
                if self.spec["callback_mode"] == "task-summary"
                else ""
            )
            callback_sha256 = ""
            if (
                self.spec["callback_mode"] != "task-summary"
                or (
                    self._pipeline_name == "engineering/fix"
                    and (not self.fix_transport_complete)
                )
                or (self.is_custom_pipeline and (not self.custom_transport_complete))
            ):
                try:
                    callback_sha256 = _bounded_file_sha256(
                        _callback_target(self.spec)[3]
                    )
                except RuntimeWorkerError:
                    callback_sha256 = ""
            decision = self.liveness_controller.observe(
                LivenessEvidence(
                    observed_at=time.time(),
                    process_status=process_status,
                    operation_revision=record.revision,
                    operation_state=record.state,
                    screen_sha256=self.latest_screen_digest,
                    prompt_state=self.latest_prompt_state,
                    typed_result_sha256=typed_result_sha256,
                    callback_sha256=callback_sha256,
                    receipt_sha256=_current_callback_receipt_sha256(
                        self.spec_path.parent
                    ),
                ),
                self.liveness_policy,
            )
            if decision.action != "observe":
                telemetry_marker = (
                    self.spec_path.parent
                    / "liveness"
                    / "telemetry"
                    / f"{decision.action_id}.json"
                )
                if not telemetry_marker.exists():
                    emit_lifecycle_event(
                        self.spec["cwd"],
                        "pipeline-liveness",
                        actor=decision.action,
                        counts={"model_call": int(decision.model_call)},
                        identifiers={
                            "stage": decision.action,
                            "action_id": decision.action_id,
                        },
                        status=(
                            "degraded"
                            if decision.action
                            in {"suspected-idle", "attention-required"}
                            else "ok"
                        ),
                    )
                    _atomic_json(
                        telemetry_marker,
                        {
                            "schema_version": 1,
                            "action_id": decision.action_id,
                            "status": "emitted",
                        },
                    )
            if decision.action == "reconcile-result":
                if self.spec["callback_mode"] == "task-summary":
                    self.recover_task_summary_attention()
                    self.drive_fix_transport()
                    self.drive_custom_transport()
                    self.inspect_task_summary()
                elif self.spec["callback_mode"] in {"research-fetch", "research-synth"}:
                    self.inspect_research()
                else:
                    self.inspect_callback()
            elif decision.action == "nudge":
                self.cmux_adapter.send(
                    self.spec["surface_id"],
                    "Harness liveness check: continue the current task, or if it is complete, write the exact required typed callback now.",
                )
                self.cmux_adapter.send_key(self.spec["surface_id"], "Enter")
            elif decision.action == "restart":
                self.restart_for_liveness(decision.action_id)
            elif decision.action == "attention-required":
                current = self.store.read(
                    self.spec["owner_id"], self.spec["operation_id"]
                )
                if (
                    current.state not in TERMINAL
                    and current.state != "attention-required"
                ):
                    self.store.transition(
                        self.spec["owner_id"],
                        self.spec["operation_id"],
                        "attention-required",
                        reason=AttentionReason.RETRY_EXHAUSTED,
                    )
        except (
            HarnessContractError,
            OSError,
            ProcessError,
            StoreError,
            TypeError,
            ValueError,
        ):
            return
