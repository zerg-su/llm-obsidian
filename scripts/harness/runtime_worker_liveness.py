"""Extracted runtime-worker responsibility mixin."""

from __future__ import annotations
from dataclasses import replace
from .callback_submit_recovery import INVALID_ARTIFACT_STATES
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

    def _retire_accepted_callback_submit_binding(
        self,
        record: object,
        generation: int,
        binding_sha256: str,
        binding_status: str,
    ) -> bool:
        """Prove and retire only an accepted predecessor generation."""

        if binding_status != "sent":
            return False
        receipt_path = self.spec_path.parent / "callback-receipt.json"
        try:
            if receipt_path.is_symlink() or not receipt_path.is_file():
                return False
            raw = receipt_path.read_bytes()
            if not raw or len(raw) > MAX_OUTBOX_BYTES:
                return False
            receipt = json.loads(raw)
            prior_generation = receipt.get("generation")
            operation_id = receipt.get("operation_id")
            run_id = receipt.get("run_id")
            callback_id = receipt.get("callback_id")
            payload_sha256 = receipt.get("payload_sha256")
            if (
                receipt.get("schema_version") != 1
                or receipt.get("status") not in {"accepted", "duplicate"}
                or type(prior_generation) is not int
                or prior_generation < 1
                or prior_generation >= generation
                or not isinstance(operation_id, str)
                or not IDENTIFIER.fullmatch(operation_id)
                or not isinstance(run_id, str)
                or not IDENTIFIER.fullmatch(run_id)
                or not isinstance(callback_id, str)
                or not IDENTIFIER.fullmatch(callback_id)
                or not isinstance(payload_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", payload_sha256)
            ):
                return False
            accepted_child = self.store.read(
                self.spec["owner_id"], operation_id
            )
            if (
                accepted_child.run_id != run_id
                or accepted_child.lane_id != record.lane_id
                or accepted_child.accepted_callback_kind != "review"
                or accepted_child.accepted_callback_id != callback_id
                or accepted_child.accepted_callback_sha256 != payload_sha256
            ):
                return False
            self.liveness_controller.retire_callback_submit_after_acceptance(
                binding_sha256,
                hashlib.sha256(raw).hexdigest(),
                generation=prior_generation,
                operation_id=operation_id,
                run_id=run_id,
                lane_id=accepted_child.lane_id,
            )
            return True
        except (
            ContractError,
            HarnessContractError,
            StoreError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return False

    def _expected_callback_child(
        self,
        parent: object,
        operation_id: str,
        run_id: str,
    ) -> object | None:
        """Resolve callback identity independently from callback-target.json."""

        try:
            child = self.store.read(self.spec["owner_id"], operation_id)
        except StoreError:
            return None
        expected_kind = (
            parent.spec.kind
            if operation_id == parent.spec.operation_id
            else "review-round"
        )
        if (
            child.run_id != run_id
            or child.lane_id != parent.lane_id
            or child.state != "awaiting-callback"
            or child.accepted_callback_id
            or child.spec.kind != expected_kind
        ):
            return None
        return child

    def callback_submit_attention(self, reason: str) -> None:
        marker = self.spec_path.parent / "callback-submit-attention.json"
        value = {
            "schema_version": 1,
            "operation_id": self.spec["operation_id"],
            "run_id": self.spec["run_id"],
            "reason": reason,
            "status": "attention-required",
        }
        marker_matches = False
        if marker.is_file() and not marker.is_symlink():
            try:
                if json.loads(marker.read_text(encoding="utf-8")) == value:
                    marker_matches = True
            except (OSError, json.JSONDecodeError):
                pass
        if not marker_matches:
            _atomic_json(marker, value)
        try:
            current = self.store.read(
                self.spec["owner_id"], self.spec["operation_id"]
            )
            if current.state in TERMINAL or current.state == "attention-required":
                return
            self.store.transition(
                self.spec["owner_id"],
                self.spec["operation_id"],
                "attention-required",
                reason=AttentionReason.ATTENTION_REQUIRED,
            )
        except (StoreError, OSError, ValueError, TypeError) as exc:
            raise RuntimeWorkerError(
                "callback submit attention transition failed"
            ) from exc

    def _reconcile_sent_callback_submit(
        self, binding_sha256: str, current: object
    ) -> object | None:
        """Heal the exact sent-state/reserved-receipt crash phase."""

        if getattr(current, "callback_submit_status", "") != "sent":
            return current
        try:
            self.liveness_controller.mark_callback_submit_sent(binding_sha256)
            return self.liveness_controller.current_state()
        except (ContractError, OSError, TypeError, ValueError):
            self.callback_submit_attention("callback-submit-evidence-malformed")
            return None

    def _settle_reserved_callback_submit(
        self, binding_sha256: str, artifact_sha256: str
    ) -> bool:
        try:
            self.liveness_controller.settle_callback_submit_with_artifact(
                binding_sha256,
                artifact_sha256,
            )
        except (ContractError, OSError, TypeError, ValueError):
            self.callback_submit_attention("callback-submit-evidence-malformed")
            return False
        return True

    def _artifact_after_callback_reservation(
        self, callback_path: Path, binding_sha256: str
    ) -> str:
        """Return send, wait, or handled after an exact post-reserve read."""

        (
            input_artifact,
            self.callback_recovery_input_digest,
            self.callback_recovery_input_reads,
        ) = observe_review_artifact(
            callback_path.with_name(".review-input.json"),
            self.callback_recovery_input_digest,
            self.callback_recovery_input_reads,
        )
        (
            callback_artifact,
            self.callback_recovery_digest,
            self.callback_recovery_reads,
        ) = observe_review_artifact(
            callback_path,
            self.callback_recovery_digest,
            self.callback_recovery_reads,
        )
        current_receipt_sha256 = _current_callback_receipt_sha256(
            self.spec_path.parent
        )
        if (
            input_artifact.state in INVALID_ARTIFACT_STATES
            or callback_artifact.state in INVALID_ARTIFACT_STATES
        ):
            self.callback_submit_attention("callback-submit-artifact-invalid")
            return "handled"
        winner = current_receipt_sha256
        if not winner and input_artifact.state == "stable":
            winner = input_artifact.sha256
        if not winner and callback_artifact.state == "stable":
            winner = callback_artifact.sha256
        if winner:
            if self._settle_reserved_callback_submit(binding_sha256, winner):
                self.inspect_callback()
            return "handled"
        if (
            input_artifact.state == "unstable"
            or callback_artifact.state == "unstable"
        ):
            return "wait"
        return "send"

    def _send_callback_submit_recovery(
        self, callback_path: Path, binding_sha256: str
    ) -> None:
        input_path = callback_path.with_name(".review-input.json")
        submit_command = shlex.join(
            (
                sys.executable,
                str(self.trusted_vault / "scripts/harness/review_submit.py"),
                "--worktree",
                str(self.spec["product_root"]),
                "--state-dir",
                str(callback_path.parent),
                "--input-file",
                str(input_path),
            )
        )
        message = (
            "Harness callback-submit recovery: write the already completed "
            f"review JSON only to {input_path}, then run this exact command: "
            f"{submit_command}. Do not write the callback manually."
        )
        if len(message.encode()) > 4096:
            self.callback_submit_attention("callback-submit-evidence-malformed")
            return
        try:
            self.cmux_adapter.send(self.spec["surface_id"], message)
            self.cmux_adapter.send_key(self.spec["surface_id"], "Enter")
        except Exception:
            try:
                self.liveness_controller.mark_callback_submit_uncertain(
                    binding_sha256
                )
            except Exception:
                pass
            self.callback_submit_attention("callback-submit-effect-uncertain")
            return
        self.liveness_controller.mark_callback_submit_sent(binding_sha256)

    def inspect_callback_submit_recovery(
        self, record: object, process_status: str
    ) -> None:
        try:
            generation, operation_id, run_id, callback_path = _callback_target(
                self.spec
            )
            target_sha256 = _bounded_file_sha256(
                self.spec["callback_registration"]
            )
            if not target_sha256:
                raise RuntimeWorkerError("callback target digest is unavailable")
        except RuntimeWorkerError:
            self.callback_submit_attention("callback-submit-evidence-malformed")
            return
        receipt_sha256 = _current_callback_receipt_sha256(
            self.spec_path.parent
        )
        if receipt_sha256:
            try:
                accepted_child = self.store.read(
                    self.spec["owner_id"], operation_id
                )
            except StoreError:
                accepted_child = None
            if (
                accepted_child is not None
                and accepted_child.run_id == run_id
                and accepted_child.lane_id == record.lane_id
                and accepted_child.accepted_callback_kind == "review"
                and bool(accepted_child.accepted_callback_id)
                and bool(accepted_child.accepted_callback_sha256)
                and _current_callback_receipt_sha256(
                    self.spec_path.parent,
                    expected_callback_id=accepted_child.accepted_callback_id,
                    expected_payload_sha256=(
                        accepted_child.accepted_callback_sha256
                    ),
                )
                == receipt_sha256
            ):
                current = self.liveness_controller.current_state()
                if (
                    current is not None
                    and current.callback_submit_status == "sent"
                ):
                    self._reconcile_sent_callback_submit(
                        current.callback_submit_binding, current
                    )
                elif (
                    current is not None
                    and current.callback_submit_status == "reserved"
                ):
                    self._settle_reserved_callback_submit(
                        current.callback_submit_binding, receipt_sha256
                    )
                return
            self.callback_submit_attention("callback-submit-stale-generation")
            return
        child = self._expected_callback_child(record, operation_id, run_id)
        if child is None:
            self.callback_submit_attention("callback-submit-stale-generation")
            return

        observed_at = self.clock()
        generation_identity = (
            f"{generation}:{operation_id}:{run_id}:{target_sha256}"
        )
        if generation_identity != self.callback_generation_identity:
            self.callback_generation_identity = generation_identity
            try:
                target_created_at = self.spec["callback_registration"].stat().st_mtime
            except OSError:
                target_created_at = observed_at
            self.callback_generation_progress_at = min(
                observed_at, target_created_at
            )
            self.callback_idle_observations = 0
            self.callback_prompt_observations = 0
            self.callback_recovery_input_digest = ""
            self.callback_recovery_input_reads = 0
            self.callback_recovery_digest = ""
            self.callback_recovery_reads = 0

        reader = getattr(self.cmux_adapter, "read", None)
        if reader is not None:
            try:
                screen = str(reader(self.spec["surface_id"]))
                encoded = screen.encode("utf-8", errors="replace")
                if encoded and len(encoded) <= MAX_SCREEN_BYTES:
                    prompt = classify(
                        self.spec["runtime"],
                        screen,
                        closure_armed=record.state == "exiting",
                    )
                    self.latest_callback_prompt_class = classify_callback_prompt(
                        self.spec["runtime"],
                        screen,
                        interactive=prompt.interactive,
                        recognized=prompt.recognized,
                    )
            except Exception:
                self.latest_callback_prompt_class = "unknown"

        self.callback_prompt_observations += 1
        if self.latest_callback_prompt_class == "idle-prompt":
            self.callback_idle_observations += 1
        else:
            self.callback_idle_observations = 0

        input_artifact, self.callback_recovery_input_digest, self.callback_recovery_input_reads = observe_review_artifact(
            callback_path.with_name(".review-input.json"),
            self.callback_recovery_input_digest,
            self.callback_recovery_input_reads,
        )
        callback_artifact, self.callback_recovery_digest, self.callback_recovery_reads = observe_review_artifact(
            callback_path,
            self.callback_recovery_digest,
            self.callback_recovery_reads,
        )
        receipt_sha256 = _current_callback_receipt_sha256(self.spec_path.parent)
        receipt_artifact = (
            ArtifactEvidence("stable", receipt_sha256)
            if receipt_sha256
            else ArtifactEvidence()
        )
        current = self.liveness_controller.current_state()
        if current is None:
            self.liveness_controller.observe(
                LivenessEvidence(
                    observed_at=observed_at,
                    process_status=process_status,
                    operation_revision=record.revision,
                    operation_state=record.state,
                    callback_sha256=callback_artifact.sha256,
                    receipt_sha256=receipt_artifact.sha256,
                ),
                self.liveness_policy,
            )
            current = self.liveness_controller.current_state()
        if current is None:
            self.callback_submit_attention("callback-submit-evidence-malformed")
            return

        try:
            surface_status = self.cmux_adapter.status(self.spec["surface_id"])
        except Exception:
            surface_status = "unknown"
        exact_resources = (
            record.resources.surface_id == self.spec["surface_id"]
            and record.resources.process_group == self.handle.process_group
            and record.resources.process_identity == self.handle.process_identity
        )
        if not exact_resources:
            surface_status = "unknown"
        evidence = CallbackSubmitEvidence(
            observed_at=observed_at,
            generation_progress_at=self.callback_generation_progress_at,
            callback_deadline_at=record.deadline_at,
            operation_id=operation_id,
            run_id=run_id,
            lane_id=child.lane_id,
            generation=generation,
            expected_operation_id=child.spec.operation_id,
            expected_run_id=child.run_id,
            expected_lane_id=child.lane_id,
            expected_generation=generation,
            target_sha256=target_sha256,
            expected_target_sha256=target_sha256,
            operation_state=record.state,
            process_status=process_status,
            surface_status=surface_status,
            prompt_class=self.latest_callback_prompt_class,
            stable_idle_observations=self.callback_idle_observations,
            input_artifact=input_artifact,
            callback_artifact=callback_artifact,
            receipt_artifact=receipt_artifact,
            nudge_count=current.nudge_count,
            restart_count=current.restart_count,
        )
        if (
            self.callback_prompt_observations < 2
            and input_artifact.state in {"missing", "unstable"}
            and callback_artifact.state in {"missing", "unstable"}
            and receipt_artifact.state == "missing"
        ):
            return
        binding_sha256 = callback_submit_binding_sha256(evidence)
        if current.callback_submit_binding == binding_sha256:
            current = self._reconcile_sent_callback_submit(
                binding_sha256, current
            )
            if current is None:
                return
            evidence = replace(
                evidence, recovery_status=current.callback_submit_status
            )
        elif current.callback_submit_binding:
            if not self._retire_accepted_callback_submit_binding(
                record,
                generation,
                current.callback_submit_binding,
                current.callback_submit_status,
            ):
                self.callback_submit_attention(
                    "callback-submit-stale-generation"
                )
                return
            current = self.liveness_controller.current_state()
            if current is None:
                self.callback_submit_attention(
                    "callback-submit-evidence-malformed"
                )
                return
            evidence = replace(
                evidence,
                nudge_count=current.nudge_count,
                restart_count=current.restart_count,
            )
        decision = classify_callback_submit(evidence, self.callback_submit_policy)
        if decision.action in {"submit-typed-input", "accept-callback"}:
            if current.callback_submit_status == "reserved":
                artifact_sha256 = (
                    input_artifact.sha256
                    if decision.action == "submit-typed-input"
                    else callback_artifact.sha256
                )
                if not self._settle_reserved_callback_submit(
                    current.callback_submit_binding, artifact_sha256
                ):
                    return
            self.inspect_callback()
            return
        if decision.action == "attention-required":
            self.callback_submit_attention(decision.reason)
            return
        if decision.action == "reserve-submit-recovery":
            if (
                _bounded_file_sha256(self.spec["callback_registration"])
                != target_sha256
                or self._expected_callback_child(record, operation_id, run_id)
                is None
            ):
                self.callback_submit_attention("callback-submit-stale-generation")
                return
            if not self.liveness_controller.reserve_callback_submit(
                binding_sha256,
                callback_submit_binding_identity(evidence),
            ):
                return
        else:
            return

        # Reservation is durable. Re-read every artifact and target immediately
        # before the single provider-facing effect.
        if _bounded_file_sha256(self.spec["callback_registration"]) != target_sha256:
            self.callback_submit_attention("callback-submit-stale-generation")
            return
        if self._expected_callback_child(record, operation_id, run_id) is None:
            self.callback_submit_attention("callback-submit-stale-generation")
            return
        if (
            self._artifact_after_callback_reservation(
                callback_path, binding_sha256
            )
            != "send"
        ):
            return
        self._send_callback_submit_recovery(callback_path, binding_sha256)

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
            if (
                record.spec.route.profile == "reviewer-callback"
                and process_status != "dead"
            ):
                self.inspect_callback_submit_recovery(record, process_status)
                return
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
