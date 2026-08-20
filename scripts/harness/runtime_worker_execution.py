"""Composition root for one short-lived runtime worker."""

from __future__ import annotations

from .runtime_worker import *  # noqa: F401,F403
from .runtime_worker import (
    _atomic_json,
    _contain_provider_start_failure,
    _research_input_provenance,
)
from .runtime_provider_events import (
    RuntimeProviderEventError,
    RuntimeProviderEventStream,
)
from .runtime_provider_input import interactive_provider_input
from .runtime_session_contracts import MAX_PROMPT_BYTES
from .runtime_session_continuation import (
    _editor_digest,
    _no_checkpoint,
    _prompt_anchor,
    _screen_digest,
    await_initial_input_ready,
    await_initial_input_visible,
    await_initial_start_acknowledged,
    paste_editor_text,
    resolve_recognized_provider_prompt,
)
from .runtime_worker_control import RuntimeWorkerControlMixin
from .runtime_worker_fix import RuntimeWorkerFixMixin
from .runtime_worker_custom import RuntimeWorkerCustomMixin
from .runtime_worker_summary import RuntimeWorkerSummaryMixin
from .runtime_worker_review_bridge import RuntimeWorkerReviewBridgeMixin
from .runtime_worker_verification import RuntimeWorkerVerificationMixin
from .runtime_worker_liveness import RuntimeWorkerLivenessMixin
from .runtime_worker_loop import RuntimeWorkerLoopMixin
from .artifact_repair import ArtifactRepairError
from .cmux_wake_source import CmuxWakeSource, WakeBinding, WakeObservation


