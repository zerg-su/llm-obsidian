"""Engineering/fix transport reconstruction and callback acceptance."""

from __future__ import annotations

from dataclasses import dataclass

from .runtime_worker import *  # noqa: F401,F403
from .runtime_worker import (
    _atomic_json,
    _bounded_file_sha256,
    _callback_target,
    _envelope,
    _submit_failure_requires_attention,
)


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
        result_pointer = (
            f".task-pipeline/results/pass-{state.iteration}/{round_.step_id}.json"
        )
        output_pointer = (
            f".task-pipeline/outputs/pass-{state.iteration}/{round_.step_id}.md"
        )
        request = {
            "schema_version": 1,
            "operation_id": round_.spec.operation_id,
            "run_id": round_.run_id,
            "parent_operation_id": round_.parent_operation_id,
            "lane_id": round_.lane_id,
            "definition_sha256": round_.spec.contract_sha256,
            "step_id": round_.step_id,
            "iteration": round_.iteration,
            "input_schema": round_.input_schema,
            "input_sha256": round_.input_sha256,
            "input_head_sha": round_.input_head_sha,
            "prior_receipt_sha256": round_.prior_receipt_sha256,
            "verification_sha256": round_.verification_sha256,
            "output_schema": round_.output_schema,
            "result_pointer": result_pointer,
            "output_pointer": output_pointer,
        }
        _atomic_json(self.spec["cwd"] / ".task-pipeline-step-request.json", request)
        self.retarget_fix_callback(
            operation_id=round_.spec.operation_id,
            run_id=round_.run_id,
            callback_pointer=".task-pipeline-step-callback.json",
        )
        self.notify_fix_phase(request)
        _generation, operation_id, run_id, callback_path = _callback_target(self.spec)
        if operation_id != round_.spec.operation_id or run_id != round_.run_id:
            raise RuntimeWorkerError("engineering/fix active callback target changed")
        return callback_path, result_pointer

    def submit_fix_result(
        self, round_: object, callback_path: Path, result_pointer: str
    ) -> None:
        result_path = self.spec["cwd"] / result_pointer
        result_digest = _bounded_file_sha256(result_path)
        if not result_digest:
            return
        if result_digest != self.fix_result_digest:
            self.fix_result_digest = result_digest
            self.fix_result_stable_reads = 1
            return
        self.fix_result_stable_reads += 1
        if (
            self.fix_result_stable_reads < 2
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
        if _submit_failure_requires_attention(submitted, callback_path):
            _atomic_json(
                self.spec_path.parent / "pipeline-fix" / "submit-failed.json",
                {
                    "schema_version": 1,
                    "operation_id": round_.spec.operation_id,
                    "returncode": submitted.returncode,
                    "status": "attention-required",
                },
            )
            self.summary_attention(
                "pipeline-fix-submit-failed", AttentionReason.CALLBACK_INVALID
            )

    def accept_fix_callback(
        self, state: FixTransportState, round_: object, callback_path: Path
    ) -> None:
        try:
            raw = callback_path.read_bytes()
        except FileNotFoundError:
            result_pointer = (
                f".task-pipeline/results/pass-{state.iteration}/{round_.step_id}.json"
            )
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
