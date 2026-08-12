"""Typed request/result contracts for provider runtime sessions."""

from __future__ import annotations

MODEL_JSON_BOUNDARIES = (
    "live-dispatch-ack",
    "permissions",
    "dependencies",
    "external-state",
)

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol

from .contracts import (
    CapabilityReport,
    DEFAULT_ATTEMPT_LIMIT,
    DEFAULT_MODEL_RESTART_LIMIT,
    DEFAULT_TIME_BUDGET_SECONDS,
    DEFAULT_TOKEN_LIMIT,
    OperationRecord,
    OperationSpec,
    RuntimeRoute,
)


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SURFACE_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\Z"
)
MAX_PROMPT_BYTES = 65_536

class RuntimeSessionError(RuntimeError):
    """A provider session cannot advance without violating lifecycle ownership."""


class RuntimeCheckpointEvidenceMissing(RuntimeSessionError):
    """The exact provider checkpoint artifact was never materialized."""


def checkpointless_reviewer_route(route: RuntimeRoute) -> bool:
    """Return whether this review route legitimately has no cmux resume binding."""

    return (
        route.runtime == "claude"
        and route.profile == "reviewer-callback"
    )


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
    def read(self, surface_id: str) -> str: ...
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
StatusNotifier = Callable[[Path, str, str], object]


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
        _normalize_core_request(self)
        _normalize_summary_contract(self)
        _normalize_initial_callback(self)
        _normalize_runtime_fields(self)
        _normalize_checkpoint_and_product(self)


def _normalize_core_request(request: RuntimeSessionRequest) -> None:
    if not IDENTIFIER.fullmatch(request.lane_id):
        raise RuntimeSessionError("lane_id must be a bounded identifier")
    if not IDENTIFIER.fullmatch(request.run_id):
        raise RuntimeSessionError("run_id must be a bounded identifier")
    if not SURFACE_UUID.fullmatch(request.origin_surface):
        raise RuntimeSessionError("origin_surface must be an exact UUID")
    if request.placement not in {"split", "workspace"}:
        raise RuntimeSessionError("placement must be split or workspace")
    if request.callback_mode not in {
        "envelope",
        "task-summary",
        "research-fetch",
        "research-synth",
    }:
        raise RuntimeSessionError("runtime callback mode is invalid")
    if (
        type(request.attempt_limit) is not int
        or request.attempt_limit < 1
        or type(request.model_restart_limit) is not int
        or request.model_restart_limit < 0
        or not isinstance(request.time_budget_seconds, (int, float))
        or isinstance(request.time_budget_seconds, bool)
        or request.time_budget_seconds <= 0
        or type(request.token_limit) is not int
        or request.token_limit < 1
    ):
        raise RuntimeSessionError("runtime operation budget is invalid")
    cwd = request.cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise RuntimeSessionError("runtime cwd must be an existing directory")
    object.__setattr__(request, "cwd", cwd)
    object.__setattr__(
        request,
        "prompt_pointer",
        _relative(request.prompt_pointer, "prompt_pointer"),
    )
    object.__setattr__(
        request,
        "callback_pointer",
        _relative(request.callback_pointer, "callback_pointer"),
    )


def _normalize_summary_contract(request: RuntimeSessionRequest) -> None:
    if request.callback_mode == "task-summary":
        normalized = _relative(
            request.task_summary_pointer, "task_summary_pointer"
        )
        if normalized != ".task-summary.json":
            raise RuntimeSessionError(
                "task-summary callback requires canonical .task-summary.json"
            )
        object.__setattr__(request, "task_summary_pointer", normalized)
    elif request.task_summary_pointer:
        raise RuntimeSessionError(
            "task_summary_pointer requires task-summary callback mode"
        )


def _normalize_initial_callback(request: RuntimeSessionRequest) -> None:
    if bool(request.initial_callback_operation_id) != bool(
        request.initial_callback_run_id
    ):
        raise RuntimeSessionError(
            "initial callback target identity must be complete"
        )
    if request.initial_callback_operation_id and (
        not IDENTIFIER.fullmatch(request.initial_callback_operation_id)
        or not IDENTIFIER.fullmatch(request.initial_callback_run_id)
    ):
        raise RuntimeSessionError(
            "initial callback target identity is invalid"
        )


