"""Lossy identity-bound cmux events.v1 hints for durable reconciliation."""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Mapping


UUID = re.compile(r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
PROTOCOL = "cmux-events"
PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024
EVENT_NAMES = frozenset(
    {
        "agent.hook.SessionStart",
        "agent.hook.PostToolUse",
        "agent.hook.Stop",
        "agent.hook.SessionEnd",
        "notification.created",
        "surface.created",
        "surface.closed",
        "workspace.created",
        "workspace.closed",
    }
)
SESSION_EVENTS = frozenset(
    {
        "agent.hook.SessionStart",
        "agent.hook.PostToolUse",
        "agent.hook.Stop",
        "agent.hook.SessionEnd",
    }
)
SURFACE_EVENTS = frozenset(
    {"notification.created", "surface.created", "surface.closed"}
)
WORKSPACE_EVENTS = frozenset({"workspace.created", "workspace.closed"})
WAKE_SOURCES = frozenset(
    {
        "cmux-event",
        "reconnect",
        "cursor-gap",
        "unavailable",
        "degraded",
        "fallback-poll",
        "stability-confirmation",
    }
)
EVENT_KEYS = frozenset(
    {
        "type",
        "protocol",
        "version",
        "boot_id",
        "seq",
        "id",
        "name",
        "category",
        "source",
        "occurred_at",
        "workspace_id",
        "surface_id",
        "pane_id",
        "window_id",
        "payload",
    }
)


@dataclass(frozen=True)
class WakeBinding:
    """Durable worker identity; cmux observations cannot change it."""

    runtime_root: Path
    workspace_id: str
    surface_id: str
    owner_id: str
    operation_id: str
    run_id: str
    generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_root, Path) or not self.runtime_root.is_absolute():
            raise ValueError("runtime_root must be an absolute path")
        if not UUID.fullmatch(self.workspace_id) or not UUID.fullmatch(self.surface_id):
            raise ValueError("cmux wake binding requires exact workspace and surface UUIDs")
        for value in (self.owner_id, self.operation_id, self.run_id):
            if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
                raise ValueError("cmux wake binding identity is invalid")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("cmux wake generation must be positive")


@dataclass(frozen=True)
class WakeObservation:
    """Bounded content-free reason to re-read existing durable authority."""

    source: str
    event_name: str = ""
    sequence: int = 0
    observed_at: float = 0.0

    def __post_init__(self) -> None:
        if self.source not in WAKE_SOURCES:
            raise ValueError("cmux wake source is invalid")
        if self.event_name and self.event_name not in EVENT_NAMES:
            raise ValueError("cmux wake event name is invalid")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("cmux wake sequence is invalid")
        if not isinstance(self.observed_at, (int, float)) or self.observed_at < 0:
            raise ValueError("cmux wake observation time is invalid")


def _integer(value: object, *, minimum: int = 0) -> int | None:
    if type(value) is not int or value < minimum:
        return None
    return value


def _uuid_or_none(value: object) -> str | None | bool:
    if value is None:
        return None
    if not isinstance(value, str) or not UUID.fullmatch(value):
        return False
    return value


