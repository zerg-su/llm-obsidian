"""Public exact-process adapter with injected platform/lifecycle collaborators."""

from __future__ import annotations

# These module objects remain public monkeypatch seams used by hermetic tests.
# Collaborators import the same singleton modules, so patched attributes remain
# effective across the extracted implementation boundaries.
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .process_identity import (
    capture_identity as _capture_identity,
    current_identity as _current_identity,
    darwin_boot_id as _darwin_boot_id,
    darwin_process_fields as _darwin_process_fields,
    darwin_process_record as _darwin_process_record,
    darwin_session_file_id as _darwin_session_file_id,
    darwin_sysctl_boot_id as _darwin_sysctl_boot_id,
    linux_process_record as _linux_process_record,
)
from .process_launch import (
    SURFACE_UUID,
    ProcessHandle,
    SurfaceLaunch,
    await_surface_handle as _await_surface_handle,
    contain_unidentified_child as _contain_unidentified_child,
    env_shebang_interpreter as _env_shebang_interpreter,
    prepare_surface_launch as _prepare_surface_launch,
    start_process as _start_process,
    write_json as _write_json,
)
from .process_signals import (
    group_has_other_members as _group_has_other_members,
    pid_status as _pid_status,
    process_status as _process_status,
    request_guardian_signal as _request_guardian_signal,
    signal_exact as _signal_exact,
    signal_owned_child_group as _signal_owned_child_group,
)


class ProcessError(RuntimeError):
    pass


