#!/usr/bin/env python3
"""Frozen missing-submit incident and integrated reviewer recovery seams."""

from __future__ import annotations

import json
import hashlib
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.callback_submit_recovery import (  # noqa: E402
    CallbackSubmitEvidence,
    CallbackSubmitPolicy,
    classify_callback_prompt,
    classify_callback_submit,
)
from harness.callbacks import CallbackBroker  # noqa: E402
from harness.contracts import (  # noqa: E402
    AttentionReason,
    CallbackEnvelope,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
    to_dict,
)
from harness.liveness import (  # noqa: E402
    LivenessController,
    LivenessEvidence,
    LivenessPolicy,
)
from harness.pipeline_builtins import compiled_builtin  # noqa: E402
from harness.runtime_worker_summary import (  # noqa: E402
    RuntimeWorkerSummaryMixin,
    SummaryPipelineState,
)
from harness.runtime_worker_review_bridge import (  # noqa: E402
    RuntimeWorkerReviewBridgeMixin,
)
from harness.runtime_callback_io import (  # noqa: E402
    _callback_target,
    _current_callback_receipt_sha256,
)
from harness.runtime_worker_liveness import RuntimeWorkerLivenessMixin  # noqa: E402
from harness.runtime_sessions import RuntimeSessionManager  # noqa: E402
from harness.runtime_session_contracts import RuntimeSessionError  # noqa: E402
from harness.store import OperationStore  # noqa: E402
from harness.supervisor import OperationSupervisor  # noqa: E402
from harness.workflows.review import (  # noqa: E402
    ReviewContext,
    ReviewLaneSession,
    ReviewOperationRequest,
    ReviewRequest,
    ReviewResult,
    ReviewRound,
    accept_review_round,
    review_round_envelope,
)
from harness.workflows.review_gate import (  # noqa: E402
    ReviewGateController,
    ReviewPreset,
)


SURFACE = "11111111-1111-1111-1111-111111111111"


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


with tempfile.TemporaryDirectory(prefix="accepted-receipt-identity.") as raw:
    runtime_root = Path(raw)
    target = {
        "schema_version": 1,
        "generation": 7,
        "operation_id": "round-7",
        "run_id": "expected-run",
    }
    receipt = {
        **target,
        "callback_id": "review-accepted-receipt",
        "payload_sha256": "a" * 64,
        "status": "accepted",
    }
    (runtime_root / "callback-target.json").write_text(
        json.dumps(target), encoding="utf-8"
    )
    receipt_path = runtime_root / "callback-receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    check(
        "accepted receipt binds the complete current callback identity",
        bool(
            _current_callback_receipt_sha256(
                runtime_root,
                expected_callback_id=receipt["callback_id"],
                expected_payload_sha256=receipt["payload_sha256"],
            )
        ),
    )
    check(
        "accepted receipt rejects a different valid broker callback identity",
        not _current_callback_receipt_sha256(
            runtime_root,
            expected_callback_id="review-other-valid-callback",
            expected_payload_sha256=receipt["payload_sha256"],
        ),
    )
    for field, invalid in (
        ("run_id", "wrong-run"),
        ("callback_id", "not valid whitespace"),
        ("payload_sha256", "not-a-digest"),
    ):
        malformed = {**receipt, field: invalid}
        receipt_path.write_text(json.dumps(malformed), encoding="utf-8")
        check(
            f"accepted receipt rejects malformed {field}",
            not _current_callback_receipt_sha256(runtime_root),
        )


fixture = json.loads(
    (
        ROOT
        / "tests/harness/fixtures/callback-submit"
        / "v2.6.3-missing-review-submit.json"
    ).read_text(encoding="utf-8")
)
incident = fixture["incident"]
dogfood_evidence = json.loads(
    (
        ROOT
        / "docs/acceptance/v2.6.4-unattended-missing-submit-dogfood.json"
    ).read_text(encoding="utf-8")
)