class CmuxWakePolicy:
    """Pure closed-frame parser, cursor, and hierarchical identity policy."""

    def __init__(self, binding: WakeBinding, *, debounce_seconds: float = 0.05):
        self.binding = binding
        self.debounce_seconds = max(0.0, debounce_seconds)
        self._boot_id = ""
        self._subscription_id = ""
        self._external_session_id = ""
        self._last_sequence = 0
        self._last_wake_at = -1.0

    def observe(self, frame: object, observed_at: float) -> WakeObservation | None:
        if not isinstance(frame, dict) or not isinstance(observed_at, (int, float)):
            return WakeObservation("degraded", observed_at=max(0.0, float(observed_at or 0)))
        frame_type = frame.get("type")
        if frame_type == "ack":
            return self._observe_ack(frame, float(observed_at))
        if frame_type == "event":
            return self._observe_event(frame, float(observed_at))
        return WakeObservation("degraded", observed_at=float(observed_at))

    def _observe_ack(
        self, frame: Mapping[str, object], observed_at: float
    ) -> WakeObservation | None:
        if not self._valid_ack(frame):
            return WakeObservation("degraded", observed_at=observed_at)
        boot_id = str(frame["boot_id"])
        subscription_id = str(frame["subscription_id"])
        resume = frame["resume"]
        assert isinstance(resume, dict)
        sequence = int(resume["latest_seq"])
        gap = bool(resume["gap"])
        reconnect = bool(self._subscription_id)
        if self._boot_id and self._boot_id.casefold() != boot_id.casefold():
            self._external_session_id = ""
            self._last_sequence = 0
        self._boot_id = boot_id
        self._subscription_id = subscription_id
        if gap:
            return WakeObservation("cursor-gap", sequence=sequence, observed_at=observed_at)
        if reconnect:
            return WakeObservation("reconnect", sequence=sequence, observed_at=observed_at)
        return None

    @staticmethod
    def _valid_ack(frame: Mapping[str, object]) -> bool:
        required = {
            "type",
            "protocol",
            "version",
            "boot_id",
            "subscription_id",
            "heartbeat_interval_seconds",
            "replay_count",
            "resume",
            "filters",
        }
        if set(frame) != required or frame.get("protocol") != PROTOCOL:
            return False
        if frame.get("version") != PROTOCOL_VERSION or type(frame.get("version")) is not int:
            return False
        if not isinstance(frame.get("boot_id"), str) or not UUID.fullmatch(str(frame["boot_id"])):
            return False
        if not isinstance(frame.get("subscription_id"), str) or not UUID.fullmatch(str(frame["subscription_id"])):
            return False
        heartbeat = frame.get("heartbeat_interval_seconds")
        if isinstance(heartbeat, bool) or not isinstance(heartbeat, (int, float)) or heartbeat <= 0:
            return False
        if _integer(frame.get("replay_count")) is None:
            return False
        filters = frame.get("filters")
        if filters != {"names": sorted(EVENT_NAMES), "categories": []}:
            return False
        return CmuxWakePolicy._valid_resume(frame.get("resume"))

    @staticmethod
    def _valid_resume(value: object) -> bool:
        if not isinstance(value, dict):
            return False
        gap = value.get("gap")
        keys = {
            "after_seq",
            "requested_after_seq",
            "oldest_seq",
            "latest_seq",
            "next_seq",
            "gap",
        }
        if gap is True:
            keys.add("gap_reason")
        if set(value) != keys or type(gap) is not bool:
            return False
        after = value.get("after_seq")
        if after is not None and _integer(after) is None:
            return False
        requested = _integer(value.get("requested_after_seq"))
        oldest = _integer(value.get("oldest_seq"))
        latest = _integer(value.get("latest_seq"))
        next_seq = _integer(value.get("next_seq"), minimum=1)
        if None in {requested, oldest, latest, next_seq} or next_seq != latest + 1:
            return False
        reason = value.get("gap_reason")
        return not gap or (isinstance(reason, str) and 0 < len(reason) <= 256)

    def _observe_event(
        self, frame: Mapping[str, object], observed_at: float
    ) -> WakeObservation | None:
        if any(
            _uuid_or_none(frame.get(field)) is False
            for field in ("pane_id", "window_id")
        ):
            return None
        if (
            "payload_truncated" in frame
            and type(frame["payload_truncated"]) is not bool
        ):
            return None
        valid = self._valid_event(frame)
        if valid is None:
            return WakeObservation("degraded", observed_at=observed_at)
        boot_id, sequence, name, workspace, surface, payload = valid
        if not self._boot_id or self._boot_id.casefold() != boot_id.casefold():
            return None
        if sequence <= self._last_sequence:
            return None
        self._last_sequence = sequence
        if name not in EVENT_NAMES:
            return None
        if not self._routes(name, workspace, surface, payload):
            return None
        if self._last_wake_at >= 0 and observed_at - self._last_wake_at < self.debounce_seconds:
            return None
        self._last_wake_at = observed_at
        return WakeObservation("cmux-event", name, sequence, observed_at)

    @staticmethod
    def _valid_event(
        frame: Mapping[str, object],
    ) -> tuple[str, int, str, str | None, str | None, Mapping[str, object]] | None:
        keys = set(frame)
        if keys not in {EVENT_KEYS, EVENT_KEYS | {"payload_truncated"}}:
            return None
        if frame.get("protocol") != PROTOCOL or frame.get("version") != PROTOCOL_VERSION:
            return None
        if type(frame.get("version")) is not int:
            return None
        boot_id = frame.get("boot_id")
        sequence = _integer(frame.get("seq"), minimum=1)
        name = frame.get("name")
        if not isinstance(boot_id, str) or not UUID.fullmatch(boot_id) or sequence is None:
            return None
        if frame.get("id") != f"{boot_id}-{sequence}":
            return None
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
            return None
        if frame.get("category") != name.split(".", 1)[0]:
            return None
        for field in ("source", "occurred_at"):
            if not isinstance(frame.get(field), str) or not 0 < len(str(frame[field])) <= 128:
                return None
        workspace = _uuid_or_none(frame.get("workspace_id"))
        surface = _uuid_or_none(frame.get("surface_id"))
        if workspace is False or surface is False or not isinstance(frame.get("payload"), dict):
            return None
        if frame.get("payload_truncated") is True:
            return (boot_id, sequence, name, None, None, {})
        return (boot_id, sequence, name, workspace, surface, frame["payload"])

    def _routes(
        self,
        name: str,
        workspace: str | None,
        surface: str | None,
        payload: Mapping[str, object],
    ) -> bool:
        workspace_matches = workspace is None or workspace.casefold() == self.binding.workspace_id.casefold()
        surface_matches = surface is None or surface.casefold() == self.binding.surface_id.casefold()
        if name in SESSION_EVENTS:
            session_id = payload.get("session_id")
            if payload.get("phase") != "completed" or not isinstance(session_id, str) or not IDENTIFIER.fullmatch(session_id):
                return False
            if name == "agent.hook.SessionStart":
                if workspace is None or surface is None or not workspace_matches or not surface_matches:
                    return False
                if self._external_session_id and self._external_session_id != session_id:
                    return False
                self._external_session_id = session_id
                return True
            return bool(self._external_session_id == session_id and workspace_matches and surface_matches)
        if name in SURFACE_EVENTS:
            return bool(workspace is not None and surface is not None and workspace_matches and surface_matches)
        if name in WORKSPACE_EVENTS:
            return bool(workspace is not None and workspace_matches and surface_matches)
        return False


