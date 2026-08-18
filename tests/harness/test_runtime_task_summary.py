#!/usr/bin/env python3
"""Hermetic task-summary callback and coordinator wake regressions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Callable


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.adapters.process import ProcessAdapter
from harness.callbacks import CallbackBroker
from harness.contracts import (
    DEFAULT_TIME_BUDGET_SECONDS,
    DEFAULT_TOKEN_LIMIT,
    AttentionReason,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.pipeline_builtins import (
    builtin_definitions,
    builtin_registry,
    compiled_builtin,
)
from harness.pipelines import compile_pipeline
from harness.runtime_sessions import RuntimeSessionManager, RuntimeSessionRequest
from harness.runtime_worker_contracts import RuntimeWorkerError
from harness.cmux_wake_source import WakeObservation
from harness.runtime_worker_execution import RuntimeWorkerExecution
from harness.runtime_worker import (
    _pipeline_verify_identity,
    _review_resolution_handoff_ready,
    run as run_worker,
)
from harness.runtime_worker_review_bridge import (
    RuntimeWorkerReviewBridgeMixin,
    _review_drive_failure_receipt,
)
from harness.runtime_worker_custom import RuntimeWorkerCustomMixin
from harness.review_continuation_recovery import (
    RecoveryReason,
    RecoverySnapshot,
    classify_review_continuation,
)
from harness.runtime_callback_io import record_review_drive_failure
from harness.store import OperationStore
from harness.state_machine import TERMINAL
from harness.supervisor import OperationSupervisor
from harness.verification import load_profiles
from approved_plan_snapshot import bind_approved_plan_snapshot
from harness.artifact_repair import (  # noqa: E402
    build_verification_gap_escalation,
    build_verification_escalation,
    publish_pipeline_step_contract,
    resolve_verification_escalation,
)
from harness.workflows.engineering_fix import (  # noqa: E402
    fix_phase_request,
    prepare_next_phase,
)
from harness.verification_attempt import (  # noqa: E402
    VerificationAttempt,
    verification_input_sha256,
)

import importlib.util  # noqa: E402

_resubmit_spec = importlib.util.spec_from_file_location(
    "pipeline_verification_resubmit",
    ROOT / "scripts" / "pipeline-verification-resubmit.py",
)
assert _resubmit_spec and _resubmit_spec.loader
verification_resubmit = importlib.util.module_from_spec(_resubmit_spec)
_resubmit_spec.loader.exec_module(verification_resubmit)


class FallbackWakeSource:
    """Hermetic pacing that never fabricates a cmux event receipt."""

    def start(self) -> bool:
        return True

    def wait(self, timeout: float) -> WakeObservation:
        time.sleep(min(max(0.0, timeout), 0.02))
        return WakeObservation("fallback-poll", observed_at=time.monotonic())

    def retry(self) -> bool:
        return True

    def refresh_generation(self, _generation: int) -> None:
        return None

    def close(self) -> None:
        return None


class FakeMonotonicClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class EventlessWakeSource:
    """Advance a fake clock through the requested wait without an event."""

    def __init__(self, clock: FakeMonotonicClock) -> None:
        self.clock = clock
        self.waits: list[float] = []

    def start(self) -> bool:
        return True

    def wait(self, timeout: float) -> None:
        self.waits.append(timeout)
        self.clock.advance(timeout)
        time.sleep(0.002)
        return None

    def retry(self) -> bool:
        return True

    def refresh_generation(self, _generation: int) -> None:
        return None

    def close(self) -> None:
        return None


from harness.workflows.reap import run_reap
from harness.workflows.review import (
    ReviewContext,
    ReviewOperationRequest,
    ReviewResult,
)
from harness.workflows.review_gate import ReviewGateController, ReviewPreset
from outcome_contract import extract_from_bytes
from review_resolution import review_transport_identity_sha256
from task_escalation_records import (
    append_raise,
    append_resolution,
    load_attention,
)
from task_review_flow import TaskReviewError, _admitted_review_launch


ORIGIN = "11111111-1111-1111-1111-111111111111"
CHILD = "22222222-2222-2222-2222-222222222222"
PROJECT = "33333333-3333-4333-8333-333333333333"
TASK = "44444444-4444-4444-8444-444444444444"
INVALID_TASK = "55555555-5555-4555-8555-555555555555"
BLOCKED_TASK = "66666666-6666-4666-8666-666666666666"

ATOMIC_SUMMARY_PUBLISHER = (
    "import os,pathlib,tempfile,time\n"
    "def publish_summary(path,text,barrier=''):\n"
    "  descriptor,raw=tempfile.mkstemp(prefix=f'.{path.name}.',dir=path.parent)\n"
    "  temporary=pathlib.Path(raw)\n"
    "  try:\n"
    "    with os.fdopen(descriptor,'w',encoding='utf-8') as handle:\n"
    "      if barrier:\n"
    "        handle.write(text[:1]); handle.flush(); os.fsync(handle.fileno())\n"
    "        marker=pathlib.Path(barrier)\n"
    "        marker.with_suffix('.ready').write_text(temporary.name+'\\n',encoding='utf-8')\n"
    "        for _ in range(1000):\n"
    "          if marker.with_suffix('.release').is_file(): break\n"
    "          time.sleep(0.005)\n"
    "        else: raise SystemExit(6)\n"
    "        handle.seek(0); handle.truncate()\n"
    "      handle.write(text); handle.flush(); os.fsync(handle.fileno())\n"
    "    os.replace(temporary,path)\n"
    "  finally:\n"
    "    temporary.unlink(missing_ok=True)\n"
)


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def assert_review_drive_failure_receipt_is_content_free() -> None:
    raw_error = (
        "task-review-runner: runtime preflight failed: capability-mismatch "
        "for /private/sensitive/review-prompt.md"
    )
    receipt = _review_drive_failure_receipt(
        subprocess.CompletedProcess(
            ("task-review-runner.py", "run"),
            3,
            stdout="",
            stderr=raw_error,
        ),
        drive_sha256="d" * 64,
    )
    encoded = json.dumps(receipt, sort_keys=True)
    check(
        "failed automatic review keeps only a typed content-free reason",
        receipt["reason_code"] == "runtime-preflight-failed"
        and receipt["returncode"] == 3
        and receipt["drive_sha256"] == "d" * 64
        and receipt["stdout_sha256"] == hashlib.sha256(b"").hexdigest()
        and receipt["stderr_sha256"]
        == hashlib.sha256(raw_error.encode()).hexdigest()
        and raw_error not in encoded
        and "/private/" not in encoded,
        receipt,
    )
    generic = _review_drive_failure_receipt(
        subprocess.CompletedProcess(
            ("task-review-runner.py", "run"),
            2,
            stdout="",
            stderr="task-review-runner: unexpected nonzero exit",
        ),
        drive_sha256="e" * 64,
    )
    check(
        "unrecognized runner failures keep the generic typed reason",
        generic["reason_code"] == "runner-exit-nonzero",
        generic,
    )


def assert_review_drive_failure_receipts_are_cycle_scoped(root: Path) -> None:
    """A later review cycle must not collide with an earlier failure receipt."""

    state = root / "review-drive-receipts"

    first = _review_drive_failure_receipt(
        subprocess.CompletedProcess(
            ("task-review-runner.py", "run"),
            3,
            stdout="",
            stderr="task-review-runner: review checkpoint cannot be resurrected",
        ),
        drive_sha256="1" * 64,
    )
    second = _review_drive_failure_receipt(
        subprocess.CompletedProcess(
            ("task-review-runner.py", "run"),
            3,
            stdout="",
            stderr="task-review-runner: review checkpoint cannot be resurrected",
        ),
        drive_sha256="2" * 64,
    )
    record_review_drive_failure(state, first)
    record_review_drive_failure(state, second)
    record_review_drive_failure(state, second)
    archive = state / "review-drive-failures"
    latest = json.loads(
        (state / "review-drive-failure.json").read_text(encoding="utf-8")
    )
    check(
        "distinct review cycles retain immutable failure receipts",
        latest == second
        and json.loads((archive / f"{'1' * 64}.json").read_text()) == first
        and json.loads((archive / f"{'2' * 64}.json").read_text()) == second,
        (latest, sorted(path.name for path in archive.iterdir())),
    )


class FakeCmux:
    def __init__(
        self,
        *,
        key_observer: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.sent: list[tuple[str, str]] = []
        self.keys: list[tuple[str, str]] = []
        self.key_observer = key_observer

    def send(self, surface_id: str, text: str) -> None:
        self.sent.append((surface_id, text))

    def send_key(self, surface_id: str, key: str) -> None:
        self.keys.append((surface_id, key))
        if self.key_observer is not None:
            message = next(
                (
                    text
                    for target, text in reversed(self.sent)
                    if target == surface_id
                ),
                "",
            )
            self.key_observer(surface_id, key, message)


@dataclass(frozen=True)
class FakeReviewSessionResult:
    record: object
    checkpoint: str


class TypedReviewRuntime:
    """Deterministic provider port; the real review gate owns all transitions."""

    def __init__(self, store: OperationStore, owner_id: str) -> None:
        self.store = store
        self.owner_id = owner_id

    def start(
        self,
        request: object,
        *,
        on_surface_opened=None,
        admit_provider_start=None,
    ) -> FakeReviewSessionResult:
        if admit_provider_start is not None:
            admit_provider_start()
        record = self.store.create(
            request.spec, lane_id=request.lane_id, run_id=request.run_id
        )
        record = replace(
            record,
            resources=OwnedResources(surface_id=CHILD),
            revision=record.revision + 1,
        )
        self.store.save(record, expected_revision=record.revision - 1)
        result = FakeReviewSessionResult(record, "checkpoint-typed-review")
        if on_surface_opened is not None:
            on_surface_opened(result)
        return result

    def status(self, owner_id: str, operation_id: str) -> FakeReviewSessionResult:
        return FakeReviewSessionResult(
            self.store.read(owner_id, operation_id),
            "checkpoint-typed-review",
        )

    def register_callback_target(self, *_args: object) -> None:
        return None

    def accept_callback(self, envelope: object) -> object:
        return CallbackBroker(self.store, self.owner_id).accept(envelope)

    def request_exit(self, owner_id: str, operation_id: str) -> object:
        record = self.store.read(owner_id, operation_id)
        if record.state in {"complete", "failed", "cancelled"}:
            return record
        if record.state in {
            "created",
            "preflight",
            "starting",
            "attention-required",
        }:
            self.store.transition(owner_id, operation_id, "cancelling")
        elif record.state != "finalizing":
            self.store.transition(owner_id, operation_id, "finalizing")
        self.store.transition(owner_id, operation_id, "exiting")
        return self.store.read(owner_id, operation_id)

    def cleanup(self, owner_id: str, operation_id: str) -> object:
        record = self.store.read(owner_id, operation_id)
        if record.state == "exiting":
            self.store.transition(owner_id, operation_id, "complete")
        completed = self.store.read(owner_id, operation_id)
        if completed.resources != OwnedResources():
            updated = replace(
                completed,
                resources=OwnedResources(),
                revision=completed.revision + 1,
            )
            self.store.save(updated, expected_revision=completed.revision)
            completed = updated
        return completed


class TerminalCmuxPort:
    """Exact owned surface port for the production cleanup state machine."""

    def __init__(self, surface_id: str) -> None:
        self.surface_id = surface_id
        self.state = "alive"

    def status(self, surface_id: str) -> str:
        if surface_id != self.surface_id:
            raise AssertionError("cleanup probed another surface")
        return self.state

    def close_exact(self, surface_id: str) -> None:
        if surface_id != self.surface_id:
            raise AssertionError("cleanup closed another surface")
        self.state = "missing"


class TerminalProcessPort:
    """Exact owned process port that records one guardian exit effect."""

    def __init__(self, resources: OwnedResources) -> None:
        self.resources = resources
        self.process_state = "alive"
        self.supervisor_state = "alive"
        self.effects: list[str] = []

    def process_status(self, process_group: int, identity: str) -> str:
        if (
            process_group != self.resources.process_group
            or identity != self.resources.process_identity
        ):
            raise AssertionError("cleanup probed another process")
        return self.process_state

    def pid_status(self, pid: int, identity: str) -> str:
        if (
            pid != self.resources.supervisor_pid
            or identity != self.resources.supervisor_identity
        ):
            raise AssertionError("cleanup probed another supervisor")
        return self.supervisor_state

    def capture_identity(self, pid: int, *, process_group: int = 0) -> str:
        if pid == self.resources.process_group and process_group == pid:
            return self.resources.process_identity
        if pid == self.resources.supervisor_pid and process_group == 0:
            return self.resources.supervisor_identity
        return ""

    def request_guardian_signal(self, _control_path: Path, **kwargs: object) -> None:
        if (
            kwargs.get("action") != "request-exit"
            or kwargs.get("process_group") != self.resources.process_group
            or kwargs.get("process_identity")
            != self.resources.process_identity
            or kwargs.get("supervisor_pid") != self.resources.supervisor_pid
            or kwargs.get("supervisor_identity")
            != self.resources.supervisor_identity
        ):
            raise AssertionError("cleanup exit identity changed")
        self.effects.append("request-exit")
        self.process_state = "dead"
        self.supervisor_state = "dead"


def assert_summary_refresh_notification_replays_without_effect(root: Path) -> None:
    """Cover the durable replay branch without relying on worker-thread timing."""

    operation_id = "91919191-9191-4191-8191-919191919191"
    state = root / "summary-refresh-replay"
    state.mkdir()
    digest = "b" * 64
    approved_head = "c" * 40
    write_json(
        state / "pipeline-review-resolution-notify.json",
        {
            "schema_version": 1,
            "operation_id": operation_id,
            "reviewed_head_sha": "a" * 40,
            "summary_sha256": digest,
            "status": "sent",
        },
    )
    write_json(
        state / "pipeline-summary-refresh-notify.json",
        {
            "schema_version": 1,
            "operation_id": operation_id,
            "approved_head_sha": approved_head,
            "summary_sha256": digest,
            "status": "sent",
        },
    )
    cmux = FakeCmux()
    worker = SimpleNamespace(
        spec_path=state / "runtime.json",
        spec={"operation_id": operation_id, "surface_id": CHILD},
        digest=digest,
        cmux_adapter=cmux,
    )
    replayed = RuntimeWorkerReviewBridgeMixin.wait_for_summary_refresh_after_resolution(
        worker,
        {"context": {"head_sha": approved_head}},
    )
    check(
        "durable summary refresh replay performs no duplicate cmux effect",
        replayed and cmux.sent == [] and cmux.keys == [],
    )


def assert_review_resolution_notification_crashes_fail_closed(root: Path) -> None:
    """A crash after either cmux effect cannot replay the resolution packet."""

    class PartialResolutionCmux:
        def __init__(self, fail_after: str) -> None:
            self.fail_after = fail_after
            self.events: list[tuple[str, str]] = []

        def send(self, surface_id: str, _message: str) -> None:
            self.events.append(("send", surface_id))
            if self.fail_after == "send":
                raise RuntimeError("crash after review resolution paste")

        def send_key(self, surface_id: str, _key: str) -> None:
            self.events.append(("key", surface_id))
            if self.fail_after == "key":
                raise RuntimeError("crash after review resolution Enter")

    for fail_after, expected in (
        ("send", [("send", CHILD)]),
        ("key", [("send", CHILD), ("key", CHILD)]),
    ):
        state = root / f"review-resolution-{fail_after}"
        state.mkdir()
        cmux = PartialResolutionCmux(fail_after)
        worker = SimpleNamespace(
            spec_path=state / "runtime.json",
            spec={"operation_id": TASK, "surface_id": CHILD},
            digest="d" * 64,
            cmux_adapter=cmux,
        )
        packet = {"reviewed_head_sha": "a" * 40}
        notify_path = state / "pipeline-review-resolution-notify.json"

        for attempt in range(2):
            notified = RuntimeWorkerReviewBridgeMixin.load_review_notification(
                worker, notify_path
            )
            try:
                RuntimeWorkerReviewBridgeMixin.send_review_resolution_notification(
                    worker,
                    packet=packet,
                    packet_path=Path(".task-review.json"),
                    resolution_path=Path(".task-review-resolution.json"),
                    notify_path=notify_path,
                    notified=notified,
                )
            except Exception as exc:
                assert "uncertain" in str(exc)
            else:
                raise AssertionError(
                    "ambiguous review resolution effect must fail closed"
                )
            assert cmux.events == expected, (fail_after, attempt, cmux.events)
        marker = json.loads(
            (state / "review-resolution-wake" / "callback-wake.json").read_text(
                encoding="utf-8"
            )
        )
        assert marker["status"] == "effect-uncertain", marker
    print("OK   review resolution cmux crash windows never replay effects")


def assert_malformed_review_resolution_self_heals_boundedly(root: Path) -> None:
    """A live executor gets two schema corrections, never a review replay."""

    worktree = root / "resolution-correction-product"
    worktree.mkdir()
    state = root / "resolution-correction-runtime"
    state.mkdir()
    packet = {
        "schema_version": 1,
        "operation_id": TASK,
        "review_operation_id": "review-cycle-1",
        "review_callbacks": [],
        "review_identity_sha256": "b" * 64,
        "reviewed_head_sha": "a" * 40,
        "allowed_dispositions": ["applied", "out-of-scope", "rejected"],
        "resolution_path": ".task-review-resolution.json",
        "material_finding_ids": ["F-1"],
        "findings": [],
    }
    findings = [{"finding_id": "F-1"}]
    resolution_path = worktree / ".task-review-resolution.json"
    cmux = FakeCmux()
    worker = SimpleNamespace(
        spec_path=state / "runtime.json",
        spec={"operation_id": TASK, "surface_id": CHILD, "cwd": worktree},
        cmux_adapter=cmux,
        resumed_wake_identities=set(),
    )
    malformed = {
        "schema_version": 1,
        "review_operation_id": "review-cycle-1",
        "reviewed_head_sha": "a" * 40,
        "resolved_head_sha": "c" * 40,
        "resolutions": [
            {
                "finding_id": "F-1",
                "disposition": "applied",
                "rationale": "fixed",
                "commit_sha": "c" * 40,
            }
        ],
        "resolution_summary": "fixed",
    }
    resolution_path.write_text("{\n", encoding="utf-8")
    first = RuntimeWorkerReviewBridgeMixin.ensure_review_resolution_template(
        worker, packet=packet, material_findings=findings
    )
    restored = json.loads(first.read_text(encoding="utf-8"))
    write_json(resolution_path, malformed | {"resolution_summary": "fixed twice"})
    second = RuntimeWorkerReviewBridgeMixin.ensure_review_resolution_template(
        worker, packet=packet, material_findings=findings
    )
    write_json(resolution_path, malformed | {"resolution_summary": "fixed thrice"})
    RuntimeWorkerReviewBridgeMixin.ensure_review_resolution_template(
        worker, packet=packet, material_findings=findings
    )
    write_json(resolution_path, malformed | {"resolution_summary": "fixed fourth"})
    try:
        RuntimeWorkerReviewBridgeMixin.ensure_review_resolution_template(
            worker, packet=packet, material_findings=findings
        )
    except RuntimeWorkerError as exc:
        exhausted = "correction budget exhausted" in str(exc)
        exhaustion_error = str(exc)
    else:
        exhausted = False
        exhaustion_error = ""
    correction_root = (
        state
        / "contract-corrections"
        / "review-resolution"
        / packet["review_identity_sha256"]
    )
    receipts = sorted(correction_root.glob("attempt-*/reservation.json"))
    check(
        "deterministic repair precedes two bounded review-resolution corrections",
        first == resolution_path
        and second == resolution_path
        and set(restored)
        == {
            "schema_version",
            "operation_id",
            "review_identity_sha256",
            "reviewed_head_sha",
            "resolved_head_sha",
            "resolutions",
        }
        and restored["operation_id"] == TASK
        and set(restored["resolutions"][0])
        == {"finding_id", "disposition", "rationale", "follow_up"}
        and len(cmux.sent) == 2
        and cmux.keys == [(CHILD, "Enter"), (CHILD, "Enter")]
        and all("exact template" in message for _, message in cmux.sent)
        and len(receipts) == 2
        and [json.loads(path.read_text())["attempt"] for path in receipts]
        == [1, 2]
        and exhausted,
        (restored, cmux.sent, receipts, exhausted, exhaustion_error),
    )


def assert_resolution_correction_crash_resumes_once(root: Path) -> None:
    """A torn correction wake resumes from its pending durable receipt."""

    class CrashAfterSend:
        def send(self, _surface_id: str, _message: str) -> None:
            raise RuntimeError("crash after correction paste")

        def send_key(self, _surface_id: str, _key: str) -> None:
            raise AssertionError("Enter cannot follow a failed paste")

    worktree = root / "resolution-correction-crash-product"
    worktree.mkdir()
    state = root / "resolution-correction-crash-runtime"
    state.mkdir()
    packet = {
        "schema_version": 1,
        "operation_id": TASK,
        "review_operation_id": "review-cycle-1",
        "review_callbacks": [],
        "review_identity_sha256": "b" * 64,
        "reviewed_head_sha": "a" * 40,
        "allowed_dispositions": ["applied", "out-of-scope", "rejected"],
        "resolution_path": ".task-review-resolution.json",
        "material_finding_ids": ["F-1"],
        "findings": [],
    }
    findings = [{"finding_id": "F-1"}]
    resolution_path = worktree / ".task-review-resolution.json"
    crashed = SimpleNamespace(
        spec_path=state / "runtime.json",
        spec={"operation_id": TASK, "surface_id": CHILD, "cwd": worktree},
        cmux_adapter=CrashAfterSend(),
        resumed_wake_identities=set(),
    )
    RuntimeWorkerReviewBridgeMixin.ensure_review_resolution_template(
        crashed, packet=packet, material_findings=findings
    )
    write_json(
        resolution_path,
        {
            "schema_version": 1,
            "operation_id": TASK,
            "review_identity_sha256": packet["review_identity_sha256"],
            "reviewed_head_sha": packet["reviewed_head_sha"],
            "resolved_head_sha": "c" * 40,
            "resolutions": [
                {
                    "finding_id": "F-1",
                    "disposition": "applied",
                    "rationale": "fixed",
                    "commit_sha": "c" * 40,
                }
            ],
        },
    )
    try:
        RuntimeWorkerReviewBridgeMixin.ensure_review_resolution_template(
            crashed, packet=packet, material_findings=findings
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("correction wake crash was hidden")
    attempt_root = (
        state
        / "contract-corrections"
        / "review-resolution"
        / packet["review_identity_sha256"]
        / "attempt-01"
    )
    pending = json.loads(
        (attempt_root / "notification-reserved.json").read_text(encoding="utf-8")
    )
    cmux = FakeCmux()
    restarted = SimpleNamespace(
        spec_path=state / "runtime.json",
        spec={"operation_id": TASK, "surface_id": CHILD, "cwd": worktree},
        cmux_adapter=cmux,
        resumed_wake_identities=set(),
    )
    try:
        RuntimeWorkerReviewBridgeMixin.ensure_review_resolution_template(
            restarted, packet=packet, material_findings=findings
        )
    except RuntimeWorkerError as exc:
        uncertain = "notification effect is uncertain" in str(exc)
    else:
        uncertain = False
    check(
        "torn review resolution correction stays uncertain without duplicate prompt",
        pending["status"] == "reserved"
        and uncertain
        and cmux.sent == []
        and cmux.keys == []
        and len(list(attempt_root.parent.glob("attempt-*"))) == 1,
        (pending, uncertain, cmux.sent, cmux.keys),
    )


def assert_resolved_changed_head_gates_review_on_exact_head_receipt(
    root: Path,
) -> None:
    """The live RC3 sequence: a resolved changed HEAD must be verified first.

    Durable shape from the live cell: review terminal changes-requested at the
    reviewed HEAD, the fix commit moved the product HEAD, and the automatic
    review drive launched the bounded iteration before any scoped verification
    receipt existed at that HEAD.  The drive must instead hand progress to the
    verification owner and launch only after a complete receipt names exactly
    the current HEAD.
    """

    operation_id = "92929292-9292-4292-8292-929292929292"
    state = root / "resolved-head-gate"
    state.mkdir()
    product = root / "resolved-head-product"
    product.mkdir()
    for argv in (
        ("init", "-b", "main"),
        ("config", "user.email", "corridor@example.invalid"),
        ("config", "user.name", "Corridor World"),
    ):
        subprocess.run(
            ["git", "-C", str(product), *argv], check=True, capture_output=True
        )

    def commit(name: str) -> str:
        (product / "product.txt").write_text(name + "\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(product), "add", "product.txt"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(product), "commit", "-m", name],
            check=True,
            capture_output=True,
        )
        return subprocess.run(
            ["git", "-C", str(product), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    reviewed_head = commit("reviewed")
    fix_head = commit("fix")
    write_json(
        state / "pipeline-review-resolution-notify.json",
        {
            "schema_version": 1,
            "operation_id": operation_id,
            "reviewed_head_sha": reviewed_head,
            "summary_sha256": "b" * 64,
            "status": "sent",
        },
    )
    calls: list[object] = []
    receipt_box: list[object] = [None]

    class GateWorker(RuntimeWorkerReviewBridgeMixin):
        def __init__(self) -> None:
            self.spec_path = state / "runtime.json"
            self.spec = {"operation_id": operation_id, "cwd": product}
            self.profile = object()
            self.verification_head = reviewed_head
            self.pipeline = SimpleNamespace(
                definition=SimpleNamespace(
                    steps=(SimpleNamespace(primitive_id="verify"),)
                )
            )

        def _bind_verification_attempt(self, index: int) -> None:
            calls.append(("bind", index))

        def adopt_invalidated_verification_successor(self) -> bool:
            calls.append(("adopt",))
            return True

        def verification_receipt(self) -> object:
            return receipt_box[0]

        def run_verification(self) -> None:
            calls.append(("verify",))

    worker = GateWorker()
    launched = worker.drive_review()
    check(
        "a resolved changed HEAD drives verification before any review launch",
        launched is False
        and ("verify",) in calls
        and ("bind", 0) in calls
        and worker.verification_head == fix_head,
        (launched, calls, worker.verification_head),
    )
    receipt_box[0] = {
        "status": "complete",
        "evidence": [{"head_sha": reviewed_head}],
    }
    stale_ready = worker._resolved_head_verification_ready()
    check(
        "a receipt for the reviewed HEAD cannot authorize the changed-HEAD review",
        stale_ready is False,
        stale_ready,
    )
    receipt_box[0] = {
        "status": "complete",
        "evidence": [{"head_sha": fix_head}],
    }
    ready = worker._resolved_head_verification_ready()
    check(
        "a complete exact-HEAD receipt at the fix HEAD releases the review drive",
        ready is True,
        ready,
    )
    (state / "pipeline-review-resolution-notify.json").unlink()
    bare = GateWorker()
    del bare.profile
    untouched = bare._resolved_head_verification_ready()
    check(
        "corridors without a changed-HEAD resolution stay untouched",
        untouched is True,
        untouched,
    )
    print("OK   resolved changed HEAD gates review on the exact-HEAD receipt")


def assert_rejected_drive_with_live_review_stays_waiting(root: Path) -> None:
    """The live RC3 false-attention latch: a started review is not a failure.

    Live ordering: verification complete, the runner partially launches the
    exact reviewer, rehydration emits the constant checkpoint error, and the
    current code latches the root attention-required although the bound
    review attempt exists, the reviewer's ready ownership is alive, and the
    review parent/round are awaiting their callback.  The drive must classify
    this durable review-in-progress and leave the root waiting; every
    absent/dead/mismatched/changed shape stays fail-closed.
    """

    task_id = "93939393-9393-4393-8393-939393939393"
    state = root / "live-review-latch"
    state.mkdir()
    vault = root / "live-review-vault"
    (vault / "scripts").mkdir(parents=True)
    runner = vault / "scripts" / "task-review-runner.py"
    runner.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        'sys.stderr.write("task-review-runner: review attempt checkpoint '
        'cannot be resurrected\\n")\n'
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    product = root / "live-review-product"
    product.mkdir()
    for argv in (
        ("init", "-b", "main"),
        ("config", "user.email", "review@example.invalid"),
        ("config", "user.name", "Review Latch Test"),
    ):
        subprocess.run(
            ["git", "-C", str(product), *argv], check=True, capture_output=True
        )
    (product / "product.txt").write_text("ready\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(product), "add", "product.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(product), "commit", "-m", "ready"],
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "-C", str(product), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    store = OperationStore(vault / ".vault-meta" / "harness")
    review_root = (
        vault / ".vault-meta" / "harness" / "review-data" / task_id / task_id
    )
    runtime = TypedReviewRuntime(store, "owner-1")
    gate = ReviewGateController(review_root, runtime, store)
    scratch = review_root / "runtime-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    preset = ReviewPreset.from_flags(model="sol", effort="medium")
    policy = preset.request(
        f"{task_id}-review",
        purpose="implementation",
        selected_provider="openai",
    )
    run = gate.begin_attempt(
        dispatch_operation_id=task_id,
        finalization_lineage_id=task_id,
        cycle=1,
        plan_sha256="8" * 64,
        outcome_sha256="9" * 64,
        request=ReviewOperationRequest(
            policy,
            "owner-1",
            RuntimeRoute(
                "codex", "gpt-5.6-sol", "medium", "reviewer-callback", "4" * 64
            ),
            ReviewContext(
                "packets/task/manifest.json", head, "scoped", "5" * 64
            ),
        ),
        origin_surface=ORIGIN,
        cwd=scratch,
        product_root=product,
        prompt_pointer=".task-prompt.md",
        callback_root="callbacks/review",
    )
    lane = run.execution.lanes[0]
    for step in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition("owner-1", lane.operation_id, step)
    reviewer = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )
    try:
        identity = ProcessAdapter.capture_identity(
            reviewer.pid, process_group=reviewer.pid
        )
        supervisor_identity = ProcessAdapter.capture_identity(os.getpid())
        parent = store.read("owner-1", lane.operation_id)
        bound = replace(
            parent,
            resources=OwnedResources(
                surface_id=CHILD,
                process_group=reviewer.pid,
                supervisor_pid=os.getpid(),
                process_identity=identity,
                supervisor_identity=supervisor_identity,
            ),
            revision=parent.revision + 1,
        )
        store.save(bound, expected_revision=parent.revision)
        write_json(
            vault
            / ".vault-meta"
            / "harness"
            / "owners"
            / "owner-1"
            / "runtime"
            / lane.operation_id
            / "ready.json",
            {
                "schema_version": 1,
                "status": "ready",
                "pid": reviewer.pid,
                "process_group": reviewer.pid,
                "supervisor_pid": os.getpid(),
                "process_identity": identity,
                "supervisor_identity": supervisor_identity,
            },
        )
        dispatch_spec = OperationSpec(
            task_id,
            f"key-{task_id}",
            "dispatch",
            "owner-1",
            RuntimeRoute("claude", "sonnet", "medium", "executor", "6" * 64),
            "packets/task.json",
            "scoped",
        )
        store.create(dispatch_spec, lane_id="latch-lane", run_id="latch-run")
        for step in ("preflight", "starting", "running", "awaiting-callback"):
            store.transition("owner-1", task_id, step)
        attention: list[tuple[str, object]] = []

        class LatchWorker(RuntimeWorkerReviewBridgeMixin):
            def __init__(self) -> None:
                self.spec_path = state / "runtime.json"
                self.spec = {
                    "operation_id": task_id,
                    "owner_id": "owner-1",
                    "cwd": product,
                    "surface_id": CHILD,
                }
                self.trusted_vault = vault
                self.store = store
                self.process = ProcessAdapter()
                self.review = SimpleNamespace(gate_root=review_root)
                self.review_launcher = None
                self.pipeline = SimpleNamespace(definition_sha256="7" * 64)
                self.marker_path = state / "pipeline-review-start.json"

            def write_immutable_json(self, path, value) -> None:
                if not Path(path).exists():
                    write_json(Path(path), value)

            def summary_attention(self, status, reason=None, **_kw) -> None:
                attention.append((status, reason))

        worker = LatchWorker()
        launched = worker.drive_review()
        marker = json.loads(worker.marker_path.read_text(encoding="utf-8"))
        record = store.read("owner-1", task_id)
        check(
            "a rejected drive over a live durable review stays waiting",
            launched is True
            and attention == []
            and marker["status"] == "started"
            and record.state == "awaiting-callback",
            (launched, attention, marker, record.state),
        )
        bound_rounds = [
            row
            for row in store.list("owner-1")
            if row.spec.kind == "review-round"
            and row.spec.parent_operation_id == lane.operation_id
        ]
        round_id = bound_rounds[0].spec.operation_id
        store.transition("owner-1", round_id, "verifying")
        attention.clear()
        worker.marker_path.unlink(missing_ok=True)
        verifying = worker.drive_review()
        verifying_record = store.read("owner-1", task_id)
        check(
            "a rejected drive while the bound round verifies stays waiting",
            verifying is True
            and attention == []
            and verifying_record.state == "awaiting-callback",
            (verifying, attention, verifying_record.state),
        )
        round_record = store.read("owner-1", round_id)
        store.save(
            replace(round_record, state="failed", revision=round_record.revision + 1),
            expected_revision=round_record.revision,
        )
        attention.clear()
        worker.marker_path.unlink(missing_ok=True)
        terminal_round = worker.drive_review()
        check(
            "a terminal bound round stays fail-closed",
            terminal_round is False and attention != [],
            (terminal_round, attention),
        )
        failed_record = store.read("owner-1", round_id)
        store.save(
            replace(
                failed_record,
                state="awaiting-callback",
                revision=failed_record.revision + 1,
            ),
            expected_revision=failed_record.revision,
        )
        decision = gate.complete_round(
            run,
            lane,
            run.rounds[lane.axis],
            ReviewResult(lane.axis, "approve"),
        )
        check(
            "the live review completes without any coordinator resume",
            decision.action == "approved",
            decision.action,
        )

        rejections: list[tuple[str, Callable[[], None], Callable[[], None]]] = []
        ready_path = (
            vault
            / ".vault-meta"
            / "harness"
            / "owners"
            / "owner-1"
            / "runtime"
            / lane.operation_id
            / "ready.json"
        )
        original_ready = ready_path.read_text(encoding="utf-8")

        def tamper_ready() -> None:
            value = json.loads(original_ready)
            value["status"] = "failed"
            write_json(ready_path, value)

        def restore_ready() -> None:
            ready_path.write_text(original_ready, encoding="utf-8")

        def tamper_head() -> None:
            (product / "product.txt").write_text("drift\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(product), "commit", "-am", "drift"],
                check=True,
                capture_output=True,
            )

        def restore_head() -> None:
            subprocess.run(
                ["git", "-C", str(product), "reset", "--hard", head],
                check=True,
                capture_output=True,
            )

        def tamper_identity() -> None:
            value = json.loads(original_ready)
            value["process_identity"] = "0" * 64
            write_json(ready_path, value)

        rejections = [
            ("a failed reviewer handshake stays fail-closed", tamper_ready, restore_ready),
            ("a changed HEAD stays fail-closed", tamper_head, restore_head),
            ("a mismatched reviewer identity stays fail-closed", tamper_identity, restore_ready),
        ]
        for label, tamper, restore in rejections:
            tamper()
            try:
                attention.clear()
                worker.marker_path.unlink(missing_ok=True)
                latched = worker.drive_review()
                check(
                    label,
                    latched is False and attention != [],
                    (label, latched, attention),
                )
            finally:
                restore()

        reviewer.terminate()
        reviewer.wait(timeout=10)
        attention.clear()
        worker.marker_path.unlink(missing_ok=True)
        dead = worker.drive_review()
        check(
            "a dead reviewer process stays fail-closed",
            dead is False and attention != [],
            (dead, attention),
        )
    finally:
        if reviewer.poll() is None:
            reviewer.kill()
    print("OK   rejected drive over a live durable review stays waiting")


def assert_durable_review_packet_generation_can_advance(root: Path) -> None:
    """A durably notified review packet may advance to one later cycle."""

    packet_path = root / "review-packet-generation.json"
    prior = {
        "schema_version": 1,
        "operation_id": TASK,
        "review_operation_id": "review-cycle-1",
        "reviewed_head_sha": "a" * 40,
        "review_callbacks": [{"callback_id": "callback-cycle-1"}],
    }
    packet_path.write_text(
        json.dumps(prior, sort_keys=True) + "\n", encoding="utf-8"
    )
    prior_sha256 = canonical_sha256(prior)
    notified = {
        "schema_version": 1,
        "operation_id": TASK,
        "packet_sha256": prior_sha256,
        "reviewed_head_sha": prior["reviewed_head_sha"],
        "status": "sent",
    }
    next_packet = {
        "schema_version": 1,
        "operation_id": TASK,
        "review_operation_id": "review-cycle-2",
        "reviewed_head_sha": "b" * 40,
        "review_callbacks": [{"callback_id": "callback-cycle-2"}],
    }
    worker = SimpleNamespace(spec={"operation_id": TASK})

    RuntimeWorkerReviewBridgeMixin.validate_existing_review_packet(
        worker, packet_path, notified, next_packet
    )

    for invalid_notification in (
        None,
        {**notified, "packet_sha256": "c" * 64},
    ):
        try:
            RuntimeWorkerReviewBridgeMixin.validate_existing_review_packet(
                worker, packet_path, invalid_notification, next_packet
            )
        except Exception as exc:
            assert "identity changed" in str(exc), exc
        else:
            raise AssertionError(
                "an unproven prior review packet must block generation advance"
            )
    print("OK   durable review packet generation advances exactly once")


def assert_resolution_head_drift_wakes_once(root: Path) -> None:
    """A coordinator repair after resolution gets one exact rebind wake."""

    worktree = root / "resolution-head-drift-product"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=worktree,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=worktree, check=True
    )
    (worktree / "product.txt").write_text("resolved\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.txt"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "resolved"], cwd=worktree, check=True
    )
    resolved_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    (worktree / "repair.txt").write_text("repair\n", encoding="utf-8")
    subprocess.run(["git", "add", "repair.txt"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "coordinator repair"],
        cwd=worktree,
        check=True,
    )
    current_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    resolution_path = worktree / ".task-review-resolution.json"
    write_json(resolution_path, {"resolved_head_sha": resolved_head})
    state = root / "resolution-head-drift-runtime"
    state.mkdir()
    packet = {"reviewed_head_sha": "a" * 40}
    packet_sha256 = canonical_sha256(packet)
    notify_path = state / "pipeline-review-resolution-notify.json"
    notified = {
        "schema_version": 1,
        "operation_id": TASK,
        "packet_sha256": packet_sha256,
        "reviewed_head_sha": "a" * 40,
        "status": "sent",
    }
    write_json(notify_path, notified)
    cmux = FakeCmux()
    worker = SimpleNamespace(
        spec_path=state / "runtime.json",
        spec={"operation_id": TASK, "surface_id": CHILD, "cwd": worktree},
        digest="d" * 64,
        cmux_adapter=cmux,
    )
    for _ in range(2):
        RuntimeWorkerReviewBridgeMixin.send_review_resolution_notification(
            worker,
            packet=packet,
            packet_path=worktree / ".task-review.json",
            resolution_path=resolution_path,
            notify_path=notify_path,
            notified=notified,
        )
    wake = json.loads(
        (state / "review-resolution-wake/callback-wake.json").read_text(
            encoding="utf-8"
        )
    )
    check(
        "resolution HEAD drift wakes the executor exactly once",
        current_head != resolved_head
        and len(cmux.sent) == 1
        and cmux.keys == [(CHILD, "Enter")]
        and wake["status"] == "sent"
        and wake["callback_id"]
        == hashlib.sha256(f"{packet_sha256}:{current_head}".encode()).hexdigest(),
    )


def assert_invalidated_verification_hands_off_to_exact_head_replacement(
    root: Path,
) -> None:
    """Preserved 2.7.2 ordering: green probes at HEAD A, amend to HEAD B before
    callback acceptance, interrupted own-identity effect on the HEAD-B attempt,
    settlement/terminalization — the verification owner must invalidate the
    stale attempt and create exactly one predecessor-bound exact-B successor."""

    from harness.contracts import EffectOutcome
    from harness.verification_attempt import pipeline_verify_effect_id

    profile = load_profiles(ROOT / "config" / "verification-profiles.toml")[
        "scoped"
    ]
    pipeline = compile_pipeline(
        builtin_definitions()["engineering/change"],
        builtin_registry(),
        capabilities=("route:resolved",),
    )
    summary = {
        "schema_version": 1,
        "type": "session",
        "title": "Runtime Result",
        "session": "executor-session",
        "body": "Bounded completed task.",
    }
    seeded: dict[str, dict[str, object]] = {}
    probe_commands: dict[str, list[tuple[str, ...]]] = {}

    def make_probe_runner(task: str):
        recorded = probe_commands.setdefault(task, [])

        def runner(argv: list[str], **kwargs: object):
            if argv == ["git", "rev-parse", "HEAD"]:
                return subprocess.run(
                    argv,
                    cwd=kwargs["cwd"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
            recorded.append(tuple(argv))
            return subprocess.CompletedProcess(argv, 0, "ok\n", "")

        return runner

    def approve_at_current_head(task: str):
        def launcher(vault: Path, worktree: Path) -> None:
            meta = json.loads(
                (worktree / ".task-meta.json").read_text(encoding="utf-8")
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            ReviewGateController.skip(
                vault / ".vault-meta" / "harness" / "review-data" / task / task,
                dispatch_operation_id=task,
                owner_id=task,
                preset=ReviewPreset.from_flags(no_review=True),
                context=ReviewContext(
                    "packets/task/manifest.json",
                    head,
                    "scoped",
                    meta["review_policy"]["verification_profile_sha256"],
                ),
                product_root=worktree,
            )

        return launcher

    def build_child(
        store: OperationStore,
        parent: object,
        input_sha256: str,
        attempt_index: int,
    ) -> tuple[object, str, str, str]:
        spec, lane_id, run_id = _pipeline_verify_identity(
            parent.spec,
            definition_sha256=pipeline.definition_sha256,
            input_sha256=input_sha256,
            profile="scoped",
            attempt_index=attempt_index,
        )
        store.create(spec, lane_id=lane_id, run_id=run_id)
        supervisor = OperationSupervisor(store, "owner-1", spec.operation_id)
        supervisor.configure_budget(
            attempt_limit=1,
            model_restart_limit=0,
            time_budget_seconds=DEFAULT_TIME_BUDGET_SECONDS,
            token_limit=DEFAULT_TOKEN_LIMIT,
        )
        for child_state in ("preflight", "starting", "running", "verifying"):
            supervisor.transition(child_state)
        supervisor.consume_attempt()
        effect_id = pipeline_verify_effect_id(input_sha256, attempt_index)
        store.begin_effect("owner-1", spec.operation_id, effect_id)
        return spec, lane_id, run_id, effect_id

    def seed_invalidated_state(
        task: str,
        *,
        stale_terminal: bool,
        seed_attempt1: str = "",
    ):
        def before_start(
            vault: Path, worktree: Path, state: Path, profile_sha: str
        ) -> None:
            store = OperationStore(vault / ".vault-meta" / "harness")
            parent = store.read("owner-1", task)
            head_a = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()

            # Green probes finish at HEAD A: complete settled child, linked
            # schema-v2 receipt, all scoped commands green.
            input_a = verification_input_sha256(
                pipeline.definition_sha256, head_a, profile_sha, 1
            )
            spec_a, lane_a, run_a, effect_a = build_child(
                store, parent, input_a, 0
            )
            attempt_a = VerificationAttempt(
                task, "scoped", profile_sha, head_a, 0
            )
            evidence_dir = (
                state / "pipeline-verification" / spec_a.operation_id / "evidence"
            )
            evidence_dir.mkdir(parents=True)
            evidence = []
            for index in range(3):
                output = evidence_dir / f"scoped-{index + 1}.log"
                output.write_text("ok\n", encoding="utf-8")
                evidence.append(
                    {
                        "schema_version": 2,
                        "profile": "scoped",
                        "profile_sha256": profile_sha,
                        "head_sha": head_a,
                        "command_id": f"scoped-{index + 1}",
                        "cwd": ".",
                        "exit_code": 0,
                        "started_at": "1",
                        "finished_at": "2",
                        "output_pointer": output.relative_to(state).as_posix(),
                        "output_sha256": hashlib.sha256(
                            output.read_bytes()
                        ).hexdigest(),
                        "output_bytes": len(output.read_bytes()),
                    }
                )
            receipt_a = {
                "schema_version": 2,
                "operation_id": spec_a.operation_id,
                "parent_operation_id": task,
                "lane_id": lane_a,
                "run_id": run_a,
                "definition_sha256": pipeline.definition_sha256,
                "step_id": "verify",
                "head_sha": head_a,
                "input_sha256": input_a,
                "profile": "scoped",
                "profile_sha256": profile_sha,
                "effect_id": effect_a,
                "status": "complete",
                "evidence": evidence,
                "verification_attempt": attempt_a.as_dict(),
                "verification_attempt_sha256": attempt_a.sha256,
            }
            write_json(
                state
                / "pipeline-verification"
                / spec_a.operation_id
                / "receipt.json",
                receipt_a,
            )
            write_json(state / "pipeline-step-verify.json", receipt_a)
            store.resolve_effect(
                "owner-1", spec_a.operation_id, EffectOutcome.SUCCEEDED
            )
            supervisor_a = OperationSupervisor(
                store, "owner-1", spec_a.operation_id
            )
            for child_state in ("finalizing", "exiting", "complete"):
                supervisor_a.transition(child_state)

            # The evidence/doc amend moves the clean product HEAD to B before
            # any callback acceptance.
            (worktree / "EVIDENCE.md").write_text(
                "evidence amend\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "EVIDENCE.md"], cwd=worktree, check=True
            )
            subprocess.run(
                ["git", "commit", "-m", "docs: bind evidence"],
                cwd=worktree,
                text=True,
                capture_output=True,
                check=True,
            )
            head_b = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()

            # The HEAD-B attempt-0 verifier dies mid own-identity effect (no
            # receipt), then its stale effect is settled; terminalization
            # varies by scenario.
            input_b = verification_input_sha256(
                pipeline.definition_sha256, head_b, profile_sha, 1
            )
            spec_b0, _lane_b0, _run_b0, _effect_b0 = build_child(
                store, parent, input_b, 0
            )
            partial = (
                state
                / "pipeline-verification"
                / spec_b0.operation_id
                / "evidence"
            )
            partial.mkdir(parents=True)
            (partial / "scoped-1.log").write_text(
                "interrupted\n", encoding="utf-8"
            )
            store.resolve_effect(
                "owner-1", spec_b0.operation_id, EffectOutcome.SUCCEEDED
            )
            if stale_terminal:
                store.transition(
                    "owner-1",
                    spec_b0.operation_id,
                    "attention-required",
                    reason=AttentionReason.ATTENTION_REQUIRED,
                )
                store.transition("owner-1", spec_b0.operation_id, "failed")

            spec_b1 = _pipeline_verify_identity(
                parent.spec,
                definition_sha256=pipeline.definition_sha256,
                input_sha256=input_b,
                profile="scoped",
                attempt_index=1,
            )[0]
            if seed_attempt1:
                spec_b1, _lane, _run, _effect = build_child(
                    store, parent, input_b, 1
                )
                partial_b1 = (
                    state
                    / "pipeline-verification"
                    / spec_b1.operation_id
                    / "evidence"
                )
                partial_b1.mkdir(parents=True)
                (partial_b1 / "scoped-1.log").write_text(
                    "interrupted successor\n", encoding="utf-8"
                )
                if seed_attempt1 == "terminal":
                    store.resolve_effect(
                        "owner-1", spec_b1.operation_id, EffectOutcome.SUCCEEDED
                    )
                    store.transition(
                        "owner-1",
                        spec_b1.operation_id,
                        "attention-required",
                        reason=AttentionReason.ATTENTION_REQUIRED,
                    )
                    store.transition(
                        "owner-1", spec_b1.operation_id, "failed"
                    )

            seeded[task] = {
                "head_a": head_a,
                "head_b": head_b,
                "input_b": input_b,
                "complete_a": spec_a.operation_id,
                "stale_b0": spec_b0.operation_id,
                "successor_b1": spec_b1.operation_id,
                "receipt_a": receipt_a,
            }

        return before_start

    def read_state_json(state: Path, *parts: str) -> object:
        path = state.joinpath(*parts)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def verify_children(store: OperationStore) -> dict[str, str]:
        return {
            record.spec.operation_id: record.state
            for record in store.list("owner-1")
            if record.spec.kind == "pipeline-verify"
        }

    scoped_commands = [
        ("make", "test-harness"),
        ("make", "test-model-routing"),
        ("git", "diff", "--check"),
    ]

    def assert_handoff_completes(label: str, task: str, *, stale_terminal: bool):
        store, _cmux, state, rc = run_case(
            root,
            task,
            summary,
            pipeline_name="engineering/change",
            before_start=seed_invalidated_state(
                task, stale_terminal=stale_terminal
            ),
            verification_runner=make_probe_runner(task),
            review_launcher=approve_at_current_head(task),
            review_state="missing",
            wake_source=FallbackWakeSource(),
        )
        facts = seeded[task]
        parent = store.read("owner-1", task)
        children = verify_children(store)
        successor = str(facts["successor_b1"])
        fresh_receipt = read_state_json(
            state, "pipeline-verification", successor, "receipt.json"
        )
        linked = read_state_json(state, "pipeline-step-verify.json")
        invalidation = read_state_json(
            state,
            "pipeline-verification",
            str(facts["stale_b0"]),
            "invalidation.json",
        )
        preserved_a = read_state_json(
            state,
            "pipeline-verification",
            str(facts["complete_a"]),
            "receipt.json",
        )
        check(
            f"{label}: one wake creates exactly one exact-HEAD successor "
            "and finishes through the ordinary summary path",
            rc == 0
            and parent.state == "finalizing"
            and bool(parent.accepted_callback_id)
            and children
            == {
                str(facts["complete_a"]): "complete",
                str(facts["stale_b0"]): "failed",
                successor: "complete",
            }
            and isinstance(fresh_receipt, dict)
            and fresh_receipt["status"] == "complete"
            and fresh_receipt["head_sha"] == facts["head_b"]
            and fresh_receipt["verification_attempt"]["attempt_index"] == 1
            and isinstance(linked, dict)
            and linked["operation_id"] == successor
            and probe_commands[task] == scoped_commands
            and preserved_a == facts["receipt_a"],
            (rc, parent, children, fresh_receipt, linked, probe_commands[task]),
        )
        predecessor_attempt = VerificationAttempt(
            task, "scoped", profile.sha256, str(facts["head_b"]), 0
        )
        successor_attempt = VerificationAttempt(
            task, "scoped", profile.sha256, str(facts["head_b"]), 1
        )
        check(
            f"{label}: the invalidation record binds the stale attempt to "
            "its exact successor",
            isinstance(invalidation, dict)
            and invalidation.get("schema_version") == 1
            and invalidation.get("operation_id") == facts["stale_b0"]
            and invalidation.get("parent_operation_id") == task
            and invalidation.get("profile_sha256") == profile.sha256
            and invalidation.get("predecessor_attempt_sha256")
            == predecessor_attempt.sha256
            and invalidation.get("predecessor_effect_id")
            == pipeline_verify_effect_id(str(facts["input_b"]), 0)
            and invalidation.get("successor_operation_id") == successor
            and invalidation.get("successor_attempt_sha256")
            == successor_attempt.sha256
            and invalidation.get("successor_effect_id")
            == pipeline_verify_effect_id(str(facts["input_b"]), 1)
            and invalidation.get("current_head_sha") == facts["head_b"]
            and invalidation.get("status") == "invalidated",
            invalidation,
        )
        return store, state

    # Exact preserved live ordering: settled and terminalized stale attempt.
    terminal_task = "27300000-2730-4273-8273-273000000001"
    terminal_store, terminal_state = assert_handoff_completes(
        "terminal invalidated attempt",
        terminal_task,
        stale_terminal=True,
    )

    # A repeated worker wake after the handoff creates no duplicate verifier
    # and no second verification effect.
    children_before = verify_children(terminal_store)
    probes_before = list(probe_commands[terminal_task])
    worktree = root / f"worktree-{terminal_task}"
    relaunch = ProcessAdapter().prepare_surface_launch(
        argv=(
            str(Path(sys.executable).resolve()),
            "-c",
            "import time; time.sleep(0.15)",
        ),
        cwd=worktree,
        state_root=root / f"state-{terminal_task}",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=worktree / ".task-summary.json",
        store_root=terminal_store.root,
        owner_id="owner-1",
        operation_id=terminal_task,
        run_id=f"run-{terminal_task}",
        surface_id=CHILD,
        runtime="codex",
        callback_mode="task-summary",
        task_summary_pointer=worktree / ".task-summary.json",
        origin_surface=ORIGIN,
    )
    rewake_rc = run_worker(
        relaunch.spec_path,
        poll_seconds=0.02,
        checkpoint_probe=lambda _surface, _runtime: "checkpoint-1",
        cmux_adapter=FakeCmux(),
        verification_runner=make_probe_runner(terminal_task),
        wake_source=FallbackWakeSource(),
    )
    check(
        "repeated wake after the handoff stays idempotent: no duplicate "
        "verifier and no second effect",
        rewake_rc == 0
        and verify_children(terminal_store) == children_before
        and probe_commands[terminal_task] == probes_before,
        (rewake_rc, verify_children(terminal_store), probe_commands[terminal_task]),
    )

    # Settlement without terminalization is the same invalidated class: the
    # owner itself must terminalize the stale attempt before the handoff.
    assert_handoff_completes(
        "settled non-terminal invalidated attempt",
        "27300000-2730-4273-8273-273000000002",
        stale_terminal=False,
    )

    # Crash re-entry: the successor already exists mid own-identity effect;
    # restart completes the same predecessor-bound handoff with no third
    # attempt.
    resume_task = "27300000-2730-4273-8273-273000000003"
    resume_store, _resume_cmux, resume_state, resume_rc = run_case(
        root,
        resume_task,
        summary,
        pipeline_name="engineering/change",
        before_start=seed_invalidated_state(
            resume_task, stale_terminal=True, seed_attempt1="pending"
        ),
        verification_runner=make_probe_runner(resume_task),
        review_launcher=approve_at_current_head(resume_task),
        review_state="missing",
        wake_source=FallbackWakeSource(),
    )
    resume_facts = seeded[resume_task]
    resume_parent = resume_store.read("owner-1", resume_task)
    resume_children = verify_children(resume_store)
    resume_receipt = read_state_json(
        resume_state,
        "pipeline-verification",
        str(resume_facts["successor_b1"]),
        "receipt.json",
    )
    check(
        "restart resumes the interrupted predecessor-bound successor and "
        "creates no third attempt",
        resume_rc == 0
        and resume_parent.state == "finalizing"
        and resume_children
        == {
            str(resume_facts["complete_a"]): "complete",
            str(resume_facts["stale_b0"]): "failed",
            str(resume_facts["successor_b1"]): "complete",
        }
        and isinstance(resume_receipt, dict)
        and resume_receipt["status"] == "complete"
        and resume_receipt["head_sha"] == resume_facts["head_b"]
        and resume_receipt["verification_attempt"]["attempt_index"] == 1
        and probe_commands[resume_task] == scoped_commands,
        (resume_rc, resume_parent, resume_children, resume_receipt),
    )

    # Both same-HEAD attempt identities burned without receipts: the handoff
    # budget is exhausted — typed attention, no replacement, no probes.
    exhausted_task = "27300000-2730-4273-8273-273000000004"
    exhausted_store, _exhausted_cmux, exhausted_state, exhausted_rc = run_case(
        root,
        exhausted_task,
        summary,
        pipeline_name="engineering/change",
        before_start=seed_invalidated_state(
            exhausted_task, stale_terminal=True, seed_attempt1="terminal"
        ),
        verification_runner=make_probe_runner(exhausted_task),
        review_launcher=approve_at_current_head(exhausted_task),
        review_state="missing",
        wake_source=FallbackWakeSource(),
    )
    exhausted_facts = seeded[exhausted_task]
    exhausted_parent = exhausted_store.read("owner-1", exhausted_task)
    check(
        "exhausted successor identities stay typed attention with no "
        "replacement and no probe replay",
        exhausted_rc == 0
        and exhausted_parent.state == "attention-required"
        and exhausted_parent.attention_reason == AttentionReason.RETRY_EXHAUSTED
        and not exhausted_parent.accepted_callback_id
        and verify_children(exhausted_store)
        == {
            str(exhausted_facts["complete_a"]): "complete",
            str(exhausted_facts["stale_b0"]): "failed",
            str(exhausted_facts["successor_b1"]): "failed",
        }
        and probe_commands[exhausted_task] == []
        and read_state_json(
            exhausted_state,
            "pipeline-verification",
            str(exhausted_facts["successor_b1"]),
            "receipt.json",
        )
        is None,
        (exhausted_rc, exhausted_parent, verify_children(exhausted_store)),
    )

    # Tracked or untracked product dirt at the current HEAD refuses the
    # handoff outright: typed attention with no terminalization, no
    # invalidation record, no successor, and no probe effect.
    for dirt_label, dirt_task, make_dirty in (
        (
            "tracked product dirt",
            "27300000-2730-4273-8273-273000000005",
            lambda worktree: (worktree / "product.txt").write_text(
                "ready\ndirty\n", encoding="utf-8"
            ),
        ),
        (
            "untracked product dirt",
            "27300000-2730-4273-8273-273000000006",
            lambda worktree: (worktree / "junk.txt").write_text(
                "junk\n", encoding="utf-8"
            ),
        ),
    ):

        def seed_with_dirt(task: str, make_dirty=make_dirty):
            seed = seed_invalidated_state(task, stale_terminal=False)

            def before_start(
                vault: Path, worktree: Path, state: Path, profile_sha: str
            ) -> None:
                seed(vault, worktree, state, profile_sha)
                make_dirty(worktree)

            return before_start

        dirt_store, _dirt_cmux, dirt_state, dirt_rc = run_case(
            root,
            dirt_task,
            summary,
            pipeline_name="engineering/change",
            before_start=seed_with_dirt(dirt_task),
            verification_runner=make_probe_runner(dirt_task),
            review_launcher=approve_at_current_head(dirt_task),
            review_state="missing",
            wake_source=FallbackWakeSource(),
        )
        dirt_facts = seeded[dirt_task]
        dirt_parent = dirt_store.read("owner-1", dirt_task)
        dirt_latch = read_state_json(dirt_state, "callback-error.json")
        check(
            f"{dirt_label} before the handoff stays typed attention with "
            "no mutation, no replacement, and no probes",
            dirt_rc == 0
            and dirt_parent.state == "attention-required"
            and dirt_parent.attention_reason
            == AttentionReason.ATTENTION_REQUIRED
            and not dirt_parent.accepted_callback_id
            and verify_children(dirt_store)
            == {
                str(dirt_facts["complete_a"]): "complete",
                str(dirt_facts["stale_b0"]): "verifying",
            }
            and probe_commands[dirt_task] == []
            and read_state_json(
                dirt_state,
                "pipeline-verification",
                str(dirt_facts["stale_b0"]),
                "invalidation.json",
            )
            is None
            and isinstance(dirt_latch, dict)
            and dirt_latch.get("status") == "pipeline-verification-dirty-tree",
            (dirt_rc, dirt_parent, verify_children(dirt_store), dirt_latch),
        )

    # Bytes mutated while the replacement's probes run can never be attested
    # as the clean HEAD: no successor receipt and no root-attention clearance.
    probe_dirt_task = "27300000-2730-4273-8273-273000000007"
    probe_dirt_worktree = root / f"worktree-{probe_dirt_task}"
    probe_dirt_recorded = probe_commands.setdefault(probe_dirt_task, [])

    def dirtying_probe_runner(argv: list[str], **kwargs: object):
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
        (probe_dirt_worktree / "probe-junk.txt").write_text(
            "mutated during probes\n", encoding="utf-8"
        )
        probe_dirt_recorded.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    probe_dirt_store, _probe_cmux, probe_dirt_state, probe_dirt_rc = run_case(
        root,
        probe_dirt_task,
        summary,
        pipeline_name="engineering/change",
        before_start=seed_invalidated_state(
            probe_dirt_task, stale_terminal=True
        ),
        verification_runner=dirtying_probe_runner,
        review_launcher=approve_at_current_head(probe_dirt_task),
        review_state="missing",
        wake_source=FallbackWakeSource(),
    )
    probe_dirt_facts = seeded[probe_dirt_task]
    probe_dirt_parent = probe_dirt_store.read("owner-1", probe_dirt_task)
    check(
        "during-probe dirt on the replacement is never receipted and never "
        "clears root attention",
        probe_dirt_rc == 0
        and probe_dirt_parent.state == "attention-required"
        and not probe_dirt_parent.accepted_callback_id
        and len(probe_dirt_recorded) >= 1
        and read_state_json(
            probe_dirt_state,
            "pipeline-verification",
            str(probe_dirt_facts["successor_b1"]),
            "receipt.json",
        )
        is None,
        (probe_dirt_rc, probe_dirt_parent, probe_dirt_recorded),
    )


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def write_json_atomic(
    path: Path,
    value: object,
    *,
    publication_barrier: tuple[threading.Event, threading.Event] | None = None,
) -> None:
    """Publish a live fixture artifact without exposing partial JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, sort_keys=True) + "\n")
        if publication_barrier is not None:
            ready, release = publication_barrier
            ready.set()
            if not release.wait(timeout=2.0):
                raise AssertionError("atomic JSON publication was not released")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def start_checked_thread(
    target: Callable[[], None],
    threads: list[threading.Thread],
    errors: list[BaseException],
) -> None:
    """Start one fixture helper whose failure remains main-thread evidence."""

    def checked() -> None:
        try:
            target()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=checked)
    threads.append(thread)
    thread.start()


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def read_json_eventually(path: Path, *, timeout: float = 10.0) -> object:
    """Read one worker artifact after its bounded atomic-publication window."""

    deadline = time.monotonic() + timeout
    while True:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def dispatch_record(
    store: OperationStore,
    operation_id: str,
    *,
    bind_contract: bool = True,
    pipeline_name: str = "lifecycle/default",
) -> None:
    route = RuntimeRoute("codex", "gpt-5.6-sol", "high", "executor", "a" * 64)
    lifecycle = compile_pipeline(
        builtin_definitions()[pipeline_name],
        builtin_registry(),
        capabilities=("route:resolved",),
    )
    store.create(
        OperationSpec(
            operation_id,
            f"key-{operation_id}",
            "dispatch",
            "owner-1",
            route,
            "packets/task.json",
            "scoped",
            contract_sha256=(
                lifecycle.definition_sha256 if bind_contract else ""
            ),
        ),
        lane_id="lane-1",
        run_id=f"run-{operation_id}",
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition("owner-1", operation_id, state)
def task_meta(
    vault: Path,
    worktree: Path,
    plan: Path,
    task_id: str,
    profile_sha: str,
    pipeline_name: str,
    completion_policy: str = "attention",
    total_pass_limit: int = 2,
    version: int = 3,
) -> dict[str, object]:
    pipeline = compile_pipeline(
        builtin_definitions()[pipeline_name],
        builtin_registry(),
        capabilities=("route:resolved",),
    )
    meta: dict[str, object] = {
        "version": version,
        "project_id": PROJECT,
        "task_id": task_id,
        "task_name": "Runtime summary",
        "origin_session": "coordinator-session",
        "executor_runtime": "codex",
        "interaction_policy": "unattended",
        "pipeline_policy": {
            "name": pipeline_name,
            "definition_sha256": pipeline.definition_sha256,
            "completion_policy": completion_policy,
            "total_pass_limit": total_pass_limit,
        },
        "plan_file": str(plan),
        "approved_plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "vault_root": str(vault),
        "review_policy": {
            "mode": "skip",
            "cross_model": False,
            "runtime": "",
            "model": "",
            "effort": "",
            "max_verify_iterations": 0,
            "verification_profile": "scoped",
            "verification_profile_sha256": profile_sha,
            "auto_resolve_severities": ["warning", "nit"],
            "escalate_severities": ["blocking"],
        },
        "reap_policy": {
            "mode": "final",
            "auto_file": True,
            "allowed_types": ["session"],
            "title": "Runtime Result",
        },
        "surface_policy": {"auto_close": True, "placement": "split"},
        "watchdog_policy": {
            "enabled": True,
            "poll_seconds": 30,
            "warn_after_seconds": 900,
            "alert_after_seconds": 1200,
        },
        "forbidden_actions": [
            "push",
            "deploy",
            "publish",
            "delete-worktree",
            "delete-branch",
            "expand-scope",
        ],
        "task_surface": CHILD,
        "worktree": str(worktree),
    }
    if version == 4:
        snapshot = bind_approved_plan_snapshot(
            {"vault_root": vault, "plan_file": plan}
        )
        meta["plan_snapshot_file"] = str(snapshot["_approved_plan_file"])
        meta["outcome_contract_sha256"] = extract_from_bytes(plan.read_bytes()).sha256
        review = dict(meta["review_policy"])
        review.pop("auto_resolve_severities")
        review.pop("escalate_severities")
        meta["review_policy"] = review
    return meta


def run_case(
    root: Path,
    operation_id: str,
    summary: object,
    *,
    review_state: str = "skipped",
    review_launcher: Callable[[Path, Path], None] | None = None,
    before_start: (
        Callable[[Path, Path, Path, str], None] | None
    ) = None,
    bind_contract: bool = True,
    pipeline_name: str = "lifecycle/default",
    fix_outcome: str = "complete",
    fix_retry_passes: int = 0,
    fix_retry_summary: object | None = None,
    fix_retry_null_change: bool = False,
    fix_restart_after: str = "",
    model_restart_limit: int | None = None,
    completion_policy: str = "attention",
    total_pass_limit: int = 2,
    verification_runner: Callable[..., subprocess.CompletedProcess[str]]
    | None = None,
    task_version: int = 3,
    bind_runtime_resources: bool = False,
    typed_review: bool = False,
    await_final_callback: bool = False,
    atomic_publication_barrier: bool = False,
    phase_callback_publication_barrier_step: str = "",
    wake_source: object | None = None,
    monotonic_clock: Callable[[], float] | None = None,
    restart_after_attention: bool = False,
    cmux: FakeCmux | None = None,
    summary_publication_barrier: (
        tuple[threading.Event, threading.Event, threading.Event] | None
    ) = None,
) -> tuple[
    OperationStore, FakeCmux, Path, int
]:
    vault = root / f"vault-{operation_id}"
    worktree = root / f"worktree-{operation_id}"
    (vault / "wiki" / "plans").mkdir(parents=True)
    (vault / "scripts").mkdir()
    (vault / "config").mkdir()
    shutil.copy2(
        ROOT / "config" / "verification-profiles.toml",
        vault / "config" / "verification-profiles.toml",
    )
    (vault / "scripts" / "reap-runner.py").write_text(
        "#!/usr/bin/env python3\n", encoding="utf-8"
    )
    worktree.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "runtime@example.invalid"],
        cwd=worktree,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Runtime Summary Test"],
        cwd=worktree,
        check=True,
    )
    (worktree / "product.txt").write_text("ready\n", encoding="utf-8")
    # Runtime transport is repository-ignored exactly as in the product
    # checkout (`.git/info/exclude` there), so cleanliness observation sees
    # only real product dirt.
    (worktree / ".gitignore").write_text(
        ".task-*\n..task-*\n.provider-*\n.atomic-*\n"
        ".null-change-retry\n.review-*\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "product.txt", ".gitignore"], cwd=worktree, check=True
    )
    subprocess.run(
        ["git", "commit", "-m", "ready"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    plan = vault / "wiki" / "plans" / "approved.md"
    plan.write_text(
        "# Approved\n\n```json\n"
        '{"schema_version":1,"desired_outcome":"Complete the runtime fixture.",'
        '"success_evidence":[{"evidence_id":"runtime-green",'
        '"observable":"The runtime accepts the exact typed summary."}],'
        '"non_goals":["No authority expansion."]}\n```\n',
        encoding="utf-8",
    )
    write_json(
        vault
        / ".vault-meta"
        / "task-sessions"
        / "session-bindings"
        / "coordinator-session"
        / "binding.json",
        {
            "session_id": "coordinator-session",
            "project_id": PROJECT,
            "task_id": operation_id,
        },
    )
    store = OperationStore(vault / ".vault-meta" / "harness")
    dispatch_record(
        store,
        operation_id,
        bind_contract=bind_contract,
        pipeline_name=pipeline_name,
    )
    if bind_runtime_resources:
        resources = OwnedResources(
            surface_id=CHILD,
            process_group=4101,
            supervisor_pid=4102,
            process_identity="1" * 64,
            supervisor_identity="2" * 64,
        )
        OperationSupervisor(
            store, "owner-1", operation_id
        ).bind_resources(resources)
        write_json(
            store.root
            / "owners"
            / "owner-1"
            / "runtime"
            / operation_id
            / "session.json",
            {
                "schema_version": 1,
                "operation_id": operation_id,
                "run_id": f"run-{operation_id}",
                "placement": "split",
            },
        )
        write_json(
            store.root
            / "owners"
            / "owner-1"
            / "runtime"
            / operation_id
            / "callback-target.json",
            {
                "schema_version": 1,
                "generation": 1,
                "operation_id": operation_id,
                "run_id": f"run-{operation_id}",
                "callback_pointer": ".task-summary.json",
            },
        )
    if model_restart_limit is not None:
        OperationSupervisor(
            store, "owner-1", operation_id
        ).configure_budget(
            attempt_limit=3,
            model_restart_limit=model_restart_limit,
            time_budget_seconds=DEFAULT_TIME_BUDGET_SECONDS,
            token_limit=DEFAULT_TOKEN_LIMIT,
        )
    profile_sha = load_profiles(
        vault / "config" / "verification-profiles.toml"
    )["scoped"].sha256
    meta = task_meta(
        vault,
        worktree,
        plan,
        operation_id,
        profile_sha,
        pipeline_name,
        completion_policy,
        total_pass_limit,
        task_version,
    )
    if typed_review:
        meta["review_policy"] = {
            "mode": "simple",
            "cross_model": False,
            "runtime": "codex",
            "model": "sol",
            "effort": "high",
            "max_verify_iterations": 1,
            "verification_profile": "scoped",
            "verification_profile_sha256": profile_sha,
            "auto_resolve_severities": ["warning", "nit"],
            "escalate_severities": ["blocking"],
        }
    write_json(worktree / ".task-meta.json", meta)
    if review_state == "skipped":
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        ReviewGateController.skip(
            store.root / "review-data" / operation_id / operation_id,
            dispatch_operation_id=operation_id,
            owner_id=operation_id,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                profile_sha,
            ),
            product_root=worktree,
        )
    (worktree / ".task-prompt.md").write_text(
        "Complete the approved task and write the canonical summary.",
        encoding="utf-8",
    )
    parent = store.read("owner-1", operation_id)
    request = RuntimeSessionRequest(
        parent.spec,
        parent.lane_id,
        parent.run_id,
        ORIGIN,
        worktree,
        ".task-prompt.md",
        ".task-summary.json",
        callback_mode="task-summary",
        task_summary_pointer=".task-summary.json",
    )
    check(
        "request carries canonical task-summary mode without a wake command",
        request.callback_mode == "task-summary"
        and request.task_summary_pointer == ".task-summary.json"
        and not hasattr(request, "wake_message"),
    )
    provider = root / f"provider-{operation_id}.py"
    if pipeline_name == "engineering/fix" and fix_restart_after:
        provider.write_text(
            ATOMIC_SUMMARY_PUBLISHER
            + "import hashlib,json,pathlib,subprocess,sys,time\n"
            "root=pathlib.Path.cwd()\n"
            "summary=pathlib.Path(sys.argv[1])\n"
            "publish_summary(summary,sys.argv[2])\n"
            "if (root/'.provider-prelaunched-reproduce').is_file():\n"
            "  request=root/'.task-pipeline-step-request.json'\n"
            "  for _ in range(500):\n"
            "    if request.is_file() and json.loads(request.read_text(encoding='utf-8')).get('step_id')!='reproduce': break\n"
            "    time.sleep(0.01)\n"
            "  else: raise SystemExit(3)\n"
            "state=pathlib.Path(sys.argv[4])\n"
            "callback_barrier=sys.argv[5] if len(sys.argv)>7 else ''\n"
            "callback_barrier_step=sys.argv[6] if len(sys.argv)>7 else ''\n"
            "restart_after=sys.argv[7] if len(sys.argv)>7 else sys.argv[5]\n"
            "request=root/'.task-pipeline-step-request.json'\n"
            "outbox=root/'.task-pipeline-step-callback.json'\n"
            "log=root/'.provider-step-log.json'\n"
            "crashed=root/'.provider-crashed.json'\n"
            "seen=json.loads(log.read_text(encoding='utf-8')) if log.is_file() else []\n"
            "for _ in range(1000):\n"
            "  if request.is_file():\n"
            "    row=json.loads(request.read_text(encoding='utf-8'))\n"
            "  else: row={}\n"
            "  if row.get('operation_id') and row['operation_id'] not in [item['operation_id'] for item in seen]:\n"
            "    output=root/row['output_pointer']\n"
            "    output.parent.mkdir(parents=True,exist_ok=True)\n"
            "    output.write_text(row['step_id']+' evidence\\n',encoding='utf-8')\n"
            "    head=subprocess.run(['git','rev-parse','HEAD'],cwd=root,text=True,capture_output=True,check=True).stdout.strip()\n"
            "    payload={key:row[key] for key in ('schema_version','parent_operation_id','definition_sha256','step_id','iteration','input_schema','input_sha256','input_head_sha','prior_receipt_sha256','verification_sha256','output_schema')}\n"
            "    payload.update({'output_pointer':row['output_pointer'],'output_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'head_sha':head,'status':'complete'})\n"
            "    encoded=json.dumps(payload,sort_keys=True,separators=(',',':')).encode()\n"
            "    digest=hashlib.sha256(encoded).hexdigest()\n"
            "    callback={'schema_version':1,'callback_id':'result-'+digest[:24],'operation_id':row['operation_id'],'run_id':row['run_id'],'kind':'result','payload':payload,'payload_sha256':digest}\n"
            "    callback_text=json.dumps(callback,sort_keys=True)+'\\n'\n"
            "    publish_summary(outbox,callback_text,callback_barrier if row['step_id']==callback_barrier_step else '')\n"
            "    for _ in range(500):\n"
            "      if not outbox.exists(): break\n"
            "      time.sleep(0.01)\n"
            "    else: raise SystemExit(4)\n"
            "    if row['step_id']==restart_after:\n"
            "      timing=state/'pipeline-fix'/'timing'/'pass-0'/row['step_id']\n"
            "      (state/'pipeline-fix'/'timing-before-restart.json').write_text(json.dumps({'step_id':row['step_id'],'start':(timing/'start.json').read_text(encoding='utf-8'),'completion':(timing/'completion.json').read_text(encoding='utf-8')},sort_keys=True)+'\\n',encoding='utf-8')\n"
            "    seen.append({'operation_id':row['operation_id'],'step_id':row['step_id']})\n"
            "    log.write_text(json.dumps(seen,sort_keys=True)+'\\n',encoding='utf-8')\n"
            "    if row['step_id']==restart_after and not crashed.is_file():\n"
            "      crashed.write_text(json.dumps({'status':'exited-after-acceptance'})+'\\n',encoding='utf-8')\n"
            "      raise SystemExit(17)\n"
            "    if row['step_id']=='minimal-fix':\n"
            "      for _ in range(500):\n"
            "        if (state/'pipeline-fix'/'finalization-notify.json').is_file(): break\n"
            "        time.sleep(0.01)\n"
            "      else: raise SystemExit(5)\n"
            "      publish_summary(summary,sys.argv[2])\n"
            "      time.sleep(0.3)\n"
            "      raise SystemExit(0)\n"
            "  time.sleep(0.01)\n"
            "raise SystemExit(3)\n",
            encoding="utf-8",
        )
    elif pipeline_name == "engineering/fix" and fix_retry_passes:
        provider.write_text(
            ATOMIC_SUMMARY_PUBLISHER
            + "import hashlib,json,pathlib,subprocess,sys,time\n"
            "root=pathlib.Path.cwd()\n"
            "summary=pathlib.Path(sys.argv[1])\n"
            "publish_summary(summary,sys.argv[2])\n"
            "state=pathlib.Path(sys.argv[4])\n"
            "passes=int(sys.argv[5])\n"
            "request=root/'.task-pipeline-step-request.json'\n"
            "outbox=root/'.task-pipeline-step-callback.json'\n"
            "seen=set()\n"
            "for iteration in range(passes):\n"
            "  expected_steps=('reproduce','root-cause','regression-test','minimal-fix') if iteration==0 else ('root-cause','regression-test','minimal-fix')\n"
            "  for expected in expected_steps:\n"
            "    for _ in range(500):\n"
            "      if request.is_file():\n"
            "        row=json.loads(request.read_text(encoding='utf-8'))\n"
            "        if row.get('iteration')==iteration and row.get('step_id')==expected and row.get('operation_id') not in seen: break\n"
            "      time.sleep(0.01)\n"
            "    else: raise SystemExit(3)\n"
            "    seen.add(row['operation_id'])\n"
            "    output=root/row['output_pointer']\n"
            "    output.parent.mkdir(parents=True,exist_ok=True)\n"
            "    output.write_text(f'{iteration}:{expected} evidence\\n',encoding='utf-8')\n"
            "    head=subprocess.run(['git','rev-parse','HEAD'],cwd=root,text=True,capture_output=True,check=True).stdout.strip()\n"
            "    payload={key:row[key] for key in ('schema_version','parent_operation_id','definition_sha256','step_id','iteration','input_schema','input_sha256','input_head_sha','prior_receipt_sha256','verification_sha256','output_schema')}\n"
            "    payload.update({'output_pointer':row['output_pointer'],'output_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'head_sha':head,'status':'complete'})\n"
            "    encoded=json.dumps(payload,sort_keys=True,separators=(',',':')).encode()\n"
            "    digest=hashlib.sha256(encoded).hexdigest()\n"
            "    callback={'schema_version':1,'callback_id':'result-'+digest[:24],'operation_id':row['operation_id'],'run_id':row['run_id'],'kind':'result','payload':payload,'payload_sha256':digest}\n"
            "    publish_summary(outbox,json.dumps(callback,sort_keys=True)+'\\n')\n"
            "    for _ in range(500):\n"
            "      if not outbox.exists(): break\n"
            "      time.sleep(0.01)\n"
            "    else: raise SystemExit(4)\n"
            "  marker=(state/'pipeline-fix'/'finalization-notify.json') if iteration==0 else (state/'pipeline-fix'/f'pass-{iteration}'/'finalization-notify.json')\n"
            "  for _ in range(2000):\n"
            "    if marker.is_file(): break\n"
            "    time.sleep(0.01)\n"
            "  else: raise SystemExit(5)\n"
            "  if iteration==0 or not (root/'.null-change-retry').is_file():\n"
            "    subprocess.run(['git','commit','--allow-empty','-m',f'provider pass {iteration + 1}'],cwd=root,text=True,capture_output=True,check=True)\n"
            "  publish_summary(summary,sys.argv[6] if iteration and len(sys.argv)>6 else sys.argv[2])\n"
            "publish_summary(root/'.provider-final-summary-published',json.dumps({'schema_version':1,'iteration':passes-1,'summary_sha256':hashlib.sha256(summary.read_bytes()).hexdigest()},sort_keys=True)+'\\n')\n"
            "if (root/'.provider-await-final-callback').is_file():\n"
            "  for _ in range(2000):\n"
            "    if (state/'callback-receipt.json').is_file(): break\n"
            "    time.sleep(0.01)\n"
            "  else: raise SystemExit(6)\n"
            "else:\n"
            "  for _ in range(2000):\n"
            "    if (root/'.provider-terminal-observed').is_file(): break\n"
            "    time.sleep(0.01)\n"
            "  else: raise SystemExit(7)\n",
            encoding="utf-8",
        )
    elif pipeline_name == "engineering/fix":
        provider.write_text(
            ATOMIC_SUMMARY_PUBLISHER
            + "import hashlib,json,pathlib,subprocess,sys,time\n"
            "root=pathlib.Path.cwd()\n"
            "summary=pathlib.Path(sys.argv[1])\n"
            "time.sleep(0.2)\n"
            "publish_summary(summary,sys.argv[2])\n"
            "outcome=sys.argv[3]\n"
            "state=pathlib.Path(sys.argv[4])\n"
            "request=root/'.task-pipeline-step-request.json'\n"
            "outbox=root/'.task-pipeline-step-callback.json'\n"
            "log=root/'.provider-step-log.json'\n"
            "seen=set()\n"
            "processed=[]\n"
            "for expected in ('reproduce','root-cause','regression-test','minimal-fix'):\n"
            "  for _ in range(2000):\n"
            "    if request.is_file():\n"
            "      row=json.loads(request.read_text(encoding='utf-8'))\n"
            "      if row.get('step_id')==expected and row.get('operation_id') not in seen: break\n"
            "    time.sleep(0.01)\n"
            "  else: raise SystemExit(3)\n"
            "  seen.add(row['operation_id'])\n"
            "  processed.append({'operation_id':row['operation_id'],'step_id':row['step_id']})\n"
            "  log.write_text(json.dumps(processed,sort_keys=True)+'\\n',encoding='utf-8')\n"
            "  output=root/row['output_pointer']\n"
            "  output.parent.mkdir(parents=True,exist_ok=True)\n"
            "  output.write_text(expected+' evidence\\n',encoding='utf-8')\n"
            "  head=subprocess.run(['git','rev-parse','HEAD'],cwd=root,text=True,capture_output=True,check=True).stdout.strip()\n"
            "  status='cannot-reproduce' if expected=='reproduce' and outcome=='cannot-reproduce' else 'complete'\n"
            "  payload={key:row[key] for key in ('schema_version','parent_operation_id','definition_sha256','step_id','iteration','input_schema','input_sha256','input_head_sha','prior_receipt_sha256','verification_sha256','output_schema')}\n"
            "  payload.update({'output_pointer':row['output_pointer'],'output_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'head_sha':head,'status':status})\n"
            "  encoded=json.dumps(payload,sort_keys=True,separators=(',',':')).encode()\n"
            "  digest=hashlib.sha256(encoded).hexdigest()\n"
            "  callback={'schema_version':1,'callback_id':'result-'+digest[:24],'operation_id':row['operation_id'],'run_id':row['run_id'],'kind':'result','payload':payload,'payload_sha256':digest}\n"
            "  publish_summary(outbox,json.dumps(callback,sort_keys=True)+'\\n')\n"
            "  for _ in range(2000):\n"
            "    if not outbox.exists(): break\n"
            "    time.sleep(0.01)\n"
            "  else: raise SystemExit(4)\n"
            "  if status=='cannot-reproduce': time.sleep(0.1); raise SystemExit(0)\n"
            "for _ in range(2000):\n"
            "  if (state/'pipeline-fix'/'finalization-notify.json').is_file(): break\n"
            "  time.sleep(0.01)\n"
            "else: raise SystemExit(5)\n"
            "publish_summary(summary,sys.argv[2])\n"
            "time.sleep(0.3)\n",
            encoding="utf-8",
        )
    else:
        provider.write_text(
            ATOMIC_SUMMARY_PUBLISHER
            + "import json,pathlib,sys,time\n"
            "summary=pathlib.Path(sys.argv[1])\n"
            f"publish_summary(summary,sys.argv[2],sys.argv[3] if {atomic_publication_barrier!r} else '')\n"
            + (
                "state=pathlib.Path(sys.argv[3])\n"
                "for _ in range(2000):\n"
                "  if (state/'callback-receipt.json').is_file(): break\n"
                "  time.sleep(0.01)\n"
                "else: raise SystemExit(8)\n"
                if restart_after_attention
                else "time.sleep(0.3)\n"
            ),
            encoding="utf-8",
        )
    summary_path = worktree / ".task-summary.json"
    launch = ProcessAdapter().prepare_surface_launch(
        argv=(
            str(Path(sys.executable).resolve()),
            str(provider),
            str(summary_path),
            json.dumps(summary, sort_keys=True),
            *(
                (str(worktree / ".atomic-summary-publication"),)
                if atomic_publication_barrier
                else ()
            ),
            *(
                (str(root / f"state-{operation_id}"),)
                if restart_after_attention
                and pipeline_name != "engineering/fix"
                else ()
            ),
            *(
                (fix_outcome,)
                if pipeline_name == "engineering/fix"
                else ()
            ),
            *(
                (str(root / f"state-{operation_id}"),)
                if pipeline_name == "engineering/fix"
                else ()
            ),
            *(
                (str(fix_retry_passes),)
                if pipeline_name == "engineering/fix"
                and fix_retry_passes
                else ()
            ),
            *(
                (json.dumps(fix_retry_summary, sort_keys=True),)
                if pipeline_name == "engineering/fix"
                and fix_retry_passes
                and fix_retry_summary is not None
                else ()
            ),
            *(
                (
                    str(worktree / ".atomic-phase-callback-publication"),
                    phase_callback_publication_barrier_step,
                )
                if phase_callback_publication_barrier_step
                else ()
            ),
            *(
                (fix_restart_after,)
                if pipeline_name == "engineering/fix"
                and fix_restart_after
                else ()
            ),
        ),
        cwd=worktree,
        state_root=root / f"state-{operation_id}",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=summary_path,
        store_root=store.root,
        owner_id="owner-1",
        operation_id=operation_id,
        run_id=f"run-{operation_id}",
        surface_id=CHILD,
        runtime="codex",
        callback_mode="task-summary",
        task_summary_pointer=summary_path,
        origin_surface=ORIGIN,
    )
    cmux = cmux or FakeCmux()
    if fix_retry_null_change:
        # The retry provider reads this marker and leaves HEAD untouched, so the
        # bounded retry completes with an intentionally empty change set.
        (worktree / ".null-change-retry").write_text(
            "null-change\n", encoding="utf-8"
        )
    if await_final_callback:
        if pipeline_name != "engineering/fix" or not fix_retry_passes:
            raise AssertionError(
                "final callback wait requires the retry fixture"
            )
        (worktree / ".provider-await-final-callback").write_text(
            "await\n", encoding="utf-8"
        )
    if before_start is not None:
        before_start(
            vault,
            worktree,
            launch.spec_path.parent,
            profile_sha,
        )
    result: list[int] = []
    watcher_observed = threading.Event()
    original_inspect_task_summary = None
    original_summary_barrier_inspection = None
    phase_callback_watcher_observed = threading.Event()
    original_accept_fix_callback = None
    if atomic_publication_barrier:
        if pipeline_name != "lifecycle/default":
            raise AssertionError(
                "atomic publication barrier is limited to the summary fixture"
            )
        original_inspect_task_summary = (
            RuntimeWorkerExecution.inspect_task_summary
        )

        def observe_atomic_publication(
            worker: RuntimeWorkerExecution,
        ) -> None:
            if worker.spec["operation_id"] == operation_id:
                watcher_observed.set()
            original_inspect_task_summary(worker)

        RuntimeWorkerExecution.inspect_task_summary = (
            observe_atomic_publication
        )
    if phase_callback_publication_barrier_step:
        if pipeline_name != "engineering/fix" or not fix_restart_after:
            raise AssertionError(
                "phase callback barrier requires the restart fixture"
            )
        original_accept_fix_callback = (
            RuntimeWorkerExecution.accept_fix_callback
        )

        def observe_atomic_phase_callback(
            worker: RuntimeWorkerExecution,
            state: object,
            round_: object,
            callback_path: Path,
        ) -> None:
            if (
                worker.spec["operation_id"] == operation_id
                and getattr(round_, "step_id", "")
                == phase_callback_publication_barrier_step
            ):
                phase_callback_watcher_observed.set()
            original_accept_fix_callback(
                worker, state, round_, callback_path
            )

        RuntimeWorkerExecution.accept_fix_callback = (
            observe_atomic_phase_callback
        )
    if summary_publication_barrier is not None:
        ready, release, observed = summary_publication_barrier
        original_summary_barrier_inspection = (
            RuntimeWorkerExecution.inspect_task_summary
        )

        def observe_summary_publication(
            worker: RuntimeWorkerExecution,
        ) -> None:
            original_summary_barrier_inspection(worker)
            if ready.is_set() and not observed.is_set():
                observed.set()
                release.set()

        RuntimeWorkerExecution.inspect_task_summary = (
            observe_summary_publication
        )
    thread = threading.Thread(
        target=lambda: result.append(
            run_worker(
                launch.spec_path,
                poll_seconds=0.02,
                checkpoint_probe=lambda _surface, _runtime: "checkpoint-1",
                cmux_adapter=cmux,
                review_launcher=review_launcher,
                verification_runner=verification_runner,
                wake_source=wake_source or FallbackWakeSource(),
                monotonic_clock=monotonic_clock,
            )
        )
    )
    thread.start()
    atomic_publication_evidence: dict[str, object] = {}
    phase_callback_publication_evidence: dict[str, object] = {}
    if atomic_publication_barrier:
        barrier = worktree / ".atomic-summary-publication"
        ready = barrier.with_suffix(".ready")
        release = barrier.with_suffix(".release")
        try:
            deadline = time.monotonic() + 2.0
            while not ready.is_file():
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "atomic summary provider did not reach its barrier"
                    )
                time.sleep(0.005)
            watcher_observed.clear()
            if not watcher_observed.wait(timeout=1.0):
                raise AssertionError(
                    "task-summary watcher did not inspect the pre-replace window"
                )
            temporary = worktree / ready.read_text(
                encoding="utf-8"
            ).strip()
            atomic_publication_evidence = {
                "summary_absent": not summary_path.exists(),
                "temporary_is_local": (
                    temporary.parent.resolve() == worktree.resolve()
                    and temporary.name.startswith("..task-summary.json.")
                ),
                "temporary_is_partial": (
                    temporary.is_file()
                    and temporary.read_text(encoding="utf-8")
                    == json.dumps(summary, sort_keys=True)[:1]
                ),
                "operation_state": store.read(
                    "owner-1", operation_id
                ).state,
                "callback_error_absent": not (
                    launch.spec_path.parent / "callback-error.json"
                ).exists(),
            }
        finally:
            release.write_text("release\n", encoding="utf-8")
    if phase_callback_publication_barrier_step:
        barrier = worktree / ".atomic-phase-callback-publication"
        ready = barrier.with_suffix(".ready")
        release = barrier.with_suffix(".release")
        try:
            deadline = time.monotonic() + 4.0
            while not ready.is_file():
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "synthetic phase callback did not reach its barrier"
                    )
                time.sleep(0.005)
            phase_callback_watcher_observed.clear()
            if not phase_callback_watcher_observed.wait(timeout=1.0):
                raise AssertionError(
                    "phase callback watcher did not inspect the pre-replace window"
                )
            temporary = worktree / ready.read_text(
                encoding="utf-8"
            ).strip()
            callback_path = worktree / ".task-pipeline-step-callback.json"
            phase_callback_publication_evidence = {
                "callback_absent": not callback_path.exists(),
                "temporary_is_local": (
                    temporary.parent.resolve() == worktree.resolve()
                    and temporary.name.startswith(
                        "..task-pipeline-step-callback.json."
                    )
                ),
                "temporary_is_partial": (
                    temporary.is_file()
                    and temporary.read_text(encoding="utf-8") == "{"
                ),
                "operation_state": store.read(
                    "owner-1", operation_id
                ).state,
                "callback_error_absent": not (
                    launch.spec_path.parent / "callback-error.json"
                ).exists(),
            }
        finally:
            release.write_text("release\n", encoding="utf-8")
    if review_state == "delayed-skip":
        # The provider exits after 0.3s; approval arrives later. The runtime
        # worker must remain alive as the code-owned finalization watcher.
        time.sleep(0.45)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        ReviewGateController.skip(
            store.root / "review-data" / operation_id / operation_id,
            dispatch_operation_id=operation_id,
            owner_id=operation_id,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                profile_sha,
            ),
            product_root=worktree,
        )
    # The join timeout only bounds hang detection; passing workers join the
    # moment they finish.  Multi-round resolution and fix-retry corridors
    # re-observe the exact candidate HEAD and tree at every consumption
    # boundary, which at the fixture's 0.02s poll cadence needs headroom a
    # one-pass corridor never does.
    join_timeout = (
        max(12, 6 * fix_retry_passes)
        if wake_source is not None or fix_retry_passes
        else 8
    )
    if fix_retry_passes and not await_final_callback:
        deadline = time.monotonic() + join_timeout
        while True:
            current = store.read("owner-1", operation_id)
            final_publication_path = (
                worktree / ".provider-final-summary-published"
            )
            if final_publication_path.is_file():
                final_publication = json.loads(
                    final_publication_path.read_text(encoding="utf-8")
                )
                if final_publication != {
                    "schema_version": 1,
                    "iteration": fix_retry_passes - 1,
                    "summary_sha256": hashlib.sha256(
                        json.dumps(
                            fix_retry_summary
                            if fix_retry_summary is not None
                            else summary,
                            sort_keys=True,
                        ).encode()
                    ).hexdigest(),
                }:
                    raise AssertionError(
                        "final provider publication identity changed"
                    )
                (worktree / ".provider-terminal-observed").write_text(
                    f"pass-{fix_retry_passes - 1}\n", encoding="utf-8"
                )
            if not thread.is_alive():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=min(0.05, remaining))
    else:
        thread.join(timeout=join_timeout)
    if thread.is_alive():
        current = store.read("owner-1", operation_id)
        raise AssertionError(
            "runtime worker exceeded the bounded fixture join: "
            f"operation_id={operation_id} state={current.state} "
            f"revision={current.revision} "
            f"attention_reason={current.attention_reason}"
        )
    if not result:
        raise AssertionError(
            "runtime worker terminated without a fixture result: "
            f"operation_id={operation_id}"
        )
    if (
        restart_after_attention
        and store.read("owner-1", operation_id).state
        == "attention-required"
    ):
        paused = store.read("owner-1", operation_id)
        store.transition(
            "owner-1",
            operation_id,
            paused.resume_state or "awaiting-callback",
        )
        result.append(
            run_worker(
                launch.spec_path,
                poll_seconds=0.02,
                checkpoint_probe=lambda _surface, _runtime: "checkpoint-1",
                cmux_adapter=cmux,
                review_launcher=review_launcher,
                verification_runner=verification_runner,
                wake_source=wake_source or FallbackWakeSource(),
                monotonic_clock=monotonic_clock,
            )
        )
    if original_summary_barrier_inspection is not None:
        RuntimeWorkerExecution.inspect_task_summary = (
            original_summary_barrier_inspection
        )
    if original_inspect_task_summary is not None:
        RuntimeWorkerExecution.inspect_task_summary = (
            original_inspect_task_summary
        )
    if original_accept_fix_callback is not None:
        RuntimeWorkerExecution.accept_fix_callback = (
            original_accept_fix_callback
        )
    if atomic_publication_barrier:
        check(
            "task-summary watcher never observes partial synthetic JSON",
            atomic_publication_evidence.get("summary_absent") is True
            and atomic_publication_evidence.get("temporary_is_local") is True
            and atomic_publication_evidence.get("temporary_is_partial") is True
            and atomic_publication_evidence.get("operation_state")
            == "awaiting-callback"
            and atomic_publication_evidence.get("callback_error_absent") is True,
            atomic_publication_evidence,
        )
    if phase_callback_publication_barrier_step:
        check(
            "phase callback watcher never observes partial synthetic JSON",
            phase_callback_publication_evidence.get("callback_absent") is True
            and phase_callback_publication_evidence.get("temporary_is_local")
            is True
            and phase_callback_publication_evidence.get("temporary_is_partial")
            is True
            and phase_callback_publication_evidence.get("operation_state")
            == "awaiting-callback"
            and phase_callback_publication_evidence.get(
                "callback_error_absent"
            )
            is True,
            phase_callback_publication_evidence,
        )
    return store, cmux, launch.spec_path.parent, result[-1]


