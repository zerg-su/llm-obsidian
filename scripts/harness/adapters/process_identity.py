"""Platform-specific, immutable process birth-identity probes."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


IdentityProbe = Callable[[int], tuple[int, str]]


@dataclass(frozen=True)
class DarwinProcessSnapshot:
    """Read-only libproc facts needed to reject stale ownership."""

    pid: int
    parent_pid: int
    process_group: int
    status: int
    started_at: str


def linux_process_record(pid: int) -> tuple[int, str, str]:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip()
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


def darwin_session_file_id() -> str:
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
            raise OSError(errno.EPERM, "Darwin boot session file is not trusted")
        raw = os.read(descriptor, 37)
    finally:
        os.close(descriptor)
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise OSError(errno.EIO, "Darwin boot session is not ASCII") from exc
    if not re.fullmatch(
        r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}", value
    ):
        raise OSError(errno.EIO, "Darwin boot session is invalid")
    return value.casefold()


def darwin_sysctl_boot_id(
    *, sysctlbyname: Callable[..., int] | None = None
) -> str:
    class Timeval(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_int)]

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
    if (
        sysctlbyname(
            b"kern.boottime",
            ctypes.byref(boot_time),
            ctypes.byref(size),
            None,
            0,
        )
        != 0
    ):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    if (
        size.value < ctypes.sizeof(boot_time)
        or boot_time.tv_sec <= 0
        or boot_time.tv_usec < 0
    ):
        raise OSError(errno.EIO, "invalid Darwin boot identity")
    return f"{boot_time.tv_sec}:{boot_time.tv_usec}"


def darwin_boot_id(
    *,
    session_file_id: Callable[[], str],
    sysctl_boot_id: Callable[..., str],
    sysctlbyname: Callable[..., int] | None = None,
) -> str:
    try:
        return session_file_id()
    except OSError:
        pass
    try:
        return sysctl_boot_id(sysctlbyname=sysctlbyname)
    except OSError:
        return "unavailable"


def darwin_process_snapshot(pid: int) -> DarwinProcessSnapshot:
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
    read = proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)
    if read <= 0:
        code = ctypes.get_errno()
        if code == errno.ESRCH:
            raise ProcessLookupError(pid)
        if code in {errno.EPERM, errno.EACCES}:
            raise PermissionError(code, os.strerror(code))
        raise OSError(code or errno.EIO, os.strerror(code or errno.EIO))
    if read < size or info.pbi_pid != pid:
        raise OSError(errno.EIO, "invalid Darwin process identity")
    return DarwinProcessSnapshot(
        pid=int(info.pbi_pid),
        parent_pid=int(info.pbi_ppid),
        process_group=int(info.pbi_pgid),
        status=int(info.pbi_status),
        started_at=f"{info.pbi_start_tvsec}:{info.pbi_start_tvusec}",
    )


def darwin_process_fields(pid: int) -> tuple[int, str]:
    snapshot = darwin_process_snapshot(pid)
    return snapshot.process_group, snapshot.started_at


def darwin_process_record(
    pid: int,
    *,
    fields_probe: Callable[[int], tuple[int, str]],
    boot_probe: Callable[[], str],
) -> tuple[int, str, str]:
    process_group, started = fields_probe(pid)
    return process_group, started, boot_probe()


def current_identity(
    pid: int,
    *,
    platform: str,
    linux_probe: Callable[[int], tuple[int, str, str]],
    darwin_probe: Callable[[int], tuple[int, str, str]],
) -> tuple[int, str]:
    if pid <= 1:
        raise ProcessLookupError(pid)
    if platform == "linux":
        process_group, started, boot_id = linux_probe(pid)
    elif platform == "darwin":
        process_group, started, boot_id = darwin_probe(pid)
    else:
        raise OSError(errno.ENOTSUP, "unsupported process identity platform")
    encoded = (
        f"{platform}\0{boot_id}\0{pid}\0{process_group}\0{started}"
    ).encode()
    return process_group, hashlib.sha256(encoded).hexdigest()


def capture_identity(
    pid: int,
    *,
    process_group: int,
    identity_probe: IdentityProbe,
    error_type: type[RuntimeError],
) -> str:
    try:
        actual_group, identity = identity_probe(pid)
    except ProcessLookupError:
        return ""
    except OSError as exc:
        raise error_type("process identity probe failed") from exc
    if process_group > 1 and actual_group != process_group:
        raise error_type("process identity belongs to a different group")
    return identity
