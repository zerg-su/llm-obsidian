"""Exact status, sole-parent signaling, and guardian request policy."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import signal
import sys
from collections.abc import Callable
from pathlib import Path

from .process_identity import DarwinProcessSnapshot


IdentityProbe = Callable[[int], tuple[int, str]]
DarwinSnapshotProbe = Callable[[int], DarwinProcessSnapshot]


def _group_status(
    process_group: int, identity: str, *, identity_probe: IdentityProbe
) -> tuple[str, bool]:
    if process_group <= 1 or not re.fullmatch(r"[0-9a-f]{64}", identity):
        return "unknown", False
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return "dead", False
    except PermissionError as exc:
        return "unknown", exc.errno in {errno.EPERM, errno.EACCES}
    except OSError:
        return "unknown", False
    try:
        actual_group, actual_identity = identity_probe(process_group)
    except (ProcessLookupError, OSError):
        return "unknown", False
    return (
        "alive"
        if actual_group == process_group and actual_identity == identity
        else "unknown",
        False,
    )


def _pid_status(
    pid: int, identity: str, *, identity_probe: IdentityProbe
) -> tuple[str, bool]:
    if pid <= 1 or not re.fullmatch(r"[0-9a-f]{64}", identity):
        return "unknown", False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead", False
    except PermissionError as exc:
        return "unknown", exc.errno in {errno.EPERM, errno.EACCES}
    except OSError:
        return "unknown", False
    try:
        _process_group, actual_identity = identity_probe(pid)
    except (ProcessLookupError, OSError):
        return "unknown", False
    return ("alive" if actual_identity == identity else "unknown", False)


def process_status(
    process_group: int, identity: str, *, identity_probe: IdentityProbe
) -> str:
    return _group_status(
        process_group, identity, identity_probe=identity_probe
    )[0]


def pid_status(pid: int, identity: str, *, identity_probe: IdentityProbe) -> str:
    return _pid_status(pid, identity, identity_probe=identity_probe)[0]


def exact_statuses(
    process_group: int,
    process_identity: str,
    supervisor_pid: int,
    supervisor_identity: str,
    *,
    platform: str,
    identity_probe: IdentityProbe,
    darwin_snapshot_probe: DarwinSnapshotProbe,
) -> tuple[str, str]:
    """Prove one Darwin EPERM pair alive without delivering a signal."""

    process, process_denied = _group_status(
        process_group, process_identity, identity_probe=identity_probe
    )
    supervisor, supervisor_denied = _pid_status(
        supervisor_pid, supervisor_identity, identity_probe=identity_probe
    )
    if (
        platform != "darwin"
        or not process_denied
        or not supervisor_denied
    ):
        return process, supervisor
    try:
        child = darwin_snapshot_probe(process_group)
        parent = darwin_snapshot_probe(supervisor_pid)
        child_group, child_identity = identity_probe(process_group)
        parent_group, parent_identity = identity_probe(supervisor_pid)
    except (ProcessLookupError, OSError):
        return "unknown", "unknown"
    exact_running_pair = bool(
        child.pid == process_group
        and child.process_group == process_group
        and child.parent_pid == supervisor_pid
        and child.status == 2
        and parent.pid == supervisor_pid
        and parent.status == 2
        and child_group == process_group
        and child_identity == process_identity
        and parent_group == parent.process_group
        and parent_identity == supervisor_identity
    )
    return ("alive", "alive") if exact_running_pair else ("unknown", "unknown")


def signal_exact(
    process_group: int,
    identity: str,
    sig: int,
    *,
    error_type: type[RuntimeError],
) -> None:
    del process_group, identity, sig
    raise error_type(
        "direct process-group mutation requires the sole parent guardian"
    )


def signal_owned_child_group(
    process_group: int,
    identity: str,
    sig: int,
    *,
    identity_probe: IdentityProbe,
    group_members_probe: Callable[[int, int], bool],
    error_type: type[RuntimeError],
) -> None:
    """Signal a direct child while WNOWAIT prevents PID/PGID recycling."""

    if (
        process_group <= 1
        or not re.fullmatch(r"[0-9a-f]{64}", identity)
        or sig not in {signal.SIGTERM, signal.SIGKILL}
    ):
        raise error_type("invalid owned child signal request")
    try:
        exited = os.waitid(
            os.P_PID,
            process_group,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
    except ChildProcessError as exc:
        raise error_type("process group is not owned by this guardian") from exc
    except OSError as exc:
        raise error_type("owned child guard failed") from exc
    if exited is None:
        try:
            actual_group, actual_identity = identity_probe(process_group)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise error_type("owned child identity probe failed") from exc
        if actual_group != process_group or actual_identity != identity:
            raise error_type("owned child process identity changed")
    try:
        os.killpg(process_group, sig)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        if exited is not None and not group_members_probe(
            process_group, process_group
        ):
            return
        raise error_type("owned child group signal failed") from exc
    except OSError as exc:
        raise error_type("owned child group signal failed") from exc


def group_has_other_members(process_group: int, leader_pid: int) -> bool:
    if sys.platform == "darwin":
        return _darwin_group_has_other_members(process_group, leader_pid)
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


def _darwin_group_has_other_members(process_group: int, leader_pid: int) -> bool:
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
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
        count = needed // ctypes.sizeof(ctypes.c_int) + 16
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
    json_writer: Callable[[Path, object], None],
    error_type: type[RuntimeError],
) -> None:
    """Validate and publish one signal request for the sole parent guardian."""

    parent = control_path.parent
    try:
        parent_stat = parent.stat()
    except OSError as exc:
        raise error_type("process guardian state is unavailable") from exc
    if (
        action not in {"request-exit", "terminate"}
        or not control_path.is_absolute()
        or control_path.name != "process-control.json"
        or control_path.is_symlink()
        or not parent.is_dir()
        or parent.is_symlink()
        or parent_stat.st_uid != os.getuid()
        or parent_stat.st_mode & 0o077
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", operation_id)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", run_id)
        or process_group <= 1
        or supervisor_pid <= 1
        or not re.fullmatch(r"[0-9a-f]{64}", process_identity)
        or not re.fullmatch(r"[0-9a-f]{64}", supervisor_identity)
    ):
        raise error_type("process guardian request is invalid")
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
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["command_id"] = hashlib.sha256(encoded).hexdigest()
    json_writer(control_path, payload)
