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
    classify_callback_submit,
)
from harness.contracts import (  # noqa: E402
    AttentionReason,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
    to_dict,
)
from harness.liveness import (  # noqa: E402
    LivenessController,
    LivenessPolicy,
)
from harness.runtime_worker_review_bridge import (  # noqa: E402
    RuntimeWorkerReviewBridgeMixin,
)
from harness.runtime_callback_io import _callback_target  # noqa: E402
from harness.runtime_worker_liveness import RuntimeWorkerLivenessMixin  # noqa: E402
from harness.runtime_sessions import RuntimeSessionManager  # noqa: E402
from harness.runtime_session_contracts import RuntimeSessionError  # noqa: E402
from harness.store import OperationStore  # noqa: E402
from harness.supervisor import OperationSupervisor  # noqa: E402


SURFACE = "11111111-1111-1111-1111-111111111111"


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


fixture = json.loads(
    (
        ROOT
        / "tests/harness/fixtures/callback-submit"
        / "v2.6.3-missing-review-submit.json"
    ).read_text(encoding="utf-8")
)
incident = fixture["incident"]


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
    def read(self, surface_id):
        self.reads += 1
        return f"provider working {self.reads}"
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
        and observed["provider_reads"] > 0
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
    pass


class RecoveryWorker(RuntimeWorkerLivenessMixin):
    def inspect_callback(self) -> None:
        raise AssertionError("missing-artifact recovery must not ingest a callback")


class SymlinkWorker(RuntimeWorkerReviewBridgeMixin):
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
    store.create(parent, lane_id="openai-holistic", run_id="review-parent-run-3")
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

    class Process:
        def process_status(self, process_group: int, identity: str) -> str:
            check(
                "recovery process probe uses exact ownership",
                process_group == 321 and identity == "1" * 64,
            )
            return "alive"

    class Cmux:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str]] = []
            self.keys: list[tuple[str, str]] = []

        def status(self, surface_id: str) -> str:
            check("recovery surface probe uses exact ownership", surface_id == SURFACE)
            return "alive"

        def send(self, surface_id: str, message: str) -> None:
            self.sent.append((surface_id, message))

        def send_key(self, surface_id: str, key: str) -> None:
            self.keys.append((surface_id, key))

    cmux = Cmux()
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
    worker.process = Process()
    worker.handle = SimpleNamespace(process_group=321, process_identity="1" * 64)
    worker.provider_exited = False
    worker.cmux_adapter = cmux
    worker.clock = lambda: now
    worker.liveness_policy = LivenessPolicy.default()
    worker.callback_submit_policy = CallbackSubmitPolicy(30, 60, 1)
    worker.liveness_controller = LivenessController(state_root / "liveness")
    worker.latest_callback_prompt_class = "idle-prompt"
    worker.callback_idle_observations = 0
    worker.callback_prompt_observations = 0
    worker.callback_generation_identity = ""
    worker.callback_generation_progress_at = 0.0
    worker.callback_recovery_input_digest = ""
    worker.callback_recovery_input_reads = 0
    worker.callback_recovery_digest = ""
    worker.callback_recovery_reads = 0
    worker.trusted_vault = ROOT
    worker.inspect_liveness()
    worker.inspect_liveness()
    worker.inspect_liveness()
    recovery_state = worker.liveness_controller.current_state()
    check(
        "runtime reserves and sends one same-session submit-only nudge",
        len(cmux.sent) == 1
        and cmux.keys == [(SURFACE, "Enter")]
        and ".review-input.json" in cmux.sent[0][1]
        and "review_submit.py" in cmux.sent[0][1]
        and recovery_state is not None
        and recovery_state.nudge_count == 1
        and recovery_state.callback_submit_status == "sent",
        (cmux.sent, cmux.keys, recovery_state),
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