def _normalize_runtime_fields(request: RuntimeSessionRequest) -> None:
    runtime_home = (
        request.runtime_home.expanduser()
        if request.runtime_home is not None
        else None
    )
    if request.callback_mode in {"research-fetch", "research-synth"}:
        runtime_home = _normalize_research_fields(request, runtime_home)
    elif (
        request.callback_mode == "envelope"
        and request.spec.route.profile == "reviewer-callback"
    ):
        _validate_reviewer_fields(request, runtime_home)
    elif runtime_home is not None or request.research_request_sha256 or request.callback_wake:
        raise RuntimeSessionError(
            "research runtime fields require research callback mode"
        )
    object.__setattr__(request, "runtime_home", runtime_home)


def _normalize_research_fields(
    request: RuntimeSessionRequest,
    runtime_home: Path | None,
) -> Path:
    expected_callback = (
        "artifact.json"
        if request.callback_mode == "research-fetch"
        else "complete.json"
    )
    if (
        request.spec.route.runtime != "codex"
        or request.spec.route.profile != "research-safe"
        or request.callback_pointer != expected_callback
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
    cwd = request.cwd
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
        not isinstance(request.callback_wake, str)
        or not request.callback_wake
        or request.callback_wake != request.callback_wake.strip()
        or "\0" in request.callback_wake
        or "\n" in request.callback_wake
        or "\r" in request.callback_wake
        or len(request.callback_wake.encode()) > 4096
    ):
        raise RuntimeSessionError("research callback wake must be bounded")
    if request.callback_mode == "research-fetch":
        if not re.fullmatch(r"[0-9a-f]{64}", request.research_request_sha256):
            raise RuntimeSessionError(
                "research fetch requires its request digest"
            )
    elif request.research_request_sha256:
        raise RuntimeSessionError(
            "research synth derives identity from its artifact"
        )
    return runtime_home


def _validate_reviewer_fields(
    request: RuntimeSessionRequest,
    runtime_home: Path | None,
) -> None:
    if request.callback_wake and (
        request.callback_wake != request.callback_wake.strip()
        or "\0" in request.callback_wake
        or "\n" in request.callback_wake
        or "\r" in request.callback_wake
        or len(request.callback_wake.encode()) > 4096
    ):
        raise RuntimeSessionError(
            "review callback wake must be one bounded line"
        )
    if runtime_home is not None or request.research_request_sha256:
        raise RuntimeSessionError(
            "research runtime fields require research callback mode"
        )


def _normalize_checkpoint_and_product(
    request: RuntimeSessionRequest,
) -> None:
    if request.checkpoint and not IDENTIFIER.fullmatch(request.checkpoint):
        raise RuntimeSessionError("checkpoint must be a bounded identifier")
    product_root = (
        request.product_root.expanduser().resolve()
        if request.product_root is not None
        else None
    )
    if request.spec.route.profile == "reviewer-callback":
        if product_root is None or not product_root.is_dir():
            raise RuntimeSessionError(
                "review callback profile requires an existing product root"
            )
        cwd = request.cwd
        if (
            product_root == cwd
            or product_root in cwd.parents
            or cwd in product_root.parents
        ):
            raise RuntimeSessionError(
                "review callback scratch must be isolated from product root"
            )
    elif request.callback_mode not in {"research-fetch", "research-synth"}:
        if product_root is None:
            product_root = request.cwd
        if (
            not product_root.is_dir()
            or product_root != request.cwd
        ):
            raise RuntimeSessionError(
                "ordinary runtime requires product root equal to cwd"
            )
    object.__setattr__(request, "product_root", product_root)


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
    checkpoint_sha256: str = ""

    @property
    def operation_id(self) -> str:
        return self.record.spec.operation_id

    @property
    def lane_id(self) -> str:
        return self.record.lane_id

    @property
    def run_id(self) -> str:
        return self.record.run_id


def continuation_effect_id(prompt: str) -> str:
    """Return the write-ahead identity for one exact continuation prompt."""

    if not isinstance(prompt, str) or not prompt:
        raise RuntimeSessionError("continuation prompt must be non-empty")
    return f"continue-{hashlib.sha256(prompt.encode()).hexdigest()[:32]}"
