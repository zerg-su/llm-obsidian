#!/usr/bin/env python3
"""Provider-free repetition gate for historically fragile transition seams."""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.liveness import (  # noqa: E402
    LivenessEvidence,
    LivenessPolicy,
    LivenessState,
    observe_liveness,
)
from harness.retained_notification import deliver_worker_notification  # noqa: E402
from harness.runtime_session_continuation import deliver_continuation  # noqa: E402


REPETITIONS = 50
SURFACE = "11111111-1111-1111-1111-111111111111"
WORKSPACE = "22222222-2222-2222-2222-222222222222"
NOTIFICATION_CORRIDORS = (
    "dispatch",
    "custom-step",
    "engineering-fix",
    "verification-retry",
    "review-callback",
)
RECONCILIATION_CORRIDORS = ("built-in-summary", "summary-refresh")


class Port:
    def __init__(self, screens: list[str]) -> None:
        self.screens = list(screens)
        self.sent: list[str] = []
        self.keys: list[str] = []

    def read(self, surface_id: str) -> str:
        assert surface_id == SURFACE
        return self.screens.pop(0) if self.screens else "› old"

    def send(self, surface_id: str, text: str) -> None:
        assert surface_id == SURFACE
        self.sent.append(text)

    def send_key(self, surface_id: str, key: str) -> None:
        assert surface_id == SURFACE and key == "Enter"
        self.keys.append(key)

    def agent_status(self, workspace_id: str, runtime: str) -> str:
        assert workspace_id == WORKSPACE and runtime in {"codex", "claude"}
        return "idle"


class Worker:
    def __init__(self, port: Port, runtime: str) -> None:
        self.cmux_adapter = port
        self.spec = {"surface_id": SURFACE, "runtime": runtime}

    def _workspace_id(self) -> str:
        return WORKSPACE

    @staticmethod
    def write_immutable_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def notification_race(root: Path, corridor: str, repetition: int) -> None:
    runtime = "codex" if repetition % 2 == 0 else "claude"
    message = f"# {corridor} transition\nComplete the exact registered step."
    anchor = message.splitlines()[0]
    prompt_screen = f"› {anchor}" if runtime == "codex" else f"❯ {anchor}"
    ordinary_port = Port(["› old", prompt_screen])
    ordinary = root / corridor / "ordinary" / f"{repetition:03d}.json"
    deliver_worker_notification(
        Worker(ordinary_port, runtime),
        notify_path=ordinary,
        marker={
            "schema_version": 1,
            "operation_id": f"{corridor}-ordinary-{repetition}",
            "status": "sent",
        },
        message=message,
        successor_ready=lambda: False,
    )
    assert ordinary_port.sent == [message] and ordinary_port.keys == ["Enter"]
    assert json.loads(
        ordinary.with_name(f"{ordinary.stem}-delivery.json").read_text(
            encoding="utf-8"
        )
    )["stage"] == "submit-accepted"

    port = Port(["› old", prompt_screen])
    readiness = iter((False, True))
    notify = root / corridor / f"{repetition:03d}.json"
    deliver_worker_notification(
        Worker(port, runtime),
        notify_path=notify,
        marker={
            "schema_version": 1,
            "operation_id": f"{corridor}-{repetition}",
            "status": "sent",
        },
        message=message,
        successor_ready=lambda: next(readiness, True),
    )
    receipt = json.loads(
        notify.with_name(f"{notify.stem}-delivery.json").read_text(encoding="utf-8")
    )
    assert port.sent == [message]
    assert port.keys == []
    assert receipt["stage"] == "superseded" and receipt["submit_count"] == 0
    deliver_worker_notification(
        Worker(port, runtime),
        notify_path=notify,
        marker={
            "schema_version": 1,
            "operation_id": f"{corridor}-{repetition}",
            "status": "sent",
        },
        message=message,
        successor_ready=lambda: True,
    )
    assert port.sent == [message] and port.keys == []


def reconciliation_race(corridor: str, repetition: int) -> None:
    digest = f"{repetition + 1:064x}"[-64:]
    base = LivenessEvidence(
        observed_at=1000,
        process_status="alive",
        operation_revision=repetition + 1,
        operation_state="awaiting-callback",
        screen_sha256="a" * 64,
        prompt_state="non-interactive",
        typed_result_sha256=digest,
        agent_status="idle",
    )
    state = LivenessState.start(base)
    decision, reconciled = observe_liveness(
        state, replace(base, observed_at=1060), LivenessPolicy.default()
    )
    assert decision.action == "reconcile-result" and not decision.model_call
    decision, recovered = observe_liveness(
        reconciled, replace(base, observed_at=1901), LivenessPolicy.default()
    )
    assert decision.action == "nudge" and recovered.nudge_count == 1, corridor


def reap_without_provider_effect(repetition: int) -> None:
    port = Port([])
    result = deliver_continuation(
        port,
        surface_id=SURFACE,
        prompt=f"# reap {repetition}",
        runtime="codex",
        artifact_ready=lambda: True,
        ownership_ready=lambda: True,
        reserve_retry=lambda: False,
        observe_stage=lambda *_args: None,
        wait=lambda _seconds: None,
    )
    assert result.acknowledged and result.evidence == "artifact"
    assert port.sent == [] and port.keys == []


with tempfile.TemporaryDirectory(prefix="transition-transport-stress.") as raw:
    root = Path(raw)
    for corridor in NOTIFICATION_CORRIDORS:
        for repetition in range(REPETITIONS):
            notification_race(root, corridor, repetition)
    for corridor in RECONCILIATION_CORRIDORS:
        for repetition in range(REPETITIONS):
            reconciliation_race(corridor, repetition)
    for repetition in range(REPETITIONS):
        reap_without_provider_effect(repetition)

print(
    "Transition transport stress passed: "
    f"{len(NOTIFICATION_CORRIDORS) + len(RECONCILIATION_CORRIDORS) + 1} "
    f"corridors x {REPETITIONS} repetitions"
)
