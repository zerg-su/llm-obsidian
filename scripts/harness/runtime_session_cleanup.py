"""Status, exit, and exact-resource cleanup for provider sessions."""

from __future__ import annotations

from time import time

from .contracts import (
    AttentionReason,
    EffectOutcome,
    OperationRecord,
    OwnedResources,
)
from .runtime_session_contracts import (
    RuntimeSessionError,
    RuntimeSessionResult,
)
from .state_machine import TERMINAL
from .supervisor import OperationSupervisor


class RuntimeSessionCleanupMixin:
    """Own observation, exit requests, and exact cleanup effects."""


    def _owner_for_operation(self, operation_id: str) -> str:
        matches: list[str] = []
        owners = self.store.root / "owners"
        if owners.is_dir():
            for directory in owners.iterdir():
                if (
                    directory.is_dir()
                    and (directory / "operations" / f"{operation_id}.json").is_file()
                ):
                    matches.append(directory.name)
        if len(matches) != 1:
            raise RuntimeSessionError("callback operation owner is ambiguous or unknown")
        return matches[0]

    def status(self, owner_id: str, operation_id: str) -> RuntimeSessionResult:
        """Read exact resource liveness without mutating durable state."""

        record = self.store.read(owner_id, operation_id)
        if not record.resources.surface_id or record.resources.process_group <= 1:
            action = "terminal" if record.state in TERMINAL else "attention-required"
            return self._result(record, action)
        process_status = self.process.process_status(
            record.resources.process_group,
            record.resources.process_identity,
        )
        supervisor_status = self._supervisor_status(record)
        try:
            surface_status = self.cmux.status(record.resources.surface_id)
        except Exception:
            surface_status = "unknown"
        checkpoint = ""
        if process_status == "alive" and surface_status == "alive":
            try:
                checkpoint = self.cmux.resume_checkpoint(
                    record.resources.surface_id, record.spec.route.runtime
                )
            except Exception:
                checkpoint = ""
        action = (
            "attention-required"
            if (
                "unknown" in {process_status, surface_status, supervisor_status}
                or (process_status == "alive" and supervisor_status == "dead")
            )
            else "observed"
        )
        return self._result(
            record,
            action,
            checkpoint=checkpoint,
            process_status=process_status,
            surface_status=surface_status,
        )

    def request_exit(
        self, owner_id: str, operation_id: str
    ) -> RuntimeSessionResult:
        """Request exact provider PGID exit; never close the surface here."""

        record = self.store.read(owner_id, operation_id)
        if record.state in TERMINAL:
            return self._result(record, "terminal")
        if record.state == "exiting":
            return self._result(record, "exit-requested")
        if record.resources.process_group <= 1:
            current = self._mark_attention(
                record, AttentionReason.PROCESS_ORPHANED
            )
            return self._result(current, "attention-required")
        process_status, supervisor_status = (
            self._cleanup_ownership_statuses(record)
        )
        if "unknown" in {process_status, supervisor_status} or (
            process_status == "alive" and supervisor_status in {"dead", "unknown"}
        ):
            current = self._mark_attention(
                record, AttentionReason.ATTENTION_REQUIRED
            )
            return self._result(current, "attention-required")
        supervisor = OperationSupervisor(self.store, owner_id, operation_id)
        if record.state == "attention-required":
            supervisor.transition("cancelling")
        elif record.state != "finalizing":
            supervisor.transition("finalizing")

        def exit_provider(_record: OperationRecord) -> None:
            if process_status == "alive":
                self.process.request_guardian_signal(
                    self._control_path(record),
                    action="request-exit",
                    operation_id=record.spec.operation_id,
                    run_id=record.run_id,
                    process_group=record.resources.process_group,
                    process_identity=record.resources.process_identity,
                    supervisor_pid=record.resources.supervisor_pid,
                    supervisor_identity=record.resources.supervisor_identity,
                )

        supervisor.effect("request-exit", exit_provider)
        current = supervisor.transition("exiting")
        self._notify(current.spec.owner_id, current.spec.operation_id)
        return self._result(
            current,
            "exit-requested",
            process_status=process_status,
            surface_status="alive",
        )

    def cleanup(self, owner_id: str, operation_id: str) -> RuntimeSessionResult:
        """Finalize only after provider exit, then close the exact owned surface."""

        record = self.store.read(owner_id, operation_id)
        if record.state in TERMINAL:
            return self._result(record, "terminal")
        if record.state == "attention-required":
            return self._result(record, "attention-required")
        if record.state != "exiting":
            raise RuntimeSessionError("cleanup requires an exiting operation")
        resources = record.resources
        metadata = self._metadata(record)
        workspace_placement = metadata.get("placement") == "workspace"
        workspace_id = str(metadata.get("workspace_id") or "")
        window_id = str(metadata.get("window_id") or "")
        process_status, supervisor_status = (
            self._cleanup_ownership_statuses(record)
        )
        try:
            surface_status = (
                self.cmux.status(resources.surface_id)
                if resources.surface_id
                else "missing"
            )
        except Exception:
            surface_status = "unknown"
        workspace_status = "missing"
        if workspace_placement:
            try:
                workspace_status = self.cmux.workspace_status(
                    workspace_id, window_id
                )
            except Exception:
                workspace_status = "unknown"
        if (
            surface_status == "unknown"
            and process_status == "dead"
            and supervisor_status == "dead"
            and resources.surface_id
        ):
            try:
                surface_status = self.cmux.status(resources.surface_id)
            except Exception:
                surface_status = "unknown"
        if "unknown" in {
            process_status,
            supervisor_status,
            surface_status,
            workspace_status,
        }:
            return self._result(
                record,
                "wait-for-ownership",
                process_status=process_status,
                surface_status=surface_status,
            )
        if process_status == "alive":
            if supervisor_status == "dead":
                current = self._mark_attention(
                    record, AttentionReason.CLEANUP_INCOMPLETE
                )
                return self._result(
                    current,
                    "attention-required",
                    process_status=process_status,
                    surface_status=surface_status,
                )
            if surface_status == "missing":
                self.process.request_guardian_signal(
                    self._control_path(record),
                    action="terminate",
                    operation_id=record.spec.operation_id,
                    run_id=record.run_id,
                    process_group=resources.process_group,
                    process_identity=resources.process_identity,
                    supervisor_pid=resources.supervisor_pid,
                    supervisor_identity=resources.supervisor_identity,
                )
                return self._result(
                    record,
                    "terminate-orphan",
                    process_status=process_status,
                    surface_status=surface_status,
                )
            if (
                record.spec.kind in {"research-fetch", "research-synth"}
                and record.spec.route.profile == "research-safe"
                and record.spec.verification_profile
                == "research-cited-artifact"
                and record.accepted_callback_id
                and record.accepted_callback_kind == "research"
                and record.effect_id == "request-exit"
                and record.effect_outcome == EffectOutcome.SUCCEEDED
                and record.deadline_at
                and time() >= record.deadline_at
            ):
                self.process.request_guardian_signal(
                    self._control_path(record),
                    action="terminate",
                    operation_id=record.spec.operation_id,
                    run_id=record.run_id,
                    process_group=resources.process_group,
                    process_identity=resources.process_identity,
                    supervisor_pid=resources.supervisor_pid,
                    supervisor_identity=resources.supervisor_identity,
                )
            return self._result(
                record,
                "wait-for-exit",
                process_status=process_status,
                surface_status=surface_status,
            )
        if supervisor_status == "alive":
            return self._result(
                record,
                "wait-for-supervisor",
                process_status=process_status,
                surface_status=surface_status,
            )
        keep_open_alive = (
            workspace_status == "alive"
            if workspace_placement
            else surface_status == "alive"
        )
        if record.spec.keep_open and keep_open_alive:
            return self._result(
                record,
                "keep-open",
                process_status=process_status,
                surface_status=surface_status,
            )
        supervisor = OperationSupervisor(self.store, owner_id, operation_id)
        if workspace_placement:
            if workspace_status == "alive":
                supervisor.effect(
                    "close-workspace",
                    lambda _record: self.cmux.close_workspace_exact(
                        workspace_id,
                        window_id,
                    ),
                )
                try:
                    workspace_status = self.cmux.workspace_status(
                        workspace_id, window_id
                    )
                except Exception:
                    workspace_status = "unknown"
                if workspace_status != "missing":
                    current = self._mark_attention(
                        supervisor.read(), AttentionReason.CLEANUP_INCOMPLETE
                    )
                    return self._result(
                        current,
                        "attention-required",
                        process_status="dead",
                        surface_status=surface_status,
                    )
        elif surface_status == "alive":
            supervisor.effect(
                "close-surface",
                lambda _record: self.cmux.close_exact(
                    resources.surface_id
                ),
            )
        supervisor.bind_resources(OwnedResources())
        current = supervisor.transition("complete")
        self._notify(current.spec.owner_id, current.spec.operation_id)
        return self._result(
            current,
            "cleaned",
            process_status="dead",
            surface_status="missing",
        )
