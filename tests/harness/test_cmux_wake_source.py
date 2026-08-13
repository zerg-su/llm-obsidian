#!/usr/bin/env python3
"""Strict non-authoritative cmux events.v1 wake contract."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.cmux_wake_source import (  # noqa: E402
    EVENT_NAMES,
    CmuxWakePolicy,
    CmuxWakeSource,
    WakeBinding,
    WakeObservation,
)


WORKSPACE = "11111111-1111-4111-8111-111111111111"
OTHER_WORKSPACE = "99999999-9999-4999-8999-999999999999"
SURFACE = "22222222-2222-4222-8222-222222222222"
OTHER_SURFACE = "33333333-3333-4333-8333-333333333333"
BOOT = "44444444-4444-4444-8444-444444444444"
SUBSCRIPTION = "55555555-5555-4555-8555-555555555555"
SESSION = "session-exact"


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def binding(root: Path) -> WakeBinding:
    return WakeBinding(
        runtime_root=root,
        workspace_id=WORKSPACE,
        surface_id=SURFACE,
        owner_id="owner-exact",
        operation_id="operation-exact",
        run_id="run-exact",
        generation=3,
    )


def ack(
    *,
    boot: str = BOOT,
    subscription: str = SUBSCRIPTION,
    gap: bool = False,
) -> dict[str, object]:
    resume: dict[str, object] = {
        "after_seq": None,
        "requested_after_seq": 0,
        "oldest_seq": 1,
        "latest_seq": 0,
        "next_seq": 1,
        "gap": gap,
    }
    if gap:
        resume["gap_reason"] = (
            "requested sequence is older than the retained in-memory event log"
        )
    return {
        "type": "ack",
        "protocol": "cmux-events",
        "version": 1,
        "boot_id": boot,
        "subscription_id": subscription,
        "heartbeat_interval_seconds": 15,
        "replay_count": 0,
        "resume": resume,
        "filters": {"names": sorted(EVENT_NAMES), "categories": []},
    }


def event(
    name: str,
    seq: int,
    *,
    workspace: str | None = WORKSPACE,
    surface: str | None = SURFACE,
    session: str | None = SESSION,
    phase: str = "completed",
    boot: str = BOOT,
    payload_extra: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"phase": phase}
    if session is not None:
        payload["session_id"] = session
    payload.update(payload_extra or {})
    return {
        "type": "event",
        "protocol": "cmux-events",
        "version": 1,
        "boot_id": boot,
        "seq": seq,
        "id": f"{boot}-{seq}",
        "name": name,
        "category": name.split(".", 1)[0],
        "source": "test",
        "occurred_at": "2026-08-13T19:00:00.000Z",
        "workspace_id": workspace,
        "surface_id": surface,
        "pane_id": None,
        "window_id": None,
        "payload": payload,
    }


with tempfile.TemporaryDirectory() as raw_root:
    policy = CmuxWakePolicy(binding(Path(raw_root)))
    check("initial exact acknowledgement is not itself a reconnect", policy.observe(ack(), 1.0) is None)

    started = policy.observe(event("agent.hook.SessionStart", 1), 1.1)
    check(
        "completed exact SessionStart binds the external session and wakes",
        started == WakeObservation("cmux-event", "agent.hook.SessionStart", 1, 1.1),
    )
    later = policy.observe(
        event(
            "agent.hook.PostToolUse",
            2,
            workspace=None,
            surface=None,
        ),
        1.2,
    )
    check(
        "bound session hook may omit surface and workspace",
        later == WakeObservation("cmux-event", "agent.hook.PostToolUse", 2, 1.2),
    )

    ignored = (
        event("agent.hook.PreToolUse", 3),
        event("agent.hook.Stop", 4, phase="started"),
        event("agent.hook.Stop", 5, session="other-session"),
        event("agent.hook.Stop", 6, workspace=OTHER_WORKSPACE),
        event("agent.hook.Stop", 7, surface=OTHER_SURFACE),
        event("feed.item.received", 8),
        event("surface.focused", 9),
        event("workspace.prompt.submitted", 10),
    )
    check(
        "unknown incomplete noisy and contradictory hook frames do not wake",
        all(policy.observe(candidate, 2.0) is None for candidate in ignored),
    )

    notification = policy.observe(event("notification.created", 11, session=None), 2.1)
    surface = policy.observe(event("surface.closed", 12, session=None), 2.2)
    workspace = policy.observe(
        event("workspace.closed", 13, surface=None, session=None), 2.3
    )
    check(
        "notification surface and workspace events use hierarchical exact routing",
        [item.event_name for item in (notification, surface, workspace) if item]
        == ["notification.created", "surface.closed", "workspace.closed"],
    )
    check(
        "dashboard surface and foreign workspace remain isolated",
        policy.observe(
            event("notification.created", 14, surface=OTHER_SURFACE, session=None),
            2.4,
        )
        is None
        and policy.observe(
            event(
                "workspace.created",
                15,
                workspace=OTHER_WORKSPACE,
                surface=None,
                session=None,
            ),
            2.5,
        )
        is None,
    )
    check(
        "duplicate stale and contradictory source frames remain no-wake",
        policy.observe(event("surface.created", 15, session=None), 2.6) is None
        and policy.observe(event("surface.created", 14, session=None), 2.7) is None
        and policy.observe(
            event("surface.created", 16, boot="66666666-6666-4666-8666-666666666666", session=None),
            2.8,
        )
        is None,
    )

    reconnected = policy.observe(
        ack(subscription="77777777-7777-4777-8777-777777777777"), 3.0
    )
    check("a subsequent exact acknowledgement is a reconnect wake", reconnected.source == "reconnect")
    gap = policy.observe(
        ack(
            subscription="88888888-8888-4888-8888-888888888888",
            gap=True,
        ),
        3.1,
    )
    check("an acknowledged cursor gap is an immediate cursor-gap wake", gap.source == "cursor-gap")


with tempfile.TemporaryDirectory() as raw_root:
    unbound = CmuxWakePolicy(binding(Path(raw_root)))
    unbound.observe(ack(), 1.0)
    check(
        "unbound session hooks never wake",
        unbound.observe(event("agent.hook.Stop", 1), 1.1) is None,
    )
    unbound.observe(
        event("agent.hook.SessionStart", 2, surface=OTHER_SURFACE), 1.2
    )
    check(
        "contradictory SessionStart never creates a binding",
        unbound.observe(event("agent.hook.Stop", 3), 1.3) is None,
    )


def degraded(label: str, raw: object) -> None:
    with tempfile.TemporaryDirectory() as raw_root:
        current = CmuxWakePolicy(binding(Path(raw_root)))
        observation = current.observe(raw, 4.0)
        check(label, observation is not None and observation.source == "degraded")


degraded("non-object frame degrades without interpreting content", [])
degraded("wrong protocol degrades", {**ack(), "protocol": "events.v2"})
degraded("wrong scalar types degrade", {**ack(), "version": True})
degraded("heartbeat frame degrades because launch disables heartbeats", {"type": "heartbeat"})
degraded("unknown control frame degrades", {"type": "attention"})


class FakeProcess:
    def __init__(self, lines: list[dict[str, object] | bytes]):
        encoded = b"".join(
            item if isinstance(item, bytes) else json.dumps(item).encode() + b"\n"
            for item in lines
        )
        self.stdout = io.BytesIO(encoded)
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("cmux", timeout)
        return self.returncode


class Factory:
    def __init__(self, process: FakeProcess):
        self.process = process
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> FakeProcess:
        self.calls.append((argv, kwargs))
        return self.process


def capable(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        argv,
        0,
        "--cursor-file --name --reconnect --no-heartbeat",
        "",
    )


with tempfile.TemporaryDirectory() as raw_root:
    process = FakeProcess([ack(), event("agent.hook.SessionStart", 1)])
    factory = Factory(process)
    source = CmuxWakeSource(
        binding(Path(raw_root)),
        binary="/opt/cmux/bin/cmux",
        runner=capable,
        popen=factory,
        wait_readable=lambda stream, timeout: stream.tell()
        < len(stream.getbuffer()),
        monotonic=lambda: 10.0,
    )
    observation = source.wait(0.5)
    argv, kwargs = factory.calls[0]
    check(
        "adapter compiles one fixed filtered reconnecting subscription",
        argv[:4]
        == [
            "/opt/cmux/bin/cmux",
            "events",
            "--cursor-file",
            str(Path(raw_root) / "cmux-events.cursor"),
        ]
        and "--reconnect" in argv
        and "--no-heartbeat" in argv
        and argv.count("--name") == len(EVENT_NAMES)
        and kwargs["stdin"] is subprocess.DEVNULL
        and kwargs["stderr"] is subprocess.DEVNULL,
    )
    check(
        "adapter returns only content-free wake facts",
        observation == WakeObservation("cmux-event", "agent.hook.SessionStart", 1, 10.0)
        and not hasattr(observation, "payload")
        and "prompt" not in observation.__dict__,
    )
    source.close()
    check("adapter closes its exact child process", process.terminated and not process.killed)


with tempfile.TemporaryDirectory() as raw_root:
    process = FakeProcess([b"{" + b"x" * 20_000 + b"}\n"])
    source = CmuxWakeSource(
        binding(Path(raw_root)),
        runner=capable,
        popen=Factory(process),
        wait_readable=lambda stream, timeout: True,
        monotonic=lambda: 11.0,
    )
    check("oversized NDJSON degrades to polling", source.wait(0.1).source == "degraded")
    source.close()


for label, lines in (
    ("malformed NDJSON degrades to polling", [b"{\n"]),
    ("event-stream EOF degrades to polling", []),
):
    with tempfile.TemporaryDirectory() as raw_root:
        process = FakeProcess(lines)
        source = CmuxWakeSource(
            binding(Path(raw_root)),
            runner=capable,
            popen=Factory(process),
            wait_readable=lambda stream, timeout: True,
            monotonic=lambda: 12.0,
        )
        check(label, source.wait(0.1).source == "degraded")
        source.close()


with tempfile.TemporaryDirectory() as raw_root:
    read_fd, write_fd = os.pipe()
    process = FakeProcess([])
    process.stdout = os.fdopen(read_fd, "rb", buffering=0)
    source = CmuxWakeSource(
        binding(Path(raw_root)),
        runner=capable,
        popen=Factory(process),
    )
    os.write(write_fd, b'{"partial":1')
    observations: list[WakeObservation | None] = []
    started = time.monotonic()
    waiter = threading.Thread(
        target=lambda: observations.append(source.wait(0.05)),
        daemon=True,
    )
    waiter.start()
    waiter.join(0.25)
    returned_in_budget = not waiter.is_alive()
    elapsed = time.monotonic() - started
    os.close(write_fd)
    waiter.join(0.5)
    check(
        "partial NDJSON cannot block past the event wait deadline",
        returned_in_budget
        and elapsed < 0.25
        and observations
        and observations[0] is not None
        and observations[0].source == "degraded",
    )
    source.close()


with tempfile.TemporaryDirectory() as raw_root:
    def launch_failure(argv: list[str], **kwargs: object) -> object:
        raise OSError("unavailable")

    source = CmuxWakeSource(
        binding(Path(raw_root)), runner=capable, popen=launch_failure
    )
    check("subscription launch failure is unavailable without attention", source.wait(0.1).source == "unavailable")
    source.close()


with tempfile.TemporaryDirectory() as raw_root:
    calls: list[list[str]] = []

    def unavailable(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "missing")

    source = CmuxWakeSource(binding(Path(raw_root)), runner=unavailable)
    check("missing optional capability returns unavailable", source.wait(0.1).source == "unavailable")
    check("capability probe is fixed and code-only", calls == [["cmux", "events", "--help"]])
    source.close()


with tempfile.TemporaryDirectory() as raw_root:
    capability_calls: list[list[str]] = []
    launches: list[FakeProcess] = []

    def cached_capability(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        capability_calls.append(argv)
        return capable(argv, **kwargs)

    def persistently_invalid(
        _argv: list[str], **_kwargs: object
    ) -> FakeProcess:
        process = FakeProcess([{**ack(), "protocol": "invalid"}])
        launches.append(process)
        return process

    source = CmuxWakeSource(
        binding(Path(raw_root)),
        runner=cached_capability,
        popen=persistently_invalid,
        wait_readable=lambda stream, timeout: True,
        monotonic=lambda: 13.0,
    )
    check(
        "invalid acknowledgement degrades the first subscription",
        source.wait(0.1).source == "degraded",
    )
    source.retry()
    check(
        "degraded retries cache capability and start only one new subscription",
        len(capability_calls) == 1 and len(launches) == 2,
    )
    source.close()


with tempfile.TemporaryDirectory() as raw_root:
    root = Path(raw_root)
    os.symlink(root, root / "linked")
    source = CmuxWakeSource(binding(root / "linked"), runner=capable)
    check("symlinked cursor root is unavailable without launch", source.wait(0.1).source == "unavailable")
    source.close()


print("cmux wake source contract matrix: ok")
