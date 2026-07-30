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
from .contracts import AttentionReason, CallbackEnvelope
from .prompts import PromptDecision, classify
from .pipeline_builtins import compiled_builtin
from .pipelines import reconcile_pipeline
from .review_finalization import task_review_status
from .state_machine import TERMINAL
from .store import OperationStore
from .supervisor import OperationSupervisor, SupervisorError
from research_contract import (
    ResearchContractError,
    load_artifact,
    validate_result_artifact,
)
from task_contract import ContractError, validate_handoff
from wiki_summary_contract import WikiSummaryError, validate_summary


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SURFACE_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\Z"
)
MAX_OUTBOX_BYTES = 70_000
MAX_SCREEN_BYTES = 70_000
RESEARCH_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
CALLBACK_WAIT_STATES = frozenset(
    {"running", "awaiting-callback", "verifying"}
)


class RuntimeWorkerError(RuntimeError):
    pass


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
    cmux_adapter = cmux_adapter or CmuxAdapter()
    lifecycle = compiled_builtin("lifecycle/default")
    last_prompt_digest = ""
    next_prompt_probe = 0.0
    handled_control_id = ""
    invalid_control_digest = ""

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
        nonlocal callback_handled
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
        _atomic_json(
            spec_path.parent / "callback-error.json",
            {"schema_version": 1, "status": status},
        )

    def inspect_task_summary() -> None:
        nonlocal callback_handled, summary_digest, summary_stable_reads
        if callback_handled:
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
            if operation.spec.contract_sha256 != lifecycle.definition_sha256:
                summary_attention(
                    "pipeline-contract-drift",
                    AttentionReason.CONTRACT_DRIFT,
                )
                return
            review_observation = {
                "missing": "pending",
                "reviewing": "running",
                "approved": "complete",
                "skipped": "complete",
                "attention": "attention",
                "stale": "attention",
            }[review.status]
            progress = reconcile_pipeline(
                lifecycle,
                {
                    "dispatch": "complete",
                    "review": review_observation,
                },
            )
            if progress.action == "start":
                marker_path = spec_path.parent / "pipeline-review-start.json"
                marker = None
                if marker_path.is_file() and not marker_path.is_symlink():
                    marker = json.loads(marker_path.read_text(encoding="utf-8"))
                if marker is not None:
                    if (
                        marker.get("schema_version") != 1
                        or marker.get("operation_id") != spec["operation_id"]
                        or marker.get("definition_sha256")
                        != lifecycle.definition_sha256
                        or marker.get("status") not in {"pending", "started"}
                    ):
                        raise RuntimeWorkerError(
                            "pipeline review launch receipt is invalid"
                        )
                    if marker["status"] == "started":
                        return
                _atomic_json(
                    marker_path,
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "definition_sha256": lifecycle.definition_sha256,
                        "status": "pending",
                    },
                )
                if review_launcher is not None:
                    review_launcher(trusted_vault, spec["cwd"])
                else:
                    runner = trusted_vault / "scripts" / "task-review-runner.py"
                    if not runner.is_file() or runner.is_symlink():
                        raise RuntimeWorkerError(
                            "trusted task review runner is unavailable"
                        )
                    try:
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
                            timeout=30,
                        )
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        raise RuntimeWorkerError(
                            "automatic task review launch failed"
                        ) from exc
                    if launched.returncode != 0:
                        raise RuntimeWorkerError(
                            "automatic task review launch failed"
                        )
                _atomic_json(
                    marker_path,
                    {
                        "schema_version": 1,
                        "operation_id": spec["operation_id"],
                        "definition_sha256": lifecycle.definition_sha256,
                        "status": "started",
                    },
                )
                return
            if progress.action == "wait":
                return
            if progress.action == "attention":
                summary_attention(f"review-finalization-{review.status}")
                return
            if progress.action != "reap-ready":
                raise RuntimeWorkerError(
                    "compiled lifecycle returned an invalid finalization action"
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
