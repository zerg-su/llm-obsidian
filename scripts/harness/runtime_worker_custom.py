"""Extracted runtime-worker responsibility mixin."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager

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
)
from .workflows.research_contracts import (
    fetch_callback_payload,
    research_callback_identity,
)
from task_escalation_records import EscalationRecordError, append_raise
from .artifact_repair import ContractArtifactOwner
from .review_continuation_recovery import (
    RecoveryDecision,
    RecoveryDisposition,
    RecoveryReason,
    RecoveryReceipt,
)
from .state_machine import transition as transition_operation
from .retained_notification import (
    RetainedNotificationError,
    deliver_worker_notification,
)


@contextmanager
def _review_continuation_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_REVIEW_CONTINUATION_RECOVERIES = frozenset(
    {
        RecoveryDisposition.REVIEW_DRIVE_REARM,
        RecoveryDisposition.ACCEPTED_CALLBACK_INGEST,
    }
)


def _recovery_receipt_dir(spec_path: Path) -> Path:
    # Continuation receipts reserve root-CAS recovery authority.  They are
    # intentionally separate from the generation-local callback-timeout
    # receipt, which records only one accepted timeout transition.
    return spec_path.parent / "review-continuation-recovery"


def _recovery_receipt_path(spec_path: Path, receipt: RecoveryReceipt) -> Path:
    return _recovery_receipt_dir(spec_path) / f"{receipt.identity.scope_sha256}.json"


def _read_recovery_receipt(path: Path) -> RecoveryReceipt | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            return None
        return RecoveryReceipt.from_mapping(raw)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _prepared_recovery_receipts(root: Path) -> list[tuple[Path, RecoveryReceipt]]:
    if not root.is_dir() or root.is_symlink():
        return []
    prepared: list[tuple[Path, RecoveryReceipt]] = []
    for path in sorted(root.glob("*.json")):
        receipt = _read_recovery_receipt(path)
        if receipt is not None and receipt.status == "prepared":
            prepared.append((path, receipt))
    return prepared


class RuntimeWorkerCustomMixin:

    def _deliver_custom_notification(
        self,
        notify_path: Path,
        marker: dict[str, object],
        message: str,
    ) -> None:
        try:
            deliver_worker_notification(
                self,
                notify_path=notify_path,
                marker=marker,
                message=message,
            )
        except RetainedNotificationError as exc:
            raise RuntimeWorkerError(
                "custom notification delivery or recovery failed"
            ) from exc

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
        allowed = request["allowed_outcomes"]
        if not isinstance(allowed, list):
            raise RuntimeWorkerError("custom step outcomes are unavailable")
        message = f"Typed custom step {request['step_id']} visit {request['visit']} is ready in .task-pipeline-step-request.json. Complete only this registered step, write its exact evidence/result, choose one of these outcomes: {', '.join((str(item) for item in allowed))}; then publish with pipeline-step-submit.py. Remain in this same session for the next harness-owned transition."
        if len(message.encode()) > 4096:
            raise RuntimeWorkerError("custom step notification exceeds its bound")
        self._deliver_custom_notification(notify_path, marker, message)

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
        message = f"All {receipt_count} custom model-step receipts are accepted. Finish the task in this same session, commit the approved result, run only task-specific checks not already owned by the harness, and write the canonical .task-summary.json. The harness now owns configured verification and review."
        self._deliver_custom_notification(notify_path, marker, message)

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
            request, _owner = self.publish_pipeline_step_contract(request)
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
                if self.adopt_fresh_pipeline_step_result():
                    return
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
                        self.handle_pipeline_step_submit_failure(
                            submitted,
                            callback_path,
                            receipt_path=(
                                self.spec_path.parent
                                / "pipeline-custom"
                                / "submit-failed.json"
                            ),
                            operation_id=round_.spec.operation_id,
                            invalid_sha256=(
                                _bounded_file_sha256(result_path) or result_digest
                            ),
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
        self.recover_review_continuation()
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

    def recover_review_continuation(self) -> None:
        """Rearm and advance one exact classifier-authorized recovery."""

        decision_owner = getattr(self, "review_continuation_decision", None)
        execution_owner = getattr(self, "execute_review_continuation", None)
        if decision_owner is None or execution_owner is None:
            return
        receipt_root = _recovery_receipt_dir(self.spec_path)
        try:
            observed_root = self.store.read(
                self.spec["owner_id"], self.spec["operation_id"]
            )
        except Exception:
            return
        initial: RecoveryDecision | None = None
        if (
            observed_root.state == "attention-required"
            and observed_root.resume_state in CALLBACK_WAIT_STATES
            and not observed_root.pending_effect
        ):
            initial = decision_owner()
            if (
                initial.receipt is None
                or initial.disposition not in _REVIEW_CONTINUATION_RECOVERIES
            ):
                initial = None
        elif not receipt_root.is_dir():
            return
        if initial is None and not receipt_root.is_dir():
            return

        lock_path = receipt_root / ".lock"
        with _review_continuation_lock(lock_path):
            receipt: RecoveryReceipt
            decision: RecoveryDecision
            prepared = _prepared_recovery_receipts(receipt_root)
            if len(prepared) > 1:
                attention_owner = getattr(self, "summary_attention", None)
                if attention_owner is not None:
                    attention_owner(
                        "review-continuation-recovery-ambiguous",
                        AttentionReason.ATTENTION_REQUIRED,
                    )
                return
            if prepared:
                receipt_path, receipt = prepared[0]
                disposition = (
                    RecoveryDisposition.REVIEW_DRIVE_REARM
                    if receipt.identity.recovery_class == "review-drive"
                    else RecoveryDisposition.ACCEPTED_CALLBACK_INGEST
                )
                decision = RecoveryDecision(
                    disposition,
                    RecoveryReason.ELIGIBLE,
                    receipt,
                )
            else:
                if initial is None or initial.receipt is None:
                    return
                decision = initial
                receipt = initial.receipt
                receipt_path = _recovery_receipt_path(self.spec_path, receipt)
                existing = _read_recovery_receipt(receipt_path)
                if existing is not None:
                    if existing.status == "finalized":
                        return
                    receipt = existing
                elif receipt_path.exists():
                    return
                else:
                    _atomic_json(receipt_path, receipt.payload())

            identity = receipt.identity
            if (
                identity.owner_id != self.spec["owner_id"]
                or identity.root_operation_id != self.spec["operation_id"]
            ):
                return
            try:
                current = self.store.read(
                    self.spec["owner_id"], self.spec["operation_id"]
                )
            except Exception:
                return
            exact_prepared = (
                current.run_id == identity.root_run_id
                and current.revision == identity.root_revision
                and current.state == "attention-required"
                and current.resume_state in CALLBACK_WAIT_STATES
                and not current.pending_effect
            )
            exact_transitioned = (
                current.run_id == identity.root_run_id
                and current.revision == identity.root_revision + 1
                and current.state in CALLBACK_WAIT_STATES
                and not current.pending_effect
            )
            if exact_prepared:
                try:
                    updated, result = transition_operation(
                        current, current.resume_state
                    )
                    if not result.changed:
                        return
                    self.store.save(
                        updated, expected_revision=identity.root_revision
                    )
                    current = updated
                except Exception:
                    return
            elif not exact_transitioned:
                completion_owner = getattr(
                    self,
                    "review_continuation_recovery_completed",
                    None,
                )
                try:
                    completed = bool(
                        completion_owner(identity)
                        if completion_owner is not None
                        else False
                    )
                except Exception:
                    completed = False
                if not completed:
                    _atomic_json(
                        receipt_path,
                        receipt.payload(
                            status="finalized",
                            outcome="refused",
                            reason="recovery-identity-drift",
                        ),
                    )
                    attention_owner = getattr(self, "summary_attention", None)
                    if attention_owner is not None:
                        attention_owner(
                            "review-continuation-recovery-identity-drift",
                            AttentionReason.ATTENTION_REQUIRED,
                        )
                    return
                _atomic_json(
                    receipt_path,
                    receipt.payload(
                        status="finalized",
                        outcome="advanced",
                        reason="durable-progress-observed",
                    ),
                )
                return

            self.callback_handled = False
            self.summary_attention_revision = -1
            completion_owner = getattr(
                self, "review_continuation_recovery_completed", None
            )
            try:
                already_completed = bool(
                    completion_owner(identity)
                    if completion_owner is not None
                    else False
                )
            except Exception:
                already_completed = False
            try:
                advanced = (
                    True
                    if already_completed
                    else bool(execution_owner(decision))
                )
            except Exception:
                # Preserve the prepared receipt.  A process crash or bounded
                # mechanism failure can converge from the durable transition.
                return
            if advanced:
                _atomic_json(
                    receipt_path,
                    receipt.payload(
                        status="finalized",
                        outcome="advanced",
                        reason="registered-workflow-advanced",
                    ),
                )
                return
            try:
                after = self.store.read(
                    self.spec["owner_id"], self.spec["operation_id"]
                )
                completed = bool(
                    completion_owner(identity)
                    if completion_owner is not None
                    else False
                )
            except Exception:
                return
            if completed:
                _atomic_json(
                    receipt_path,
                    receipt.payload(
                        status="finalized",
                        outcome="advanced",
                        reason="durable-progress-observed",
                    ),
                )
            elif after.state == "attention-required":
                _atomic_json(
                    receipt_path,
                    receipt.payload(
                        status="finalized",
                        outcome="refused",
                        reason="registered-workflow-refused",
                    ),
                )
            # A false workflow result while the root remains on its exact
            # callback boundary is a documented wait signal (for example an
            # exact-HEAD verification started this tick).  Keep the prepared
            # receipt so the next poll converges without another reservation.

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
            self.record_provider_result(generation, envelope.payload_sha256)
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
