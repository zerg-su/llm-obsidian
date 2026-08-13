"""Provider-loop ownership for the short-lived runtime worker."""

from __future__ import annotations

from .runtime_worker import *  # noqa: F401,F403
from .runtime_worker import _atomic_json
from .runtime_callback_io import _callback_target
from .cmux_wake_source import WakeObservation


FULL_TRANSPORT_FALLBACK_SECONDS = 30.0
CROSS_SESSION_TRANSPORT_POLL_SECONDS = 1.0
MAX_WAKE_RETRIES = 5
TRANSPORT_STABILITY_FIELDS = (
    "stable_reads",
    "review_input_stable_reads",
    "summary_stable_reads",
    "callback_recovery_input_reads",
    "callback_recovery_reads",
    "fix_callback_stable_reads",
    "fix_result_stable_reads",
    "fix_output_stable_reads",
    "custom_callback_stable_reads",
    "custom_result_stable_reads",
    "custom_output_stable_reads",
)


class RuntimeWorkerLoopMixin:
    def transport_confirmation_pending(self) -> bool:
        """Preserve every existing two-read guard across event wakeups."""

        return any(getattr(self, field, 0) == 1 for field in TRANSPORT_STABILITY_FIELDS)

    def callback_deadline_monotonic(self, now: float) -> float:
        """Project the durable wall-clock callback deadline onto this wait."""
        try:
            record = self.store.read(
                self.spec["owner_id"], self.spec["operation_id"]
            )
            if self.callback_handled or not record.deadline_at:
                return float("inf")
            remaining = record.deadline_at - self.wall_clock()
            return now + max(max(0.02, self.poll_seconds), remaining)
        except Exception:
            # The ordinary fallback reconciliation remains authoritative.
            return float("inf")

    def _next_light_deadline(self, now: float) -> float:
        deadlines = [
            self.next_prompt_probe,
            self.next_liveness_probe,
            self.next_provider_exit_probe,
            self.callback_deadline_monotonic(now),
        ]
        if not self.checkpoint:
            deadlines.append(self.next_checkpoint_probe)
        return min(deadlines)

    def _next_wake_deadline(self, now: float) -> float:
        return min(
            self.next_full_reconcile,
            self.next_transport_confirmation,
            getattr(self, "next_cross_session_reconcile", float("inf")),
            self._next_light_deadline(now),
        )

    def cross_session_transport_pending(self) -> bool:
        """Keep parent-owned child handoffs prompt after its provider exits."""

        spec = getattr(self, "spec", {})
        return bool(
            isinstance(spec, dict)
            and spec.get("callback_mode") == "task-summary"
            and self.provider_exited
            and not self.callback_handled
        )

    def refresh_cross_session_reconcile(self, now: float) -> None:
        if self.cross_session_transport_pending():
            if getattr(
                self, "next_cross_session_reconcile", float("inf")
            ) == float("inf"):
                self.next_cross_session_reconcile = (
                    now + CROSS_SESSION_TRANSPORT_POLL_SECONDS
                )
        else:
            self.next_cross_session_reconcile = float("inf")

    def transport_snapshot(self) -> dict[str, object]:
        """Return bounded durable identities, never provider content."""
        snapshot: dict[str, object] = {}
        try:
            parent = self.store.read(
                self.spec["owner_id"], self.spec["operation_id"]
            )
            snapshot["parent"] = {
                "operation_id": self.spec["operation_id"],
                "run_id": parent.run_id,
                "revision": parent.revision,
                "state": parent.state,
                "accepted_callback_id": parent.accepted_callback_id,
                "accepted_callback_kind": parent.accepted_callback_kind,
                "accepted_callback_sha256": parent.accepted_callback_sha256,
            }
        except Exception:
            snapshot["parent"] = {"operation_id": self.spec["operation_id"]}
        try:
            generation, operation_id, run_id, _pointer = _callback_target(self.spec)
            child = self.store.read(self.spec["owner_id"], operation_id)
            snapshot["callback"] = {
                "generation": generation,
                "operation_id": operation_id,
                "run_id": run_id,
                "revision": child.revision,
                "state": child.state,
                "accepted_callback_id": child.accepted_callback_id,
                "accepted_callback_kind": child.accepted_callback_kind,
                "accepted_callback_sha256": child.accepted_callback_sha256,
            }
        except Exception:
            snapshot["callback"] = {}
        return snapshot

    def _wake_generation(self) -> int:
        try:
            return _callback_target(self.spec)[0]
        except Exception:
            return int(getattr(self, "initial_generation", 1))

    def record_transport_wake(
        self,
        observation: WakeObservation,
        before: object,
        after: object,
    ) -> None:
        """Publish one crash-safe, content-free reconciliation receipt."""

        outcome = "progressed" if before != after else "no-change"
        payload = {
            "schema_version": 1,
            "owner_id": self.spec["owner_id"],
            "operation_id": self.spec["operation_id"],
            "run_id": self.spec["run_id"],
            "generation": self._wake_generation(),
            "source": observation.source,
            "event_name": observation.event_name,
            "sequence": observation.sequence,
            "observed_at": observation.observed_at,
            "recorded_at": self.wall_clock(),
            "outcome": outcome,
            "before": before,
            "after": after,
        }
        _atomic_json(self.spec_path.parent / "wake-observation.json", payload)
        if outcome == "progressed":
            _atomic_json(self.spec_path.parent / "wake-progress.json", payload)

    def record_wake_source_state(self, observation: WakeObservation) -> None:
        if getattr(self, "_last_wake_source_state", "") == observation.source:
            return
        self._last_wake_source_state = observation.source
        _atomic_json(
            self.spec_path.parent / "wake-source-state.json",
            {
                "schema_version": 1,
                "owner_id": self.spec["owner_id"],
                "operation_id": self.spec["operation_id"],
                "run_id": self.spec["run_id"],
                "generation": self._wake_generation(),
                "source": observation.source,
                "observed_at": observation.observed_at,
            },
        )

    def _full_reconcile(self, observation: WakeObservation, now: float) -> None:
        before = self.transport_snapshot()
        self.inspect_transport()
        after = self.transport_snapshot()
        self.record_transport_wake(observation, before, after)
        self.next_full_reconcile = now + FULL_TRANSPORT_FALLBACK_SECONDS
        self.next_transport_confirmation = (
            round(now + max(0.02, self.poll_seconds), 9)
            if self.transport_confirmation_pending()
            else float("inf")
        )
        self.next_cross_session_reconcile = (
            now + CROSS_SESSION_TRANSPORT_POLL_SECONDS
            if self.cross_session_transport_pending()
            else float("inf")
        )

    def inspect_transport(self) -> None:
        inspect_control = getattr(self, "inspect_control", None)
        if inspect_control is not None:
            inspect_control()
        if self.spec["callback_mode"] == "task-summary":
            self.recover_task_summary_attention()
            self.drive_fix_transport()
            self.drive_custom_transport()
            self.inspect_task_summary()
        elif self.spec["callback_mode"] in {"research-fetch", "research-synth"}:
            self.inspect_research()
        else:
            self.inspect_callback()
            inspect_rejections = getattr(
                self, "inspect_submit_rejections", None
            )
            if inspect_rejections is not None:
                inspect_rejections()

    def tick_observers(self) -> None:
        # Guardian control is a narrow identity-bound duty.  It must remain
        # prompt even when the optional event source is unavailable, without
        # turning the rest of transport inspection back into a fast poll.
        inspect_control = getattr(self, "inspect_control", None)
        if inspect_control is not None:
            inspect_control()
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

    def mark_failed_artifact_repair_runtime(self) -> None:
        """Terminalize a one-shot artifact worker that returned no result."""

        if (
            not self.provider_exited
            or self.callback_handled
            or self.spec["callback_mode"] != "artifact-repair"
        ):
            return
        try:
            current = self.store.read(
                self.spec["owner_id"], self.spec["operation_id"]
            )
            if current.state not in TERMINAL and current.state != "attention-required":
                self.store.transition(
                    self.spec["owner_id"],
                    self.spec["operation_id"],
                    "attention-required",
                    reason=AttentionReason.CALLBACK_INVALID,
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
        from .dashboard_facade import launch_bound_facade_dashboard

        launch_bound_facade_dashboard(
            worktree=self.spec["cwd"],
            facade="recovery",
            root_operation_id=str(self.spec["owner_id"]),
        )
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
                self.provider_command,
                str(self.spec["runtime"]),
                self.checkpoint,
                deferred_initial_input=isinstance(
                    self.spec.get("initial_input_pointer"), Path
                ),
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

        if not hasattr(self, "wake_source"):
            raise RuntimeWorkerError("runtime worker wake source is unavailable")

        now = self.monotonic_clock()
        self.refresh_cross_session_reconcile(now)
        observation: WakeObservation | None = None
        if now >= self.next_transport_confirmation:
            observation = WakeObservation("stability-confirmation", "", 0, now)
        elif now >= self.next_cross_session_reconcile:
            observation = WakeObservation("fallback-poll", "", 0, now)
        elif now >= self.next_full_reconcile:
            observation = WakeObservation("fallback-poll", "", 0, now)
        else:
            generation = self._wake_generation()
            if generation != getattr(self, "_last_wake_generation", generation):
                refresh = getattr(self.wake_source, "refresh_generation", None)
                if refresh is not None:
                    refresh(generation)
                self._last_wake_generation = generation
            timeout = max(0.0, self._next_wake_deadline(now) - now)
            if self.wake_source_disabled:
                self.sleeper(timeout)
            elif (
                self.next_wake_retry != float("inf")
                and now < self.next_wake_retry
            ):
                self.sleeper(min(timeout, self.next_wake_retry - now))
            else:
                observation = self.wake_source.wait(timeout)
            now = self.monotonic_clock()
            if observation is None and now >= self.next_transport_confirmation:
                observation = WakeObservation(
                    "stability-confirmation", "", 0, now
                )
            elif observation is None and now >= self.next_cross_session_reconcile:
                observation = WakeObservation("fallback-poll", "", 0, now)
            elif observation is None and now >= self.next_full_reconcile:
                observation = WakeObservation("fallback-poll", "", 0, now)

        # The loop accepts only the adapter's closed hints plus its two
        # code-owned full-reconcile reasons.
        if observation is not None and observation.source in {
            "cmux-event",
            "reconnect",
            "cursor-gap",
            "degraded",
            "fallback-poll",
            "stability-confirmation",
        }:
            if observation.source == "degraded":
                first_degradation = getattr(
                    self, "_last_wake_source_state", ""
                ) != "degraded"
                self.record_wake_source_state(observation)
                if self.wake_retry_attempts < MAX_WAKE_RETRIES:
                    delay = min(
                        FULL_TRANSPORT_FALLBACK_SECONDS,
                        max(1.0, self.poll_seconds)
                        * (2 ** self.wake_retry_attempts),
                    )
                    self.next_wake_retry = now + delay
                else:
                    self.wake_source_disabled = True
                observation = (
                    WakeObservation(
                        "fallback-poll", "", 0, observation.observed_at
                    )
                    if first_degradation
                    else None
                )
            elif observation.source in {"cmux-event", "reconnect", "cursor-gap"}:
                self._last_wake_source_state = ""
                self.wake_retry_attempts = 0
                self.wake_source_disabled = False
            if observation is not None:
                self._full_reconcile(observation, now)
        elif observation is not None and observation.source == "unavailable":
            self.record_wake_source_state(observation)
            if self.wake_retry_attempts < MAX_WAKE_RETRIES:
                self.next_wake_retry = now + min(
                    FULL_TRANSPORT_FALLBACK_SECONDS,
                    max(1.0, self.poll_seconds)
                    * (2 ** self.wake_retry_attempts),
                )
            else:
                self.wake_source_disabled = True

        if (
            self.next_wake_retry != float("inf")
            and now >= self.next_wake_retry
            and hasattr(self.wake_source, "retry")
        ):
            started = self.wake_source.retry()
            self.wake_retry_attempts += 1
            self.next_wake_retry = float("inf")
            if not started and self.wake_retry_attempts >= MAX_WAKE_RETRIES:
                self.wake_source_disabled = True
        self.tick_observers()
        if now >= self.next_provider_exit_probe:
            self.next_provider_exit_probe = now + max(0.02, self.poll_seconds)
            return self.observe_provider_exit()
        return True

    def settle_exit_once(self) -> bool:
        """Classify one observed exit and decide restart versus finality."""

        self.mark_failed_research_runtime()
        self.mark_failed_artifact_repair_runtime()
        self.mark_failed_task_summary_correction_runtime()
        self.mark_failed_pipeline_step_correction_runtime()
        if self.needs_provider_restart():
            self.restart_provider()
        return self.provider_exit_is_final()

    def run_provider_loop(self) -> int:
        try:
            while True:
                if not self.poll_once():
                    continue
                if self.settle_exit_once():
                    break
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
        finally:
            source = getattr(self, "wake_source", None)
            if source is not None:
                source.close()
