"""Extracted runtime-worker responsibility mixin."""

from __future__ import annotations

MODEL_JSON_BOUNDARIES = ("pipeline-step-result",)
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
from .workflows.research_contracts import (
    fetch_callback_payload,
    research_callback_identity,
)
from task_escalation_records import EscalationRecordError, append_raise
from .artifact_repair import ContractArtifactOwner


class RuntimeWorkerCustomMixin:

    def notify_custom_step(self, request: dict[str, object]) -> None:
        operation_id = str(request["operation_id"])
        notify_path = (
            self.spec_path.parent
            / "pipeline-custom"
            / "notifications"
            / f"{operation_id}.json"
        )
        marker = {
            "schema_version": 1,
            "operation_id": operation_id,
            "step_id": str(request["step_id"]),
            "visit": int(request["visit"]),
            "status": "sent",
        }
        if notify_path.is_file() and (not notify_path.is_symlink()):
            if json.loads(notify_path.read_text(encoding="utf-8")) != marker:
                raise RuntimeWorkerError("custom step notification changed")
            return
        allowed = request["allowed_outcomes"]
        if not isinstance(allowed, list):
            raise RuntimeWorkerError("custom step outcomes are unavailable")
        message = f"Typed custom step {request['step_id']} visit {request['visit']} is ready in .task-pipeline-step-request.json. Complete only this registered step, write its exact evidence/result, choose one of these outcomes: {', '.join((str(item) for item in allowed))}; then publish with pipeline-step-submit.py. Remain in this same session for the next harness-owned transition."
        if len(message.encode()) > 4096:
            raise RuntimeWorkerError("custom step notification exceeds its bound")
        self.cmux_adapter.send(self.spec["surface_id"], message)
        self.cmux_adapter.send_key(self.spec["surface_id"], "Enter")
        self.write_immutable_json(notify_path, marker)

    def notify_custom_finalization(self, receipt_count: int) -> None:
        notify_path = (
            self.spec_path.parent / "pipeline-custom" / "finalization-notify.json"
        )
        marker = {
            "schema_version": 1,
            "operation_id": self.spec["operation_id"],
            "receipt_count": receipt_count,
            "status": "sent",
        }
        if notify_path.is_file() and (not notify_path.is_symlink()):
            if json.loads(notify_path.read_text(encoding="utf-8")) != marker:
                raise RuntimeWorkerError("custom finalization notification changed")
            return
        message = f"All {receipt_count} custom model-step receipts are accepted. Finish the task in this same session, commit the approved result, run only task-specific checks not already owned by the harness, and write the canonical .task-summary.json. The harness now owns configured verification and review."
        self.cmux_adapter.send(self.spec["surface_id"], message)
        self.cmux_adapter.send_key(self.spec["surface_id"], "Enter")
        self.write_immutable_json(notify_path, marker)

    def notify_custom_attention(
        self, outcome: str, receipt: CustomStepReceipt | None
    ) -> None:
        receipt_sha256 = receipt.receipt_sha256 if receipt is not None else ""
        path = self.spec["cwd"] / ".task-needs-attention.json"
        packet = {
            "version": 1,
            "id": f"custom-decision-{(receipt_sha256 or self.pipeline.definition_sha256)[:24]}",
            "status": "pending",
            "task_name": "custom pipeline decision",
            "category": "pipeline-decision",
            "reason": "The approved custom pipeline reached a declared terminal outcome",
            "question": f"Resolve declared outcome: {outcome}",
            "worktree": str(self.spec["cwd"]),
            "task_surface": self.spec["surface_id"],
            "raised_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "receipt_operation_id": receipt.operation_id if receipt is not None else "",
            "receipt_sha256": receipt_sha256,
            "allowed_decisions": ["stop", "reapprove-pipeline"],
        }
        try:
            raised = append_raise(self.spec["cwd"], packet)
        except EscalationRecordError as exc:
            raise RuntimeWorkerError(f"custom decision packet is invalid: {exc}") from exc
        if raised.record_id != packet["id"] or raised.payload.get("status") != "pending":
            return
        notify_path = (
            self.spec_path.parent / "pipeline-custom" / "attention-notify.json"
        )
        if notify_path.is_file() and (not notify_path.is_symlink()):
            return
        command = (
            "python3 "
            + shlex.quote(str(self.trusted_vault / "scripts" / "task_escalation.py"))
            + " resolve --worktree "
            + shlex.quote(str(self.spec["cwd"]))
            + " --decision <decision>"
        )
        self.cmux_adapter.send(
            self.spec["origin_surface"],
            f"Typed custom pipeline escalation received. Inspect {path} and resolve from the originating coordinator with: {command}. Allowed decisions: stop, reapprove-pipeline.",
        )
        self.cmux_adapter.send_key(self.spec["origin_surface"], "Enter")
        self.write_immutable_json(
            notify_path,
            {
                "schema_version": 1,
                "operation_id": self.spec["operation_id"],
                "receipt_sha256": receipt_sha256,
                "status": "sent",
            },
        )

    def drive_custom_transport(self) -> None:
        if (
            not self.is_custom_pipeline
            or self.custom_pipeline_spec is None
            or self.pipeline is None
            or self.callback_handled
            or self.custom_transport_complete
        ):
            return
        try:
            meta = json.loads(
                (self.spec["cwd"] / ".task-meta.json").read_text(encoding="utf-8")
            )
            policy = meta.get("pipeline_policy") if isinstance(meta, dict) else None
            if (
                not isinstance(policy, dict)
                or policy.get("definition_sha256") != self.pipeline.definition_sha256
            ):
                raise RuntimeWorkerError(
                    "custom metadata mismatches its compiled contract"
                )
            approved_plan_sha256 = str(meta.get("approved_plan_sha256") or "")
            controller_path = (
                self.spec_path.parent / "pipeline-custom" / "controller.json"
            )
            if controller_path.is_symlink():
                raise RuntimeWorkerError("custom controller must not be a symlink")
            if controller_path.is_file():
                controller = json.loads(controller_path.read_text(encoding="utf-8"))
                if (
                    not isinstance(controller, dict)
                    or set(controller)
                    != {
                        "schema_version",
                        "operation_id",
                        "definition_sha256",
                        "approved_plan_sha256",
                        "initial_head_sha",
                    }
                    or controller.get("schema_version") != 1
                    or (controller.get("operation_id") != self.spec["operation_id"])
                    or (
                        controller.get("definition_sha256")
                        != self.pipeline.definition_sha256
                    )
                    or (controller.get("approved_plan_sha256") != approved_plan_sha256)
                ):
                    raise RuntimeWorkerError("custom controller receipt changed")
            else:
                initial_request = json.loads(
                    (self.spec["cwd"] / ".task-pipeline-step-request.json").read_text(
                        encoding="utf-8"
                    )
                )
                initial_head_sha = str(
                    initial_request.get("input_head_sha")
                    if isinstance(initial_request, dict)
                    else ""
                )
                if not re.fullmatch("[0-9a-f]{40,64}", initial_head_sha):
                    raise RuntimeWorkerError("custom initial HEAD is unavailable")
                controller = {
                    "schema_version": 1,
                    "operation_id": self.spec["operation_id"],
                    "definition_sha256": self.pipeline.definition_sha256,
                    "approved_plan_sha256": approved_plan_sha256,
                    "initial_head_sha": initial_head_sha,
                }
                self.write_immutable_json(controller_path, controller)
            receipt_root = self.spec_path.parent / "pipeline-custom" / "receipts"
            receipts: list[CustomStepReceipt] = []
            if receipt_root.is_dir():
                paths = sorted(receipt_root.glob("*.json"))
                expected_names = [f"{index:03d}.json" for index in range(len(paths))]
                if [path.name for path in paths] != expected_names:
                    raise RuntimeWorkerError(
                        "custom receipts are not a contiguous prefix"
                    )
                receipts = [load_custom_receipt(path) for path in paths]
            parent = self.store.read(self.spec["owner_id"], self.spec["operation_id"])
            progress = reconcile_custom_sequence(
                parent,
                self.custom_pipeline_spec,
                definition_sha256=self.pipeline.definition_sha256,
                approved_plan_sha256=approved_plan_sha256,
                initial_head_sha=str(controller["initial_head_sha"]),
                receipts=tuple(receipts),
            )
            if progress.action == "attention":
                self.notify_custom_attention(
                    progress.terminal_outcome, progress.prior_receipt
                )
                self.summary_attention(
                    f"pipeline-custom-{progress.terminal_outcome}",
                    AttentionReason.ATTENTION_REQUIRED,
                )
                return
            if progress.action == "complete":
                self.custom_transport_complete = True
                self.notify_custom_finalization(len(receipts))
                emit_compiled_pipeline_event(
                    self.spec["cwd"],
                    event="custom-model-steps-complete",
                    pipeline_id=self.pipeline.definition.pipeline_id,
                    pipeline_version=self.pipeline.definition.version,
                    profile=self.pipeline.definition.profile,
                    compiler_outcome="custom-resolved",
                    definition_sha=self.pipeline.definition_sha256,
                    primitive_count=len(self.pipeline.definition.steps),
                    loop_iteration=max(0, len(receipts) - 1),
                    terminal_category="model-steps-complete",
                )
                return
            if self.spec["task_summary_pointer"].is_file():
                _atomic_json(
                    self.spec_path.parent / "pipeline-custom" / "early-summary.json",
                    {
                        "schema_version": 1,
                        "operation_id": self.spec["operation_id"],
                        "status": "ignored-until-model-steps-complete",
                    },
                )
            round_ = prepare_custom_step(
                self.store,
                parent,
                self.custom_pipeline_spec,
                definition_sha256=self.pipeline.definition_sha256,
                approved_plan_sha256=approved_plan_sha256,
                initial_head_sha=str(controller["initial_head_sha"]),
                receipts=tuple(receipts),
            )
            request = custom_step_request(round_)
            self.publish_pipeline_step_contract(request)
            _atomic_json(self.spec["cwd"] / ".task-pipeline-step-request.json", request)
            self.retarget_fix_callback(
                operation_id=round_.spec.operation_id,
                run_id=round_.run_id,
                callback_pointer=".task-pipeline-step-callback.json",
            )
            self.notify_custom_step(request)
            _generation, operation_id, run_id, callback_path = _callback_target(
                self.spec
            )
            if operation_id != round_.spec.operation_id or run_id != round_.run_id:
                raise RuntimeWorkerError("custom callback target changed")
            if not callback_path.exists():
                result_path = self.spec["cwd"] / str(request["result_pointer"])
                result_digest = _bounded_file_sha256(result_path)
                if result_digest:
                    owner = getattr(self, "pipeline_step_artifact_owner", None)
                    if (
                        isinstance(owner, ContractArtifactOwner)
                        and owner.actual_target == result_path
                        and result_digest == owner.template_artifact_sha256
                    ):
                        return
                    output_path = self.spec["cwd"] / str(request["output_pointer"])
                    output_digest = _bounded_file_sha256(output_path)
                    if not output_digest:
                        return
                    if output_digest != self.custom_output_digest:
                        self.custom_output_digest = output_digest
                        self.custom_output_stable_reads = 1
                        return
                    self.custom_output_stable_reads += 1
                    if result_digest != self.custom_result_digest:
                        self.custom_result_digest = result_digest
                        self.custom_result_stable_reads = 1
                    else:
                        self.custom_result_stable_reads += 1
                    if (
                        self.custom_result_stable_reads >= 2
                        and self.custom_output_stable_reads >= 2
                        and self.custom_submit_attempt_digest != result_digest
                    ):
                        self.custom_submit_attempt_digest = result_digest
                        submitted = subprocess.run(
                            [
                                sys.executable,
                                str(
                                    self.trusted_vault
                                    / "scripts"
                                    / "pipeline-step-submit.py"
                                ),
                                "--worktree",
                                str(self.spec["cwd"]),
                            ],
                            cwd=self.spec["cwd"],
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        if _submit_failure_requires_attention(submitted, callback_path):
                            _atomic_json(
                                self.spec_path.parent
                                / "pipeline-custom"
                                / "submit-failed.json",
                                {
                                    "schema_version": 1,
                                    "operation_id": round_.spec.operation_id,
                                    "returncode": submitted.returncode,
                                    "status": "attention-required",
                                },
                            )
                            current_digest = (
                                _bounded_file_sha256(result_path) or result_digest
                            )
                            self.request_pipeline_step_correction(
                                current_digest,
                                stage="pipeline-custom-submit",
                            )
                return
            raw = callback_path.read_bytes()
            if not raw or len(raw) > MAX_OUTBOX_BYTES:
                raise RuntimeWorkerError("custom callback is invalid")
            digest = hashlib.sha256(raw).hexdigest()
            if digest != self.custom_callback_digest:
                self.custom_callback_digest = digest
                self.custom_callback_stable_reads = 1
                return
            self.custom_callback_stable_reads += 1
            if self.custom_callback_stable_reads < 2:
                return
            envelope = _envelope(json.loads(raw))
            accepted = accept_custom_step(
                self.store,
                round_,
                envelope,
                current_head_sha=self.git_head(),
                receipt_path=receipt_root / f"{round_.visit:03d}.json",
            )
            callback_path.unlink()
            emit_compiled_pipeline_event(
                self.spec["cwd"],
                event="custom-step-accepted",
                pipeline_id=self.pipeline.definition.pipeline_id,
                pipeline_version=self.pipeline.definition.version,
                profile=self.pipeline.definition.profile,
                compiler_outcome="custom-resolved",
                definition_sha=self.pipeline.definition_sha256,
                primitive_count=len(self.pipeline.definition.steps),
                loop_iteration=accepted.visit,
                terminal_category=accepted.step_id,
            )
            self.custom_callback_digest = ""
            self.custom_callback_stable_reads = 0
            self.custom_result_digest = ""
            self.custom_result_stable_reads = 0
            self.custom_output_digest = ""
            self.custom_output_stable_reads = 0
            self.custom_submit_attempt_digest = ""
        except (
            CustomSequenceError,
            RuntimeWorkerError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            self.summary_attention("pipeline-custom-callback-invalid")

    def recover_task_summary_attention(self) -> None:
        if self.spec["callback_mode"] != "task-summary":
            return
        self.recover_restart_summary_attention()
        if not self.callback_handled or self.summary_attention_revision < 0:
            return
        try:
            current = self.store.read(self.spec["owner_id"], self.spec["operation_id"])
        except Exception:
            return
        if (
            current.state not in CALLBACK_WAIT_STATES
            or current.revision <= self.summary_attention_revision
        ):
            return
        _atomic_json(
            self.spec_path.parent / "callback-recovery.json",
            {
                "schema_version": 1,
                "operation_id": self.spec["operation_id"],
                "attention_revision": self.summary_attention_revision,
                "resumed_revision": current.revision,
                "status": "resumed",
            },
        )
        self.callback_handled = False
        self.summary_digest = ""
        self.summary_stable_reads = 0
        self.summary_attention_revision = -1

    def recover_restart_summary_attention(self) -> None:
        """Resume once per generation from a durable mechanism attention latch.

        A prior worker generation that latched a typed mechanism/transport
        failure exits immediately on restart, so the restarted generation is
        the sole owner that can consume the durable resume boundary.  Exactly
        one recovery per generation keeps a persistent failure fail-closed:
        it re-latches on the next poll and stays with the coordinator.
        """

        if getattr(self, "restart_attention_recovery_done", True):
            return
        if self.callback_handled or self.summary_attention_revision >= 0:
            return
        latch_path = self.spec_path.parent / "callback-error.json"
        if not latch_path.is_file() or latch_path.is_symlink():
            return
        try:
            latch = json.loads(latch_path.read_text(encoding="utf-8"))
            current = self.store.read(
                self.spec["owner_id"], self.spec["operation_id"]
            )
        except Exception:
            return
        if (
            not isinstance(latch, dict)
            or latch.get("status") not in RESUMABLE_SUMMARY_ATTENTION
            or current.state != "attention-required"
            or current.resume_state not in CALLBACK_WAIT_STATES
        ):
            return
        self.restart_attention_recovery_done = True
        try:
            self.store.transition(
                self.spec["owner_id"],
                self.spec["operation_id"],
                current.resume_state,
            )
        except Exception:
            return
        _atomic_json(
            self.spec_path.parent / "callback-recovery.json",
            {
                "schema_version": 1,
                "operation_id": self.spec["operation_id"],
                "attention_revision": current.revision,
                "resumed_revision": current.revision + 1,
                "attention_status": str(latch.get("status")),
                "status": "restart-resumed",
            },
        )

    def inspect_task_summary(self) -> None:
        if self.callback_handled:
            return
        if (
            self._pipeline_name == "engineering/fix"
            and (not self.fix_transport_complete)
            or (self.is_custom_pipeline and (not self.custom_transport_complete))
        ):
            return
        summary_path: Path = self.spec["task_summary_pointer"]
        try:
            raw = summary_path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            self.summary_attention("wiki-summary-unreadable")
            return
        if not raw or len(raw) > MAX_OUTBOX_BYTES:
            self.summary_attention("wiki-summary-invalid")
            return
        self.finish_task_summary(raw)

    def inspect_research(self) -> None:
        if self.callback_handled:
            return
        try:
            target = _callback_target(self.spec)
        except RuntimeWorkerError:
            self.summary_attention("research-callback-invalid")
            return
        if target != self.active_target:
            if self.active_target is not None and target[0] <= self.active_target[0]:
                return
            self.active_target = target
            self.last_digest = ""
            self.stable_reads = 0
        generation, operation_id, run_id, callback_path = target
        try:
            raw = callback_path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            self.summary_attention("research-callback-unreadable")
            return
        if not raw or len(raw) > MAX_OUTBOX_BYTES:
            self.summary_attention("research-callback-invalid")
            return
        digest = hashlib.sha256(raw).hexdigest()
        if digest != self.last_digest:
            self.last_digest = digest
            self.stable_reads = 1
            return
        self.stable_reads += 1
        if self.stable_reads < 2:
            return
        try:
            if self.spec["callback_mode"] == "research-fetch":
                normalized_raw = _normalize_fetch_errors_at_provider_boundary(
                    callback_path, raw
                )
                if normalized_raw != raw:
                    self.last_digest = hashlib.sha256(normalized_raw).hexdigest()
                    self.stable_reads = 1
                    return
                artifact = load_artifact(
                    str(callback_path),
                    expected_run_id=run_id,
                    expected_request_sha256=self.spec["research_request_sha256"],
                )
                payload = fetch_callback_payload(
                    artifact_sha256=digest,
                    source_count=len(artifact["sources"]),
                )
            else:
                if (
                    not self.research_input_sha256
                    or _research_input_provenance(
                        self.spec, self.spec_path, create=False
                    )
                    != self.research_input_sha256
                ):
                    raise RuntimeWorkerError(
                        "research input artifact changed after launch"
                    )
                artifact = load_artifact(str(self.spec["cwd"] / "artifact.json"))
                complete = json.loads(raw)
                result = validate_result_artifact(
                    complete,
                    root=self.spec["cwd"],
                    expected_run_id=run_id,
                    source_urls={str(source["url"]) for source in artifact["sources"]},
                )
                payload = {
                    "stage": "synth",
                    "artifact_path": result["artifact"]["path"],
                    "artifact_sha256": result["artifact"]["sha256"],
                    "citation_count": len(result["artifact"]["citations"]),
                }
            callback_id, payload_sha256 = research_callback_identity(payload)
            envelope = CallbackEnvelope(
                callback_id=callback_id,
                operation_id=operation_id,
                run_id=run_id,
                kind="research",
                payload=payload,
                payload_sha256=payload_sha256,
            )
            acceptance = CallbackBroker(self.store, self.spec["owner_id"]).accept(
                envelope
            )
            self.callback_handled = True
            _atomic_json(
                self.spec_path.parent / "callback-receipt.json",
                {
                    "schema_version": 1,
                    "generation": generation,
                    "callback_id": envelope.callback_id,
                    "operation_id": operation_id,
                    "run_id": envelope.run_id,
                    "payload_sha256": envelope.payload_sha256,
                    "status": "duplicate" if acceptance.duplicate else "accepted",
                },
            )
            notify_path = self.spec_path.parent / "research-notify.json"
            if notify_path.exists():
                marker = json.loads(notify_path.read_text(encoding="utf-8"))
                if (
                    marker.get("schema_version") != 1
                    or marker.get("callback_id") != envelope.callback_id
                ):
                    raise RuntimeWorkerError("research notification marker is invalid")
                if marker.get("status") == "sent":
                    return
                if marker.get("status") == "pending":
                    self.store.transition(
                        self.spec["owner_id"],
                        self.spec["operation_id"],
                        "attention-required",
                        reason=AttentionReason.ATTENTION_REQUIRED,
                    )
                    return
                raise RuntimeWorkerError(
                    "research notification marker state is invalid"
                )
            _atomic_json(
                notify_path,
                {
                    "schema_version": 1,
                    "callback_id": envelope.callback_id,
                    "status": "pending",
                },
            )
            self.cmux_adapter.send(
                self.spec["origin_surface"], self.spec["callback_wake"]
            )
            self.cmux_adapter.send_key(self.spec["origin_surface"], "Enter")
            _atomic_json(
                notify_path,
                {
                    "schema_version": 1,
                    "callback_id": envelope.callback_id,
                    "status": "sent",
                },
            )
        except (
            CallbackError,
            ResearchContractError,
            RuntimeWorkerError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            self.summary_attention("research-callback-invalid")
            return
