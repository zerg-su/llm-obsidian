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
from .store import OperationStore, StoreError
from .supervisor import OperationSupervisor


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SURFACE_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\Z"
)
MAX_PROMPT_BYTES = 65_536


class RuntimeSessionError(RuntimeError):
    """A provider session cannot advance without violating lifecycle ownership."""


class ProviderDriver(Protocol):
    def command(
        self,
        route: RuntimeRoute,
        *,
        resume: str = "",
        callback_pointer: Path | None = None,
        product_root: Path | None = None,
        session_root: Path | None = None,
    ) -> tuple[str, ...]: ...


class CmuxPort(Protocol):
    def open_split(self, origin_surface: str) -> object: ...
    def open_workspace(
        self, origin_surface: str, *, cwd: Path | None = None
    ) -> object: ...
    def send(self, surface_id: str, text: str) -> None: ...
    def send_key(self, surface_id: str, key: str) -> None: ...
    def status(self, surface_id: str) -> str: ...
    def workspace_status(
        self, workspace_id: str, window_id: str
    ) -> str: ...
    def resume_checkpoint(self, surface_id: str, runtime: str) -> str: ...
    def close_exact(self, surface_id: str) -> None: ...
    def close_workspace_exact(
        self, workspace_id: str, window_id: str
    ) -> None: ...


class ProcessPort(Protocol):
    def prepare_surface_launch(self, **kwargs: object) -> object: ...
    def await_surface_handle(
        self, launch: object, *, timeout_seconds: float
    ) -> object: ...
    def process_status(self, process_group: int, identity: str) -> str: ...
    def request_exit(self, process_group: int, identity: str) -> None: ...
    def terminate_exact(self, process_group: int, identity: str) -> None: ...
    def request_guardian_signal(
        self,
        control_path: Path,
        *,
        action: str,
        operation_id: str,
        run_id: str,
        process_group: int,
        process_identity: str,
        supervisor_pid: int,
        supervisor_identity: str,
    ) -> None: ...


Preflight = Callable[[RuntimeRoute, Path], CapabilityReport]
SurfacePrepared = Callable[["RuntimeSessionResult"], None]
StatusNotifier = Callable[[Path], object]


def _relative(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
    ):
        raise RuntimeSessionError(f"{label} must be an owner-relative path")
    return path.as_posix()