class _UnavailableWakeSource:
    """Optional-source containment when no exact cmux binding is available."""

    def __init__(self, monotonic: Callable[[], float]) -> None:
        self.monotonic = monotonic

    def start(self) -> bool:
        return False

    def wait(self, _timeout: float) -> WakeObservation:
        return WakeObservation("unavailable", observed_at=self.monotonic())

    def retry(self) -> bool:
        return False

    def refresh_generation(self, _generation: int) -> None:
        return None

    def close(self) -> None:
        return None


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
    def _optional_wake_source(self, workspace_id: str) -> object:
        """Contain an unusable optional wake identity as polling fallback."""

        if not workspace_id:
            return _UnavailableWakeSource(self.monotonic_clock)
        try:
            binding = WakeBinding(
                runtime_root=self.spec_path.parent,
                workspace_id=workspace_id,
                surface_id=self.spec["surface_id"],
                owner_id=self.spec["owner_id"],
                operation_id=self.spec["operation_id"],
                run_id=self.spec["run_id"],
                generation=self.initial_generation,
            )
        except ValueError:
            return _UnavailableWakeSource(self.monotonic_clock)
        return CmuxWakeSource(binding, monotonic=self.monotonic_clock)

    def _await_parent_start_committed(
        self,
        *,
        observation_limit: int = 160,
        observation_interval_seconds: float = 0.05,
    ) -> bool:
        """Wait until the parent has durably adopted the published process.

        The ready handshake proves only process ownership.  Provider input is
        a later external effect and must not race the manager's start-provider
        commit.  Existing direct worker fixtures begin at awaiting-callback and
        therefore cross this seam immediately.
        """

        for observation in range(observation_limit):
            record = self.store.read(
                self.spec["owner_id"], self.spec["operation_id"]
            )
            if (
                record.state in {"running", "awaiting-callback"}
                and not record.pending_effect
            ):
                return True
            if record.state in {
                "attention-required",
                "complete",
                "failed",
                "cancelled",
            }:
                return False
            if observation + 1 < observation_limit:
                self.sleeper(observation_interval_seconds)
        return False

    def _await_own_start_committed(
        self,
        *,
        observation_limit: int = 160,
        observation_interval_seconds: float = 0.05,
    ) -> bool:
        """Wait until the manager durably committed this start-provider effect.

        Provider input must not race the manager's start commit, but only the
        launch window owns that hazard: a restarted generation begins with no
        pending start effect and crosses this seam immediately, leaving every
        later recovery state with its existing owner.
        """

        for observation in range(observation_limit):
            record = self.store.read(
                self.spec["owner_id"], self.spec["operation_id"]
            )
            starting = (
                record.state == "starting"
                or record.pending_effect == "start-provider"
            )
            if not starting:
                return True
            if observation + 1 < observation_limit:
                self.sleeper(observation_interval_seconds)
        return False

    def _workspace_id(self) -> str:
        path = self.spec_path.parent / "session.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeWorkerError(
                "runtime provider event session identity is unavailable"
            ) from exc
        workspace_id = value.get("workspace_id") if isinstance(value, dict) else ""
        if (
            not isinstance(value, dict)
            or value.get("operation_id") != self.spec["operation_id"]
            or value.get("run_id") != self.spec["run_id"]
            or not isinstance(workspace_id, str)
            or not workspace_id
        ):
            raise RuntimeWorkerError(
                "runtime provider event workspace identity is unavailable"
            )
        return workspace_id

    @property
    def _provider_event_root(self) -> Path:
        return self.spec_path.parent / "provider-events"

    def _create_provider_stream(
        self, *, generation: int, input_sha256: str
    ) -> RuntimeProviderEventStream:
        return RuntimeProviderEventStream.create(
            self._provider_event_root,
            owner_id=self.spec["owner_id"],
            operation_id=self.spec["operation_id"],
            run_id=self.spec["run_id"],
            generation=generation,
            process_identity=self.handle.process_identity,
            workspace_id=self._workspace_id(),
            surface_id=self.spec["surface_id"],
            input_sha256=input_sha256,
        )

    def _initial_generation(self) -> int:
        try:
            value = json.loads(
                self.spec["callback_registration"].read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeWorkerError(
                "initial provider callback generation is unavailable"
            ) from exc
        generation = value.get("generation") if isinstance(value, dict) else None
        if type(generation) is not int or generation < 1:
            raise RuntimeWorkerError(
                "initial provider callback generation is invalid"
            )
        return generation

    def _provider_stream(
        self, generation: int
    ) -> RuntimeProviderEventStream | None:
        state = (
            self._provider_event_root
            / f"generation-{generation}"
            / "delivery"
            / "delivery-state.json"
        )
        if not state.is_file() or state.is_symlink():
            return None
        try:
            return RuntimeProviderEventStream.rehydrate(
                self._provider_event_root, generation
            )
        except RuntimeProviderEventError as exc:
            raise RuntimeWorkerError(
                "runtime provider delivery authority is unavailable"
            ) from exc

    def record_provider_result(self, generation: int, sha256: str) -> None:
        try:
            stream = self._provider_stream(generation)
            if stream is None:
                return
            decision = stream.result(sha256)
        except RuntimeProviderEventError as exc:
            raise RuntimeWorkerError("provider result event is invalid") from exc
        if decision.action not in {"close", "wait"}:
            raise RuntimeWorkerError("provider result did not reach a close boundary")

    def record_provider_exit(self, exit_code: int) -> None:
        for directory in sorted(self._provider_event_root.glob("generation-*")):
            try:
                generation = int(directory.name.removeprefix("generation-"))
                stream = self._provider_stream(generation)
                if stream is None:
                    continue
                decision = stream.process_exited(exit_code)
            except (RuntimeProviderEventError, RuntimeWorkerError, ValueError):
                self.mark_attention(AttentionReason.ATTENTION_REQUIRED)
                continue
            if decision.action == "attention":
                self.mark_attention(AttentionReason.ATTENTION_REQUIRED)

    def _submit_initial_input(
        self, initial_start_observation_limit: int | None
    ) -> bool:
        """Deliver one initial prompt and prove its semantic start boundary."""

        initial_input = self.spec.get("initial_input_pointer")
        if not isinstance(initial_input, Path):
            return True
        initial_start_budget: dict[str, int] = (
            {}
            if initial_start_observation_limit is None
            else {"observation_limit": initial_start_observation_limit}
        )
        stream: RuntimeProviderEventStream | None = None
        failure_reason = "initial-input-error"
        try:
            raw_input = initial_input.read_bytes()
            if not raw_input or len(raw_input) > MAX_PROMPT_BYTES:
                raise RuntimeWorkerError("initial provider input is invalid")
            if self.spec["callback_mode"] in {"research-fetch", "research-synth"}:
                failure_reason = "research-argv-prompt-drift"
                try:
                    argv_input = self.spec["argv"][-1].encode("utf-8")
                except (IndexError, UnicodeEncodeError) as exc:
                    raise RuntimeWorkerError(
                        "persisted research argv input is invalid"
                    ) from exc
                if raw_input != argv_input:
                    raise RuntimeWorkerError(
                        "research prompt pointer drifted from persisted argv"
                    )
                stream = self._create_provider_stream(
                    generation=self.initial_generation,
                    input_sha256=hashlib.sha256(raw_input).hexdigest(),
                )
                stream.start()
                if stream.reserve_input().action != "send":
                    raise RuntimeWorkerError(
                        "initial provider input was not durably reserved"
                    )
                stream.accept_input()
                return True
            input_text = raw_input.decode("utf-8")
            delivery_text = interactive_provider_input(
                self.spec["runtime"], initial_input, input_text
            )
            delivery_bytes = delivery_text.encode("utf-8")
            if not await_initial_input_ready(
                self.cmux_adapter,
                surface_id=self.spec["surface_id"],
                runtime=self.spec["runtime"],
            ):
                raise RuntimeWorkerError(
                    "initial provider editor did not become ready"
                )
            stream = self._create_provider_stream(
                generation=self.initial_generation,
                input_sha256=hashlib.sha256(delivery_bytes).hexdigest(),
            )
            stream.start()
            decision = stream.reserve_input()
            if decision.action != "send":
                raise RuntimeWorkerError(
                    "initial provider input was not durably reserved"
                )
            before_editor_sha256 = _editor_digest(
                self.spec["runtime"],
                self.cmux_adapter.read(self.spec["surface_id"]),
            )
            paste_editor_text(
                self.cmux_adapter,
                surface_id=self.spec["surface_id"],
                text=delivery_text,
            )
            if not await_initial_input_visible(
                self.cmux_adapter,
                surface_id=self.spec["surface_id"],
                runtime=self.spec["runtime"],
                text=delivery_text,
                before_editor_sha256=before_editor_sha256,
            ):
                raise RuntimeWorkerError(
                    "initial provider input was not visible"
                )
            paste_screen_sha256 = _screen_digest(
                self.cmux_adapter.read(self.spec["surface_id"])
            )
            self.cmux_adapter.send_key(self.spec["surface_id"], "Enter")
            # A returned keystroke is transport, not a started turn.  Hold the
            # reserved send until the provider crosses a semantic boundary.
            acknowledgement = await_initial_start_acknowledged(
                self.cmux_adapter,
                surface_id=self.spec["surface_id"],
                runtime=self.spec["runtime"],
                anchor=_prompt_anchor(delivery_text),
                paste_screen_sha256=paste_screen_sha256,
                artifact_ready=self.spec["callback_pointer"].is_file,
                checkpoint_probe=self.checkpoint_probe or _no_checkpoint,
                **initial_start_budget,
            )
            if acknowledgement == "still-composing":
                # A swallowed submit keystroke leaves the exact pasted prompt
                # in an input-ready composer for the whole first window.  One
                # second identity-bound Enter — never a prompt resend — may
                # start the turn; every other verdict (permission, unknown,
                # unconfirmed, missing) keeps its original single-keystroke
                # fail-closed boundary, and a still-composing second window
                # stays contained as before.
                self.cmux_adapter.send_key(self.spec["surface_id"], "Enter")
                acknowledgement = await_initial_start_acknowledged(
                    self.cmux_adapter,
                    surface_id=self.spec["surface_id"],
                    runtime=self.spec["runtime"],
                    anchor=_prompt_anchor(delivery_text),
                    paste_screen_sha256=paste_screen_sha256,
                    artifact_ready=self.spec["callback_pointer"].is_file,
                    checkpoint_probe=self.checkpoint_probe or _no_checkpoint,
                    **initial_start_budget,
                )
            if acknowledgement == "permission":
                family = resolve_recognized_provider_prompt(
                    self.cmux_adapter,
                    surface_id=self.spec["surface_id"],
                    runtime=self.spec["runtime"],
                )
                if family:
                    acknowledgement = await_initial_start_acknowledged(
                        self.cmux_adapter,
                        surface_id=self.spec["surface_id"],
                        runtime=self.spec["runtime"],
                        anchor=_prompt_anchor(delivery_text),
                        paste_screen_sha256=paste_screen_sha256,
                        artifact_ready=self.spec["callback_pointer"].is_file,
                        checkpoint_probe=(
                            self.checkpoint_probe or _no_checkpoint
                        ),
                        **initial_start_budget,
                    )
                    failure_reason = (
                        f"post-submit-{family}-{acknowledgement}"
                    )
                else:
                    failure_reason = "post-submit-prompt-unresolved"
            else:
                failure_reason = f"initial-start-{acknowledgement}"
            if acknowledgement != "started":
                raise RuntimeWorkerError(
                    "initial provider start was not acknowledged: "
                    f"{acknowledgement}"
                )
            stream.accept_input()
            return True
        except (
            OSError,
            UnicodeDecodeError,
            RuntimeProviderEventError,
            RuntimeWorkerError,
        ):
            try:
                if stream is not None:
                    stream.ambiguous_input()
            except Exception:
                pass
            self.contain_provider_start_failure(self.process, self.handle)
            self.mark_attention(AttentionReason.ATTENTION_REQUIRED)
            _atomic_json(self.ready, {"schema_version": 1, "status": "failed"})
            _atomic_json(
                self.exit_path,
                {
                    "schema_version": 1,
                    "status": "input-unconfirmed",
                    "exit_code": 2,
                    "reason": failure_reason,
                },
            )
            return False

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
        wall_clock: Callable[[], float] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        wake_source: object | None = None,
        fault_observer: Callable[[str], None] | None = None,
        initial_start_observation_limit: int | None = None,
    ) -> int:
        self.spec_path = spec_path
        self.poll_seconds = poll_seconds
        self.checkpoint_probe = checkpoint_probe
        self.cmux_adapter = cmux_adapter
        self.review_launcher = review_launcher
        self.verification_runner = verification_runner
        self.wall_clock = wall_clock or clock or time.time
        self.monotonic_clock = monotonic_clock or time.monotonic
        self.sleeper = sleeper or time.sleep
        self.fault_observer = fault_observer
        # Compatibility owner for existing callback-timeout code.  New test
        # seams name wall and monotonic clocks independently.
        self.clock = self.wall_clock
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
        self.task_summary_artifact_owner = None
        if self.spec["callback_mode"] == "task-summary":
            try:
                self.publish_task_summary_contract()
            except (ArtifactRepairError, ContractError, RuntimeWorkerError):
                _atomic_json(self.ready, {"schema_version": 1, "status": "failed"})
                _atomic_json(
                    self.exit_path,
                    {
                        "schema_version": 1,
                        "status": "task-summary-template-invalid",
                        "exit_code": 2,
                    },
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
        self.cmux_adapter = self.cmux_adapter or CmuxAdapter()
        try:
            self.provider_command = provider_argv(self.spec)
            self.provider_env = provider_environment(self.spec)
            self.initial_generation = self._initial_generation()
            self._last_wake_generation = self.initial_generation
            try:
                wake_workspace_id = self._workspace_id()
            except RuntimeWorkerError:
                wake_workspace_id = ""
            self.wake_source = wake_source or self._optional_wake_source(
                wake_workspace_id
            )
            start_wake_source = getattr(self.wake_source, "start", None)
            if start_wake_source is not None:
                start_wake_source()
            self.handle = self.process.start(
                self.provider_command, cwd=self.spec["cwd"], env=self.provider_env
            )
            self.supervisor_identity = self.process.capture_identity(os.getpid())
            if not self.supervisor_identity:
                raise ProcessError("runtime worker identity is unavailable")
        except (OSError, ProcessError, RuntimeWorkerError):
            if self.handle is not None:
                self.contain_provider_start_failure(self.process, self.handle)
            source = getattr(self, "wake_source", None)
            if source is not None:
                source.close()
            _atomic_json(self.ready, {"schema_version": 1, "status": "failed"})
            _atomic_json(
                self.exit_path,
                {"schema_version": 1, "status": "start-failed", "exit_code": 127},
            )
            return 127
        # The ready handshake carries only process/supervisor ownership, so
        # every session publishes it before semantic input acknowledgement:
        # a real executor's start acknowledgement can outlast the manager's
        # bounded start budget, and input acceptance stays a separate, later
        # boundary that fails closed through the worker/operation path.
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
                "provider_generation": self.initial_generation,
            },
        )
        start_committed = (
            self._await_parent_start_committed()
            if expected_reviewer
            else self._await_own_start_committed()
        )
        if not start_committed:
            self.contain_provider_start_failure(self.process, self.handle)
            self.wake_source.close()
            _atomic_json(
                self.exit_path,
                {
                    "schema_version": 1,
                    "status": "parent-start-unconfirmed",
                    "exit_code": 2,
                },
            )
            return 2
        if not self._submit_initial_input(initial_start_observation_limit):
            self.wake_source.close()
            return 2
        self.checkpoint_probe = self.checkpoint_probe or CmuxAdapter().resume_checkpoint
        self.checkpoint = ""
        self.next_checkpoint_probe = 0.0
        self.next_full_reconcile = 0.0
        self.next_transport_confirmation = float("inf")
        self.next_cross_session_reconcile = float("inf")
        self.next_provider_exit_probe = 0.0
        self.next_wake_retry = float("inf")
        self.wake_retry_attempts = 0
        self.wake_source_disabled = False
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
        self.restart_attention_recovery_done = False
        self.resumed_wake_identities: set[str] = set()
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
        self.fix_output_digest = ""
        self.fix_output_stable_reads = 0
        self.fix_submit_attempt_digest = ""
        self.fix_transport_complete = (
            self._pipeline_name != "engineering/fix" or self.is_custom_pipeline
        )
        self.custom_transport_complete = not self.is_custom_pipeline
        self.custom_callback_digest = ""
        self.custom_callback_stable_reads = 0
        self.custom_result_digest = ""
        self.custom_result_stable_reads = 0
        self.custom_output_digest = ""
        self.custom_output_stable_reads = 0
        self.custom_submit_attempt_digest = ""
        self.pipeline_step_artifact_owner = None
        self.exit_code = 0
        self.provider_exited = False
        self.exit_containment_failed = False
        return self.run_provider_loop()
