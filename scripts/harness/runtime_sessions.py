"""Generic provider-session lifecycle owned by the restartable harness."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from time import time
from typing import Callable, Mapping, Protocol, Sequence

from model_routing import RoutingError, load_config, validate_effort

from . import capabilities
from .adapters.claude import ClaudeDriver
from .adapters.cmux import CmuxAdapter
from .adapters.codex import CodexDriver
from .adapters.process import ProcessAdapter
from .callbacks import CallbackBroker
from .contracts import (
    AttentionReason,
    CallbackEnvelope,
    CapabilityReport,
    DEFAULT_ATTEMPT_LIMIT,
    DEFAULT_MODEL_RESTART_LIMIT,
    DEFAULT_TIME_BUDGET_SECONDS,
    DEFAULT_TOKEN_LIMIT,
    EffectOutcome,
    OperationRecord,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from .state_machine import TERMINAL
from .reconciliation import prove_accepted_callback_ownership
from .store import OperationStore, StoreError
from .supervisor import OperationSupervisor


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SURFACE_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\Z"
)
MAX_PROMPT_BYTES = 65_536


from .runtime_session_contracts import (
    IDENTIFIER,
    MAX_PROMPT_BYTES,
    SURFACE_UUID,
    CmuxPort,
    Preflight,
    ProcessPort,
    ProviderDriver,
    RuntimeSessionError,
    RuntimeSessionRequest,
    RuntimeSessionResult,
    StatusNotifier,
    SurfacePrepared,
    _relative,
)
from .runtime_session_launch import RuntimeSessionLaunchMixin
from .runtime_session_cancel import RuntimeSessionCancellationMixin
from .runtime_session_cleanup import RuntimeSessionCleanupMixin
from .runtime_session_checkpoint import RuntimeSessionCheckpointMixin

class RuntimeSessionManager(
    RuntimeSessionLaunchMixin,
    RuntimeSessionCancellationMixin,
    RuntimeSessionCleanupMixin,
    RuntimeSessionCheckpointMixin,
):
    """Drive exact provider resources through one durable OperationRecord."""

    def start_fresh_artifact_repair(self, repair: object) -> object:
        """Run only the registered typed fresh-artifact repair facade."""

        from .fresh_artifact_repair import FreshArtifactRepair

        if not isinstance(repair, FreshArtifactRepair):
            raise RuntimeSessionError("fresh artifact repair request is invalid")
        return repair.start(self)

    def __init__(
        self,
        store: OperationStore,
        cmux: CmuxPort,
        process: ProcessPort,
        drivers: Mapping[str, ProviderDriver] | None = None,
        *,
        preflight: Preflight | None = None,
        registered_models: Mapping[str, frozenset[str]] | None = None,
        start_timeout_seconds: float = 8.0,
        worker: Path | None = None,
        status_notifier: StatusNotifier | None = None,
        fault_observer: Callable[[str], None] | None = None,
    ):
        self.store = store
        self.cmux = cmux
        self.process = process
        self.drivers = dict(drivers or {})
        self.registered_models = dict(registered_models or {})
        self.preflight = preflight or (
            lambda route, callback_dir: capabilities.check(
                route, callback_dir=callback_dir
            )
        )
        self.start_timeout_seconds = start_timeout_seconds
        self.status_notifier = status_notifier
        self._fault_observer = fault_observer
        self.worker = (
            worker
            or Path(__file__).resolve().parents[1] / "harness-runtime-worker.py"
        ).resolve()

    @classmethod
    def for_root(
        cls,
        root: Path,
        *,
        store_root: Path | None = None,
        start_timeout_seconds: float = 8.0,
    ) -> "RuntimeSessionManager":
        """Construct the production local harness without a legacy skill seam."""

        root = root.expanduser().resolve()
        if not root.is_dir():
            raise RuntimeSessionError("runtime root must be an existing directory")
        state = (
            store_root.expanduser().resolve()
            if store_root is not None
            else root / ".vault-meta" / "harness"
        )
        try:
            routing = load_config(root)
        except (OSError, RoutingError) as exc:
            raise RuntimeSessionError("runtime routing config is unavailable") from exc

        def configured_preflight(
            route: RuntimeRoute, callback_dir: Path
        ) -> CapabilityReport:
            registry = routing.data.get("model_registry", {})
            try:
                validate_effort(route.runtime, route.effort)
            except RoutingError:
                registered = False
            else:
                registered = (
                    isinstance(registry, dict)
                    and registry.get(route.model) == route.runtime
                )
            if not registered:
                return CapabilityReport(
                    route,
                    False,
                    ("routing:fingerprint",),
                    AttentionReason.CAPABILITY_MISMATCH,
                )
            return capabilities.check(
                route,
                callback_dir=callback_dir,
                expected_routing_sha256=routing.fingerprint,
            )
        return cls(
            OperationStore(state),
            CmuxAdapter(),
            ProcessAdapter(),
            preflight=configured_preflight,
            registered_models={
                runtime: frozenset(
                    model
                    for model, provider in routing.data["model_registry"].items()
                    if provider == runtime
                )
                for runtime in ("claude", "codex")
            },
            start_timeout_seconds=start_timeout_seconds,
            status_notifier=_default_status_notifier(),
        )

    def _driver(self, route: RuntimeRoute) -> ProviderDriver:
        configured = self.drivers.get(route.runtime)
        if configured is not None:
            return configured
        binary = shutil.which(route.runtime)
        if not binary:
            raise RuntimeSessionError(f"{route.runtime} runtime is unavailable")
        if route.runtime == "claude":
            return ClaudeDriver(
                Path(binary).resolve(),
                self.registered_models.get("claude", frozenset()),
            )
        return CodexDriver(
            Path(binary).resolve(),
            self.registered_models.get("codex", frozenset()),
        )

    def check_route(
        self,
        route: RuntimeRoute,
        callback_dir: Path,
        *,
        origin_surface: str,
    ) -> CapabilityReport:
        """Probe one complete route and exact origin without durable effects."""

        if not SURFACE_UUID.fullmatch(origin_surface):
            return CapabilityReport(
                route, False, (), AttentionReason.CAPABILITY_MISMATCH
            )
        report = self.preflight(route, callback_dir.expanduser().resolve())
        if not report.compatible:
            return report
        try:
            origin_status = self.cmux.status(origin_surface)
        except Exception:
            origin_status = "unknown"
        if origin_status != "alive":
            return CapabilityReport(
                route,
                False,
                report.capabilities,
                AttentionReason.CAPABILITY_MISMATCH,
            )
        return CapabilityReport(
            route,
            True,
            (*report.capabilities, "cmux:origin-alive"),
        )

    def preflight_routes(
        self,
        requests: Sequence[tuple[RuntimeRoute, Path, str]],
    ) -> tuple[CapabilityReport, ...]:
        """Evaluate every requested route before a caller starts any model."""

        return tuple(
            self.check_route(route, callback_dir, origin_surface=origin_surface)
            for route, callback_dir, origin_surface in requests
        )

    @staticmethod
    def _resolve_pointer(cwd: Path, pointer: str, *, must_exist: bool) -> Path:
        candidate = (cwd / _relative(pointer, "runtime pointer")).resolve()
        try:
            candidate.relative_to(cwd)
        except ValueError as exc:
            raise RuntimeSessionError("runtime pointer escapes cwd") from exc
        if must_exist and (not candidate.is_file() or candidate.is_symlink()):
            raise RuntimeSessionError("runtime prompt pointer is unavailable")
        return candidate

    @staticmethod
    def _read_prompt(path: Path) -> str:
        raw = path.read_bytes()
        if not raw or len(raw) > MAX_PROMPT_BYTES:
            raise RuntimeSessionError("runtime prompt must be non-empty and bounded")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeSessionError("runtime prompt must be UTF-8") from exc

    def _state_root(self, record: OperationRecord) -> Path:
        return (
            self.store.root
            / "owners"
            / record.spec.owner_id
            / "runtime"
            / record.spec.operation_id
        )

    def _metadata_path(self, record: OperationRecord) -> Path:
        return self._state_root(record) / "session.json"

    def _control_path(self, record: OperationRecord) -> Path:
        return self._state_root(record) / "process-control.json"

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        tmp = Path(raw)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            tmp.chmod(0o600)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def _write_metadata(
        self,
        record: OperationRecord,
        request: RuntimeSessionRequest,
    ) -> None:
        self._write_json(
            self._metadata_path(record),
            {
                "schema_version": 1,
                "operation_id": record.spec.operation_id,
                "run_id": record.run_id,
                "cwd": str(request.cwd),
                "origin_surface": request.origin_surface,
                "prompt_pointer": request.prompt_pointer,
                "callback_pointer": request.callback_pointer,
                "placement": request.placement,
                "checkpoint": request.checkpoint,
                "product_root": (
                    str(request.product_root) if request.product_root else ""
                ),
                "callback_mode": request.callback_mode,
                "task_summary_pointer": request.task_summary_pointer,
                "initial_callback_operation_id": (
                    request.initial_callback_operation_id
                ),
                "initial_callback_run_id": (
                    request.initial_callback_run_id
                ),
                "runtime_home": (
                    str(request.runtime_home) if request.runtime_home else ""
                ),
                "research_request_sha256": request.research_request_sha256,
                "callback_wake": request.callback_wake,
                "attempt_limit": request.attempt_limit,
                "model_restart_limit": request.model_restart_limit,
                "time_budget_seconds": request.time_budget_seconds,
                "token_limit": request.token_limit,
            },
        )

    def _metadata(self, record: OperationRecord) -> dict[str, object]:
        try:
            value = json.loads(
                self._metadata_path(record).read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            raise RuntimeSessionError("runtime session metadata is unavailable") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or value.get("operation_id") != record.spec.operation_id
            or value.get("run_id") != record.run_id
        ):
            raise RuntimeSessionError("runtime session metadata identity is invalid")
        return value

    def _callback_pointer(self, record: OperationRecord) -> str:
        target = self._callback_target(record)
        value = target.get("callback_pointer")
        if not isinstance(value, str):
            raise RuntimeSessionError("runtime callback pointer is invalid")
        return _relative(value, "callback_pointer")

    def _write_surface_metadata(
        self, record: OperationRecord, opened: object
    ) -> None:
        metadata = self._metadata(record)
        metadata.update(
            {
                "surface_ref": str(getattr(opened, "surface_ref", "")),
                "workspace_id": str(getattr(opened, "workspace_id", "")),
                "workspace_ref": str(getattr(opened, "workspace_ref", "")),
                "window_id": str(getattr(opened, "window_id", "")),
                "window_ref": str(getattr(opened, "window_ref", "")),
            }
        )
        self._write_json(self._metadata_path(record), metadata)

    def _callback_target_path(self, record: OperationRecord) -> Path:
        return self._state_root(record) / "callback-target.json"

    def _callback_target(self, record: OperationRecord) -> dict[str, object]:
        try:
            value = json.loads(
                self._callback_target_path(record).read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            raise RuntimeSessionError("runtime callback target is unavailable") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or type(value.get("generation")) is not int
            or int(value["generation"]) < 1
            or not IDENTIFIER.fullmatch(str(value.get("operation_id") or ""))
            or not IDENTIFIER.fullmatch(str(value.get("run_id") or ""))
        ):
            raise RuntimeSessionError("runtime callback target is invalid")
        _relative(str(value.get("callback_pointer") or ""), "callback_pointer")
        return value

    def _write_callback_target(
        self,
        parent: OperationRecord,
        *,
        operation_id: str,
        run_id: str,
        callback_pointer: str,
        generation: int,
    ) -> None:
        self._write_json(
            self._callback_target_path(parent),
            {
                "schema_version": 1,
                "generation": generation,
                "operation_id": operation_id,
                "run_id": run_id,
                "callback_pointer": _relative(
                    callback_pointer, "callback_pointer"
                ),
            },
        )

    def _notify(self, trigger_owner: str, trigger_operation: str) -> None:
        if self.status_notifier is None:
            return
        try:
            self.status_notifier(
                self.store.root, trigger_owner, trigger_operation
            )
        except Exception:
            pass

    def _result(
        self,
        record: OperationRecord,
        action: str,
        *,
        checkpoint: str = "",
        process_status: str = "unknown",
        surface_status: str = "unknown",
    ) -> RuntimeSessionResult:
        callback_pointer = ""
        surface_metadata: dict[str, object] = {}
        if self._metadata_path(record).is_file():
            callback_pointer = self._callback_pointer(record)
            surface_metadata = self._metadata(record)
        return RuntimeSessionResult(
            record,
            action,
            checkpoint,
            callback_pointer,
            process_status,
            surface_status,
            str(surface_metadata.get("surface_ref") or ""),
            str(surface_metadata.get("workspace_id") or ""),
            str(surface_metadata.get("workspace_ref") or ""),
            str(surface_metadata.get("window_id") or ""),
            str(surface_metadata.get("window_ref") or ""),
        )

    def _mark_attention(
        self, record: OperationRecord, reason: AttentionReason
    ) -> OperationRecord:
        if record.state == "attention-required":
            return record
        if record.state in TERMINAL:
            return record
        self.store.transition(
            record.spec.owner_id,
            record.spec.operation_id,
            "attention-required",
            reason=reason,
        )
        updated = self.store.read(record.spec.owner_id, record.spec.operation_id)
        self._notify(record.spec.owner_id, record.spec.operation_id)
        return updated

    def _supervisor_status(self, record: OperationRecord) -> str:
        probe = getattr(self.process, "pid_status", None)
        if probe is None:
            return "alive"
        return str(
            probe(
                record.resources.supervisor_pid,
                record.resources.supervisor_identity,
            )
        )

    def _cleanup_ownership_statuses(
        self, record: OperationRecord
    ) -> tuple[str, str]:
        proof = prove_accepted_callback_ownership(record, self.process)
        if proof.applicable:
            return proof.process_status, proof.supervisor_status
        resources = record.resources
        process_status = (
            self.process.process_status(
                resources.process_group,
                resources.process_identity,
            )
            if resources.process_group > 1
            else "dead"
        )
        supervisor_status = (
            self._supervisor_status(record)
            if resources.supervisor_pid > 1
            else "dead"
        )
        if "unknown" in {process_status, supervisor_status}:
            try:
                proof = self.prove_durable_cleanup_ownership(
                    record.spec.owner_id, record.spec.operation_id
                )
            except RuntimeSessionError:
                return process_status, supervisor_status
            return proof.process_status, proof.supervisor_status
        return process_status, supervisor_status



def _default_status_notifier() -> StatusNotifier | None:
    try:
        from .status_segment import publish
    except ImportError:
        return None
    return lambda state_root, trigger_owner, trigger_operation: publish(
        state_root,
        trigger_owner=trigger_owner,
        trigger_operation=trigger_operation,
    )