@dataclass(frozen=True)
class RuntimeSessionRequest:
    spec: OperationSpec
    lane_id: str
    run_id: str
    origin_surface: str
    cwd: Path
    prompt_pointer: str
    callback_pointer: str
    placement: str = "split"
    checkpoint: str = ""
    product_root: Path | None = None
    callback_mode: str = "envelope"
    task_summary_pointer: str = ""
    initial_callback_operation_id: str = ""
    initial_callback_run_id: str = ""
    runtime_home: Path | None = None
    research_request_sha256: str = ""
    callback_wake: str = ""
    attempt_limit: int = DEFAULT_ATTEMPT_LIMIT
    model_restart_limit: int = DEFAULT_MODEL_RESTART_LIMIT
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS
    token_limit: int = DEFAULT_TOKEN_LIMIT

    def __post_init__(self) -> None:
        if not IDENTIFIER.fullmatch(self.lane_id):
            raise RuntimeSessionError("lane_id must be a bounded identifier")
        if not IDENTIFIER.fullmatch(self.run_id):
            raise RuntimeSessionError("run_id must be a bounded identifier")
        if not SURFACE_UUID.fullmatch(self.origin_surface):
            raise RuntimeSessionError("origin_surface must be an exact UUID")
        if self.placement not in {"split", "workspace"}:
            raise RuntimeSessionError("placement must be split or workspace")
        if self.callback_mode not in {
            "envelope",
            "task-summary",
            "research-fetch",
            "research-synth",
        }:
            raise RuntimeSessionError("runtime callback mode is invalid")
        if (
            type(self.attempt_limit) is not int
            or self.attempt_limit < 1
            or type(self.model_restart_limit) is not int
            or self.model_restart_limit < 0
            or not isinstance(self.time_budget_seconds, (int, float))
            or isinstance(self.time_budget_seconds, bool)
            or self.time_budget_seconds <= 0
            or type(self.token_limit) is not int
            or self.token_limit < 1
        ):
            raise RuntimeSessionError("runtime operation budget is invalid")
        cwd = self.cwd.expanduser().resolve()
        if not cwd.is_dir():
            raise RuntimeSessionError("runtime cwd must be an existing directory")
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(
            self, "prompt_pointer", _relative(self.prompt_pointer, "prompt_pointer")
        )
        object.__setattr__(
            self,
            "callback_pointer",
            _relative(self.callback_pointer, "callback_pointer"),
        )
        if self.callback_mode == "task-summary":
            normalized_summary = _relative(
                self.task_summary_pointer, "task_summary_pointer"
            )
            if normalized_summary != ".task-summary.json":
                raise RuntimeSessionError(
                    "task-summary callback requires canonical .task-summary.json"
                )
            object.__setattr__(
                self, "task_summary_pointer", normalized_summary
            )
        elif self.task_summary_pointer:
            raise RuntimeSessionError(
                "task_summary_pointer requires task-summary callback mode"
            )
        if bool(self.initial_callback_operation_id) != bool(
            self.initial_callback_run_id
        ):
            raise RuntimeSessionError(
                "initial callback target identity must be complete"
            )
        if self.initial_callback_operation_id:
            if (
                not IDENTIFIER.fullmatch(
                    self.initial_callback_operation_id
                )
                or not IDENTIFIER.fullmatch(
                    self.initial_callback_run_id
                )
            ):
                raise RuntimeSessionError(
                    "initial callback target identity is invalid"
                )
        research_mode = self.callback_mode in {
            "research-fetch",
            "research-synth",
        }
        runtime_home = (
            self.runtime_home.expanduser()
            if self.runtime_home is not None
            else None
        )
        reviewer_wake_mode = (
            self.callback_mode == "envelope"
            and self.spec.route.profile == "reviewer-callback"
        )
        if research_mode:
            expected_callback = (
                "artifact.json"
                if self.callback_mode == "research-fetch"
                else "complete.json"
            )
            if (
                self.spec.route.runtime != "codex"
                or self.spec.route.profile != "research-safe"
                or self.callback_pointer != expected_callback
                or runtime_home is None
                or runtime_home.is_symlink()
            ):
                raise RuntimeSessionError(
                    "safe research requires its exact isolated runtime"
                )
            runtime_home = runtime_home.resolve()
            try:
                runtime_stat = runtime_home.stat()
            except OSError as exc:
                raise RuntimeSessionError(
                    "safe research runtime home is unavailable"
                ) from exc
            if (
                not runtime_home.is_dir()
                or runtime_stat.st_uid != os.getuid()
                or runtime_stat.st_mode & 0o077
                or runtime_home == cwd
                or runtime_home in cwd.parents
                or cwd in runtime_home.parents
            ):
                raise RuntimeSessionError(
                    "safe research runtime home must be owner-only and disjoint"
                )
            if (
                not isinstance(self.callback_wake, str)
                or not self.callback_wake
                or self.callback_wake != self.callback_wake.strip()
                or "\0" in self.callback_wake
                or "\n" in self.callback_wake
                or "\r" in self.callback_wake
                or len(self.callback_wake.encode()) > 4096
            ):
                raise RuntimeSessionError(
                    "research callback wake must be bounded"
                )
            if self.callback_mode == "research-fetch":
                if not re.fullmatch(
                    r"[0-9a-f]{64}", self.research_request_sha256
                ):
                    raise RuntimeSessionError(
                        "research fetch requires its request digest"
                    )
            elif self.research_request_sha256:
                raise RuntimeSessionError(
                    "research synth derives identity from its artifact"
                )
        elif reviewer_wake_mode:
            if self.callback_wake and (
                self.callback_wake != self.callback_wake.strip()
                or "\0" in self.callback_wake
                or "\n" in self.callback_wake
                or "\r" in self.callback_wake
                or len(self.callback_wake.encode()) > 4096
            ):
                raise RuntimeSessionError(
                    "review callback wake must be one bounded line"
                )
            if runtime_home is not None or self.research_request_sha256:
                raise RuntimeSessionError(
                    "research runtime fields require research callback mode"
                )
        elif (
            runtime_home is not None
            or self.research_request_sha256
            or self.callback_wake
        ):
            raise RuntimeSessionError(
                "research runtime fields require research callback mode"
            )
        object.__setattr__(self, "runtime_home", runtime_home)
        if self.checkpoint and not IDENTIFIER.fullmatch(self.checkpoint):
            raise RuntimeSessionError("checkpoint must be a bounded identifier")
        product_root = (
            self.product_root.expanduser().resolve()
            if self.product_root is not None
            else None
        )
        if self.spec.route.profile == "reviewer-callback":
            if product_root is None or not product_root.is_dir():
                raise RuntimeSessionError(
                    "review callback profile requires an existing product root"
                )
            if (
                product_root == cwd
                or product_root in cwd.parents
                or cwd in product_root.parents
            ):
                raise RuntimeSessionError(
                    "review callback scratch must be isolated from product root"
                )
        object.__setattr__(self, "product_root", product_root)