with tempfile.TemporaryDirectory(prefix="v263-exact-runtime.") as raw:
    root = Path(raw).resolve()
    snapshot = root / "snapshot"
    snapshot.mkdir()
    archive = subprocess.check_output(
        ["git", "archive", fixture["base_commit"]], cwd=ROOT
    )
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        bundle.extractall(snapshot, filter="data")
    for base_path, expected_key in (
        (
            "scripts/harness/runtime_worker.py",
            "base_runtime_worker_sha256",
        ),
        (
            "scripts/harness/runtime_worker_liveness.py",
            "base_runtime_worker_liveness_sha256",
        ),
        (
            "scripts/harness/runtime_worker_loop.py",
            "base_runtime_worker_loop_sha256",
        ),
        (
            "scripts/harness/runtime_worker_execution.py",
            "base_runtime_worker_execution_sha256",
        ),
        ("scripts/harness/callbacks.py", "base_callbacks_sha256"),
        ("scripts/harness/store.py", "base_store_sha256"),
    ):
        executed_bytes = (snapshot / base_path).read_bytes()
        check(
            f"executed v2.6.3 snapshot binds exact {base_path}",
            hashlib.sha256(executed_bytes).hexdigest()
            == fixture[expected_key],
        )

    compatibility_runner = r'''
import json
import os
import sys
import threading
import time
from pathlib import Path

snapshot = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2]).resolve()

from harness.adapters.codex import CodexDriver
from harness.adapters.process import ProcessAdapter
from harness.callbacks import CallbackBroker
from harness.contracts import OperationSpec, OwnedResources, RuntimeRoute
from harness.runtime_worker import run
from harness.store import OperationStore, StoreError
from harness.supervisor import OperationSupervisor
import harness.runtime_worker as executed_runtime_worker
import harness.runtime_worker_liveness as executed_runtime_liveness

assert Path(executed_runtime_worker.__file__).resolve() == (
    snapshot / "scripts/harness/runtime_worker.py"
)
assert Path(executed_runtime_liveness.__file__).resolve() == (
    snapshot / "scripts/harness/runtime_worker_liveness.py"
)

scratch = root / "scratch"
product = root / "product"
state_root = root / "worker-state"
callback_dir = scratch / "callbacks" / "openai-holistic"
for directory in (scratch, product, state_root, callback_dir):
    directory.mkdir(parents=True, exist_ok=True)
callback_path = callback_dir / ".review-callback.json"
provider_marker = root / "provider-starts.jsonl"
fake_codex = root / "codex"
fake_codex.write_text(
    f"#!{sys.executable}\n"
    "import pathlib,time\n"
    f"marker=pathlib.Path({str(provider_marker)!r})\n"
    "with marker.open('a', encoding='utf-8') as handle: handle.write('start\\n')\n"
    "time.sleep(0.8)\n",
    encoding="utf-8",
)
fake_codex.chmod(0o755)

route = RuntimeRoute(
    "codex", "gpt-5.6-sol", "high", "reviewer-callback", "6" * 64
)
spec = OperationSpec(
    "review-parent-1",
    "v263-missing-submit-key",
    "simple-review-holistic",
    "v263-review-owner",
    route,
    "packets/review.json",
    "scoped",
)
store = OperationStore(root / "store")
store.create(
    spec,
    lane_id="openai-holistic",
    run_id="review-run-1",
)
supervisor = OperationSupervisor(
    store, "v263-review-owner", "review-parent-1"
)
supervisor.configure_budget(
    attempt_limit=1,
    model_restart_limit=0,
    time_budget_seconds=1,
    token_limit=100,
    now=time.time() - 2,
)
for state in ("preflight", "starting", "running", "awaiting-callback"):
    store.transition("v263-review-owner", "review-parent-1", state)

provider_command = CodexDriver(fake_codex).command(
    route,
    callback_pointer=callback_path,
    product_root=product,
    session_root=scratch,
)
launch = ProcessAdapter().prepare_surface_launch(
    argv=(*provider_command, "review"),
    cwd=scratch,
    state_root=state_root,
    worker=snapshot / "scripts/harness-runtime-worker.py",
    callback_pointer=callback_path,
    product_root=product,
    reviewer_sandbox=True,
    store_root=store.root,
    owner_id="v263-review-owner",
    operation_id="review-parent-1",
    run_id="review-run-1",
    surface_id="11111111-1111-4111-8111-111111111111",
    runtime="codex",
)

broker_calls = [0]
original_accept = CallbackBroker.accept
def counted_accept(self, *args, **kwargs):
    broker_calls[0] += 1
    return original_accept(self, *args, **kwargs)
CallbackBroker.accept = counted_accept

class FakeCmux:
    def __init__(self):
        self.sends = []
        self.keys = []
        self.reads = 0
        self.screens = []
    def read(self, surface_id):
        self.reads += 1
        self.screens.append("›")
        return self.screens[-1]
    def send(self, surface_id, message):
        self.sends.append((surface_id, message))
    def send_key(self, surface_id, key):
        self.keys.append((surface_id, key))

cmux = FakeCmux()
worker_results = []
worker = threading.Thread(
    target=lambda: worker_results.append(
        run(
            launch.spec_path,
            poll_seconds=0.02,
            checkpoint_probe=lambda _surface, _runtime: "checkpoint-v263",
            cmux_adapter=cmux,
        )
    )
)
worker.start()
ready_path = state_root / "ready.json"
ready_deadline = time.monotonic() + 2
while not ready_path.is_file() and time.monotonic() < ready_deadline:
    time.sleep(0.01)
ready = json.loads(ready_path.read_text(encoding="utf-8"))
resources = OwnedResources(
    "11111111-1111-4111-8111-111111111111",
    ready["process_group"],
    ready["supervisor_pid"],
    ready["process_identity"],
    ready["supervisor_identity"],
)
bind_deadline = time.monotonic() + 1
while True:
    try:
        supervisor.bind_resources(resources)
        break
    except StoreError as exc:
        if str(exc) != "stale operation writer" or time.monotonic() >= bind_deadline:
            raise
        time.sleep(0.01)
worker.join(timeout=3)
assert not worker.is_alive()
exit_code = worker_results[0]
record = store.read("v263-review-owner", "review-parent-1")
timeout_marker = state_root / "callback-timeout.json"
print(json.dumps({
    "executed_runtime_worker": str(Path(executed_runtime_worker.__file__).resolve()),
    "executed_runtime_liveness": str(Path(executed_runtime_liveness.__file__).resolve()),
    "exit_code": exit_code,
    "state": record.state,
    "attention_reason": (
        record.attention_reason.value if record.attention_reason else ""
    ),
    "provider_starts": (
        len(provider_marker.read_text(encoding="utf-8").splitlines())
        if provider_marker.is_file() else 0
    ),
    "provider_sends": len(cmux.sends),
    "provider_keys": len(cmux.keys),
    "provider_reads": cmux.reads,
    "provider_screens": cmux.screens,
    "broker_calls": broker_calls[0],
    "accepted_callback_id": record.accepted_callback_id,
    "input_exists": callback_path.with_name(".review-input.json").exists(),
    "callback_exists": callback_path.exists(),
    "receipt_exists": (state_root / "callback-receipt.json").exists(),
    "timeout_exists": timeout_marker.is_file(),
    "timeout_status": (
        json.loads(timeout_marker.read_text(encoding="utf-8"))["status"]
        if timeout_marker.is_file() else ""
    ),
}, sort_keys=True))
'''
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(snapshot / "scripts")
    environment["PYTHONNOUSERSITE"] = "1"
    executed = subprocess.run(
        [sys.executable, "-c", compatibility_runner, str(snapshot), str(root)],
        cwd=snapshot,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    try:
        observed = json.loads(executed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise AssertionError(
            f"v2.6.3 exact runtime did not return evidence: {executed.stderr}"
        ) from exc
    check(
        "exact v2.6.3 worker reproduces missing submit through callback timeout",
        executed.returncode == 0
        and observed["executed_runtime_worker"]
        == str((snapshot / "scripts/harness/runtime_worker.py").resolve())
        and observed["executed_runtime_liveness"]
        == str(
            (snapshot / "scripts/harness/runtime_worker_liveness.py").resolve()
        )
        and observed["exit_code"] == 0
        and observed["state"] == "attention-required"
        and observed["attention_reason"]
        == fixture["v2_6_3_observed"]["terminal_reason"]
        and observed["provider_starts"] == 1
        and observed["provider_reads"] >= 2
        and len(set(observed["provider_screens"])) == 1
        and classify_callback_prompt(
            "codex",
            observed["provider_screens"][-1],
            interactive=False,
            recognized=False,
        )
        == "idle-prompt"
        and observed["provider_sends"]
        == fixture["v2_6_3_observed"]["provider_sends"]
        and observed["provider_keys"] == 0
        and observed["broker_calls"]
        == fixture["v2_6_3_observed"]["callbacks_accepted"]
        and not observed["accepted_callback_id"]
        and not observed["input_exists"]
        and not observed["callback_exists"]
        and not observed["receipt_exists"]
        and observed["timeout_exists"]
        and observed["timeout_status"] == "attention-required",
        (observed, executed.stderr),
    )


with tempfile.TemporaryDirectory(prefix="superseded-review-cleanup.") as raw:
    root = Path(raw)
    store = OperationStore(root / "store")
    route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "high", "reviewer-callback", "7" * 64
    )
    old_spec = OperationSpec(
        "review-old-1",
        "review-old-key-1",
        "simple-review-holistic",
        "review-old-owner-1",
        route,
        "packets/old.json",
        "scoped",
    )
    new_spec = OperationSpec(
        "review-new-1",
        "review-new-key-1",
        "simple-review-holistic",
        "review-new-owner-1",
        route,
        "packets/new.json",
        "scoped",
    )
    store.create(old_spec, lane_id="openai-holistic", run_id="review-old-run-1")
    store.create(new_spec, lane_id="openai-holistic", run_id="review-new-run-1")
    old_supervisor = OperationSupervisor(
        store, "review-old-owner-1", "review-old-1"
    )
    for owner_id, operation_id in (
        ("review-old-owner-1", "review-old-1"),
        ("review-new-owner-1", "review-new-1"),
    ):
        for state in ("preflight", "starting"):
            store.transition(owner_id, operation_id, state)
    old_supervisor.bind_resources(
        OwnedResources(SURFACE, 401, 402, "4" * 64, "5" * 64)
    )
    for owner_id, operation_id in (
        ("review-old-owner-1", "review-old-1"),
        ("review-new-owner-1", "review-new-1"),
    ):
        store.transition(owner_id, operation_id, "running")
    old_record = store.read("review-old-owner-1", "review-old-1")
    old_sha256 = hashlib.sha256(
        json.dumps(to_dict(old_record), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    session = (
        store.root / "owners/review-old-owner-1/runtime/review-old-1/session.json"
    )
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": "review-old-1",
                "run_id": "review-old-run-1",
                "placement": "split",
            }
        ),
        encoding="utf-8",
    )
    (session.parent / "callback-target.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation": 1,
                "operation_id": "review-old-round-1",
                "run_id": "review-old-round-run-1",
                "callback_pointer": "callbacks/old.json",
            }
        ),
        encoding="utf-8",
    )
    authorization = {
        "schema_version": 1,
        "operation_id": "review-boundary-old-1",
        "kind": "context",
        "previous_context_sha256": "8" * 64,
        "next_context_sha256": "9" * 64,
        "reason": "authorized review context replacement",
        "authorization_provenance": "coordinator-approved",
        "verification_operation_id": "verification-old-1",
        "verification_receipt_sha256": "a" * 64,
        "status": "authorized",
    }
    authorization_path = store.root / "supersession/authorization.json"
    authorization_path.parent.mkdir()
    authorization_path.write_text(
        json.dumps(authorization, sort_keys=True) + "\n", encoding="utf-8"
    )
    authorization_sha256 = hashlib.sha256(
        authorization_path.read_bytes()
    ).hexdigest()
    receipt = {
        "schema_version": 1,
        "status": "authorized",
        "superseded_owner_id": "review-old-owner-1",
        "superseded_review_operation_id": "review-boundary-old-1",
        "superseded_operation_id": "review-old-1",
        "superseded_run_id": "review-old-run-1",
        "superseded_record_sha256": old_sha256,
        "replacement_owner_id": "review-new-owner-1",
        "replacement_review_operation_id": "review-boundary-new-1",
        "replacement_operation_id": "review-new-1",
        "replacement_run_id": "review-new-run-1",
        "store_sha256": hashlib.sha256(str(store.root).encode()).hexdigest(),
        "authorization_pointer": "supersession/authorization.json",
        "authorization_sha256": authorization_sha256,
    }
    receipt_path = store.root / "supersession/cleanup.json"
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8"
    )

    current_receipt = dict(receipt)
    current_receipt.update(
        {
            "superseded_owner_id": "review-new-owner-1",
            "superseded_review_operation_id": "review-boundary-new-1",
            "superseded_operation_id": "review-new-1",
            "superseded_run_id": "review-new-run-1",
            "replacement_owner_id": "review-new-owner-1",
            "replacement_review_operation_id": "review-boundary-new-1",
            "replacement_operation_id": "review-new-1",
            "replacement_run_id": "review-new-run-1",
        }
    )
    current_receipt_path = store.root / "supersession/current-cleanup.json"
    current_receipt_path.write_text(
        json.dumps(current_receipt, sort_keys=True) + "\n", encoding="utf-8"
    )

    class CleanupProcess:
        def __init__(self) -> None:
            self.status = "unknown"

        def process_status(self, process_group: int, identity: str) -> str:
            check(
                "superseded cleanup probes exact old process",
                process_group == 401 and identity == "4" * 64,
            )
            return self.status

        def pid_status(self, pid: int, identity: str) -> str:
            check(
                "superseded cleanup probes exact old supervisor",
                pid == 402 and identity == "5" * 64,
            )
            return self.status

    class CleanupCmux:
        def __init__(self) -> None:
            self.alive = True
            self.closed: list[str] = []

        def status(self, surface_id: str) -> str:
            check("superseded cleanup probes exact old surface", surface_id == SURFACE)
            return "alive" if self.alive else "missing"

        def close_exact(self, surface_id: str) -> None:
            self.closed.append(surface_id)
            self.alive = False

    cleanup_cmux = CleanupCmux()
    cleanup_process = CleanupProcess()
    manager = RuntimeSessionManager(store, cleanup_cmux, cleanup_process)
    try:
        manager.cleanup_superseded_review(current_receipt_path)
    except RuntimeSessionError:
        pass
    else:
        raise AssertionError("active review cleanup receipt was accepted")
    check(
        "current review identity never closes through superseded cleanup",
        cleanup_cmux.closed == []
        and store.read("review-new-owner-1", "review-new-1").state == "running",
    )
    ownership_wait = manager.cleanup_superseded_review(receipt_path)
    check(
        "unknown superseded ownership fails closed without close",
        ownership_wait.action == "attention-required"
        and ownership_wait.record.resources.surface_id == SURFACE
        and cleanup_cmux.closed == [],
        ownership_wait,
    )
    cleanup_process.status = "dead"
    cleaned = manager.cleanup_superseded_review(receipt_path)
    for state in ("finalizing", "exiting", "complete"):
        store.transition("review-new-owner-1", "review-new-1", state)
    replay = manager.cleanup_superseded_review(receipt_path)
    check(
        "authorized superseded review cleanup reaches resource-free terminal once",
        cleaned.record.state == "complete"
        and cleaned.record.resources == OwnedResources()
        and replay.record == cleaned.record
        and cleanup_cmux.closed == [SURFACE]
        and authorization_path.is_file(),
        (cleaned, replay, cleanup_cmux.closed),
    )


class FastPathWorker(RuntimeWorkerReviewBridgeMixin):
    @staticmethod
    def reserve_callback_submit(_generation: int) -> str:
        return ""

    @staticmethod
    def record_provider_result(_generation: int, _sha256: str) -> None:
        pass


class RecoveryWorker(RuntimeWorkerLivenessMixin):
    def inspect_callback(self) -> None:
        raise AssertionError("missing-artifact recovery must not ingest a callback")


