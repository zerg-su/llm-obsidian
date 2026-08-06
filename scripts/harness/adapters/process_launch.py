"""Provider process start and durable surface-worker launch preparation."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path


SURFACE_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\Z"
)


@dataclass(frozen=True)
class ProcessHandle:
    pid: int
    process_group: int
    supervisor_pid: int = 0
    process_identity: str = ""
    supervisor_identity: str = ""
    _process: subprocess.Popen[object] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True)
class SurfaceLaunch:
    """One code-owned worker command and its exact handshake files."""

    command: str
    spec_path: Path
    ready_path: Path
    exit_path: Path


def env_shebang_interpreter(
    argv: Sequence[str], env: Mapping[str, str]
) -> Path | None:
    """Resolve an env shebang while the coordinator still has host PATH."""

    if not argv:
        return None
    try:
        with Path(argv[0]).open("rb") as handle:
            first_line = handle.readline(256)
    except OSError:
        return None
    match = re.fullmatch(
        rb"#![ \t]*/usr/bin/env[ \t]+([A-Za-z0-9._+-]+)[ \t]*\r?\n?",
        first_line,
    )
    if match is None:
        return None
    interpreter = shutil.which(match.group(1).decode("ascii"), path=env.get("PATH"))
    if not interpreter:
        return None
    resolved = Path(interpreter).expanduser().resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return None
    return resolved


def contain_unidentified_child(proc: subprocess.Popen[object]) -> None:
    """Synchronously reap a just-spawned child whose identity was not bound."""

    import signal

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        proc.wait(timeout=1.0)
        return
    except subprocess.TimeoutExpired:
        pass
    except (OSError, ProcessLookupError):
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=1.0)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        pass


def start_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdin: int | None,
    stdout: int | None,
    stderr: int | None,
    capture_identity: Callable[..., str],
    contain_child: Callable[[subprocess.Popen[object]], None],
    error_type: type[RuntimeError],
) -> ProcessHandle:
    if not argv or not Path(argv[0]).is_absolute():
        raise error_type("runtime executable must be an absolute resolved path")
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
    try:
        process_group = os.getpgid(proc.pid)
        identity = capture_identity(proc.pid, process_group=process_group)
    except (OSError, error_type) as exc:
        contain_child(proc)
        raise error_type("started process identity is unavailable") from exc
    if not identity:
        contain_child(proc)
        raise error_type("started process exited before identity capture")
    return ProcessHandle(
        proc.pid,
        process_group,
        process_identity=identity,
        _process=proc,
    )


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_surface_launch(
    *,
    argv: Sequence[str],
    cwd: Path,
    state_root: Path,
    worker: Path,
    callback_pointer: Path,
    product_root: Path | None,
    reviewer_sandbox: bool,
    callback_registration: Path | None,
    store_root: Path,
    owner_id: str,
    operation_id: str,
    run_id: str,
    surface_id: str,
    runtime: str,
    callback_mode: str,
    task_summary_pointer: Path | None,
    origin_surface: str,
    runtime_home: Path | None,
    research_request_sha256: str,
    callback_wake: str,
    initial_input_pointer: Path | None,
    json_writer: Callable[[Path, object], None],
    shebang_resolver: Callable[[Sequence[str], Mapping[str, str]], Path | None],
    error_type: type[RuntimeError],
) -> SurfaceLaunch:
    """Validate and persist one bounded worker spec without launching it."""

    resolved_state_root = state_root.resolve()
    registration = (
        callback_registration.resolve()
        if callback_registration is not None
        else resolved_state_root / "callback-target.json"
    )
    if (
        not argv
        or not Path(argv[0]).is_absolute()
        or not worker.is_absolute()
        or not cwd.is_absolute()
        or not callback_pointer.is_absolute()
        or not registration.is_absolute()
        or not store_root.is_absolute()
    ):
        raise error_type("surface launch paths and runtime must be absolute")
    effective_product_root = product_root
    if (
        effective_product_root is None
        and not reviewer_sandbox
        and callback_mode not in {"research-fetch", "research-synth"}
    ):
        effective_product_root = cwd
    resolved_product_root = (
        effective_product_root.expanduser().resolve()
        if effective_product_root is not None
        else None
    )
    if effective_product_root is not None and (
        not effective_product_root.is_absolute()
        or effective_product_root.is_symlink()
        or resolved_product_root is None
        or not resolved_product_root.is_dir()
    ):
        raise error_type("surface launch product root is invalid")
    if registration.parent != resolved_state_root:
        raise error_type("callback registration must stay in launch state")
    if runtime not in {"claude", "codex"}:
        raise error_type("surface launch runtime is invalid")
    if callback_mode not in {
        "envelope",
        "task-summary",
        "research-fetch",
        "research-synth",
    }:
        raise error_type("surface callback mode is invalid")
    if not isinstance(reviewer_sandbox, bool) or reviewer_sandbox and (
        callback_mode != "envelope" or resolved_product_root is None
    ):
        raise error_type("reviewer sandbox identity is invalid")
    summary_pointer = (
        task_summary_pointer.resolve() if task_summary_pointer is not None else None
    )
    if callback_mode == "task-summary" and (
        summary_pointer is None
        or not summary_pointer.is_absolute()
        or not SURFACE_UUID.fullmatch(origin_surface)
    ):
        raise error_type(
            "task-summary mode requires an exact source and origin surface"
        )
    resolved_runtime_home = (
        runtime_home.expanduser().resolve() if runtime_home is not None else None
    )
    runtime_interpreter = shebang_resolver(argv, os.environ)
    _validate_research_fields(
        runtime=runtime,
        callback_mode=callback_mode,
        runtime_home=runtime_home,
        resolved_runtime_home=resolved_runtime_home,
        cwd=cwd,
        origin_surface=origin_surface,
        research_request_sha256=research_request_sha256,
        callback_wake=callback_wake,
        error_type=error_type,
    )
    if any(not isinstance(part, str) or "\0" in part for part in argv):
        raise error_type("surface launch argv is invalid")
    initial_input = (
        initial_input_pointer.expanduser().resolve()
        if initial_input_pointer is not None
        else None
    )
    if initial_input is not None:
        try:
            initial_input.relative_to(cwd.resolve())
        except ValueError as exc:
            raise error_type("initial provider input escapes runtime cwd") from exc
        if initial_input.is_symlink() or not initial_input.is_file():
            raise error_type("initial provider input is unavailable")

    resolved_state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved_state_root.chmod(0o700)
    if not registration.exists():
        json_writer(
            registration,
            {
                "schema_version": 1,
                "generation": 1,
                "operation_id": operation_id,
                "run_id": run_id,
                "callback_pointer": str(callback_pointer),
            },
        )
    launch = SurfaceLaunch(
        command="",
        spec_path=resolved_state_root / "launch.json",
        ready_path=resolved_state_root / "ready.json",
        exit_path=resolved_state_root / "exit.json",
    )
    json_writer(
        launch.spec_path,
        _launch_spec(
            argv=argv,
            cwd=cwd,
            callback_pointer=callback_pointer,
            product_root=resolved_product_root,
            reviewer_sandbox=reviewer_sandbox,
            callback_mode=callback_mode,
            summary_pointer=summary_pointer,
            origin_surface=origin_surface,
            resolved_runtime_home=resolved_runtime_home,
            runtime_interpreter=runtime_interpreter,
            research_request_sha256=research_request_sha256,
            callback_wake=callback_wake,
            registration=registration,
            store_root=store_root,
            owner_id=owner_id,
            operation_id=operation_id,
            run_id=run_id,
            surface_id=surface_id,
            runtime=runtime,
            launch=launch,
            initial_input=initial_input,
        ),
    )
    command = shlex.join(
        [
            str(Path(os.sys.executable).resolve()),
            str(worker),
            "--spec",
            str(launch.spec_path),
        ]
    )
    return SurfaceLaunch(
        command=f"exec {command}",
        spec_path=launch.spec_path,
        ready_path=launch.ready_path,
        exit_path=launch.exit_path,
    )


def _valid_callback_wake(value: str) -> bool:
    return bool(
        value
        and value == value.strip()
        and "\0" not in value
        and "\n" not in value
        and "\r" not in value
        and len(value.encode()) <= 4096
    )


def _validate_research_fields(
    *,
    runtime: str,
    callback_mode: str,
    runtime_home: Path | None,
    resolved_runtime_home: Path | None,
    cwd: Path,
    origin_surface: str,
    research_request_sha256: str,
    callback_wake: str,
    error_type: type[RuntimeError],
) -> None:
    research_mode = callback_mode in {"research-fetch", "research-synth"}
    if research_mode:
        if (
            runtime != "codex"
            or resolved_runtime_home is None
            or runtime_home is None
            or runtime_home.is_symlink()
            or not resolved_runtime_home.is_dir()
            or resolved_runtime_home.stat().st_uid != os.getuid()
            or resolved_runtime_home.stat().st_mode & 0o077
            or resolved_runtime_home == cwd
            or resolved_runtime_home in cwd.parents
            or cwd in resolved_runtime_home.parents
            or not SURFACE_UUID.fullmatch(origin_surface)
            or not _valid_callback_wake(callback_wake)
        ):
            raise error_type("research launch identity is invalid")
        if callback_mode == "research-fetch":
            if not re.fullmatch(r"[0-9a-f]{64}", research_request_sha256):
                raise error_type("research fetch request identity is invalid")
        elif research_request_sha256:
            raise error_type("research synth request identity must be derived")
    elif resolved_runtime_home is not None or research_request_sha256:
        raise error_type("research launch fields require research callback mode")
    elif callback_wake and (
        callback_mode != "envelope" or not _valid_callback_wake(callback_wake)
    ):
        raise error_type("review callback wake is invalid")


def _launch_spec(
    *,
    argv: Sequence[str],
    cwd: Path,
    callback_pointer: Path,
    product_root: Path | None,
    reviewer_sandbox: bool,
    callback_mode: str,
    summary_pointer: Path | None,
    origin_surface: str,
    resolved_runtime_home: Path | None,
    runtime_interpreter: Path | None,
    research_request_sha256: str,
    callback_wake: str,
    registration: Path,
    store_root: Path,
    owner_id: str,
    operation_id: str,
    run_id: str,
    surface_id: str,
    runtime: str,
    launch: SurfaceLaunch,
    initial_input: Path | None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "argv": list(argv),
        "cwd": str(cwd),
        "callback_pointer": str(callback_pointer),
        "product_root": str(product_root) if product_root else "",
        "reviewer_sandbox": reviewer_sandbox,
        "callback_mode": callback_mode,
        "task_summary_pointer": str(summary_pointer) if summary_pointer else "",
        "origin_surface": origin_surface,
        "runtime_home": str(resolved_runtime_home) if resolved_runtime_home else "",
        "runtime_interpreter": (
            str(runtime_interpreter) if runtime_interpreter else ""
        ),
        "research_request_sha256": research_request_sha256,
        "callback_wake": callback_wake,
        "callback_registration": str(registration),
        "store_root": str(store_root),
        "owner_id": owner_id,
        "operation_id": operation_id,
        "run_id": run_id,
        "surface_id": surface_id,
        "runtime": runtime,
        "ready_path": str(launch.ready_path),
        "exit_path": str(launch.exit_path),
        "initial_input_pointer": str(initial_input) if initial_input else "",
    }


def await_surface_handle(
    launch: SurfaceLaunch,
    *,
    timeout_seconds: float,
    error_type: type[RuntimeError],
) -> ProcessHandle:
    """Read the worker's exact provider PID/PGID handshake."""

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() <= deadline:
        try:
            value = json.loads(launch.ready_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        except (OSError, json.JSONDecodeError) as exc:
            raise error_type("runtime worker handshake is invalid") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise error_type("runtime worker handshake has an invalid schema")
        if value.get("status") != "ready":
            raise error_type("runtime worker failed before provider readiness")
        pid = value.get("pid")
        process_group = value.get("process_group")
        supervisor_pid = value.get("supervisor_pid")
        process_identity = value.get("process_identity")
        supervisor_identity = value.get("supervisor_identity")
        if (
            type(pid) is not int
            or type(process_group) is not int
            or type(supervisor_pid) is not int
            or pid <= 1
            or process_group <= 1
            or supervisor_pid <= 1
            or pid != process_group
            or not isinstance(process_identity, str)
            or not re.fullmatch(r"[0-9a-f]{64}", process_identity)
            or not isinstance(supervisor_identity, str)
            or not re.fullmatch(r"[0-9a-f]{64}", supervisor_identity)
        ):
            raise error_type("runtime worker returned an invalid process identity")
        return ProcessHandle(
            pid,
            process_group,
            supervisor_pid,
            process_identity,
            supervisor_identity,
        )
    raise error_type("runtime worker readiness timed out")
