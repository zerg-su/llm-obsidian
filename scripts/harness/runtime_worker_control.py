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
from task_escalation_records import EscalationRecordError, append_raise


class RuntimeWorkerControlMixin:

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
                _atomic_json(
                    self.spec_path.parent / "callback-error.json",
                    {"schema_version": 1, "status": "callback-target-invalid"},
                )
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
            self.record_provider_result(generation, digest)
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
            _atomic_json(
                self.spec_path.parent / "callback-error.json",
                {"schema_version": 1, "status": "callback-invalid"},
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
            _atomic_json(
                self.spec_path.parent / "callback-error.json",
                {"schema_version": 1, "status": status},
            )

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
        if notify_path.is_file() and (not notify_path.is_symlink()):
            if json.loads(notify_path.read_text(encoding="utf-8")) != marker:
                raise RuntimeWorkerError("engineering/fix phase notification changed")
            return
        message = f"""Typed engineering/fix phase {step_id} is ready in .task-pipeline-step-request.json. Complete only this phase. {prior_context}Write evidence to {request['output_pointer']} and write {request['result_pointer']} as exact JSON with fields {{"schema_version":1,"status":"complete","output_sha256":"<sha256-of-evidence>","head_sha":"<current-git-head>"}}. For the reproduce phase only, status may instead be "cannot-reproduce". Then publish the request-bound callback with pipeline-step-submit.py. Remain in this same session for the next typed request."""
        if len(message.encode()) > 4096:
            raise RuntimeWorkerError(
                "engineering/fix phase notification exceeds its bound"
            )
        self.cmux_adapter.send(self.spec["surface_id"], message)
        self.cmux_adapter.send_key(self.spec["surface_id"], "Enter")
        self.write_immutable_json(notify_path, marker)

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
        if notify_path.is_file() and (not notify_path.is_symlink()):
            if json.loads(notify_path.read_text(encoding="utf-8")) != marker:
                raise RuntimeWorkerError(
                    "engineering/fix finalization notification changed"
                )
            return False
        phase_count = "four" if iteration == 0 else "three retry"
        message = f"All {phase_count} typed engineering/fix phase receipts are accepted. Finish the task in this same session: commit the minimal fix, run the approved scoped verification, and write the canonical .task-summary.json. Do not repeat an accepted phase."
        self.cmux_adapter.send(self.spec["surface_id"], message)
        self.cmux_adapter.send_key(self.spec["surface_id"], "Enter")
        self.write_immutable_json(notify_path, marker)
        return True

    def notify_cannot_reproduce(self, receipt: FixStepReceipt) -> None:
        receipt_sha256 = receipt.receipt_sha256
        attention_path = self.spec["cwd"] / ".task-needs-attention.json"
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
            raised = append_raise(self.spec["cwd"], marker)
        except EscalationRecordError as exc:
            raise RuntimeWorkerError(f"pipeline decision packet is invalid: {exc}") from exc
        if raised.record_id != marker["id"] or raised.payload.get("status") != "pending":
            return
        notify_path = (
            self.spec_path.parent / "pipeline-fix" / "cannot-reproduce-notify.json"
        )
        delivery = {
            "schema_version": 1,
            "operation_id": self.spec["operation_id"],
            "receipt_sha256": receipt_sha256,
            "status": "sent",
        }
        if notify_path.is_file() and (not notify_path.is_symlink()):
            if json.loads(notify_path.read_text(encoding="utf-8")) != delivery:
                raise RuntimeWorkerError("pipeline decision delivery changed")
            return
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
        message = f"Typed task escalation callback received. Category: pipeline-decision. The approved engineering/fix pipeline cannot reproduce the defect. Inspect {attention_path} and resolve from the originating coordinator with: {command}. Allowed decisions: stop, retry-with-fixture."
        if len(message.encode()) > 4096:
            raise RuntimeWorkerError("pipeline decision notification exceeds its bound")
        self.cmux_adapter.send(self.spec["origin_surface"], message)
        self.cmux_adapter.send_key(self.spec["origin_surface"], "Enter")
        self.write_immutable_json(notify_path, delivery)
