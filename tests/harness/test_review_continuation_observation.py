#!/usr/bin/env python3
"""Durable-adapter regressions for RC6.4 review continuation."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import (  # noqa: E402
    AttentionReason,
    CallbackEnvelope,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.review_continuation_observation import (  # noqa: E402
    observe_review_continuation,
)
from harness.review_continuation_recovery import (  # noqa: E402
    RecoveryDisposition,
    RecoveryReason,
    classify_review_continuation,
)
from harness.state_machine import begin_effect  # noqa: E402
from harness.store import OperationStore  # noqa: E402


def check(label: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


HEAD = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    cwd=ROOT,
    text=True,
    capture_output=True,
    check=True,
).stdout.strip()
REVIEWED_HEAD = subprocess.run(
    ["git", "rev-parse", "HEAD^"],
    cwd=ROOT,
    text=True,
    capture_output=True,
    check=True,
).stdout.strip()
ROUTE = RuntimeRoute(
    "codex", "gpt-5.6-terra", "medium", "reviewer-callback", "a" * 64
)


def operation_spec(
    operation_id: str,
    owner_id: str,
    kind: str,
    *,
    parent_operation_id: str = "",
    root_operation_id: str = "",
) -> OperationSpec:
    return OperationSpec(
        operation_id,
        f"key-{operation_id}",
        kind,
        owner_id,
        ROUTE,
        "packets/review/manifest.json",
        "scoped",
        parent_operation_id=parent_operation_id,
        root_operation_id=root_operation_id,
    )


def attention_root(
    store: OperationStore, owner_id: str, operation_id: str, run_id: str
):
    store.create(
        operation_spec(operation_id, owner_id, "dispatch"),
        lane_id=f"lane-{operation_id}",
        run_id=run_id,
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(owner_id, operation_id, state)
    store.transition(
        owner_id,
        operation_id,
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )
    return store.read(owner_id, operation_id)


def write_gate(path: Path, value: dict[str, object]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "review-gate.json").write_text(
        json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
    )


class AliveProcess:
    def process_status(self, _process_group: int, _identity: str) -> str:
        return "alive"


with tempfile.TemporaryDirectory(prefix="review-continuation-observation.") as raw:
    base = Path(raw)
    store = OperationStore(base / "store")

    drive_owner = "observation-drive-owner"
    drive_operation = "observation-drive-root"
    drive_run = "observation-drive-run"
    drive_root = attention_root(store, drive_owner, drive_operation, drive_run)
    drive_runtime = (
        store.root / "owners" / drive_owner / "runtime" / drive_operation
    )
    drive_runtime.mkdir(parents=True)
    (drive_runtime / "callback-error.json").write_text(
        '{"schema_version":1,"status":"review-drive-failed"}\n',
        encoding="utf-8",
    )
    (drive_runtime / "pipeline-review-resolution-notify.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "reviewed_head_sha": REVIEWED_HEAD,
                "resolved_head_sha": HEAD,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    verification_path = drive_runtime / "verification-receipt.json"
    verification = {"schema_version": 2, "status": "complete", "head_sha": HEAD}
    verification_path.write_text(
        json.dumps(verification, sort_keys=True) + "\n", encoding="utf-8"
    )
    drive_gate = base / "drive-gate"
    drive_attempt = "observation-drive-attempt"
    write_gate(
        drive_gate,
        {
            "schema_version": 1,
            "dispatch_operation_id": drive_operation,
            "status": "changes-requested",
            "context": {"head_sha": REVIEWED_HEAD},
            "attempt": {
                "status": "terminal",
                "identity": {
                    "attempt_id": drive_attempt,
                    "cycle": 1,
                    "exact_head_sha": REVIEWED_HEAD,
                },
            },
            "lanes": [],
            "round_results": {},
        },
    )
    drive_worker = SimpleNamespace(
        spec={
            "owner_id": drive_owner,
            "operation_id": drive_operation,
            "cwd": ROOT,
        },
        spec_path=drive_runtime / "launch.json",
        store=store,
        review=SimpleNamespace(gate_root=drive_gate),
        meta={"finalization_policy": {"max_cycles": 5}},
        verification_receipt_path=verification_path,
        verification_receipt=lambda: verification,
    )
    drive_snapshot = observe_review_continuation(drive_worker)
    drive_decision = classify_review_continuation(drive_snapshot)
    check(
        "durable drive records reconstruct the captured rearm authority",
        drive_snapshot.root.revision == drive_root.revision
        and drive_snapshot.attention_status == "review-drive-failed"
        and drive_snapshot.resolution is not None
        and drive_snapshot.verification is not None
        and drive_decision.disposition is RecoveryDisposition.REVIEW_DRIVE_REARM,
        (drive_snapshot, drive_decision),
    )

    callback_owner = "observation-callback-owner"
    callback_operation = "observation-callback-root"
    callback_run = "observation-callback-run"
    attention_root(store, callback_owner, callback_operation, callback_run)
    callback_runtime = (
        store.root
        / "owners"
        / callback_owner
        / "runtime"
        / callback_operation
    )
    callback_runtime.mkdir(parents=True)
    attempt_id = "observation-callback-attempt"
    parent_id = f"{attempt_id}-holistic"
    parent_run = "observation-parent-run"
    lane_id = "observation-callback-lane"
    parent = store.create(
        operation_spec(
            parent_id,
            callback_owner,
            "simple-review-holistic",
            parent_operation_id=callback_operation,
            root_operation_id=callback_operation,
        ),
        lane_id=lane_id,
        run_id=parent_run,
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(callback_owner, parent_id, state)
    parent = store.read(callback_owner, parent_id)
    parent = replace(
        parent,
        resources=OwnedResources(
            process_group=2345,
            process_identity="b" * 64,
        ),
        revision=parent.revision + 1,
    )
    store.save(parent, expected_revision=parent.revision - 1)
    parent_runtime = (
        store.root / "owners" / callback_owner / "runtime" / parent_id
    )
    parent_runtime.mkdir(parents=True)
    (parent_runtime / "ready.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "ready",
                "process_group": 2345,
                "process_identity": "b" * 64,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    round_id = f"{parent_id}-round"
    round_run = "observation-round-run"
    store.create(
        operation_spec(
            round_id,
            callback_owner,
            "review-round",
            parent_operation_id=parent_id,
            root_operation_id=callback_operation,
        ),
        lane_id=lane_id,
        run_id=round_run,
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(callback_owner, round_id, state)
    payload = {"axis": "openai-holistic", "verdict": "approve"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    envelope = CallbackEnvelope(
        "review-observation-callback",
        round_id,
        round_run,
        "review",
        payload,
        hashlib.sha256(encoded).hexdigest(),
    )
    round_record = store.read(callback_owner, round_id)
    store.accept_callback(
        callback_owner,
        envelope,
        expected_revision=round_record.revision,
        next_state="finalizing",
        reason=None,
        now=1.0,
    )
    callback_gate = base / "callback-gate"
    callback_gate_value = {
        "schema_version": 1,
        "dispatch_operation_id": callback_operation,
        "status": "reviewing",
        "context": {"head_sha": HEAD},
        "attempt": {
            "status": "awaiting-callback",
            "identity": {
                "attempt_id": attempt_id,
                "cycle": 1,
                "exact_head_sha": HEAD,
            },
        },
        "lanes": [
            {
                "axis": "openai-holistic",
                "operation_id": parent_id,
                "run_id": parent_run,
                "lane_id": lane_id,
            }
        ],
        "round_results": {},
    }
    write_gate(callback_gate, callback_gate_value)
    callback_worker = SimpleNamespace(
        spec={
            "owner_id": callback_owner,
            "operation_id": callback_operation,
            "cwd": ROOT,
        },
        spec_path=callback_runtime / "launch.json",
        store=store,
        review=SimpleNamespace(gate_root=callback_gate),
        meta={"finalization_policy": {"max_cycles": 5}},
        process=AliveProcess(),
    )
    callback_snapshot = observe_review_continuation(callback_worker)
    callback_decision = classify_review_continuation(callback_snapshot)
    check(
        "durable callback records reconstruct exact ingestion authority",
        len(callback_snapshot.accepted_callbacks) == 1
        and callback_snapshot.accepted_callbacks[0].callback_id
        == envelope.callback_id
        and callback_decision.disposition
        is RecoveryDisposition.ACCEPTED_CALLBACK_INGEST,
        (callback_snapshot, callback_decision),
    )

    healthy = replace(
        callback_snapshot,
        root=replace(
            callback_snapshot.root,
            state="awaiting-callback",
            resume_state="",
        ),
    )
    check(
        "the same durable callback is refused outside a stuck root",
        classify_review_continuation(healthy).reason
        is RecoveryReason.ATTENTION_NOT_RECOVERABLE,
    )

    missing_parent_id = f"{attempt_id}-missing-axis"
    missing_parent_run = "observation-missing-parent-run"
    store.create(
        operation_spec(
            missing_parent_id,
            callback_owner,
            "deep-review-holistic",
            parent_operation_id=callback_operation,
            root_operation_id=callback_operation,
        ),
        lane_id="observation-missing-lane",
        run_id=missing_parent_run,
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(callback_owner, missing_parent_id, state)
    missing_parent = store.read(callback_owner, missing_parent_id)
    pending_parent = begin_effect(missing_parent, "start-provider")
    store.save(pending_parent, expected_revision=missing_parent.revision)
    callback_gate_value["lanes"].append(
        {
            "axis": "anthropic-holistic",
            "operation_id": missing_parent_id,
            "run_id": missing_parent_run,
            "lane_id": "observation-missing-lane",
        }
    )
    write_gate(callback_gate, callback_gate_value)
    incomplete = observe_review_continuation(callback_worker)
    incomplete_decision = classify_review_continuation(incomplete)
    check(
        "every gate lane is observed and an unresolved lane effect fails closed",
        len(incomplete.lanes) == 2
        and any(
            lane.operation_id == missing_parent_id
            and not lane.round_operation_id
            and lane.pending_effect == "start-provider"
            for lane in incomplete.lanes
        )
        and incomplete_decision.reason is RecoveryReason.EFFECT_REPLAY_REQUIRED,
        (incomplete, incomplete_decision),
    )
    missing_round_without_effect = replace(
        incomplete,
        root=replace(
            incomplete.root,
            state="awaiting-callback",
            resume_state="",
        ),
        lanes=tuple(
            replace(lane, pending_effect="") for lane in incomplete.lanes
        ),
        accepted_callbacks=(),
        effect_requires_replay=False,
    )
    check(
        "a gate lane without a durable round cannot prove live review progress",
        classify_review_continuation(missing_round_without_effect).disposition
        is not RecoveryDisposition.REVIEW_IN_PROGRESS,
    )

print("review continuation observation tests passed")