class SymlinkWorker(RuntimeWorkerReviewBridgeMixin):
    @staticmethod
    def reserve_callback_submit(_generation: int) -> str:
        return ""

    @staticmethod
    def record_provider_result(_generation: int, _sha256: str) -> None:
        pass

    def summary_attention(
        self,
        status: str,
        reason: AttentionReason = AttentionReason.CALLBACK_INVALID,
        *,
        write_error: bool = True,
    ) -> None:
        self.callback_handled = True
        self.store.transition(
            self.spec["owner_id"],
            self.spec["operation_id"],
            "attention-required",
            reason=reason,
        )
        self.attention_statuses.append(status)


with tempfile.TemporaryDirectory(prefix="callback-submit-attention-replay.") as raw:
    attention_root = Path(raw)
    attention_store = OperationStore(attention_root / "store")
    attention_route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "high", "reviewer-callback", "a" * 64
    )
    attention_spec = OperationSpec(
        "attention-parent",
        "attention-key",
        "review-session",
        "attention-owner",
        attention_route,
        "packets/review.json",
        "scoped",
    )
    attention_store.create(
        attention_spec, lane_id="openai-holistic", run_id="attention-run"
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        attention_store.transition("attention-owner", "attention-parent", state)

    attention_worker = RecoveryWorker()
    attention_worker.spec_path = attention_root / "worker" / "launch.json"
    attention_worker.spec_path.parent.mkdir()
    attention_worker.spec = {
        "owner_id": "attention-owner",
        "operation_id": "attention-parent",
        "run_id": "attention-run",
    }

    class FailOnceTransitionStore:
        def __init__(self, store: OperationStore) -> None:
            self.store = store
            self.failed = False

        def read(self, owner_id: str, operation_id: str):
            return self.store.read(owner_id, operation_id)

        def transition(self, *args, **kwargs):
            if not self.failed:
                self.failed = True
                raise OSError("simulated store transition failure")
            return self.store.transition(*args, **kwargs)

    flaky_store = FailOnceTransitionStore(attention_store)
    attention_worker.store = flaky_store
    try:
        attention_worker.callback_submit_attention(
            "callback-submit-stale-generation"
        )
    except Exception as exc:
        check(
            "attention transition failure is surfaced to the owning worker",
            "attention transition failed" in str(exc),
            exc,
        )
    else:
        raise AssertionError("attention transition failure was swallowed")
    check(
        "failed transition leaves a durable typed marker without false state",
        (
            attention_worker.spec_path.parent
            / "callback-submit-attention.json"
        ).is_file()
        and attention_store.read(
            "attention-owner", "attention-parent"
        ).state
        == "awaiting-callback",
    )
    attention_worker.callback_submit_attention(
        "callback-submit-stale-generation"
    )
    attention_record = attention_store.read(
        "attention-owner", "attention-parent"
    )
    check(
        "matching marker replay still reaches durable operation attention",
        attention_record.state == "attention-required",
        attention_record,
    )


with tempfile.TemporaryDirectory(prefix="callback-pointer-symlink.") as raw:
    root = Path(raw)
    scratch = (root / "scratch").resolve()
    product = (root / "product").resolve()
    state_root = (root / "worker-state").resolve()
    callback_dir = scratch / "callbacks" / "openai-holistic"
    for directory in (scratch, product, state_root, callback_dir):
        directory.mkdir(parents=True, exist_ok=True)
    target_path = callback_dir / "target.json"
    target_path.write_text("{}\n", encoding="utf-8")
    callback_path = callback_dir / ".review-callback.json"
    callback_path.symlink_to(target_path)
    registration = state_root / "callback-target.json"
    registration.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation": 1,
                "operation_id": "symlink-round-1",
                "run_id": "symlink-round-run-1",
                "callback_pointer": str(callback_path),
            }
        ),
        encoding="utf-8",
    )
    route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "high", "reviewer-callback", "b" * 64
    )
    store = OperationStore(root / "store")
    parent = OperationSpec(
        "symlink-parent-1",
        "symlink-parent-key-1",
        "simple-review-holistic",
        "symlink-owner-1",
        route,
        "packets/review.json",
        "scoped",
    )
    child = OperationSpec(
        "symlink-round-1",
        "symlink-round-key-1",
        "review-round",
        "symlink-owner-1",
        route,
        "packets/review.json",
        "scoped",
    )
    store.create(parent, lane_id="openai-holistic", run_id="symlink-parent-run-1")
    store.create(child, lane_id="openai-holistic", run_id="symlink-round-run-1")
    for operation_id in ("symlink-parent-1", "symlink-round-1"):
        for state in ("preflight", "starting", "running", "awaiting-callback"):
            store.transition("symlink-owner-1", operation_id, state)
    cmux_effects: list[object] = []
    worker = SymlinkWorker()
    worker.spec_path = state_root / "launch.json"
    worker.spec = {
        "owner_id": "symlink-owner-1",
        "operation_id": "symlink-parent-1",
        "run_id": "symlink-parent-run-1",
        "cwd": scratch,
        "product_root": product,
        "callback_registration": registration,
    }
    worker.store = store
    worker.trusted_vault = ROOT
    worker.active_target = None
    worker.last_digest = ""
    worker.stable_reads = 0
    worker.review_input_digest = ""
    worker.review_input_stable_reads = 0
    worker.callback_handled = False
    worker.registration_invalid = False
    worker.cmux_adapter = cmux_effects
    worker.attention_statuses = []
    worker.inspect_callback()
    worker.inspect_callback()
    parent_record = store.read("symlink-owner-1", "symlink-parent-1")
    child_record = store.read("symlink-owner-1", "symlink-round-1")
    check(
        "symlink callback pointer fails closed with zero runtime effects",
        callback_path.is_symlink()
        and worker.attention_statuses == ["callback-artifact-invalid"]
        and parent_record.state == "attention-required"
        and child_record.state == "awaiting-callback"
        and not child_record.accepted_callback_id
        and not (state_root / "callback-receipt.json").exists()
        and not (callback_dir / ".review-input.json").exists()
        and cmux_effects == [],
        (parent_record, child_record, worker.attention_statuses),
    )

    aliased_parent = scratch / "aliased-callbacks"
    aliased_parent.symlink_to(callback_dir, target_is_directory=True)
    parent_registration = state_root / "parent-symlink-target.json"
    parent_registration.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation": 2,
                "operation_id": "symlink-round-1",
                "run_id": "symlink-round-run-1",
                "callback_pointer": str(
                    aliased_parent / ".review-callback.json"
                ),
            }
        ),
        encoding="utf-8",
    )
    try:
        _callback_target(
            {"cwd": scratch, "callback_registration": parent_registration}
        )
    except Exception as exc:
        parent_rejected = "parent is a symlink" in str(exc)
    else:
        parent_rejected = False
    check(
        "symlinked callback parent is rejected before artifact observation",
        parent_rejected,
    )


with tempfile.TemporaryDirectory(prefix="review-input-runtime.") as raw:
    root = Path(raw)
    scratch = (root / "scratch").resolve()
    product = (root / "product").resolve()
    state_root = (root / "worker-state").resolve()
    scratch.mkdir()
    product.mkdir()
    state_root.mkdir()
    callback_dir = scratch / "callbacks" / "openai-holistic"
    callback_dir.mkdir(parents=True)
    callback_path = callback_dir / ".review-callback.json"
    input_path = callback_dir / ".review-input.json"
    registration = state_root / "callback-target.json"
    registration.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation": 3,
                "operation_id": "review-round-1",
                "run_id": "review-round-run-1",
                "callback_pointer": str(callback_path),
            }
        ),
        encoding="utf-8",
    )
    (callback_dir / ".review-meta.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "transport": "review-round",
                "operation_id": "review-round-1",
                "run_id": "review-round-run-1",
                "review_id": "review-parent-1",
                "parent_session_operation_id": "review-parent-1",
                "axis": "openai-holistic",
                "verification_iteration": 0,
                "verification_profile": {
                    "name": "scoped",
                    "sha256": "d" * 64,
                },
                "worktree": str(product),
            }
        ),
        encoding="utf-8",
    )
    input_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "axis": "openai-holistic",
                "verdict": "approve",
                "verification_iteration": 0,
                "findings": [],
            }
        ),
        encoding="utf-8",
    )
    route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "high", "reviewer-callback", "e" * 64
    )
    store = OperationStore(root / "store")
    parent = OperationSpec(
        "review-parent-1",
        "review-parent-key-1",
        "review-session",
        "review-owner-1",
        route,
        "packets/review.json",
        "scoped",
    )
    child = OperationSpec(
        "review-round-1",
        "review-round-key-1",
        "review-round",
        "review-owner-1",
        route,
        "packets/review.json",
        "scoped",
    )
    store.create(parent, lane_id="openai-holistic", run_id="review-parent-run-1")
    OperationSupervisor(store, "review-owner-1", "review-parent-1").configure_budget(
        attempt_limit=1,
        model_restart_limit=0,
        time_budget_seconds=3600,
        token_limit=100,
        now=time.time(),
    )
    store.create(child, lane_id="openai-holistic", run_id="review-round-run-1")
    for operation_id in ("review-parent-1", "review-round-1"):
        for state in ("preflight", "starting", "running", "awaiting-callback"):
            store.transition("review-owner-1", operation_id, state)
    parent_record = store.read("review-owner-1", "review-parent-1")
    store.save(
        replace(
            parent_record,
            deadline_at=1.0,
            revision=parent_record.revision + 1,
        ),
        expected_revision=parent_record.revision,
    )
    store.transition(
        "review-owner-1",
        "review-parent-1",
        "attention-required",
        reason=AttentionReason.CALLBACK_TIMEOUT,
    )
    session = (
        store.root
        / "owners/review-owner-1/runtime/review-parent-1/session.json"
    )
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": "review-parent-1",
                "run_id": "review-parent-run-1",
                "time_budget_seconds": 3600,
            }
        ),
        encoding="utf-8",
    )
    worker = FastPathWorker()
    worker.spec_path = state_root / "launch.json"
    worker.spec = {
        "owner_id": "review-owner-1",
        "operation_id": "review-parent-1",
        "run_id": "review-parent-run-1",
        "cwd": scratch,
        "product_root": product,
        "callback_registration": registration,
    }
    worker.store = store
    worker.trusted_vault = ROOT
    worker.active_target = None
    worker.last_digest = ""
    worker.stable_reads = 0
    worker.review_input_digest = ""
    worker.review_input_stable_reads = 0
    worker.callback_handled = False
    worker.registration_invalid = False
    worker.cmux_adapter = object()
    worker.inspect_callback()
    worker.inspect_callback()
    worker.inspect_callback()
    accepted = store.read("review-owner-1", "review-round-1")
    rearmed = store.read("review-owner-1", "review-parent-1")
    rearm_receipt = json.loads(
        (state_root / "callback-timeout-rearm.json").read_text(encoding="utf-8")
    )
    check(
        "runtime rearms only to ingest a stable typed callback without model input",
        accepted.state == "finalizing"
        and bool(accepted.accepted_callback_id)
        and rearmed.state == "awaiting-callback"
        and rearm_receipt["status"] == "accepted"
        and (state_root / "callback-receipt.json").is_file()
        and not input_path.exists(),
        accepted,
    )
    rearm_receipt["status"] = "prepared"
    (state_root / "callback-timeout-rearm.json").write_text(
        json.dumps(rearm_receipt, sort_keys=True) + "\n", encoding="utf-8"
    )
    worker.callback_handled = False
    worker.inspect_callback()
    repaired_rearm = json.loads(
        (state_root / "callback-timeout-rearm.json").read_text(encoding="utf-8")
    )
    check(
        "accepted callback replay repairs a prepared rearm receipt without effect",
        repaired_rearm["status"] == "accepted"
        and json.loads(
            (state_root / "callback-receipt.json").read_text(encoding="utf-8")
        )["status"]
        == "duplicate",
        repaired_rearm,
    )


