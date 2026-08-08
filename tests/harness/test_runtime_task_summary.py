#!/usr/bin/env python3
"""Hermetic task-summary callback and coordinator wake regressions."""

from __future__ import annotations

import hashlib
import json
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
from harness.store import OperationStore
from harness.supervisor import OperationSupervisor
from harness.verification import load_profiles
from harness.verification_attempt import (  # noqa: E402
    VerificationAttempt,
    mechanism_flake_decision_text,
)
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


class FakeCmux:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.keys: list[tuple[str, str]] = []

    def send(self, surface_id: str, text: str) -> None:
        self.sent.append((surface_id, text))

    def send_key(self, surface_id: str, key: str) -> None:
        self.keys.append((surface_id, key))


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
        self, request: object, *, on_surface_opened=None
    ) -> FakeReviewSessionResult:
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


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


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
    fix_restart_after: str = "",
    model_restart_limit: int | None = None,
    completion_policy: str = "attention",
    total_pass_limit: int = 2,
    verification_runner: Callable[..., subprocess.CompletedProcess[str]]
    | None = None,
    task_version: int = 3,
    bind_runtime_resources: bool = False,
    typed_review: bool = False,
    atomic_publication_barrier: bool = False,
    phase_callback_publication_barrier_step: str = "",
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
    subprocess.run(["git", "add", "product.txt"], cwd=worktree, check=True)
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
            "  subprocess.run(['git','commit','--allow-empty','-m',f'provider pass {iteration + 1}'],cwd=root,text=True,capture_output=True,check=True)\n"
            "  publish_summary(summary,sys.argv[6] if iteration and len(sys.argv)>6 else sys.argv[2])\n"
            "time.sleep(0.3)\n",
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
            "seen=set()\n"
            "for expected in ('reproduce','root-cause','regression-test','minimal-fix'):\n"
            "  for _ in range(2000):\n"
            "    if request.is_file():\n"
            "      row=json.loads(request.read_text(encoding='utf-8'))\n"
            "      if row.get('step_id')==expected and row.get('operation_id') not in seen: break\n"
            "    time.sleep(0.01)\n"
            "  else: raise SystemExit(3)\n"
            "  seen.add(row['operation_id'])\n"
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
            "publish_summary(summary,sys.argv[2],sys.argv[3] if len(sys.argv)>3 else '')\n"
            "time.sleep(0.3)\n",
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
    cmux = FakeCmux()
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
    thread = threading.Thread(
        target=lambda: result.append(
            run_worker(
                launch.spec_path,
                poll_seconds=0.02,
                checkpoint_probe=lambda _surface, _runtime: "checkpoint-1",
                cmux_adapter=cmux,
                review_launcher=review_launcher,
                verification_runner=verification_runner,
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
    thread.join(timeout=8 if pipeline_name == "engineering/fix" else 3)
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
    return store, cmux, launch.spec_path.parent, result[0]


with tempfile.TemporaryDirectory(prefix="runtime-task-summary.") as raw:
    root = Path(raw)
    assert_review_drive_failure_receipt_is_content_free()
    assert_summary_refresh_notification_replays_without_effect(root)
    assert_review_resolution_notification_crashes_fail_closed(root)
    assert_durable_review_packet_generation_can_advance(root)
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
        == 1,
        (
            restart_parent,
            restart_receipt,
            restart_steps,
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

    autonomous_limit_task = "ebeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    (
        autonomous_limit_store,
        _autonomous_limit_cmux,
        autonomous_limit_state,
        autonomous_limit_rc,
    ) = run_case(
        root,
        autonomous_limit_task,
        valid_summary,
        pipeline_name="engineering/fix",
        fix_retry_passes=3,
        completion_policy="autonomous",
        total_pass_limit=3,
        verification_runner=fail_verification,
    )
    autonomous_limit_parent = autonomous_limit_store.read(
        "owner-1", autonomous_limit_task
    )
    terminal_exhausted = json.loads(
        (
            autonomous_limit_state
            / "pipeline-fix"
            / "terminal-exhausted.json"
        ).read_text(encoding="utf-8")
    )
    check(
        "autonomous fix policy fails terminally after three total passes",
        autonomous_limit_rc == 0
        and autonomous_limit_parent.state == "failed"
        and terminal_exhausted["status"] == "retry-exhausted"
        and terminal_exhausted["total_pass_limit"] == 3
        and not (
            autonomous_limit_state / "callback-error.json"
        ).exists(),
        (autonomous_limit_parent, terminal_exhausted),
    )

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
        )["status"]
        == "accepted"
        and commands_before_resubmission == [1]
        and failing_commands == [4]
        and failed_cmux.sent
        and failed_cmux.sent[0][0] == CHILD
        and ".task-verification.json" in failed_cmux.sent[0][1],
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
                    },
                )
                resolution = append_resolution(
                    worktree,
                    mechanism_flake_decision_text(
                        attempt, str(packet["verification_operation_id"])
                    ),
                    resolved_at="2026-08-05T12:01:00Z",
                )
                packet_sha256 = hashlib.sha256(
                    json.dumps(
                        packet, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest()
                next_attempt = attempt.same_head_retry()
                write_json(
                    worktree / ".task-verification-response.json",
                    {
                        "schema_version": 2,
                        "operation_id": task_id,
                        "verification_operation_id": packet[
                            "verification_operation_id"
                        ],
                        "failed_head_sha": packet["head_sha"],
                        "packet_sha256": packet_sha256,
                        "response": "retry-mechanism-flake",
                        "resubmitted_head_sha": packet["head_sha"],
                        "failed_attempt_sha256": attempt.sha256,
                        "next_attempt": next_attempt.as_dict(),
                        "next_attempt_sha256": next_attempt.sha256,
                        "mechanism_flake_decision_id": escalation_id,
                        "mechanism_flake_decision_sha256": resolution.sha256,
                    },
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
        input_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "definition_sha256": pipeline.definition_sha256,
                    "head_sha": failed_head,
                    "profile_sha256": profile_sha,
                    "schema_version": 1,
                    "summary": valid_summary,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
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
        child_receipt = {
            "schema_version": 1,
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
        and len(crash_response_receipts) == 1,
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
        "invalid handoff becomes attention and never notifies coordinator",
        invalid_rc == 0
        and invalid_record.state == "attention-required"
        and not invalid_record.accepted_callback_id
        and invalid_cmux.sent == []
        and invalid_cmux.keys == []
        and not (invalid_state / "task-summary-notify.json").exists(),
        invalid_record,
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
