"""Composition root for one short-lived runtime worker."""

from __future__ import annotations

from .runtime_worker import *  # noqa: F401,F403
from .runtime_worker import (
    _atomic_json,
    _contain_provider_start_failure,
    _research_input_provenance,
)
from .runtime_worker_control import RuntimeWorkerControlMixin
from .runtime_worker_fix import RuntimeWorkerFixMixin
from .runtime_worker_custom import RuntimeWorkerCustomMixin
from .runtime_worker_summary import RuntimeWorkerSummaryMixin
from .runtime_worker_review_bridge import RuntimeWorkerReviewBridgeMixin
from .runtime_worker_verification import RuntimeWorkerVerificationMixin
from .runtime_worker_liveness import RuntimeWorkerLivenessMixin
from .runtime_worker_loop import RuntimeWorkerLoopMixin


class RuntimeWorkerExecution(
    RuntimeWorkerReviewBridgeMixin,
    RuntimeWorkerControlMixin,
    RuntimeWorkerFixMixin,
    RuntimeWorkerCustomMixin,
    RuntimeWorkerSummaryMixin,
    RuntimeWorkerVerificationMixin,
    RuntimeWorkerLivenessMixin,
    RuntimeWorkerLoopMixin,
):
    def execute(
        self,
        spec_path: Path,
        *,
        poll_seconds: float = 0.1,
        checkpoint_probe: Callable[[str, str], str] | None = None,
        cmux_adapter: object | None = None,
        review_launcher: Callable[[Path, Path], None] | None = None,
        verification_runner: (
            Callable[..., subprocess.CompletedProcess[str]] | None
        ) = None,
        callback_submit_policy: CallbackSubmitPolicy | None = None,
        clock: Callable[[], float] | None = None,
    ) -> int:
        self.spec_path = spec_path
        self.poll_seconds = poll_seconds
        self.checkpoint_probe = checkpoint_probe
        self.cmux_adapter = cmux_adapter
        self.review_launcher = review_launcher
        self.verification_runner = verification_runner
        self.clock = clock or time.time
        self.spec = load_spec(self.spec_path.resolve())
        self.ready = self.spec["ready_path"]
        self.exit_path = self.spec["exit_path"]
        self.store = OperationStore(self.spec["store_root"])
        try:
            operation = self.store.read(
                self.spec["owner_id"], self.spec["operation_id"]
            )
        except StoreError as exc:
            raise RuntimeWorkerError(
                "runtime operation authority is unavailable"
            ) from exc
        expected_reviewer = (
            operation.spec.route.profile == "reviewer-callback"
        )
        if (
            self.spec["reviewer_sandbox"] is not expected_reviewer
            or self.spec["runtime"] != operation.spec.route.runtime
        ):
            raise RuntimeWorkerError("runtime launch authority drifted")
        self.trusted_store = self.spec["store_root"]
        self.trusted_vault = self.trusted_store.parent.parent
        if (
            self.spec["callback_mode"] == "task-summary"
            and self.trusted_store != self.trusted_vault / ".vault-meta" / "harness"
        ):
            _atomic_json(self.ready, {"schema_version": 1, "status": "failed"})
            _atomic_json(
                self.exit_path,
                {"schema_version": 1, "status": "store-root-invalid", "exit_code": 2},
            )
            return 2
        self.process = ProcessAdapter()
        self.handle: ProcessHandle | None = None
        self.research_input_sha256 = ""
        if self.spec["callback_mode"] == "research-synth":
            try:
                self.research_input_sha256 = _research_input_provenance(
                    self.spec, self.spec_path, create=True
                )
            except (OSError, ResearchContractError, RuntimeWorkerError):
                try:
                    self.store.transition(
                        self.spec["owner_id"],
                        self.spec["operation_id"],
                        "attention-required",
                        reason=AttentionReason.CALLBACK_INVALID,
                    )
                except Exception:
                    pass
                _atomic_json(self.ready, {"schema_version": 1, "status": "failed"})
                _atomic_json(
                    self.exit_path,
                    {
                        "schema_version": 1,
                        "status": "research-input-invalid",
                        "exit_code": 2,
                    },
                )
                return 2
        try:
            self.provider_command = provider_argv(self.spec)
            self.provider_env = provider_environment(self.spec)
            self.handle = self.process.start(
                self.provider_command, cwd=self.spec["cwd"], env=self.provider_env
            )
            self.supervisor_identity = self.process.capture_identity(os.getpid())
            if not self.supervisor_identity:
                raise ProcessError("runtime worker identity is unavailable")
        except (OSError, ProcessError):
            if self.handle is not None:
                self.contain_provider_start_failure(self.process, self.handle)
            _atomic_json(self.ready, {"schema_version": 1, "status": "failed"})
            _atomic_json(
                self.exit_path,
                {"schema_version": 1, "status": "start-failed", "exit_code": 127},
            )
            return 127
        _atomic_json(
            self.ready,
            {
                "schema_version": 1,
                "status": "ready",
                "pid": self.handle.pid,
                "process_group": self.handle.process_group,
                "supervisor_pid": os.getpid(),
                "process_identity": self.handle.process_identity,
                "supervisor_identity": self.supervisor_identity,
            },
        )
        self.checkpoint_probe = self.checkpoint_probe or CmuxAdapter().resume_checkpoint
        self.checkpoint = ""
        self.next_checkpoint_probe = 0.0
        self.active_target: tuple[int, str, str, Path] | None = None
        self.last_digest = ""
        self.stable_reads = 0
        self.review_input_digest = ""
        self.review_input_stable_reads = 0
        self.callback_handled = False
        self.registration_invalid = False
        self.summary_digest = ""
        self.summary_stable_reads = 0
        self.summary_attention_revision = -1
        self.cmux_adapter = self.cmux_adapter or CmuxAdapter()
        self.operation_contract = operation.spec.contract_sha256
        try:
            self._pipeline_name, self.pipeline = compiled_executable_for_contract(
                self.operation_contract
            )
            self.pipeline_extra_commands: tuple[str, ...] = ()
            self.custom_pipeline_spec = None
        except ValueError:
            try:
                (
                    self._pipeline_name,
                    self.pipeline,
                    self.pipeline_extra_commands,
                    self.custom_pipeline_spec,
                ) = resolve_custom_executable(
                    store_root=self.spec_path.parent.parent,
                    operation_id=self.spec["operation_id"],
                    definition_sha256=self.operation_contract,
                    registry=builtin_registry(),
                    policy=CustomPipelinePolicy.default(),
                    capabilities=("route:resolved",),
                )
            except (ContractError, OSError, ValueError):
                self._pipeline_name, self.pipeline, self.pipeline_extra_commands = (
                    "",
                    None,
                    (),
                )
                self.custom_pipeline_spec = None
        self.is_custom_pipeline = self.custom_pipeline_spec is not None
        self.last_prompt_digest = ""
        self.latest_screen_digest = ""
        self.latest_prompt_state = "unknown"
        self.next_prompt_probe = 0.0
        self.liveness_policy = LivenessPolicy.default()
        self.callback_submit_policy = (
            callback_submit_policy or CallbackSubmitPolicy.default()
        )
        self.liveness_controller = LivenessController(
            self.spec_path.parent / "liveness"
        )
        self.next_liveness_probe = 0.0
        self.latest_callback_prompt_class = "unknown"
        self.callback_idle_observations = 0
        self.callback_prompt_observations = 0
        self.callback_generation_identity = ""
        self.callback_generation_progress_at = 0.0
        self.callback_recovery_input_digest = ""
        self.callback_recovery_input_reads = 0
        self.callback_recovery_digest = ""
        self.callback_recovery_reads = 0
        self.handled_control_id = ""
        self.invalid_control_digest = ""
        self.fix_callback_digest = ""
        self.fix_callback_stable_reads = 0
        self.fix_result_digest = ""
        self.fix_result_stable_reads = 0
        self.fix_submit_attempt_digest = ""
        self.fix_transport_complete = (
            self._pipeline_name != "engineering/fix" or self.is_custom_pipeline
        )
        self.custom_transport_complete = not self.is_custom_pipeline
        self.custom_callback_digest = ""
        self.custom_callback_stable_reads = 0
        self.custom_result_digest = ""
        self.custom_result_stable_reads = 0
        self.custom_submit_attempt_digest = ""
        self.exit_code = 0
        self.provider_exited = False
        self.exit_containment_failed = False
        return self.run_provider_loop()