with tempfile.TemporaryDirectory(prefix="review-submit-nudge-runtime.") as raw:
    root = Path(raw)
    scratch = (root / "scratch").resolve()
    product = (root / "product").resolve()
    state_root = (root / "worker-state").resolve()
    scratch.mkdir()
    product.mkdir()
    state_root.mkdir()
    callback_dir = scratch / "callbacks" / "openai-holistic"
    callback_dir.mkdir(parents=True)
    callback_path = callback_dir / ".review-callback.json"
    registration = state_root / "callback-target.json"
    registration.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation": 3,
                "operation_id": "review-round-3",
                "run_id": "review-round-run-3",
                "callback_pointer": str(callback_path),
            }
        ),
        encoding="utf-8",
    )
    now = time.time()
    os.utime(registration, (now - 60, now - 60))
    route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "high", "reviewer-callback", "f" * 64
    )
    store = OperationStore(root / "store")
    parent = OperationSpec(
        "review-parent-3",
        "review-parent-key-3",
        "review-session",
        "review-owner-3",
        route,
        "packets/review.json",
        "scoped",
    )
    child = OperationSpec(
        "review-round-3",
        "review-round-key-3",
        "review-round",
        "review-owner-3",
        route,
        "packets/review.json",
        "scoped",
    )
    store.create(parent, lane_id="openai-holistic", run_id="review-parent-run-3")
    store.create(child, lane_id="openai-holistic", run_id="review-round-run-3")
    supervisor = OperationSupervisor(store, "review-owner-3", "review-parent-3")
    supervisor.configure_budget(
        attempt_limit=1,
        model_restart_limit=0,
        time_budget_seconds=300,
        token_limit=100,
        now=now,
    )
    for state in ("preflight", "starting"):
        store.transition("review-owner-3", "review-parent-3", state)
    supervisor.bind_resources(
        OwnedResources(
            SURFACE,
            321,
            322,
            "1" * 64,
            "2" * 64,
        )
    )
    for state in ("running", "awaiting-callback"):
        store.transition("review-owner-3", "review-parent-3", state)
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition("review-owner-3", "review-round-3", state)
    wrong_lane_spec = OperationSpec(
        "wrong-lane-round-3",
        "wrong-lane-round-key-3",
        "review-round",
        "review-owner-3",
        route,
        "packets/review.json",
        "scoped",
    )
    store.create(
        wrong_lane_spec, lane_id="other-lane", run_id="wrong-lane-round-run-3"
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition("review-owner-3", "wrong-lane-round-3", state)
    terminal_spec = OperationSpec(
        "terminal-round-3",
        "terminal-round-key-3",
        "review-round",
        "review-owner-3",
        route,
        "packets/review.json",
        "scoped",
    )
    store.create(
        terminal_spec,
        lane_id="openai-holistic",
        run_id="terminal-round-run-3",
    )
    terminal = store.read("review-owner-3", "terminal-round-3")
    store.save(
        replace(terminal, state="cancelled", revision=terminal.revision + 1),
        expected_revision=terminal.revision,
    )

    lifecycle_effects: list[str] = []

    class Process:
        def __init__(self, effects: list[str] | None = None) -> None:
            self.status = "alive"
            self.exit_requests = 0
            self.effects = effects if effects is not None else []

        def process_status(self, process_group: int, identity: str) -> str:
            check(
                "recovery process probe uses exact ownership",
                process_group == 321 and identity == "1" * 64,
            )
            return self.status

        def pid_status(self, supervisor_pid: int, identity: str) -> str:
            check(
                "recovery supervisor probe uses exact ownership",
                supervisor_pid == 322 and identity == "2" * 64,
            )
            return self.status

        def request_guardian_signal(self, _control_path: Path, **identity) -> None:
            check(
                "harness exit uses exact retained reviewer ownership",
                identity["action"] == "request-exit"
                and identity["operation_id"] == "review-parent-3"
                and identity["run_id"] == "review-parent-run-3"
                and identity["process_group"] == 321
                and identity["process_identity"] == "1" * 64
                and identity["supervisor_pid"] == 322
                and identity["supervisor_identity"] == "2" * 64,
                identity,
            )
            self.exit_requests += 1
            self.effects.append("harness-request-exit")
            self.status = "dead"

    class Cmux:
        def __init__(self, effects: list[str] | None = None) -> None:
            self.sent: list[tuple[str, str]] = []
            self.keys: list[tuple[str, str]] = []
            self.reads = 0
            self.provider_artifact_writes = 0
            self.closed: list[str] = []
            self.effects = effects if effects is not None else []

        def status(self, surface_id: str) -> str:
            check("recovery surface probe uses exact ownership", surface_id == SURFACE)
            return "missing" if surface_id in self.closed else "alive"

        def close_exact(self, surface_id: str) -> None:
            check("harness closes only the exact review surface", surface_id == SURFACE)
            if surface_id not in self.closed:
                self.closed.append(surface_id)
                self.effects.append("harness-close-surface")

        def send(self, surface_id: str, message: str) -> None:
            self.sent.append((surface_id, message))
            self.effects.append("harness-provider-prompt")

        def read(self, surface_id: str) -> str:
            check("recovery screen read uses exact surface", surface_id == SURFACE)
            self.reads += 1
            return "›"

        def send_key(self, surface_id: str, key: str) -> None:
            self.keys.append((surface_id, key))
            self.effects.append("harness-provider-enter")

        def provider_complete_review(self, path: Path, payload: object) -> None:
            """Fake-provider boundary: model output appears only after Enter."""

            if self.keys != [(SURFACE, "Enter")]:
                raise AssertionError("fake provider completed before one submit")
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.provider_artifact_writes += 1
            self.effects.append("provider-callback-write")

    cmux = Cmux(lifecycle_effects)
    worker = RecoveryWorker()
    worker.spec_path = state_root / "launch.json"
    worker.spec = {
        "owner_id": "review-owner-3",
        "operation_id": "review-parent-3",
        "run_id": "review-parent-run-3",
        "cwd": scratch,
        "product_root": product,
        "callback_registration": registration,
        "surface_id": SURFACE,
        "runtime": "codex",
    }
    worker.store = store
    process = Process(lifecycle_effects)
    worker.process = process
    worker.handle = SimpleNamespace(process_group=321, process_identity="1" * 64)
    worker.provider_exited = False
    worker.cmux_adapter = cmux
    worker.clock = lambda: now
    worker.liveness_policy = LivenessPolicy.default()
    worker.callback_submit_policy = CallbackSubmitPolicy(30, 60, 1)
    worker.liveness_controller = LivenessController(state_root / "liveness")
    worker.latest_callback_prompt_class = "unknown"
    worker.callback_idle_observations = 0
    worker.callback_prompt_observations = 0
    worker.callback_generation_identity = ""
    worker.callback_generation_progress_at = 0.0
    worker.callback_recovery_input_digest = ""
    worker.callback_recovery_input_reads = 0
    worker.callback_recovery_digest = ""
    worker.callback_recovery_reads = 0
    worker.trusted_vault = ROOT
    check(
        "runtime resolves callback child independently from target JSON",
        worker._expected_callback_child(
            store.read("review-owner-3", "review-parent-3"),
            "review-round-3",
            "review-round-run-3",
        )
        is not None
        and worker._expected_callback_child(
            store.read("review-owner-3", "review-parent-3"),
            "missing-round-3",
            "missing-round-run-3",
        )
        is None
        and worker._expected_callback_child(
            store.read("review-owner-3", "review-parent-3"),
            "review-round-3",
            "wrong-run-3",
        )
        is None
        and worker._expected_callback_child(
            store.read("review-owner-3", "review-parent-3"),
            "wrong-lane-round-3",
            "wrong-lane-round-run-3",
        )
        is None
        and worker._expected_callback_child(
            store.read("review-owner-3", "review-parent-3"),
            "terminal-round-3",
            "terminal-round-run-3",
        )
        is None,
    )
    worker.inspect_liveness()
    worker.inspect_liveness()
    worker.inspect_liveness()
    recovery_state = worker.liveness_controller.current_state()
    check(
        "runtime reserves and sends one same-session submit-only nudge",
        len(cmux.sent) == 1
        and cmux.keys == [(SURFACE, "Enter")]
        and cmux.reads >= 2
        and worker.latest_callback_prompt_class == "idle-prompt"
        and ".review-input.json" in cmux.sent[0][1]
        and "review_submit.py" in cmux.sent[0][1]
        and recovery_state is not None
        and recovery_state.nudge_count == 1
        and recovery_state.callback_submit_status == "sent",
        (cmux.sent, cmux.keys, recovery_state),
    )

    submit_receipt_path = (
        state_root
        / "liveness"
        / "receipts"
        / f"callback-submit-{recovery_state.callback_submit_binding}.json"
    )
    mixed_receipt = json.loads(submit_receipt_path.read_text(encoding="utf-8"))
    mixed_receipt["status"] = "reserved"
    submit_receipt_path.write_text(
        json.dumps(mixed_receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    replay_cmux = Cmux()
    replay_worker = RecoveryWorker()
    replay_worker.__dict__.update(worker.__dict__)
    replay_worker.liveness_controller = LivenessController(
        state_root / "liveness"
    )
    replay_worker.cmux_adapter = replay_cmux
    replay_worker.inspect_liveness()
    healed_receipt = json.loads(
        submit_receipt_path.read_text(encoding="utf-8")
    )
    check(
        "runtime restart heals sent state with reserved receipt without replay",
        healed_receipt["status"] == "sent"
        and replay_cmux.sent == []
        and replay_cmux.keys == [],
        (healed_receipt, replay_cmux.sent, replay_cmux.keys),
    )

    class SentReceiptAttentionWorker(RecoveryWorker):
        def callback_submit_attention(self, reason: str) -> None:
            self.attention_reason = reason

    submit_receipt_path.write_text("not-json\n", encoding="utf-8")
    attention_cmux = Cmux()
    attention_worker = SentReceiptAttentionWorker()
    attention_worker.__dict__.update(worker.__dict__)
    attention_worker.liveness_controller = LivenessController(
        state_root / "liveness"
    )
    attention_worker.cmux_adapter = attention_cmux
    attention_worker.inspect_liveness()
    check(
        "malformed sent recovery receipt raises typed attention without effect",
        attention_worker.attention_reason
        == "callback-submit-evidence-malformed"
        and attention_cmux.sent == []
        and attention_cmux.keys == [],
        (
            attention_worker.attention_reason,
            attention_cmux.sent,
            attention_cmux.keys,
        ),
    )
    submit_receipt_path.write_text(
        json.dumps(healed_receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    class AttentionOnlyRecoveryWorker(RecoveryWorker):
        def callback_submit_attention(self, reason: str) -> None:
            self.attention_reason = reason

    class CrashCmux(Cmux):
        def __init__(self, phase: str) -> None:
            super().__init__()
            self.phase = phase

        def send(self, surface_id: str, message: str) -> None:
            super().send(surface_id, message)
            if self.phase == "paste":
                raise KeyboardInterrupt("simulated hard crash after paste")

        def send_key(self, surface_id: str, key: str) -> None:
            super().send_key(surface_id, key)
            if self.phase == "enter":
                raise KeyboardInterrupt("simulated hard crash after Enter")

    for crash_phase in ("paste", "enter"):
        crash_root = root / f"crash-{crash_phase}"
        crash_root.mkdir()
        crash_worker = AttentionOnlyRecoveryWorker()
        crash_worker.__dict__.update(worker.__dict__)
        crash_worker.spec_path = crash_root / "launch.json"
        crash_worker.liveness_controller = LivenessController(
            crash_root / "liveness"
        )
        crash_worker.callback_idle_observations = 0
        crash_worker.callback_prompt_observations = 0
        crash_worker.callback_generation_identity = ""
        crash_worker.callback_generation_progress_at = 0.0
        crash_worker.callback_recovery_input_digest = ""
        crash_worker.callback_recovery_input_reads = 0
        crash_worker.callback_recovery_digest = ""
        crash_worker.callback_recovery_reads = 0
        crashing_cmux = CrashCmux(crash_phase)
        crash_worker.cmux_adapter = crashing_cmux
        try:
            crash_worker.inspect_liveness()
            crash_worker.inspect_liveness()
            crash_worker.inspect_liveness()
        except KeyboardInterrupt:
            pass
        crash_state = crash_worker.liveness_controller.current_state()
        replay_cmux = Cmux()
        crash_worker.cmux_adapter = replay_cmux
        crash_worker.inspect_liveness()
        check(
            f"hard crash after {crash_phase} leaves ambiguous reservation without replay",
            crash_state is not None
            and crash_state.callback_submit_status == "reserved"
            and crash_worker.attention_reason
            == "callback-submit-effect-uncertain"
            and replay_cmux.sent == []
            and replay_cmux.keys == [],
            (crash_state, replay_cmux.sent, replay_cmux.keys),
        )

    swapped_state_root = root / "swapped-worker-state"
    swapped_state_root.mkdir()
    registration.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation": 3,
                "operation_id": "review-round-3",
                "run_id": "review-round-run-3",
                "callback_pointer": str(callback_path),
            }
        ),
        encoding="utf-8",
    )
    os.utime(registration, (now - 60, now - 60))

    class SwappingCmux(Cmux):
        def __init__(self) -> None:
            super().__init__()
            self.status_calls = 0

        def status(self, surface_id: str) -> str:
            self.status_calls += 1
            if self.status_calls == 2:
                registration.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "generation": 4,
                            "operation_id": "review-round-3",
                            "run_id": "review-round-run-3",
                            "callback_pointer": str(callback_path),
                        }
                    ),
                    encoding="utf-8",
                )
            return super().status(surface_id)

    class NonMutatingSwapWorker(RecoveryWorker):
        def callback_submit_attention(self, reason: str) -> None:
            self.attention_reason = reason

    swapping_cmux = SwappingCmux()
    swapping_worker = NonMutatingSwapWorker()
    swapping_worker.spec_path = swapped_state_root / "launch.json"
    swapping_worker.spec = {**worker.spec, "callback_registration": registration}
    swapping_worker.store = store
    swapping_worker.process = Process()
    swapping_worker.handle = worker.handle
    swapping_worker.provider_exited = False
    swapping_worker.cmux_adapter = swapping_cmux
    swapping_worker.clock = lambda: now
    swapping_worker.liveness_policy = LivenessPolicy.default()
    swapping_worker.callback_submit_policy = CallbackSubmitPolicy(30, 60, 1)
    swapping_worker.liveness_controller = LivenessController(
        swapped_state_root / "liveness"
    )
    swapping_worker.latest_callback_prompt_class = "idle-prompt"
    swapping_worker.callback_idle_observations = 0
    swapping_worker.callback_prompt_observations = 0
    swapping_worker.callback_generation_identity = ""
    swapping_worker.callback_generation_progress_at = 0.0
    swapping_worker.callback_recovery_input_digest = ""
    swapping_worker.callback_recovery_input_reads = 0
    swapping_worker.callback_recovery_digest = ""
    swapping_worker.callback_recovery_reads = 0
    swapping_worker.trusted_vault = ROOT
    swapping_worker.inspect_liveness()
    swapping_worker.inspect_liveness()
    swapping_worker.inspect_liveness()
    check(
        "target swap before reservation has zero provider effect",
        swapping_cmux.sent == []
        and swapping_cmux.keys == []
        and swapping_worker.attention_reason
        == "callback-submit-stale-generation",
        (swapping_cmux.sent, swapping_cmux.keys),
    )

    registration.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation": 3,
                "operation_id": "review-round-3",
                "run_id": "review-round-run-3",
                "callback_pointer": str(callback_path),
            }
        ),
        encoding="utf-8",
    )
    (callback_dir / ".review-meta.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "transport": "review-round",
                "operation_id": "review-round-3",
                "run_id": "review-round-run-3",
                "review_id": "review-parent-3",
                "parent_session_operation_id": "review-parent-3",
                "axis": "openai-holistic",
                "verification_iteration": 0,
                "verification_profile": {
                    "name": "scoped",
                    "sha256": "d" * 64,
                },
                "worktree": str(product),
            }
        ),
        encoding="utf-8",
    )
    input_path = callback_dir / ".review-input.json"
    cmux.provider_complete_review(
        input_path,
        {
            "schema_version": 1,
            "axis": "openai-holistic",
            "verdict": "approve",
            "verification_iteration": 0,
            "findings": [],
        },
    )
    callback_worker = FastPathWorker()
    callback_worker.spec_path = state_root / "launch.json"
    callback_worker.spec = worker.spec
    callback_worker.store = store
    callback_worker.trusted_vault = ROOT
    callback_worker.active_target = None
    callback_worker.last_digest = ""
    callback_worker.stable_reads = 0
    callback_worker.review_input_digest = ""
    callback_worker.review_input_stable_reads = 0
    callback_worker.callback_handled = False
    callback_worker.registration_invalid = False
    callback_worker.cmux_adapter = cmux
    callback_worker.inspect_callback()
    callback_worker.inspect_callback()
    callback_worker.inspect_callback()
    joined_child = store.read("review-owner-3", "review-round-3")
    joined_receipt = json.loads(
        (state_root / "callback-receipt.json").read_text(encoding="utf-8")
    )
    joined_envelope = CallbackEnvelope(
        **json.loads(callback_path.read_text(encoding="utf-8"))
    )
    check(
        "one unattended missing-submit incident reaches accepted next stage",
        len(cmux.sent) == 1
        and cmux.keys == [(SURFACE, "Enter")]
        and joined_child.state == "finalizing"
        and bool(joined_child.accepted_callback_id)
        and joined_receipt["status"] == "accepted"
        and not callback_worker.registration_invalid,
        (cmux.sent, cmux.keys, joined_child, joined_receipt),
    )
    accepted_mixed_receipt = {
        **json.loads(submit_receipt_path.read_text(encoding="utf-8")),
        "status": "reserved",
    }
    submit_receipt_path.write_text(
        json.dumps(accepted_mixed_receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    worker.inspect_liveness()
    accepted_parent = store.read("review-owner-3", "review-parent-3")
    accepted_healed_receipt = json.loads(
        submit_receipt_path.read_text(encoding="utf-8")
    )
    check(
        "accepted callback first heals the sent/reserved crash phase without replay",
        accepted_parent.state == "awaiting-callback"
        and accepted_healed_receipt["status"] == "sent"
        and not (state_root / "callback-submit-attention.json").exists()
        and len(cmux.sent) == 1
        and cmux.keys == [(SURFACE, "Enter")],
        (
            accepted_parent,
            accepted_healed_receipt,
            cmux.sent,
            cmux.keys,
        ),
    )

    next_child = OperationSpec(
        "review-round-4",
        "review-round-key-4",
        "review-round",
        "review-owner-3",
        route,
        "packets/review.json",
        "scoped",
    )
    store.create(
        next_child,
        lane_id="openai-holistic",
        run_id="review-round-run-4",
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition("review-owner-3", "review-round-4", state)
    callback_path.unlink(missing_ok=True)
    registration.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation": 4,
                "operation_id": "review-round-4",
                "run_id": "review-round-run-4",
                "callback_pointer": str(callback_path),
            }
        ),
        encoding="utf-8",
    )
    os.utime(registration, (now, now))

    stale_state_root = root / "stale-receipt-worker"
    stale_state_root.mkdir()
    stale_registration = stale_state_root / "callback-target.json"
    stale_registration.write_bytes(registration.read_bytes())
    stale_receipt = {**joined_receipt, "status": "accepted"}
    (stale_state_root / "callback-receipt.json").write_text(
        json.dumps(stale_receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.utime(stale_registration, (now - 60, now - 60))
    stale_cmux = Cmux()
    stale_worker = RecoveryWorker()
    stale_worker.__dict__.update(worker.__dict__)
    stale_worker.spec_path = stale_state_root / "launch.json"
    stale_worker.spec = {
        **worker.spec,
        "callback_registration": stale_registration,
    }
    stale_worker.liveness_controller = LivenessController(
        stale_state_root / "liveness"
    )
    stale_worker.liveness_controller.observe(
        LivenessEvidence(
            observed_at=now - 120,
            process_status="alive",
            operation_revision=8,
            operation_state="starting",
        ),
        stale_worker.liveness_policy,
    )
    stale_worker.cmux_adapter = stale_cmux
    stale_worker.callback_idle_observations = 0
    stale_worker.callback_prompt_observations = 0
    stale_worker.callback_generation_identity = ""
    stale_worker.callback_generation_progress_at = 0.0
    stale_worker.callback_recovery_input_digest = ""
    stale_worker.callback_recovery_input_reads = 0
    stale_worker.callback_recovery_digest = ""
    stale_worker.callback_recovery_reads = 0
    stale_worker.latest_callback_prompt_class = "idle-prompt"
    stale_worker.inspect_liveness()
    stale_worker.inspect_liveness()
    stale_worker.inspect_liveness()
    stale_recovery_state = stale_worker.liveness_controller.current_state()
    check(
        "stale prior-generation callback receipt does not strand current recovery",
        len(stale_cmux.sent) == 1
        and stale_cmux.keys == [(SURFACE, "Enter")]
        and stale_recovery_state is not None
        and stale_recovery_state.operation_revision
        == store.read("review-owner-3", "review-parent-3").revision
        and stale_recovery_state.operation_state == "awaiting-callback"
        and stale_recovery_state.nudge_count == 1
        and stale_recovery_state.callback_submit_status == "sent"
        and not (stale_state_root / "callback-submit-attention.json").exists(),
        (stale_cmux.sent, stale_cmux.keys, stale_recovery_state),
    )

    race_state_root = root / "artifact-race-worker"
    race_state_root.mkdir()
    race_registration = race_state_root / "callback-target.json"
    race_registration.write_bytes(registration.read_bytes())
    os.utime(race_registration, (now - 60, now - 60))
    input_path.unlink(missing_ok=True)
    callback_path.unlink(missing_ok=True)

    class ArtifactRaceController(LivenessController):
        def reserve_callback_submit(
            self, binding_sha256: str, identity: dict[str, object]
        ) -> bool:
            reserved = super().reserve_callback_submit(
                binding_sha256, identity
            )
            if reserved:
                input_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "axis": "openai-holistic",
                            "verdict": "approve",
                            "verification_iteration": 1,
                            "findings": [],
                        }
                    ),
                    encoding="utf-8",
                )
            return reserved

    class ArtifactRaceWorker(RecoveryWorker):
        def inspect_callback(self) -> None:
            self.callback_inspections += 1

    race_cmux = Cmux()
    race_worker = ArtifactRaceWorker()
    race_worker.__dict__.update(worker.__dict__)
    race_worker.spec_path = race_state_root / "launch.json"
    race_worker.spec = {
        **worker.spec,
        "callback_registration": race_registration,
    }
    race_worker.liveness_controller = ArtifactRaceController(
        race_state_root / "liveness"
    )
    race_worker.cmux_adapter = race_cmux
    race_worker.callback_idle_observations = 0
    race_worker.callback_prompt_observations = 0
    race_worker.callback_generation_identity = ""
    race_worker.callback_generation_progress_at = 0.0
    race_worker.callback_recovery_input_digest = ""
    race_worker.callback_recovery_input_reads = 0
    race_worker.callback_recovery_digest = ""
    race_worker.callback_recovery_reads = 0
    race_worker.latest_callback_prompt_class = "idle-prompt"
    race_worker.callback_inspections = 0
    race_worker.inspect_liveness()
    race_worker.inspect_liveness()
    race_worker.inspect_liveness()
    race_worker.inspect_liveness()
    race_state = race_worker.liveness_controller.current_state()
    race_receipts = list(
        (race_state_root / "liveness" / "receipts").glob(
            "callback-submit-*.json"
        )
    )
    race_receipt = json.loads(race_receipts[0].read_text(encoding="utf-8"))
    check(
        "typed artifact winning after reservation prevents provider replay",
        race_cmux.sent == []
        and race_cmux.keys == []
        and race_worker.callback_inspections >= 1
        and race_state is not None
        and race_state.nudge_count == 1
        and race_state.callback_submit_binding == ""
        and race_state.callback_submit_status == ""
        and race_receipt["status"] == "settled-by-artifact"
        and race_receipt["artifact_sha256"]
        == hashlib.sha256(input_path.read_bytes()).hexdigest()
        and not (race_state_root / "callback-submit-attention.json").exists(),
        (
            race_cmux.sent,
            race_cmux.keys,
            race_worker.callback_inspections,
            race_state,
            race_receipt,
            hashlib.sha256(input_path.read_bytes()).hexdigest(),
        ),
    )
    input_path.unlink(missing_ok=True)

    worker.latest_callback_prompt_class = "active"
    worker.inspect_liveness()
    worker.inspect_liveness()
    rollover_state = worker.liveness_controller.current_state()
    rollover_parent = store.read("review-owner-3", "review-parent-3")
    retired_submit_receipt = json.loads(
        (
            state_root
            / "liveness"
            / "receipts"
            / (
                "callback-submit-"
                f"{recovery_state.callback_submit_binding}.json"
            )
        ).read_text(encoding="utf-8")
    )
    prior_callback_receipt_sha256 = hashlib.sha256(
        (state_root / "callback-receipt.json").read_bytes()
    ).hexdigest()
    check(
        "accepted recovery retires only its binding before an active next generation",
        rollover_parent.state == "awaiting-callback"
        and not (state_root / "callback-submit-attention.json").exists()
        and rollover_state is not None
        and rollover_state.nudge_count == 1
        and rollover_state.callback_submit_binding == ""
        and rollover_state.callback_submit_status == ""
        and retired_submit_receipt["status"] == "accepted"
        and retired_submit_receipt["accepted_callback_receipt_sha256"]
        == prior_callback_receipt_sha256
        and len(cmux.sent) == 1
        and cmux.keys == [(SURFACE, "Enter")],
        (
            rollover_parent,
            rollover_state,
            retired_submit_receipt,
            cmux.sent,
            cmux.keys,
        ),
    )

    (callback_dir / ".review-meta.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "transport": "review-round",
                "operation_id": "review-round-4",
                "run_id": "review-round-run-4",
                "review_id": "review-parent-3",
                "parent_session_operation_id": "review-parent-3",
                "axis": "openai-holistic",
                "verification_iteration": 1,
                "verification_profile": {
                    "name": "scoped",
                    "sha256": "d" * 64,
                },
                "worktree": str(product),
            }
        ),
        encoding="utf-8",
    )
    cmux.provider_complete_review(
        input_path,
        {
            "schema_version": 1,
            "axis": "openai-holistic",
            "verdict": "approve",
            "verification_iteration": 1,
            "findings": [],
        },
    )
    next_callback_worker = FastPathWorker()
    next_callback_worker.spec_path = state_root / "launch.json"
    next_callback_worker.spec = worker.spec
    next_callback_worker.store = store
    next_callback_worker.trusted_vault = ROOT
    next_callback_worker.active_target = None
    next_callback_worker.last_digest = ""
    next_callback_worker.stable_reads = 0
    next_callback_worker.review_input_digest = ""
    next_callback_worker.review_input_stable_reads = 0
    next_callback_worker.callback_handled = False
    next_callback_worker.registration_invalid = False
    next_callback_worker.cmux_adapter = cmux
    next_callback_worker.inspect_callback()
    next_callback_worker.inspect_callback()
    next_callback_worker.inspect_callback()
    accepted_next_child = store.read("review-owner-3", "review-round-4")
    next_envelope = CallbackEnvelope(
        **json.loads(callback_path.read_text(encoding="utf-8"))
    )
    check(
        "active next generation accepts its callback without a second provider effect",
        accepted_next_child.state == "finalizing"
        and bool(accepted_next_child.accepted_callback_id)
        and len(cmux.sent) == 1
        and cmux.keys == [(SURFACE, "Enter")],
        (accepted_next_child, cmux.sent, cmux.keys),
    )
    runtime = RuntimeSessionManager(
        store,
        cmux,
        process,
        status_notifier=None,
    )
    parent_before_cleanup = store.read("review-owner-3", "review-parent-3")
    runtime._write_json(
        runtime._metadata_path(parent_before_cleanup),
        {
            "schema_version": 1,
            "operation_id": "review-parent-3",
            "run_id": "review-parent-run-3",
            "placement": "split",
        },
    )
    runtime._write_json(
        runtime._callback_target_path(parent_before_cleanup),
        {
            "schema_version": 1,
            "generation": 4,
            "operation_id": "review-round-4",
            "run_id": "review-round-run-4",
            "callback_pointer": (
                "callbacks/openai-holistic/.review-callback.json"
            ),
        },
    )
    initial_lane = ReviewLaneSession(
        "openai-holistic",
        "review-owner-3",
        "review-parent-3",
        "openai-holistic",
        "review-parent-run-3",
        SURFACE,
        "",
        parent_before_cleanup.spec,
        0,
        1,
        state=parent_before_cleanup.state,
    )
    initial_round = ReviewRound(
        "review-parent-3",
        "review-round-3",
        "review-owner-3",
        "openai-holistic",
        "review-round-run-3",
        "openai-holistic",
        0,
        joined_child.spec,
    )
    first_cleanup = accept_review_round(
        runtime,
        store,
        initial_lane,
        initial_round,
        joined_envelope,
    )
    verification_lane = replace(
        initial_lane,
        verification_iteration=1,
        state=(first_cleanup.state if first_cleanup is not None else "running"),
    )
    verification_round = ReviewRound(
        "review-parent-3",
        "review-round-4",
        "review-owner-3",
        "openai-holistic",
        "review-round-run-4",
        "openai-holistic",
        1,
        accepted_next_child.spec,
    )
    accept_review_round(
        runtime,
        store,
        verification_lane,
        verification_round,
        next_envelope,
    )
    terminal_child = store.read("review-owner-3", "review-round-3")
    terminal_parent = store.read("review-owner-3", "review-parent-3")
    pipeline = compiled_builtin("lifecycle/default")
    class PipelineLifecycleDriver(RuntimeWorkerSummaryMixin):
        def review_gate_state(self) -> dict[str, object]:
            return {"status": "approved"}

        def wait_for_summary_refresh_after_resolution(
            self, _gate_state: object, *, target_head: str = ""
        ) -> bool:
            return False

    pipeline_driver = PipelineLifecycleDriver()
    pipeline_driver.pipeline = pipeline
    pipeline_driver.review = SimpleNamespace(status="approved")
    pipeline_ready = pipeline_driver.advance_compiled_pipeline(
        SummaryPipelineState(
            summary={},
            marker=None,
            steps=pipeline.definition.steps,
            verify_step=None,
            existing_verification=None,
        )
    )
    trace_receipt = {
        "schema_version": 1,
        "pipeline_definition_sha256": pipeline.definition_sha256,
        "parent_operation_id": terminal_parent.spec.operation_id,
        "parent_state": terminal_parent.state,
        "child_operation_id": terminal_child.spec.operation_id,
        "child_state": terminal_child.state,
        "accepted_callback_id": terminal_child.accepted_callback_id,
        "accepted_callback_sha256": terminal_child.accepted_callback_sha256,
        "next_action": "reap-ready" if pipeline_ready else "wait",
        "next_step_id": "",
        "resources_released": terminal_parent.resources == OwnedResources(),
        "effect_counts": {
            name: lifecycle_effects.count(name)
            for name in (
                "harness-provider-prompt",
                "harness-provider-enter",
                "provider-callback-write",
                "harness-request-exit",
                "harness-close-surface",
                "manual-current",
                "manual-resume",
                "manual-send",
                "manual-callback-write",
            )
        },
    }
    trace_receipt_path = state_root / "pipeline-stage-receipt.json"
    trace_receipt_path.write_text(
        json.dumps(trace_receipt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    trace_receipt_sha256 = hashlib.sha256(
        trace_receipt_path.read_bytes()
    ).hexdigest()
    check(
        "accepted callback advances through terminal cleanup to reap-ready",
        terminal_child.state == "complete"
        and terminal_parent.state == "complete"
        and terminal_parent.resources == OwnedResources()
        and pipeline_ready
        and process.exit_requests == 1
        and cmux.closed == [SURFACE],
        trace_receipt,
    )
    check(
        "legacy recovery unit remains terminal but is not release evidence",
        trace_receipt_sha256
        == "2c64a851c6cbee6c6b9e3245f4ae4a08aff59108e0121603deab1fc6a172aa0c",
        trace_receipt,
    )

    def invalid_target_has_zero_effect(
        operation_id: str, run_id: str, label: str
    ) -> None:
        registration.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation": 5,
                    "operation_id": operation_id,
                    "run_id": run_id,
                    "callback_pointer": str(callback_path),
                }
            ),
            encoding="utf-8",
        )

        class InvalidTargetWorker(RecoveryWorker):
            def callback_submit_attention(self, reason: str) -> None:
                self.attention_reason = reason

        invalid_cmux = Cmux()
        invalid_worker = InvalidTargetWorker()
        invalid_worker.spec_path = state_root / f"{label}.json"
        invalid_worker.spec = {
            **worker.spec,
            "callback_registration": registration,
        }
        invalid_worker.store = store
        invalid_worker.process = Process()
        invalid_worker.handle = worker.handle
        invalid_worker.provider_exited = False
        invalid_worker.cmux_adapter = invalid_cmux
        invalid_worker.inspect_liveness()
        check(
            f"{label} callback child has zero recovery effect",
            invalid_cmux.sent == []
            and invalid_cmux.keys == []
            and invalid_worker.attention_reason
            == "callback-submit-stale-generation",
            (invalid_cmux.sent, invalid_cmux.keys),
        )

    invalid_target_has_zero_effect(
        "missing-round-3", "missing-round-run-3", "missing"
    )
    invalid_target_has_zero_effect(
        "wrong-lane-round-3", "wrong-lane-round-run-3", "wrong-lane"
    )
    invalid_target_has_zero_effect(
        "terminal-round-3", "terminal-round-run-3", "terminal"
    )
    invalid_target_has_zero_effect(
        "review-round-3", "wrong-run-3", "wrong-run"
    )


