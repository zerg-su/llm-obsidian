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
from .runtime_callback_io import _pipeline_submit_failure
from task_escalation_records import EscalationRecordError, append_raise
from .contracts import ContractFamily, contract_registry
from .artifact_repair import (
    ArtifactRepairError,
    ContractArtifactOwner,
    CorrectionBudgetExhausted,
    CorrectionNotificationUncertain,
    publish_pipeline_step_contract as publish_step_contract,
)
from .fresh_artifact_repair import (
    FreshArtifactRepair,
    FreshRepairError,
    FreshRepairInvalid,
    launch_fresh_repair_for_worker,
)
from .retained_notification import (
    RetainedNotificationError,
    RetainedNotificationPending,
    deliver_worker_notification,
)


_REVIEW_REJECTION_FIELDS = frozenset(
    {
        "schema_version", "status", "operation_id", "run_id", "axis",
        "input_sha256", "attempt", "error_code", "error", "expected", "actual",
    }
)

_MECHANISM_FAILURE_ATTENTION = frozenset(
    {"pipeline-custom-callback-invalid", "review-drive-failed"}
)


def _latest_review_rejection(
    paths: list[Path], *, operation_id: str, run_id: str, axis: str
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for path in paths:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeWorkerError("review rejection receipt is unreadable") from exc
        attempt = row.get("attempt") if isinstance(row, dict) else None
        digest = row.get("input_sha256") if isinstance(row, dict) else None
        if (
            not isinstance(row, dict)
            or set(row) != _REVIEW_REJECTION_FIELDS
            or row.get("schema_version") != 1
            or row.get("status") != "rejected"
            or row.get("operation_id") != operation_id
            or row.get("run_id") != run_id
            or row.get("axis") != axis
            or type(attempt) is not int
            or attempt < 1
            or not re.fullmatch("[0-9a-f]{64}", str(digest or ""))
            or path.name != f"{str(digest)[:12]}-a{attempt}.json"
        ):
            raise RuntimeWorkerError("review rejection receipt identity is invalid")
        rows.append(row)
    attempts = sorted(int(row["attempt"]) for row in rows)
    digests = {str(row["input_sha256"]) for row in rows}
    if attempts != list(range(1, len(rows) + 1)) or len(digests) != len(rows):
        raise RuntimeWorkerError("review rejection receipt order is invalid")
    return max(rows, key=lambda row: int(row["attempt"]))


class RuntimeWorkerControlMixin:

    def pipeline_step_callback_ready(
        self, *, operation_id: str, run_id: str
    ) -> bool:
        """Recognize only the exact registered pipeline-step successor."""

        try:
            _generation, current_operation, current_run, callback_path = (
                _callback_target(self.spec)
            )
            if (
                current_operation != operation_id
                or current_run != run_id
                or callback_path.is_symlink()
                or not callback_path.is_file()
                or callback_path.stat().st_size > MAX_OUTBOX_BYTES
            ):
                return False
            envelope = _envelope(json.loads(callback_path.read_text(encoding="utf-8")))
        except (
            HarnessContractError,
            RuntimeWorkerError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return False
        return (
            envelope.operation_id == operation_id
            and envelope.run_id == run_id
            and envelope.kind == "result"
        )

    def relay_mechanism_attention(self, status: str) -> None:
        """Wake the coordinator for the two repo-owned transition seams."""

        if status not in _MECHANISM_FAILURE_ATTENTION:
            return
        trusted_vault = getattr(self, "trusted_vault", None)
        if not isinstance(trusted_vault, Path):
            return
        runner = getattr(self, "task_escalation_runner", subprocess.run)
        try:
            runner(
                [
                    sys.executable,
                    str(trusted_vault / "scripts" / "task_escalation.py"),
                    "raise",
                    "--worktree",
                    str(self.spec["cwd"]),
                    "--category",
                    "mechanism-failure",
                    "--reason",
                    f"Harness transition entered attention-required at {status}",
                    "--question",
                    "Classify and repair or authorize the exact model-free continuation",
                ],
                cwd=self.spec["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError:
            return

    def handle_pipeline_step_submit_failure(
        self,
        submitted: subprocess.CompletedProcess[str],
        callback_path: Path,
        *,
        receipt_path: Path,
        operation_id: str,
        invalid_sha256: str,
        stage: str,
    ) -> bool:
        """Route typed submit rejections without spending the wrong budget."""

        failure_class, error_code = _pipeline_submit_failure(
            submitted, callback_path
        )
        if failure_class == "none":
            return False
        _atomic_json(
            receipt_path,
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "returncode": submitted.returncode,
                "failure_class": failure_class,
                "error_code": error_code,
                "status": "attention-required",
            },
        )
        if failure_class == "model-semantic":
            self.request_pipeline_step_correction(
                invalid_sha256,
                stage=stage,
            )
        else:
            self.summary_attention(
                f"{stage}-{failure_class}",
                AttentionReason.CONTRACT_DRIFT
                if failure_class == "code-authority"
                else AttentionReason.ATTENTION_REQUIRED,
            )
        return True

    def publish_pipeline_step_contract(
        self, request: dict[str, object]
    ) -> tuple[dict[str, object], ContractArtifactOwner]:
        try:
            enriched, owner = publish_step_contract(
                state_root=self.spec_path.parent,
                worktree=self.spec["cwd"],
                request=request,
            )
        except ArtifactRepairError as exc:
            raise RuntimeWorkerError(str(exc)) from exc
        self.pipeline_step_artifact_owner = owner
        return enriched, owner

    def request_pipeline_step_correction(
        self, invalid_sha256: str, *, stage: str
    ) -> None:
        owner = getattr(self, "pipeline_step_artifact_owner", None)
        if not isinstance(owner, ContractArtifactOwner):
            self.summary_attention(f"{stage}-template-unavailable")
            return
        if owner.awaiting_semantic_edit(invalid_sha256):
            return
        try:
            reservation = owner.reserve_correction(invalid_sha256)
            owner.restore_template()
            message = (
                "The pipeline-step result was rejected before callback "
                "acceptance. Harness restored the exact identity-bound result "
                "template. Edit that existing object in place; fill only status "
                "and, for a custom step, outcome. Write the requested evidence "
                "file first. The submit chokepoint derives output_sha256 and "
                "head_sha from durable authority. This is the only same-session "
                "correction; do not repeat an accepted step or start a new session."
            )

            def send(wake: str) -> None:
                self.cmux_adapter.send(self.spec["surface_id"], wake)
                self.cmux_adapter.send_key(self.spec["surface_id"], "Enter")

            owner.deliver_correction(
                reservation,
                message,
                send,
                fault_observer=getattr(self, "fault_observer", None),
            )
            self.fix_result_digest = ""
            self.fix_result_stable_reads = 0
            self.fix_output_digest = ""
            self.fix_output_stable_reads = 0
            self.fix_submit_attempt_digest = ""
            self.custom_result_digest = ""
            self.custom_result_stable_reads = 0
            self.custom_output_digest = ""
            self.custom_output_stable_reads = 0
            self.custom_submit_attempt_digest = ""
        except CorrectionBudgetExhausted:
            try:
                launch_fresh_repair_for_worker(self, owner, invalid_sha256)
                self.fix_result_digest = ""
                self.custom_result_digest = ""
            except FreshRepairError:
                self.summary_attention(
                    f"{stage}-correction-exhausted",
                    AttentionReason.RETRY_EXHAUSTED,
                )
        except CorrectionNotificationUncertain:
            self.summary_attention(
                f"{stage}-correction-notification-uncertain",
                AttentionReason.ATTENTION_REQUIRED,
            )
        except ArtifactRepairError:
            self.summary_attention(f"{stage}-template-invalid")

    def adopt_fresh_pipeline_step_result(self) -> bool:
        owner = getattr(self, "pipeline_step_artifact_owner", None)
        if not isinstance(owner, ContractArtifactOwner):
            return False
        try:
            fresh = FreshArtifactRepair.load(owner=owner)
            record = self.store.read(
                self.spec["owner_id"],
                str(fresh.reservation["operation_id"]),
            )

            def validate(value: Mapping[str, object]) -> object:
                if value.get("status") not in {"complete", "cannot-reproduce"}:
                    raise ValueError("pipeline result status is invalid")
                outcome = value.get("outcome")
                if outcome is not None and (
                    not isinstance(outcome, str) or not outcome
                ):
                    raise ValueError("pipeline result outcome is invalid")
                return value

            reconciliation = fresh.reconcile(record, validate)
            if reconciliation.status == "adopted":
                self.fix_result_digest = ""
                self.fix_result_stable_reads = 0
                self.custom_result_digest = ""
                self.custom_result_stable_reads = 0
            return reconciliation.status != "accepted"
        except FreshRepairInvalid:
            self.summary_attention(
                "pipeline-step-fresh-repair-invalid",
                AttentionReason.RETRY_EXHAUSTED,
            )
            return False
        except (FreshRepairError, StoreError, OSError, ValueError):
            return False

    def mark_failed_pipeline_step_correction_runtime(self) -> None:
        owner = getattr(self, "pipeline_step_artifact_owner", None)
        if (
            self.provider_exited
            and not self.callback_handled
            and isinstance(owner, ContractArtifactOwner)
            and owner.has_sent_correction
        ):
            self.summary_attention(
                "pipeline-step-correction-session-dead",
                AttentionReason.ATTENTION_REQUIRED,
            )

    def publish_error_latch(self, status: str) -> None:
        """Publish one owned callback/error latch, then expose its durable boundary."""

        observer = getattr(self, "fault_observer", None)
        if observer is not None:
            observer("error-latch-published:before")
        _atomic_json(
            self.spec_path.parent / "callback-error.json",
            {"schema_version": 1, "status": status},
        )
        if observer is not None:
            observer("error-latch-published")

    def inspect_control(self) -> None:
        control_path = self.spec_path.parent / "process-control.json"
        try:
            raw = control_path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            raw = b""
        digest = hashlib.sha256(raw).hexdigest()
        if digest == self.invalid_control_digest:
            return
        try:
            if not raw or len(raw) > MAX_OUTBOX_BYTES:
                raise RuntimeWorkerError("process guardian command is invalid")
            command = json.loads(raw)
            if not isinstance(command, dict):
                raise RuntimeWorkerError("process guardian command must be an object")
            command_id = str(command.get("command_id") or "")
            unsigned = dict(command)
            unsigned.pop("command_id", None)
            encoded = json.dumps(
                unsigned, sort_keys=True, separators=(",", ":")
            ).encode()
            expected_id = hashlib.sha256(encoded).hexdigest()
            action = command.get("action")
            if (
                set(command)
                != {
                    "schema_version",
                    "action",
                    "operation_id",
                    "run_id",
                    "process_group",
                    "process_identity",
                    "supervisor_pid",
                    "supervisor_identity",
                    "command_id",
                }
                or command.get("schema_version") != 1
                or action not in {"request-exit", "terminate"}
                or (command.get("operation_id") != self.spec["operation_id"])
                or (command.get("run_id") != self.spec["run_id"])
                or (command.get("process_group") != self.handle.process_group)
                or (command.get("process_identity") != self.handle.process_identity)
                or (command.get("supervisor_pid") != os.getpid())
                or (command.get("supervisor_identity") != self.supervisor_identity)
                or (command_id != expected_id)
            ):
                raise RuntimeWorkerError("process guardian command identity mismatches")
            if command_id == self.handled_control_id:
                return
            self.process.signal_owned_child_group(
                self.handle.process_group,
                self.handle.process_identity,
                signal.SIGTERM if action == "request-exit" else signal.SIGKILL,
            )
            self.handled_control_id = command_id
            _atomic_json(
                self.spec_path.parent / "process-control-receipt.json",
                {
                    "schema_version": 1,
                    "command_id": command_id,
                    "action": action,
                    "status": "accepted",
                },
            )
        except (
            json.JSONDecodeError,
            OSError,
            ProcessError,
            RuntimeWorkerError,
            TypeError,
            ValueError,
        ):
            self.invalid_control_digest = digest
            try:
                self.store.transition(
                    self.spec["owner_id"],
                    self.spec["operation_id"],
                    "attention-required",
                    reason=AttentionReason.ATTENTION_REQUIRED,
                )
            except Exception:
                pass
            _atomic_json(
                self.spec_path.parent / "process-control-error.json",
                {"schema_version": 1, "status": "invalid"},
            )

    def inspect_prompt(self) -> None:
        try:
            record = self.store.read(self.spec["owner_id"], self.spec["operation_id"])
        except Exception:
            return
        if record.resources.surface_id != self.spec["surface_id"]:
            return
        reader = getattr(self.cmux_adapter, "read", None)
        if reader is None:
            return
        try:
            screen = str(reader(self.spec["surface_id"]))
        except Exception:
            return
        encoded = screen.encode("utf-8", errors="replace")
        if not encoded or len(encoded) > MAX_SCREEN_BYTES:
            return
        digest = hashlib.sha256(encoded).hexdigest()
        decision = classify(
            self.spec["runtime"], screen, closure_armed=record.state == "exiting"
        )
        self.latest_screen_digest = digest
        self.latest_prompt_state = (
            "interactive" if decision.interactive else "non-interactive"
        )
        if digest == self.last_prompt_digest:
            return
        if not decision.interactive:
            return
        self.last_prompt_digest = digest
        automate_prompt(
            self.store,
            self.spec["owner_id"],
            self.spec["operation_id"],
            self.spec["runtime"],
            self.spec["surface_id"],
            screen,
            self.cmux_adapter,
            closure_armed=record.state == "exiting",
        )

    def inspect_callback(self) -> None:
        try:
            target = _callback_target(self.spec)
        except RuntimeWorkerError:
            if not self.registration_invalid:
                self.registration_invalid = True
                try:
                    self.store.transition(
                        self.spec["owner_id"],
                        self.spec["operation_id"],
                        "attention-required",
                        reason=AttentionReason.CALLBACK_INVALID,
                    )
                except Exception:
                    pass
                self.publish_error_latch("callback-target-invalid")
            return
        self.registration_invalid = False
        if target != self.active_target:
            if self.active_target is not None and target[0] <= self.active_target[0]:
                return
            self.active_target = target
            self.last_digest = ""
            self.stable_reads = 0
            self.callback_handled = False
        if self.callback_handled:
            return
        generation, operation_id, run_id, callback_path = target
        try:
            raw = callback_path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            raw = b""
        if not raw or len(raw) > MAX_OUTBOX_BYTES:
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
            envelope = _envelope(json.loads(raw))
            if envelope.operation_id != operation_id or envelope.run_id != run_id:
                raise RuntimeWorkerError("callback identity mismatches runtime launch")
            acceptance = CallbackBroker(self.store, self.spec["owner_id"]).accept(
                envelope, deadline_operation_id=self.spec["operation_id"]
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
            if not publish_callback_wake(
                self.spec,
                self.spec_path.parent,
                envelope.callback_id,
                self.cmux_adapter,
                resume_uncertain=wake_resume_once(self, envelope.callback_id),
            ):
                self.summary_attention("callback-wake-effect-uncertain")
                return
        except CallbackTimeoutError:
            self.callback_handled = True
            _atomic_json(
                self.spec_path.parent / "callback-timeout.json",
                {
                    "schema_version": 1,
                    "operation_id": self.spec["operation_id"],
                    "run_id": self.spec["run_id"],
                    "status": "attention-required",
                },
            )
        except (
            CallbackError,
            RuntimeWorkerError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            try:
                self.store.transition(
                    self.spec["owner_id"],
                    self.spec["operation_id"],
                    "attention-required",
                    reason=AttentionReason.CALLBACK_INVALID,
                )
            except Exception:
                pass
            self.publish_error_latch("callback-invalid")

    def inspect_submit_rejections(self) -> None:
        """Drive one bounded correction for a durably rejected review input.

        Callback/schema repair is a separate mechanism loop from semantic
        finalization cycles: while the exact reviewer session identity is
        live, each new rejection receipt consumes one existing reviewer
        attempt through the supervisor boundary and sends one idempotent
        correction prompt telling the same session to edit only the existing
        ``.review-input.json`` and rerun the exact submit command — never the
        original review prompt, never a new provider cycle.  Exhaustion of
        the attempt budget, a dead or mismatched session, a changed HEAD, or
        unreadable metadata stays fail-closed attention-required.
        """

        operation = getattr(self, "operation", None)
        if (
            operation is None
            or operation.spec.route.profile != "reviewer-callback"
        ):
            return
        try:
            target = _callback_target(self.spec)
        except RuntimeWorkerError:
            return
        _generation, operation_id, run_id, callback_path = target
        rejections = callback_path.parent / ".review-submit-rejections"
        if rejections.is_symlink() or not rejections.is_dir():
            return
        receipts = sorted(
            path
            for path in rejections.glob("*.json")
            if not path.name.startswith(".") and not path.is_symlink()
        )
        if not receipts:
            return
        record = self.store.read(
            self.spec["owner_id"], self.spec["operation_id"]
        )
        if len(receipts) > contract_registry()[ContractFamily.REVIEW_INPUT].same_session_corrections:
            self.summary_attention(
                "review-submit-rejections-exhausted",
                AttentionReason.RETRY_EXHAUSTED,
            )
            return
        handle = getattr(self, "handle", None)
        process = getattr(self, "process", None)
        if (
            handle is None
            or process is None
            or process.process_status(
                handle.process_group, handle.process_identity
            )
            != "alive"
        ):
            self.summary_attention(
                "review-submit-correction-session-dead",
                AttentionReason.ATTENTION_REQUIRED,
            )
            return
        meta_path = callback_path.parent / ".review-meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = None
        if (
            not isinstance(meta, dict)
            or meta.get("operation_id") != operation_id
            or meta.get("run_id") != run_id
            or not isinstance(meta.get("worktree"), str)
        ):
            self.summary_attention(
                "review-submit-correction-identity",
                AttentionReason.ATTENTION_REQUIRED,
            )
            return
        try:
            latest = _latest_review_rejection(
                receipts,
                operation_id=operation_id,
                run_id=run_id,
                axis=str(meta.get("axis") or ""),
            )
        except RuntimeWorkerError:
            self.summary_attention(
                "review-submit-correction-identity",
                AttentionReason.ATTENTION_REQUIRED,
            )
            return
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=meta["worktree"],
            text=True,
            capture_output=True,
            check=False,
        )
        if (
            head_result.returncode
            or head_result.stdout.strip() != meta.get("head_sha")
        ):
            self.summary_attention(
                "review-submit-correction-head-drift",
                AttentionReason.CONTRACT_DRIFT,
            )
            return
        input_path = callback_path.parent / ".review-input.json"
        submit = shlex.join(
            (
                str(Path(sys.executable).resolve()),
                str(self.trusted_vault / "scripts/harness/review_submit.py"),
                "--worktree",
                str(meta["worktree"]),
                "--state-dir",
                str(callback_path.parent),
                "--input-file",
                str(input_path),
            )
        )
        message = (
            "Correction: your review submission was rejected "
            f"({latest.get('error_code')}): {str(latest.get('error') or '')[:200]} "
            f"Expected {latest.get('expected')!r}, actual {latest.get('actual')!r}. "
            f"Edit only the existing `{input_path}` so it satisfies the stated "
            "contract - change nothing else and do not restart the review - "
            f"then rerun exactly: {submit}"
        )
        pointer = meta.get("contract_template_pointer")
        if not isinstance(pointer, str) or not pointer:
            self.summary_attention("review-submit-correction-template-invalid")
            return
        sidecar = Path(pointer).expanduser()
        try:
            owner = ContractArtifactOwner.load(
                state_root=sidecar.parents[2],
                worktree=input_path.parent,
                family=ContractFamily.REVIEW_INPUT,
                attempt_id=operation_id,
            )
            invalid_sha256 = str(latest.get("input_sha256") or "")
            reservation = owner.reserve_correction(invalid_sha256)
            observer = getattr(self, "fault_observer", None)
            if reservation.created and observer is not None:
                observer("correction-reserved")
            current = self.store.read(
                self.spec["owner_id"], self.spec["operation_id"]
            )
            if current.attempt == reservation.attempt - 1:
                OperationSupervisor(
                    self.store,
                    self.spec["owner_id"],
                    self.spec["operation_id"],
                ).consume_attempt()
                if observer is not None:
                    observer("correction-attempt-consumed")
            elif current.attempt != reservation.attempt:
                raise ArtifactRepairError("review correction attempt authority drifted")
            notification_state = owner.correction_notification_state(reservation)
            if notification_state == "unreserved":
                owner.restore_template()
                if observer is not None:
                    observer("correction-template-restored")

            def send(wake: str) -> None:
                self.cmux_adapter.send(self.spec["surface_id"], wake)
                self.cmux_adapter.send_key(self.spec["surface_id"], "Enter")

            owner.deliver_correction(
                reservation,
                message,
                send,
                fault_observer=observer,
            )
        except CorrectionBudgetExhausted:
            self.summary_attention(
                "review-submit-rejections-exhausted",
                AttentionReason.RETRY_EXHAUSTED,
            )
        except CorrectionNotificationUncertain:
            self.summary_attention(
                "review-submit-correction-notification-uncertain",
                AttentionReason.ATTENTION_REQUIRED,
            )
        except (ArtifactRepairError, IndexError, SupervisorError):
            self.summary_attention(
                "review-submit-correction-template-invalid",
                AttentionReason.ATTENTION_REQUIRED,
            )

    def summary_attention(
        self,
        status: str,
        reason: AttentionReason = AttentionReason.CALLBACK_INVALID,
        *,
        write_error: bool = True,
    ) -> None:
        self.callback_handled = True
        try:
            self.store.transition(
                self.spec["owner_id"],
                self.spec["operation_id"],
                "attention-required",
                reason=reason,
            )
        except Exception:
            pass
        try:
            current = self.store.read(self.spec["owner_id"], self.spec["operation_id"])
            if current.state == "attention-required":
                self.summary_attention_revision = current.revision
        except Exception:
            pass
        if self.is_custom_pipeline and self.pipeline is not None:
            marker = (
                self.spec_path.parent / "pipeline-custom" / "attention-telemetry.json"
            )
            if not marker.exists():
                emit_compiled_pipeline_event(
                    self.spec["cwd"],
                    event="attention",
                    pipeline_id=self.pipeline.definition.pipeline_id,
                    pipeline_version=self.pipeline.definition.version,
                    profile=self.pipeline.definition.profile,
                    compiler_outcome="custom-resolved",
                    definition_sha=self.pipeline.definition_sha256,
                    primitive_count=len(self.pipeline.definition.steps),
                    attention_category="custom-attention",
                    status="degraded",
                )
                _atomic_json(
                    marker,
                    {
                        "schema_version": 1,
                        "operation_id": self.spec["operation_id"],
                        "status": "emitted",
                    },
                )
        if write_error:
            self.publish_error_latch(status)
        self.relay_mechanism_attention(status)

    def write_immutable_json(self, path: Path, value: dict[str, object]) -> None:
        encoded = (
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=448)
        if path.is_symlink():
            raise RuntimeWorkerError("immutable runtime receipt cannot be a symlink")
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 384)
        except FileExistsError:
            try:
                current = path.read_bytes()
            except OSError as exc:
                raise RuntimeWorkerError(
                    "immutable runtime receipt is unreadable"
                ) from exc
            if current != encoded:
                raise RuntimeWorkerError("immutable runtime receipt changed")
            return
        try:
            with os.fdopen(descriptor, "wb") as handle_file:
                handle_file.write(encoded)
                handle_file.flush()
                os.fsync(handle_file.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def git_head(self) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.spec["cwd"],
            text=True,
            capture_output=True,
            check=False,
        )
        head = result.stdout.strip()
        if result.returncode or not re.fullmatch("[0-9a-f]{40,64}", head):
            raise RuntimeWorkerError("engineering/fix HEAD is unavailable")
        return head

    def retarget_fix_callback(
        self, *, operation_id: str, run_id: str, callback_pointer: str
    ) -> None:
        generation, current_operation, current_run, current_pointer = _callback_target(
            self.spec
        )
        expected_pointer = (self.spec["cwd"] / callback_pointer).resolve()
        if (
            current_operation == operation_id
            and current_run == run_id
            and (current_pointer == expected_pointer)
        ):
            return
        if current_operation != self.spec["operation_id"]:
            current_child = self.store.read(self.spec["owner_id"], current_operation)
            if current_child.state != "complete":
                raise RuntimeWorkerError(
                    "engineering/fix callback target changed before acceptance"
                )
        if expected_pointer.exists() or expected_pointer.is_symlink():
            if expected_pointer.is_symlink() or not expected_pointer.is_file():
                raise RuntimeWorkerError(
                    "engineering/fix callback outbox is not reusable"
                )
            expected_pointer.unlink()
        _atomic_json(
            self.spec["callback_registration"],
            {
                "schema_version": 1,
                "generation": generation + 1,
                "operation_id": operation_id,
                "run_id": run_id,
                "callback_pointer": callback_pointer,
            },
        )

    def notify_fix_phase(self, request: dict[str, object]) -> None:
        operation_id = str(request["operation_id"])
        step_id = str(request["step_id"])
        iteration = int(request["iteration"])
        prior_pointer = {
            "root-cause": ".task-pipeline/outputs/pass-0/reproduce.md",
            "regression-test": f".task-pipeline/outputs/pass-{iteration}/root-cause.md",
            "minimal-fix": f".task-pipeline/outputs/pass-{iteration}/regression-test.md",
        }.get(step_id, "")
        prior_context = (
            f"Read prior accepted evidence at {prior_pointer}. input_sha256 and prior_receipt_sha256 are opaque request bindings, not artifact content hashes. "
            if prior_pointer
            else ""
        )
        notify_path = (
            self.spec_path.parent
            / "pipeline-fix"
            / "notifications"
            / f"{operation_id}.json"
        )
        marker = {
            "schema_version": 1,
            "operation_id": operation_id,
            "step_id": step_id,
            "status": "sent",
        }
        message = f"""Typed engineering/fix phase {step_id} is ready in .task-pipeline-step-request.json. Complete only this phase. {prior_context}Write evidence to {request['output_pointer']} and write {request['result_pointer']} as exact JSON with fields {{"schema_version":1,"status":"complete","output_sha256":"<sha256-of-evidence>","head_sha":"<current-git-head>"}}. For the reproduce phase only, status may instead be "cannot-reproduce". Then publish the request-bound callback with pipeline-step-submit.py. Remain in this same session for the next typed request."""
        if len(message.encode()) > 4096:
            raise RuntimeWorkerError(
                "engineering/fix phase notification exceeds its bound"
            )
        try:
            deliver_worker_notification(
                self,
                notify_path=notify_path,
                marker=marker,
                message=message,
                successor_ready=lambda: self.pipeline_step_callback_ready(
                    operation_id=operation_id,
                    run_id=str(request["run_id"]),
                ),
            )
        except RetainedNotificationPending:
            return
        except RetainedNotificationError as exc:
            raise RuntimeWorkerError(
                "engineering/fix phase notification delivery failed"
            ) from exc

    def notify_fix_finalization(self, iteration: int) -> bool:
        notify_path = (
            self.spec_path.parent
            / "pipeline-fix"
            / (
                "finalization-notify.json"
                if iteration == 0
                else f"pass-{iteration}/finalization-notify.json"
            )
        )
        marker = {
            "schema_version": 1,
            "operation_id": self.spec["operation_id"],
            "iteration": iteration,
            "status": "sent",
        }
        existed = notify_path.is_file() and (not notify_path.is_symlink())
        phase_count = "four" if iteration == 0 else "three retry"
        message = f"All {phase_count} typed engineering/fix phase receipts are accepted. Finish the task in this same session: commit the minimal fix, run the approved scoped verification, and write the canonical .task-summary.json. Do not repeat an accepted phase."
        try:
            deliver_worker_notification(
                self,
                notify_path=notify_path,
                marker=marker,
                message=message,
            )
        except RetainedNotificationPending:
            return False
        except RetainedNotificationError as exc:
            raise RuntimeWorkerError(
                "engineering/fix finalization notification delivery failed"
            ) from exc
        return not existed

    def publish_pipeline_decision(
        self,
        *,
        marker: dict[str, object],
        notify_path: Path,
        delivery: dict[str, object],
        body: str,
        allowed_decisions: tuple[str, ...],
    ) -> None:
        """Append one pending decision and deliver it exactly once."""

        raised = append_raise(self.spec["cwd"], marker)
        if raised.record_id != marker["id"] or raised.payload.get("status") != "pending":
            return
        if notify_path.is_file() and (not notify_path.is_symlink()):
            if json.loads(notify_path.read_text(encoding="utf-8")) != delivery:
                raise RuntimeWorkerError("pipeline decision delivery changed")
            return
        attention_path = self.spec["cwd"] / ".task-needs-attention.json"
        command = (
            "python3 "
            + shlex.quote(
                str(
                    self.spec["store_root"].parent.parent
                    / "scripts"
                    / "task_escalation.py"
                )
            )
            + " resolve --worktree "
            + shlex.quote(str(self.spec["cwd"]))
            + " --decision <decision>"
        )
        message = f"Typed task escalation callback received. Category: pipeline-decision. {body} Inspect {attention_path} and resolve from the originating coordinator with: {command}. Allowed decisions: {', '.join(allowed_decisions)}."
        if len(message.encode()) > 4096:
            raise RuntimeWorkerError("pipeline decision notification exceeds its bound")
        self.cmux_adapter.send(self.spec["origin_surface"], message)
        self.cmux_adapter.send_key(self.spec["origin_surface"], "Enter")
        self.write_immutable_json(notify_path, delivery)

    def notify_cannot_reproduce(self, receipt: FixStepReceipt) -> None:
        receipt_sha256 = receipt.receipt_sha256
        marker = {
            "version": 1,
            "id": f"pipeline-decision-{receipt_sha256[:24]}",
            "status": "pending",
            "task_name": "engineering/fix cannot reproduce",
            "category": "pipeline-decision",
            "reason": "The approved fix pipeline cannot reproduce the reported defect",
            "question": "Choose stop or retry-with-fixture",
            "worktree": str(self.spec["cwd"]),
            "task_surface": self.spec["surface_id"],
            "raised_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "receipt_operation_id": receipt.operation_id,
            "receipt_sha256": receipt_sha256,
            "allowed_decisions": ["stop", "retry-with-fixture"],
        }
        try:
            self.publish_pipeline_decision(
                marker=marker,
                notify_path=(
                    self.spec_path.parent
                    / "pipeline-fix"
                    / "cannot-reproduce-notify.json"
                ),
                delivery={
                    "schema_version": 1,
                    "operation_id": self.spec["operation_id"],
                    "receipt_sha256": receipt_sha256,
                    "status": "sent",
                },
                body="The approved engineering/fix pipeline cannot reproduce the defect.",
                allowed_decisions=("stop", "retry-with-fixture"),
            )
        except EscalationRecordError as exc:
            raise RuntimeWorkerError(f"pipeline decision packet is invalid: {exc}") from exc
