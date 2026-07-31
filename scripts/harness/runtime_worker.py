"""Short-lived supervisor for one provider process inside an owned cmux surface."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .adapters.cmux import CmuxAdapter
from .adapters.process import ProcessAdapter, ProcessError, ProcessHandle
from .callbacks import (
    REVIEWER_PROFILES,
    CallbackBroker,
    CallbackError,
    CallbackTimeoutError,
)
from .contracts import (
    AttentionReason,
    CallbackEnvelope,
    DEFAULT_TIME_BUDGET_SECONDS,
    DEFAULT_TOKEN_LIMIT,
    EffectOutcome,
    OperationSpec,
    to_dict,
)
from .prompts import PromptDecision, classify
from .pipeline_builtins import compiled_executable_for_contract
from .pipelines import reconcile_pipeline
from .review_finalization import task_review_status
from .state_machine import TERMINAL
from .store import OperationStore
from .supervisor import OperationSupervisor, SupervisorError
from .verification import (
    VerificationError,
    load_profiles,
    run_profile,
)
from .workflows.engineering_fix import (
    FixStepReceipt,
    FixWorkflowError,
    accept_phase,
    load_receipt,
    prepare_next_phase,
    reconcile_fix,
)
from research_contract import (
    ResearchContractError,
    load_artifact,
    validate_result_artifact,
)
from lifecycle_telemetry import emit_compiled_pipeline_event
from task_contract import ContractError, validate_handoff
from wiki_summary_contract import WikiSummaryError, validate_summary


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SURFACE_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\Z"
)
MAX_OUTBOX_BYTES = 70_000
MAX_SCREEN_BYTES = 70_000
MAX_PIPELINE_VERIFY_RESUBMITS = 1
RESEARCH_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
CALLBACK_WAIT_STATES = frozenset(
    {"running", "awaiting-callback", "verifying"}
)


class RuntimeWorkerError(RuntimeError):
    pass


def _pipeline_verify_identity(
    parent: OperationSpec,
    *,
    definition_sha256: str,
    input_sha256: str,
    profile: str,
) -> tuple[OperationSpec, str, str]:
    """Derive one immutable verify operation from its exact pipeline input."""

    suffix = f"-verify-{input_sha256[:16]}"
    operation_id = f"{parent.operation_id[: 128 - len(suffix)]}{suffix}"
    idempotency_key = hashlib.sha256(
        (
            f"{parent.idempotency_key}:pipeline-verify:{operation_id}:"
            f"{definition_sha256}:{input_sha256}:{profile}"
        ).encode()
    ).hexdigest()
    child = OperationSpec(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        kind="pipeline-verify",
        owner_id=parent.owner_id,
        route=parent.route,
        context_manifest=parent.context_manifest,
        verification_profile=profile,
        keep_open=False,
        contract_sha256=definition_sha256,
    )
    lane_id = hashlib.sha256(
        f"{idempotency_key}:lane".encode()
    ).hexdigest()[:32]
    run_id = hashlib.sha256(
        f"{idempotency_key}:run".encode()
    ).hexdigest()[:32]
    return child, lane_id, run_id


def provider_exit_is_final(
    *,
    provider_exited: bool,
    callback_mode: str,
    callback_handled: bool,
    operation_state: str,
    operation_profile: str,
    callback_deadline_at: float,
) -> bool:
    """Keep callback transports alive until handled or durably stopped."""

    if not provider_exited:
        return False
    if callback_handled:
        return True
    if (
        callback_mode == "task-summary"
        or (
            operation_profile in REVIEWER_PROFILES
            and callback_deadline_at > 0
        )
    ):
        return operation_state in {
            "attention-required",
            "cancelling",
            "exiting",
            *TERMINAL,
        }
    return True


def enforce_callback_deadline(
    store: OperationStore,
    owner_id: str,
    operation_id: str,
    *,
    callback_handled: bool,
    now: float | None = None,
) -> bool:
    """Turn an expired live reviewer wait into durable typed attention."""

    record = store.read(owner_id, operation_id)
    if (
        callback_handled
        or record.spec.route.profile not in REVIEWER_PROFILES
        or record.state not in CALLBACK_WAIT_STATES
        or not record.deadline_at
    ):
        return False
    try:
        OperationSupervisor(
            store, owner_id, operation_id
        ).check_budget(
            now=now,
            timeout_reason=AttentionReason.CALLBACK_TIMEOUT,
        )
    except SupervisorError:
        current = store.read(owner_id, operation_id)
        return (
            current.state == "attention-required"
            and current.attention_reason
            == AttentionReason.CALLBACK_TIMEOUT
        )
    return False


def provider_argv(
    spec: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Select cmux's ephemeral wrapper only inside its exact owned surface."""

    argv = tuple(spec.get("argv") or ())
    runtime = str(spec.get("runtime") or "")
    surface_id = str(spec.get("surface_id") or "")
    if (
        not argv
        or runtime not in {"claude", "codex"}
        or not SURFACE_UUID.fullmatch(surface_id)
    ):
        return argv
    values = os.environ if env is None else env
    prefix = f"CMUX_{runtime.upper()}_WRAPPER_SHIM"
    raw_wrapper = str(values.get(prefix) or "").strip()
    raw_root = str(values.get(f"{prefix}_ROOT") or "").strip()
    if (
        not raw_wrapper
        or not raw_root
        or str(values.get("CMUX_SURFACE_ID") or "").casefold()
        != surface_id.casefold()
    ):
        return argv
    candidate = Path(raw_wrapper).expanduser()
    root = Path(raw_root).expanduser()
    try:
        if candidate.is_symlink() or root.is_symlink():
            return argv
        candidate = candidate.resolve()
        root = root.resolve()
        candidate_stat = candidate.stat()
        root_stat = root.stat()
    except OSError:
        return argv
    if (
        candidate.name != runtime
        or candidate.parent != root
        or root.name.casefold() != surface_id.casefold()
        or "cmux-cli-shims" not in root.parts
        or not candidate.is_file()
        or not root.is_dir()
        or not os.access(candidate, os.X_OK)
        or candidate_stat.st_uid != os.getuid()
        or root_stat.st_uid != os.getuid()
        or candidate_stat.st_mode & 0o022
        or root_stat.st_mode & 0o022
    ):
        return argv
    return (str(candidate), *argv[1:])