Runner = Callable[..., subprocess.CompletedProcess[str]]
Popen = Callable[..., object]
WaitReadable = Callable[[BinaryIO, float], bool]


def _wait_readable(stream: BinaryIO, timeout: float) -> bool:
    selector = selectors.DefaultSelector()
    try:
        selector.register(stream, selectors.EVENT_READ)
        return bool(selector.select(max(0.0, timeout)))
    finally:
        selector.close()


class CmuxWakeSource:
    """Own exactly one optional filtered cmux event subprocess."""

    def __init__(
        self,
        binding: WakeBinding,
        *,
        binary: str = "cmux",
        runner: Runner | None = None,
        popen: Popen | None = None,
        wait_readable: WaitReadable | None = None,
        monotonic: Callable[[], float] | None = None,
    ):
        self.binding = binding
        self.binary = binary
        self.runner = runner or subprocess.run
        self.popen = popen or subprocess.Popen
        self.wait_readable = wait_readable or _wait_readable
        self.monotonic = monotonic or time.monotonic
        self.policy = CmuxWakePolicy(binding)
        self.process: object | None = None
        self._capability: bool | None = None
        self._frame_buffer = bytearray()
        self._degraded = False
        self._closed = False

    def start(self) -> bool:
        """Subscribe before provider launch so replay can cover that window."""

        return not self._closed and self._ensure_started()

    def retry(self) -> bool:
        """Retry capability/process acquisition after a bounded backoff."""

        if self._closed:
            return False
        self._degraded = False
        started = self._ensure_started()
        self._degraded = not started
        return started

    def refresh_generation(self, generation: int) -> None:
        """Refresh callback routing identity without changing process scope."""

        if type(generation) is not int or generation < 1:
            return
        binding = WakeBinding(
            runtime_root=self.binding.runtime_root,
            workspace_id=self.binding.workspace_id,
            surface_id=self.binding.surface_id,
            owner_id=self.binding.owner_id,
            operation_id=self.binding.operation_id,
            run_id=self.binding.run_id,
            generation=generation,
        )
        self.binding = binding
        self.policy.binding = binding

    def wait(self, timeout: float) -> WakeObservation | None:
        if self._closed or not isinstance(timeout, (int, float)) or timeout < 0:
            return WakeObservation("unavailable", observed_at=self.monotonic())
        if self._degraded:
            return WakeObservation("unavailable", observed_at=self.monotonic())
        if not self._ensure_started():
            self._degraded = True
            return WakeObservation("unavailable", observed_at=self.monotonic())
        deadline = self.monotonic() + timeout
        while self.process is not None:
            stream = getattr(self.process, "stdout", None)
            if stream is None:
                return self._degrade()
            newline = self._frame_buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self._frame_buffer[: newline + 1])
                del self._frame_buffer[: newline + 1]
                if len(raw) > MAX_FRAME_BYTES + 1:
                    return self._degrade()
                try:
                    frame = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return self._degrade()
                observation = self.policy.observe(frame, self.monotonic())
                if observation is not None:
                    if observation.source == "degraded":
                        self._close_process()
                    return observation
                if self.monotonic() >= deadline:
                    return None
                continue
            if len(self._frame_buffer) >= MAX_FRAME_BYTES + 2:
                return self._degrade()
            remaining = max(0.0, deadline - self.monotonic())
            if not self.wait_readable(stream, remaining):
                if self._frame_buffer:
                    return self._degrade()
                if getattr(self.process, "poll")() is not None:
                    return self._degrade()
                return None
            try:
                chunk = self._read_available(
                    stream,
                    MAX_FRAME_BYTES + 2 - len(self._frame_buffer),
                )
            except (OSError, ValueError):
                return self._degrade()
            if not isinstance(chunk, bytes) or not chunk:
                return self._degrade()
            self._frame_buffer.extend(chunk)
        return WakeObservation("degraded", observed_at=self.monotonic())

    @staticmethod
    def _read_available(stream: BinaryIO, limit: int) -> bytes:
        try:
            descriptor = stream.fileno()
        except (AttributeError, OSError):
            return stream.read(limit)
        return os.read(descriptor, limit)

    def _ensure_started(self) -> bool:
        if self.process is not None:
            return True
        cursor = self._cursor_path()
        if cursor is None or not self._has_capability():
            return False
        argv = [self.binary, "events", "--cursor-file", str(cursor), "--reconnect", "--no-heartbeat"]
        for name in sorted(EVENT_NAMES):
            argv.extend(("--name", name))
        environment = dict(os.environ)
        for key in ("CMUX_PANE_ID", "CMUX_SURFACE_ID", "CMUX_WINDOW_ID", "CMUX_WORKSPACE_ID"):
            environment.pop(key, None)
        try:
            self.process = self.popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                bufsize=0,
            )
        except (OSError, ValueError):
            self.process = None
            return False
        return getattr(self.process, "stdout", None) is not None

    def _has_capability(self) -> bool:
        if self._capability is not None:
            return self._capability
        try:
            result = self.runner(
                [self.binary, "events", "--help"],
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError:
            self._capability = False
            return False
        output = (result.stdout + result.stderr)[:20_000]
        required = ("--cursor-file", "--name", "--reconnect", "--no-heartbeat")
        self._capability = result.returncode == 0 and all(token in output for token in required)
        return self._capability

    def _cursor_path(self) -> Path | None:
        root = self.binding.runtime_root
        if not root.is_dir() or root.is_symlink():
            return None
        cursor = root / "cmux-events.cursor"
        if cursor.is_symlink() or (cursor.exists() and not cursor.is_file()):
            return None
        if cursor.exists():
            try:
                raw = cursor.read_text(encoding="utf-8")
            except OSError:
                return None
            if not raw.strip().isdigit() or len(raw) > 32:
                return None
        return cursor

    def _degrade(self) -> WakeObservation:
        observation = WakeObservation("degraded", observed_at=self.monotonic())
        self._degraded = True
        self._close_process()
        return observation

    def _close_process(self) -> None:
        process, self.process = self.process, None
        self._frame_buffer.clear()
        if process is None:
            return
        try:
            if getattr(process, "poll")() is None:
                getattr(process, "terminate")()
            getattr(process, "wait")(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                getattr(process, "kill")()
                getattr(process, "wait")(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
        stream = getattr(process, "stdout", None)
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass

    def close(self) -> None:
        self._closed = True
        self._close_process()
