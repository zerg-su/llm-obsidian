#!/usr/bin/env python3
"""Provider-free repetition gate for registered production transitions."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.callbacks import CallbackBroker  # noqa: E402
from harness.contracts import CallbackEnvelope, OperationSpec, RuntimeRoute  # noqa: E402
from harness.liveness import (  # noqa: E402
    LivenessEvidence,
    LivenessPolicy,
    LivenessState,
    observe_liveness,
)
from harness.runtime_worker_control import RuntimeWorkerControlMixin  # noqa: E402
from harness.runtime_worker_custom import RuntimeWorkerCustomMixin  # noqa: E402
from harness.runtime_worker_review_bridge import RuntimeWorkerReviewBridgeMixin  # noqa: E402
from harness.runtime_session_continuation import (  # noqa: E402
    await_initial_input_visible,
)
from harness.store import OperationStore  # noqa: E402
from harness.supervisor import OperationSupervisor  # noqa: E402
from harness.verification_attempt import (  # noqa: E402
    VerificationAttempt,
    VerificationAttemptError,
    pipeline_verify_identity,
)
from harness.workflows.dispatch import (  # noqa: E402
    DispatchRequest,
    ReviewPolicy,
    run_dispatch,
)
from harness.workflows.reap import run_reap  # noqa: E402


REPETITIONS = 50
SURFACE = "11111111-1111-4111-8111-111111111111"
WORKSPACE = "22222222-2222-4222-8222-222222222222"
ROUTE = RuntimeRoute("codex", "gpt-5.6-sol", "low", "executor", "a" * 64)


def stable_id(name: str, repetition: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"v287:{name}:{repetition}"))


class Port:
    def __init__(self, runtime: str) -> None:
        self.runtime = runtime
        self.sent: list[str] = []
        self.keys: list[str] = []
        self.screen = "› old" if runtime == "codex" else "❯ old"

    def read(self, surface_id: str) -> str:
        assert surface_id == SURFACE
        return self.screen

    def send(self, surface_id: str, text: str) -> None:
        assert surface_id == SURFACE
        self.sent.append(text)
        anchor = " ".join(text.splitlines()[0].strip().split())[:96]
        marker = "›" if self.runtime == "codex" else "❯"
        self.screen = f"{marker} {anchor}"

    def send_key(self, surface_id: str, key: str) -> None:
        assert surface_id == SURFACE and key == "Enter"
        self.keys.append(key)

    def agent_status(self, workspace_id: str, runtime: str) -> str:
        assert workspace_id == WORKSPACE and runtime == self.runtime
        return "idle"


class PipelineWorkerBase:
    def __init__(
        self,
        root: Path,
        runtime: str,
        readiness: tuple[bool, ...],
        readiness_default: bool,
    ) -> None:
        self.cmux_adapter = Port(runtime)
        self.spec_path = root / "state" / "spec.json"
        self.spec_path.parent.mkdir(parents=True, exist_ok=True)
        self.spec = {"surface_id": SURFACE, "runtime": runtime}
        self._readiness = iter(readiness)
        self._readiness_default = readiness_default

    def _workspace_id(self) -> str:
        return WORKSPACE

    def pipeline_step_callback_ready(
        self, *, operation_id: str, run_id: str
    ) -> bool:
        assert operation_id and run_id
        return next(self._readiness, self._readiness_default)

    @staticmethod
    def write_immutable_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


class CustomWorker(PipelineWorkerBase, RuntimeWorkerCustomMixin):
    pass


class FixWorker(PipelineWorkerBase, RuntimeWorkerControlMixin):
    pass


class SummaryRefreshWorker(RuntimeWorkerReviewBridgeMixin):
    def __init__(self, root: Path, repetition: int) -> None:
        self.cmux_adapter = Port("codex")
        self.spec_path = root / "state" / "spec.json"
        self.spec_path.parent.mkdir(parents=True, exist_ok=True)
        self.spec = {
            "operation_id": stable_id("summary-refresh", repetition),
            "surface_id": SURFACE,
        }
        self.digest = hashlib.sha256(f"before:{repetition}".encode()).hexdigest()
        self.verification_head = f"{repetition + 1:040x}"[-40:]
        self.verification_gap_authority = None
        self.verification_receipt_path = root / "verification-receipt.json"
        self.verification_receipt_path.write_text(
            json.dumps({"status": "complete"}) + "\n", encoding="utf-8"
        )
        self.resumed_wake_identities: set[str] = set()


def pipeline_notification_race(
    root: Path,
    repetition: int,
    *,
    corridor: str,
    worker_type: type[PipelineWorkerBase],
    request: dict[str, object],
    method_name: str,
) -> None:
    runtime = "codex" if repetition % 2 == 0 else "claude"
    ordinary = worker_type(
        root / corridor / "ordinary", runtime, (), False
    )
    getattr(ordinary, method_name)(request)
    assert len(ordinary.cmux_adapter.sent) == 1
    assert ordinary.cmux_adapter.keys == ["Enter"], (
        ordinary.cmux_adapter.sent,
        ordinary.cmux_adapter.keys,
        ordinary.cmux_adapter.screen,
        list(ordinary.spec_path.parent.rglob("*-delivery.json")),
        [
            path.read_text(encoding="utf-8")
            for path in ordinary.spec_path.parent.rglob("*-delivery.json")
        ],
    )

    raced = worker_type(
        root / corridor / "successor", runtime, (False, True), True
    )
    getattr(raced, method_name)(request)
    assert len(raced.cmux_adapter.sent) == 1
    assert raced.cmux_adapter.keys == []
    operation_id = str(request["operation_id"])
    namespace = "pipeline-custom" if corridor == "custom-step" else "pipeline-fix"
    receipt = json.loads(
        (
            raced.spec_path.parent
            / namespace
            / "notifications"
            / f"{operation_id}-delivery.json"
        ).read_text(encoding="utf-8")
    )
    assert receipt["stage"] == "superseded" and receipt["submit_count"] == 0
    getattr(raced, method_name)(request)
    assert len(raced.cmux_adapter.sent) == 1 and raced.cmux_adapter.keys == []


def dispatch_handoff(root: Path, repetition: int) -> None:
    task_id = stable_id("dispatch", repetition)
    request = DispatchRequest(
        task_id,
        task_id,
        "b" * 64,
        f"packets/dispatch/{repetition}/manifest.json",
        ROUTE,
        review=ReviewPolicy(enabled=False),
    )
    store = OperationStore(root / "store")
    launches: list[str] = []
    first = run_dispatch(
        request,
        store,
        launch=lambda record: launches.append(record.run_id)
        or {"task_surface": SURFACE},
        persist_result=lambda _record, _result: None,
    )
    replay = run_dispatch(
        request,
        store,
        launch=lambda _record: (_ for _ in ()).throw(
            AssertionError("dispatch provider effect replayed")
        ),
        persist_result=lambda _record, _result: None,
    )
    assert first.record.state == replay.record.state == "awaiting-callback"
    assert len(launches) == 1


def built_in_summary_liveness(repetition: int) -> None:
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
    assert decision.action == "nudge" and recovered.nudge_count == 1


def claude_initial_input_handoff(repetition: int) -> None:
    """Current Claude renders a typed composer with ``›`` after idle ``❯``."""

    prompt = (
        "Read and follow the complete task contract at "
        f"/tmp/v287-{repetition:03d}/.task-prompt.md "
        f"(SHA-256 {repetition + 1:064x})."
    )

    class TypedClaudePort:
        def __init__(self, screen: str) -> None:
            self.screen = screen

        def read(self, surface_id: str) -> str:
            assert surface_id == SURFACE
            return self.screen

    wrapped = TypedClaudePort(f"› {prompt[:46]}\n  {prompt[46:]}")
    assert await_initial_input_visible(
        wrapped,
        surface_id=SURFACE,
        runtime="claude",
        text=prompt,
        observation_limit=1,
        observation_interval_seconds=0,
        wait=lambda _seconds: None,
    )

    collapsed = TypedClaudePort(
        f"› [Pasted text #{repetition + 1} +2 lines]"
    )
    assert await_initial_input_visible(
        collapsed,
        surface_id=SURFACE,
        runtime="claude",
        text=prompt,
        observation_limit=1,
        observation_interval_seconds=0,
        wait=lambda _seconds: None,
    )

    bare = TypedClaudePort("›")
    assert not await_initial_input_visible(
        bare,
        surface_id=SURFACE,
        runtime="claude",
        text=prompt,
        observation_limit=1,
        observation_interval_seconds=0,
        wait=lambda _seconds: None,
    )


def verification_retry_handoff(repetition: int) -> None:
    parent_id = f"verify-parent-{repetition:03d}"
    parent = OperationSpec(
        parent_id,
        hashlib.sha256(parent_id.encode()).hexdigest(),
        "dispatch",
        parent_id,
        ROUTE,
        f"packets/verify/{repetition}/manifest.json",
        "scoped",
        root_operation_id=parent_id,
    )
    input_sha256 = hashlib.sha256(f"input:{repetition}".encode()).hexdigest()
    initial = VerificationAttempt(parent_id, "scoped", "c" * 64, "d" * 40, 0)
    retry = initial.same_head_retry()
    first_spec, first_lane, first_run = pipeline_verify_identity(
        parent,
        definition_sha256="e" * 64,
        input_sha256=input_sha256,
        profile="scoped",
        attempt_index=initial.attempt_index,
    )
    retry_spec, retry_lane, retry_run = pipeline_verify_identity(
        parent,
        definition_sha256="e" * 64,
        input_sha256=input_sha256,
        profile="scoped",
        attempt_index=retry.attempt_index,
    )
    assert initial.exact_head_sha == retry.exact_head_sha
    assert (first_spec.operation_id, first_lane, first_run) != (
        retry_spec.operation_id,
        retry_lane,
        retry_run,
    )
    try:
        retry.same_head_retry()
    except VerificationAttemptError:
        pass
    else:
        raise AssertionError("verification retry authority exceeded one attempt")


def summary_refresh_handoff(root: Path, repetition: int) -> None:
    worker = SummaryRefreshWorker(root, repetition)
    verification = {
        "status": "complete",
        "operation_id": f"verify-summary-{repetition:03d}",
    }
    assert worker.wait_for_summary_refresh_after_verification(verification)
    assert worker.wait_for_summary_refresh_after_verification(verification)
    assert len(worker.cmux_adapter.sent) == 1
    assert worker.cmux_adapter.keys == ["Enter"]
    worker.digest = hashlib.sha256(f"after:{repetition}".encode()).hexdigest()
    assert not worker.wait_for_summary_refresh_after_verification(verification)
    marker = json.loads(
        (worker.spec_path.parent / "pipeline-summary-refresh-notify.json").read_text(
            encoding="utf-8"
        )
    )
    assert marker["status"] == "accepted"
    assert marker["refreshed_summary_sha256"] == worker.digest


def review_callback_handoff(root: Path, repetition: int) -> None:
    owner = stable_id("review-owner", repetition)
    operation_id = stable_id("review", repetition)
    store = OperationStore(root / "store")
    spec = OperationSpec(
        operation_id,
        hashlib.sha256(operation_id.encode()).hexdigest(),
        "review",
        owner,
        ROUTE,
        f"packets/review/{repetition}/manifest.json",
        "scoped",
    )
    record = store.create(spec, lane_id=f"lane-{repetition}", run_id=f"run-{repetition}")
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(owner, operation_id, state)
    record = store.read(owner, operation_id)
    payload = {"findings": [], "verdict": "approve"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    envelope = CallbackEnvelope(
        stable_id("review-callback", repetition),
        operation_id,
        record.run_id,
        "review",
        payload,
        hashlib.sha256(encoded).hexdigest(),
    )
    broker = CallbackBroker(store, owner)
    first = broker.accept(envelope)
    replay = broker.accept(envelope)
    assert first.accepted and first.next_state == "finalizing"
    assert replay.duplicate and not replay.accepted


def reap_handoff(root: Path, repetition: int) -> None:
    task_id = stable_id("reap", repetition)
    store = OperationStore(root / "store")
    request = DispatchRequest(
        task_id,
        task_id,
        "f" * 64,
        f"packets/reap/{repetition}/manifest.json",
        ROUTE,
        review=ReviewPolicy(enabled=False),
    )
    run_dispatch(
        request,
        store,
        launch=lambda _record: {"task_surface": SURFACE},
        persist_result=lambda _record, _result: None,
    )
    effects: list[str] = []
    summary = {"type": "repo-touch", "title": f"Result {repetition}"}
    first = run_reap(
        store,
        owner_id=task_id,
        operation_id=task_id,
        summary=summary,
        finalize=lambda record: effects.append(record.run_id)
        or {"status": "complete"},
    )
    assert first.record.state == "finalizing" and len(effects) == 1
    supervisor = OperationSupervisor(store, task_id, task_id)
    supervisor.transition("exiting")
    supervisor.transition("complete")
    replay = run_reap(
        store,
        owner_id=task_id,
        operation_id=task_id,
        summary=summary,
        finalize=lambda _record: (_ for _ in ()).throw(
            AssertionError("reap finalization effect replayed")
        ),
    )
    assert replay.record.state == "complete" and replay.result is None


with tempfile.TemporaryDirectory(prefix="transition-transport-stress.") as raw:
    root = Path(raw)
    for repetition in range(REPETITIONS):
        dispatch_handoff(root / f"dispatch-{repetition:03d}", repetition)
        built_in_summary_liveness(repetition)
        claude_initial_input_handoff(repetition)
        pipeline_notification_race(
            root / f"custom-{repetition:03d}",
            repetition,
            corridor="custom-step",
            worker_type=CustomWorker,
            request={
                "operation_id": stable_id("custom", repetition),
                "run_id": f"custom-run-{repetition}",
                "step_id": "implement",
                "visit": 1,
                "allowed_outcomes": ["complete"],
            },
            method_name="notify_custom_step",
        )
        pipeline_notification_race(
            root / f"fix-{repetition:03d}",
            repetition,
            corridor="engineering-fix",
            worker_type=FixWorker,
            request={
                "operation_id": stable_id("fix", repetition),
                "run_id": f"fix-run-{repetition}",
                "step_id": "reproduce",
                "iteration": 0,
                "output_pointer": ".task-pipeline/outputs/pass-0/reproduce.md",
                "result_pointer": ".task-pipeline/results/pass-0/reproduce.json",
            },
            method_name="notify_fix_phase",
        )
        verification_retry_handoff(repetition)
        summary_refresh_handoff(
            root / f"summary-refresh-{repetition:03d}", repetition
        )
        review_callback_handoff(
            root / f"review-callback-{repetition:03d}", repetition
        )
        reap_handoff(root / f"reap-{repetition:03d}", repetition)

print(
    "Transition transport stress passed: 9 registered production corridors x "
    f"{REPETITIONS} repetitions"
)
