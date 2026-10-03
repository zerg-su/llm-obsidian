"""Exact Codex editor-buffer observations; bodies never enter durable receipts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import time
import uuid
from pathlib import Path


def _private_directory(root: Path) -> None:
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("composer authority must not follow symlinks")
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("composer authority must be owner-only")


def _read_private(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077
        ):
            raise ValueError("composer input must be an owner-only regular file")
        data = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if (
            len(data) > limit or len(data) != info.st_size
            or after.st_size != info.st_size
            or after.st_mtime_ns != info.st_mtime_ns
        ):
            raise ValueError("composer input is oversized or changed")
        return data
    finally:
        os.close(fd)


def _publish(path: Path, value: dict) -> None:
    temporary = path.with_name("." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
    finally:
        temporary.unlink(missing_ok=True)


def configure_environment(spec: dict, values: dict[str, str]) -> dict[str, str]:
    """Configure only owned interactive Codex children, including cmux key compatibility."""
    if (
        spec.get("runtime") != "codex"
        or spec.get("callback_mode") in {"research-fetch", "research-synth"}
    ):
        return values
    if not isinstance(spec.get("ready_path"), Path):
        return values  # Historical environment-only callers have no registered child.
    root = spec["ready_path"].parent / "composer-observation"
    _private_directory(root)
    result = dict(values)
    result["VISUAL"] = shlex.join([sys.executable, str(Path(__file__).resolve()), str(root)])
    result["CODEX_TUI_DISABLE_KEYBOARD_ENHANCEMENT"] = "1"
    return result


def export_digest(root: Path, seed: Path) -> None:
    """Called by Codex's editor handoff, not by a model or arbitrary shell input."""
    _private_directory(root)
    request = json.loads(_read_private(root / "request.json", 4096))
    if set(request) != {"nonce", "process_group", "surface_id"}:
        raise ValueError("composer request shape changed")
    if not re.fullmatch(r"[0-9a-f]{32}", request["nonce"]):
        raise ValueError("composer nonce is invalid")
    if type(request["process_group"]) is not int or request["process_group"] <= 1:
        raise ValueError("composer process group is invalid")
    if os.getpgid(os.getppid()) != request["process_group"]:
        raise ValueError("editor caller is not the owned provider group")
    raw = _read_private(seed, 65536)
    raw.decode("utf-8", errors="strict")
    _publish(root / (request["nonce"] + ".json"), {
        **request, "sha256": hashlib.sha256(raw).hexdigest(), "byte_count": len(raw),
    })


class ComposerPort:
    """Own the serialized nonce protocol and exact-surface editor handoff."""

    def __init__(self, port: object, runtime_root: Path, process_group: int):
        self.port = port
        self.root = runtime_root / "composer-observation"
        self.process_group = process_group
        _private_directory(self.root)

    def read(self, surface_id: str) -> str:
        return self.port.read(surface_id)

    def send(self, surface_id: str, text: str) -> None:
        self.port.send(surface_id, text)

    def send_key(self, surface_id: str, key: str) -> None:
        self.port.send_key(surface_id, key)

    def agent_status(self, workspace_id: str, runtime: str) -> str:
        return self.port.agent_status(workspace_id, runtime)

    def observe_composer(self, surface_id: str) -> tuple[str, int] | None:
        from .runtime_session_continuation import classify_continuation_screen

        if self.process_group <= 1:
            return None
        state = classify_continuation_screen("codex", self.port.read(surface_id), "")
        if state not in {"idle", "input-ready"}:
            return None
        nonce = uuid.uuid4().hex
        request = {"nonce": nonce, "process_group": self.process_group, "surface_id": surface_id}
        receipt = self.root / (nonce + ".json")
        fd = os.open(self.root / "lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            info = os.fstat(lock.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid() or info.st_mode & 0o077
            ):
                return None
            fcntl.flock(lock, fcntl.LOCK_EX)
            pending = self.root / "request.json"
            if pending.exists() or pending.is_symlink():
                return None  # An interrupted handoff cannot be silently superseded.
            _publish(pending, request)
            completed = False
            try:
                self.port.send_key(surface_id, "ctrl+g")
                for _ in range(40):
                    if receipt.exists():
                        value = json.loads(_read_private(receipt, 4096))
                        if (
                            set(value) != {*request, "sha256", "byte_count"}
                            or any(value[k] != v for k, v in request.items())
                        ):
                            return None
                        if (
                            not isinstance(value["sha256"], str)
                            or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
                            or type(value["byte_count"]) is not int
                            or not 0 <= value["byte_count"] <= 65536
                        ):
                            return None
                        # Receipt publication precedes editor exit/TUI restoration.
                        screen = self.port.read(surface_id)
                        state = classify_continuation_screen("codex", screen, "")
                        if (
                            "Save and close external editor to continue." not in screen
                            and state in {"idle", "input-ready"}
                        ):
                            completed = True
                            return value["sha256"], value["byte_count"]
                    time.sleep(0.05)
                return None
            except (OSError, ValueError, TypeError, RuntimeError):
                return None
            finally:
                # A late helper must never adopt a replacement request's nonce.
                if completed:
                    pending.unlink(missing_ok=True)
                    receipt.unlink(missing_ok=True)


def observation_port(port: object, runtime: str, runtime_root: Path, process_group: int) -> object:
    # Test adapters supply their own logical source; native adapters use the handoff.
    if runtime != "codex" or callable(getattr(port, "observe_composer", None)):
        return port
    return ComposerPort(port, runtime_root, process_group)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 3:
            raise ValueError("invalid editor arguments")
        export_digest(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, ValueError, TypeError):
        pass
    # Codex applies trim_end on a successful editor exit. An unsuccessful exit
    # preserves the draft; the nonce receipt is our sole success authority.
    raise SystemExit(75)
