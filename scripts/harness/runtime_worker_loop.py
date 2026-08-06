"""Provider-loop ownership for the short-lived runtime worker."""

from __future__ import annotations

from .runtime_worker import *  # noqa: F401,F403
from .runtime_worker import _atomic_json


class RuntimeWorkerLoopMixin:
    def inspect_transport(self) -> None:
        self.inspect_control()
        if self.spec["callback_mode"] == "task-summary":
            self.recover_task_summary_attention()
            self.drive_fix_transport()
            self.drive_custom_transport()
            self.inspect_task_summary()
        elif self.spec["callback_mode"] in {"research-fetch", "research-synth"}:
            self.inspect_research()
        else:
            self.inspect_callback()

    def tick_observers(self) -> None:
        wall_clock = getattr(self, "wall_clock", time.time)
        if enforce_callback_deadline(
            self.store,
            self.spec["owner_id"],
            self.spec["operation_id"],
            callback_handled=self.callback_handled,
            now=wall_clock(),
        ):
            _atomic_json(
                self.spec_path.parent / "callback-timeout.json",
                {
                    "schema_version": 1,
                    "operation_id": self.spec["operation_id"],
                    "run_id": self.spec["run_id"],
                    "status": "attention-required",
                },
            )
        monotonic_clock = getattr(self, "monotonic_clock", time.monotonic)
        now = monotonic_clock()
        if now >= self.next_liveness_probe:
            self.next_liveness_probe = now + self.liveness_policy.probe_seconds
            self.inspect_liveness()
        if now >= self.next_prompt_probe:
            self.next_prompt_probe = now + 0.2
            self.inspect_prompt()
        if not self.checkpoint and now >= self.next_checkpoint_probe:
            self.next_checkpoint_probe = now + 0.5
            self.capture_checkpoint()

    def capture_checkpoint(self) -> None:
        try:
            self.checkpoint = self.checkpoint_probe(
                str(self.spec["surface_id"]), str(self.spec["runtime"])
            )
        except Exception:
            self.checkpoint = ""
        if self.checkpoint:
            _atomic_json(
                self.spec_path.parent / "checkpoint.json",
                {
                    "schema_version": 1,
                    "operation_id": self.spec["operation_id"],
                    "run_id": self.spec["run_id"],
                    "runtime": self.spec["runtime"],
                    "checkpoint": self.checkpoint,
                },
            )

    def observe_provider_exit(self) -> bool:
        """Observe and contain provider exit; return false when polling must defer."""
        if self.provider_exited:
            return True
        try:
            pending = os.waitid(
                os.P_PID,
                self.handle.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError:
            self.provider_exited = True
            self.exit_code = 0
            self.record_provider_exit(self.exit_code)
            self.mark_attention(AttentionReason.ATTENTION_REQUIRED)
            return True
        except OSError:
            return True
        if pending is None:
            return True
        try:
            self.process.signal_owned_child_group(
                self.handle.process_group,
                self.handle.process_identity,
                signal.SIGKILL,
            )
        except ProcessError:
            if not self.exit_containment_failed:
                self.exit_containment_failed = True
                self.mark_attention(AttentionReason.ATTENTION_REQUIRED)
            return False
        waited, status = os.waitpid(self.handle.pid, os.WNOHANG)
        if waited != self.handle.pid:
            return False
        self.exit_code = os.waitstatus_to_exitcode(status)
        self.provider_exited = True
        self.record_provider_exit(self.exit_code)
        return True

    def mark_attention(self, reason: AttentionReason) -> None:
        try:
            self.store.transition(
                self.spec["owner_id"],
                self.spec["operation_id"],
                "attention-required",
                reason=reason,
            )
        except Exception:
            pass

    def mark_failed_research_runtime(self) -> None:
        if (
            not self.provider_exited
            or self.exit_code == 0
            or self.callback_handled
            or self.spec["callback_mode"] not in {"research-fetch", "research-synth"}
        ):
            return
        try:
            current = self.store.read(self.spec["owner_id"], self.spec["operation_id"])
            if current.state not in TERMINAL and current.state != "attention-required":
                reason = (
                    AttentionReason.RUNTIME_UNAVAILABLE
                    if self.exit_code == 127
                    else AttentionReason.ATTENTION_REQUIRED
                )
                self.store.transition(
                    self.spec["owner_id"],
                    self.spec["operation_id"],
                    "attention-required",
                    reason=reason,
                )
        except Exception:
            pass

    def needs_provider_restart(self) -> bool:
        pending_fix = (
            self._pipeline_name == "engineering/fix" and not self.fix_transport_complete
        )
        pending_custom = self.is_custom_pipeline and not self.custom_transport_complete
        return (
            self.provider_exited
            and (pending_fix or pending_custom)
            and not self.callback_handled
        )

    def restart_provider(self) -> None:
        recovery_kind = "custom" if self.is_custom_pipeline else "fix"
        recovery_root = self.spec_path.parent / f"pipeline-{recovery_kind}"
        parent = self.store.read(self.spec["owner_id"], self.spec["operation_id"])
        if parent.state in TERMINAL or parent.state == "attention-required":
            return
        supervisor = OperationSupervisor(
            self.store, self.spec["owner_id"], self.spec["operation_id"]
        )
        old_handle = self.handle
        try:
            budgeted = supervisor.consume_model_restart(explicitly_permitted=True)
        except SupervisorError:
            self.write_immutable_json(
                recovery_root / "provider-restart-exhausted.json",
                {
                    "schema_version": 1,
                    "operation_id": self.spec["operation_id"],
                    "model_restarts": parent.model_restarts,
                    "model_restart_limit": parent.model_restart_limit,
                    "status": "retry-exhausted",
                },
            )
            self.summary_attention(
                f"pipeline-{recovery_kind}-provider-restart-exhausted",
                AttentionReason.RETRY_EXHAUSTED,
            )
            return
        self.start_replacement_provider(
            recovery_kind, recovery_root, supervisor, budgeted, old_handle
        )

    def start_replacement_provider(
        self,
        recovery_kind: str,
        recovery_root: Path,
        supervisor: OperationSupervisor,
        budgeted: object,
        old_handle: ProcessHandle,
    ) -> None:
        restarted: ProcessHandle | None = None
        try:
            resume_command = provider_resume_argv(
                self.provider_command, str(self.spec["runtime"]), self.checkpoint
            )
            restarted = self.process.start(
                resume_command, cwd=self.spec["cwd"], env=self.provider_env
            )
            previous = budgeted.resources
            supervisor.bind_resources(
                OwnedResources(
                    surface_id=previous.surface_id or self.spec["surface_id"],
                    process_group=restarted.process_group,
                    supervisor_pid=previous.supervisor_pid or os.getpid(),
                    process_identity=restarted.process_identity,
                    supervisor_identity=(
                        previous.supervisor_identity or self.supervisor_identity
                    ),
                )
            )
            self.handle = restarted
            self.provider_exited = False
            self.exit_code = 0
            self.exit_containment_failed = False
            self.write_ready()
            command_sha256 = hashlib.sha256(
                json.dumps(resume_command, separators=(",", ":")).encode()
            ).hexdigest()
            environment_sha256 = hashlib.sha256(
                json.dumps(
                    sorted(self.provider_env.items()), separators=(",", ":")
                ).encode()
            ).hexdigest()
            self.write_immutable_json(
                recovery_root / f"provider-restart-{budgeted.model_restarts}.json",
                {
                    "schema_version": 1,
                    "operation_id": self.spec["operation_id"],
                    "model_restarts": budgeted.model_restarts,
                    "checkpoint": self.checkpoint,
                    "old_process_group": old_handle.process_group,
                    "old_process_identity": old_handle.process_identity,
                    "new_process_group": restarted.process_group,
                    "new_process_identity": restarted.process_identity,
                    "provider_argv_sha256": command_sha256,
                    "provider_environment_sha256": environment_sha256,
                    "status": "restarted",
                },
            )
        except (
            ContractError,
            HarnessContractError,
            OSError,
            ProcessError,
            RuntimeWorkerError,
            StoreError,
            SupervisorError,
        ):
            if restarted is not None:
                self.contain_provider_start_failure(self.process, restarted)
            self.summary_attention(
                f"pipeline-{recovery_kind}-provider-restart-failed",
                AttentionReason.ATTENTION_REQUIRED,
            )

    def write_ready(self) -> None:
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

    def provider_exit_is_final(self) -> bool:
        try:
            operation = self.store.read(
                self.spec["owner_id"], self.spec["operation_id"]
            )
            operation_state = operation.state
            operation_profile = operation.spec.route.profile
            callback_deadline_at = operation.deadline_at
        except Exception:
            operation_state = ""
            operation_profile = ""
            callback_deadline_at = 0.0
        return provider_exit_is_final(
            provider_exited=self.provider_exited,
            callback_mode=self.spec["callback_mode"],
            callback_handled=self.callback_handled,
            operation_state=operation_state,
            operation_profile=operation_profile,
            callback_deadline_at=callback_deadline_at,
        )

    def drain_callbacks(self) -> None:
        for _ in range(3):
            self.inspect_transport()
            if self.callback_handled:
                break
            getattr(self, "sleeper", time.sleep)(max(0.02, self.poll_seconds))

    def poll_once(self) -> bool:
        """Run one production transport/observer/exit-observation iteration."""

        self.inspect_transport()
        self.tick_observers()
        return self.observe_provider_exit()

    def settle_exit_once(self) -> bool:
        """Classify one observed exit and decide restart versus finality."""

        self.mark_failed_research_runtime()
        if self.needs_provider_restart():
            self.restart_provider()
        return self.provider_exit_is_final()

    def run_provider_loop(self) -> int:
        while True:
            if not self.poll_once():
                self.sleeper(max(0.02, self.poll_seconds))
                continue
            if self.settle_exit_once():
                break
            self.sleeper(max(0.02, self.poll_seconds))
        self.drain_callbacks()
        _atomic_json(
            self.exit_path,
            {
                "schema_version": 1,
                "status": "exited",
                "exit_code": self.exit_code,
            },
        )
        return self.exit_code