def assert_orphaned_predecessor_lineage_stays_attention(root: Path) -> None:
    """2.7.4 Slice A (F273.MISSING_PREDECESSOR_FAIL_OPEN): a lost attempt-0
    predecessor record with a pending attempt-1 successor and a durable
    invalidation record is an orphaned lineage.  The real worker must latch
    typed attention with no probe effect, no minting, no receipt, no linking,
    and no review; a repeated wake stays idempotent with zero new effects."""

    from harness.verification_attempt import pipeline_verify_effect_id

    profile = load_profiles(ROOT / "config" / "verification-profiles.toml")[
        "scoped"
    ]
    pipeline = compile_pipeline(
        builtin_definitions()["engineering/change"],
        builtin_registry(),
        capabilities=("route:resolved",),
    )
    summary = {
        "schema_version": 1,
        "type": "session",
        "title": "Runtime Result",
        "session": "executor-session",
        "body": "Bounded completed task.",
    }
    task = "27400000-2740-4274-8274-274000000001"
    recorded: list[tuple[str, ...]] = []
    facts: dict[str, object] = {}

    def probe_runner(argv: list[str], **kwargs: object):
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
        recorded.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    def approve_launcher(vault: Path, worktree: Path) -> None:
        meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        ReviewGateController.skip(
            vault / ".vault-meta" / "harness" / "review-data" / task / task,
            dispatch_operation_id=task,
            owner_id=task,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                meta["review_policy"]["verification_profile_sha256"],
            ),
            product_root=worktree,
        )

    def before_start(
        vault: Path, worktree: Path, state: Path, profile_sha: str
    ) -> None:
        store = OperationStore(vault / ".vault-meta" / "harness")
        parent = store.read("owner-1", task)
        head_b = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        input_b = verification_input_sha256(
            pipeline.definition_sha256, head_b, profile_sha, 1
        )
        spec_b0 = _pipeline_verify_identity(
            parent.spec,
            definition_sha256=pipeline.definition_sha256,
            input_sha256=input_b,
            profile="scoped",
            attempt_index=0,
        )[0]
        spec_b1, lane_b1, run_b1 = _pipeline_verify_identity(
            parent.spec,
            definition_sha256=pipeline.definition_sha256,
            input_sha256=input_b,
            profile="scoped",
            attempt_index=1,
        )
        # The prior handoff's durable evidence survives — the pending
        # attempt-1 successor record and the invalidation binding — while the
        # attempt-0 predecessor record itself was lost.
        store.create(spec_b1, lane_id=lane_b1, run_id=run_b1)
        invalidation = {
            "schema_version": 1,
            "operation_id": spec_b0.operation_id,
            "parent_operation_id": task,
            "profile_sha256": profile_sha,
            "predecessor_attempt_sha256": VerificationAttempt(
                task, "scoped", profile_sha, head_b, 0
            ).sha256,
            "predecessor_effect_id": pipeline_verify_effect_id(input_b, 0),
            "successor_operation_id": spec_b1.operation_id,
            "successor_attempt_sha256": VerificationAttempt(
                task, "scoped", profile_sha, head_b, 1
            ).sha256,
            "successor_effect_id": pipeline_verify_effect_id(input_b, 1),
            "current_head_sha": head_b,
            "status": "invalidated",
        }
        write_json(
            state
            / "pipeline-verification"
            / spec_b0.operation_id
            / "invalidation.json",
            invalidation,
        )
        facts.update(
            head_b=head_b,
            stale_b0=spec_b0.operation_id,
            successor_b1=spec_b1.operation_id,
            invalidation=json.dumps(invalidation, sort_keys=True) + "\n",
        )

    def read_json_if(path: Path) -> object:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    store, _cmux, state, rc = run_case(
        root,
        task,
        summary,
        pipeline_name="engineering/change",
        before_start=before_start,
        verification_runner=probe_runner,
        review_launcher=approve_launcher,
        review_state="missing",
        wake_source=FallbackWakeSource(),
    )
    parent = store.read("owner-1", task)
    children = {
        record.spec.operation_id: record.state
        for record in store.list("owner-1")
        if record.spec.kind == "pipeline-verify"
    }
    stale_receipt = read_json_if(
        state
        / "pipeline-verification"
        / str(facts["stale_b0"])
        / "receipt.json"
    )
    linked = read_json_if(state / "pipeline-step-verify.json")
    latch = read_json_if(state / "callback-error.json")
    invalidation_now = (
        state
        / "pipeline-verification"
        / str(facts["stale_b0"])
        / "invalidation.json"
    ).read_text(encoding="utf-8")
    check(
        "an orphaned predecessor lineage stays typed attention with no probe, "
        "no minting, no receipt, no link, and no review",
        rc == 0
        and parent.state == "attention-required"
        and parent.attention_reason == AttentionReason.ATTENTION_REQUIRED
        and not parent.accepted_callback_id
        and children == {str(facts["successor_b1"]): "created"}
        and recorded == []
        and stale_receipt is None
        and linked is None
        and isinstance(latch, dict)
        and latch.get("status") == "pipeline-verification-orphaned-lineage"
        and invalidation_now == facts["invalidation"],
        (rc, parent, children, recorded, latch),
    )

    # A repeated worker wake over the orphaned lineage stays idempotent:
    # no new verifier, no probe effect, and the same latched attention.
    children_before = dict(children)
    worktree = root / f"worktree-{task}"
    relaunch = ProcessAdapter().prepare_surface_launch(
        argv=(
            str(Path(sys.executable).resolve()),
            "-c",
            "import time; time.sleep(0.15)",
        ),
        cwd=worktree,
        state_root=root / f"state-{task}",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=worktree / ".task-summary.json",
        store_root=store.root,
        owner_id="owner-1",
        operation_id=task,
        run_id=f"run-{task}",
        surface_id=CHILD,
        runtime="codex",
        callback_mode="task-summary",
        task_summary_pointer=worktree / ".task-summary.json",
        origin_surface=ORIGIN,
    )
    rewake_rc = run_worker(
        relaunch.spec_path,
        poll_seconds=0.02,
        checkpoint_probe=lambda _surface, _runtime: "checkpoint-1",
        cmux_adapter=FakeCmux(),
        verification_runner=probe_runner,
        wake_source=FallbackWakeSource(),
    )
    rewake_children = {
        record.spec.operation_id: record.state
        for record in store.list("owner-1")
        if record.spec.kind == "pipeline-verify"
    }
    rewake_parent = store.read("owner-1", task)
    check(
        "a repeated wake over the orphaned lineage stays attention with zero "
        "new effects",
        rewake_rc == 0
        and rewake_children == children_before
        and recorded == []
        and rewake_parent.state == "attention-required",
        (rewake_rc, rewake_children, recorded, rewake_parent),
    )