with tempfile.TemporaryDirectory(prefix="e6-production-entrypoint.") as raw:
    root = Path(raw).resolve()
    scratch = root / "scratch"
    product = root / "product"
    state_root = root / "worker-state"
    callback_root = scratch / "callbacks"
    for directory in (scratch, product, state_root, callback_root):
        directory.mkdir(parents=True, exist_ok=True)
    store = OperationStore(root / "store")
    route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "high", "reviewer-callback", "9" * 64
    )
    origin_surface = "22222222-2222-4222-8222-222222222222"
    reviewer_surface = SURFACE
    effects: list[str] = []
    registration = state_root / "callback-target.json"
    base_now = time.time()

    class GateRuntime:
        """Production review-gate port with effect-recording adapters."""

        def __init__(self) -> None:
            self.session_request = None

        def start(self, request: object, *, on_surface_opened=None) -> object:
            self.session_request = request
            record = store.create(
                request.spec, lane_id=request.lane_id, run_id=request.run_id
            )
            supervisor = OperationSupervisor(
                store, request.spec.owner_id, request.spec.operation_id
            )
            supervisor.configure_budget(
                attempt_limit=1,
                model_restart_limit=0,
                time_budget_seconds=300,
                token_limit=100,
                now=base_now,
            )
            for state in ("preflight", "starting"):
                store.transition(
                    request.spec.owner_id, request.spec.operation_id, state
                )
            supervisor.bind_resources(
                OwnedResources(
                    reviewer_surface,
                    321,
                    322,
                    "1" * 64,
                    "2" * 64,
                )
            )
            opened = SimpleNamespace(
                record=store.read(
                    request.spec.owner_id, request.spec.operation_id
                ),
                checkpoint="checkpoint-e6",
            )
            if on_surface_opened is not None:
                on_surface_opened(opened)
            for state in ("running", "awaiting-callback"):
                store.transition(
                    request.spec.owner_id, request.spec.operation_id, state
                )
            effects.append("harness-review-start")
            return SimpleNamespace(
                record=store.read(
                    request.spec.owner_id, request.spec.operation_id
                ),
                checkpoint="checkpoint-e6",
            )

        def register_callback_target(
            self,
            owner_id: str,
            parent_operation_id: str,
            child_operation_id: str,
            child_run_id: str,
            callback_pointer: str,
        ) -> None:
            callback_path = (scratch / callback_pointer).resolve()
            callback_path.parent.mkdir(parents=True, exist_ok=True)
            registration.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "generation": 1,
                        "operation_id": child_operation_id,
                        "run_id": child_run_id,
                        "callback_pointer": str(callback_path),
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            effects.append("harness-callback-register")

        def status(self, owner_id: str, operation_id: str) -> object:
            return SimpleNamespace(
                record=store.read(owner_id, operation_id),
                checkpoint="checkpoint-e6",
                action="observed",
                process_status="alive",
                surface_status="alive",
            )

        def accept_callback(self, envelope: CallbackEnvelope) -> object:
            return CallbackBroker(store, "e6-owner").accept(envelope)

        def request_exit(self, owner_id: str, operation_id: str) -> object:
            record = store.read(owner_id, operation_id)
            if record.state not in {"finalizing", "exiting", "complete"}:
                store.transition(owner_id, operation_id, "finalizing")
            if store.read(owner_id, operation_id).state != "exiting":
                store.transition(owner_id, operation_id, "exiting")
            effects.append("harness-request-exit")
            return store.read(owner_id, operation_id)

        def cleanup(self, owner_id: str, operation_id: str) -> object:
            record = store.read(owner_id, operation_id)
            if record.state != "complete":
                store.transition(owner_id, operation_id, "complete")
            completed = store.read(owner_id, operation_id)
            if completed.resources != OwnedResources():
                store.save(
                    replace(
                        completed,
                        resources=OwnedResources(),
                        revision=completed.revision + 1,
                    ),
                    expected_revision=completed.revision,
                )
            effects.append("harness-cleanup")
            return store.read(owner_id, operation_id)

    runtime = GateRuntime()
    preset = ReviewPreset.from_flags(
        runtime="codex", model="sol", effort="high"
    )
    context = ReviewContext(
        manifest="packets/e6/manifest.json",
        head_sha="a" * 40,
        verification_profile="scoped",
        verification_profile_sha256="b" * 64,
        implementer_summary_sha256="c" * 64,
    )
    request = ReviewOperationRequest(
        ReviewRequest(
            "e6-review",
            depth=preset.depth,
            runtime=preset.runtime,
            model=preset.model,
            effort=preset.effort,
            max_verify_iterations=preset.max_verify_iterations,
            selected_provider="openai",
        ),
        "e6-owner",
        route,
        context,
    )
    gate = ReviewGateController(root / "gate", runtime, store)
    run = gate.begin(
        dispatch_operation_id="e6-dispatch",
        request=request,
        origin_surface=origin_surface,
        cwd=scratch,
        product_root=product,
        prompt_pointer="packets/e6/review.md",
        callback_root="callbacks",
        callback_wake="resume exact E6 review gate",
    )
    lane = run.execution.lanes[0]
    round_ = run.rounds[lane.axis]
    callback_path = Path(
        json.loads(registration.read_text(encoding="utf-8"))[
            "callback_pointer"
        ]
    )
    now = base_now + 100.0
    os.utime(registration, (now - 60, now - 60))
    result = ReviewResult(lane.axis, "approve", (), 0)
    envelope = review_round_envelope(round_, result)
    gate_decisions: list[str] = []

    class GateProcess:
        def process_status(self, process_group: int, identity: str) -> str:
            return "alive"

        def pid_status(self, supervisor_pid: int, identity: str) -> str:
            return "alive"

    class GateCmux:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str]] = []
            self.keys: list[tuple[str, str]] = []

        def status(self, surface_id: str) -> str:
            return "alive"

        def send(self, surface_id: str, message: str) -> None:
            self.sent.append((surface_id, message))
            effects.append(
                "harness-provider-prompt"
                if surface_id == reviewer_surface
                else "harness-callback-wake"
            )

        def send_key(self, surface_id: str, key: str) -> None:
            self.keys.append((surface_id, key))
            if surface_id == reviewer_surface:
                callback_path.write_text(
                    json.dumps(to_dict(envelope), sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                effects.extend(
                    ("harness-provider-enter", "provider-callback-write")
                )
                return
            effects.append("harness-callback-wake-enter")
            active = gate.rehydrate()
            decision = gate.complete_round(
                active,
                active.execution.lanes[0],
                active.rounds[lane.axis],
                result,
            )
            gate_decisions.append(decision.action)
            effects.append("harness-review-gate-entrypoint")

    cmux = GateCmux()
    recovery_worker = RecoveryWorker()
    recovery_worker.spec_path = state_root / "launch.json"
    recovery_worker.spec = {
        "owner_id": "e6-owner",
        "operation_id": lane.operation_id,
        "run_id": lane.run_id,
        "cwd": scratch,
        "product_root": product,
        "callback_registration": registration,
        "surface_id": reviewer_surface,
        "origin_surface": origin_surface,
        "callback_wake": "resume exact E6 review gate",
        "runtime": "codex",
    }
    recovery_worker.store = store
    recovery_worker.process = GateProcess()
    recovery_worker.handle = SimpleNamespace(
        process_group=321, process_identity="1" * 64
    )
    recovery_worker.provider_exited = False
    recovery_worker.cmux_adapter = cmux
    recovery_worker.clock = lambda: now
    recovery_worker.liveness_policy = LivenessPolicy.default()
    recovery_worker.callback_submit_policy = CallbackSubmitPolicy(30, 60, 1)
    recovery_worker.liveness_controller = LivenessController(
        state_root / "liveness"
    )
    recovery_worker.latest_callback_prompt_class = "idle-prompt"
    recovery_worker.callback_idle_observations = 0
    recovery_worker.callback_prompt_observations = 0
    recovery_worker.callback_generation_identity = ""
    recovery_worker.callback_generation_progress_at = 0.0
    recovery_worker.callback_recovery_input_digest = ""
    recovery_worker.callback_recovery_input_reads = 0
    recovery_worker.callback_recovery_digest = ""
    recovery_worker.callback_recovery_reads = 0
    recovery_worker.trusted_vault = ROOT
    recovery_worker.inspect_liveness()
    recovery_worker.inspect_liveness()
    recovery_worker.inspect_liveness()
    recovery_state = recovery_worker.liveness_controller.current_state()

    callback_worker = FastPathWorker()
    callback_worker.spec_path = recovery_worker.spec_path
    callback_worker.spec = recovery_worker.spec
    callback_worker.store = store
    callback_worker.trusted_vault = ROOT
    callback_worker.active_target = None
    callback_worker.last_digest = ""
    callback_worker.stable_reads = 0
    callback_worker.review_input_digest = ""
    callback_worker.review_input_stable_reads = 0
    callback_worker.callback_handled = False
    callback_worker.registration_invalid = False
    callback_worker.cmux_adapter = cmux
    callback_worker.inspect_callback()
    callback_worker.inspect_callback()

    class SummaryEntrypoint(RuntimeWorkerSummaryMixin):
        def summary_is_stable(self, raw: bytes) -> bool:
            return True

        def build_summary_pipeline_state(
            self, raw: bytes
        ) -> SummaryPipelineState:
            return SummaryPipelineState(
                summary={"schema_version": 1},
                marker=None,
                steps=self.pipeline.definition.steps,
                verify_step=None,
                existing_verification=None,
            )

        def review_gate_state(self) -> dict[str, object]:
            return {"status": "approved"}

        def wait_for_summary_refresh_after_resolution(
            self, gate_state: object, *, target_head: str = ""
        ) -> bool:
            return False

        def publish_summary_callback(self, summary: dict[str, object]) -> None:
            effects.append("harness-summary-publish")

        def summary_attention(self, status: str, *args, **kwargs) -> None:
            raise AssertionError(status)

    summary_entrypoint = SummaryEntrypoint()
    summary_entrypoint.pipeline = compiled_builtin("lifecycle/default")
    summary_entrypoint.review = SimpleNamespace(status="approved")
    summary_entrypoint.finish_task_summary(b"{}")

    parent = store.read("e6-owner", lane.operation_id)
    child = store.read("e6-owner", round_.operation_id)
    callback_receipt = json.loads(
        (state_root / "callback-receipt.json").read_text(encoding="utf-8")
    )
    trace_receipt = {
        "schema_version": 2,
        "parent_operation_id": lane.operation_id,
        "parent_state": parent.state,
        "child_operation_id": round_.operation_id,
        "child_state": child.state,
        "accepted_callback_id": child.accepted_callback_id,
        "accepted_callback_sha256": child.accepted_callback_sha256,
        "next_action": (
            "reap-ready"
            if "harness-summary-publish" in effects
            else "wait"
        ),
        "resources_released": parent.resources == OwnedResources(),
        "effects": effects,
    }
    encoded_trace = (
        json.dumps(trace_receipt, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    trace_receipt_sha256 = hashlib.sha256(encoded_trace).hexdigest()
    expected = dogfood_evidence["observations"]
    check(
        "production entrypoints carry missing submit through callback to reap-ready",
        recovery_state is not None
        and recovery_state.nudge_count == 1
        and callback_receipt["status"] == "accepted"
        and gate_decisions == ["approved"]
        and gate.read()["status"] == "approved"
        and parent.state == "complete"
        and child.state == "complete"
        and parent.resources == OwnedResources()
        and trace_receipt["next_action"] == "reap-ready"
        and effects.count("harness-provider-prompt") == 1
        and effects.count("harness-provider-enter") == 1
        and effects.count("provider-callback-write") == 1
        and effects.count("harness-review-gate-entrypoint") == 1
        and effects.count("harness-summary-publish") == 1
        and dogfood_evidence["owner_id"] == lane.owner_id
        and dogfood_evidence["parent_operation_id"] == lane.operation_id
        and dogfood_evidence["child_operation_id"] == round_.operation_id
        and dogfood_evidence["child_run_id"] == round_.run_id
        and dogfood_evidence["lane_id"] == lane.lane_id
        and dogfood_evidence["generation"] == 1
        and expected
        == {
            "coordinator_online": False,
            "initial_submit_intentionally_omitted": True,
            "same_session_recovery_count": 1,
            "provider_prompt_count": 1,
            "provider_enter_count": 1,
            "provider_typed_artifact_count": 1,
            "accepted_receipt_count": 1,
            "next_child_state": "complete",
            "parent_state": "complete",
            "terminal_resources_owned": False,
            "next_pipeline_action": "reap-ready",
            "next_pipeline_step": "",
            "manual_current_count": 0,
            "manual_resume_count": 0,
            "manual_send_count": 0,
            "manual_callback_write_count": 0,
            "repeated_review_count": 0,
        }
        and dogfood_evidence["trace_receipt_sha256"]
        == trace_receipt_sha256,
        (trace_receipt, trace_receipt_sha256, expected),
    )


recovery = classify_callback_submit(
    CallbackSubmitEvidence(
        observed_at=incident["second_observed_at"],
        generation_progress_at=incident["first_observed_at"],
        callback_deadline_at=(
            incident["second_observed_at"]
            + incident["deadline_remaining_seconds"]
        ),
        operation_id=incident["operation_id"],
        run_id=incident["run_id"],
        lane_id=incident["lane_id"],
        generation=incident["generation"],
        expected_operation_id=incident["operation_id"],
        expected_run_id=incident["run_id"],
        expected_lane_id=incident["lane_id"],
        expected_generation=incident["generation"],
        target_sha256=incident["target_sha256"],
        expected_target_sha256=incident["target_sha256"],
        operation_state=incident["operation_state"],
        process_status=incident["process_status"],
        surface_status="alive",
        prompt_class="idle-prompt",
        stable_idle_observations=2,
        nudge_count=incident["nudge_count"],
        restart_count=incident["restart_count"],
    )
)
check(
    "idle current reviewer generation reserves one submit-only recovery",
    recovery.action == fixture["required_recovery"]["action"],
    recovery,
)