def provider_environment(
    spec: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a fresh environment, isolating protected research from the caller."""

    values = os.environ if env is None else env
    if spec.get("callback_mode") not in {
        "research-fetch",
        "research-synth",
    }:
        return dict(values)
    runtime_home = spec.get("runtime_home")
    if not isinstance(runtime_home, Path):
        raise RuntimeWorkerError("research runtime home is unavailable")
    temporary = runtime_home / "tmp"
    temporary.mkdir(mode=0o700, exist_ok=True)
    temporary.chmod(0o700)
    shell = "/bin/zsh" if Path("/bin/zsh").is_file() else "/bin/sh"
    return {
        "CODEX_HOME": str(runtime_home),
        "HOME": str(runtime_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": RESEARCH_PATH,
        "SHELL": shell,
        "TERM": "xterm-256color",
        "TMPDIR": str(temporary),
        "TZ": "UTC",
    }


def automate_prompt(
    store: OperationStore,
    owner_id: str,
    operation_id: str,
    runtime: str,
    surface_id: str,
    screen: str,
    cmux_adapter: object,
    *,
    closure_armed: bool = False,
) -> PromptDecision:
    """Apply only an exact prompt decision; unknown choices become durable."""

    decision = classify(runtime, screen, closure_armed=closure_armed)
    record = store.read(owner_id, operation_id)
    if record.state in TERMINAL or record.state == "attention-required":
        return decision
    if decision.recognized:
        try:
            for key in decision.keys:
                cmux_adapter.send_key(surface_id, key)
        except Exception:
            current = store.read(owner_id, operation_id)
            if current.state not in TERMINAL and current.state != "attention-required":
                store.transition(
                    owner_id,
                    operation_id,
                    "attention-required",
                    reason=AttentionReason.ATTENTION_REQUIRED,
                )
        return decision
    if decision.interactive:
        try:
            store.transition(
                owner_id,
                operation_id,
                "attention-required",
                reason=AttentionReason.PROMPT_UNKNOWN,
            )
        except Exception:
            pass
    return decision


def _atomic_json(path: Path, value: object) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_once_json(path: Path, value: object) -> None:
    encoded = (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise RuntimeWorkerError(
                "research input provenance is unreadable"
            ) from exc
        if existing != encoded:
            raise RuntimeWorkerError(
                "research input provenance changed"
            )
        return
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _research_input_provenance(
    spec: dict[str, Any],
    spec_path: Path,
    *,
    create: bool,
) -> str:
    if spec["callback_mode"] != "research-synth":
        return ""
    artifact_path = spec["cwd"] / "artifact.json"
    artifact = load_artifact(str(artifact_path))
    try:
        artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeWorkerError(
            "research input artifact is unreadable"
        ) from exc
    value = {
        "schema_version": 1,
        "operation_id": spec["operation_id"],
        "run_id": spec["run_id"],
        "fetch_run_id": artifact["run_id"],
        "request_sha256": artifact["request_sha256"],
        "artifact_sha256": artifact_sha256,
    }
    marker = spec_path.parent / "research-input.json"
    if marker.is_symlink():
        raise RuntimeWorkerError(
            "research input provenance must not be a symlink"
        )
    if create:
        _write_once_json(marker, value)
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeWorkerError(
            "research input provenance is unreadable"
        ) from exc
    if recorded != value:
        raise RuntimeWorkerError(
            "research input artifact changed after validation"
        )
    return artifact_sha256


def _absolute(value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise RuntimeWorkerError(f"{label} must be an absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeWorkerError(f"{label} must be an absolute path")
    return path.resolve()


def load_spec(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeWorkerError("runtime launch spec is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeWorkerError("runtime launch spec schema is invalid")
    for field in ("owner_id", "operation_id", "run_id"):
        if not IDENTIFIER.fullmatch(str(value.get(field) or "")):
            raise RuntimeWorkerError(f"runtime launch {field} is invalid")
    if not SURFACE_UUID.fullmatch(str(value.get("surface_id") or "")):
        raise RuntimeWorkerError("runtime launch surface identity is invalid")
    if value.get("runtime") not in {"claude", "codex"}:
        raise RuntimeWorkerError("runtime launch provider is invalid")
    callback_mode = str(value.get("callback_mode") or "envelope")
    if callback_mode not in {
        "envelope",
        "task-summary",
        "research-fetch",
        "research-synth",
    }:
        raise RuntimeWorkerError("runtime callback mode is invalid")
    argv = value.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(part, str) or "\0" in part for part in argv)
        or not Path(argv[0]).is_absolute()
    ):
        raise RuntimeWorkerError("runtime launch argv is invalid")
    cwd = _absolute(value.get("cwd"), "cwd")
    callback = _absolute(value.get("callback_pointer"), "callback_pointer")
    registration = _absolute(
        value.get("callback_registration"), "callback_registration"
    )
    task_summary: Path | None = None
    runtime_home: Path | None = None
    research_request_sha256 = str(
        value.get("research_request_sha256") or ""
    )
    callback_wake = str(value.get("callback_wake") or "")
    origin_surface = str(value.get("origin_surface") or "")
    if callback_mode == "task-summary":
        task_summary = _absolute(
            value.get("task_summary_pointer"), "task_summary_pointer"
        )
        if (
            task_summary.name != ".task-summary.json"
            or not SURFACE_UUID.fullmatch(origin_surface)
        ):
            raise RuntimeWorkerError(
                "task-summary source or origin identity is invalid"
            )
    if callback_mode in {"research-fetch", "research-synth"}:
        raw_runtime_home = value.get("runtime_home")
        if (
            value.get("runtime") != "codex"
            or not isinstance(raw_runtime_home, str)
            or not raw_runtime_home
            or Path(raw_runtime_home).expanduser().is_symlink()
            or not SURFACE_UUID.fullmatch(origin_surface)
            or not callback_wake
            or callback_wake != callback_wake.strip()
            or "\0" in callback_wake
            or "\n" in callback_wake
            or "\r" in callback_wake
            or len(callback_wake.encode()) > 4096
        ):
            raise RuntimeWorkerError("research launch identity is invalid")
        runtime_home = _absolute(raw_runtime_home, "runtime_home")
        try:
            runtime_stat = runtime_home.stat()
        except OSError as exc:
            raise RuntimeWorkerError(
                "research runtime home is unavailable"
            ) from exc
        if (
            not runtime_home.is_dir()
            or runtime_stat.st_uid != os.getuid()
            or runtime_stat.st_mode & 0o077
            or runtime_home == cwd
            or runtime_home in cwd.parents
            or cwd in runtime_home.parents
        ):
            raise RuntimeWorkerError(
                "research runtime home must be owner-only and disjoint"
            )
        expected_name = (
            "artifact.json"
            if callback_mode == "research-fetch"
            else "complete.json"
        )
        if callback.name != expected_name:
            raise RuntimeWorkerError(
                "research callback pointer is not canonical"
            )
        if callback_mode == "research-fetch":
            if not re.fullmatch(r"[0-9a-f]{64}", research_request_sha256):
                raise RuntimeWorkerError(
                    "research request digest is invalid"
                )
        elif research_request_sha256:
            raise RuntimeWorkerError(
                "research synth request digest must be derived"
            )
    elif value.get("runtime_home") or research_request_sha256 or callback_wake:
        raise RuntimeWorkerError(
            "research launch fields require research callback mode"
        )
    store_root = _absolute(value.get("store_root"), "store_root")
    ready = _absolute(value.get("ready_path"), "ready_path")
    exit_path = _absolute(value.get("exit_path"), "exit_path")
    if (
        ready.parent != path.parent
        or exit_path.parent != path.parent
        or registration.parent != path.parent
    ):
        raise RuntimeWorkerError("runtime worker markers escape launch state")
    try:
        callback.relative_to(cwd)
    except ValueError as exc:
        raise RuntimeWorkerError("runtime callback pointer escapes cwd") from exc
    if task_summary is not None:
        try:
            task_summary.relative_to(cwd)
        except ValueError as exc:
            raise RuntimeWorkerError("task summary pointer escapes cwd") from exc
    if not cwd.is_dir() or not store_root.is_dir():
        raise RuntimeWorkerError("runtime launch roots are unavailable")
    value.update(
        {
            "cwd": cwd,
            "callback_pointer": callback,
            "callback_registration": registration,
            "callback_mode": callback_mode,
            "task_summary_pointer": task_summary,
            "runtime_home": runtime_home,
            "research_request_sha256": research_request_sha256,
            "callback_wake": callback_wake,
            "origin_surface": origin_surface,
            "store_root": store_root,
            "ready_path": ready,
            "exit_path": exit_path,
        }
    )
    return value


def _callback_target(spec: dict[str, Any]) -> tuple[int, str, str, Path]:
    try:
        value = json.loads(
            spec["callback_registration"].read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeWorkerError("callback target registration is unreadable") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or type(value.get("generation")) is not int
        or int(value["generation"]) < 1
    ):
        raise RuntimeWorkerError("callback target registration is invalid")
    operation_id = str(value.get("operation_id") or "")
    run_id = str(value.get("run_id") or "")
    if not IDENTIFIER.fullmatch(operation_id) or not IDENTIFIER.fullmatch(run_id):
        raise RuntimeWorkerError("callback target identity is invalid")
    raw_pointer = value.get("callback_pointer")
    if not isinstance(raw_pointer, str) or not raw_pointer:
        raise RuntimeWorkerError("callback target pointer is invalid")
    pointer = Path(raw_pointer).expanduser()
    if not pointer.is_absolute():
        pointer = spec["cwd"] / pointer
    pointer = pointer.resolve()
    try:
        pointer.relative_to(spec["cwd"])
    except ValueError as exc:
        raise RuntimeWorkerError("callback target pointer escapes cwd") from exc
    return int(value["generation"]), operation_id, run_id, pointer


def _envelope(value: object) -> CallbackEnvelope:
    if not isinstance(value, dict):
        raise RuntimeWorkerError("callback envelope must be an object")
    return CallbackEnvelope(
        callback_id=value.get("callback_id", ""),
        operation_id=value.get("operation_id", ""),
        run_id=value.get("run_id", ""),
        kind=value.get("kind", ""),
        payload=value.get("payload", {}),
        payload_sha256=value.get("payload_sha256", ""),
        schema_version=value.get("schema_version", 0),
    )


def _reap_child(pid: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() <= deadline:
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True
        if waited == pid:
            return True
        time.sleep(0.02)
    return False


def _contain_provider_start_failure(
    process: ProcessAdapter, handle: ProcessHandle
) -> None:
    try:
        process.signal_owned_child_group(
            handle.process_group,
            handle.process_identity,
            signal.SIGTERM,
        )
    except ProcessError:
        pass
    if _reap_child(handle.pid, 0.5):
        return
    try:
        process.signal_owned_child_group(
            handle.process_group,
            handle.process_identity,
            signal.SIGKILL,
        )
    except ProcessError:
        pass
    _reap_child(handle.pid, 0.5)


def run(
    spec_path: Path,
    *,
    poll_seconds: float = 0.1,
    checkpoint_probe: Callable[[str, str], str] | None = None,
    cmux_adapter: object | None = None,
    review_launcher: Callable[[Path, Path], None] | None = None,
    verification_runner: Callable[
        ..., subprocess.CompletedProcess[str]
    ]
    | None = None,
) -> int:
    spec = load_spec(spec_path.resolve())
    ready = spec["ready_path"]
    exit_path = spec["exit_path"]
    store = OperationStore(spec["store_root"])
    process = ProcessAdapter()
    handle: ProcessHandle | None = None
    research_input_sha256 = ""
    if spec["callback_mode"] == "research-synth":
        try:
            research_input_sha256 = _research_input_provenance(
                spec,
                spec_path,
                create=True,
            )
        except (OSError, ResearchContractError, RuntimeWorkerError):
            try:
                store.transition(
                    spec["owner_id"],
                    spec["operation_id"],
                    "attention-required",
                    reason=AttentionReason.CALLBACK_INVALID,
                )
            except Exception:
                pass
            _atomic_json(
                ready,
                {"schema_version": 1, "status": "failed"},
            )
            _atomic_json(
                exit_path,
                {
                    "schema_version": 1,
                    "status": "research-input-invalid",
                    "exit_code": 2,
                },
            )
            return 2
    try:
        provider_env = provider_environment(spec)
        handle = process.start(
            provider_argv(spec),
            cwd=spec["cwd"],
            env=provider_env,
        )
        supervisor_identity = process.capture_identity(os.getpid())
        if not supervisor_identity:
            raise ProcessError("runtime worker identity is unavailable")
    except (OSError, ProcessError):
        if handle is not None:
            _contain_provider_start_failure(process, handle)
        _atomic_json(
            ready,
            {"schema_version": 1, "status": "failed"},
        )
        _atomic_json(
            exit_path,
            {"schema_version": 1, "status": "start-failed", "exit_code": 127},
        )
        return 127
    _atomic_json(
        ready,
        {
            "schema_version": 1,
            "status": "ready",
            "pid": handle.pid,
            "process_group": handle.process_group,
            "supervisor_pid": os.getpid(),
            "process_identity": handle.process_identity,
            "supervisor_identity": supervisor_identity,
        },
    )
    checkpoint_probe = checkpoint_probe or CmuxAdapter().resume_checkpoint
    checkpoint = ""
    next_checkpoint_probe = 0.0

    active_target: tuple[int, str, str, Path] | None = None
    last_digest = ""
    stable_reads = 0
    callback_handled = False
    registration_invalid = False
    summary_digest = ""
    summary_stable_reads = 0
    summary_attention_revision = -1
    cmux_adapter = cmux_adapter or CmuxAdapter()
    operation_contract = store.read(
        spec["owner_id"], spec["operation_id"]
    ).spec.contract_sha256
    try:
        _pipeline_name, pipeline = compiled_executable_for_contract(
            operation_contract
        )
    except ValueError:
        _pipeline_name, pipeline = "", None
    last_prompt_digest = ""
    next_prompt_probe = 0.0
    handled_control_id = ""
    invalid_control_digest = ""
    fix_callback_digest = ""
    fix_callback_stable_reads = 0
    fix_transport_complete = _pipeline_name != "engineering/fix"

    def inspect_control() -> None:
        nonlocal handled_control_id, invalid_control_digest
        control_path = spec_path.parent / "process-control.json"
        try:
            raw = control_path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            raw = b""
        digest = hashlib.sha256(raw).hexdigest()
        if digest == invalid_control_digest:
            return
        try:
            if not raw or len(raw) > MAX_OUTBOX_BYTES:
                raise RuntimeWorkerError(
                    "process guardian command is invalid"
                )
            command = json.loads(raw)
            if not isinstance(command, dict):
                raise RuntimeWorkerError(
                    "process guardian command must be an object"
                )
            command_id = str(command.get("command_id") or "")
            unsigned = dict(command)
            unsigned.pop("command_id", None)
            encoded = json.dumps(
                unsigned, sort_keys=True, separators=(",", ":")
            ).encode()
            expected_id = hashlib.sha256(encoded).hexdigest()
            action = command.get("action")
            if (
                set(command)
                != {
                    "schema_version",
                    "action",
                    "operation_id",
                    "run_id",
                    "process_group",
                    "process_identity",
                    "supervisor_pid",
                    "supervisor_identity",
                    "command_id",
                }
                or command.get("schema_version") != 1
                or action not in {"request-exit", "terminate"}
                or command.get("operation_id") != spec["operation_id"]
                or command.get("run_id") != spec["run_id"]
                or command.get("process_group") != handle.process_group
                or command.get("process_identity")
                != handle.process_identity
                or command.get("supervisor_pid") != os.getpid()
                or command.get("supervisor_identity")
                != supervisor_identity
                or command_id != expected_id
            ):
                raise RuntimeWorkerError(
                    "process guardian command identity mismatches"
                )
            if command_id == handled_control_id:
                return
            process.signal_owned_child_group(
                handle.process_group,
                handle.process_identity,
                (
                    signal.SIGTERM
                    if action == "request-exit"
                    else signal.SIGKILL
                ),
            )
            handled_control_id = command_id
            _atomic_json(
                spec_path.parent / "process-control-receipt.json",
                {
                    "schema_version": 1,
                    "command_id": command_id,
                    "action": action,
                    "status": "accepted",
                },
            )
        except (
            json.JSONDecodeError,
            OSError,
            ProcessError,
            RuntimeWorkerError,
            TypeError,
            ValueError,
        ):
            invalid_control_digest = digest
            try:
                store.transition(
                    spec["owner_id"],
                    spec["operation_id"],
                    "attention-required",
                    reason=AttentionReason.ATTENTION_REQUIRED,
                )
            except Exception:
                pass
            _atomic_json(
                spec_path.parent / "process-control-error.json",
                {"schema_version": 1, "status": "invalid"},
            )

    def inspect_prompt() -> None:
        nonlocal last_prompt_digest
        try:
            record = store.read(spec["owner_id"], spec["operation_id"])
        except Exception:
            return
        if record.resources.surface_id != spec["surface_id"]:
            return
        reader = getattr(cmux_adapter, "read", None)
        if reader is None:
            return
        try:
            screen = str(reader(spec["surface_id"]))
        except Exception:
            return
        encoded = screen.encode("utf-8", errors="replace")
        if not encoded or len(encoded) > MAX_SCREEN_BYTES:
            return
        digest = hashlib.sha256(encoded).hexdigest()
        if digest == last_prompt_digest:
            return
        decision = classify(
            spec["runtime"],
            screen,
            closure_armed=record.state == "exiting",
        )
        if not decision.interactive:
            return
        last_prompt_digest = digest
        automate_prompt(
            store,
            spec["owner_id"],
            spec["operation_id"],
            spec["runtime"],
            spec["surface_id"],
            screen,
            cmux_adapter,
            closure_armed=record.state == "exiting",
        )

    def inspect_callback() -> None:
        nonlocal active_target, last_digest, stable_reads, callback_handled
        nonlocal registration_invalid
        try:
            target = _callback_target(spec)
        except RuntimeWorkerError:
            if not registration_invalid:
                registration_invalid = True
                try:
                    store.transition(
                        spec["owner_id"],
                        spec["operation_id"],
                        "attention-required",
                        reason=AttentionReason.CALLBACK_INVALID,
                    )
                except Exception:
                    pass
                _atomic_json(
                    spec_path.parent / "callback-error.json",
                    {"schema_version": 1, "status": "callback-target-invalid"},
                )
            return
        registration_invalid = False
        if target != active_target:
            if active_target is not None and target[0] <= active_target[0]:
                return
            active_target = target
            last_digest = ""
            stable_reads = 0
            callback_handled = False
        if callback_handled:
            return
        generation, operation_id, run_id, callback_path = target
        try:
            raw = callback_path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            raw = b""
        if not raw or len(raw) > MAX_OUTBOX_BYTES:
            return
        digest = hashlib.sha256(raw).hexdigest()
        if digest != last_digest:
            last_digest = digest
            stable_reads = 1
            return
        stable_reads += 1
        if stable_reads < 2:
            return
        callback_handled = True
        try:
            envelope = _envelope(json.loads(raw))
            if (
                envelope.operation_id != operation_id
                or envelope.run_id != run_id
            ):
                raise RuntimeWorkerError("callback identity mismatches runtime launch")
            acceptance = CallbackBroker(
                store, spec["owner_id"]
            ).accept(
                envelope,
                deadline_operation_id=spec["operation_id"],
            )
            _atomic_json(
                spec_path.parent / "callback-receipt.json",
                {
                    "schema_version": 1,
                    "generation": generation,
                    "callback_id": envelope.callback_id,
                    "operation_id": operation_id,
                    "status": (
                        "duplicate" if acceptance.duplicate else "accepted"
                    ),
                },
            )
        except CallbackTimeoutError:
            _atomic_json(
                spec_path.parent / "callback-timeout.json",
                {
                    "schema_version": 1,
                    "operation_id": spec["operation_id"],
                    "run_id": spec["run_id"],
                    "status": "attention-required",
                },
            )
        except (
            CallbackError,
            RuntimeWorkerError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            try:
                store.transition(
                    spec["owner_id"],
                    spec["operation_id"],
                    "attention-required",
                    reason=AttentionReason.CALLBACK_INVALID,
                )
            except Exception:
                pass
            _atomic_json(
                spec_path.parent / "callback-error.json",
                {"schema_version": 1, "status": "callback-invalid"},
            )

    def summary_attention(
        status: str,
        reason: AttentionReason = AttentionReason.CALLBACK_INVALID,
    ) -> None:
        nonlocal callback_handled, summary_attention_revision
        callback_handled = True
        try:
            store.transition(
                spec["owner_id"],
                spec["operation_id"],
                "attention-required",
                reason=reason,
            )
        except Exception:
            pass
        try:
            current = store.read(
                spec["owner_id"], spec["operation_id"]
            )
            if current.state == "attention-required":
                summary_attention_revision = current.revision
        except Exception:
            pass
        _atomic_json(
            spec_path.parent / "callback-error.json",
            {"schema_version": 1, "status": status},
        )

    def write_immutable_json(path: Path, value: dict[str, object]) -> None:
        encoded = (
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise RuntimeWorkerError("immutable runtime receipt cannot be a symlink")
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            try:
                current = path.read_bytes()
            except OSError as exc:
                raise RuntimeWorkerError(
                    "immutable runtime receipt is unreadable"
                ) from exc
            if current != encoded:
                raise RuntimeWorkerError(
                    "immutable runtime receipt changed"
                )
            return
        try:
            with os.fdopen(descriptor, "wb") as handle_file:
                handle_file.write(encoded)
                handle_file.flush()
                os.fsync(handle_file.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def git_head() -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=spec["cwd"],
            text=True,
            capture_output=True,
            check=False,
        )
        head = result.stdout.strip()
        if result.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", head):
            raise RuntimeWorkerError("engineering/fix HEAD is unavailable")
        return head

    def retarget_fix_callback(
        *,
        operation_id: str,
        run_id: str,
        callback_pointer: str,
    ) -> None:
        generation, current_operation, current_run, current_pointer = (
            _callback_target(spec)
        )
        expected_pointer = (spec["cwd"] / callback_pointer).resolve()
        if (
            current_operation == operation_id
            and current_run == run_id
            and current_pointer == expected_pointer
        ):
            return
        if current_operation != spec["operation_id"]:
            current_child = store.read(spec["owner_id"], current_operation)
            if current_child.state != "complete":
                raise RuntimeWorkerError(
                    "engineering/fix callback target changed before acceptance"
                )
        if expected_pointer.exists() or expected_pointer.is_symlink():
            if expected_pointer.is_symlink() or not expected_pointer.is_file():
                raise RuntimeWorkerError(
                    "engineering/fix callback outbox is not reusable"
                )
            expected_pointer.unlink()
        _atomic_json(
            spec["callback_registration"],
            {
                "schema_version": 1,
                "generation": generation + 1,
                "operation_id": operation_id,
                "run_id": run_id,
                "callback_pointer": callback_pointer,
            },
        )

    def notify_fix_phase(request: dict[str, object]) -> None:
        operation_id = str(request["operation_id"])
        notify_path = (
            spec_path.parent
            / "pipeline-fix"
            / "notifications"
            / f"{operation_id}.json"
        )
        marker = {
            "schema_version": 1,
            "operation_id": operation_id,
            "step_id": str(request["step_id"]),
            "status": "sent",
        }
        if notify_path.is_file() and not notify_path.is_symlink():
            if json.loads(notify_path.read_text(encoding="utf-8")) != marker:
                raise RuntimeWorkerError(
                    "engineering/fix phase notification changed"
                )
            return
        message = (
            "Typed engineering/fix phase "
            f"{request['step_id']} is ready in "
            ".task-pipeline-step-request.json. Complete only this phase. "
            f"Write evidence to {request['output_pointer']} and write "
            f"{request['result_pointer']} as exact JSON with fields "
            '{"schema_version":1,"status":"complete",'
            '"output_sha256":"<sha256-of-evidence>",'
            '"head_sha":"<current-git-head>"}. For the reproduce phase only, '
            'status may instead be "cannot-reproduce". Then publish the '
            "request-bound callback with pipeline-step-submit.py. "
            "Remain in this same session for the next typed request."
        )
        if len(message.encode()) > 4096:
            raise RuntimeWorkerError(
                "engineering/fix phase notification exceeds its bound"
            )
        cmux_adapter.send(spec["surface_id"], message)
        cmux_adapter.send_key(spec["surface_id"], "Enter")
        write_immutable_json(notify_path, marker)

    def notify_fix_finalization() -> bool:
        notify_path = (
            spec_path.parent
            / "pipeline-fix"
            / "finalization-notify.json"
        )
        marker = {
            "schema_version": 1,
            "operation_id": spec["operation_id"],
            "status": "sent",
        }
        if notify_path.is_file() and not notify_path.is_symlink():
            if json.loads(notify_path.read_text(encoding="utf-8")) != marker:
                raise RuntimeWorkerError(
                    "engineering/fix finalization notification changed"
                )
            return False
        message = (
            "All four typed engineering/fix phase receipts are accepted. "
            "Finish the task in this same session: commit the minimal fix, "
            "run the approved scoped verification, and write the canonical "
            ".task-summary.json. Do not repeat an accepted phase."
        )
        cmux_adapter.send(spec["surface_id"], message)
        cmux_adapter.send_key(spec["surface_id"], "Enter")
        write_immutable_json(notify_path, marker)
        return True

    def drive_fix_transport() -> None:
        nonlocal fix_callback_digest, fix_callback_stable_reads
        nonlocal fix_transport_complete
        if (
            _pipeline_name != "engineering/fix"
            or callback_handled
            or fix_transport_complete
        ):
            return
        try:
            meta_path = spec["cwd"] / ".task-meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            policy = (
                meta.get("pipeline_policy")
                if isinstance(meta, dict)
                else None
            )
            if (
                not isinstance(policy, dict)
                or policy.get("name") != "engineering/fix"
                or pipeline is None
                or policy.get("definition_sha256")
                != pipeline.definition_sha256
            ):
                raise RuntimeWorkerError(
                    "engineering/fix metadata mismatches its compiled contract"
                )
            approved_plan_sha256 = str(
                meta.get("approved_plan_sha256") or ""
            )
            controller_path = (
                spec_path.parent / "pipeline-fix" / "controller.json"
            )
            if controller_path.is_symlink():
                raise RuntimeWorkerError(
                    "engineering/fix controller must not be a symlink"
                )
            if controller_path.is_file():
                controller = json.loads(
                    controller_path.read_text(encoding="utf-8")
                )
                if (
                    not isinstance(controller, dict)
                    or set(controller)
                    != {
                        "schema_version",
                        "operation_id",
                        "definition_sha256",
                        "approved_plan_sha256",
                        "initial_head_sha",
                        "iteration",
                    }
                    or controller.get("schema_version") != 1
                    or controller.get("operation_id")
                    != spec["operation_id"]
                    or controller.get("definition_sha256")
                    != pipeline.definition_sha256
                    or controller.get("approved_plan_sha256")
                    != approved_plan_sha256
                    or controller.get("iteration") != 0
                ):
                    raise RuntimeWorkerError(
                        "engineering/fix controller receipt changed"
                    )
            else:
                controller = {
                    "schema_version": 1,
                    "operation_id": spec["operation_id"],
                    "definition_sha256": pipeline.definition_sha256,
                    "approved_plan_sha256": approved_plan_sha256,
                    "initial_head_sha": git_head(),
                    "iteration": 0,
                }
                write_immutable_json(controller_path, controller)
            initial_head_sha = str(controller["initial_head_sha"])
            parent = store.read(spec["owner_id"], spec["operation_id"])
            receipt_root = (
                spec_path.parent / "pipeline-fix" / "pass-0"
            )
            receipts: list[FixStepReceipt] = []
            for step_id in (
                "reproduce",
                "root-cause",
                "regression-test",
                "minimal-fix",
            ):
                receipt_path = receipt_root / step_id / "receipt.json"
                if not receipt_path.is_file():
                    break
                receipts.append(load_receipt(receipt_path))
            progress = reconcile_fix(
                parent,
                definition_sha256=pipeline.definition_sha256,
                approved_plan_sha256=approved_plan_sha256,
                initial_head_sha=initial_head_sha,
                receipts=tuple(receipts),
                iteration=0,
            )
            if progress.action == "attention":
                emit_compiled_pipeline_event(
                    spec["cwd"],
                    event="fix-phase-attention",
                    pipeline_id=pipeline.definition.pipeline_id,
                    pipeline_version=pipeline.definition.version,
                    profile=pipeline.definition.profile,
                    compiler_outcome="resolved",
                    definition_sha=pipeline.definition_sha256,
                    primitive_count=len(pipeline.definition.steps),
                    loop_iteration=0,
                    attention_category="cannot-reproduce",
                )
                summary_attention(
                    "pipeline-fix-cannot-reproduce",
                    AttentionReason.ATTENTION_REQUIRED,
                )
                return
            if progress.action == "complete":
                retarget_fix_callback(
                    operation_id=spec["operation_id"],
                    run_id=spec["run_id"],
                    callback_pointer=".task-summary.json",
                )
                if notify_fix_finalization():
                    emit_compiled_pipeline_event(
                        spec["cwd"],
                        event="fix-final-retarget",
                        pipeline_id=pipeline.definition.pipeline_id,
                        pipeline_version=pipeline.definition.version,
                        profile=pipeline.definition.profile,
                        compiler_outcome="resolved",
                        definition_sha=pipeline.definition_sha256,
                        primitive_count=len(pipeline.definition.steps),
                        loop_iteration=0,
                        terminal_category="phases-complete",
                    )
                fix_transport_complete = True
                return
            if spec["task_summary_pointer"].is_file():
                _atomic_json(
                    spec_path.parent
                    / "pipeline-fix"
                    / "early-summary.json",
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "status": "ignored-until-phases-complete",
                    },
                )
            round_ = prepare_next_phase(
                store,
                parent,
                definition_sha256=pipeline.definition_sha256,
                approved_plan_sha256=approved_plan_sha256,
                initial_head_sha=initial_head_sha,
                receipts=tuple(receipts),
                iteration=0,
            )
            result_pointer = (
                f".task-pipeline/results/pass-0/{round_.step_id}.json"
            )
            output_pointer = (
                f".task-pipeline/outputs/pass-0/{round_.step_id}.md"
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
            _atomic_json(
                spec["cwd"] / ".task-pipeline-step-request.json",
                request,
            )
            retarget_fix_callback(
                operation_id=round_.spec.operation_id,
                run_id=round_.run_id,
                callback_pointer=".task-pipeline-step-callback.json",
            )
            notify_fix_phase(request)
            _generation, operation_id, run_id, callback_path = (
                _callback_target(spec)
            )
            if (
                operation_id != round_.spec.operation_id
                or run_id != round_.run_id
            ):
                raise RuntimeWorkerError(
                    "engineering/fix active callback target changed"
                )
            try:
                raw = callback_path.read_bytes()
            except FileNotFoundError:
                return
            if not raw or len(raw) > MAX_OUTBOX_BYTES:
                raise RuntimeWorkerError(
                    "engineering/fix phase callback is invalid"
                )
            digest = hashlib.sha256(raw).hexdigest()
            if digest != fix_callback_digest:
                fix_callback_digest = digest
                fix_callback_stable_reads = 1
                return
            fix_callback_stable_reads += 1
            if fix_callback_stable_reads < 2:
                return
            envelope = _envelope(json.loads(raw))
            receipt_path = (
                receipt_root
                / round_.step_id
                / "receipt.json"
            )
            accepted_receipt = accept_phase(
                store,
                round_,
                envelope,
                current_head_sha=git_head(),
                receipt_path=receipt_path,
            )
            callback_path.unlink()
            emit_compiled_pipeline_event(
                spec["cwd"],
                event="fix-phase-accepted",
                pipeline_id=pipeline.definition.pipeline_id,
                pipeline_version=pipeline.definition.version,
                profile=pipeline.definition.profile,
                compiler_outcome="resolved",
                definition_sha=pipeline.definition_sha256,
                primitive_count=len(pipeline.definition.steps),
                loop_iteration=accepted_receipt.iteration,
                terminal_category=accepted_receipt.step_id,
            )
            fix_callback_digest = ""
            fix_callback_stable_reads = 0
        except (
            FixWorkflowError,
            RuntimeWorkerError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            summary_attention("pipeline-fix-callback-invalid")

    def recover_task_summary_attention() -> None:
        nonlocal callback_handled, summary_digest, summary_stable_reads
        nonlocal summary_attention_revision
        if (
            spec["callback_mode"] != "task-summary"
            or not callback_handled
            or summary_attention_revision < 0
        ):
            return
        try:
            current = store.read(
                spec["owner_id"], spec["operation_id"]
            )
        except Exception:
            return
        if (
            current.state not in CALLBACK_WAIT_STATES
            or current.revision <= summary_attention_revision
        ):
            return
        _atomic_json(
            spec_path.parent / "callback-recovery.json",
            {
                "schema_version": 1,
                "operation_id": spec["operation_id"],
                "attention_revision": summary_attention_revision,
                "resumed_revision": current.revision,
                "status": "resumed",
            },
        )
        callback_handled = False
        summary_digest = ""
        summary_stable_reads = 0
        summary_attention_revision = -1

    def inspect_task_summary() -> None:
        nonlocal callback_handled, summary_digest, summary_stable_reads
        if callback_handled:
            return
        if _pipeline_name == "engineering/fix" and not fix_transport_complete:
            return
        summary_path: Path = spec["task_summary_pointer"]
        try:
            raw = summary_path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            summary_attention("wiki-summary-unreadable")
            return
        if not raw or len(raw) > MAX_OUTBOX_BYTES:
            summary_attention("wiki-summary-invalid")
            return
        finish_task_summary(raw)

    def inspect_research() -> None:
        nonlocal active_target, last_digest, stable_reads, callback_handled
        if callback_handled:
            return
        try:
            target = _callback_target(spec)
        except RuntimeWorkerError:
            summary_attention("research-callback-invalid")
            return
        if target != active_target:
            if active_target is not None and target[0] <= active_target[0]:
                return
            active_target = target
            last_digest = ""
            stable_reads = 0
        generation, operation_id, run_id, callback_path = target
        try:
            raw = callback_path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            summary_attention("research-callback-unreadable")
            return
        if not raw or len(raw) > MAX_OUTBOX_BYTES:
            summary_attention("research-callback-invalid")
            return
        digest = hashlib.sha256(raw).hexdigest()
        if digest != last_digest:
            last_digest = digest
            stable_reads = 1
            return
        stable_reads += 1
        if stable_reads < 2:
            return
        try:
            if spec["callback_mode"] == "research-fetch":
                artifact = load_artifact(
                    str(callback_path),
                    expected_run_id=run_id,
                    expected_request_sha256=spec[
                        "research_request_sha256"
                    ],
                )
                payload = {
                    "stage": "fetch",
                    "artifact_path": "artifact.json",
                    "artifact_sha256": digest,
                    "source_count": len(artifact["sources"]),
                }
            else:
                if (
                    not research_input_sha256
                    or _research_input_provenance(
                        spec,
                        spec_path,
                        create=False,
                    )
                    != research_input_sha256
                ):
                    raise RuntimeWorkerError(
                        "research input artifact changed after launch"
                    )
                artifact = load_artifact(str(spec["cwd"] / "artifact.json"))
                complete = json.loads(raw)
                result = validate_result_artifact(
                    complete,
                    root=spec["cwd"],
                    expected_run_id=run_id,
                    source_urls={
                        str(source["url"])
                        for source in artifact["sources"]
                    },
                )
                payload = {
                    "stage": "synth",
                    "artifact_path": result["artifact"]["path"],
                    "artifact_sha256": result["artifact"]["sha256"],
                    "citation_count": len(
                        result["artifact"]["citations"]
                    ),
                }
            encoded = json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            ).encode()
            payload_sha256 = hashlib.sha256(encoded).hexdigest()
            envelope = CallbackEnvelope(
                callback_id=(
                    f"research-{payload['stage']}-"
                    f"{payload_sha256[:24]}"
                ),
                operation_id=operation_id,
                run_id=run_id,
                kind="research",
                payload=payload,
                payload_sha256=payload_sha256,
            )
            acceptance = CallbackBroker(
                store, spec["owner_id"]
            ).accept(envelope)
            callback_handled = True
            _atomic_json(
                spec_path.parent / "callback-receipt.json",
                {
                    "schema_version": 1,
                    "generation": generation,
                    "callback_id": envelope.callback_id,
                    "operation_id": operation_id,
                    "status": (
                        "duplicate" if acceptance.duplicate else "accepted"
                    ),
                },
            )
            notify_path = spec_path.parent / "research-notify.json"
            if notify_path.exists():
                marker = json.loads(notify_path.read_text(encoding="utf-8"))
                if (
                    marker.get("schema_version") != 1
                    or marker.get("callback_id") != envelope.callback_id
                ):
                    raise RuntimeWorkerError(
                        "research notification marker is invalid"
                    )
                if marker.get("status") == "sent":
                    return
                if marker.get("status") == "pending":
                    store.transition(
                        spec["owner_id"],
                        spec["operation_id"],
                        "attention-required",
                        reason=AttentionReason.ATTENTION_REQUIRED,
                    )
                    return
                raise RuntimeWorkerError(
                    "research notification marker state is invalid"
                )
            _atomic_json(
                notify_path,
                {
                    "schema_version": 1,
                    "callback_id": envelope.callback_id,
                    "status": "pending",
                },
            )
            cmux_adapter.send(
                spec["origin_surface"], spec["callback_wake"]
            )
            cmux_adapter.send_key(spec["origin_surface"], "Enter")
            _atomic_json(
                notify_path,
                {
                    "schema_version": 1,
                    "callback_id": envelope.callback_id,
                    "status": "sent",
                },
            )
        except (
            CallbackError,
            ResearchContractError,
            RuntimeWorkerError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            summary_attention("research-callback-invalid")
            return

    def finish_task_summary(raw: bytes) -> None:
        nonlocal callback_handled, summary_digest, summary_stable_reads
        digest = hashlib.sha256(raw).hexdigest()
        if digest != summary_digest:
            summary_digest = digest
            summary_stable_reads = 1
            return
        summary_stable_reads += 1
        if summary_stable_reads < 2:
            return
        try:
            raw_summary = json.loads(raw)
            summary = validate_summary(
                raw_summary, allow_missing_session=True, require_schema=True
            )
            meta_path = spec["cwd"] / ".task-meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(meta, dict) or meta.get("version") != 3:
                raise RuntimeWorkerError("task summary requires v3 metadata")
            if (
                meta.get("task_id") != spec["operation_id"]
                or Path(str(meta.get("worktree") or "")).resolve()
                != spec["cwd"]
                or meta.get("task_surface") != spec["surface_id"]
            ):
                raise RuntimeWorkerError(
                    "task summary metadata mismatches the runtime owner"
                )
            current_session = str(meta.get("origin_session") or "")
            validate_handoff(meta, summary, current_session)
            trusted_store = spec["store_root"]
            trusted_vault = trusted_store.parent.parent
            if trusted_store != trusted_vault / ".vault-meta" / "harness":
                raise RuntimeWorkerError(
                    "task summary store is not the trusted vault harness"
                )
            review = task_review_status(
                meta,
                spec["cwd"],
                expected_vault=trusted_vault,
                expected_operation_id=spec["operation_id"],
            )
            operation = store.read(
                spec["owner_id"], spec["operation_id"]
            )
            if (
                pipeline is None
                or operation.spec.contract_sha256
                != pipeline.definition_sha256
            ):
                summary_attention(
                    "pipeline-contract-drift",
                    AttentionReason.CONTRACT_DRIFT,
                )
                return
            marker_path = spec_path.parent / "pipeline-review-start.json"
            marker = None
            if marker_path.is_file() and not marker_path.is_symlink():
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                if (
                    marker.get("schema_version") != 1
                    or marker.get("operation_id") != spec["operation_id"]
                    or marker.get("definition_sha256")
                    != pipeline.definition_sha256
                    or marker.get("status") not in {"pending", "started"}
                ):
                    raise RuntimeWorkerError(
                        "pipeline review launch receipt is invalid"
                    )

            def review_drive_sha256() -> str:
                digest = hashlib.sha256()
                gate_state = review.gate_root / "review-gate.json"
                if gate_state.is_file():
                    if gate_state.is_symlink():
                        raise RuntimeWorkerError(
                            "review gate state cannot be a symlink"
                        )
                    digest.update(gate_state.read_bytes())
                head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=spec["cwd"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if head.returncode:
                    raise RuntimeWorkerError(
                        "automatic review cannot resolve product HEAD"
                    )
                digest.update(head.stdout.strip().encode())
                callback_root = (
                    trusted_vault
                    / ".vault-meta"
                    / "harness"
                    / "review-runtime"
                    / spec["operation_id"]
                    / "callbacks"
                )
                if callback_root.is_dir():
                    for callback in sorted(
                        callback_root.rglob(".review-callback.json")
                    ):
                        if callback.is_symlink():
                            raise RuntimeWorkerError(
                                "review callback cannot be a symlink"
                            )
                        digest.update(
                            callback.relative_to(callback_root)
                            .as_posix()
                            .encode()
                        )
                        digest.update(callback.read_bytes())
                return digest.hexdigest()

            def drive_review() -> bool:
                input_sha256 = review_drive_sha256()
                _atomic_json(
                    marker_path,
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "definition_sha256": pipeline.definition_sha256,
                        "status": "pending",
                        "drive_sha256": input_sha256,
                    },
                )
                try:
                    if review_launcher is not None:
                        review_launcher(trusted_vault, spec["cwd"])
                    else:
                        runner = (
                            trusted_vault
                            / "scripts"
                            / "task-review-runner.py"
                        )
                        if not runner.is_file() or runner.is_symlink():
                            raise RuntimeWorkerError(
                                "trusted task review runner is unavailable"
                            )
                        launched = subprocess.run(
                            [
                                sys.executable,
                                str(runner),
                                "run",
                                "--worktree",
                                str(spec["cwd"]),
                            ],
                            cwd=trusted_vault,
                            text=True,
                            capture_output=True,
                            check=False,
                            timeout=10,
                        )
                        if launched.returncode != 0:
                            raise RuntimeWorkerError(
                                "automatic task review drive failed"
                            )
                except (
                    OSError,
                    RuntimeWorkerError,
                    subprocess.TimeoutExpired,
                ):
                    summary_attention(
                        "review-drive-failed",
                        AttentionReason.ATTENTION_REQUIRED,
                    )
                    return False
                _atomic_json(
                    marker_path,
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "definition_sha256": pipeline.definition_sha256,
                        "status": "started",
                        "drive_sha256": input_sha256,
                    },
                )
                return True

            def review_gate_state() -> dict[str, object]:
                gate_path = review.gate_root / "review-gate.json"
                if not gate_path.is_file() or gate_path.is_symlink():
                    return {}
                state = json.loads(gate_path.read_text(encoding="utf-8"))
                if (
                    not isinstance(state, dict)
                    or state.get("schema_version") != 1
                    or state.get("dispatch_operation_id")
                    != spec["operation_id"]
                ):
                    raise RuntimeWorkerError(
                        "review gate state is invalid"
                    )
                return state

            def notify_review_resolution(
                gate_state: dict[str, object],
            ) -> None:
                awaiting = gate_state.get("awaiting_resolution")
                if not isinstance(awaiting, dict) or not awaiting:
                    raise RuntimeWorkerError(
                        "review resolution evidence is unavailable"
                    )
                findings: list[dict[str, object]] = []
                reviewed_heads: set[str] = set()
                for axis in sorted(awaiting):
                    evidence = awaiting[axis]
                    if not isinstance(evidence, dict):
                        raise RuntimeWorkerError(
                            "review resolution evidence is invalid"
                        )
                    pointer = Path(str(evidence.get("pointer") or ""))
                    result_path = (review.gate_root / pointer).resolve()
                    if (
                        pointer.is_absolute()
                        or review.gate_root not in result_path.parents
                        or not result_path.is_file()
                        or result_path.is_symlink()
                    ):
                        raise RuntimeWorkerError(
                            "review result pointer is invalid"
                        )
                    result = json.loads(
                        result_path.read_text(encoding="utf-8")
                    )
                    rows = (
                        result.get("findings")
                        if isinstance(result, dict)
                        else None
                    )
                    if (
                        not isinstance(result, dict)
                        or result.get("axis") != axis
                        or not isinstance(rows, list)
                    ):
                        raise RuntimeWorkerError(
                            "review result evidence is invalid"
                        )
                    for finding in rows:
                        if not isinstance(finding, dict):
                            raise RuntimeWorkerError(
                                "review finding evidence is invalid"
                            )
                        findings.append(dict(finding))
                    reviewed_heads.add(
                        str(evidence.get("reviewed_head_sha") or "")
                    )
                if (
                    not findings
                    or len(findings) > 50
                    or len(reviewed_heads) != 1
                    or "" in reviewed_heads
                ):
                    raise RuntimeWorkerError(
                        "review decision packet is invalid"
                    )
                packet = {
                    "schema_version": 1,
                    "operation_id": spec["operation_id"],
                    "reviewed_head_sha": next(iter(reviewed_heads)),
                    "allowed_responses": [
                        "applied",
                        "rejected",
                        "escalated",
                    ],
                    "findings": findings,
                }
                encoded = json.dumps(
                    packet, sort_keys=True, separators=(",", ":")
                ).encode()
                if len(encoded) > MAX_OUTBOX_BYTES:
                    raise RuntimeWorkerError(
                        "review decision packet exceeds size cap"
                    )
                packet_sha256 = hashlib.sha256(encoded).hexdigest()
                packet_path = spec["cwd"] / ".task-review.json"
                if packet_path.is_symlink():
                    raise RuntimeWorkerError(
                        "review decision packet cannot be a symlink"
                    )
                if packet_path.exists():
                    current = json.loads(
                        packet_path.read_text(encoding="utf-8")
                    )
                    if (
                        not isinstance(current, dict)
                        or current.get("schema_version") != 1
                        or current.get("operation_id")
                        != spec["operation_id"]
                    ):
                        raise RuntimeWorkerError(
                            "review decision packet identity changed"
                        )
                _atomic_json(packet_path, packet)
                notify_path = (
                    spec_path.parent
                    / "pipeline-review-resolution-notify.json"
                )
                notified = None
                if notify_path.is_file() and not notify_path.is_symlink():
                    notified = json.loads(
                        notify_path.read_text(encoding="utf-8")
                    )
                    if (
                        not isinstance(notified, dict)
                        or notified.get("schema_version") != 1
                        or notified.get("operation_id")
                        != spec["operation_id"]
                    ):
                        raise RuntimeWorkerError(
                            "review resolution notification is invalid"
                        )
                    if (
                        notified.get("packet_sha256") == packet_sha256
                        and notified.get("status") == "sent"
                    ):
                        return
                _atomic_json(
                    notify_path,
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "packet_sha256": packet_sha256,
                        "status": "pending",
                    },
                )
                message = (
                    "Typed review findings are ready in "
                    f"{packet_path.name}. Resolve every finding as applied or "
                    "rejected and commit a new HEAD; for escalation use the "
                    "task_escalation.py raise contract. Do not launch review. "
                    "Remain available for same-session verification."
                )
                if len(message.encode()) > 4096:
                    raise RuntimeWorkerError(
                        "review resolution notification is too large"
                    )
                cmux_adapter.send(spec["surface_id"], message)
                cmux_adapter.send_key(spec["surface_id"], "Enter")
                _atomic_json(
                    notify_path,
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "packet_sha256": packet_sha256,
                        "status": "sent",
                    },
                )

            steps = pipeline.definition.steps
            primitive_shape = tuple(
                step.primitive_id for step in steps
            )
            if primitive_shape not in {
                ("model_step", "review"),
                ("model_step", "verify", "review"),
                (
                    "model_step",
                    "model_step",
                    "model_step",
                    "model_step",
                    "verify",
                    "review",
                ),
            }:
                raise RuntimeWorkerError(
                    "compiled production pipeline shape is unsupported"
                )
            verify_step = next(
                (
                    step
                    for step in steps
                    if step.primitive_id == "verify"
                ),
                None,
            )
            verification_controller_receipt_path = (
                spec_path.parent / "pipeline-step-verify.json"
            )
            review_policy = meta.get("review_policy")
            if not isinstance(review_policy, dict):
                raise RuntimeWorkerError(
                    "task verification policy is unavailable"
                )
            profiles = load_profiles(
                trusted_vault / "config" / "verification-profiles.toml"
            )
            profile_name = str(
                review_policy.get("verification_profile") or ""
            )
            profile = profiles.get(profile_name)
            if (
                profile is None
                or profile.sha256
                != review_policy.get("verification_profile_sha256")
            ):
                raise RuntimeWorkerError(
                    "task verification profile binding is stale"
                )
            verification_head = ""
            if verify_step is not None:
                head_result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=spec["cwd"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                verification_head = head_result.stdout.strip()
                if (
                    head_result.returncode
                    or not re.fullmatch(
                        r"[0-9a-f]{40,64}", verification_head
                    )
                ):
                    raise RuntimeWorkerError(
                        "pipeline verification HEAD is unavailable"
                    )
            verification_input_sha256 = hashlib.sha256(
                json.dumps(
                    {
                        "definition_sha256": (
                            pipeline.definition_sha256
                        ),
                        "head_sha": verification_head,
                        "profile_sha256": profile.sha256,
                        "schema_version": (
                            verify_step.schema_version
                            if verify_step is not None
                            else 1
                        ),
                        "summary": summary,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            verification_effect_id = (
                "pipeline-verify-"
                + verification_input_sha256[:32]
            )
            (
                verification_spec,
                verification_lane_id,
                verification_run_id,
            ) = _pipeline_verify_identity(
                operation.spec,
                definition_sha256=pipeline.definition_sha256,
                input_sha256=verification_input_sha256,
                profile=profile.name,
            )
            verification_root = (
                spec_path.parent
                / "pipeline-verification"
                / verification_spec.operation_id
            )
            verification_receipt_path = (
                verification_root / "receipt.json"
            )

            def load_verification_receipt(
                receipt_path: Path,
            ) -> dict[str, object] | None:
                if not receipt_path.exists():
                    return None
                if (
                    not receipt_path.is_file()
                    or receipt_path.is_symlink()
                ):
                    raise RuntimeWorkerError(
                        "pipeline verification receipt is invalid"
                    )
                receipt = json.loads(
                    receipt_path.read_text(encoding="utf-8")
                )
                evidence = (
                    receipt.get("evidence")
                    if isinstance(receipt, dict)
                    else None
                )
                if (
                    not isinstance(receipt, dict)
                    or receipt.get("schema_version") != 1
                    or receipt.get("parent_operation_id")
                    != spec["operation_id"]
                    or receipt.get("definition_sha256")
                    != pipeline.definition_sha256
                    or receipt.get("step_id") != "verify"
                    or receipt.get("profile") != profile.name
                    or receipt.get("profile_sha256")
                    != profile.sha256
                    or not re.fullmatch(
                        r"[0-9a-f]{40,64}",
                        str(receipt.get("head_sha") or ""),
                    )
                    or receipt.get("status")
                    not in {"complete", "failed"}
                    or not IDENTIFIER.fullmatch(
                        str(receipt.get("operation_id") or "")
                    )
                    or not IDENTIFIER.fullmatch(
                        str(receipt.get("lane_id") or "")
                    )
                    or not IDENTIFIER.fullmatch(
                        str(receipt.get("run_id") or "")
                    )
                    or not isinstance(evidence, list)
                    or not evidence
                ):
                    raise RuntimeWorkerError(
                        "pipeline verification receipt is invalid"
                    )
                receipt_head = str(receipt["head_sha"])
                receipt_input_sha256 = hashlib.sha256(
                    json.dumps(
                        {
                            "definition_sha256": (
                                pipeline.definition_sha256
                            ),
                            "head_sha": receipt_head,
                            "profile_sha256": profile.sha256,
                            "schema_version": (
                                verify_step.schema_version
                                if verify_step is not None
                                else 1
                            ),
                            "summary": summary,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                (
                    expected_spec,
                    expected_lane_id,
                    expected_run_id,
                ) = _pipeline_verify_identity(
                    operation.spec,
                    definition_sha256=pipeline.definition_sha256,
                    input_sha256=receipt_input_sha256,
                    profile=profile.name,
                )
                if (
                    receipt.get("input_sha256")
                    != receipt_input_sha256
                    or receipt.get("operation_id")
                    != expected_spec.operation_id
                    or receipt.get("lane_id") != expected_lane_id
                    or receipt.get("run_id") != expected_run_id
                    or receipt.get("effect_id")
                    != "pipeline-verify-"
                    + receipt_input_sha256[:32]
                ):
                    raise RuntimeWorkerError(
                        "pipeline verification replay identity is invalid"
                    )
                exit_codes: list[int] = []
                heads: set[str] = set()
                command_ids: list[str] = []
                for row in evidence:
                    if (
                        not isinstance(row, dict)
                        or row.get("profile") != profile.name
                        or row.get("profile_sha256")
                        != profile.sha256
                        or type(row.get("exit_code")) is not int
                        or not re.fullmatch(
                            r"[0-9a-f]{40,64}",
                            str(row.get("head_sha") or ""),
                        )
                    ):
                        raise RuntimeWorkerError(
                            "pipeline verification evidence is invalid"
                        )
                    pointer = Path(
                        str(row.get("output_pointer") or "")
                    )
                    output = (spec_path.parent / pointer).resolve()
                    evidence_root = (
                        spec_path.parent / "pipeline-verification"
                    ).resolve()
                    if (
                        pointer.is_absolute()
                        or evidence_root not in output.parents
                        or not output.is_file()
                        or output.is_symlink()
                    ):
                        raise RuntimeWorkerError(
                            "pipeline verification output is invalid"
                        )
                    exit_codes.append(int(row["exit_code"]))
                    heads.add(str(row["head_sha"]))
                    command_ids.append(str(row.get("command_id") or ""))
                succeeded = all(code == 0 for code in exit_codes)
                expected_command_ids = [
                    f"{profile.name}-{index + 1}"
                    for index in range(len(profile.commands))
                ]
                if (
                    len(heads) != 1
                    or heads != {receipt_head}
                    or command_ids
                    != expected_command_ids[: len(command_ids)]
                    or (
                        succeeded
                        and len(command_ids)
                        != len(expected_command_ids)
                    )
                    or (
                        not succeeded
                        and exit_codes[-1] == 0
                    )
                    or (
                        receipt["status"] == "complete"
                    )
                    != succeeded
                ):
                    raise RuntimeWorkerError(
                        "pipeline verification outcome is invalid"
                    )
                stored = store.read(
                    spec["owner_id"], expected_spec.operation_id
                )
                if (
                    stored.spec != expected_spec
                    or stored.lane_id != expected_lane_id
                    or stored.run_id != expected_run_id
                ):
                    raise RuntimeWorkerError(
                        "pipeline verification operation identity is invalid"
                    )
                expected_path = (
                    spec_path.parent
                    / "pipeline-verification"
                    / expected_spec.operation_id
                    / "receipt.json"
                )
                if receipt_path.resolve() != expected_path.resolve():
                    raise RuntimeWorkerError(
                        "pipeline verification receipt pointer is invalid"
                    )
                return receipt

            def controller_verification_receipt(
            ) -> dict[str, object] | None:
                linked: dict[str, object] | None = None
                if verification_controller_receipt_path.exists():
                    if (
                        not verification_controller_receipt_path.is_file()
                        or verification_controller_receipt_path.is_symlink()
                    ):
                        raise RuntimeWorkerError(
                            "pipeline verification controller receipt is invalid"
                        )
                    raw_linked = json.loads(
                        verification_controller_receipt_path.read_text(
                            encoding="utf-8"
                        )
                    )
                    if not isinstance(raw_linked, dict):
                        raise RuntimeWorkerError(
                            "pipeline verification controller receipt is invalid"
                        )
                    linked_operation_id = str(
                        raw_linked.get("operation_id") or ""
                    )
                    if not IDENTIFIER.fullmatch(linked_operation_id):
                        raise RuntimeWorkerError(
                            "pipeline verification controller receipt is invalid"
                        )
                    child_path = (
                        spec_path.parent
                        / "pipeline-verification"
                        / linked_operation_id
                        / "receipt.json"
                    )
                    linked = load_verification_receipt(child_path)
                    if linked != raw_linked:
                        raise RuntimeWorkerError(
                            "pipeline verification controller linkage is invalid"
                        )

                receipts_root = (
                    spec_path.parent / "pipeline-verification"
                )
                receipts = (
                    [
                        receipt
                        for path in receipts_root.glob("*/receipt.json")
                        if (
                            receipt := load_verification_receipt(path)
                        )
                        is not None
                    ]
                    if receipts_root.is_dir()
                    else []
                )
                unresolved_failures = [
                    receipt
                    for receipt in receipts
                    if receipt["status"] == "failed"
                    and not verification_response_accepted(receipt)
                ]
                if len(unresolved_failures) > 1:
                    raise RuntimeWorkerError(
                        "multiple failed verification children need reconciliation"
                    )
                if unresolved_failures:
                    recovered = unresolved_failures[0]
                    if recovered != linked:
                        link_verification_receipt(recovered)
                    return recovered
                current_receipts = [
                    receipt
                    for receipt in receipts
                    if receipt["operation_id"]
                    == verification_spec.operation_id
                ]
                if len(current_receipts) > 1:
                    raise RuntimeWorkerError(
                        "duplicate verification child receipts are invalid"
                    )
                if current_receipts:
                    recovered = current_receipts[0]
                    if recovered != linked:
                        link_verification_receipt(recovered)
                    return recovered
                return linked

            def verification_receipt() -> dict[str, object] | None:
                receipt = load_verification_receipt(
                    verification_receipt_path
                )
                if receipt is None:
                    return None
                if (
                    receipt["head_sha"] != verification_head
                    or receipt["operation_id"]
                    != verification_spec.operation_id
                ):
                    return None
                return receipt

            def verification_response_accepted(
                receipt: dict[str, object],
            ) -> bool:
                response_receipt_path = (
                    spec_path.parent
                    / "pipeline-verification"
                    / str(receipt["operation_id"])
                    / "response-receipt.json"
                )
                if not response_receipt_path.exists():
                    return False
                if (
                    not response_receipt_path.is_file()
                    or response_receipt_path.is_symlink()
                ):
                    raise RuntimeWorkerError(
                        "verification response receipt is invalid"
                    )
                accepted = json.loads(
                    response_receipt_path.read_text(encoding="utf-8")
                )
                if (
                    not isinstance(accepted, dict)
                    or accepted.get("schema_version") != 1
                    or accepted.get("operation_id")
                    != spec["operation_id"]
                    or accepted.get("verification_operation_id")
                    != receipt["operation_id"]
                    or accepted.get("failed_head_sha")
                    != receipt["head_sha"]
                    or accepted.get("status") != "accepted"
                    or not re.fullmatch(
                        r"[0-9a-f]{40,64}",
                        str(accepted.get("resubmitted_head_sha") or ""),
                    )
                    or accepted.get("resubmitted_head_sha")
                    == receipt["head_sha"]
                    or not re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(accepted.get("response_sha256") or ""),
                    )
                ):
                    raise RuntimeWorkerError(
                        "verification response receipt is invalid"
                    )
                return True

            def link_verification_receipt(
                receipt: dict[str, object],
            ) -> None:
                if verification_controller_receipt_path.is_symlink():
                    raise RuntimeWorkerError(
                        "pipeline verification controller receipt is invalid"
                    )
                _atomic_json(
                    verification_controller_receipt_path, receipt
                )

            def failed_verification_count() -> int:
                count = 0
                receipts_root = (
                    spec_path.parent / "pipeline-verification"
                )
                if not receipts_root.is_dir():
                    return 0
                for path in receipts_root.glob("*/receipt.json"):
                    receipt = load_verification_receipt(path)
                    if receipt is not None and receipt["status"] == "failed":
                        count += 1
                return count

            def verification_attention_packet(
                receipt: dict[str, object],
                *,
                allow_resubmit: bool,
            ) -> tuple[dict[str, object], str]:
                raw_evidence = receipt.get("evidence")
                if not isinstance(raw_evidence, list):
                    raise RuntimeWorkerError(
                        "verification attention evidence is invalid"
                    )
                packet_evidence = [
                    {
                        "command_id": str(row["command_id"]),
                        "exit_code": int(row["exit_code"]),
                        "output_pointer": str(
                            (
                                spec_path.parent
                                / str(row["output_pointer"])
                            ).resolve()
                        ),
                    }
                    for row in raw_evidence
                    if isinstance(row, dict)
                ]
                if len(packet_evidence) != len(raw_evidence):
                    raise RuntimeWorkerError(
                        "verification attention evidence is invalid"
                    )
                allowed = (
                    ["fix-and-resubmit", "escalate"]
                    if allow_resubmit
                    else ["escalate"]
                )
                packet = {
                    "schema_version": 1,
                    "operation_id": spec["operation_id"],
                    "verification_operation_id": str(
                        receipt["operation_id"]
                    ),
                    "verification_lane_id": str(receipt["lane_id"]),
                    "verification_run_id": str(receipt["run_id"]),
                    "definition_sha256": pipeline.definition_sha256,
                    "step_id": "verify",
                    "head_sha": str(receipt["head_sha"]),
                    "status": "attention-required",
                    "reason": "verification-failed",
                    "safe_boundary": "tdd-slices-complete",
                    "allowed_responses": allowed,
                    "response_pointer": (
                        ".task-verification-response.json"
                    ),
                    "receipt_pointer": str(
                        (
                            spec_path.parent
                            / "pipeline-verification"
                            / str(receipt["operation_id"])
                            / "receipt.json"
                        ).resolve()
                    ),
                    "evidence": packet_evidence,
                }
                encoded = json.dumps(
                    packet, sort_keys=True, separators=(",", ":")
                ).encode()
                if len(encoded) > MAX_OUTBOX_BYTES:
                    raise RuntimeWorkerError(
                        "verification attention packet is too large"
                    )
                return packet, hashlib.sha256(encoded).hexdigest()

            def notify_verification_attention(
                receipt: dict[str, object],
                *,
                allow_resubmit: bool,
            ) -> str:
                packet, packet_sha256 = verification_attention_packet(
                    receipt, allow_resubmit=allow_resubmit
                )
                packet_path = spec["cwd"] / ".task-verification.json"
                if packet_path.is_symlink():
                    raise RuntimeWorkerError(
                        "verification attention packet cannot be a symlink"
                    )
                _atomic_json(packet_path, packet)
                notify_path = (
                    spec_path.parent
                    / "pipeline-verification-attention-notify.json"
                )
                if notify_path.is_file():
                    if notify_path.is_symlink():
                        raise RuntimeWorkerError(
                            "verification attention notification is invalid"
                        )
                    notified = json.loads(
                        notify_path.read_text(encoding="utf-8")
                    )
                    if (
                        not isinstance(notified, dict)
                        or notified.get("schema_version") != 1
                        or notified.get("operation_id")
                        != spec["operation_id"]
                    ):
                        raise RuntimeWorkerError(
                            "verification attention notification is invalid"
                        )
                    if (
                        notified.get("packet_sha256")
                        == packet_sha256
                        and notified.get("status") == "sent"
                    ):
                        return packet_sha256
                _atomic_json(
                    notify_path,
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "packet_sha256": packet_sha256,
                        "status": "pending",
                    },
                )
                cmux_adapter.send(
                    spec["surface_id"],
                    "Typed pipeline verification attention is ready in "
                    ".task-verification.json. For fix-and-resubmit, "
                    "commit the fix and write the exact identity-bound "
                    ".task-verification-response.json; otherwise use "
                    "task_escalation.py. Do not launch review or reap.",
                )
                cmux_adapter.send_key(spec["surface_id"], "Enter")
                _atomic_json(
                    notify_path,
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "packet_sha256": packet_sha256,
                        "status": "sent",
                    },
                )
                return packet_sha256

            def accept_verification_resubmission(
                failed: dict[str, object],
            ) -> bool:
                if verification_head == failed["head_sha"]:
                    return False
                _, packet_sha256 = verification_attention_packet(
                    failed, allow_resubmit=True
                )
                response_path = (
                    spec["cwd"] / ".task-verification-response.json"
                )
                try:
                    raw = response_path.read_bytes()
                except FileNotFoundError:
                    return False
                if (
                    response_path.is_symlink()
                    or not raw
                    or len(raw) > MAX_OUTBOX_BYTES
                ):
                    raise RuntimeWorkerError(
                        "verification resubmission response is invalid"
                    )
                response = json.loads(raw)
                expected_keys = {
                    "schema_version",
                    "operation_id",
                    "verification_operation_id",
                    "failed_head_sha",
                    "packet_sha256",
                    "response",
                    "resubmitted_head_sha",
                }
                if (
                    not isinstance(response, dict)
                    or set(response) != expected_keys
                    or response.get("schema_version") != 1
                    or response.get("operation_id")
                    != spec["operation_id"]
                    or response.get("verification_operation_id")
                    != failed["operation_id"]
                    or response.get("failed_head_sha")
                    != failed["head_sha"]
                    or response.get("packet_sha256")
                    != packet_sha256
                    or response.get("response")
                    != "fix-and-resubmit"
                    or response.get("resubmitted_head_sha")
                    != verification_head
                ):
                    raise RuntimeWorkerError(
                        "verification resubmission response is invalid"
                    )
                response_sha256 = hashlib.sha256(
                    json.dumps(
                        response, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest()
                response_receipt_path = (
                    spec_path.parent
                    / "pipeline-verification"
                    / str(failed["operation_id"])
                    / "response-receipt.json"
                )
                response_receipt = {
                    "schema_version": 1,
                    "operation_id": spec["operation_id"],
                    "verification_operation_id": failed["operation_id"],
                    "failed_head_sha": failed["head_sha"],
                    "resubmitted_head_sha": verification_head,
                    "response_sha256": response_sha256,
                    "status": "accepted",
                }
                if response_receipt_path.is_file():
                    if response_receipt_path.is_symlink():
                        raise RuntimeWorkerError(
                            "verification response receipt is invalid"
                        )
                    existing = json.loads(
                        response_receipt_path.read_text(encoding="utf-8")
                    )
                    if existing != response_receipt:
                        raise RuntimeWorkerError(
                            "verification response receipt is invalid"
                        )
                else:
                    _atomic_json(
                        response_receipt_path, response_receipt
                    )
                failed_record = store.read(
                    spec["owner_id"], str(failed["operation_id"])
                )
                if failed_record.state == "attention-required":
                    store.transition(
                        spec["owner_id"],
                        failed_record.spec.operation_id,
                        "failed",
                    )
                elif failed_record.state != "failed":
                    raise RuntimeWorkerError(
                        "failed verification operation cannot resume"
                    )
                return True

            def reconcile_failed_verification_child(
                failed: dict[str, object],
            ) -> None:
                failed_operation_id = str(failed["operation_id"])
                failed_record = store.read(
                    spec["owner_id"], failed_operation_id
                )
                if failed_record.pending_effect:
                    if (
                        failed_record.pending_effect
                        != failed["effect_id"]
                    ):
                        raise RuntimeWorkerError(
                            "failed verification effect is uncertain"
                        )
                    store.resolve_effect(
                        spec["owner_id"],
                        failed_operation_id,
                        EffectOutcome.SUCCEEDED,
                    )
                    failed_record = store.read(
                        spec["owner_id"], failed_operation_id
                    )
                if failed_record.state == "verifying":
                    store.transition(
                        spec["owner_id"],
                        failed_operation_id,
                        "attention-required",
                        reason=AttentionReason.ATTENTION_REQUIRED,
                    )
                elif failed_record.state not in {
                    "attention-required",
                    "failed",
                }:
                    raise RuntimeWorkerError(
                        "failed verification operation state is invalid"
                    )

            def run_verification() -> None:
                existing = verification_receipt()
                current = store.create(
                    verification_spec,
                    lane_id=verification_lane_id,
                    run_id=verification_run_id,
                )
                supervisor = OperationSupervisor(
                    store,
                    spec["owner_id"],
                    verification_spec.operation_id,
                )
                supervisor.configure_budget(
                    attempt_limit=1,
                    model_restart_limit=0,
                    time_budget_seconds=DEFAULT_TIME_BUDGET_SECONDS,
                    token_limit=DEFAULT_TOKEN_LIMIT,
                )
                current = supervisor.read()
                if current.state == "created":
                    supervisor.transition("preflight")
                    supervisor.transition("starting")
                    supervisor.transition("running")
                    supervisor.transition("verifying")
                    supervisor.consume_attempt()
                    current = supervisor.read()
                if current.pending_effect:
                    if (
                        current.pending_effect
                        == verification_effect_id
                        and existing is not None
                    ):
                        store.resolve_effect(
                            spec["owner_id"],
                            verification_spec.operation_id,
                            EffectOutcome.SUCCEEDED,
                        )
                    else:
                        summary_attention(
                            "pipeline-verification-effect-uncertain"
                        )
                        return
                if existing is None:
                    current = supervisor.read()
                    if current.state != "verifying":
                        raise RuntimeWorkerError(
                            "pipeline verification state is invalid"
                        )

                    def execute_verification(
                        _record: object,
                    ) -> list[object]:
                        evidence = list(
                            run_profile(
                                profile,
                                root=spec["cwd"],
                                evidence_dir=(
                                    verification_root / "evidence"
                                ),
                                runner=(
                                    verification_runner
                                    or subprocess.run
                                ),
                                pointer_root=spec_path.parent,
                            )
                        )
                        verified_heads = {
                            str(item.head_sha) for item in evidence
                        }
                        current_head = subprocess.run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=spec["cwd"],
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        if (
                            current_head.returncode
                            or current_head.stdout.strip()
                            != verification_head
                            or verified_heads != {verification_head}
                        ):
                            raise VerificationError(
                                "verification HEAD changed during execution"
                            )
                        return evidence

                    def persist_verification(
                        _record: object,
                        evidence: list[object],
                    ) -> None:
                        rows = [to_dict(item) for item in evidence]
                        _atomic_json(
                            verification_receipt_path,
                            {
                                "schema_version": 1,
                                "operation_id": (
                                    verification_spec.operation_id
                                ),
                                "parent_operation_id": (
                                    spec["operation_id"]
                                ),
                                "lane_id": verification_lane_id,
                                "run_id": verification_run_id,
                                "definition_sha256": (
                                    pipeline.definition_sha256
                                ),
                                "step_id": "verify",
                                "head_sha": verification_head,
                                "input_sha256": (
                                    verification_input_sha256
                                ),
                                "profile": profile.name,
                                "profile_sha256": profile.sha256,
                                "effect_id": verification_effect_id,
                                "status": (
                                    "complete"
                                    if all(
                                        row["exit_code"] == 0
                                        for row in rows
                                    )
                                    else "failed"
                                ),
                                "evidence": rows,
                            }
                        )
                        persisted = json.loads(
                            verification_receipt_path.read_text(
                                encoding="utf-8"
                            )
                        )
                        link_verification_receipt(persisted)

                    supervisor.effect(
                        verification_effect_id,
                        execute_verification,
                        persist_result=persist_verification,
                    )
                    existing = verification_receipt()
                if existing is None:
                    raise RuntimeWorkerError(
                        "pipeline verification produced no receipt"
                    )
                if existing["status"] == "failed":
                    current = supervisor.read()
                    if current.state == "verifying":
                        store.transition(
                            spec["owner_id"],
                            verification_spec.operation_id,
                            "attention-required",
                            reason=AttentionReason.ATTENTION_REQUIRED,
                        )
                    return
                current = supervisor.read()
                if current.state == "verifying":
                    supervisor.transition("finalizing")
                    supervisor.transition("exiting")
                    supervisor.transition("complete")

            previous_verification = (
                controller_verification_receipt()
                if verify_step is not None
                else None
            )
            if (
                previous_verification is not None
                and previous_verification["status"] == "failed"
                and previous_verification["head_sha"]
                != verification_head
            ):
                reconcile_failed_verification_child(
                    previous_verification
                )
                allow_resubmit = (
                    failed_verification_count()
                    <= MAX_PIPELINE_VERIFY_RESUBMITS
                )
                notify_verification_attention(
                    previous_verification,
                    allow_resubmit=allow_resubmit,
                )
                if not allow_resubmit:
                    summary_attention(
                        "pipeline-verification-retry-exhausted",
                        AttentionReason.RETRY_EXHAUSTED,
                    )
                    return
                if not accept_verification_resubmission(
                    previous_verification
                ):
                    return
            existing_verification = (
                verification_receipt()
                if verify_step is not None
                else None
            )
            if existing_verification is not None:
                run_verification()
                existing_verification = verification_receipt()
                if (
                    existing_verification is not None
                    and existing_verification["status"] == "failed"
                ):
                    allow_resubmit = (
                        failed_verification_count()
                        <= MAX_PIPELINE_VERIFY_RESUBMITS
                    )
                    notify_verification_attention(
                        existing_verification,
                        allow_resubmit=allow_resubmit,
                    )
                    if not allow_resubmit:
                        summary_attention(
                            "pipeline-verification-retry-exhausted",
                            AttentionReason.RETRY_EXHAUSTED,
                        )
                    return
            if (
                existing_verification is not None
                and existing_verification["status"] == "complete"
            ):
                evidence = existing_verification["evidence"]
                if (
                    not isinstance(evidence, list)
                    or verification_head
                    != evidence[0]["head_sha"]
                ):
                    summary_attention(
                        "pipeline-verification-head-drift",
                        AttentionReason.CONTRACT_DRIFT,
                    )
                    return

            verification_complete = (
                verify_step is None
                or existing_verification is not None
                and existing_verification["status"] == "complete"
            )
            if verification_complete:
                gate_state = review_gate_state()
                if gate_state.get("status") == "awaiting-resolution":
                    notify_review_resolution(gate_state)
                    if review.status == "stale":
                        drive_review()
                        return
                    _atomic_json(
                        marker_path,
                        {
                            "schema_version": 1,
                            "operation_id": spec["operation_id"],
                            "definition_sha256": (
                                pipeline.definition_sha256
                            ),
                            "status": "started",
                            "drive_sha256": review_drive_sha256(),
                        },
                    )
                    return

                if (
                    marker is not None
                    and review.status in {"reviewing", "stale"}
                ):
                    current_drive_sha256 = review_drive_sha256()
                    if (
                        marker["status"] == "pending"
                        or marker.get("drive_sha256")
                        != current_drive_sha256
                    ):
                        drive_review()
                    return
            review_observation = {
                "missing": "pending",
                "reviewing": "running",
                "approved": "complete",
                "skipped": "complete",
                "attention": "attention",
                "stale": "attention",
            }[review.status]
            observations: dict[str, str] = {}
            for step in steps:
                if step.primitive_id == "model_step":
                    observations[step.step_id] = "complete"
                elif step.primitive_id == "verify":
                    observations[step.step_id] = (
                        "pending"
                        if existing_verification is None
                        else (
                            "complete"
                            if existing_verification["status"]
                            == "complete"
                            else "attention"
                        )
                    )
                else:
                    observations[step.step_id] = (
                        review_observation
                        if (
                            verify_step is None
                            or existing_verification is not None
                            and existing_verification["status"]
                            == "complete"
                        )
                        else "pending"
                    )
            progress = reconcile_pipeline(
                pipeline,
                observations,
            )
            if progress.action == "start":
                step = next(
                    row
                    for row in steps
                    if row.step_id == progress.step_id
                )
                if step.primitive_id == "verify":
                    run_verification()
                    return
                if marker is not None:
                    if marker["status"] == "started":
                        return
                drive_review()
                return
            if progress.action == "wait":
                return
            if progress.action == "attention":
                if progress.step_id == (
                    verify_step.step_id if verify_step else ""
                ):
                    summary_attention(
                        "pipeline-verification-failed",
                        AttentionReason.ATTENTION_REQUIRED,
                    )
                else:
                    summary_attention(
                        f"review-finalization-{review.status}"
                    )
                return
            if progress.action != "reap-ready":
                raise RuntimeWorkerError(
                    "compiled pipeline returned an invalid finalization action"
                )
            callback_handled = True
            encoded = json.dumps(
                summary, sort_keys=True, separators=(",", ":")
            ).encode()
            payload_sha256 = hashlib.sha256(encoded).hexdigest()
            envelope = CallbackEnvelope(
                callback_id=f"wiki-summary-{payload_sha256[:24]}",
                operation_id=spec["operation_id"],
                run_id=spec["run_id"],
                kind="wiki-summary",
                payload=summary,
                payload_sha256=payload_sha256,
            )
            acceptance = CallbackBroker(
                store, spec["owner_id"]
            ).accept(envelope)
            _atomic_json(
                spec_path.parent / "callback-receipt.json",
                {
                    "schema_version": 1,
                    "callback_id": envelope.callback_id,
                    "operation_id": envelope.operation_id,
                    "status": (
                        "duplicate" if acceptance.duplicate else "accepted"
                    ),
                },
            )
            notify_path = spec_path.parent / "task-summary-notify.json"
            if notify_path.exists():
                marker = json.loads(notify_path.read_text(encoding="utf-8"))
                if (
                    marker.get("schema_version") != 1
                    or marker.get("callback_id") != envelope.callback_id
                ):
                    raise RuntimeWorkerError(
                        "task summary notification marker is invalid"
                    )
                if marker.get("status") == "sent":
                    return
                if marker.get("status") == "pending":
                    try:
                        store.transition(
                            spec["owner_id"],
                            spec["operation_id"],
                            "attention-required",
                            reason=AttentionReason.ATTENTION_REQUIRED,
                        )
                    except Exception:
                        pass
                    return
                raise RuntimeWorkerError(
                    "task summary notification marker state is invalid"
                )
            vault_root = Path(str(meta.get("vault_root") or "")).resolve()
            reap_runner = vault_root / "scripts" / "reap-runner.py"
            if (
                not reap_runner.is_file()
                or reap_runner.is_symlink()
                or not (vault_root / "wiki").is_dir()
            ):
                raise RuntimeWorkerError("trusted reap runner is unavailable")
            command = shlex.join(
                [
                    "python3",
                    str(reap_runner),
                    "--vault-root",
                    str(vault_root),
                    "--worktree",
                    str(spec["cwd"]),
                ]
            )
            wake = (
                "Typed final task summary callback was accepted. "
                f"Run this exact command now: {command}"
            )
            if len(wake.encode()) > 4096:
                raise RuntimeWorkerError("task summary wake message is too large")
            _atomic_json(
                notify_path,
                {
                    "schema_version": 1,
                    "callback_id": envelope.callback_id,
                    "status": "pending",
                },
            )
            cmux_adapter.send(spec["origin_surface"], wake)
            cmux_adapter.send_key(spec["origin_surface"], "Enter")
            _atomic_json(
                notify_path,
                {
                    "schema_version": 1,
                    "callback_id": envelope.callback_id,
                    "status": "sent",
                },
            )
        except (
            CallbackError,
            ContractError,
            RuntimeWorkerError,
            VerificationError,
            WikiSummaryError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            summary_attention("wiki-summary-invalid")

    exit_code = 0
    provider_exited = False
    exit_containment_failed = False
    while True:
        inspect_control()
        if spec["callback_mode"] == "task-summary":
            recover_task_summary_attention()
            drive_fix_transport()
            inspect_task_summary()
        elif spec["callback_mode"] in {
            "research-fetch",
            "research-synth",
        }:
            inspect_research()
        else:
            inspect_callback()
        if enforce_callback_deadline(
            store,
            spec["owner_id"],
            spec["operation_id"],
            callback_handled=callback_handled,
        ):
            _atomic_json(
                spec_path.parent / "callback-timeout.json",
                {
                    "schema_version": 1,
                    "operation_id": spec["operation_id"],
                    "run_id": spec["run_id"],
                    "status": "attention-required",
                },
            )
        now = time.monotonic()
        if now >= next_prompt_probe:
            next_prompt_probe = now + 0.2
            inspect_prompt()
        if not checkpoint and now >= next_checkpoint_probe:
            next_checkpoint_probe = now + 0.5
            try:
                checkpoint = checkpoint_probe(
                    str(spec["surface_id"]), str(spec["runtime"])
                )
            except Exception:
                checkpoint = ""
            if checkpoint:
                _atomic_json(
                    spec_path.parent / "checkpoint.json",
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "run_id": spec["run_id"],
                        "runtime": spec["runtime"],
                        "checkpoint": checkpoint,
                    },
                )
        if not provider_exited:
            try:
                exit_pending = os.waitid(
                    os.P_PID,
                    handle.pid,
                    os.WEXITED | os.WNOHANG | os.WNOWAIT,
                )
            except ChildProcessError:
                exit_pending = None
                provider_exited = True
                exit_code = 0
                try:
                    store.transition(
                        spec["owner_id"],
                        spec["operation_id"],
                        "attention-required",
                        reason=AttentionReason.ATTENTION_REQUIRED,
                    )
                except Exception:
                    pass
            except OSError:
                exit_pending = None
            if exit_pending is not None:
                try:
                    process.signal_owned_child_group(
                        handle.process_group,
                        handle.process_identity,
                        signal.SIGKILL,
                    )
                except ProcessError:
                    if not exit_containment_failed:
                        exit_containment_failed = True
                        try:
                            store.transition(
                                spec["owner_id"],
                                spec["operation_id"],
                                "attention-required",
                                reason=AttentionReason.ATTENTION_REQUIRED,
                            )
                        except Exception:
                            pass
                    time.sleep(max(0.02, poll_seconds))
                    continue
                waited, status = os.waitpid(handle.pid, os.WNOHANG)
                if waited != handle.pid:
                    time.sleep(max(0.02, poll_seconds))
                    continue
                exit_code = os.waitstatus_to_exitcode(status)
                provider_exited = True
        try:
            operation_record = store.read(
                spec["owner_id"], spec["operation_id"]
            )
            operation_state = operation_record.state
            operation_profile = operation_record.spec.route.profile
            callback_deadline_at = operation_record.deadline_at
        except Exception:
            operation_state = ""
            operation_profile = ""
            callback_deadline_at = 0.0
        if provider_exit_is_final(
            provider_exited=provider_exited,
            callback_mode=spec["callback_mode"],
            callback_handled=callback_handled,
            operation_state=operation_state,
            operation_profile=operation_profile,
            callback_deadline_at=callback_deadline_at,
        ):
            break
        time.sleep(max(0.02, poll_seconds))
    for _ in range(3):
        if spec["callback_mode"] == "task-summary":
            recover_task_summary_attention()
            drive_fix_transport()
            inspect_task_summary()
        elif spec["callback_mode"] in {
            "research-fetch",
            "research-synth",
        }:
            inspect_research()
        else:
            inspect_callback()
        if callback_handled:
            break
        time.sleep(max(0.02, poll_seconds))
    _atomic_json(
        exit_path,
        {
            "schema_version": 1,
            "status": "exited",
            "exit_code": exit_code,
        },
    )
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return run(args.spec)
    except RuntimeWorkerError:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