@dataclass(frozen=True)
class RuntimeSessionResult:
    record: OperationRecord
    action: str
    checkpoint: str = ""
    callback_pointer: str = ""
    process_status: str = "unknown"
    surface_status: str = "unknown"
    surface_ref: str = ""
    workspace_id: str = ""
    workspace_ref: str = ""
    window_id: str = ""
    window_ref: str = ""

    @property
    def operation_id(self) -> str:
        return self.record.spec.operation_id

    @property
    def lane_id(self) -> str:
        return self.record.lane_id

    @property
    def run_id(self) -> str:
        return self.record.run_id


class RuntimeSessionManager:
    """Drive exact provider resources through one durable OperationRecord."""

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

    def _notify(self) -> None:
        if self.status_notifier is None:
            return
        try:
            self.status_notifier(self.store.root)
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
        self._notify()
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

    def _abort_prepared_surface(
        self,
        supervisor: OperationSupervisor,
        opened: object,
        placement: str,
    ) -> None:
        surface_id = str(getattr(opened, "surface_id", ""))
        try:
            if placement == "workspace":
                self.cmux.close_workspace_exact(
                    str(getattr(opened, "workspace_id", "")),
                    str(getattr(opened, "window_id", "")),
                )
            else:
                self.cmux.close_exact(surface_id)
        except Exception as exc:
            record = self._mark_attention(
                supervisor.read(), AttentionReason.CLEANUP_INCOMPLETE
            )
            raise RuntimeSessionError(
                f"prepared surface cleanup requires attention: {record.state}"
            ) from exc
        supervisor.bind_resources(OwnedResources())
        self.store.transition(
            supervisor.owner_id, supervisor.operation_id, "failed"
        )
        self._notify()

    def start(
        self,
        request: RuntimeSessionRequest,
        *,
        on_surface_opened: SurfacePrepared | None = None,
    ) -> RuntimeSessionResult:
        """Preflight, bind one exact surface, then launch one provider worker."""

        prompt_path = self._resolve_pointer(
            request.cwd, request.prompt_pointer, must_exist=True
        )
        callback_path = self._resolve_pointer(
            request.cwd, request.callback_pointer, must_exist=False
        )
        callback_outbox = request.cwd / request.callback_pointer
        if not callback_path.parent.is_dir():
            raise RuntimeSessionError("runtime callback directory is unavailable")
        try:
            existing = self.store.read(
                request.spec.owner_id, request.spec.operation_id
            )
        except StoreError:
            existing = None
        exact_replay = (
            existing is not None
            and existing.spec == request.spec
            and existing.lane_id == request.lane_id
            and existing.run_id == request.run_id
            and existing.state != "created"
        )
        callback_exists = callback_outbox.exists() or callback_outbox.is_symlink()
        if callback_exists and (
            not exact_replay
            or callback_outbox.is_symlink()
            or not callback_outbox.is_file()
        ):
            raise RuntimeSessionError(
                "runtime callback pointer must be a fresh owned outbox"
            )
        task_summary_path = (
            self._resolve_pointer(
                request.cwd, request.task_summary_pointer, must_exist=False
            )
            if request.callback_mode == "task-summary"
            else None
        )
        task_summary_source = (
            request.cwd / request.task_summary_pointer
            if task_summary_path is not None
            else None
        )
        if task_summary_source is not None and (
            task_summary_source.exists() or task_summary_source.is_symlink()
        ):
            if (
                not exact_replay
                or task_summary_source.is_symlink()
                or not task_summary_source.is_file()
            ):
                raise RuntimeSessionError(
                    "task summary source must be a fresh owned handoff"
                )
        prompt = self._read_prompt(prompt_path)
        driver = self._driver(request.spec.route)
        argv = (
            *driver.command(
                request.spec.route,
                resume=request.checkpoint,
                callback_pointer=callback_path,
                product_root=request.product_root,
                session_root=request.cwd,
            ),
            prompt,
        )
        report = self.check_route(
            request.spec.route,
            callback_path.parent,
            origin_surface=request.origin_surface,
        )
        if not report.compatible:
            reason = report.reason.value if report.reason else "capability-mismatch"
            raise RuntimeSessionError(f"runtime preflight failed: {reason}")

        record = self.store.create(
            request.spec, lane_id=request.lane_id, run_id=request.run_id
        )
        if record.state != "created":
            metadata = self._metadata(record)
            expected = {
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
            }
            if any(metadata.get(key) != value for key, value in expected.items()):
                raise RuntimeSessionError(
                    "idempotent start request changed runtime session identity"
                )
            observed = self.status(
                request.spec.owner_id, request.spec.operation_id
            )
            return (
                replace(observed, action="already-started")
                if observed.action == "observed"
                else observed
            )
        supervisor = OperationSupervisor(
            self.store, request.spec.owner_id, request.spec.operation_id
        )
        try:
            record = supervisor.configure_budget(
                attempt_limit=request.attempt_limit,
                model_restart_limit=request.model_restart_limit,
                time_budget_seconds=request.time_budget_seconds,
                token_limit=request.token_limit,
            )
            record = supervisor.consume_attempt()
        except Exception as exc:
            raise RuntimeSessionError(
                "runtime operation budget requires attention"
            ) from exc
        self._write_metadata(record, request)
        initial_operation_id = request.spec.operation_id
        initial_run_id = record.run_id
        if request.initial_callback_operation_id:
            child = self.store.read(
                request.spec.owner_id,
                request.initial_callback_operation_id,
            )
            if (
                child.run_id != request.initial_callback_run_id
                or child.lane_id != record.lane_id
                or child.state != "awaiting-callback"
            ):
                raise RuntimeSessionError(
                    "initial callback target must be the exact awaiting "
                    "same-lane child"
                )
            initial_operation_id = child.spec.operation_id
            initial_run_id = child.run_id
        self._write_callback_target(
            record,
            operation_id=initial_operation_id,
            run_id=initial_run_id,
            callback_pointer=request.callback_pointer,
            generation=1,
        )
        self.store.transition(
            request.spec.owner_id, request.spec.operation_id, "preflight"
        )
        self.store.transition(
            request.spec.owner_id, request.spec.operation_id, "starting"
        )
        def bind_surface(_record: OperationRecord, opened: object) -> None:
            surface_id = str(getattr(opened, "surface_id", ""))
            if not SURFACE_UUID.fullmatch(surface_id):
                raise RuntimeSessionError("cmux returned no exact owned surface")
            supervisor.bind_resources(OwnedResources(surface_id=surface_id))

        try:
            opened = supervisor.effect(
                "open-surface",
                lambda _record: (
                    self.cmux.open_split(request.origin_surface)
                    if request.placement == "split"
                    else self.cmux.open_workspace(
                        request.origin_surface, cwd=request.cwd
                    )
                ),
                persist_result=bind_surface,
            ).value
        except Exception as exc:
            current = self._mark_attention(
                supervisor.read(), AttentionReason.SURFACE_OPEN_FAILED
            )
            raise RuntimeSessionError(
                f"surface open requires attention: {current.state}"
            ) from exc
        surface_id = str(getattr(opened, "surface_id", ""))
        self._write_surface_metadata(supervisor.read(), opened)

        if on_surface_opened is not None:
            try:
                on_surface_opened(self._result(supervisor.read(), "surface-opened"))
            except Exception as exc:
                self._abort_prepared_surface(
                    supervisor, opened, request.placement
                )
                raise RuntimeSessionError(
                    "surface preparation failed before provider launch"
                ) from exc

        try:
            launch = self.process.prepare_surface_launch(
                argv=argv,
                cwd=request.cwd,
                state_root=self._state_root(supervisor.read()),
                worker=self.worker,
                callback_pointer=callback_path,
                callback_registration=self._callback_target_path(supervisor.read()),
                store_root=self.store.root,
                owner_id=request.spec.owner_id,
                operation_id=request.spec.operation_id,
                run_id=request.run_id,
                surface_id=surface_id,
                runtime=request.spec.route.runtime,
                callback_mode=request.callback_mode,
                task_summary_pointer=task_summary_path,
                origin_surface=request.origin_surface,
                runtime_home=request.runtime_home,
                research_request_sha256=request.research_request_sha256,
                callback_wake=request.callback_wake,
            )
        except Exception as exc:
            self._abort_prepared_surface(
                supervisor, opened, request.placement
            )
            raise RuntimeSessionError("provider worker preparation failed") from exc

        def start_provider(_record: OperationRecord) -> object:
            self.cmux.send(surface_id, str(getattr(launch, "command")))
            self.cmux.send_key(surface_id, "Enter")
            return self.process.await_surface_handle(
                launch, timeout_seconds=self.start_timeout_seconds
            )

        def bind_process(_record: OperationRecord, handle: object) -> None:
            pid = int(getattr(handle, "pid", 0))
            pgid = int(getattr(handle, "process_group", 0))
            supervisor_pid = int(getattr(handle, "supervisor_pid", 0)) or pid
            process_identity = str(
                getattr(handle, "process_identity", "")
            )
            supervisor_identity = str(
                getattr(handle, "supervisor_identity", "")
            )
            if (
                pid <= 1
                or pgid <= 1
                or pid != pgid
                or not re.fullmatch(r"[0-9a-f]{64}", process_identity)
                or not re.fullmatch(r"[0-9a-f]{64}", supervisor_identity)
            ):
                raise RuntimeSessionError("provider worker returned invalid ownership")
            supervisor.bind_resources(
                OwnedResources(
                    surface_id=surface_id,
                    process_group=pgid,
                    supervisor_pid=supervisor_pid,
                    process_identity=process_identity,
                    supervisor_identity=supervisor_identity,
                )
            )

        try:
            supervisor.effect(
                "start-provider",
                start_provider,
                persist_result=bind_process,
            )
        except Exception as exc:
            current = self._mark_attention(
                supervisor.read(), AttentionReason.PROCESS_START_FAILED
            )
            raise RuntimeSessionError(
                f"provider start requires attention: {current.state}"
            ) from exc
        supervisor.transition("running")
        record = supervisor.transition("awaiting-callback")
        self._notify()
        checkpoint = ""
        try:
            checkpoint = self.cmux.resume_checkpoint(
                surface_id, record.spec.route.runtime
            )
        except Exception:
            pass
        return self._result(record, "started", checkpoint=checkpoint)

    def continue_session(
        self,
        owner_id: str,
        operation_id: str,
        checkpoint: str,
        prompt_pointer: str,
    ) -> RuntimeSessionResult:
        """Send one bounded prompt to the exact existing provider session."""

        if not checkpoint or not IDENTIFIER.fullmatch(checkpoint):
            raise RuntimeSessionError("same-session continuation needs a checkpoint")
        record = self.store.read(owner_id, operation_id)
        if record.state not in {"running", "awaiting-callback", "verifying"}:
            raise RuntimeSessionError("operation cannot continue from its current state")
        if not record.resources.surface_id or record.resources.process_group <= 1:
            raise RuntimeSessionError("operation has no exact live ownership")
        actual = self.cmux.resume_checkpoint(
            record.resources.surface_id, record.spec.route.runtime
        )
        if actual != checkpoint:
            raise RuntimeSessionError("same-session checkpoint identity changed")
        metadata = self._metadata(record)
        cwd = Path(str(metadata.get("cwd") or "")).resolve()
        prompt_path = self._resolve_pointer(cwd, prompt_pointer, must_exist=True)
        prompt = self._read_prompt(prompt_path)
        effect_id = f"continue-{hashlib.sha256(prompt.encode()).hexdigest()[:32]}"
        supervisor = OperationSupervisor(self.store, owner_id, operation_id)
        current = supervisor.read()
        if not (
            current.effect_id == effect_id
            and current.effect_outcome == EffectOutcome.SUCCEEDED
        ):
            time_budget_seconds = metadata.get("time_budget_seconds")
            if (
                not isinstance(time_budget_seconds, (int, float))
                or isinstance(time_budget_seconds, bool)
                or time_budget_seconds <= 0
            ):
                raise RuntimeSessionError(
                    "same-session continuation has no valid time budget"
                )
            supervisor.begin_continuation(
                time_budget_seconds=float(time_budget_seconds)
            )

        def send_prompt(_record: OperationRecord) -> None:
            self.cmux.send(record.resources.surface_id, prompt)
            self.cmux.send_key(record.resources.surface_id, "Enter")

        supervisor.effect(effect_id, send_prompt)
        current = supervisor.read()
        if current.state != "running":
            current = supervisor.transition("running")
        self._notify()
        return self._result(current, "continued", checkpoint=checkpoint)

    def register_callback_target(
        self,
        owner_id: str,
        parent_operation_id: str,
        callback_operation_id: str,
        callback_run_id: str,
        callback_pointer: str,
    ) -> RuntimeSessionResult:
        """Atomically retarget the live worker to one same-lane child receipt."""

        parent = self.store.read(owner_id, parent_operation_id)
        child = self.store.read(owner_id, callback_operation_id)
        if (
            child.run_id != callback_run_id
            or child.lane_id != parent.lane_id
            or child.state != "awaiting-callback"
        ):
            raise RuntimeSessionError(
                "callback target must be the exact awaiting same-lane child"
            )
        metadata = self._metadata(parent)
        cwd = Path(str(metadata.get("cwd") or "")).resolve()
        pointer_path = self._resolve_pointer(
            cwd, callback_pointer, must_exist=False
        )
        if (
            not pointer_path.parent.is_dir()
            or not os.access(pointer_path.parent, os.W_OK)
        ):
            raise RuntimeSessionError("callback target directory is unavailable")
        normalized = _relative(callback_pointer, "callback_pointer")
        with self.store.locked(owner_id):
            current = self._callback_target(parent)
            if (
                current.get("operation_id") == callback_operation_id
                and current.get("run_id") == callback_run_id
                and current.get("callback_pointer") == normalized
            ):
                return self._result(parent, "callback-target-unchanged")
            if (
                parent.spec.route.runtime == "claude"
                and parent.spec.route.profile == "reviewer-callback"
                and normalized != self._metadata(parent).get("callback_pointer")
            ):
                raise RuntimeSessionError(
                    "Claude callback target must reuse its exact allowed outbox"
                )
            if pointer_path.exists() or pointer_path.is_symlink():
                if (
                    normalized != current.get("callback_pointer")
                    or pointer_path.is_symlink()
                    or not pointer_path.is_file()
                ):
                    raise RuntimeSessionError(
                        "callback target is not a reusable owned outbox"
                    )
                pointer_path.unlink()
            self._write_callback_target(
                parent,
                operation_id=callback_operation_id,
                run_id=callback_run_id,
                callback_pointer=normalized,
                generation=int(current["generation"]) + 1,
            )
        self._notify()
        return self._result(parent, "callback-target-registered")

    def continue_same_session_round(
        self,
        owner_id: str,
        parent_operation_id: str,
        checkpoint: str,
        prompt_pointer: str,
        callback_operation_id: str,
        callback_run_id: str,
        callback_pointer: str,
    ) -> RuntimeSessionResult:
        """Retarget one child, send one prompt, then await its callback.

        The child owns only the typed result receipt. Provider and cmux
        resources remain anchored to the persistent parent operation.
        """

        self.register_callback_target(
            owner_id,
            parent_operation_id,
            callback_operation_id,
            callback_run_id,
            callback_pointer,
        )
        self.continue_session(
            owner_id,
            parent_operation_id,
            checkpoint,
            prompt_pointer,
        )
        parent = self.store.read(owner_id, parent_operation_id)
        if parent.state == "running":
            self.store.transition(
                owner_id,
                parent_operation_id,
                "awaiting-callback",
            )
            parent = self.store.read(owner_id, parent_operation_id)
        elif parent.state != "awaiting-callback":
            raise RuntimeSessionError(
                "same-session round cannot await its callback"
            )
        self._notify()
        return self._result(
            parent,
            "round-continued",
            checkpoint=checkpoint,
        )

    def accept_callback(
        self, envelope: CallbackEnvelope
    ) -> RuntimeSessionResult:
        # Operation ids are owner-scoped; resolve the only exact durable match
        # rather than accepting a caller-supplied ownership guess.
        owner_id = self._owner_for_operation(envelope.operation_id)
        record = self.store.read(owner_id, envelope.operation_id)
        deadline_operation_id = ""
        if (
            envelope.kind == "review"
            and record.spec.kind == "review-round"
        ):
            parent = envelope.payload.get(
                "parent_session_operation_id"
            )
            if (
                not isinstance(parent, str)
                or not IDENTIFIER.fullmatch(parent)
            ):
                raise RuntimeSessionError(
                    "review round callback has no exact parent session"
                )
            deadline_operation_id = parent
        acceptance = CallbackBroker(
            self.store, record.spec.owner_id
        ).accept(
            envelope,
            deadline_operation_id=deadline_operation_id,
        )
        updated = self.store.read(record.spec.owner_id, envelope.operation_id)
        action = "callback-duplicate" if acceptance.duplicate else "callback-accepted"
        self._notify()
        return self._result(updated, action)

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
        process_status = self.process.process_status(
            record.resources.process_group,
            record.resources.process_identity,
        )
        supervisor_status = self._supervisor_status(record)
        if process_status == "unknown" or (
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
        self._notify()
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
        self._notify()
        return self._result(
            current,
            "cleaned",
            process_status="dead",
            surface_status="missing",
        )


def _default_status_notifier() -> StatusNotifier | None:
    try:
        from .status_segment import publish
    except ImportError:
        return None
    return publish
