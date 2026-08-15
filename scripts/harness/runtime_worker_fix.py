"""Engineering/fix transport reconstruction and callback acceptance."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .runtime_worker import *  # noqa: F401,F403
from .runtime_worker import (
    _atomic_json,
    _bounded_file_sha256,
    _callback_target,
    _envelope,
)
from task_escalation_records import EscalationRecordError, append_raise
from .artifact_repair import ContractArtifactOwner
from .workflows.engineering_fix import fix_phase_request


@dataclass
class FixTransportState:
    completion_policy: str
    total_pass_limit: int
    approved_plan_sha256: str
    initial_head_sha: str
    parent: object
    initial_receipts: list[FixStepReceipt]
    iteration: int
    receipt_root: Path
    receipts: list[FixStepReceipt]
    progress: object
    retry_intent: dict[str, object] | None


class RuntimeWorkerFixMixin:
    @staticmethod
    def _phase_timing_identity(round_: object) -> dict[str, object]:
        return {
            "schema_version": 1,
            "owner_id": round_.spec.owner_id,
            "parent_operation_id": round_.parent_operation_id,
            "operation_id": round_.spec.operation_id,
            "run_id": round_.run_id,
            "step_id": round_.step_id,
            "iteration": round_.iteration,
        }

    def _phase_timing_start(
        self, state: FixTransportState, round_: object
    ) -> tuple[Path, float] | None:
        root = (
            state.receipt_root.parent / "timing" / state.receipt_root.name
            / round_.step_id
        )
        path = root / "start.json"
        identity = self._phase_timing_identity(round_)
        if path.exists() or path.is_symlink():
            if path.is_symlink():
                return None
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    return None
                raw_started_at = value.get("started_at")
                if isinstance(raw_started_at, bool):
                    return None
                started_at = float(raw_started_at)
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                return None
            if (
                set(value) != {*identity, "started_at"}
                or any(value.get(key) != expected for key, expected in identity.items())
                or not math.isfinite(started_at)
                or started_at < 0
            ):
                return None
            return path, started_at
        started_at = time.time()
        try:
            self.write_immutable_json(path, {**identity, "started_at": started_at})
        except (OSError, RuntimeWorkerError):
            return None
        return path, started_at

    def _write_phase_timing_completion(
        self, state: FixTransportState, round_: object, receipt: FixStepReceipt
    ) -> None:
        root = (
            state.receipt_root.parent / "timing" / state.receipt_root.name
            / round_.step_id
        )
        start_path = root / "start.json"
        # Completion is display-only evidence: never synthesize its missing
        # start marker during callback acceptance.
        if not start_path.is_file() or start_path.is_symlink():
            return
        start = self._phase_timing_start(state, round_)
        if start is None:
            return
        _start_path, _started_at = start
        path = start_path.with_name("completion.json")
        identity = self._phase_timing_identity(round_)
        value = {
            **identity,
            "completed_at": time.time(),
            "receipt_sha256": receipt.receipt_sha256,
        }
        if path.exists() or path.is_symlink():
            if path.is_symlink():
                return
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(current, dict):
                    return
                raw_completed_at = current.get("completed_at")
                if isinstance(raw_completed_at, bool):
                    return
                completed_at = float(raw_completed_at)
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                return
            if (
                set(current) != {*identity, "completed_at", "receipt_sha256"}
                or any(current.get(key) != expected for key, expected in identity.items())
                or current.get("receipt_sha256") != receipt.receipt_sha256
                or not math.isfinite(completed_at)
                or completed_at < 0
            ):
                return
            return
        try:
            self.write_immutable_json(path, value)
        except (OSError, RuntimeWorkerError):
            return

    def load_fix_policy(self) -> tuple[dict[str, object], str, int, str]:
        meta = json.loads(
            (self.spec["cwd"] / ".task-meta.json").read_text(encoding="utf-8")
        )
        policy = meta.get("pipeline_policy") if isinstance(meta, dict) else None
        if (
            not isinstance(policy, dict)
            or policy.get("name") != "engineering/fix"
            or self.pipeline is None
            or policy.get("definition_sha256") != self.pipeline.definition_sha256
        ):
            raise RuntimeWorkerError(
                "engineering/fix metadata mismatches its compiled contract"
            )
        completion_policy = str(policy.get("completion_policy") or "")
        total_pass_limit = policy.get("total_pass_limit")
        limits = {"attention": 2, "autonomous": 3}
        if (
            completion_policy not in limits
            or type(total_pass_limit) is not int
            or total_pass_limit != limits[completion_policy]
        ):
            raise RuntimeWorkerError("engineering/fix completion policy is invalid")
        return (
            policy,
            completion_policy,
            total_pass_limit,
            str(meta.get("approved_plan_sha256") or ""),
        )

    def load_fix_controller(self, approved_plan_sha256: str) -> dict[str, object]:
        controller_path = self.spec_path.parent / "pipeline-fix" / "controller.json"
        if controller_path.is_symlink():
            raise RuntimeWorkerError("engineering/fix controller must not be a symlink")
        expected_fields = {
            "schema_version",
            "operation_id",
            "definition_sha256",
            "approved_plan_sha256",
            "initial_head_sha",
            "iteration",
        }
        if controller_path.is_file():
            controller = json.loads(controller_path.read_text(encoding="utf-8"))
            if (
                not isinstance(controller, dict)
                or set(controller) != expected_fields
                or controller.get("schema_version") != 1
                or controller.get("operation_id") != self.spec["operation_id"]
                or controller.get("definition_sha256")
                != self.pipeline.definition_sha256
                or controller.get("approved_plan_sha256") != approved_plan_sha256
                or controller.get("iteration") != 0
            ):
                raise RuntimeWorkerError("engineering/fix controller receipt changed")
            return controller
        try:
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
        except (OSError, json.JSONDecodeError):
            initial_head_sha = self.git_head()
        if not re.fullmatch("[0-9a-f]{40,64}", initial_head_sha):
            raise RuntimeWorkerError("pipeline initial HEAD is unavailable")
        controller = {
            "schema_version": 1,
            "operation_id": self.spec["operation_id"],
            "definition_sha256": self.pipeline.definition_sha256,
            "approved_plan_sha256": approved_plan_sha256,
            "initial_head_sha": initial_head_sha,
            "iteration": 0,
        }
        self.write_immutable_json(controller_path, controller)
        return controller

    @staticmethod
    def load_fix_receipts(
        root: Path, step_ids: tuple[str, ...]
    ) -> list[FixStepReceipt]:
        receipts: list[FixStepReceipt] = []
        for step_id in step_ids:
            receipt_path = root / step_id / "receipt.json"
            if not receipt_path.is_file():
                break
            receipts.append(load_receipt(receipt_path))
        return receipts

    def load_retry_intent(
        self,
        *,
        completion_policy: str,
        total_pass_limit: int,
        initial_receipts: list[FixStepReceipt],
        initial_progress: object,
    ) -> tuple[dict[str, object] | None, int]:
        paths = sorted(
            (self.spec_path.parent / "pipeline-fix").glob("pass-*/retry-intent.json")
        )
        if paths and initial_progress.action != "complete":
            raise RuntimeWorkerError(
                "fix retry started before the initial pass completed"
            )
        observed: list[int] = []
        for path in paths:
            match = re.fullmatch("pass-([1-9][0-9]*)", path.parent.name)
            if match is None:
                raise RuntimeWorkerError("fix retry intent path is invalid")
            observed.append(int(match.group(1)))
        if observed != list(range(1, len(paths) + 1)) or len(paths) >= total_pass_limit:
            raise RuntimeWorkerError("fix retry intents are not a bounded prefix")
        if not paths:
            return None, 0
        path = paths[-1]
        if path.is_symlink():
            raise RuntimeWorkerError("fix retry intent cannot be a symlink")
        intent = json.loads(path.read_text(encoding="utf-8"))
        iteration = observed[-1]
        expected_fields = {
            "schema_version",
            "operation_id",
            "definition_sha256",
            "iteration",
            "completion_policy",
            "total_pass_limit",
            "reproduction_receipt_sha256",
            "verification_operation_id",
            "verification_sha256",
            "failed_head_sha",
            "current_head_sha",
            "status",
        }
        if (
            not isinstance(intent, dict)
            or set(intent) != expected_fields
            or intent.get("schema_version") != 1
            or intent.get("operation_id") != self.spec["operation_id"]
            or intent.get("definition_sha256") != self.pipeline.definition_sha256
            or intent.get("iteration") != iteration
            or intent.get("completion_policy") != completion_policy
            or intent.get("total_pass_limit") != total_pass_limit
            or intent.get("status") != "pending"
            or not initial_receipts
            or intent.get("reproduction_receipt_sha256")
            != initial_receipts[0].receipt_sha256
        ):
            raise RuntimeWorkerError("fix retry intent identity changed")
        self.validate_retry_verification(intent)
        return intent, iteration

    def validate_retry_verification(self, intent: dict[str, object]) -> None:
        path = (
            self.spec_path.parent
            / "pipeline-verification"
            / str(intent["verification_operation_id"])
            / "receipt.json"
        )
        if path.is_symlink() or not path.is_file():
            raise RuntimeWorkerError("fix retry verification receipt is unavailable")
        value = json.loads(path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if (
            not isinstance(value, dict)
            or value.get("status") != "failed"
            or value.get("parent_operation_id") != self.spec["operation_id"]
            or value.get("head_sha") != intent["failed_head_sha"]
            or digest != intent["verification_sha256"]
        ):
            raise RuntimeWorkerError("fix retry verification binding changed")

    def reconstruct_fix_state(self) -> FixTransportState | None:
        _, completion_policy, total_pass_limit, approved_plan_sha256 = (
            self.load_fix_policy()
        )
        controller = self.load_fix_controller(approved_plan_sha256)
        initial_head_sha = str(controller["initial_head_sha"])
        parent = self.store.read(self.spec["owner_id"], self.spec["operation_id"])
        initial_root = self.spec_path.parent / "pipeline-fix" / "pass-0"
        initial_receipts = self.load_fix_receipts(
            initial_root,
            ("reproduce", "root-cause", "regression-test", "minimal-fix"),
        )
        initial_progress = reconcile_fix(
            parent,
            definition_sha256=self.pipeline.definition_sha256,
            approved_plan_sha256=approved_plan_sha256,
            initial_head_sha=initial_head_sha,
            receipts=tuple(initial_receipts),
            iteration=0,
        )
        if initial_progress.action == "attention":
            cannot_receipt = initial_progress.prior_receipt
            if cannot_receipt is None:
                raise RuntimeWorkerError("cannot-reproduce receipt is unavailable")
            emit_compiled_pipeline_event(
                self.spec["cwd"],
                event="fix-phase-attention",
                pipeline_id=self.pipeline.definition.pipeline_id,
                pipeline_version=self.pipeline.definition.version,
                profile=self.pipeline.definition.profile,
                compiler_outcome="resolved",
                definition_sha=self.pipeline.definition_sha256,
                primitive_count=len(self.pipeline.definition.steps),
                loop_iteration=0,
                attention_category="cannot-reproduce",
            )
            self.notify_cannot_reproduce(cannot_receipt)
            self.summary_attention(
                "pipeline-fix-cannot-reproduce",
                AttentionReason.ATTENTION_REQUIRED,
            )
            return None
        retry_intent, iteration = self.load_retry_intent(
            completion_policy=completion_policy,
            total_pass_limit=total_pass_limit,
            initial_receipts=initial_receipts,
            initial_progress=initial_progress,
        )
        receipt_root = initial_root
        receipts = initial_receipts
        progress = initial_progress
        if retry_intent is not None:
            receipt_root = self.spec_path.parent / "pipeline-fix" / f"pass-{iteration}"
            receipts = self.load_fix_receipts(
                receipt_root, ("root-cause", "regression-test", "minimal-fix")
            )
            progress = reconcile_retry_fix(
                parent,
                definition_sha256=self.pipeline.definition_sha256,
                reproduction_receipt=initial_receipts[0],
                verification_sha256=str(retry_intent["verification_sha256"]),
                failed_head_sha=str(retry_intent["failed_head_sha"]),
                current_head_sha=str(retry_intent["current_head_sha"]),
                receipts=tuple(receipts),
                iteration=iteration,
            )
        return FixTransportState(
            completion_policy,
            total_pass_limit,
            approved_plan_sha256,
            initial_head_sha,
            parent,
            initial_receipts,
            iteration,
            receipt_root,
            receipts,
            progress,
            retry_intent,
        )

    def publish_null_change_continuation(self, state: FixTransportState) -> None:
        """Raise one typed decision for a retry that changed nothing."""

        head_sha = self.git_head()
        notify_path = (
            self.spec_path.parent
            / "pipeline-fix"
            / f"pass-{state.iteration}"
            / "null-change-notify.json"
        )
        delivery = {
            "schema_version": 1,
            "operation_id": self.spec["operation_id"],
            "iteration": state.iteration,
            "head_sha": head_sha,
            "status": "sent",
        }
        if notify_path.is_file() and (not notify_path.is_symlink()):
            if json.loads(notify_path.read_text(encoding="utf-8")) != delivery:
                raise RuntimeWorkerError("null-change continuation changed")
            return
        attention_path = self.spec["cwd"] / ".task-needs-attention.json"
        marker = {
            "version": 1,
            "id": f"pipeline-decision-{head_sha[:24]}",
            "status": "pending",
            "task_name": "engineering/fix retry changed nothing",
            "category": "pipeline-decision",
            "reason": "The bounded fix retry completed without changing the verified HEAD",
            "question": "Choose stop or retry-with-scope",
            "worktree": str(self.spec["cwd"]),
            "task_surface": self.spec["surface_id"],
            "raised_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "iteration": state.iteration,
            "head_sha": head_sha,
            "allowed_decisions": ["stop", "retry-with-scope"],
        }
        try:
            raised = append_raise(self.spec["cwd"], marker)
        except EscalationRecordError as exc:
            raise RuntimeWorkerError(f"pipeline decision packet is invalid: {exc}") from exc
        if raised.record_id != marker["id"] or raised.payload.get("status") != "pending":
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
        message = f"Typed task escalation callback received. Category: pipeline-decision. Bounded fix retry {state.iteration} completed with an empty change set at {head_sha}. Inspect {attention_path} and resolve from the originating coordinator with: {command}. Allowed decisions: stop, retry-with-scope."
        if len(message.encode()) > 4096:
            raise RuntimeWorkerError("pipeline decision notification exceeds its bound")
        emit_compiled_pipeline_event(
            self.spec["cwd"],
            event="fix-retry-null-change",
            pipeline_id=self.pipeline.definition.pipeline_id,
            pipeline_version=self.pipeline.definition.version,
            profile=self.pipeline.definition.profile,
            compiler_outcome="resolved",
            definition_sha=self.pipeline.definition_sha256,
            primitive_count=len(self.pipeline.definition.steps),
            loop_iteration=state.iteration,
            attention_category="retry-null-change",
        )
        self.cmux_adapter.send(self.spec["origin_surface"], message)
        self.cmux_adapter.send_key(self.spec["origin_surface"], "Enter")
        self.write_immutable_json(notify_path, delivery)
        self.summary_attention(
            "pipeline-fix-retry-null-change",
            AttentionReason.ATTENTION_REQUIRED,
        )

    def prepare_fix_round(self, state: FixTransportState) -> object | None:
        if state.progress.action == "complete":
            self.retarget_fix_callback(
                operation_id=self.spec["operation_id"],
                run_id=self.spec["run_id"],
                callback_pointer=".task-summary.json",
            )
            if self.notify_fix_finalization(state.iteration):
                emit_compiled_pipeline_event(
                    self.spec["cwd"],
                    event="fix-final-retarget",
                    pipeline_id=self.pipeline.definition.pipeline_id,
                    pipeline_version=self.pipeline.definition.version,
                    profile=self.pipeline.definition.profile,
                    compiler_outcome="resolved",
                    definition_sha=self.pipeline.definition_sha256,
                    primitive_count=len(self.pipeline.definition.steps),
                    loop_iteration=state.iteration,
                    terminal_category="phases-complete",
                )
            if (
                state.retry_intent is not None
                and self.git_head() == state.retry_intent["current_head_sha"]
            ):
                # An unchanged HEAD cannot carry the failed verification into
                # completion. Wait for the retry to publish its finalization,
                # then hand the null-change outcome to the coordinator instead
                # of returning silently forever.
                if self.spec["task_summary_pointer"].is_file():
                    self.publish_null_change_continuation(state)
                return None
            self.fix_transport_complete = True
            return None
        if self.spec["task_summary_pointer"].is_file():
            _atomic_json(
                self.spec_path.parent / "pipeline-fix" / "early-summary.json",
                {
                    "schema_version": 1,
                    "operation_id": self.spec["operation_id"],
                    "status": "ignored-until-phases-complete",
                },
            )
        if state.retry_intent is None:
            return prepare_next_phase(
                self.store,
                state.parent,
                definition_sha256=self.pipeline.definition_sha256,
                approved_plan_sha256=state.approved_plan_sha256,
                initial_head_sha=state.initial_head_sha,
                receipts=tuple(state.receipts),
                iteration=0,
            )
        return prepare_retry_phase(
            self.store,
            state.parent,
            definition_sha256=self.pipeline.definition_sha256,
            reproduction_receipt=state.initial_receipts[0],
            verification_sha256=str(state.retry_intent["verification_sha256"]),
            failed_head_sha=str(state.retry_intent["failed_head_sha"]),
            current_head_sha=str(state.retry_intent["current_head_sha"]),
            receipts=tuple(state.receipts),
            iteration=state.iteration,
        )

    def publish_fix_request(
        self, state: FixTransportState, round_: object
    ) -> tuple[Path, str]:
        request = fix_phase_request(round_)
        result_pointer = str(request["result_pointer"])
        request, owner = self.publish_pipeline_step_contract(request)
        self._phase_timing_start(state, round_)
        self.retarget_fix_callback(
            operation_id=round_.spec.operation_id,
            run_id=round_.run_id,
            callback_pointer=".task-pipeline-step-callback.json",
        )
        _generation, operation_id, run_id, callback_path = _callback_target(self.spec)
        if operation_id != round_.spec.operation_id or run_id != round_.run_id:
            raise RuntimeWorkerError("engineering/fix active callback target changed")
        result_digest = _bounded_file_sha256(
            self.spec["cwd"] / result_pointer
        )
        if (
            not callback_path.is_file()
            and (
                not result_digest
                or result_digest == owner.template_artifact_sha256
            )
        ):
            self.notify_fix_phase(request)
        return callback_path, result_pointer

    def submit_fix_result(
        self, round_: object, callback_path: Path, result_pointer: str
    ) -> None:
        if self.adopt_fresh_pipeline_step_result():
            return
        result_path = self.spec["cwd"] / result_pointer
        result_digest = _bounded_file_sha256(result_path)
        if not result_digest:
            return
        owner = getattr(self, "pipeline_step_artifact_owner", None)
        if (
            isinstance(owner, ContractArtifactOwner)
            and owner.actual_target == result_path
            and result_digest == owner.template_artifact_sha256
        ):
            return
        try:
            request = json.loads(
                (self.spec["cwd"] / ".task-pipeline-step-request.json").read_text(
                    encoding="utf-8"
                )
            )
            output_path = self.spec["cwd"] / str(request["output_pointer"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.summary_attention("pipeline-fix-request-invalid")
            return
        output_digest = _bounded_file_sha256(output_path)
        if not output_digest:
            return
        if output_digest != self.fix_output_digest:
            self.fix_output_digest = output_digest
            self.fix_output_stable_reads = 1
            return
        self.fix_output_stable_reads += 1
        if result_digest != self.fix_result_digest:
            self.fix_result_digest = result_digest
            self.fix_result_stable_reads = 1
            return
        self.fix_result_stable_reads += 1
        if (
            self.fix_result_stable_reads < 2
            or self.fix_output_stable_reads < 2
            or self.fix_submit_attempt_digest == result_digest
        ):
            return
        self.fix_submit_attempt_digest = result_digest
        submitted = subprocess.run(
            [
                sys.executable,
                str(self.trusted_vault / "scripts" / "pipeline-step-submit.py"),
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
                self.spec_path.parent / "pipeline-fix" / "submit-failed.json"
            ),
            operation_id=round_.spec.operation_id,
            invalid_sha256=(
                _bounded_file_sha256(result_path) or result_digest
            ),
            stage="pipeline-fix-submit",
        )

    def accept_fix_callback(
        self, state: FixTransportState, round_: object, callback_path: Path
    ) -> None:
        try:
            raw = callback_path.read_bytes()
        except FileNotFoundError:
            result_pointer = str(fix_phase_request(round_)["result_pointer"])
            self.submit_fix_result(round_, callback_path, result_pointer)
            return
        if not raw or len(raw) > MAX_OUTBOX_BYTES:
            raise RuntimeWorkerError("engineering/fix phase callback is invalid")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != self.fix_callback_digest:
            self.fix_callback_digest = digest
            self.fix_callback_stable_reads = 1
            return
        self.fix_callback_stable_reads += 1
        if self.fix_callback_stable_reads < 2:
            return
        envelope = _envelope(json.loads(raw))
        receipt_path = state.receipt_root / round_.step_id / "receipt.json"
        accepted = accept_phase(
            self.store,
            round_,
            envelope,
            current_head_sha=self.git_head(),
            receipt_path=receipt_path,
        )
        self._write_phase_timing_completion(state, round_, accepted)
        callback_path.unlink()
        emit_compiled_pipeline_event(
            self.spec["cwd"],
            event="fix-phase-accepted",
            pipeline_id=self.pipeline.definition.pipeline_id,
            pipeline_version=self.pipeline.definition.version,
            profile=self.pipeline.definition.profile,
            compiler_outcome="resolved",
            definition_sha=self.pipeline.definition_sha256,
            primitive_count=len(self.pipeline.definition.steps),
            loop_iteration=accepted.iteration,
            terminal_category=accepted.step_id,
        )
        self.fix_callback_digest = ""
        self.fix_callback_stable_reads = 0
        self.fix_result_digest = ""
        self.fix_result_stable_reads = 0
        self.fix_output_digest = ""
        self.fix_output_stable_reads = 0
        self.fix_submit_attempt_digest = ""

    def drive_fix_transport(self) -> None:
        if (
            self._pipeline_name != "engineering/fix"
            or self.callback_handled
            or self.fix_transport_complete
        ):
            return
        try:
            state = self.reconstruct_fix_state()
            if state is None:
                return
            round_ = self.prepare_fix_round(state)
            if round_ is None:
                return
            callback_path, _result_pointer = self.publish_fix_request(state, round_)
            self.accept_fix_callback(state, round_, callback_path)
        except (
            FixWorkflowError,
            RuntimeWorkerError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            self.summary_attention("pipeline-fix-callback-invalid")
