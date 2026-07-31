"""Exact process-group launch, status, graceful exit, and termination."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence


SURFACE_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\Z"
)


class ProcessError(RuntimeError):
    pass


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


class ProcessAdapter:
    @staticmethod
    def _contain_unidentified_child(proc: subprocess.Popen[object]) -> None:
        """Synchronously reap a just-spawned child whose identity was not bound."""

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

    @staticmethod
    def _linux_process_record(pid: int) -> tuple[int, str, str]:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            boot_id = Path(
                "/proc/sys/kernel/random/boot_id"
            ).read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise ProcessLookupError(pid) from exc
        except OSError:
            raise
        closing = raw.rfind(")")
        if closing < 0 or not boot_id:
            raise OSError(errno.EIO, "invalid Linux process identity")
        fields = raw[closing + 1 :].split()
        try:
            process_group = int(fields[2])
            start_ticks = fields[19]
        except (IndexError, ValueError) as exc:
            raise OSError(errno.EIO, "invalid Linux process identity") from exc
        return process_group, start_ticks, boot_id

    @staticmethod
    def _darwin_session_file_id() -> str:
        path = "/private/var/run/bootSessionMA.txt"
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o022
                or info.st_size != 36
            ):
                raise OSError(
                    errno.EPERM,
                    "Darwin boot session file is not trusted",
                )
            raw = os.read(descriptor, 37)
        finally:
            os.close(descriptor)
        try:
            value = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise OSError(
                errno.EIO, "Darwin boot session is not ASCII"
            ) from exc
        if not re.fullmatch(
            r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}",
            value,
        ):
            raise OSError(errno.EIO, "Darwin boot session is invalid")
        return value.casefold()

    @staticmethod
    def _darwin_sysctl_boot_id(
        *,
        sysctlbyname: Callable[..., int] | None = None,
    ) -> str:
        class Timeval(ctypes.Structure):
            _fields_ = [
                ("tv_sec", ctypes.c_long),
                ("tv_usec", ctypes.c_int),
            ]

        if sysctlbyname is None:
            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            sysctlbyname = libc.sysctlbyname
            sysctlbyname.argtypes = [
                ctypes.c_char_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_size_t),
                ctypes.c_void_p,
                ctypes.c_size_t,
            ]
            sysctlbyname.restype = ctypes.c_int
        boot_time = Timeval()
        size = ctypes.c_size_t(ctypes.sizeof(boot_time))
        if sysctlbyname(
            b"kern.boottime",
            ctypes.byref(boot_time),
            ctypes.byref(size),
            None,
            0,
        ) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        if (
            size.value < ctypes.sizeof(boot_time)
            or boot_time.tv_sec <= 0
            or boot_time.tv_usec < 0
        ):
            raise OSError(errno.EIO, "invalid Darwin boot identity")
        return f"{boot_time.tv_sec}:{boot_time.tv_usec}"

    @staticmethod
    def _darwin_boot_id(
        *,
        sysctlbyname: Callable[..., int] | None = None,
    ) -> str:
        try:
            return ProcessAdapter._darwin_session_file_id()
        except OSError:
            pass
        try:
            return ProcessAdapter._darwin_sysctl_boot_id(
                sysctlbyname=sysctlbyname
            )
        except OSError:
            return "unavailable"

    @staticmethod
    def _darwin_process_fields(pid: int) -> tuple[int, str]:
        class ProcBsdInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = libproc.proc_pidinfo
        proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        proc_pidinfo.restype = ctypes.c_int
        info = ProcBsdInfo()
        size = ctypes.sizeof(info)
        read = proc_pidinfo(
            pid, 3, 0, ctypes.byref(info), size
        )
        if read <= 0:
            code = ctypes.get_errno()
            if code == errno.ESRCH:
                raise ProcessLookupError(pid)
            if code in {errno.EPERM, errno.EACCES}:
                raise PermissionError(code, os.strerror(code))
            raise OSError(code or errno.EIO, os.strerror(code or errno.EIO))
        if read < size or info.pbi_pid != pid:
            raise OSError(errno.EIO, "invalid Darwin process identity")
        started = f"{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"
        return int(info.pbi_pgid), started

    @staticmethod
    def _darwin_process_record(pid: int) -> tuple[int, str, str]:
        process_group, started = ProcessAdapter._darwin_process_fields(pid)
        return process_group, started, ProcessAdapter._darwin_boot_id()

    @staticmethod
    def _current_identity(pid: int) -> tuple[int, str]:
        if pid <= 1:
            raise ProcessLookupError(pid)
        if sys.platform == "linux":
            process_group, started, boot_id = (
                ProcessAdapter._linux_process_record(pid)
            )
        elif sys.platform == "darwin":
            process_group, started, boot_id = (
                ProcessAdapter._darwin_process_record(pid)
            )
        else:
            raise OSError(errno.ENOTSUP, "unsupported process identity platform")
        encoded = (
            f"{sys.platform}\0{boot_id}\0{pid}\0"
            f"{process_group}\0{started}"
        ).encode()
        return process_group, hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def capture_identity(pid: int, *, process_group: int = 0) -> str:
        try:
            actual_group, identity = ProcessAdapter._current_identity(pid)
        except ProcessLookupError:
            return ""
        except OSError as exc:
            raise ProcessError("process identity probe failed") from exc
        if process_group > 1 and actual_group != process_group:
            raise ProcessError("process identity belongs to a different group")
        return identity

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
        if not argv or not Path(argv[0]).is_absolute():
            raise ProcessError("runtime executable must be an absolute resolved path")
        proc = subprocess.Popen(
            list(argv), cwd=cwd, env=dict(env), stdin=stdin, stdout=stdout, stderr=stderr,
            start_new_session=True,
        )
        try:
            process_group = os.getpgid(proc.pid)
            identity = self.capture_identity(
                proc.pid, process_group=process_group
            )
        except (OSError, ProcessError) as exc:
            self._contain_unidentified_child(proc)
            raise ProcessError("started process identity is unavailable") from exc
        if not identity:
            self._contain_unidentified_child(proc)
            raise ProcessError("started process exited before identity capture")
        return ProcessHandle(
            proc.pid,
            process_group,
            process_identity=identity,
            _process=proc,
        )

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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

    def prepare_surface_launch(
        self,
        *,
        argv: Sequence[str],
        cwd: Path,
        state_root: Path,
        worker: Path,
        callback_pointer: Path,
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
        """Prepare a bounded worker spec without starting an external process."""

        registration = (
            callback_registration.resolve()
            if callback_registration is not None
            else state_root.resolve() / "callback-target.json"
        )
        resolved_state_root = state_root.resolve()
        if (
            not argv
            or not Path(argv[0]).is_absolute()
            or not worker.is_absolute()
            or not cwd.is_absolute()
            or not callback_pointer.is_absolute()
            or not registration.is_absolute()
            or not store_root.is_absolute()
        ):
            raise ProcessError("surface launch paths and runtime must be absolute")
        if registration.parent != resolved_state_root:
            raise ProcessError("callback registration must stay in launch state")
        if runtime not in {"claude", "codex"}:
            raise ProcessError("surface launch runtime is invalid")
        if callback_mode not in {
            "envelope",
            "task-summary",
            "research-fetch",
            "research-synth",
        }:
            raise ProcessError("surface callback mode is invalid")
        summary_pointer = (
            task_summary_pointer.resolve()
            if task_summary_pointer is not None
            else None
        )
        if callback_mode == "task-summary" and (
            summary_pointer is None
            or not summary_pointer.is_absolute()
            or not SURFACE_UUID.fullmatch(origin_surface)
        ):
            raise ProcessError(
                "task-summary mode requires an exact source and origin surface"
            )
        resolved_runtime_home = (
            runtime_home.expanduser().resolve()
            if runtime_home is not None
            else None
        )
        research_mode = callback_mode in {
            "research-fetch",
            "research-synth",
        }
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
                or not callback_wake
                or callback_wake != callback_wake.strip()
                or "\0" in callback_wake
                or "\n" in callback_wake
                or "\r" in callback_wake
                or len(callback_wake.encode()) > 4096
            ):
                raise ProcessError("research launch identity is invalid")
            if callback_mode == "research-fetch":
                if not re.fullmatch(
                    r"[0-9a-f]{64}", research_request_sha256
                ):
                    raise ProcessError(
                        "research fetch request identity is invalid"
                    )
            elif research_request_sha256:
                raise ProcessError(
                    "research synth request identity must be derived"
                )
        elif resolved_runtime_home is not None or research_request_sha256:
            raise ProcessError(
                "research launch fields require research callback mode"
            )
        elif callback_wake and (
            callback_mode != "envelope"
            or callback_wake != callback_wake.strip()
            or "\0" in callback_wake
            or "\n" in callback_wake
            or "\r" in callback_wake
            or len(callback_wake.encode()) > 4096
        ):
            raise ProcessError("review callback wake is invalid")
        if any(not isinstance(part, str) or "\0" in part for part in argv):
            raise ProcessError("surface launch argv is invalid")
        resolved_state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved_state_root.chmod(0o700)
        if not registration.exists():
            self._write_json(
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
        self._write_json(
            launch.spec_path,
            {
                "schema_version": 1,
                "argv": list(argv),
                "cwd": str(cwd),
                "callback_pointer": str(callback_pointer),
                "callback_mode": callback_mode,
                "task_summary_pointer": (
                    str(summary_pointer) if summary_pointer is not None else ""
                ),
                "origin_surface": origin_surface,
                "runtime_home": (
                    str(resolved_runtime_home)
                    if resolved_runtime_home is not None
                    else ""
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
            },
        )
        command = shlex.join(
            [str(Path(os.sys.executable).resolve()), str(worker), "--spec", str(launch.spec_path)]
        )
        return SurfaceLaunch(
            command=f"exec {command}",
            spec_path=launch.spec_path,
            ready_path=launch.ready_path,
            exit_path=launch.exit_path,
        )

    @staticmethod
    def await_surface_handle(
        launch: SurfaceLaunch, *, timeout_seconds: float
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
                raise ProcessError("runtime worker handshake is invalid") from exc
            if not isinstance(value, dict) or value.get("schema_version") != 1:
                raise ProcessError("runtime worker handshake has an invalid schema")
            if value.get("status") != "ready":
                raise ProcessError("runtime worker failed before provider readiness")
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
                raise ProcessError("runtime worker returned an invalid process identity")
            return ProcessHandle(
                pid,
                process_group,
                supervisor_pid,
                process_identity,
                supervisor_identity,
            )
        raise ProcessError("runtime worker readiness timed out")

    @staticmethod
    def process_status(process_group: int, identity: str = "") -> str:
        if process_group <= 1 or not re.fullmatch(r"[0-9a-f]{64}", identity):
            return "unknown"
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return "dead"
        except OSError:
            return "unknown"
        try:
            actual_group, actual_identity = ProcessAdapter._current_identity(
                process_group
            )
        except (ProcessLookupError, OSError):
            return "unknown"
        return (
            "alive"
            if actual_group == process_group and actual_identity == identity
            else "unknown"
        )

    @staticmethod
    def pid_status(pid: int, identity: str = "") -> str:
        if pid <= 1 or not re.fullmatch(r"[0-9a-f]{64}", identity):
            return "unknown"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "dead"
        except OSError:
            return "unknown"
        try:
            _process_group, actual_identity = ProcessAdapter._current_identity(pid)
        except (ProcessLookupError, OSError):
            return "unknown"
        return "alive" if actual_identity == identity else "unknown"

    @staticmethod
    def _signal_exact(process_group: int, identity: str, sig: int) -> None:
        del process_group, identity, sig
        raise ProcessError(
            "direct process-group mutation requires the sole parent guardian"
        )

    @staticmethod
    def signal_owned_child_group(
        process_group: int, identity: str, sig: int
    ) -> None:
        """Signal a direct child while WNOWAIT prevents PID/PGID recycling."""

        if (
            process_group <= 1
            or not re.fullmatch(r"[0-9a-f]{64}", identity)
            or sig not in {signal.SIGTERM, signal.SIGKILL}
        ):
            raise ProcessError("invalid owned child signal request")
        try:
            exited = os.waitid(
                os.P_PID,
                process_group,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError as exc:
            raise ProcessError(
                "process group is not owned by this guardian"
            ) from exc
        except OSError as exc:
            raise ProcessError("owned child guard failed") from exc
        if exited is None:
            try:
                actual_group, actual_identity = (
                    ProcessAdapter._current_identity(process_group)
                )
            except ProcessLookupError:
                return
            except OSError as exc:
                raise ProcessError(
                    "owned child identity probe failed"
                ) from exc
            if actual_group != process_group or actual_identity != identity:
                raise ProcessError("owned child process identity changed")
        try:
            os.killpg(process_group, sig)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            if (
                exited is not None
                and not ProcessAdapter._group_has_other_members(
                    process_group, process_group
                )
            ):
                return
            raise ProcessError("owned child group signal failed") from exc
        except OSError as exc:
            raise ProcessError("owned child group signal failed") from exc

    @staticmethod
    def _group_has_other_members(
        process_group: int, leader_pid: int
    ) -> bool:
        if sys.platform == "darwin":
            try:
                libproc = ctypes.CDLL(
                    "/usr/lib/libproc.dylib", use_errno=True
                )
                proc_listpids = libproc.proc_listpids
                proc_listpids.argtypes = [
                    ctypes.c_uint32,
                    ctypes.c_uint32,
                    ctypes.c_void_p,
                    ctypes.c_int,
                ]
                proc_listpids.restype = ctypes.c_int
                ctypes.set_errno(0)
                needed = proc_listpids(2, process_group, None, 0)
                if needed <= 0:
                    return ctypes.get_errno() != 0
                count = (
                    needed // ctypes.sizeof(ctypes.c_int) + 16
                )
                pids = (ctypes.c_int * count)()
                ctypes.set_errno(0)
                read = proc_listpids(
                    2,
                    process_group,
                    ctypes.byref(pids),
                    ctypes.sizeof(pids),
                )
            except OSError:
                return True
            if read <= 0:
                return ctypes.get_errno() != 0
            return any(
                pid > 1 and pid != leader_pid
                for pid in pids[: read // ctypes.sizeof(ctypes.c_int)]
            )
        if sys.platform == "linux":
            try:
                for entry in Path("/proc").iterdir():
                    if not entry.name.isdecimal():
                        continue
                    pid = int(entry.name)
                    if pid <= 1 or pid == leader_pid:
                        continue
                    try:
                        if os.getpgid(pid) == process_group:
                            return True
                    except ProcessLookupError:
                        continue
                    except OSError:
                        return True
            except OSError:
                return True
            return False
        return True

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
        """Publish one exact signal request for the sole parent guardian."""

        parent = control_path.parent
        try:
            parent_stat = parent.stat()
        except OSError as exc:
            raise ProcessError("process guardian state is unavailable") from exc
        if (
            action not in {"request-exit", "terminate"}
            or not control_path.is_absolute()
            or control_path.name != "process-control.json"
            or control_path.is_symlink()
            or not parent.is_dir()
            or parent.is_symlink()
            or parent_stat.st_uid != os.getuid()
            or parent_stat.st_mode & 0o077
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", operation_id
            )
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", run_id
            )
            or process_group <= 1
            or supervisor_pid <= 1
            or not re.fullmatch(r"[0-9a-f]{64}", process_identity)
            or not re.fullmatch(r"[0-9a-f]{64}", supervisor_identity)
        ):
            raise ProcessError("process guardian request is invalid")
        payload = {
            "schema_version": 1,
            "action": action,
            "operation_id": operation_id,
            "run_id": run_id,
            "process_group": process_group,
            "process_identity": process_identity,
            "supervisor_pid": supervisor_pid,
            "supervisor_identity": supervisor_identity,
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode()
        payload["command_id"] = hashlib.sha256(encoded).hexdigest()
        ProcessAdapter._write_json(control_path, payload)

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