class ProcessAdapter:
    """Stable façade for exact process identity, launch, and guardian policy."""

    @staticmethod
    def env_shebang_interpreter(
        argv: Sequence[str], env: Mapping[str, str]
    ) -> Path | None:
        return _env_shebang_interpreter(argv, env)

    @staticmethod
    def _contain_unidentified_child(proc: subprocess.Popen[object]) -> None:
        _contain_unidentified_child(proc)

    @staticmethod
    def _linux_process_record(pid: int) -> tuple[int, str, str]:
        return _linux_process_record(pid)

    @staticmethod
    def _darwin_session_file_id() -> str:
        return _darwin_session_file_id()

    @staticmethod
    def _darwin_sysctl_boot_id(
        *, sysctlbyname: Callable[..., int] | None = None
    ) -> str:
        return _darwin_sysctl_boot_id(sysctlbyname=sysctlbyname)

    @staticmethod
    def _darwin_boot_id(
        *, sysctlbyname: Callable[..., int] | None = None
    ) -> str:
        return _darwin_boot_id(
            session_file_id=ProcessAdapter._darwin_session_file_id,
            sysctl_boot_id=ProcessAdapter._darwin_sysctl_boot_id,
            sysctlbyname=sysctlbyname,
        )

    @staticmethod
    def _darwin_process_fields(pid: int) -> tuple[int, str]:
        return _darwin_process_fields(pid)

    @staticmethod
    def _darwin_process_record(pid: int) -> tuple[int, str, str]:
        return _darwin_process_record(
            pid,
            fields_probe=ProcessAdapter._darwin_process_fields,
            boot_probe=ProcessAdapter._darwin_boot_id,
        )

    @staticmethod
    def _current_identity(pid: int) -> tuple[int, str]:
        return _current_identity(
            pid,
            platform=sys.platform,
            linux_probe=ProcessAdapter._linux_process_record,
            darwin_probe=ProcessAdapter._darwin_process_record,
        )

    @staticmethod
    def capture_identity(pid: int, *, process_group: int = 0) -> str:
        return _capture_identity(
            pid,
            process_group=process_group,
            identity_probe=ProcessAdapter._current_identity,
            error_type=ProcessError,
        )

    def start(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
    ) -> ProcessHandle:
        return _start_process(
            argv,
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            capture_identity=self.capture_identity,
            contain_child=self._contain_unidentified_child,
            error_type=ProcessError,
        )

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        _write_json(path, value)

    def prepare_surface_launch(
        self,
        *,
        argv: Sequence[str],
        cwd: Path,
        state_root: Path,
        worker: Path,
        callback_pointer: Path,
        product_root: Path | None = None,
        reviewer_sandbox: bool = False,
        callback_registration: Path | None = None,
        store_root: Path,
        owner_id: str,
        operation_id: str,
        run_id: str,
        surface_id: str,
        runtime: str,
        callback_mode: str = "envelope",
        task_summary_pointer: Path | None = None,
        origin_surface: str = "",
        runtime_home: Path | None = None,
        research_request_sha256: str = "",
        callback_wake: str = "",
    ) -> SurfaceLaunch:
        return _prepare_surface_launch(
            argv=argv,
            cwd=cwd,
            state_root=state_root,
            worker=worker,
            callback_pointer=callback_pointer,
            product_root=product_root,
            reviewer_sandbox=reviewer_sandbox,
            callback_registration=callback_registration,
            store_root=store_root,
            owner_id=owner_id,
            operation_id=operation_id,
            run_id=run_id,
            surface_id=surface_id,
            runtime=runtime,
            callback_mode=callback_mode,
            task_summary_pointer=task_summary_pointer,
            origin_surface=origin_surface,
            runtime_home=runtime_home,
            research_request_sha256=research_request_sha256,
            callback_wake=callback_wake,
            json_writer=self._write_json,
            shebang_resolver=self.env_shebang_interpreter,
            error_type=ProcessError,
        )

    @staticmethod
    def await_surface_handle(
        launch: SurfaceLaunch, *, timeout_seconds: float
    ) -> ProcessHandle:
        return _await_surface_handle(
            launch, timeout_seconds=timeout_seconds, error_type=ProcessError
        )

    @staticmethod
    def process_status(process_group: int, identity: str = "") -> str:
        return _process_status(
            process_group,
            identity,
            identity_probe=ProcessAdapter._current_identity,
        )

    @staticmethod
    def pid_status(pid: int, identity: str = "") -> str:
        return _pid_status(
            pid, identity, identity_probe=ProcessAdapter._current_identity
        )

    @staticmethod
    def _signal_exact(process_group: int, identity: str, sig: int) -> None:
        _signal_exact(
            process_group, identity, sig, error_type=ProcessError
        )

    @staticmethod
    def signal_owned_child_group(
        process_group: int, identity: str, sig: int
    ) -> None:
        _signal_owned_child_group(
            process_group,
            identity,
            sig,
            identity_probe=ProcessAdapter._current_identity,
            group_members_probe=ProcessAdapter._group_has_other_members,
            error_type=ProcessError,
        )

    @staticmethod
    def _group_has_other_members(
        process_group: int, leader_pid: int
    ) -> bool:
        return _group_has_other_members(process_group, leader_pid)

    @staticmethod
    def request_guardian_signal(
        control_path: Path,
        *,
        action: str,
        operation_id: str,
        run_id: str,
        process_group: int,
        process_identity: str,
        supervisor_pid: int,
        supervisor_identity: str,
    ) -> None:
        _request_guardian_signal(
            control_path,
            action=action,
            operation_id=operation_id,
            run_id=run_id,
            process_group=process_group,
            process_identity=process_identity,
            supervisor_pid=supervisor_pid,
            supervisor_identity=supervisor_identity,
            json_writer=ProcessAdapter._write_json,
            error_type=ProcessError,
        )

    @staticmethod
    def request_exit(process_group: int, identity: str = "") -> None:
        if process_group <= 1:
            raise ProcessError("invalid owned process group")
        ProcessAdapter._signal_exact(
            process_group, identity, signal.SIGTERM
        )

    @staticmethod
    def terminate_exact(process_group: int, identity: str = "") -> None:
        if process_group <= 1:
            raise ProcessError("invalid owned process group")
        ProcessAdapter._signal_exact(
            process_group, identity, signal.SIGKILL
        )