def assert_clean_commit_race_never_consumes_stale_authority(root: Path) -> None:
    """2.7.4 Slice B (F273.EXACT_HEAD_ACCEPTANCE_RACE): a clean commit that
    moves the product HEAD after the final HEAD observation must never yield
    linked or consumed stale verification authority and must never release a
    review effect.  The receipt stays immutable evidence for its own HEAD;
    the continuation halts to the bounded handoff or typed attention.  An
    exact clean same-HEAD receipt keeps the ordinary path."""

    import harness.dashboard_facade as dashboard_facade
    import harness.runtime_worker_verification as runtime_worker_verification
    from harness.runtime_worker_summary import RuntimeWorkerSummaryMixin
    from harness.runtime_worker_verification import (
        RuntimeWorkerVerificationMixin,
    )

    root = root.resolve()
    profile = load_profiles(ROOT / "config" / "verification-profiles.toml")[
        "scoped"
    ]
    pipeline = compile_pipeline(
        builtin_definitions()["engineering/change"],
        builtin_registry(),
        capabilities=("route:resolved",),
    )
    real_subprocess = subprocess

    class RaceWorker(RuntimeWorkerVerificationMixin, RuntimeWorkerSummaryMixin):
        def __init__(
            self,
            *,
            store: OperationStore,
            operation: object,
            state: Path,
            product: Path,
            runner: object,
        ) -> None:
            self.store = store
            self.operation = operation
            self.spec_path = state / "runtime.json"
            self.spec = {
                "owner_id": "owner-1",
                "operation_id": operation.spec.operation_id,
                "cwd": product,
                "surface_id": "race-surface",
            }
            self.pipeline = pipeline
            self.pipeline_extra_commands = ()
            self.profile = profile
            self._pipeline_name = "engineering/change"
            self.verification_runner = runner
            self.verification_step_schema_version = 1
            self.verification_controller_receipt_path = (
                state / "pipeline-step-verify.json"
            )
            self.verification_head = real_subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=product,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            self.attention_calls: list[tuple[object, ...]] = []
            self._bind_verification_attempt(0)

        def summary_attention(
            self, code: str, reason: object = None, **kwargs: object
        ) -> None:
            self.attention_calls.append((code, reason))

    def race_world(name: str):
        product = root / f"race-product-{name}"
        product.mkdir()
        for argv in (
            ("init", "-b", "main"),
            ("config", "user.email", "race@example.invalid"),
            ("config", "user.name", "Race World"),
        ):
            real_subprocess.run(
                ["git", "-C", str(product), *argv],
                check=True,
                capture_output=True,
            )
        (product / "product.txt").write_text("base\n", encoding="utf-8")
        real_subprocess.run(
            ["git", "-C", str(product), "add", "product.txt"],
            check=True,
            capture_output=True,
        )
        real_subprocess.run(
            ["git", "-C", str(product), "commit", "-m", "base"],
            check=True,
            capture_output=True,
        )
        state = root / f"race-state-{name}"
        state.mkdir()
        store = OperationStore(root / f"race-store-{name}")
        operation_id = f"race-{name}"
        store.create(
            OperationSpec(
                operation_id,
                f"{operation_id}-key",
                "dispatch",
                "owner-1",
                RuntimeRoute("claude", "sonnet", "medium", "executor", "6" * 64),
                "packets/task.json",
                "scoped",
            ),
            lane_id="race-lane",
            run_id=f"run-{operation_id}",
        )
        for step in ("preflight", "starting", "running"):
            store.transition("owner-1", operation_id, step)

        def green_runner(argv: list[str], **kwargs: object):
            if argv == ["git", "rev-parse", "HEAD"]:
                return real_subprocess.run(
                    argv,
                    cwd=kwargs["cwd"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
            return real_subprocess.CompletedProcess(argv, 0, "ok\n", "")

        worker = RaceWorker(
            store=store,
            operation=store.read("owner-1", operation_id),
            state=state,
            product=product,
            runner=green_runner,
        )
        return SimpleNamespace(
            product=product,
            state=state,
            store=store,
            worker=worker,
            head_b=worker.verification_head,
        )

    def land_clean_commit(product: Path, name: str) -> str:
        (product / "product.txt").write_text(name + "\n", encoding="utf-8")
        real_subprocess.run(
            ["git", "-C", str(product), "add", "product.txt"],
            check=True,
            capture_output=True,
        )
        real_subprocess.run(
            ["git", "-C", str(product), "commit", "-m", name],
            check=True,
            capture_output=True,
        )
        return real_subprocess.run(
            ["git", "-C", str(product), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def read_json_if(path: Path) -> object:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    original_dashboard = dashboard_facade.launch_bound_facade_dashboard
    dashboard_facade.launch_bound_facade_dashboard = lambda **kwargs: (
        SimpleNamespace(status="skipped")
    )
    try:
        # Injection window: the clean commit lands immediately after the
        # final untampered in-effect HEAD observation, before receipt
        # persistence and controller linking.
        persist = race_world("persist-window")
        armed = {"armed": True, "head_c": ""}

        class CommitAfterFinalObservation:
            CompletedProcess = real_subprocess.CompletedProcess

            @staticmethod
            def run(argv: list[str], **kwargs: object):
                result = real_subprocess.run(argv, **kwargs)
                if list(argv) == ["git", "rev-parse", "HEAD"] and armed["armed"]:
                    armed["armed"] = False
                    armed["head_c"] = land_clean_commit(
                        persist.product, "moved"
                    )
                return result

        original_subprocess = runtime_worker_verification.subprocess
        runtime_worker_verification.subprocess = CommitAfterFinalObservation
        try:
            persist.worker.run_verification()
        finally:
            runtime_worker_verification.subprocess = original_subprocess
        persist_linked = read_json_if(persist.state / "pipeline-step-verify.json")
        check(
            "a clean commit inside the persistence window never becomes "
            "linked stale authority and halts the continuation",
            bool(armed["head_c"])
            and not (
                isinstance(persist_linked, dict)
                and persist_linked.get("head_sha") == persist.head_b
            )
            and bool(persist.worker.attention_calls),
            (armed, persist_linked, persist.worker.attention_calls),
        )

        # Injection window: crash after receipt persistence but before
        # controller linking; the recovery census runs after a clean commit.
        recovery = race_world("link-recovery-window")
        recovery.worker.run_verification()
        (recovery.state / "pipeline-step-verify.json").unlink()
        land_clean_commit(recovery.product, "recovery-moved")
        recovery.worker.controller_verification_receipt()
        recovery_linked = read_json_if(
            recovery.state / "pipeline-step-verify.json"
        )
        check(
            "link recovery after a clean commit never relinks stale authority",
            not (
                isinstance(recovery_linked, dict)
                and recovery_linked.get("head_sha") == recovery.head_b
            ),
            recovery_linked,
        )

        # Injection window: the clean commit lands after linking but before
        # summary consumption / attention clearance.
        consume = race_world("summary-window")
        consume.worker.run_verification()
        land_clean_commit(consume.product, "summary-moved")
        consumed, _halted = consume.worker.resolve_current_verification(object())
        check(
            "summary consumption after a clean commit refuses stale authority "
            "and halts to typed attention",
            consumed is None and bool(consume.worker.attention_calls),
            (consumed, consume.worker.attention_calls),
        )

        # Tracked-or-untracked dirt at the consumption boundary is the same
        # refusal: stale-or-unattested authority is never consumed.
        dirty = race_world("dirty-consumption")
        dirty.worker.run_verification()
        (dirty.product / "junk.txt").write_text("dirt\n", encoding="utf-8")
        dirty_consumed, _dirty_halted = dirty.worker.resolve_current_verification(
            object()
        )
        check(
            "summary consumption over a dirty tree refuses verification "
            "authority and halts to typed attention",
            dirty_consumed is None and bool(dirty.worker.attention_calls),
            (dirty_consumed, dirty.worker.attention_calls),
        )

        # Injection window: the clean commit lands immediately before the
        # review drive; no provider effect may launch on stale authority.
        drive_state = root / "race-review-drive"
        drive_state.mkdir()
        (drive_state / "vault").mkdir()
        drive_product = root / "race-review-drive-product"
        drive_product.mkdir()
        for argv in (
            ("init", "-b", "main"),
            ("config", "user.email", "race@example.invalid"),
            ("config", "user.name", "Race World"),
        ):
            real_subprocess.run(
                ["git", "-C", str(drive_product), *argv],
                check=True,
                capture_output=True,
            )
        (drive_product / "product.txt").write_text("base\n", encoding="utf-8")
        real_subprocess.run(
            ["git", "-C", str(drive_product), "add", "product.txt"],
            check=True,
            capture_output=True,
        )
        real_subprocess.run(
            ["git", "-C", str(drive_product), "commit", "-m", "base"],
            check=True,
            capture_output=True,
        )
        drive_head_b = real_subprocess.run(
            ["git", "-C", str(drive_product), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        launcher_calls: list[tuple[object, ...]] = []
        drive_calls: list[tuple[object, ...]] = []

        class DriveWorker(RuntimeWorkerReviewBridgeMixin):
            def __init__(self) -> None:
                self.spec_path = drive_state / "runtime.json"
                self.spec = {
                    "operation_id": "race-review-drive",
                    "cwd": drive_product,
                }
                self.pipeline = SimpleNamespace(
                    definition_sha256="6" * 64,
                    definition=SimpleNamespace(
                        steps=(SimpleNamespace(primitive_id="verify"),)
                    ),
                )
                self.trusted_vault = drive_state / "vault"
                self.marker_path = drive_state / "pipeline-review-marker.json"
                self.review = SimpleNamespace(
                    gate_root=drive_state / "gate", status="missing"
                )
                self.review_launcher = lambda vault, cwd: launcher_calls.append(
                    (vault, cwd)
                )
                self.profile = profile
                self.verification_head = drive_head_b
                self.attention_calls: list[tuple[object, ...]] = []

            def summary_attention(
                self, code: str, reason: object = None, **kwargs: object
            ) -> None:
                self.attention_calls.append((code, reason))

            def verification_receipt(self) -> dict[str, object]:
                return {
                    "status": "complete",
                    "head_sha": drive_head_b,
                    "evidence": [{"head_sha": drive_head_b}],
                }

            def _bind_verification_attempt(self, index: int) -> None:
                drive_calls.append(("bind", index))

            def adopt_invalidated_verification_successor(self) -> bool:
                drive_calls.append(("adopt",))
                return True

            def run_verification(self) -> None:
                drive_calls.append(("verify",))

        land_clean_commit(drive_product, "pre-drive-move")
        drive_worker = DriveWorker()
        drive_worker.drive_review()
        drive_marker = read_json_if(drive_state / "pipeline-review-marker.json")
        check(
            "the review drive refuses current-HEAD drift on stale authority "
            "and launches no provider effect",
            launcher_calls == []
            and not (
                isinstance(drive_marker, dict)
                and drive_marker.get("status") == "started"
            ),
            (launcher_calls, drive_marker, drive_calls),
        )

        # Control: an exact clean same-HEAD receipt keeps the ordinary path.
        control = race_world("same-head-control")
        control.worker.run_verification()
        control_consumed, control_halted = (
            control.worker.resolve_current_verification(object())
        )
        control_linked = read_json_if(
            control.state / "pipeline-step-verify.json"
        )
        check(
            "an exact clean same-HEAD receipt keeps the ordinary consumption "
            "path with no attention",
            isinstance(control_consumed, dict)
            and control_consumed.get("head_sha") == control.head_b
            and control_halted is False
            and control.worker.attention_calls == []
            and isinstance(control_linked, dict)
            and control_linked.get("head_sha") == control.head_b,
            (control_consumed, control_halted, control.worker.attention_calls),
        )
    finally:
        dashboard_facade.launch_bound_facade_dashboard = original_dashboard


def assert_resolution_marker_is_wait_only_never_authorization(root: Path) -> None:
    """2.7.4 findings patch (F274.RESOLUTION_DRIFT_BYPASS): an exact valid
    review-resolution notification suppresses only the stale-authority
    attention latch.  It never authorizes linking, consuming, or
    review-releasing stale or dirty verification authority."""

    import harness.dashboard_facade as dashboard_facade
    from harness.runtime_worker_summary import RuntimeWorkerSummaryMixin
    from harness.runtime_worker_verification import (
        RuntimeWorkerVerificationMixin,
    )

    root = root.resolve()
    profile = load_profiles(ROOT / "config" / "verification-profiles.toml")[
        "scoped"
    ]
    pipeline = compile_pipeline(
        builtin_definitions()["engineering/change"],
        builtin_registry(),
        capabilities=("route:resolved",),
    )
    real_subprocess = subprocess

    class MarkerWorker(RuntimeWorkerVerificationMixin, RuntimeWorkerSummaryMixin):
        def __init__(
            self,
            *,
            store: OperationStore,
            operation: object,
            state: Path,
            product: Path,
        ) -> None:
            self.store = store
            self.operation = operation
            self.spec_path = state / "runtime.json"
            self.spec = {
                "owner_id": "owner-1",
                "operation_id": operation.spec.operation_id,
                "cwd": product,
                "surface_id": "marker-surface",
            }
            self.pipeline = pipeline
            self.pipeline_extra_commands = ()
            self.profile = profile
            self._pipeline_name = "engineering/change"
            self.verification_step_schema_version = 1
            self.verification_controller_receipt_path = (
                state / "pipeline-step-verify.json"
            )
            self.verification_head = real_subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=product,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            self.attention_calls: list[tuple[object, ...]] = []
            self._bind_verification_attempt(0)

            def green_runner(argv: list[str], **kwargs: object):
                if argv == ["git", "rev-parse", "HEAD"]:
                    return real_subprocess.run(
                        argv,
                        cwd=kwargs["cwd"],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                return real_subprocess.CompletedProcess(argv, 0, "ok\n", "")

            self.verification_runner = green_runner

        def summary_attention(
            self, code: str, reason: object = None, **kwargs: object
        ) -> None:
            self.attention_calls.append((code, reason))

    def marker_world(name: str):
        product = root / f"marker-product-{name}"
        product.mkdir()
        for argv in (
            ("init", "-b", "main"),
            ("config", "user.email", "marker@example.invalid"),
            ("config", "user.name", "Marker World"),
        ):
            real_subprocess.run(
                ["git", "-C", str(product), *argv],
                check=True,
                capture_output=True,
            )
        (product / "product.txt").write_text("base\n", encoding="utf-8")
        real_subprocess.run(
            ["git", "-C", str(product), "add", "product.txt"],
            check=True,
            capture_output=True,
        )
        real_subprocess.run(
            ["git", "-C", str(product), "commit", "-m", "base"],
            check=True,
            capture_output=True,
        )
        state = root / f"marker-state-{name}"
        state.mkdir()
        store = OperationStore(root / f"marker-store-{name}")
        operation_id = f"marker-{name}"
        store.create(
            OperationSpec(
                operation_id,
                f"{operation_id}-key",
                "dispatch",
                "owner-1",
                RuntimeRoute("claude", "sonnet", "medium", "executor", "6" * 64),
                "packets/task.json",
                "scoped",
            ),
            lane_id="marker-lane",
            run_id=f"run-{operation_id}",
        )
        for step in ("preflight", "starting", "running"):
            store.transition("owner-1", operation_id, step)
        worker = MarkerWorker(
            store=store,
            operation=store.read("owner-1", operation_id),
            state=state,
            product=product,
        )
        return SimpleNamespace(
            product=product,
            state=state,
            store=store,
            worker=worker,
            head_b=worker.verification_head,
        )

    def write_exact_marker(world: SimpleNamespace, reviewed_head: str) -> None:
        write_json(
            world.state / "pipeline-review-resolution-notify.json",
            {
                "schema_version": 1,
                "operation_id": world.worker.spec["operation_id"],
                "packet_sha256": "c" * 64,
                "reviewed_head_sha": reviewed_head,
                "summary_sha256": "d" * 64,
                "status": "sent",
            },
        )

    def land_marker_commit(product: Path) -> None:
        (product / "product.txt").write_text("resolved\n", encoding="utf-8")
        real_subprocess.run(
            ["git", "-C", str(product), "add", "product.txt"],
            check=True,
            capture_output=True,
        )
        real_subprocess.run(
            ["git", "-C", str(product), "commit", "-m", "resolved"],
            check=True,
            capture_output=True,
        )

    def read_json_if(path: Path) -> object:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    original_dashboard = dashboard_facade.launch_bound_facade_dashboard
    dashboard_facade.launch_bound_facade_dashboard = lambda **kwargs: (
        SimpleNamespace(status="skipped")
    )
    try:
        # Summary consumption: the exact valid marker suppresses only the
        # attention latch; the stale receipt is still never consumed.
        consume = marker_world("summary")
        consume.worker.run_verification()
        write_exact_marker(consume, consume.head_b)
        land_marker_commit(consume.product)
        consumed, halted = consume.worker.resolve_current_verification(object())
        check(
            "an exact resolution marker never authorizes stale summary "
            "consumption and stays latch-free",
            consumed is None
            and halted is True
            and consume.worker.attention_calls == [],
            (consumed, halted, consume.worker.attention_calls),
        )

        # Link recovery: the marker never authorizes relinking stale
        # authority either.
        (consume.state / "pipeline-step-verify.json").unlink()
        consume.worker.controller_verification_receipt()
        relinked = read_json_if(consume.state / "pipeline-step-verify.json")
        check(
            "an exact resolution marker never authorizes relinking stale "
            "authority",
            not (
                isinstance(relinked, dict)
                and relinked.get("head_sha") == consume.head_b
            ),
            relinked,
        )

        # Review drive: reviewed HEAD equals current HEAD, so the resolved
        # gate is ready, but same-HEAD dirt stays wait-only with no launch
        # and no rebind side effect.
        drive = marker_world("drive")
        drive.worker.run_verification()
        write_exact_marker(drive, drive.head_b)
        launcher_calls: list[tuple[object, ...]] = []
        drive_calls: list[tuple[object, ...]] = []

        class MarkerDriveWorker(RuntimeWorkerReviewBridgeMixin):
            def __init__(self) -> None:
                self.spec_path = drive.state / "runtime.json"
                self.spec = {
                    "operation_id": drive.worker.spec["operation_id"],
                    "cwd": drive.product,
                }
                self.pipeline = pipeline
                self.trusted_vault = drive.state / "vault"
                self.marker_path = (
                    drive.state / "pipeline-review-marker.json"
                )
                self.review = SimpleNamespace(
                    gate_root=drive.state / "gate", status="missing"
                )
                self.review_launcher = (
                    lambda vault, cwd: launcher_calls.append((vault, cwd))
                )
                self.profile = profile
                self.verification_head = drive.head_b
                self.attention_calls: list[tuple[object, ...]] = []

            def summary_attention(
                self, code: str, reason: object = None, **kwargs: object
            ) -> None:
                self.attention_calls.append((code, reason))

            def verification_receipt(self) -> dict[str, object]:
                return {
                    "status": "complete",
                    "head_sha": drive.head_b,
                    "evidence": [{"head_sha": drive.head_b}],
                }

            def _bind_verification_attempt(self, index: int) -> None:
                drive_calls.append(("bind", index))

            def adopt_invalidated_verification_successor(self) -> bool:
                drive_calls.append(("adopt",))
                return True

            def run_verification(self) -> None:
                drive_calls.append(("verify",))

        (drive.product / "junk.txt").write_text("dirt\n", encoding="utf-8")
        launched = MarkerDriveWorker().drive_review()
        drive_marker = read_json_if(
            drive.state / "pipeline-review-marker.json"
        )
        check(
            "an exact resolution marker never releases a review launch over "
            "same-HEAD dirt, with no rebind side effect",
            launched is False
            and launcher_calls == []
            and drive_calls == []
            and not (
                isinstance(drive_marker, dict)
                and drive_marker.get("status") == "started"
            ),
            (launched, launcher_calls, drive_calls, drive_marker),
        )
    finally:
        dashboard_facade.launch_bound_facade_dashboard = original_dashboard


def assert_post_check_clean_commit_never_launches_review(root: Path) -> None:
    """2.7.5 F274.POST_CHECK_LAUNCH_RACE (review-launch consumer): a clean
    commit landing strictly after the closing candidate read and before the
    review launch must never release a provider effect.  The launch is
    admitted only by the exact durable verification receipt/HEAD pair, which
    the drive publishes for the runner instead of a carried boolean; the
    unchanged exact clean candidate keeps the ordinary launch path."""

    import harness.runtime_worker_review_bridge as bridge_module

    root = root.resolve()
    profile = load_profiles(ROOT / "config" / "verification-profiles.toml")[
        "scoped"
    ]
    real_subprocess = subprocess

    def launch_world(name: str) -> SimpleNamespace:
        product = root / f"launch-product-{name}"
        product.mkdir()
        for argv in (
            ("init", "-b", "main"),
            ("config", "user.email", "launch@example.invalid"),
            ("config", "user.name", "Launch World"),
        ):
            real_subprocess.run(
                ["git", "-C", str(product), *argv],
                check=True,
                capture_output=True,
            )
        (product / "product.txt").write_text("base\n", encoding="utf-8")
        real_subprocess.run(
            ["git", "-C", str(product), "add", "product.txt"],
            check=True,
            capture_output=True,
        )
        real_subprocess.run(
            ["git", "-C", str(product), "commit", "-m", "base"],
            check=True,
            capture_output=True,
        )
        head_b = real_subprocess.run(
            ["git", "-C", str(product), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        state = root / f"launch-state-{name}"
        state.mkdir()
        (state / "vault").mkdir()
        operation_id = f"launch-admission-{name}"
        receipt = {
            "schema_version": 2,
            "operation_id": "verify-launch-op",
            "parent_operation_id": operation_id,
            "lane_id": "verify-launch-lane",
            "run_id": "verify-launch-run",
            "head_sha": head_b,
            "status": "complete",
            "evidence": [{"head_sha": head_b}],
        }
        launcher_calls: list[tuple[object, ...]] = []

        class LaunchWorker(RuntimeWorkerReviewBridgeMixin):
            def __init__(self) -> None:
                self.spec_path = state / "runtime.json"
                self.spec = {"operation_id": operation_id, "cwd": product}
                self.pipeline = SimpleNamespace(
                    definition_sha256="6" * 64,
                    definition=SimpleNamespace(
                        steps=(SimpleNamespace(primitive_id="verify"),)
                    ),
                )
                self.trusted_vault = state / "vault"
                self.marker_path = state / "pipeline-review-marker.json"
                self.review = SimpleNamespace(
                    gate_root=state / "gate", status="missing"
                )
                self.review_launcher = (
                    lambda vault, cwd: launcher_calls.append((vault, cwd))
                )
                self.profile = profile
                self.verification_head = head_b
                self.attention_calls: list[tuple[object, ...]] = []

            def summary_attention(
                self, code: str, reason: object = None, **kwargs: object
            ) -> None:
                self.attention_calls.append((code, reason))

            def verification_receipt(self) -> dict[str, object]:
                return dict(receipt)

            def _bind_verification_attempt(self, index: int) -> None:
                pass

            def adopt_invalidated_verification_successor(self) -> bool:
                return True

            def run_verification(self) -> None:
                pass

        return SimpleNamespace(
            product=product,
            state=state,
            head_b=head_b,
            receipt=receipt,
            launcher_calls=launcher_calls,
            worker=LaunchWorker(),
            admission_path=(
                state
                / "vault"
                / ".vault-meta"
                / "harness"
                / "review-runtime"
                / operation_id
                / "review-launch-admission.json"
            ),
        )

    def land_clean_commit(product: Path, name: str) -> str:
        (product / "product.txt").write_text(name + "\n", encoding="utf-8")
        real_subprocess.run(
            ["git", "-C", str(product), "add", "product.txt"],
            check=True,
            capture_output=True,
        )
        real_subprocess.run(
            ["git", "-C", str(product), "commit", "-m", name],
            check=True,
            capture_output=True,
        )
        return real_subprocess.run(
            ["git", "-C", str(product), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def read_json_if(path: Path) -> object:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # Injection window: the clean commit lands strictly after the closing
    # candidate read of the drive's currency check and before the launch.
    raced = launch_world("raced")
    armed = {"armed": True}
    real_candidate_is_current = bridge_module._verification_candidate_is_current

    def commit_after_closing_candidate_read(
        cwd: Path, expected_head_sha: str
    ) -> bool:
        result = real_candidate_is_current(cwd, expected_head_sha)
        if result and armed["armed"]:
            armed["armed"] = False
            land_clean_commit(raced.product, "post-check-move")
        return result

    bridge_module._verification_candidate_is_current = (
        commit_after_closing_candidate_read
    )
    try:
        raced_launched = raced.worker.drive_review()
    finally:
        bridge_module._verification_candidate_is_current = (
            real_candidate_is_current
        )
    raced_marker = read_json_if(raced.state / "pipeline-review-marker.json")
    raced_admission = read_json_if(raced.admission_path)
    check(
        "a clean commit after the closing candidate read and before the "
        "review launch never releases a provider effect or a stale "
        "launch admission",
        armed["armed"] is False
        and raced_launched is False
        and raced.launcher_calls == []
        and not (
            isinstance(raced_marker, dict)
            and raced_marker.get("status") == "started"
        )
        and not (
            isinstance(raced_admission, dict)
            and raced_admission.get("head_sha") == raced.head_b
            and raced_admission.get("status") == "admitted"
        ),
        (armed, raced_launched, raced.launcher_calls, raced_marker),
    )

    # Control: the unchanged exact clean candidate launches once and binds
    # the launch to the exact receipt/HEAD pair, not a carried boolean.
    control = launch_world("control")
    control_launched = control.worker.drive_review()
    control_marker = read_json_if(
        control.state / "pipeline-review-marker.json"
    )
    control_admission = read_json_if(control.admission_path)
    expected_receipt_sha256 = hashlib.sha256(
        json.dumps(
            control.receipt, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    check(
        "the unchanged exact clean candidate launches once and publishes "
        "the exact receipt/HEAD launch admission for the runner",
        control_launched is True
        and len(control.launcher_calls) == 1
        and isinstance(control_marker, dict)
        and control_marker.get("status") == "started"
        and isinstance(control_admission, dict)
        and control_admission.get("schema_version") == 1
        and control_admission.get("operation_id")
        == control.worker.spec["operation_id"]
        and control_admission.get("verification_operation_id")
        == "verify-launch-op"
        and control_admission.get("verification_lane_id")
        == "verify-launch-lane"
        and control_admission.get("verification_run_id")
        == "verify-launch-run"
        and control_admission.get("receipt_sha256")
        == expected_receipt_sha256
        and control_admission.get("receipt_pointer")
        == str(
            (
                control.state
                / "pipeline-verification"
                / "verify-launch-op"
                / "receipt.json"
            ).resolve()
        )
        and control_admission.get("head_sha") == control.head_b
        and control_admission.get("status") == "admitted",
        (control_launched, control.launcher_calls, control_admission),
    )
    print("OK   post-check clean commit never launches review")


def run_focused_summary_refresh_corridor(root: Path) -> None:
    """Exercise only the canonical-summary refresh race corridor."""

    task = "bcbcbcbc-bcbc-4cbc-8cbc-bcbcbcbcbcbc"
    stale_summary = {
        "schema_version": 2,
        "type": "session",
        "title": "Runtime Result",
        "session": "executor-session",
        "body": "Verification is still pending.",
        "outcome_disposition": "achieved",
        "outcome_evidence_ids": ["runtime-green"],
        "residual_gap_pointers": [],
    }
    refreshed_body = "Exact-HEAD verification completed before review."
    refresh_observed = threading.Event()
    refresh_wake = threading.Event()
    publication_ready = threading.Event()
    publication_release = threading.Event()
    publication_observed = threading.Event()
    helper_threads: list[threading.Thread] = []
    helper_errors: list[BaseException] = []
    reviewed_bodies: list[str] = []

    def observe_wake(_surface: str, key: str, message: str) -> None:
        if key == "Enter" and "Exact-HEAD verification completed" in message:
            refresh_wake.set()

    cmux = FakeCmux(key_observer=observe_wake)

    def pass_verification(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    def refresh_after_verification(
        _vault: Path, worktree: Path, _state: Path, _profile_sha: str
    ) -> None:
        def refresh() -> None:
            if not refresh_wake.wait(timeout=2.0):
                raise AssertionError("summary refresh wake was not delivered")
            current = read_json_eventually(worktree / ".task-summary.json")
            write_json_atomic(
                worktree / ".task-summary.json",
                {**current, "body": refreshed_body},
                publication_barrier=(publication_ready, publication_release),
            )
            refresh_observed.set()

        start_checked_thread(refresh, helper_threads, helper_errors)

    def approve_refreshed_summary(vault: Path, worktree: Path) -> None:
        summary_path = worktree / ".task-summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        reviewed_bodies.append(str(summary.get("body") or ""))
        meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        ReviewGateController.skip(
            vault / ".vault-meta" / "harness" / "review-data" / task / task,
            dispatch_operation_id=task,
            owner_id=task,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                meta["review_policy"]["verification_profile_sha256"],
                hashlib.sha256(summary_path.read_bytes()).hexdigest(),
            ),
            product_root=worktree,
        )

    store, _cmux, state, rc = run_case(
        root,
        task,
        stale_summary,
        pipeline_name="engineering/change",
        verification_runner=pass_verification,
        review_state="missing",
        review_launcher=approve_refreshed_summary,
        before_start=refresh_after_verification,
        task_version=4,
        cmux=cmux,
        summary_publication_barrier=(
            publication_ready,
            publication_release,
            publication_observed,
        ),
    )
    for thread in helper_threads:
        thread.join(timeout=3)
    record = store.read("owner-1", task)
    marker = state / "pipeline-summary-refresh-notify.json"
    check(
        "focused canonical summary refresh is atomic and review-current",
        rc == 0
        and refresh_observed.is_set()
        and publication_observed.is_set()
        and all(not thread.is_alive() for thread in helper_threads)
        and not helper_errors
        and marker.is_file()
        and reviewed_bodies == [refreshed_body]
        and record.state == "finalizing"
        and record.accepted_callback_kind == "wiki-summary",
        (rc, helper_errors, reviewed_bodies, record),
    )


def run_focused_baseline_gap_corridor(root: Path) -> None:
    """Exercise only the baseline-gap authority refresh race corridor."""

    task = "ccdddddd-dddd-4ddd-8ddd-dddddddddddd"
    summary = {
        "schema_version": 2,
        "type": "session",
        "title": "Runtime Result",
        "session": "executor-session",
        "body": "The declared runtime evidence is established.",
        "outcome_disposition": "achieved",
        "outcome_evidence_ids": ["runtime-green"],
        "residual_gap_pointers": [],
    }
    authorized = threading.Event()
    refresh_wake = threading.Event()
    launches: list[dict[str, object]] = []
    helper_threads: list[threading.Thread] = []
    helper_errors: list[BaseException] = []

    def fail_verification(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
        return subprocess.CompletedProcess(argv, 1, "", "failed\n")

    def observe_wake(_surface: str, key: str, message: str) -> None:
        if key == "Enter" and "Exact-HEAD verification completed" in message:
            refresh_wake.set()

    cmux = FakeCmux(key_observer=observe_wake)

    def authorize_gap(
        _vault: Path,
        worktree: Path,
        _state: Path,
        _profile_sha: str,
    ) -> None:
        def authorize() -> None:
            packet = read_json_eventually(
                worktree / ".task-verification.json", timeout=3
            )
            failed_receipt = json.loads(
                Path(str(packet["receipt_pointer"])).read_text(encoding="utf-8")
            )
            receipt_sha256 = canonical_sha256(failed_receipt)
            command_ids = tuple(
                str(row["command_id"]) for row in packet["evidence"]
            )
            attempt = VerificationAttempt.from_dict(
                packet["verification_attempt"]
            )
            escalation = build_verification_gap_escalation(
                attempt,
                str(packet["verification_operation_id"]),
                failed_receipt_sha256=receipt_sha256,
                command_ids=command_ids,
                origin_session="coordinator-session",
            )
            raised = append_raise(
                worktree,
                {
                    "version": 1,
                    "id": f"baseline-gap-{task[:8]}",
                    "status": "pending",
                    "task_name": "baseline gap continuation runtime",
                    "category": "pipeline-decision",
                    "reason": "isolated unrelated baseline verification gap",
                    "question": "Continue review with the failed receipt?",
                    "worktree": str(worktree.resolve()),
                    "task_surface": CHILD,
                    "raised_at": "2026-08-17T12:00:00Z",
                    "verification_escalation": escalation,
                    "allowed_decisions": [
                        "continue-unrelated-baseline-gap",
                        "stop",
                    ],
                },
            )
            resolution = resolve_verification_escalation(
                escalation,
                decision="continue-unrelated-baseline-gap",
                evidence_note="The failed command is an unrelated baseline gap.",
            )
            resolved = append_resolution(
                worktree,
                "continue-unrelated-baseline-gap",
                verification_resolution=resolution,
                resolved_at="2026-08-17T12:01:00Z",
            )
            replayed = append_resolution(
                worktree,
                "continue-unrelated-baseline-gap",
                verification_resolution=resolution,
                resolved_at="2026-08-17T12:01:00Z",
            )
            if not refresh_wake.wait(timeout=3.0):
                raise AssertionError("baseline-gap refresh wake was not delivered")
            current = json.loads(
                (worktree / ".task-summary.json").read_text(encoding="utf-8")
            )
            current["body"] = (
                str(current["body"])
                + "\n\nExact failed receipt preserved as an admitted baseline gap."
            )
            write_json_atomic(worktree / ".task-summary.json", current)
            if raised.record_type != "raise" or resolved.record_id != replayed.record_id:
                raise AssertionError("gap authority chain did not replay exactly")
            authorized.set()

        start_checked_thread(authorize, helper_threads, helper_errors)

    def consume_and_approve(vault: Path, worktree: Path) -> None:
        meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        runtime = vault / ".vault-meta" / "harness" / "review-runtime" / task
        context = ReviewContext(
            "packets/task/manifest.json",
            head,
            "scoped",
            meta["review_policy"]["verification_profile_sha256"],
        )
        _admitted_review_launch(meta, vault, runtime, worktree, task, context)
        launches.append(
            json.loads(
                (runtime / "review-launch-admission.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        gate = vault / ".vault-meta" / "harness" / "review-data" / task / task
        (gate / "review-gate.json").unlink(missing_ok=True)
        ReviewGateController.skip(
            gate,
            dispatch_operation_id=task,
            owner_id=task,
            preset=ReviewPreset.from_flags(no_review=True),
            context=context,
            product_root=worktree,
        )

    store, _cmux, state, rc = run_case(
        root,
        task,
        summary,
        review_state="missing",
        pipeline_name="engineering/change",
        verification_runner=fail_verification,
        before_start=authorize_gap,
        review_launcher=consume_and_approve,
        restart_after_attention=True,
        task_version=4,
        cmux=cmux,
    )
    for thread in helper_threads:
        thread.join(timeout=1)
    record = store.read("owner-1", task)
    receipt = json.loads(
        next((state / "pipeline-verification").glob("*/receipt.json")).read_text(
            encoding="utf-8"
        )
    )
    authority = json.loads(
        (state / "pipeline-verification-gap-authority.json").read_text(
            encoding="utf-8"
        )
    )
    refresh = json.loads(
        (state / "pipeline-summary-refresh-notify.json").read_text(
            encoding="utf-8"
        )
    )
    check(
        "focused baseline-gap authority refresh is exact and replay-safe",
        rc == 0
        and authorized.is_set()
        and all(not thread.is_alive() for thread in helper_threads)
        and not helper_errors
        and record.state == "finalizing"
        and receipt["status"] == "failed"
        and authority["failed_receipt_sha256"] == canonical_sha256(receipt)
        and refresh["status"] == "accepted"
        and len(launches) == 1
        and launches[0]["status"] == "admitted-with-gap"
        and launches[0]["decision_record_id"] == authority["decision_record_id"],
        (rc, record, receipt, authority, refresh, launches, helper_errors),
    )


def run_focused_autonomous_limit_corridor(
    root: Path, summary: dict[str, object]
) -> None:
    """Exercise final-pass provider release only after durable exhaustion."""

    task = "ebeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"

    def fail_verification(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
        return subprocess.CompletedProcess(argv, 1, "", "failed\n")

    store, _cmux, state, rc = run_case(
        root,
        task,
        summary,
        pipeline_name="engineering/fix",
        fix_retry_passes=3,
        completion_policy="autonomous",
        total_pass_limit=3,
        verification_runner=fail_verification,
    )
    parent = store.read("owner-1", task)
    terminal_exhausted = read_json_eventually(
        state / "pipeline-fix" / "terminal-exhausted.json"
    )
    check(
        "focused autonomous retry releases after durable exhaustion",
        rc == 0
        and parent.state == "failed"
        and terminal_exhausted["status"] == "retry-exhausted"
        and terminal_exhausted["total_pass_limit"] == 3
        and not (state / "callback-error.json").exists(),
        (parent, terminal_exhausted),
    )


if sys.argv[1:] == ["--focus-concurrency"]:
    with tempfile.TemporaryDirectory(prefix="runtime-task-summary-focus.") as raw:
        focused_root = Path(raw)
        run_focused_summary_refresh_corridor(focused_root)
        run_focused_baseline_gap_corridor(focused_root)
        run_focused_autonomous_limit_corridor(
            focused_root,
            {
                "schema_version": 2,
                "type": "session",
                "title": "Runtime Result",
                "session": "executor-session",
                "body": "The declared runtime evidence is established.",
                "outcome_disposition": "achieved",
                "outcome_evidence_ids": ["runtime-green"],
                "residual_gap_pointers": [],
            },
        )
    raise SystemExit(0)
if sys.argv[1:]:
    raise SystemExit("usage: test_runtime_task_summary.py [--focus-concurrency]")


with tempfile.TemporaryDirectory(prefix="runtime-task-summary.") as raw:
    root = Path(raw)
    assert_review_drive_failure_receipt_is_content_free()
    assert_review_drive_failure_receipts_are_cycle_scoped(root)
    assert_summary_refresh_notification_replays_without_effect(root)
    assert_review_resolution_notification_crashes_fail_closed(root)
    assert_malformed_review_resolution_self_heals_boundedly(root)
    assert_resolution_correction_crash_resumes_once(root)
    assert_resolved_changed_head_gates_review_on_exact_head_receipt(root)
    assert_rejected_drive_with_live_review_stays_waiting(root)
    assert_durable_review_packet_generation_can_advance(root)
    assert_resolution_head_drift_wakes_once(root)
    assert_invalidated_verification_hands_off_to_exact_head_replacement(root)
    assert_orphaned_predecessor_lineage_stays_attention(root)
    assert_clean_commit_race_never_consumes_stale_authority(root)
    assert_resolution_marker_is_wait_only_never_authorization(root)
    assert_post_check_clean_commit_never_launches_review(root)
    handoff = root / "resolution-handoff"
    handoff.mkdir()
    reviewed_head = "a" * 40
    resolved_head = "b" * 40
    handoff_review_operation = "review-operation-current"
    handoff_callbacks = [
        {
            "axis": "openai-holistic",
            "round_operation_id": "round-operation-current",
            "round_run_id": "round-run-current",
            "callback_id": "callback-current",
            "callback_sha256": "c" * 64,
        }
    ]
    handoff_identity = review_transport_identity_sha256(
        handoff_review_operation, handoff_callbacks
    )
    handoff_gate = {
        "active_review_operation_id": handoff_review_operation,
        "awaiting_resolution": {
            "openai-holistic": {
                "reviewed_head_sha": reviewed_head,
                "material_finding_ids": ["F-material"],
                "review_operation_id": handoff_review_operation,
                "round_operation_id": "round-operation-current",
                "round_run_id": "round-run-current",
                "callback_id": "callback-current",
                "callback_sha256": "c" * 64,
            }
        }
    }
    write_json(
        handoff / ".task-review-resolution.json",
        {
            "schema_version": 1,
            "operation_id": TASK,
            "review_identity_sha256": handoff_identity,
            "reviewed_head_sha": reviewed_head,
            "resolved_head_sha": "",
            "resolutions": [
                {
                    "finding_id": "F-material",
                    "disposition": "",
                    "rationale": "",
                    "follow_up": "",
                }
            ],
        },
    )
    check(
        "automatic review drive waits for the complete resolution handoff",
        not _review_resolution_handoff_ready(
            worktree=handoff,
            operation_id=TASK,
            gate_state=handoff_gate,
            current_head=resolved_head,
        ),
    )
    write_json(
        handoff / ".task-review-resolution.json",
        {
            "schema_version": 1,
            "operation_id": TASK,
            "review_identity_sha256": handoff_identity,
            "reviewed_head_sha": reviewed_head,
            "resolved_head_sha": resolved_head,
            "resolutions": [
                {
                    "finding_id": "F-material",
                    "disposition": "applied",
                    "rationale": "The bounded correction is committed.",
                    "follow_up": "",
                }
            ],
        },
    )
    check(
        "automatic review drive resumes after the exact durable handoff",
        _review_resolution_handoff_ready(
            worktree=handoff,
            operation_id=TASK,
            gate_state=handoff_gate,
            current_head=resolved_head,
        ),
    )
    terminal_handoff_gate = {
        "active_review_operation_id": handoff_review_operation,
        "attempt": {
            "schema_version": 1,
            "status": "terminal",
            "terminal": {
                "schema_version": 1,
                "result": "changes-requested",
            },
        },
        "review_notification_evidence": handoff_gate["awaiting_resolution"],
    }
    check(
        "terminal exact review resumes after the durable resolution handoff",
        _review_resolution_handoff_ready(
            worktree=handoff,
            operation_id=TASK,
            gate_state=terminal_handoff_gate,
            current_head=resolved_head,
        ),
    )
    terminal_handoff_gate["attempt"]["terminal"]["result"] = "approved"
    check(
        "terminal non-resolution result cannot consume finding evidence",
        not _review_resolution_handoff_ready(
            worktree=handoff,
            operation_id=TASK,
            gate_state=terminal_handoff_gate,
            current_head=resolved_head,
        ),
    )
    stale_handoff = json.loads(
        (handoff / ".task-review-resolution.json").read_text(
            encoding="utf-8"
        )
    )
    stale_handoff["review_identity_sha256"] = "f" * 64
    write_json(handoff / ".task-review-resolution.json", stale_handoff)
    check(
        "automatic review drive rejects a prior-boundary callback identity",
        not _review_resolution_handoff_ready(
            worktree=handoff,
            operation_id=TASK,
            gate_state=handoff_gate,
            current_head=resolved_head,
        ),
    )
    for material_axis in (
        "anthropic-holistic",
        "openai-holistic",
    ):
        mixed_callbacks = [
            {
                "axis": axis,
                "round_operation_id": f"round-{axis}",
                "round_run_id": f"run-{axis}",
                "callback_id": f"callback-{axis}",
                "callback_sha256": (
                    "d" * 64 if axis == "anthropic-holistic" else "e" * 64
                ),
            }
            for axis in (
                "anthropic-holistic",
                "openai-holistic",
            )
        ]
        mixed_gate = {
            "active_review_operation_id": handoff_review_operation,
            "awaiting_resolution": {
                callback["axis"]: {
                    "reviewed_head_sha": reviewed_head,
                    "material_finding_ids": (
                        ["F-mixed"]
                        if callback["axis"] == material_axis
                        else []
                    ),
                    "review_operation_id": handoff_review_operation,
                    **{
                        key: value
                        for key, value in callback.items()
                        if key != "axis"
                    },
                }
                for callback in mixed_callbacks
            },
        }
        write_json(
            handoff / ".task-review-resolution.json",
            {
                "schema_version": 1,
                "operation_id": TASK,
                "review_identity_sha256": (
                    review_transport_identity_sha256(
                        handoff_review_operation, mixed_callbacks
                    )
                ),
                "reviewed_head_sha": reviewed_head,
                "resolved_head_sha": resolved_head,
                "resolutions": [
                    {
                        "finding_id": "F-mixed",
                        "disposition": "applied",
                        "rationale": "The mixed-axis defect is fixed.",
                        "follow_up": "",
                    }
                ],
            },
        )
        check(
            f"automatic review drive accepts one material {material_axis} axis",
            _review_resolution_handoff_ready(
                worktree=handoff,
                operation_id=TASK,
                gate_state=mixed_gate,
                current_head=resolved_head,
            ),
        )
    valid_summary = {
        "schema_version": 1,
        "type": "session",
        "title": "Runtime Result",
        "session": "executor-session",
        "body": "Bounded completed task.",
    }
    atomic_task = "10101010-1010-4010-8010-101010101010"
    atomic_store, _atomic_cmux, _atomic_state, atomic_rc = run_case(
        root,
        atomic_task,
        valid_summary,
        atomic_publication_barrier=True,
    )
    atomic_record = atomic_store.read("owner-1", atomic_task)
    check(
        "atomic synthetic summary reaches the normal callback boundary",
        atomic_rc == 0
        and atomic_record.state == "finalizing"
        and atomic_record.accepted_callback_kind == "wiki-summary",
        atomic_record,
    )
    store, cmux, state, rc = run_case(root, TASK, valid_summary)
    record = store.read("owner-1", TASK)
    check(
        "valid canonical v3 summary becomes one durable parent callback",
        rc == 0
        and record.state == "finalizing"
        and record.accepted_callback_kind == "wiki-summary"
        and record.accepted_callback_id.startswith("wiki-summary-"),
        record,
    )
    check(
        "accepted receipt wakes only the exact origin with code-owned reap command",
        len(cmux.sent) == 1
        and cmux.sent[0][0] == ORIGIN
        and "Typed final task summary callback was accepted" in cmux.sent[0][1]
        and "scripts/reap-runner.py" in cmux.sent[0][1]
        and str(root / f"vault-{TASK}") in cmux.sent[0][1]
        and str(root / f"worktree-{TASK}") in cmux.sent[0][1]
        and "Bounded completed task" not in cmux.sent[0][1]
        and cmux.keys == [(ORIGIN, "Enter")],
        (cmux.sent, cmux.keys),
    )
    sent_marker = json.loads(
        (state / "task-summary-notify.json").read_text(encoding="utf-8")
    )
    check(
        "notification marker records exact durable callback before send completion",
        sent_marker["status"] == "sent"
        and sent_marker["callback_id"] == record.accepted_callback_id,
        sent_marker,
    )

    valid_v4_summary = {
        "schema_version": 2,
        "type": "session",
        "title": "Runtime Result",
        "session": "executor-session",
        "body": "The declared runtime evidence is established.",
        "outcome_disposition": "achieved",
        "outcome_evidence_ids": ["runtime-green"],
        "residual_gap_pointers": [],
    }
    v4_task = "12121212-1212-4212-8212-121212121212"
    v4_store, _v4_cmux, _v4_state, v4_rc = run_case(
        root,
        v4_task,
        valid_v4_summary,
        task_version=4,
    )
    v4_record = v4_store.read("owner-1", v4_task)
    check(
        "valid canonical v4 summary binds declared outcome evidence",
        v4_rc == 0
        and v4_record.state == "finalizing"
        and v4_record.accepted_callback_kind == "wiki-summary",
        v4_record,
    )
    partial_v4_summary = {
        **valid_v4_summary,
        "outcome_disposition": "partially-achieved",
        "outcome_evidence_ids": [],
        "residual_gap_pointers": ["[[Runtime summary follow-up]]"],
    }
    partial_v4_task = "13131313-1313-4313-8313-131313131313"
    partial_store, _partial_cmux, _partial_state, partial_rc = run_case(
        root,
        partial_v4_task,
        partial_v4_summary,
        task_version=4,
    )
    partial_record = partial_store.read("owner-1", partial_v4_task)
    check(
        "partially-achieved v4 summary remains callback-eligible",
        partial_rc == 0
        and partial_record.state == "finalizing"
        and partial_record.accepted_callback_kind == "wiki-summary",
        partial_record,
    )

    engineering_task = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    verification_calls: list[tuple[str, ...]] = []

    def pass_verification(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
        verification_calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    (
        engineering_store,
        engineering_cmux,
        engineering_state,
        engineering_rc,
    ) = run_case(
        root,
        engineering_task,
        valid_summary,
        pipeline_name="engineering/change",
        verification_runner=pass_verification,
    )
    engineering_record = engineering_store.read(
        "owner-1", engineering_task
    )
    verification_receipt = json.loads(
        (
            engineering_state / "pipeline-step-verify.json"
        ).read_text(encoding="utf-8")
    )
    engineering_operations = engineering_store.list("owner-1")
    engineering_verify = [
        record
        for record in engineering_operations
        if record.spec.kind == "pipeline-verify"
    ]
    check(
        "engineering change runs verification in one derived operation",
        engineering_rc == 0
        and engineering_record.spec.operation_id == engineering_task
        and engineering_record.state == "finalizing"
        and engineering_record.accepted_callback_kind == "wiki-summary"
        and len(engineering_verify) == 1
        and engineering_verify[0].spec.operation_id
        == verification_receipt["operation_id"]
        and engineering_verify[0].spec.operation_id != engineering_task
        and engineering_verify[0].lane_id
        == verification_receipt["lane_id"]
        and engineering_verify[0].run_id
        == verification_receipt["run_id"]
        and engineering_verify[0].state == "complete"
        and verification_receipt["parent_operation_id"]
        == engineering_task
        and verification_receipt["status"] == "complete"
        and verification_receipt["step_id"] == "verify"
        and len(verification_receipt["evidence"]) == 3
        and verification_calls
        == [
            ("make", "test-harness"),
            ("make", "test-model-routing"),
            ("git", "diff", "--check"),
        ]
        and len(engineering_cmux.sent) == 1,
        (
            engineering_record,
            engineering_verify,
            verification_receipt,
            verification_calls,
        ),
    )

    summary_currency_task = "bcbcbcbc-bcbc-4cbc-8cbc-bcbcbcbcbcbc"
    stale_summary = {
        **valid_v4_summary,
        "body": "Verification is still pending.",
    }
    refreshed_body = "Exact-HEAD verification completed before review."
    refresh_observed = threading.Event()
    refresh_wake = threading.Event()
    refresh_publication_ready = threading.Event()
    refresh_publication_release = threading.Event()
    refresh_publication_observed = threading.Event()
    refresh_threads: list[threading.Thread] = []
    refresh_errors: list[BaseException] = []
    reviewed_bodies: list[str] = []

    def observe_refresh_wake(_surface: str, key: str, message: str) -> None:
        if key == "Enter" and "Exact-HEAD verification completed" in message:
            refresh_wake.set()

    summary_currency_cmux = FakeCmux(key_observer=observe_refresh_wake)

    def refresh_after_verification(
        _vault: Path, worktree: Path, state: Path, _profile_sha: str
    ) -> None:
        def refresh() -> None:
            if not refresh_wake.wait(timeout=2.0):
                raise AssertionError("summary refresh wake was not delivered")
            current = read_json_eventually(worktree / ".task-summary.json")
            write_json_atomic(
                worktree / ".task-summary.json",
                {**current, "body": refreshed_body},
                publication_barrier=(
                    refresh_publication_ready,
                    refresh_publication_release,
                ),
            )
            refresh_observed.set()

        start_checked_thread(refresh, refresh_threads, refresh_errors)

    def approve_refreshed_summary(vault: Path, worktree: Path) -> None:
        summary_path = worktree / ".task-summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        reviewed_bodies.append(str(summary.get("body") or ""))
        meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        ReviewGateController.skip(
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / summary_currency_task
            / summary_currency_task,
            dispatch_operation_id=summary_currency_task,
            owner_id=summary_currency_task,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                meta["review_policy"]["verification_profile_sha256"],
                hashlib.sha256(summary_path.read_bytes()).hexdigest(),
            ),
            product_root=worktree,
        )

    (
        summary_currency_store,
        _summary_currency_cmux,
        summary_currency_state,
        summary_currency_rc,
    ) = run_case(
        root,
        summary_currency_task,
        stale_summary,
        pipeline_name="engineering/change",
        verification_runner=pass_verification,
        review_state="missing",
        review_launcher=approve_refreshed_summary,
        before_start=refresh_after_verification,
        task_version=4,
        cmux=summary_currency_cmux,
        summary_publication_barrier=(
            refresh_publication_ready,
            refresh_publication_release,
            refresh_publication_observed,
        ),
    )
    for thread in refresh_threads:
        thread.join(timeout=3)
    refresh_threads_stopped = all(
        not thread.is_alive() for thread in refresh_threads
    )
    summary_currency_record = summary_currency_store.read(
        "owner-1", summary_currency_task
    )
    refresh_marker = (
        summary_currency_state / "pipeline-summary-refresh-notify.json"
    )
    check(
        "review consumes only a canonical summary refreshed after exact-HEAD verification",
        summary_currency_rc == 0
        and refresh_observed.is_set()
        and refresh_publication_observed.is_set()
        and refresh_threads_stopped
        and not refresh_errors
        and refresh_marker.is_file()
        and reviewed_bodies == [refreshed_body]
        and summary_currency_record.state == "finalizing"
        and summary_currency_record.accepted_callback_kind == "wiki-summary",
        (
            summary_currency_rc,
            refresh_observed.is_set(),
            refresh_publication_observed.is_set(),
            refresh_threads_stopped,
            refresh_errors,
            reviewed_bodies,
            summary_currency_record,
        ),
    )

    fix_task = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    fix_store, fix_cmux, fix_state, fix_rc = run_case(
        root,
        fix_task,
        valid_summary,
        pipeline_name="engineering/fix",
        verification_runner=pass_verification,
    )
    fix_record = fix_store.read("owner-1", fix_task)
    fix_receipts = sorted(
        (fix_state / "pipeline-fix" / "pass-0").glob("*/receipt.json")
    )
    fix_children = [
        record
        for record in fix_store.list("owner-1")
        if record.spec.kind == "pipeline-model-step"
    ]
    fix_target = json.loads(
        (fix_state / "callback-target.json").read_text(encoding="utf-8")
    )
    check(
        "engineering fix multiplexes four typed children in one persistent session",
        fix_rc == 0
        and fix_record.state == "finalizing"
        and fix_record.accepted_callback_kind == "wiki-summary"
        and len(fix_receipts) == 4
        and {
            json.loads(path.read_text(encoding="utf-8"))["step_id"]
            for path in fix_receipts
        }
        == {
            "reproduce",
            "root-cause",
            "regression-test",
            "minimal-fix",
        }
        and len(fix_children) == 4
        and all(record.state == "complete" for record in fix_children)
        and fix_target["operation_id"] == fix_task
        and fix_target["run_id"] == f"run-{fix_task}"
        and fix_target["callback_pointer"] == ".task-summary.json"
        and fix_target["generation"] == 6
        and len(
            [
                item
                for item in fix_cmux.sent
                if ".task-pipeline-step-request.json" in item[1]
            ]
        )
        == 4
        and all(
            '"schema_version":1,"status":"complete"' in item[1]
            and '"output_sha256":"<sha256-of-evidence>"' in item[1]
            and '"head_sha":"<current-git-head>"' in item[1]
            for item in fix_cmux.sent
            if ".task-pipeline-step-request.json" in item[1]
        )
        and len(
            [
                item
                for item in fix_cmux.sent
                if "All four typed engineering/fix phase receipts are accepted"
                in item[1]
            ]
        )
        == 1,
        (
            fix_record,
            fix_children,
            fix_receipts,
            fix_target,
            fix_cmux.sent,
        ),
    )
    phase_timing = fix_state / "pipeline-fix" / "timing" / "pass-0"
    timing_evidence: list[tuple[dict[str, object], dict[str, object]]] = []
    for receipt_path in fix_receipts:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        step_id = str(receipt["step_id"])
        start_path = phase_timing / step_id / "start.json"
        completion_path = phase_timing / step_id / "completion.json"
        timing_evidence.append(
            (
                json.loads(start_path.read_text(encoding="utf-8")),
                json.loads(completion_path.read_text(encoding="utf-8")),
            )
        )
    check(
        "engineering fix persists exact immutable phase intervals bound to accepted receipts",
        len(timing_evidence) == 4
        and all(
            start["schema_version"] == 1
            and completion["schema_version"] == 1
            and start["owner_id"] == "owner-1"
            and completion["owner_id"] == "owner-1"
            and start["parent_operation_id"] == fix_task
            and completion["parent_operation_id"] == fix_task
            and start["operation_id"] == completion["operation_id"]
            and start["run_id"] == completion["run_id"]
            and start["step_id"] == completion["step_id"]
            and start["iteration"] == completion["iteration"] == 0
            and isinstance(start["started_at"], (int, float))
            and isinstance(completion["completed_at"], (int, float))
            and completion["completed_at"] >= start["started_at"]
            and isinstance(completion["receipt_sha256"], str)
            and len(completion["receipt_sha256"]) == 64
            for start, completion in timing_evidence
        ),
        timing_evidence,
    )
    phase_messages = [
        item[1]
        for item in fix_cmux.sent
        if ".task-pipeline-step-request.json" in item[1]
    ]
    check(
        "engineering fix exposes each prior evidence pointer and opaque hash semantics",
        all(
            any(
                f"phase {step}" in message
                and f"Read prior accepted evidence at {pointer}." in message
                and "opaque request bindings, not artifact content hashes" in message
                for message in phase_messages
            )
            for step, pointer in (
                ("root-cause", ".task-pipeline/outputs/pass-0/reproduce.md"),
                ("regression-test", ".task-pipeline/outputs/pass-0/root-cause.md"),
                ("minimal-fix", ".task-pipeline/outputs/pass-0/regression-test.md"),
            )
        ),
        phase_messages,
    )

    adopted_task = "eafeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    adopted_prelaunch: dict[str, object] = {}

    def publish_stable_initial_fix_result(
        vault: Path, worktree: Path, state: Path, _profile_sha: str
    ) -> None:
        shutil.copytree(
            ROOT / "scripts",
            vault / "scripts",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        adopted_store = OperationStore(vault / ".vault-meta" / "harness")
        parent = adopted_store.read("owner-1", adopted_task)
        meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        initial_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        round_ = prepare_next_phase(
            adopted_store,
            parent,
            definition_sha256=parent.spec.contract_sha256,
            approved_plan_sha256=meta["approved_plan_sha256"],
            initial_head_sha=initial_head,
            receipts=(),
            iteration=0,
        )
        request, owner = publish_pipeline_step_contract(
            state_root=state,
            worktree=worktree,
            request=fix_phase_request(round_),
        )
        output = worktree / str(request["output_pointer"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("pre-launched reproduction evidence\n", encoding="utf-8")
        write_json(
            worktree / str(request["result_pointer"]),
            {
                "schema_version": 1,
                "status": "complete",
                "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "head_sha": initial_head,
            },
        )
        adopted_prelaunch.update(
            request=request,
            contract_template_pointer=str(owner.sidecar_path),
            operation_id=round_.spec.operation_id,
        )
        (worktree / ".provider-prelaunched-reproduce").write_text(
            "ready\n", encoding="utf-8"
        )

    (
        adopted_store,
        adopted_cmux,
        adopted_state,
        adopted_rc,
    ) = run_case(
        root,
        adopted_task,
        valid_summary,
        pipeline_name="engineering/fix",
        verification_runner=pass_verification,
        before_start=publish_stable_initial_fix_result,
        fix_restart_after="root-cause",
        model_restart_limit=1,
    )
    adopted_receipts = sorted(
        (adopted_state / "pipeline-fix" / "pass-0").glob("*/receipt.json")
    )
    adopted_log = (
        root
        / f"worktree-{adopted_task}"
        / ".provider-step-log.json"
    )
    adopted_steps = (
        json.loads(adopted_log.read_text(encoding="utf-8"))
        if adopted_log.is_file()
        else []
    )
    adopted_children = [
        record
        for record in adopted_store.list("owner-1")
        if record.spec.kind == "pipeline-model-step"
    ]
    adopted_phase_messages = [
        item[1]
        for item in adopted_cmux.sent
        if ".task-pipeline-step-request.json" in item[1]
    ]
    adopted_submit_failure_path = (
        adopted_state / "pipeline-fix" / "submit-failed.json"
    )
    adopted_submit_failure = (
        json.loads(adopted_submit_failure_path.read_text(encoding="utf-8"))
        if adopted_submit_failure_path.is_file()
        else None
    )
    check(
        "engineering fix adopts the pre-launched reproduce result exactly once",
        adopted_rc == 0
        and adopted_store.read("owner-1", adopted_task).state
        == "finalizing"
        and adopted_store.read("owner-1", adopted_task).model_restarts == 1
        and len(adopted_receipts) == 4
        and len(adopted_children) == 4
        and len(
            [
                record
                for record in adopted_children
                if record.spec.operation_id == adopted_prelaunch["operation_id"]
                and record.state == "complete"
            ]
        )
        == 1
        and len(
            [
                record
                for record in adopted_children
                if record.spec.operation_id != adopted_prelaunch["operation_id"]
                and record.spec.kind == "pipeline-model-step"
                and record.state == "complete"
            ]
        )
        == 3
        and adopted_prelaunch["request"]["result_pointer"]
        == ".task-pipeline/results/pass-0/reproduce.json"
        and adopted_prelaunch["request"]["output_pointer"]
        == ".task-pipeline/outputs/pass-0/reproduce.md"
        and adopted_prelaunch["request"]["contract_template_pointer"]
        == adopted_prelaunch["contract_template_pointer"]
        and [item["step_id"] for item in adopted_steps]
        == ["root-cause", "regression-test", "minimal-fix"]
        and not any("phase reproduce" in message for message in adopted_phase_messages)
        and len(
            [message for message in adopted_phase_messages if "phase root-cause" in message]
        )
        == 1,
        (
            adopted_prelaunch,
            adopted_rc,
            adopted_store.read("owner-1", adopted_task),
            adopted_submit_failure,
            adopted_children,
            adopted_receipts,
            adopted_steps,
            adopted_phase_messages,
        ),
    )

    restart_task = "eadeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    (
        restart_store,
        restart_cmux,
        restart_state,
        restart_rc,
    ) = run_case(
        root,
        restart_task,
        valid_summary,
        pipeline_name="engineering/fix",
        fix_restart_after="root-cause",
        model_restart_limit=1,
        verification_runner=pass_verification,
        phase_callback_publication_barrier_step="regression-test",
    )
    restart_parent = restart_store.read("owner-1", restart_task)
    restart_receipt = json.loads(
        (
            restart_state
            / "pipeline-fix"
            / "provider-restart-1.json"
        ).read_text(encoding="utf-8")
    )
    restart_steps = json.loads(
        (
            root
            / f"worktree-{restart_task}"
            / ".provider-step-log.json"
        ).read_text(encoding="utf-8")
    )
    restart_timing = json.loads(
        (
            restart_state / "pipeline-fix" / "timing-before-restart.json"
        ).read_text(encoding="utf-8")
    )
    restart_timing_root = (
        restart_state / "pipeline-fix" / "timing" / "pass-0" / "root-cause"
    )
    check(
        "engineering fix restarts one provider without replaying an accepted phase",
        restart_rc == 0
        and restart_parent.state == "finalizing"
        and restart_parent.model_restarts == 1
        and restart_parent.resources.surface_id == CHILD
        and restart_parent.resources.process_group
        == restart_receipt["new_process_group"]
        and restart_receipt["status"] == "restarted"
        and restart_receipt["old_process_group"]
        != restart_receipt["new_process_group"]
        and [item["step_id"] for item in restart_steps]
        == [
            "reproduce",
            "root-cause",
            "regression-test",
            "minimal-fix",
        ]
        and len(
            [
                item
                for item in restart_cmux.sent
                if "phase root-cause" in item[1]
            ]
        )
        == 1
        and restart_timing["step_id"] == "root-cause"
        and restart_timing["start"]
        == (restart_timing_root / "start.json").read_text(encoding="utf-8")
        and restart_timing["completion"]
        == (restart_timing_root / "completion.json").read_text(encoding="utf-8"),
        (
            restart_parent,
            restart_receipt,
            restart_steps,
            restart_timing,
            restart_cmux.sent,
        ),
    )

    restart_exhausted_task = "eacdeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    (
        restart_exhausted_store,
        _restart_exhausted_cmux,
        restart_exhausted_state,
        restart_exhausted_rc,
    ) = run_case(
        root,
        restart_exhausted_task,
        valid_summary,
        pipeline_name="engineering/fix",
        fix_restart_after="root-cause",
        model_restart_limit=0,
        verification_runner=pass_verification,
    )
    restart_exhausted_parent = restart_exhausted_store.read(
        "owner-1", restart_exhausted_task
    )
    restart_exhausted = json.loads(
        (
            restart_exhausted_state
            / "pipeline-fix"
            / "provider-restart-exhausted.json"
        ).read_text(encoding="utf-8")
    )
    check(
        "engineering fix stops when provider restart budget is exhausted",
        restart_exhausted_rc == 17
        and restart_exhausted_parent.state == "attention-required"
        and restart_exhausted_parent.attention_reason
        == AttentionReason.RETRY_EXHAUSTED
        and restart_exhausted_parent.model_restarts == 0
        and restart_exhausted["status"] == "retry-exhausted",
        (restart_exhausted_parent, restart_exhausted),
    )

    retry_task = "edeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    retry_verification_pass = [0]

    def fail_once_verification(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
        if argv == ["make", "test-harness"]:
            retry_verification_pass[0] += 1
        return subprocess.CompletedProcess(
            argv,
            1 if retry_verification_pass[0] == 1 else 0,
            "",
            "failed\n" if retry_verification_pass[0] == 1 else "",
        )

    def approve_retry(vault: Path, worktree: Path) -> None:
        retry_meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        retry_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        review_root = (
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / retry_task
            / retry_task
        )
        review_store = OperationStore(vault / ".vault-meta" / "harness")
        runtime = TypedReviewRuntime(review_store, retry_task)
        gate = ReviewGateController(review_root, runtime, review_store)
        review_scratch = review_root / "runtime-scratch"
        review_scratch.mkdir(parents=True, exist_ok=True)
        context = ReviewContext(
            "packets/task/manifest.json",
            retry_head,
            "scoped",
            retry_meta["review_policy"]["verification_profile_sha256"],
            purpose="implementation",
        )
        preset = ReviewPreset.from_flags(model="sol", effort="high")
        policy = preset.request(
            f"{retry_task}-review",
            purpose="implementation",
            selected_provider="openai",
        )
        route = RuntimeRoute(
            "codex",
            "gpt-5.6-sol",
            "high",
            "reviewer-callback",
            "3" * 64,
        )
        run = gate.begin(
            dispatch_operation_id=retry_task,
            request=ReviewOperationRequest(
                policy,
                retry_task,
                route,
                context,
            ),
            origin_surface=ORIGIN,
            cwd=review_scratch,
            product_root=worktree,
            prompt_pointer=".task-prompt.md",
            callback_root="callbacks/review",
        )
        lane = run.execution.lanes[0]
        decision = gate.complete_round(
            run,
            lane,
            run.rounds[lane.axis],
            ReviewResult(lane.axis, "approve"),
        )
        if decision.action != "approved":
            raise AssertionError(
                f"typed retry review did not approve: {decision.action} {gate.read()}"
            )

    retry_store, _retry_cmux, retry_state, retry_rc = run_case(
        root,
        retry_task,
        valid_summary,
        pipeline_name="engineering/fix",
        fix_retry_passes=2,
        fix_retry_summary={
            **valid_summary,
            "body": "Final summary updated after the bounded retry.",
        },
        review_state="missing",
        review_launcher=approve_retry,
        verification_runner=fail_once_verification,
        bind_runtime_resources=True,
        typed_review=True,
        await_final_callback=True,
    )
    retry_parent = retry_store.read("owner-1", retry_task)
    retry_receipts = list(
        (retry_state / "pipeline-fix").glob("pass-*/*/receipt.json")
    )
    retry_verifications = [
        record
        for record in retry_store.list("owner-1")
        if record.spec.kind == "pipeline-verify"
    ]
    retry_intent = read_json_eventually(
        retry_state / "pipeline-fix" / "pass-1" / "retry-intent.json"
    )
    retry_step_rows = [
        (
            path,
            json.loads(path.read_text(encoding="utf-8")),
        )
        for path in sorted(retry_receipts)
    ]
    retry_callback_path = retry_state / "callback-receipt.json"
    if not retry_callback_path.is_file():
        raise AssertionError(
            (
                "typed retry did not publish its final callback",
                retry_rc,
                retry_parent,
                sorted(path.relative_to(retry_state).as_posix() for path in retry_state.rglob("*.json")),
            )
        )
    retry_callback_receipt = read_json_eventually(retry_callback_path)
    review_root = (
        root
        / f"vault-{retry_task}"
        / ".vault-meta"
        / "harness"
        / "review-data"
        / retry_task
        / retry_task
    )
    review_gate = json.loads(
        (review_root / "review-gate.json").read_text(encoding="utf-8")
    )
    review_evidence = review_gate["evidence"]
    review_lane_record = retry_store.read(
        retry_task, review_gate["lanes"][0]["operation_id"]
    )
    checkpoint_path = retry_state / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    final_summary = json.loads(
        (root / f"worktree-{retry_task}" / ".task-summary.json").read_text(
            encoding="utf-8"
        )
    )
    reap_receipt_path = retry_state / "reap-finalize-receipt.json"

    def finalize_retry(record: object) -> dict[str, object]:
        receipt = {
            "schema_version": 1,
            "operation_id": record.spec.operation_id,
            "run_id": record.run_id,
            "summary_sha256": canonical_sha256(final_summary),
            "status": "complete",
        }
        write_json(reap_receipt_path, receipt)
        return receipt

    reap_result = run_reap(
        retry_store,
        owner_id="owner-1",
        operation_id=retry_task,
        summary=final_summary,
        finalize=finalize_retry,
    )
    terminal_resources = retry_store.read(
        "owner-1", retry_task
    ).resources
    terminal_cmux = TerminalCmuxPort(terminal_resources.surface_id)
    terminal_process = TerminalProcessPort(terminal_resources)
    terminal_runtime = RuntimeSessionManager(
        retry_store,
        terminal_cmux,
        terminal_process,
    )
    exit_result = terminal_runtime.request_exit("owner-1", retry_task)
    cleanup_result = terminal_runtime.cleanup("owner-1", retry_task)
    retry_parent = retry_store.read("owner-1", retry_task)
    terminal_cleanup_receipt = {
        "schema_version": 1,
        "operation_id": retry_task,
        "run_id": retry_parent.run_id,
        "exit_action": exit_result.action,
        "cleanup_action": cleanup_result.action,
        "state": retry_parent.state,
        "resources_owned": retry_parent.resources != OwnedResources(),
        "guardian_effects": terminal_process.effects,
        "surface_state": terminal_cmux.state,
    }
    terminal_cleanup_path = retry_state / "terminal-cleanup-receipt.json"
    write_json(terminal_cleanup_path, terminal_cleanup_receipt)
    verification_receipts = []
    for record in retry_verifications:
        projection = {
            "operation_id": record.spec.operation_id,
            "run_id": record.run_id,
            "parent_operation_id": record.spec.parent_operation_id,
            "state": record.state,
            "verification_profile": record.spec.verification_profile,
        }
        verification_receipts.append(
            {**projection, "identity_sha256": canonical_sha256(projection)}
        )
    normalized_effect_trace = {
        "schema_version": 1,
        "pipeline": "engineering/fix",
        "pipeline_definition_sha256": compiled_builtin(
            "engineering/fix"
        ).definition_sha256,
        "parent_operation_id": retry_task,
        "parent_state": retry_parent.state,
        "parent_resources_owned": retry_parent.resources != OwnedResources(),
        "plan_step_receipts": sorted(
            [
                {
                    "operation_id": row["operation_id"],
                    "run_id": row["run_id"],
                    "parent_operation_id": row["parent_operation_id"],
                    "definition_sha256": row["definition_sha256"],
                    "iteration": row["iteration"],
                    "step_id": row["step_id"],
                    "status": row["status"],
                    "input_head_sha": row["input_head_sha"],
                    "head_sha": row["head_sha"],
                    "output_sha256": row["output_sha256"],
                    "receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path, row in retry_step_rows
            ],
            key=lambda row: (row["iteration"], row["step_id"]),
        ),
        "verification_receipts": sorted(
            verification_receipts,
            key=lambda row: row["operation_id"],
        ),
        "review_receipt": {
            "status": review_gate["status"],
            "operation_id": review_evidence["operation_id"],
            "run_id": review_evidence["run_id"],
            "sha256": review_evidence["sha256"],
            "axis": review_gate["lanes"][0]["axis"],
            "lane_operation_id": review_gate["lanes"][0]["operation_id"],
            "lane_run_id": review_gate["lanes"][0]["run_id"],
            "lane_state": review_lane_record.state,
        },
        "checkpoint_receipt": {
            **checkpoint,
            "sha256": hashlib.sha256(checkpoint_path.read_bytes()).hexdigest(),
        },
        "callback_receipt": {
            **retry_callback_receipt,
            "sha256": hashlib.sha256(
                (retry_state / "callback-receipt.json").read_bytes()
            ).hexdigest(),
        },
        "reap_receipt": {
            **json.loads(reap_receipt_path.read_text(encoding="utf-8")),
            "sha256": hashlib.sha256(reap_receipt_path.read_bytes()).hexdigest(),
            "effect_replayed": reap_result.result is None,
        },
        "terminal_cleanup_receipt": {
            **terminal_cleanup_receipt,
            "sha256": hashlib.sha256(
                terminal_cleanup_path.read_bytes()
            ).hexdigest(),
        },
        "accepted_callback_kind": retry_parent.accepted_callback_kind,
        "next_action": (
            "closed"
            if retry_parent.state == "complete"
            and retry_parent.resources == OwnedResources()
            else cleanup_result.action
        ),
    }
    exact_trace_path = retry_state / "engineering-fix-effect-trace.json"
    write_json(exact_trace_path, normalized_effect_trace)
    trace_contract_projection = {
        "schema_version": 1,
        "pipeline": normalized_effect_trace["pipeline"],
        "pipeline_definition_sha256": normalized_effect_trace[
            "pipeline_definition_sha256"
        ],
        "parent_operation_id": normalized_effect_trace[
            "parent_operation_id"
        ],
        "parent_state": normalized_effect_trace["parent_state"],
        "parent_resources_owned": normalized_effect_trace[
            "parent_resources_owned"
        ],
        "plan_step_receipts": [
            {
                "iteration": row["iteration"],
                "step_id": row["step_id"],
                "status": row["status"],
            }
            for row in normalized_effect_trace["plan_step_receipts"]
        ],
        "receipt_manifest": {
            "plan_steps": {
                "count": len(normalized_effect_trace["plan_step_receipts"]),
                "identity_fields": [
                    "operation_id",
                    "run_id",
                    "parent_operation_id",
                    "definition_sha256",
                    "input_head_sha",
                    "head_sha",
                ],
                "digest_fields": ["output_sha256", "receipt_sha256"],
                "all_identity_bound": all(
                    len(row["run_id"]) == 32
                    and len(row["definition_sha256"]) == 64
                    and len(row["output_sha256"]) == 64
                    and len(row["receipt_sha256"]) == 64
                    for row in normalized_effect_trace[
                        "plan_step_receipts"
                    ]
                ),
            },
            "verification": {
                "count": len(verification_receipts),
                "states": sorted(row["state"] for row in verification_receipts),
                "identity_fields": [
                    "operation_id",
                    "run_id",
                    "parent_operation_id",
                    "verification_profile",
                    "identity_sha256",
                ],
                "all_identity_bound": all(
                    len(row["run_id"]) == 32
                    and len(row["identity_sha256"]) == 64
                    for row in verification_receipts
                ),
            },
            "review": {
                "status": normalized_effect_trace["review_receipt"]["status"],
                "axis": normalized_effect_trace["review_receipt"]["axis"],
                "lane_state": normalized_effect_trace["review_receipt"][
                    "lane_state"
                ],
                "identity_fields": [
                    "operation_id",
                    "run_id",
                    "lane_operation_id",
                    "lane_run_id",
                    "sha256",
                ],
                "evidence_digest_bound": len(
                    normalized_effect_trace["review_receipt"]["sha256"]
                )
                == 64,
            },
            "checkpoint": {
                "status": "recorded",
                "identity_fields": [
                    "operation_id",
                    "run_id",
                    "runtime",
                    "checkpoint",
                    "sha256",
                ],
                "digest_bound": len(
                    normalized_effect_trace["checkpoint_receipt"]["sha256"]
                )
                == 64,
            },
            "callback": {
                "status": normalized_effect_trace["callback_receipt"]["status"],
                "kind": retry_parent.accepted_callback_kind,
                "digest_bound": len(
                    normalized_effect_trace["callback_receipt"]["sha256"]
                )
                == 64,
            },
            "reap": {
                "status": normalized_effect_trace["reap_receipt"]["status"],
                "effect_replayed": normalized_effect_trace["reap_receipt"][
                    "effect_replayed"
                ],
                "digest_bound": len(
                    normalized_effect_trace["reap_receipt"]["sha256"]
                )
                == 64,
            },
            "terminal_cleanup": {
                "exit_action": terminal_cleanup_receipt["exit_action"],
                "cleanup_action": terminal_cleanup_receipt["cleanup_action"],
                "state": terminal_cleanup_receipt["state"],
                "resources_owned": terminal_cleanup_receipt[
                    "resources_owned"
                ],
                "guardian_effects": terminal_cleanup_receipt[
                    "guardian_effects"
                ],
                "surface_state": terminal_cleanup_receipt["surface_state"],
                "digest_bound": len(
                    normalized_effect_trace["terminal_cleanup_receipt"][
                        "sha256"
                    ]
                )
                == 64,
            },
        },
        "accepted_callback_kind": normalized_effect_trace[
            "accepted_callback_kind"
        ],
        "next_action": normalized_effect_trace["next_action"],
    }
    trace_contract = json.loads(
        (
            ROOT
            / "docs/acceptance/v2.6.4-harness-control-plane-final.json"
        ).read_text(encoding="utf-8")
    )["engineering_fix_effect_trace"]
    check(
        "engineering fix records one exact two-pass lifecycle trace",
        trace_contract_projection == trace_contract
        and len({row["operation_id"] for _path, row in retry_step_rows}) == 7
        and all(
            row["parent_operation_id"] == retry_task
            and row["definition_sha256"]
            == normalized_effect_trace["pipeline_definition_sha256"]
            and len(row["run_id"]) == 32
            and len(
                hashlib.sha256(
                    json.dumps(
                        row, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest()
            )
            == 64
            for _path, row in retry_step_rows
        )
        and len({record.spec.operation_id for record in retry_verifications})
        == 2
        and review_gate["status"] == "approved"
        and retry_parent.state == "complete"
        and retry_parent.resources == OwnedResources()
        and terminal_process.effects == ["request-exit"],
        trace_contract_projection,
    )
    check(
        "engineering fix retries once from the original reproduction receipt",
        retry_rc == 0
        and retry_parent.state == "complete"
        and retry_parent.accepted_callback_kind == "wiki-summary"
        and len(retry_receipts) == 7
        and len(retry_verifications) == 2
        and sorted(record.state for record in retry_verifications)
        == ["complete", "failed"]
        and retry_intent["iteration"] == 1
        and retry_intent["status"] == "pending"
        and retry_intent["reproduction_receipt_sha256"]
        == hashlib.sha256(
            json.dumps(
                json.loads(
                    (
                        retry_state
                        / "pipeline-fix"
                        / "pass-0"
                        / "reproduce"
                        / "receipt.json"
                    ).read_text(encoding="utf-8")
                ),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        (retry_parent, retry_receipts, retry_verifications, retry_intent),
    )

    null_change_task = "edefeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    null_change_pass = [0]

    def fail_once_null_change_verification(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
        if argv == ["make", "test-harness"]:
            null_change_pass[0] += 1
        return subprocess.CompletedProcess(
            argv,
            1 if null_change_pass[0] == 1 else 0,
            "",
            "failed\n" if null_change_pass[0] == 1 else "",
        )

    null_change_store, null_change_cmux, null_change_state, null_change_rc = (
        run_case(
            root,
            null_change_task,
            valid_summary,
            pipeline_name="engineering/fix",
            fix_retry_passes=2,
            fix_retry_summary={
                **valid_summary,
                "body": "Retry verified the repair without changing the tree.",
            },
            fix_retry_null_change=True,
            verification_runner=fail_once_null_change_verification,
        )
    )
    null_change_parent = null_change_store.read("owner-1", null_change_task)
    null_change_intent = read_json_eventually(
        null_change_state / "pipeline-fix" / "pass-1" / "retry-intent.json"
    )
    null_change_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root / f"worktree-{null_change_task}",
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    null_change_notify = (
        null_change_state / "pipeline-fix" / "pass-1" / "null-change-notify.json"
    )
    null_change_attention = load_attention(root / f"worktree-{null_change_task}")
    null_change_notifications = [
        item
        for item in null_change_cmux.sent
        if item[0] == ORIGIN and "pipeline-decision" in item[1]
    ]
    check(
        "a null-change bounded retry publishes one typed continuation",
        null_change_rc == 0
        and null_change_head == null_change_intent["current_head_sha"]
        and null_change_notify.is_file()
        and json.loads(null_change_notify.read_text(encoding="utf-8"))
        == {
            "schema_version": 1,
            "operation_id": null_change_task,
            "iteration": 1,
            "head_sha": null_change_head,
            "status": "sent",
        }
        and null_change_attention is not None
        and null_change_attention["category"] == "pipeline-decision"
        and null_change_attention["status"] == "pending"
        and null_change_attention["allowed_decisions"]
        == ["stop", "retry-with-scope"]
        and null_change_attention["head_sha"] == null_change_head
        and len(null_change_notifications) == 1
        and "task_escalation.py" in null_change_notifications[0][1]
        and "resolve --worktree" in null_change_notifications[0][1],
        (
            null_change_parent,
            null_change_intent,
            null_change_attention,
            null_change_notifications,
        ),
    )
    check(
        "a null-change retry never completes the fix transport on the same HEAD",
        null_change_parent.state == "attention-required"
        and null_change_parent.attention_reason
        == AttentionReason.ATTENTION_REQUIRED
        and not null_change_parent.accepted_callback_id
        and not (null_change_state / "callback-receipt.json").exists()
        and len(
            [
                record
                for record in null_change_store.list("owner-1")
                if record.spec.kind == "pipeline-verify"
            ]
        )
        == 1,
        null_change_parent,
    )

    attention_limit_task = "eceeeeee-eeee-4eee-8eee-eeeeeeeeeeee"

    def fail_verification(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
        return subprocess.CompletedProcess(argv, 1, "", "failed\n")

    (
        attention_limit_store,
        _attention_limit_cmux,
        attention_limit_state,
        attention_limit_rc,
    ) = run_case(
        root,
        attention_limit_task,
        valid_summary,
        pipeline_name="engineering/fix",
        fix_retry_passes=2,
        completion_policy="attention",
        total_pass_limit=2,
        verification_runner=fail_verification,
    )
    attention_limit_parent = attention_limit_store.read(
        "owner-1", attention_limit_task
    )
    check(
        "attention fix policy defers summary until verification is terminal",
        attention_limit_rc == 0
        and attention_limit_parent.state == "attention-required"
        and attention_limit_parent.attention_reason
        == AttentionReason.RETRY_EXHAUSTED
        and not (
            attention_limit_state
            / "pipeline-fix"
            / "pass-2"
            / "retry-intent.json"
        ).exists()
        and not (attention_limit_state / "callback-error.json").exists(),
        attention_limit_parent,
    )

    run_focused_autonomous_limit_corridor(root, valid_summary)

    cannot_task = "efefefef-efef-4fef-8fef-efefefefefef"
    cannot_store, cannot_cmux, cannot_state, cannot_rc = run_case(
        root,
        cannot_task,
        valid_summary,
        pipeline_name="engineering/fix",
        fix_outcome="cannot-reproduce",
        verification_runner=pass_verification,
    )
    cannot_record = cannot_store.read("owner-1", cannot_task)
    cannot_receipt = json.loads(
        next(
            (cannot_state / "pipeline-fix" / "pass-0").glob(
                "*/receipt.json"
            )
        ).read_text(encoding="utf-8")
    )
    cannot_attention = load_attention(
        root / f"worktree-{cannot_task}"
    )
    cannot_notifications = [
        item
        for item in cannot_cmux.sent
        if item[0] == ORIGIN and "pipeline-decision" in item[1]
    ]
    check(
        "cannot reproduce is a typed durable attention boundary",
        cannot_rc == 0
        and cannot_record.state == "attention-required"
        and cannot_record.attention_reason
        == AttentionReason.ATTENTION_REQUIRED
        and not cannot_record.accepted_callback_id
        and cannot_receipt["step_id"] == "reproduce"
        and cannot_receipt["status"] == "cannot-reproduce"
        and cannot_attention is not None
        and cannot_attention["category"] == "pipeline-decision"
        and cannot_attention["status"] == "pending"
        and cannot_attention["allowed_decisions"]
        == ["stop", "retry-with-fixture"]
        and cannot_attention["receipt_operation_id"]
        == cannot_receipt["operation_id"]
        and len(cannot_notifications) == 1
        and "task_escalation.py" in cannot_notifications[0][1]
        and "resolve --worktree" in cannot_notifications[0][1],
        (
            cannot_record,
            cannot_receipt,
            cannot_attention,
            cannot_notifications,
        ),
    )

    failing_task = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    failing_commands = [0]
    commands_before_resubmission = []
    resubmit_helper_done = threading.Event()
    resubmit_helpers: list[threading.Thread] = []

    def fail_then_pass_verification(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
        failing_commands[0] += 1
        return subprocess.CompletedProcess(
            argv,
            1 if failing_commands[0] == 1 else 0,
            "ok\n" if failing_commands[0] > 1 else "",
            "failed\n" if failing_commands[0] == 1 else "",
        )

    def resubmit_failed_verification(
        _vault: Path,
        worktree: Path,
        _state: Path,
        _profile_sha: str,
    ) -> None:
        def respond() -> None:
            import time

            packet_path = worktree / ".task-verification.json"
            packet = read_json_eventually(packet_path, timeout=3)
            (worktree / "product.txt").write_text(
                "ready\nfixed\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "product.txt"], cwd=worktree, check=True
            )
            subprocess.run(
                ["git", "commit", "-m", "fix verification"],
                cwd=worktree,
                text=True,
                capture_output=True,
                check=True,
            )
            resubmitted_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            (
                _vault
                / ".vault-meta"
                / "harness"
                / "review-data"
                / failing_task
                / failing_task
                / "review-gate.json"
            ).unlink(missing_ok=True)
            time.sleep(0.12)
            commands_before_resubmission.append(failing_commands[0])
            packet_sha256 = hashlib.sha256(
                json.dumps(
                    packet, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            write_json(
                worktree / ".task-verification-response.json",
                {
                    "schema_version": 1,
                    "operation_id": failing_task,
                    "verification_operation_id": packet[
                        "verification_operation_id"
                    ],
                    "failed_head_sha": packet["head_sha"],
                    "packet_sha256": packet_sha256,
                    "response": "fix-and-resubmit",
                    "resubmitted_head_sha": resubmitted_head,
                },
            )
            resubmit_helper_done.set()

        helper = threading.Thread(target=respond)
        resubmit_helpers.append(helper)
        helper.start()

    def approve_resubmitted_verification(
        vault: Path, worktree: Path
    ) -> None:
        gate = (
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / failing_task
            / failing_task
        )
        (gate / "review-gate.json").unlink(missing_ok=True)
        meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        ReviewGateController.skip(
            gate,
            dispatch_operation_id=failing_task,
            owner_id=failing_task,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                meta["review_policy"]["verification_profile_sha256"],
            ),
            product_root=worktree,
        )

    failed_store, failed_cmux, failed_state, failed_rc = run_case(
        root,
        failing_task,
        valid_summary,
        pipeline_name="engineering/change",
        verification_runner=fail_then_pass_verification,
        before_start=resubmit_failed_verification,
        review_launcher=approve_resubmitted_verification,
    )
    if not resubmit_helper_done.wait(timeout=1):
        raise AssertionError("resubmission helper did not publish its response")
    for helper in resubmit_helpers:
        helper.join(timeout=1)
    failed_record = failed_store.read("owner-1", failing_task)
    resubmitted_receipt = read_json_eventually(
        failed_state / "pipeline-step-verify.json"
    )
    failed_packet = json.loads(
        (
            root
            / f"worktree-{failing_task}"
            / ".task-verification.json"
        ).read_text(encoding="utf-8")
    )
    response_receipts = list(
        (failed_state / "pipeline-verification").glob(
            "*/response-receipt.json"
        )
    )
    failed_verifications = [
        record
        for record in failed_store.list("owner-1")
        if record.spec.kind == "pipeline-verify"
    ]
    failed_by_id = {
        record.spec.operation_id: record
        for record in failed_verifications
    }
    failed_attempt = failed_by_id.get(
        str(failed_packet["verification_operation_id"])
    )
    resubmitted_attempt = failed_by_id.get(
        str(resubmitted_receipt["operation_id"])
    )
    check(
        "fix-and-resubmit consumes an identity-bound response and reaches review",
        failed_rc == 0
        and failed_record.state == "finalizing"
        and failed_record.accepted_callback_kind == "wiki-summary"
        and len(failed_verifications) == 2
        and failed_attempt is not None
        and failed_attempt.state == "failed"
        and resubmitted_attempt is not None
        and resubmitted_attempt.state == "complete"
        and failed_attempt.spec.operation_id
        != resubmitted_attempt.spec.operation_id
        and resubmitted_receipt["parent_operation_id"] == failing_task
        and resubmitted_receipt["status"] == "complete"
        and failed_packet["status"] == "attention-required"
        and failed_packet["step_id"] == "verify"
        and failed_packet["safe_boundary"] == "tdd-slices-complete"
        and failed_packet["allowed_responses"]
        == ["fix-and-resubmit", "retry-mechanism-flake", "escalate"]
        and failed_packet["evidence"][0]["command_id"]
        == "scoped-1"
        and failed_packet["response_pointer"]
        == ".task-verification-response.json"
        and len(response_receipts) == 1
        and json.loads(
            response_receipts[0].read_text(encoding="utf-8")
        )["schema_version"]
        == 2
        and json.loads(
            response_receipts[0].read_text(encoding="utf-8")
        )["status"] == "accepted"
        and commands_before_resubmission == [1]
        and failing_commands == [4]
        and failed_cmux.sent
        and failed_cmux.sent[0][0] == CHILD
        and ".task-verification.json" in failed_cmux.sent[0][1]
        and "task_escalation.py raise" in failed_cmux.sent[0][1]
        and "--verification-mechanism-flake" in failed_cmux.sent[0][1]
        and "--decision `retry-mechanism-flake`" not in failed_cmux.sent[0][1],
        (
            failed_record,
            failed_verifications,
            resubmitted_receipt,
            failed_packet,
            response_receipts,
            commands_before_resubmission,
            failed_cmux.sent,
        ),
    )

    gap_task = "ccdddddd-dddd-4ddd-8ddd-dddddddddddd"
    gap_authorized = threading.Event()
    gap_refresh_wake = threading.Event()
    gap_launches: list[dict[str, object]] = []
    gap_threads: list[threading.Thread] = []
    gap_errors: list[BaseException] = []

    def observe_gap_refresh_wake(
        _surface: str, key: str, message: str
    ) -> None:
        if key == "Enter" and "Exact-HEAD verification completed" in message:
            gap_refresh_wake.set()

    gap_cmux = FakeCmux(key_observer=observe_gap_refresh_wake)

    def authorize_baseline_gap(
        _vault: Path,
        worktree: Path,
        _state: Path,
        _profile_sha: str,
    ) -> None:
        def authorize() -> None:
            packet = read_json_eventually(
                worktree / ".task-verification.json", timeout=3
            )
            failed_receipt = json.loads(
                Path(str(packet["receipt_pointer"])).read_text(
                    encoding="utf-8"
                )
            )
            receipt_sha256 = hashlib.sha256(
                json.dumps(
                    failed_receipt,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            command_ids = tuple(
                str(row["command_id"]) for row in packet["evidence"]
            )
            attempt = VerificationAttempt.from_dict(
                packet["verification_attempt"]
            )
            escalation = build_verification_gap_escalation(
                attempt,
                str(packet["verification_operation_id"]),
                failed_receipt_sha256=receipt_sha256,
                command_ids=command_ids,
                origin_session="coordinator-session",
            )
            raised = append_raise(
                worktree,
                {
                    "version": 1,
                    "id": f"baseline-gap-{gap_task[:8]}",
                    "status": "pending",
                    "task_name": "baseline gap continuation runtime",
                    "category": "pipeline-decision",
                    "reason": "isolated unrelated baseline verification gap",
                    "question": "Continue review with the preserved failed receipt?",
                    "worktree": str(worktree.resolve()),
                    "task_surface": CHILD,
                    "raised_at": "2026-08-17T12:00:00Z",
                    "verification_escalation": escalation,
                    "allowed_decisions": [
                        "continue-unrelated-baseline-gap",
                        "stop",
                    ],
                },
            )
            resolution = resolve_verification_escalation(
                escalation,
                decision="continue-unrelated-baseline-gap",
                evidence_note=(
                    "The exact failed command is an unrelated baseline gap."
                ),
            )
            resolved = append_resolution(
                worktree,
                "continue-unrelated-baseline-gap",
                verification_resolution=resolution,
                resolved_at="2026-08-17T12:01:00Z",
            )
            replayed = append_resolution(
                worktree,
                "continue-unrelated-baseline-gap",
                verification_resolution=resolution,
                resolved_at="2026-08-17T12:01:00Z",
            )
            if not gap_refresh_wake.wait(timeout=3.0):
                raise AssertionError("baseline-gap refresh wake was not delivered")
            current = json.loads(
                (worktree / ".task-summary.json").read_text(encoding="utf-8")
            )
            current["body"] = (
                str(current["body"])
                + "\n\nExact failed receipt preserved as an admitted baseline gap."
            )
            write_json_atomic(worktree / ".task-summary.json", current)
            if (
                raised.record_type != "raise"
                or resolved.record_id != replayed.record_id
            ):
                raise AssertionError("gap authority chain did not replay exactly")
            gap_authorized.set()

        start_checked_thread(authorize, gap_threads, gap_errors)

    def consume_and_approve_gap(vault: Path, worktree: Path) -> None:
        meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        review_runtime = (
            vault
            / ".vault-meta"
            / "harness"
            / "review-runtime"
            / gap_task
        )
        context = ReviewContext(
            "packets/task/manifest.json",
            head,
            "scoped",
            meta["review_policy"]["verification_profile_sha256"],
        )
        _admitted_review_launch(
            meta, vault, review_runtime, worktree, gap_task, context
        )
        admission = json.loads(
            (review_runtime / "review-launch-admission.json").read_text(
                encoding="utf-8"
            )
        )
        gap_launches.append(admission)
        gate = (
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / gap_task
            / gap_task
        )
        (gate / "review-gate.json").unlink(missing_ok=True)
        ReviewGateController.skip(
            gate,
            dispatch_operation_id=gap_task,
            owner_id=gap_task,
            preset=ReviewPreset.from_flags(no_review=True),
            context=context,
            product_root=worktree,
        )

    gap_store, _gap_cmux, gap_state, gap_rc = run_case(
        root,
        gap_task,
        valid_v4_summary,
        review_state="missing",
        pipeline_name="engineering/change",
        verification_runner=fail_verification,
        before_start=authorize_baseline_gap,
        review_launcher=consume_and_approve_gap,
        restart_after_attention=True,
        task_version=4,
        cmux=gap_cmux,
    )
    for thread in gap_threads:
        thread.join(timeout=1)
    gap_threads_stopped = all(not thread.is_alive() for thread in gap_threads)
    gap_parent = gap_store.read("owner-1", gap_task)
    gap_receipt = json.loads(
        next(
            (gap_state / "pipeline-verification").glob("*/receipt.json")
        ).read_text(encoding="utf-8")
    )
    gap_authority = json.loads(
        (gap_state / "pipeline-verification-gap-authority.json").read_text(
            encoding="utf-8"
        )
    )
    gap_refresh_path = gap_state / "pipeline-summary-refresh-notify.json"
    if not gap_refresh_path.is_file():
        raise AssertionError(
            (
                "baseline gap restart did not reach summary refresh",
                gap_rc,
                gap_parent,
                sorted(path.name for path in gap_state.iterdir()),
                load_attention(root / f"worktree-{gap_task}"),
            )
        )
    gap_refresh = json.loads(gap_refresh_path.read_text(encoding="utf-8"))
    check(
        "failed receipt, durable decision, restart-safe summary refresh, and "
        "provider admission form one baseline-gap continuation",
        gap_rc == 0
        and gap_authorized.is_set()
        and gap_threads_stopped
        and not gap_errors
        and gap_parent.state == "finalizing"
        and gap_receipt["status"] == "failed"
        and gap_authority["failed_receipt_sha256"]
        == hashlib.sha256(
            json.dumps(
                gap_receipt, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        and gap_refresh["status"] == "accepted"
        and len(gap_launches) == 1
        and gap_launches[0]["status"] == "admitted-with-gap"
        and gap_launches[0]["decision_record_id"]
        == gap_authority["decision_record_id"],
        (
            gap_parent,
            gap_receipt,
            gap_authority,
            gap_refresh,
            gap_launches,
            gap_threads_stopped,
            gap_errors,
        ),
    )
    gap_worktree = root / f"worktree-{gap_task}"
    gap_vault = root / f"vault-{gap_task}"
    gap_meta = json.loads(
        (gap_worktree / ".task-meta.json").read_text(encoding="utf-8")
    )
    gap_runtime = (
        gap_vault
        / ".vault-meta"
        / "harness"
        / "review-runtime"
        / gap_task
    )
    gap_admission_path = gap_runtime / "review-launch-admission.json"
    exact_gap_admission = json.loads(
        gap_admission_path.read_text(encoding="utf-8")
    )
    gap_context = ReviewContext(
        "packets/task/manifest.json",
        str(gap_receipt["head_sha"]),
        "scoped",
        gap_meta["review_policy"]["verification_profile_sha256"],
    )

    def gap_consumer_refuses() -> bool:
        try:
            _admitted_review_launch(
                gap_meta,
                gap_vault,
                gap_runtime,
                gap_worktree,
                gap_task,
                gap_context,
            )
        except TaskReviewError:
            return True
        return False

    admission_drift_refused = []
    for field, value in (
        ("receipt_sha256", "0" * 64),
        ("verification_run_id", "foreign-run"),
        ("decision_record_sha256", "0" * 64),
        ("head_sha", "0" * 40),
    ):
        write_json(gap_admission_path, {**exact_gap_admission, field: value})
        admission_drift_refused.append(gap_consumer_refuses())
    gap_authority_path = gap_state / "pipeline-verification-gap-authority.json"
    authority_drift_refused = []
    for field, value in (
        ("command_ids", ["scoped-2"]),
        ("origin_session", "foreign-session"),
    ):
        drifted_authority = {**gap_authority, field: value}
        write_json(gap_authority_path, drifted_authority)
        write_json(
            gap_admission_path,
            {
                **exact_gap_admission,
                "gap_authority_sha256": hashlib.sha256(
                    json.dumps(
                        drifted_authority,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            },
        )
        authority_drift_refused.append(gap_consumer_refuses())
    write_json(gap_authority_path, gap_authority)
    write_json(gap_admission_path, exact_gap_admission)
    check(
        "baseline-gap provider admission rejects receipt, HEAD, decision, "
        "identity, command, and session drift without replay",
        all(admission_drift_refused)
        and all(authority_drift_refused)
        and len(gap_launches) == 1,
        (admission_drift_refused, authority_drift_refused, gap_launches),
    )

    def same_head_authorizer(
        task_id: str, completed: threading.Event
    ) -> Callable[[Path, Path, Path, str], None]:
        def arrange(
            _vault: Path,
            worktree: Path,
            _state: Path,
            _profile_sha: str,
        ) -> None:
            def authorize() -> None:
                packet = read_json_eventually(
                    worktree / ".task-verification.json", timeout=3
                )
                attempt = VerificationAttempt.from_dict(
                    packet["verification_attempt"]
                )
                escalation_id = f"mechanism-flake-{task_id[:8]}"
                meta = json.loads(
                    (worktree / ".task-meta.json").read_text(encoding="utf-8")
                )
                typed_escalation = build_verification_escalation(
                    attempt, str(packet["verification_operation_id"])
                )
                append_raise(
                    worktree,
                    {
                        "version": 1,
                        "id": escalation_id,
                        "status": "pending",
                        "task_name": "same-head verification runtime",
                        "category": "mechanism-failure",
                        "reason": (
                            "verification-mechanism-flake: exact isolated "
                            "profile passed"
                        ),
                        "question": (
                            "Authorize one exact same-HEAD verification retry?"
                        ),
                        "worktree": str(worktree.resolve()),
                        "task_surface": str(meta["task_surface"]),
                        "raised_at": "2026-08-05T12:00:00Z",
                        "coordinator_policy": (
                            "classify-and-auto-repair-if-eligible"
                        ),
                        "verification_escalation": typed_escalation,
                    },
                )
                append_resolution(
                    worktree,
                    "retry-mechanism-flake",
                    verification_resolution=resolve_verification_escalation(
                        typed_escalation,
                        decision="retry-mechanism-flake",
                        evidence_note=(
                            "Exact isolated verification proved a mechanism flake."
                        ),
                    ),
                    resolved_at="2026-08-05T12:01:00Z",
                )
                # Only the registered code-owned entry may publish the
                # identity-bound same-HEAD response; a hand-written response
                # must never satisfy this scenario.
                verification_resubmit.publish_same_head_response(
                    worktree, escalation_id
                )
                completed.set()

            threading.Thread(target=authorize).start()

        return arrange

    same_head_task = "abaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    same_head_commands: list[tuple[str, ...]] = []
    same_head_first_command = [True]
    same_head_authorized = threading.Event()

    def fail_once_same_head(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
        same_head_commands.append(tuple(argv))
        failed = same_head_first_command[0]
        same_head_first_command[0] = False
        return subprocess.CompletedProcess(
            argv, 1 if failed else 0, "ok\n" if not failed else "", ""
        )

    same_head_store, _same_head_cmux, same_head_state, same_head_rc = run_case(
        root,
        same_head_task,
        valid_summary,
        pipeline_name="engineering/change",
        verification_runner=fail_once_same_head,
        before_start=same_head_authorizer(
            same_head_task, same_head_authorized
        ),
    )
    if not same_head_authorized.wait(timeout=1):
        raise AssertionError("same-HEAD authorization was not published")
    same_head_parent = same_head_store.read("owner-1", same_head_task)
    same_head_children = [
        record
        for record in same_head_store.list("owner-1")
        if record.spec.kind == "pipeline-verify"
    ]
    same_head_receipts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(
            (same_head_state / "pipeline-verification").glob("*/receipt.json")
        )
    ]
    same_head_response_receipts = list(
        (same_head_state / "pipeline-verification").glob(
            "*/response-receipt.json"
        )
    )
    same_head_commit_count = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=root / f"worktree-{same_head_task}",
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    check(
        "one authorized same-HEAD attempt preserves attempt-zero evidence",
        same_head_rc == 0
        and same_head_parent.state == "finalizing"
        and same_head_parent.accepted_callback_kind == "wiki-summary"
        and len(same_head_children) == 2
        and sorted(record.state for record in same_head_children)
        == ["complete", "failed"]
        and {row["head_sha"] for row in same_head_receipts}
        == {same_head_receipts[0]["head_sha"]}
        and sorted(
            row["verification_attempt"]["attempt_index"]
            for row in same_head_receipts
        )
        == [0, 1]
        and len(same_head_response_receipts) == 1
        and json.loads(
            same_head_response_receipts[0].read_text(encoding="utf-8")
        )["schema_version"]
        == 2
        and same_head_commit_count == "1"
        and same_head_commands
        == [
            ("make", "test-harness"),
            ("make", "test-harness"),
            ("make", "test-model-routing"),
            ("git", "diff", "--check"),
        ]
        and all(
            command[0] not in {"codex", "claude"}
            for command in same_head_commands
        ),
        (
            same_head_parent,
            same_head_children,
            same_head_receipts,
            same_head_commands,
            same_head_commit_count,
        ),
    )

    exhausted_task = "acaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    exhausted_authorized = threading.Event()

    def always_fail_same_head(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
        return subprocess.CompletedProcess(argv, 1, "", "failed\n")

    exhausted_store, _exhausted_cmux, exhausted_state, _exhausted_rc = run_case(
        root,
        exhausted_task,
        valid_summary,
        pipeline_name="engineering/change",
        verification_runner=always_fail_same_head,
        before_start=same_head_authorizer(
            exhausted_task, exhausted_authorized
        ),
    )
    if not exhausted_authorized.wait(timeout=1):
        raise AssertionError("exhaustion authorization was not published")
    exhausted_parent = exhausted_store.read("owner-1", exhausted_task)
    exhausted_packet = json.loads(
        (
            root
            / f"worktree-{exhausted_task}"
            / ".task-verification.json"
        ).read_text(encoding="utf-8")
    )
    check(
        "a second same-HEAD retry stops at typed attention",
        exhausted_parent.state == "attention-required"
        and exhausted_parent.attention_reason == AttentionReason.RETRY_EXHAUSTED
        and exhausted_packet["verification_attempt"]["attempt_index"] == 1
        and "retry-mechanism-flake"
        not in exhausted_packet["allowed_responses"]
        and len(
            [
                record
                for record in exhausted_store.list("owner-1")
                if record.spec.kind == "pipeline-verify"
            ]
        )
        == 2,
        (exhausted_parent, exhausted_packet),
    )

    crash_task = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    crash_commands: list[tuple[str, ...]] = []
    crash_commands_before_response: list[int] = []
    recovered_links: list[str] = []
    crash_response_threads: list[threading.Thread] = []
    crash_response_errors: list[str] = []

    def pass_crash_restart_verification(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "rev-parse", "HEAD"]:
            return subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
        crash_commands.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    def prepare_receipt_before_link_crash(
        vault: Path,
        worktree: Path,
        state: Path,
        profile_sha: str,
    ) -> None:
        crash_store = OperationStore(
            vault / ".vault-meta" / "harness"
        )
        parent = crash_store.read("owner-1", crash_task)
        pipeline = compile_pipeline(
            builtin_definitions()["engineering/change"],
            builtin_registry(),
            capabilities=("route:resolved",),
        )
        failed_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        input_sha256 = verification_input_sha256(
            pipeline.definition_sha256,
            failed_head,
            profile_sha,
            1,
        )
        child, lane_id, run_id = _pipeline_verify_identity(
            parent.spec,
            definition_sha256=pipeline.definition_sha256,
            input_sha256=input_sha256,
            profile="scoped",
        )
        check(
            "pipeline verification binds its exact parent operation",
            child.parent_operation_id == parent.spec.operation_id,
        )
        crash_store.create(child, lane_id=lane_id, run_id=run_id)
        child_supervisor = OperationSupervisor(
            crash_store, "owner-1", child.operation_id
        )
        child_supervisor.configure_budget(
            attempt_limit=1,
            model_restart_limit=0,
            time_budget_seconds=DEFAULT_TIME_BUDGET_SECONDS,
            token_limit=DEFAULT_TOKEN_LIMIT,
        )
        for child_state in (
            "preflight",
            "starting",
            "running",
            "verifying",
        ):
            child_supervisor.transition(child_state)
        child_supervisor.consume_attempt()
        effect_id = f"pipeline-verify-{input_sha256[:32]}"
        crash_store.begin_effect(
            "owner-1", child.operation_id, effect_id
        )
        evidence_dir = (
            state
            / "pipeline-verification"
            / child.operation_id
            / "evidence"
        )
        evidence_dir.mkdir(parents=True)
        output = evidence_dir / "scoped-1.log"
        output.write_text("failed before crash\n", encoding="utf-8")
        verification_attempt = VerificationAttempt(
            crash_task, "scoped", profile_sha, failed_head, 0
        )
        child_receipt = {
            "schema_version": 2,
            "operation_id": child.operation_id,
            "parent_operation_id": crash_task,
            "lane_id": lane_id,
            "run_id": run_id,
            "definition_sha256": pipeline.definition_sha256,
            "step_id": "verify",
            "head_sha": failed_head,
            "input_sha256": input_sha256,
            "profile": "scoped",
            "profile_sha256": profile_sha,
            "effect_id": effect_id,
            "status": "failed",
            "verification_attempt": verification_attempt.as_dict(),
            "verification_attempt_sha256": verification_attempt.sha256,
            "evidence": [
                {
                    "profile": "scoped",
                    "profile_sha256": profile_sha,
                    "head_sha": failed_head,
                    "command_id": "scoped-1",
                    "cwd": ".",
                    "exit_code": 1,
                    "started_at": "1",
                    "finished_at": "2",
                    "output_pointer": output.relative_to(state).as_posix(),
                    "output_sha256": hashlib.sha256(
                        output.read_bytes()
                    ).hexdigest(),
                    "output_bytes": len(output.read_bytes()),
                    "schema_version": 2,
                }
            ],
        }
        write_json(
            state
            / "pipeline-verification"
            / child.operation_id
            / "receipt.json",
            child_receipt,
        )
        check(
            "crash fixture stops after child receipt and before controller link",
            not (state / "pipeline-step-verify.json").exists(),
            state,
        )
        (worktree / "product.txt").write_text(
            "ready\nfixed after crash\n", encoding="utf-8"
        )
        subprocess.run(
            ["git", "add", "product.txt"], cwd=worktree, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "fix after verify crash"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        )
        resubmitted_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        (
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / crash_task
            / crash_task
            / "review-gate.json"
        ).unlink(missing_ok=True)

        def respond_after_recovery() -> None:
            packet_path = worktree / ".task-verification.json"
            for _ in range(500):
                if packet_path.is_file():
                    break
                time.sleep(0.02)
            if not packet_path.is_file():
                crash_response_errors.append("verification-packet-timeout")
                return
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            linked = json.loads(
                (state / "pipeline-step-verify.json").read_text(
                    encoding="utf-8"
                )
            )
            recovered_links.append(str(linked["operation_id"]))
            crash_commands_before_response.append(
                len(crash_commands)
            )
            packet_sha256 = hashlib.sha256(
                json.dumps(
                    packet, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            write_json(
                worktree / ".task-verification-response.json",
                {
                    "schema_version": 1,
                    "operation_id": crash_task,
                    "verification_operation_id": child.operation_id,
                    "failed_head_sha": failed_head,
                    "packet_sha256": packet_sha256,
                    "response": "fix-and-resubmit",
                    "resubmitted_head_sha": resubmitted_head,
                },
            )

        response_thread = threading.Thread(target=respond_after_recovery)
        crash_response_threads.append(response_thread)
        response_thread.start()

    def approve_crash_restart(vault: Path, worktree: Path) -> None:
        gate = (
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / crash_task
            / crash_task
        )
        meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        ReviewGateController.skip(
            gate,
            dispatch_operation_id=crash_task,
            owner_id=crash_task,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                meta["review_policy"]["verification_profile_sha256"],
            ),
            product_root=worktree,
        )

    (
        crash_store,
        _crash_cmux,
        crash_state,
        crash_rc,
    ) = run_case(
        root,
        crash_task,
        valid_summary,
        pipeline_name="engineering/change",
        before_start=prepare_receipt_before_link_crash,
        verification_runner=pass_crash_restart_verification,
        review_launcher=approve_crash_restart,
        review_state="missing",
    )
    for response_thread in crash_response_threads:
        response_thread.join(timeout=12)
    crash_parent = crash_store.read("owner-1", crash_task)
    crash_children = [
        record
        for record in crash_store.list("owner-1")
        if record.spec.kind == "pipeline-verify"
    ]
    crash_response_receipts = list(
        (crash_state / "pipeline-verification").glob(
            "*/response-receipt.json"
        )
    )
    check(
        "restart recovers an orphan failed receipt before resubmission",
        crash_rc == 0
        and crash_parent.state == "finalizing"
        and len(crash_children) == 2
        and sorted(record.state for record in crash_children)
        == ["complete", "failed"]
        and recovered_links
        and recovered_links[0]
        != json.loads(
            (crash_state / "pipeline-step-verify.json").read_text(
                encoding="utf-8"
            )
        )["operation_id"]
        and crash_commands_before_response == [0]
        and not crash_response_errors
        and all(not thread.is_alive() for thread in crash_response_threads)
        and crash_commands
        == [
            ("make", "test-harness"),
            ("make", "test-model-routing"),
            ("git", "diff", "--check"),
        ]
        and len(crash_response_receipts) == 1
        and json.loads(
            crash_response_receipts[0].read_text(encoding="utf-8")
        )["schema_version"] == 2,
        (
            crash_parent,
            crash_children,
            recovered_links,
            crash_commands_before_response,
            crash_response_errors,
            crash_commands,
        ),
    )

    # A restarted worker sees a duplicate durable callback and the sent marker;
    # it must not wake the coordinator twice.
    launch = ProcessAdapter().prepare_surface_launch(
        argv=(
            str(Path(sys.executable).resolve()),
            "-c",
            "import time; time.sleep(0.15)",
        ),
        cwd=root / f"worktree-{TASK}",
        state_root=state,
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=root
        / f"worktree-{TASK}"
        / ".task-summary.json",
        store_root=store.root,
        owner_id="owner-1",
        operation_id=TASK,
        run_id=f"run-{TASK}",
        surface_id=CHILD,
        runtime="codex",
        callback_mode="task-summary",
        task_summary_pointer=root
        / f"worktree-{TASK}"
        / ".task-summary.json",
        origin_surface=ORIGIN,
    )
    second_rc = run_worker(
        launch.spec_path,
        poll_seconds=0.02,
        checkpoint_probe=lambda _surface, _runtime: "checkpoint-1",
        cmux_adapter=cmux,
        wake_source=FallbackWakeSource(),
    )
    check(
        "duplicate summary callback never duplicates coordinator notification",
        second_rc == 0 and len(cmux.sent) == 1 and len(cmux.keys) == 1,
        (cmux.sent, cmux.keys),
    )

    invalid = {**valid_summary, "title": "Unapproved title"}
    invalid_store, invalid_cmux, invalid_state, invalid_rc = run_case(
        root, INVALID_TASK, invalid
    )
    invalid_record = invalid_store.read("owner-1", INVALID_TASK)
    check(
        "invalid handoff gets one same-session correction before dead-session attention",
        invalid_rc == 0
        and invalid_record.state == "attention-required"
        and not invalid_record.accepted_callback_id
        and len(invalid_cmux.sent) == 1
        and invalid_cmux.sent[0][0] == CHILD
        and "only same-session correction" in invalid_cmux.sent[0][1]
        and invalid_cmux.keys == [(CHILD, "Enter")]
        and (
            invalid_state
            / "contract-templates"
            / "task-summary"
            / f"{INVALID_TASK}.json"
        ).is_file()
        and not (invalid_state / "task-summary-notify.json").exists(),
        (invalid_record, invalid_cmux.sent, invalid_cmux.keys),
    )

    drift_task = "88888888-8888-4888-8888-888888888888"
    drift_store, drift_cmux, drift_state, drift_rc = run_case(
        root,
        drift_task,
        valid_summary,
        review_state="skipped",
        bind_contract=False,
    )
    drift_record = drift_store.read("owner-1", drift_task)
    check(
        "unbound lifecycle operation stops as typed contract drift",
        drift_rc == 0
        and drift_record.state == "attention-required"
        and drift_record.attention_reason
        == AttentionReason.CONTRACT_DRIFT
        and not drift_record.accepted_callback_id
        and drift_cmux.sent == []
        and json.loads(
            (drift_state / "callback-error.json").read_text(encoding="utf-8")
        )["status"]
        == "pipeline-contract-drift",
        drift_record,
    )

    delayed_task = BLOCKED_TASK
    delayed_store, delayed_cmux, _delayed_state, delayed_rc = run_case(
        root,
        delayed_task,
        valid_summary,
        review_state="delayed-skip",
        review_launcher=lambda _vault, _worktree: None,
    )
    delayed_record = delayed_store.read("owner-1", delayed_task)
    check(
        "worker outlives provider and accepts once typed review state arrives",
        delayed_rc == 0
        and delayed_record.state == "finalizing"
        and delayed_record.accepted_callback_kind == "wiki-summary"
        and len(delayed_cmux.sent) == 1,
        delayed_record,
    )

    automatic_task = "77777777-7777-4777-8777-777777777777"
    automatic_calls: list[str] = []

    def approve_automatically(vault: Path, worktree: Path) -> None:
        automatic_calls.append(str(worktree))
        meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        profile_sha = meta["review_policy"]["verification_profile_sha256"]
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        ReviewGateController.skip(
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / automatic_task
            / automatic_task,
            dispatch_operation_id=automatic_task,
            owner_id=automatic_task,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                profile_sha,
            ),
            product_root=worktree,
        )

    automatic_store, automatic_cmux, automatic_state, automatic_rc = run_case(
        root,
        automatic_task,
        valid_summary,
        review_state="missing",
        review_launcher=approve_automatically,
    )
    automatic_record = automatic_store.read("owner-1", automatic_task)
    check(
        "compiled lifecycle starts the missing review gate without model orchestration",
        automatic_rc == 0
        and len(automatic_calls) == 1
        and automatic_record.state == "finalizing"
        and automatic_record.accepted_callback_kind == "wiki-summary"
        and len(automatic_cmux.sent) == 1,
        (automatic_calls, automatic_record),
    )
    automatic_marker = json.loads(
        (automatic_state / "pipeline-review-start.json").read_text(
            encoding="utf-8"
        )
    )
    check(
        "automatic review launch has one durable local receipt",
        automatic_marker["status"] == "started"
        and automatic_marker["operation_id"] == automatic_task,
        automatic_marker,
    )

    default_resolution_task = "78787878-7878-4787-8787-787878787878"
    default_resolution_calls: list[str] = []

    def resolve_default_review(vault: Path, worktree: Path) -> None:
        default_resolution_calls.append(str(worktree))
        meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        profile_sha = meta["review_policy"][
            "verification_profile_sha256"
        ]
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        gate_root = (
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / default_resolution_task
            / default_resolution_task
        )
        if len(default_resolution_calls) == 1:
            result_pointer = gate_root / "round-openai-holistic-0.json"
            write_json(
                result_pointer,
                {
                    "axis": "openai-holistic",
                    "verdict": "changes-requested",
                    "verification_iteration": 0,
                    "findings": [
                        {
                            "axis": "openai-holistic",
                            "finding_id": "F-default-material",
                            "severity": "important",
                            "file": "product.txt",
                            "line": 1,
                            "summary": "Default pipeline finding",
                            "evidence": "The fixture needs one correction.",
                            "recommendation": "Commit the correction.",
                        }
                    ],
                },
            )
            write_json(
                gate_root / "review-gate.json",
                {
                    "schema_version": 1,
                    "dispatch_operation_id": default_resolution_task,
                    "owner_id": default_resolution_task,
                    "status": "awaiting-resolution",
                    "active_review_operation_id": "review-default",
                    "product_root": str(worktree),
                    "context": {
                        "head_sha": head,
                        "verification_profile": "scoped",
                        "verification_profile_sha256": profile_sha,
                    },
                    "awaiting_resolution": {
                        "openai-holistic": {
                            "pointer": result_pointer.relative_to(
                                gate_root
                            ).as_posix(),
                            "reviewed_head_sha": head,
                            "review_operation_id": "review-default",
                            "round_operation_id": "round-default",
                            "round_run_id": "run-default",
                            "callback_id": "callback-default",
                            "callback_sha256": "f" * 64,
                            "material_finding_ids": [
                                "F-default-material"
                            ],
                        }
                    },
                },
            )

            def publish_default_resolution() -> None:
                import time

                packet_path = worktree / ".task-review.json"
                for _ in range(200):
                    if packet_path.is_file():
                        break
                    time.sleep(0.01)
                else:
                    return
                packet = json.loads(
                    packet_path.read_text(encoding="utf-8")
                )
                (worktree / "product.txt").write_text(
                    "ready\nresolved\n", encoding="utf-8"
                )
                subprocess.run(
                    ["git", "add", "product.txt"],
                    cwd=worktree,
                    check=True,
                )
                subprocess.run(
                    ["git", "commit", "-m", "resolve default review"],
                    cwd=worktree,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                resolved_head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=worktree,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip()
                write_json(
                    worktree / ".task-review-resolution.json",
                    {
                        "schema_version": 1,
                        "operation_id": default_resolution_task,
                        "review_identity_sha256": packet[
                            "review_identity_sha256"
                        ],
                        "reviewed_head_sha": head,
                        "resolved_head_sha": resolved_head,
                        "resolutions": [
                            {
                                "finding_id": "F-default-material",
                                "disposition": "applied",
                                "rationale": "The correction is committed.",
                                "follow_up": "",
                            }
                        ],
                    },
                )
                refresh = (
                    root
                    / f"state-{default_resolution_task}"
                    / "pipeline-summary-refresh-notify.json"
                )
                for _ in range(200):
                    if refresh.is_file():
                        break
                    time.sleep(0.01)
                else:
                    return
                summary_path = worktree / ".task-summary.json"
                refreshed = json.loads(
                    summary_path.read_text(encoding="utf-8")
                )
                refreshed["body"] += " Resolved on the final HEAD."
                write_json(summary_path, refreshed)

            threading.Thread(target=publish_default_resolution).start()
            return
        (gate_root / "review-gate.json").unlink()
        ReviewGateController.skip(
            gate_root,
            dispatch_operation_id=default_resolution_task,
            owner_id=default_resolution_task,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                profile_sha,
            ),
            product_root=worktree,
        )

    eventless_clock = FakeMonotonicClock()
    eventless_wake = EventlessWakeSource(eventless_clock)
    (
        default_resolution_store,
        _default_resolution_cmux,
        _default_resolution_state,
        default_resolution_rc,
    ) = run_case(
        root,
        default_resolution_task,
        valid_summary,
        review_state="missing",
        review_launcher=resolve_default_review,
        pipeline_name="lifecycle/default",
        wake_source=eventless_wake,
        monotonic_clock=eventless_clock,
    )
    default_resolution_record = default_resolution_store.read(
        "owner-1", default_resolution_task
    )
    check(
        "default pipeline resolves a material finding without a verify primitive",
        default_resolution_rc == 0
        and len(default_resolution_calls) == 2
        and default_resolution_record.state == "finalizing"
        and default_resolution_record.accepted_callback_kind
        == "wiki-summary",
        (default_resolution_calls, default_resolution_record),
    )
    check(
        "eventless review handoff stays within the cross-session bound",
        bool(eventless_wake.waits)
        and max(eventless_wake.waits) <= 1.0,
        eventless_wake.waits,
    )

    asynchronous_task = "99999999-9999-4999-8999-999999999999"
    asynchronous_calls: list[str] = []
    asynchronous_verification_heads: list[str] = []
    asynchronous_verification_calls: list[tuple[str, ...]] = []
    asynchronous_review_summary_shas: list[str] = []
    asynchronous_helpers: list[threading.Thread] = []

    def record_asynchronous_verification(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == ["git", "rev-parse", "HEAD"]:
            result = subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                capture_output=True,
                check=False,
            )
            asynchronous_verification_heads.append(
                result.stdout.strip()
            )
            return result
        asynchronous_verification_calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    def complete_when_callback_arrives(vault: Path, worktree: Path) -> None:
        asynchronous_calls.append(str(worktree))
        meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        profile_sha = meta["review_policy"]["verification_profile_sha256"]
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        gate_root = (
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / asynchronous_task
            / asynchronous_task
        )
        if len(asynchronous_calls) == 1:
            write_json(
                gate_root / "review-gate.json",
                {
                    "schema_version": 1,
                    "dispatch_operation_id": asynchronous_task,
                    "owner_id": asynchronous_task,
                    "status": "reviewing",
                    "product_root": str(worktree),
                    "context": {
                        "head_sha": head,
                        "verification_profile": "scoped",
                        "verification_profile_sha256": profile_sha,
                    },
                },
            )
            # The callback lands before the launch facade returns. The receipt
            # must not acknowledge it as processed until a later drive.
            write_json(
                vault
                / ".vault-meta"
                / "harness"
                / "review-runtime"
                / asynchronous_task
                / "callbacks"
                / "anthropic-holistic"
                / ".review-callback.json",
                {"schema_version": 1, "status": "ready"},
            )
            return
        if len(asynchronous_calls) == 2:
            # A second deep-review axis lands while the facade is already
            # returning from its first incomplete readiness scan.
            write_json(
                vault
                / ".vault-meta"
                / "harness"
                / "review-runtime"
                / asynchronous_task
                / "callbacks"
                / "standards"
                / ".review-callback.json",
                {"schema_version": 1, "status": "ready"},
            )
            return
        if len(asynchronous_calls) == 3:
            result_pointer = (
                gate_root / asynchronous_task / "round-anthropic-holistic-0.json"
            )
            write_json(
                result_pointer,
                {
                    "axis": "anthropic-holistic",
                    "verdict": "changes-requested",
                    "verification_iteration": 0,
                    "findings": [
                        {
                            "axis": "anthropic-holistic",
                            "finding_id": "F-material",
                            "severity": "important",
                            "file": "product.txt",
                            "line": 1,
                            "summary": "Material review finding",
                            "evidence": "The original content is incomplete.",
                            "recommendation": "Commit the exact correction.",
                        }
                    ],
                },
            )
            gate_state = json.loads(
                (gate_root / "review-gate.json").read_text(
                    encoding="utf-8"
                )
            )
            gate_state["status"] = "awaiting-resolution"
            gate_state["active_review_operation_id"] = (
                "review-operation-current"
            )
            gate_state["awaiting_resolution"] = {
                "anthropic-holistic": {
                    "pointer": result_pointer.relative_to(
                        gate_root
                    ).as_posix(),
                    "reviewed_head_sha": head,
                    "review_operation_id": "review-operation-current",
                    "round_operation_id": "round-operation-current",
                    "round_run_id": "round-run-current",
                    "callback_id": "callback-current",
                    "callback_sha256": "c" * 64,
                    "material_finding_ids": ["F-material"],
                }
            }
            write_json(gate_root / "review-gate.json", gate_state)

            def resolve_after_packet() -> None:
                import time

                packets: list[Path] = []
                for _ in range(500):
                    packet = worktree / ".task-review.json"
                    packets = [packet] if packet.is_file() else []
                    if packets:
                        break
                    time.sleep(0.02)
                if not packets:
                    return
                decision_packet = json.loads(
                    packets[0].read_text(encoding="utf-8")
                )
                (worktree / "resolution.txt").write_text(
                    "resolved\n", encoding="utf-8"
                )
                subprocess.run(
                    ["git", "add", "resolution.txt"],
                    cwd=worktree,
                    check=True,
                )
                subprocess.run(
                    ["git", "commit", "-m", "resolve review"],
                    cwd=worktree,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                resolved_head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=worktree,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip()
                write_json(
                    worktree / ".task-review-resolution.json",
                    {
                        "schema_version": 1,
                        "operation_id": asynchronous_task,
                        "review_identity_sha256": decision_packet[
                            "review_identity_sha256"
                        ],
                        "reviewed_head_sha": head,
                        "resolved_head_sha": resolved_head,
                        "resolutions": [
                            {
                                "finding_id": "F-material",
                                "disposition": "applied",
                                "rationale": (
                                    "The committed resolution is present on "
                                    "the resolved HEAD."
                                ),
                                "follow_up": "",
                            }
                        ],
                    },
                )
                refresh = (
                    root
                    / f"state-{asynchronous_task}"
                    / "pipeline-summary-refresh-notify.json"
                )
                for _ in range(500):
                    if refresh.is_file():
                        break
                    time.sleep(0.02)
                else:
                    return
                summary_path = worktree / ".task-summary.json"
                refreshed = json.loads(
                    summary_path.read_text(encoding="utf-8")
                )
                refreshed["body"] += (
                    "\n\nResolved the material review finding at final HEAD."
                )
                write_json(summary_path, refreshed)

            helper = threading.Thread(target=resolve_after_packet)
            asynchronous_helpers.append(helper)
            helper.start()
            return
        if len(asynchronous_calls) == 4:
            result_pointer = (
                gate_root / asynchronous_task / "round-anthropic-holistic-1.json"
            )
            write_json(
                result_pointer,
                {
                    "axis": "anthropic-holistic",
                    "verdict": "changes-requested",
                    "verification_iteration": 1,
                    "findings": [
                        {
                            "axis": "anthropic-holistic",
                            "finding_id": "F-material-verified",
                            "severity": "important",
                            "file": "resolution.txt",
                            "line": 1,
                            "summary": "Verification found a second issue",
                            "evidence": (
                                "The first correction needs one bounded "
                                "follow-up."
                            ),
                            "recommendation": "Commit the follow-up.",
                        }
                    ],
                },
            )
            gate_state = json.loads(
                (gate_root / "review-gate.json").read_text(
                    encoding="utf-8"
                )
            )
            gate_state["status"] = "awaiting-resolution"
            gate_state["active_review_operation_id"] = (
                "review-operation-current"
            )
            gate_state["awaiting_resolution"] = {
                "anthropic-holistic": {
                    "pointer": result_pointer.relative_to(
                        gate_root
                    ).as_posix(),
                    "reviewed_head_sha": head,
                    "review_operation_id": "review-operation-current",
                    "round_operation_id": "round-operation-verified",
                    "round_run_id": "round-run-verified",
                    "callback_id": "callback-verified",
                    "callback_sha256": "d" * 64,
                    "material_finding_ids": ["F-material-verified"],
                }
            }
            write_json(gate_root / "review-gate.json", gate_state)

            def resolve_verified_packet() -> None:
                import time

                packet_path = worktree / ".task-review.json"
                for _ in range(500):
                    if packet_path.is_file():
                        decision_packet = json.loads(
                            packet_path.read_text(encoding="utf-8")
                        )
                        if decision_packet.get("material_finding_ids") == [
                            "F-material-verified"
                        ]:
                            break
                    time.sleep(0.02)
                else:
                    return
                (worktree / "verified-resolution.txt").write_text(
                    "resolved\n", encoding="utf-8"
                )
                subprocess.run(
                    ["git", "add", "verified-resolution.txt"],
                    cwd=worktree,
                    check=True,
                )
                subprocess.run(
                    ["git", "commit", "-m", "resolve verified review"],
                    cwd=worktree,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                resolved_head = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=worktree,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip()
                write_json(
                    worktree / ".task-review-resolution.json",
                    {
                        "schema_version": 1,
                        "operation_id": asynchronous_task,
                        "review_identity_sha256": decision_packet[
                            "review_identity_sha256"
                        ],
                        "reviewed_head_sha": head,
                        "resolved_head_sha": resolved_head,
                        "resolutions": [
                            {
                                "finding_id": "F-material-verified",
                                "disposition": "applied",
                                "rationale": (
                                    "The verified follow-up is committed."
                                ),
                                "follow_up": "",
                            }
                        ],
                    },
                )
                refresh = (
                    root
                    / f"state-{asynchronous_task}"
                    / "pipeline-summary-refresh-notify.json"
                )
                for _ in range(500):
                    if refresh.is_file():
                        refresh_payload = json.loads(
                            refresh.read_text(encoding="utf-8")
                        )
                        if (
                            refresh_payload.get("approved_head_sha")
                            == resolved_head
                        ):
                            break
                    time.sleep(0.02)
                else:
                    return
                summary_path = worktree / ".task-summary.json"
                refreshed = json.loads(
                    summary_path.read_text(encoding="utf-8")
                )
                refreshed["body"] += (
                    "\n\nResolved the verified material finding at final "
                    "HEAD."
                )
                write_json(summary_path, refreshed)

            helper = threading.Thread(target=resolve_verified_packet)
            asynchronous_helpers.append(helper)
            helper.start()
            return
        asynchronous_review_summary_shas.append(
            hashlib.sha256(
                (worktree / ".task-summary.json").read_bytes()
            ).hexdigest()
        )
        (gate_root / "review-gate.json").unlink()
        ReviewGateController.skip(
            gate_root,
            dispatch_operation_id=asynchronous_task,
            owner_id=asynchronous_task,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                profile_sha,
            ),
            product_root=worktree,
        )

    (
        asynchronous_store,
        asynchronous_cmux,
        _asynchronous_state,
        asynchronous_rc,
    ) = run_case(
        root,
        asynchronous_task,
        valid_summary,
        review_state="missing",
        review_launcher=complete_when_callback_arrives,
        pipeline_name="engineering/change",
        verification_runner=record_asynchronous_verification,
    )
    for helper in asynchronous_helpers:
        helper.join(timeout=10.0)
    asynchronous_helpers_stopped = all(
        not helper.is_alive() for helper in asynchronous_helpers
    )
    asynchronous_record = asynchronous_store.read(
        "owner-1", asynchronous_task
    )
    asynchronous_verification_children = [
        record
        for record in asynchronous_store.list("owner-1")
        if record.spec.kind == "pipeline-verify"
    ]
    review_idle_contract = (
        "end the current model turn while keeping this session open. "
        "The code-owned observer owns healthy waiting; act again in this same "
        "session only on the next typed callback wake, typed escalation, or "
        "explicit coordinator request."
    )
    check(
        "summary-only refresh reuses the exact-HEAD verification identity and effect",
        asynchronous_rc == 0
        and asynchronous_helpers_stopped
        and len(asynchronous_calls) == 5
        and len(asynchronous_verification_heads) == 3
        and len(set(asynchronous_verification_heads)) == 3
        and len(asynchronous_verification_calls) == 9
        and asynchronous_review_summary_shas
        == [
            hashlib.sha256(
                (
                    root
                    / f"worktree-{asynchronous_task}"
                    / ".task-summary.json"
                ).read_bytes()
            ).hexdigest()
        ]
        and len(asynchronous_verification_children) == 3
        and all(
            child.state == "complete"
            and child.resources.process_group == 0
            and not child.pending_effect
            for child in asynchronous_verification_children
        )
        and asynchronous_record.state == "finalizing"
        and asynchronous_record.accepted_callback_kind == "wiki-summary"
        and len(asynchronous_cmux.sent) == 5
        and asynchronous_cmux.sent[0][0] == CHILD
        and "Typed review findings" in asynchronous_cmux.sent[0][1]
        and asynchronous_cmux.sent[1][0] == CHILD
        and "Refresh .task-summary.json" in asynchronous_cmux.sent[1][1]
        and asynchronous_cmux.sent[2][0] == CHILD
        and "Typed review findings" in asynchronous_cmux.sent[2][1]
        and asynchronous_cmux.sent[3][0] == CHILD
        and "Refresh .task-summary.json" in asynchronous_cmux.sent[3][1]
        and asynchronous_cmux.sent[4][0] == ORIGIN,
        (
            asynchronous_calls,
            asynchronous_verification_heads,
            asynchronous_verification_calls,
            asynchronous_verification_children,
            asynchronous_record,
        ),
    )
    check(
        "typed review wakes return the original executor session to healthy idle",
        all(
            review_idle_contract in asynchronous_cmux.sent[index][1]
            and "Remain available" not in asynchronous_cmux.sent[index][1]
            for index in (0, 2)
        )
        and asynchronous_cmux.sent[0][0] == CHILD
        and asynchronous_cmux.sent[2][0] == CHILD,
        asynchronous_cmux.sent,
    )
    asynchronous_packet = (
        root / f"worktree-{asynchronous_task}" / ".task-review.json"
    )
    check(
        "executor receives a bounded typed decision packet",
        asynchronous_packet.is_file()
        and (
            asynchronous_packet_payload := json.loads(
                asynchronous_packet.read_text(encoding="utf-8")
            )
        )["findings"][0]["finding_id"] == "F-material-verified"
        and asynchronous_packet_payload["allowed_dispositions"]
        == ["applied", "out-of-scope", "rejected"]
        and asynchronous_packet_payload["material_finding_ids"]
        == ["F-material-verified"]
        and asynchronous_packet_payload["review_operation_id"]
        == "review-operation-current"
        and asynchronous_packet_payload["review_callbacks"]
        == [
            {
                "axis": "anthropic-holistic",
                "round_operation_id": "round-operation-verified",
                "round_run_id": "round-run-verified",
                "callback_id": "callback-verified",
                "callback_sha256": "d" * 64,
            }
        ]
        and asynchronous_packet_payload["review_identity_sha256"]
        == review_transport_identity_sha256(
            "review-operation-current",
            asynchronous_packet_payload["review_callbacks"],
        )
        and asynchronous_packet_payload["resolution_path"]
        == ".task-review-resolution.json",
        asynchronous_packet,
    )
    asynchronous_refresh = (
        root
        / f"state-{asynchronous_task}"
        / "pipeline-summary-refresh-notify.json"
    )
    check(
        "review resolution cannot finalize with its pre-resolution summary",
        asynchronous_refresh.is_file()
        and "final HEAD"
        in json.loads(
            (
                root
                / f"worktree-{asynchronous_task}"
                / ".task-summary.json"
            ).read_text(encoding="utf-8")
        )["body"],
        asynchronous_refresh,
    )

    pending_task = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    pending_calls: list[str] = []

    def prepare_pending_review(
        vault: Path,
        worktree: Path,
        state: Path,
        profile_sha: str,
    ) -> None:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        write_json(
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / pending_task
            / pending_task
            / "review-gate.json",
            {
                "schema_version": 1,
                "dispatch_operation_id": pending_task,
                "owner_id": pending_task,
                "status": "reviewing",
                "product_root": str(worktree),
                "context": {
                    "head_sha": head,
                    "verification_profile": "scoped",
                    "verification_profile_sha256": profile_sha,
                },
            },
        )
        write_json(
            state / "pipeline-review-start.json",
            {
                "schema_version": 1,
                "operation_id": pending_task,
                "definition_sha256": compile_pipeline(
                    builtin_definitions()["lifecycle/default"],
                    builtin_registry(),
                    capabilities=("route:resolved",),
                ).definition_sha256,
                "status": "pending",
            },
        )

    def approve_pending(vault: Path, worktree: Path) -> None:
        pending_calls.append(str(worktree))
        gate = (
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / pending_task
            / pending_task
        )
        (gate / "review-gate.json").unlink()
        meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        ReviewGateController.skip(
            gate,
            dispatch_operation_id=pending_task,
            owner_id=pending_task,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                meta["review_policy"]["verification_profile_sha256"],
            ),
            product_root=worktree,
        )

    pending_store, _pending_cmux, _pending_state, pending_rc = run_case(
        root,
        pending_task,
        valid_summary,
        review_state="missing",
        review_launcher=approve_pending,
        before_start=prepare_pending_review,
    )
    pending_record = pending_store.read("owner-1", pending_task)
    check(
        "pending receipt plus live gate resumes the idempotent drive",
        pending_rc == 0
        and len(pending_calls) == 1
        and pending_record.state == "finalizing",
        (pending_calls, pending_record),
    )

    resumed_task = "abababab-abab-4bab-8bab-abababababab"
    resumed_calls: list[str] = []
    resume_helper_ready = threading.Event()
    resume_helper_done = threading.Event()
    resume_helpers: list[threading.Thread] = []

    def fail_once_then_approve(vault: Path, worktree: Path) -> None:
        resumed_calls.append(str(worktree))
        store = OperationStore(vault / ".vault-meta" / "harness")
        if len(resumed_calls) == 1:
            def resume_after_attention() -> None:
                import time

                resume_helper_ready.set()
                for _ in range(100):
                    record = store.read("owner-1", resumed_task)
                    if record.state == "attention-required":
                        store.transition(
                            "owner-1",
                            resumed_task,
                            record.resume_state,
                        )
                        resume_helper_done.set()
                        return
                    time.sleep(0.01)

            helper = threading.Thread(target=resume_after_attention)
            resume_helpers.append(helper)
            helper.start()
            if not resume_helper_ready.wait(timeout=1):
                raise AssertionError("resume helper did not reach its polling boundary")
            raise OSError("simulated review drive failure")
        meta = json.loads(
            (worktree / ".task-meta.json").read_text(encoding="utf-8")
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        ReviewGateController.skip(
            store.root / "review-data" / resumed_task / resumed_task,
            dispatch_operation_id=resumed_task,
            owner_id=resumed_task,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                meta["review_policy"]["verification_profile_sha256"],
            ),
            product_root=worktree,
        )

    (
        resumed_store,
        _resumed_cmux,
        resumed_state,
        resumed_rc,
    ) = run_case(
        root,
        resumed_task,
        valid_summary,
        review_state="missing",
        review_launcher=fail_once_then_approve,
    )
    resumed_record = resumed_store.read("owner-1", resumed_task)
    if not resume_helper_done.wait(timeout=1):
        raise AssertionError("resume helper did not observe durable attention")
    for helper in resume_helpers:
        helper.join(timeout=1)
    recovery = read_json_eventually(
        resumed_state / "callback-recovery.json"
    )
    check(
        "explicit durable resume clears only the matching summary attention latch",
        resumed_rc == 0
        and len(resumed_calls) == 2
        and resumed_record.state == "finalizing"
        and recovery["status"] == "resumed"
        and recovery["resumed_revision"]
        > recovery["attention_revision"],
        (resumed_calls, resumed_record, recovery),
    )


def _continuation_fixture(name: str) -> RecoverySnapshot:
    path = (
        ROOT
        / "tests/harness/fixtures/review-continuation"
        / name
    )
    return RecoverySnapshot.from_mapping(
        json.loads(path.read_text(encoding="utf-8"))
    )


with tempfile.TemporaryDirectory(prefix="review-continuation-rearm.") as raw:
    base = Path(raw)
    store = OperationStore(base / "harness")
    owner = "review-continuation-owner"
    operation_id = "review-continuation-root"
    run_id = "review-continuation-run"
    spec = OperationSpec(
        operation_id,
        "review-continuation-key",
        "dispatch",
        owner,
        RuntimeRoute("codex", "gpt-5.6-sol", "high", "executor", "a" * 64),
        "packets/task.json",
        "scoped",
    )
    store.create(spec, lane_id="continuation-lane", run_id=run_id)
    for next_state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(owner, operation_id, next_state)
    store.transition(
        owner,
        operation_id,
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )
    current = store.read(owner, operation_id)
    state_root = base / "runtime"
    state_root.mkdir()
    write_json(
        state_root / "callback-error.json",
        {"schema_version": 1, "status": "review-drive-failed"},
    )
    captured = _continuation_fixture(
        "review-drive-failed-after-reverify.json"
    )
    snapshot = replace(
        captured,
        root=replace(
            captured.root,
            owner_id=owner,
            operation_id=operation_id,
            run_id=run_id,
            revision=current.revision,
        ),
    )
    decision = classify_review_continuation(snapshot)

    class SameGenerationWorker(RuntimeWorkerCustomMixin):
        def __init__(self) -> None:
            self.spec = {
                "callback_mode": "task-summary",
                "owner_id": owner,
                "operation_id": operation_id,
            }
            self.spec_path = state_root / "launch.json"
            self.store = store
            self.callback_handled = True
            self.summary_attention_revision = current.revision
            self.restart_attention_recovery_done = True
            self.executions = 0

        def review_continuation_decision(self):
            return decision

        def execute_review_continuation(self, _decision) -> bool:
            self.executions += 1
            return True

    worker = SameGenerationWorker()
    worker.recover_task_summary_attention()
    recovered = store.read(owner, operation_id)
    receipt_path = (
        state_root
        / "review-continuation-recovery"
        / f"{decision.receipt.identity.scope_sha256}.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    recovered_revision = recovered.revision
    worker.recover_task_summary_attention()
    repeated = store.read(owner, operation_id)
    check(
        "same-generation review-drive recovery is receipt-bound and exactly once",
        recovered.state == "awaiting-callback"
        and recovered.revision == current.revision + 1
        and receipt["status"] == "finalized"
        and receipt["outcome"] == "advanced"
        and receipt["identity_sha256"] == decision.receipt.identity_sha256
        and worker.executions == 1
        and repeated.revision == recovered_revision,
        (recovered, receipt, worker.executions, repeated),
    )

    store.transition(
        owner,
        operation_id,
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )
    callback_shape = _continuation_fixture(
        "accepted-callback-pending-ingestion.json"
    )
    callback_root = store.read(owner, operation_id)
    callback_decision = classify_review_continuation(
        replace(
            callback_shape,
            root=replace(
                callback_shape.root,
                owner_id=owner,
                operation_id=operation_id,
                run_id=run_id,
                revision=callback_root.revision,
            ),
        )
    )
    worker.review_continuation_decision = lambda: callback_decision
    worker.recover_review_continuation()
    receipts = sorted(
        (state_root / "review-continuation-recovery").glob("*.json")
    )
    check(
        "distinct recovery identities retain independent exactly-once slots",
        len(receipts) == 2
        and worker.executions == 2
        and {
            json.loads(path.read_text(encoding="utf-8"))["identity"][
                "recovery_class"
            ]
            for path in receipts
        }
        == {"review-drive", "accepted-callback"},
        receipts,
    )

    store.transition(
        owner,
        operation_id,
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )
    waiting_root = store.read(owner, operation_id)
    waiting_shape = replace(
        captured,
        gate=replace(captured.gate, sha256="e" * 64),
        attempt=replace(captured.attempt, attempt_id="waiting-attempt"),
        root=replace(
            captured.root,
            owner_id=owner,
            operation_id=operation_id,
            run_id=run_id,
            revision=waiting_root.revision,
        ),
    )
    waiting_decision = classify_review_continuation(waiting_shape)
    waits = {"count": 0}

    def wait_once(_decision):
        waits["count"] += 1
        return waits["count"] > 1

    worker.review_continuation_decision = lambda: waiting_decision
    worker.execute_review_continuation = wait_once
    worker.recover_review_continuation()
    waiting_path = (
        state_root
        / "review-continuation-recovery"
        / f"{waiting_decision.receipt.identity.scope_sha256}.json"
    )
    prepared_wait = json.loads(waiting_path.read_text(encoding="utf-8"))
    worker.recover_review_continuation()
    completed_wait = json.loads(waiting_path.read_text(encoding="utf-8"))
    check(
        "a documented not-ready workflow result retries the same prepared identity",
        prepared_wait["status"] == "prepared"
        and completed_wait["status"] == "finalized"
        and completed_wait["outcome"] == "advanced"
        and waits["count"] == 2,
        (prepared_wait, completed_wait, waits),
    )

    for status in (
        "wiki-summary-invalid",
        "callback-wake-effect-uncertain",
        "pipeline-verification-effect-uncertain",
    ):
        isolated_owner = f"owner-{status}"
        isolated_operation = f"operation-{status}"
        isolated_spec = OperationSpec(
            isolated_operation,
            f"key-{status}",
            "dispatch",
            isolated_owner,
            RuntimeRoute(
                "codex", "gpt-5.6-sol", "high", "executor", "b" * 64
            ),
            "packets/task.json",
            "scoped",
        )
        store.create(
            isolated_spec,
            lane_id=f"lane-{status}",
            run_id=f"run-{status}",
        )
        for next_state in (
            "preflight",
            "starting",
            "running",
            "awaiting-callback",
        ):
            store.transition(isolated_owner, isolated_operation, next_state)
        store.transition(
            isolated_owner,
            isolated_operation,
            "attention-required",
            reason=AttentionReason.ATTENTION_REQUIRED,
        )
        isolated = store.read(isolated_owner, isolated_operation)
        refusal = classify_review_continuation(
            replace(
                captured,
                attention_status=status,
                root=replace(
                    captured.root,
                    owner_id=isolated_owner,
                    operation_id=isolated_operation,
                    run_id=isolated.run_id,
                    revision=isolated.revision,
                ),
            )
        )
        worker.spec = {
            "callback_mode": "task-summary",
            "owner_id": isolated_owner,
            "operation_id": isolated_operation,
        }
        status_root = state_root / status
        status_root.mkdir()
        worker.spec_path = status_root / "launch.json"
        worker.callback_handled = True
        worker.summary_attention_revision = isolated.revision
        worker.review_continuation_decision = lambda refusal=refusal: refusal
        worker.recover_task_summary_attention()
        unchanged = store.read(isolated_owner, isolated_operation)
        check(
            f"{status} remains mutation-free in the same generation",
            refusal.reason is RecoveryReason.ATTENTION_NOT_RECOVERABLE
            and unchanged == isolated
            and not worker.spec_path.with_name(
                "review-continuation-recovery"
            ).exists(),
            (refusal, isolated, unchanged),
        )
