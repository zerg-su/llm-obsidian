"""Status, exit, and exact-resource cleanup for provider sessions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from time import time

from review_contract import REVIEW_PARENT_KINDS

from .contracts import (
    AttentionReason,
    EffectOutcome,
    OperationRecord,
    OwnedResources,
    to_dict,
)
from .runtime_session_contracts import (
    RuntimeSessionError,
    RuntimeSessionResult,
)
from .runtime_session_liveness import (
    ResourceClosureLedger,
    ResourceIdentity,
    ResourceObservation,
)
from .runtime_provider_events import (
    RuntimeProviderEventError,
    RuntimeProviderEventStream,
)
from .state_machine import TERMINAL
from .supervisor import OperationSupervisor


class RuntimeSessionCleanupMixin:
    """Own observation, exit requests, and exact cleanup effects."""

    _SUPERSEDED_REVIEW_RECEIPT_KEYS = frozenset(
        {
            "schema_version",
            "status",
            "superseded_owner_id",
            "superseded_review_operation_id",
            "superseded_operation_id",
            "superseded_run_id",
            "superseded_record_sha256",
            "replacement_owner_id",
            "replacement_review_operation_id",
            "replacement_operation_id",
            "replacement_run_id",
            "store_sha256",
            "authorization_pointer",
            "authorization_sha256",
        }
    )

    @staticmethod
    def _bounded_regular_json(path: Path, *, label: str) -> tuple[dict[str, object], bytes]:
        if path.is_symlink() or not path.is_file():
            raise RuntimeSessionError(f"{label} is unavailable")
        raw = path.read_bytes()
        if not raw or len(raw) > 65_536:
            raise RuntimeSessionError(f"{label} must be non-empty and bounded")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeSessionError(f"{label} is invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeSessionError(f"{label} must be an object")
        return value, raw

    def _store_relative_regular_path(self, pointer: str, *, label: str) -> Path:
        if not isinstance(pointer, str) or not pointer:
            raise RuntimeSessionError(f"{label} pointer is invalid")
        relative = Path(pointer)
        candidate_path = self.store.root / relative
        if relative.is_absolute() or candidate_path.is_symlink():
            raise RuntimeSessionError(f"{label} pointer is invalid")
        candidate = candidate_path.resolve()
        try:
            candidate.relative_to(self.store.root)
        except ValueError as exc:
            raise RuntimeSessionError(f"{label} escapes harness state") from exc
        if candidate.is_symlink() or not candidate.is_file():
            raise RuntimeSessionError(f"{label} is unavailable")
        return candidate

    def cleanup_superseded_review(
        self, receipt_path: Path
    ) -> RuntimeSessionResult:
        """Clean one superseded reviewer only with exact durable authority."""

        receipt_path = receipt_path.expanduser().absolute()
        if receipt_path.is_symlink():
            raise RuntimeSessionError("superseded review receipt is unavailable")
        receipt_path = receipt_path.resolve()
        try:
            receipt_path.relative_to(self.store.root)
        except ValueError as exc:
            raise RuntimeSessionError(
                "superseded review receipt escapes harness state"
            ) from exc
        receipt, receipt_raw = self._bounded_regular_json(
            receipt_path, label="superseded review receipt"
        )
        if (
            frozenset(receipt) != self._SUPERSEDED_REVIEW_RECEIPT_KEYS
            or receipt.get("schema_version") != 1
            or receipt.get("status") != "authorized"
            or receipt.get("store_sha256")
            != hashlib.sha256(str(self.store.root).encode()).hexdigest()
        ):
            raise RuntimeSessionError("superseded review receipt is not authorized")

        old_owner = str(receipt["superseded_owner_id"])
        old_review_operation = str(
            receipt["superseded_review_operation_id"]
        )
        old_operation = str(receipt["superseded_operation_id"])
        old_run = str(receipt["superseded_run_id"])
        replacement_owner = str(receipt["replacement_owner_id"])
        replacement_review_operation = str(
            receipt["replacement_review_operation_id"]
        )
        replacement_operation = str(receipt["replacement_operation_id"])
        replacement_run = str(receipt["replacement_run_id"])
        if (
            old_review_operation == replacement_review_operation
            or (old_owner, old_operation, old_run) == (
            replacement_owner,
            replacement_operation,
            replacement_run,
            )
        ):
            raise RuntimeSessionError("review supersession identities must differ")

        authorization_path = self._store_relative_regular_path(
            str(receipt["authorization_pointer"]),
            label="review supersession authorization",
        )
        authorization, authorization_raw = self._bounded_regular_json(
            authorization_path, label="review supersession authorization"
        )
        expected_authorization_keys = {
            "schema_version",
            "operation_id",
            "kind",
            "previous_context_sha256",
            "next_context_sha256",
            "reason",
            "authorization_provenance",
            "verification_operation_id",
            "verification_receipt_sha256",
            "status",
        }
        if (
            hashlib.sha256(authorization_raw).hexdigest()
            != receipt["authorization_sha256"]
            or set(authorization) != expected_authorization_keys
            or authorization.get("schema_version") != 1
            or authorization.get("status") != "authorized"
            or authorization.get("operation_id") != old_review_operation
            or authorization.get("kind") not in {"scope", "context"}
            or authorization.get("previous_context_sha256")
            == authorization.get("next_context_sha256")
            or not all(
                isinstance(authorization.get(key), str)
                and bool(str(authorization[key]).strip())
                for key in (
                    "reason",
                    "authorization_provenance",
                    "verification_operation_id",
                )
            )
            or authorization.get("authorization_provenance")
            not in {"coordinator-approved", "pipeline-verification"}
            or not all(
                isinstance(authorization.get(key), str)
                and len(str(authorization[key])) == 64
                for key in (
                    "previous_context_sha256",
                    "next_context_sha256",
                    "verification_receipt_sha256",
                )
            )
        ):
            raise RuntimeSessionError("review supersession authorization mismatch")

        old_record = self.store.read(old_owner, old_operation)
        replacement = self.store.read(replacement_owner, replacement_operation)
        for label, record, run_id in (
            ("superseded", old_record, old_run),
            ("replacement", replacement, replacement_run),
        ):
            if (
                record.run_id != run_id
                or record.spec.kind not in REVIEW_PARENT_KINDS
                or record.spec.route.profile != "reviewer-callback"
            ):
                raise RuntimeSessionError(f"{label} review identity mismatch")
        receipt_sha256 = hashlib.sha256(receipt_raw).hexdigest()
        result_path = receipt_path.with_name(f"{receipt_path.stem}-result.json")
        result_status = ""
        if result_path.exists():
            result, _raw = self._bounded_regular_json(
                result_path, label="superseded review cleanup result"
            )
            expected_result = {
                "schema_version": 1,
                "receipt_sha256": receipt_sha256,
                "superseded_operation_id": old_operation,
                "superseded_run_id": old_run,
            }
            result_status = str(result.get("status") or "")
            if (
                result_status not in {"started", "cleaned"}
                or {key: result.get(key) for key in expected_result}
                != expected_result
                or set(result) != {*expected_result, "status"}
            ):
                raise RuntimeSessionError("superseded review cleanup result mismatch")
            if (
                result_status == "cleaned"
                and (
                    old_record.state not in TERMINAL
                    or old_record.resources != OwnedResources()
                )
            ):
                raise RuntimeSessionError("superseded review cleanup result is stale")
            if result_status == "cleaned":
                return self._result(old_record, "terminal")

        if not result_status and replacement.state in TERMINAL:
            raise RuntimeSessionError("replacement review is no longer active")

        if result_status != "started":
            canonical_old = json.dumps(
                to_dict(old_record), sort_keys=True, separators=(",", ":")
            ).encode()
            if (
                hashlib.sha256(canonical_old).hexdigest()
                != receipt["superseded_record_sha256"]
            ):
                raise RuntimeSessionError(
                    "superseded review record changed after authorization"
                )
            self._write_json(
                result_path,
                {
                    "schema_version": 1,
                    "status": "started",
                    "receipt_sha256": receipt_sha256,
                    "superseded_operation_id": old_operation,
                    "superseded_run_id": old_run,
                },
            )

        exit_result = self.request_exit(old_owner, old_operation)
        if exit_result.action not in {"exit-requested", "terminal"}:
            return exit_result
        cleanup_result = self.cleanup(old_owner, old_operation)
        if (
            cleanup_result.record.state in TERMINAL
            and cleanup_result.record.resources == OwnedResources()
        ):
            self._write_json(
                result_path,
                {
                    "schema_version": 1,
                    "status": "cleaned",
                    "receipt_sha256": receipt_sha256,
                    "superseded_operation_id": old_operation,
                    "superseded_run_id": old_run,
                },
            )
        return cleanup_result


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

    def _record_resource_closed(
        self,
        record: OperationRecord,
        metadata: dict[str, object],
        *,
        process_status: str,
        supervisor_status: str,
        surface_status: str,
        workspace_status: str,
    ) -> str:
        """Publish the exact close receipt before clearing durable ownership."""

        target_path = self._callback_target_path(record)
        if not target_path.is_file():
            # Historical records created before provider-event generations remain
            # cleanup-compatible but cannot manufacture a typed event identity.
            return "legacy"
        target = self._callback_target(record)
        resources = record.resources
        workspace_id = str(metadata.get("workspace_id") or "")
        if not all(
            (
                resources.process_identity,
                resources.supervisor_identity,
                resources.surface_id,
                workspace_id,
            )
        ):
            # Compatibility records may prove disappearance without carrying
            # the complete new provider/resource identity. Cleanup remains
            # possible, but no typed close receipt is fabricated for them.
            return "legacy"
        identity = ResourceIdentity(
            owner_id=record.spec.owner_id,
            operation_id=record.spec.operation_id,
            run_id=record.run_id,
            generation=int(target["generation"]),
            provider_session_id=record.run_id,
            process_identity=resources.process_identity,
            supervisor_identity=resources.supervisor_identity,
            source_id=f"process:{resources.process_identity}",
            workspace_id=workspace_id,
            surface_id=resources.surface_id,
        )
        delivery_state = (
            self._state_root(record)
            / "provider-events"
            / f"generation-{int(target['generation'])}"
            / "delivery"
            / "delivery-state.json"
        )
        if not delivery_state.is_file() or delivery_state.is_symlink():
            existing_stream = any(
                path.is_file() and not path.is_symlink()
                for path in (
                    self._state_root(record) / "provider-events"
                ).glob("generation-*/delivery/delivery-state.json")
            )
            if existing_stream:
                return "attention"
            return "legacy"
        try:
            stream = RuntimeProviderEventStream.rehydrate(
                self._state_root(record) / "provider-events",
                int(target["generation"]),
            )
            cursor = stream.controller.current_state().cursor
            if (
                not record.accepted_callback_id
                or not record.accepted_callback_sha256
                or not cursor.result_published
            ):
                return "attention"
            # With the cursor already published this is an idempotent exact
            # digest assertion; it cannot synthesize a missing result.
            stream.result(record.accepted_callback_sha256)
            observation = ResourceObservation(
                process_status=process_status,
                supervisor_status=supervisor_status,
                surface_status=surface_status,
                workspace_status=workspace_status,
            )
            if self._fault_observer is not None:
                self._fault_observer("cleanup-receipt-published:before")
            result = ResourceClosureLedger(
                self._state_root(record) / "provider-events"
            ).close(identity, observation)
            if self._fault_observer is not None:
                self._fault_observer("cleanup-receipt-published")
            decision = stream.resource_closed_receipt(result.receipt)
        except (RuntimeProviderEventError, ValueError) as exc:
            raise RuntimeSessionError(
                "typed resource close delivery is invalid"
            ) from exc
        return decision.action

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

    def cleanup(
        self,
        owner_id: str,
        operation_id: str,
        *,
        terminal_state: str = "complete",
    ) -> RuntimeSessionResult:
        """Finalize only after provider exit, then close the exact owned surface."""

        if terminal_state not in {"complete", "cancelled"}:
            raise RuntimeSessionError("cleanup terminal state is invalid")

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
        workspace_status = "unknown" if workspace_placement else "missing"
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
        if record.spec.keep_open and surface_status == "alive":
            return self._result(
                record,
                "keep-open",
                process_status=process_status,
                surface_status=surface_status,
            )
        supervisor = OperationSupervisor(self.store, owner_id, operation_id)
        if surface_status == "alive":
            supervisor.effect(
                "close-surface",
                lambda _record: self.cmux.close_exact(
                    resources.surface_id
                ),
            )
            try:
                surface_status = self.cmux.status(resources.surface_id)
            except Exception:
                surface_status = "unknown"
            if surface_status != "missing":
                current = self._mark_attention(
                    supervisor.read(), AttentionReason.CLEANUP_INCOMPLETE
                )
                return self._result(
                    current,
                    "attention-required",
                    process_status="dead",
                    surface_status=surface_status,
                )
        close_action = self._record_resource_closed(
            supervisor.read(),
            metadata,
            process_status="dead",
            supervisor_status="dead",
            surface_status=surface_status,
            workspace_status=workspace_status,
        )
        if close_action == "attention":
            current = self._mark_attention(
                supervisor.read(), AttentionReason.ATTENTION_REQUIRED
            )
            self._notify(current.spec.owner_id, current.spec.operation_id)
            return self._result(
                current,
                "attention-required",
                process_status="dead",
                surface_status="missing",
            )
        supervisor.bind_resources(OwnedResources())
        current = supervisor.transition(terminal_state)
        self._notify(current.spec.owner_id, current.spec.operation_id)
        return self._result(
            current,
            "cleaned",
            process_status="dead",
            surface_status="missing",
        )
