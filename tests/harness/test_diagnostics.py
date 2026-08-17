#!/usr/bin/env python3
"""Content-free deterministic diagnostics before any model triage."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import EffectOutcome, OperationSpec, RuntimeRoute
from harness.diagnostics import observe
from harness.pipeline_builtins import compiled_builtin
from harness.runtime_worker import _pipeline_verify_identity
from harness.store import OperationStore
from harness.verification import VerificationAuthority, VerificationEvidence
from harness.verification_attempt import (
    VerificationAttempt,
    pipeline_verify_effect_id,
    verification_input_sha256,
)


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"OK   {label}")


def recovery_receipt(
    *,
    recovery_class: str,
    operation_id: str,
    status: str,
    outcome: str = "",
    owner_id: str = "",
) -> dict[str, object]:
    identity = {
        "recovery_class": recovery_class,
        "owner_id": owner_id or operation_id,
        "root_operation_id": operation_id,
        "root_run_id": f"run-{operation_id}",
        "root_revision": 5,
        "attempt_id": f"attempt-{operation_id}",
        "gate_sha256": "a" * 64,
        "authority_sha256": "b" * 64,
        "lane_id": "lane-review" if recovery_class == "accepted-callback" else "",
        "round_operation_id": (
            "round-review" if recovery_class == "accepted-callback" else ""
        ),
        "round_run_id": (
            "round-run" if recovery_class == "accepted-callback" else ""
        ),
        "callback_id": (
            "review-callback" if recovery_class == "accepted-callback" else ""
        ),
    }
    import hashlib

    identity_sha256 = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    value = {
        "schema_version": 1,
        "status": status,
        "identity": identity,
        "identity_sha256": identity_sha256,
    }
    if outcome:
        value["outcome"] = outcome
        value["reason"] = "test-reason"
    return value


with tempfile.TemporaryDirectory(prefix="harness-diagnostics.") as raw:
    root = Path(raw)
    store_root = root / "harness"
    store = OperationStore(store_root)
    owner = "diagnostic-owner"
    operation = "diagnostic-operation"
    store.create(
        OperationSpec(
            operation,
            "diagnostic-key",
            "dispatch",
            owner,
            RuntimeRoute(
                "codex",
                "gpt-5.6-sol",
                "high",
                "executor",
                "a" * 64,
            ),
            "packets/task.json",
            "scoped",
        ),
        lane_id="diagnostic-lane",
        run_id="diagnostic-run",
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(owner, operation, state)

    product = root / "product"
    product.mkdir()
    gate_root = store_root / "review-data" / owner / owner
    gate_root.mkdir(parents=True)
    (gate_root / "review-gate.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner_id": owner,
                "dispatch_operation_id": operation,
                "status": "reviewing",
                "product_root": str(product),
                "lanes": [{"axis": "openai-holistic"}],
                "round_results": {},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    callback = (
        store_root
        / "review-runtime"
        / owner
        / "callbacks"
        / "openai-holistic"
        / ".review-callback.json"
    )
    callback.parent.mkdir(parents=True)
    callback.write_text(
        '{"schema_version":1,"secret":"DO_NOT_LEAK"}\n',
        encoding="utf-8",
    )
    runtime_root = (
        store_root / "owners" / owner / "runtime" / operation
    )
    runtime_root.mkdir(parents=True)
    wake_receipt = {
        "schema_version": 1,
        "owner_id": owner,
        "operation_id": operation,
        "run_id": "diagnostic-run",
        "generation": 2,
        "source": "cmux-event",
        "event_name": "agent.hook.PostToolUse",
        "sequence": 17,
        "observed_at": 42.5,
        "outcome": "progressed",
        "private_screen": "DO_NOT_LEAK_WAKE",
    }
    for filename in ("wake-observation.json", "wake-progress.json"):
        (runtime_root / filename).write_text(
            json.dumps(wake_receipt, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    packet = observe(store_root, owner)
    encoded = json.dumps(packet, sort_keys=True)
    check(
        "code observer catches a durable callback pending ingestion",
        packet["status"] == "actionable"
        and packet["model_required"] is False
        and packet["model_policy"]["role"] == "diagnostic-fast"
        and packet["signals"][0]["code"]
        == "review-callback-pending-ingestion",
    )
    check(
        "diagnostic packet is content-free and bounded",
        "DO_NOT_LEAK" not in encoded
        and "DO_NOT_LEAK_WAKE" not in encoded
        and len(encoded.encode()) < 4096
        and packet["signals"][0]["evidence"]
        == [
            "review-data/diagnostic-owner/diagnostic-owner/review-gate.json",
            "review-runtime/diagnostic-owner/callbacks",
        ],
    )
    check(
        "diagnostics expose only bounded latest wake identities",
        packet["counts"]["wake_observations"] == 2
        and packet["wake_observations"][0]
        == {
            "kind": "latest-full-reconcile",
            "owner_id": owner,
            "operation_id": operation,
            "run_id": "diagnostic-run",
            "generation": 2,
            "source": "cmux-event",
            "event_name": "agent.hook.PostToolUse",
            "sequence": 17,
            "observed_at": 42.5,
            "outcome": "progressed",
            "evidence": (
                "owners/diagnostic-owner/runtime/"
                "diagnostic-operation/wake-observation.json"
            ),
        },
    )

    legacy_callback_receipt = (
        store_root
        / "owners"
        / owner
        / "runtime"
        / operation
        / "review-continuation-recovery.json"
    )
    callback_receipt_value = recovery_receipt(
        recovery_class="accepted-callback",
        operation_id=operation,
        owner_id=owner,
        status="prepared",
    )
    legacy_callback_receipt.parent.mkdir(parents=True, exist_ok=True)
    legacy_callback_receipt.write_text(
        json.dumps(callback_receipt_value, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    packet = observe(store_root, owner)
    check(
        "unreleased single-file recovery receipt has no diagnostic authority",
        [signal["code"] for signal in packet["signals"]]
        == ["review-callback-pending-ingestion"]
        and packet["model_required"] is False,
    )
    legacy_callback_receipt.unlink()
    callback_receipt = (
        legacy_callback_receipt.parent
        / "review-continuation-recovery"
        / f"{'c' * 64}.json"
    )
    callback_receipt.parent.mkdir()
    callback_receipt.write_text(
        json.dumps(callback_receipt_value, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    packet = observe(store_root, owner)
    check(
        "scoped accepted callback receipt refines the pending-ingestion diagnostic",
        [signal["code"] for signal in packet["signals"]]
        == ["review-callback-ingestion-prepared"]
        and packet["model_required"] is False,
    )
    callback_receipt.unlink()

    gate = json.loads(
        (gate_root / "review-gate.json").read_text(encoding="utf-8")
    )
    gate["status"] = "awaiting-resolution"
    gate["round_results"] = {"openai-holistic": "result.json"}
    gate["awaiting_resolution"] = {
        "openai-holistic": {
            "pointer": "result.json",
            "reviewed_head_sha": "b" * 40,
        }
    }
    (gate_root / "review-gate.json").write_text(
        json.dumps(gate, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    packet = observe(store_root, owner)
    check(
        "code observer catches typed findings pending task delivery",
        [signal["code"] for signal in packet["signals"]]
        == ["review-resolution-pending-delivery"]
        and packet["model_required"] is False,
    )

    unknown_owner = "unknown-attention-owner"
    store.create(
        OperationSpec(
            "unknown-attention",
            "unknown-attention-key",
            "dispatch",
            unknown_owner,
            RuntimeRoute(
                "codex",
                "gpt-5.6-sol",
                "high",
                "executor",
                "a" * 64,
            ),
            "packets/task.json",
            "scoped",
        ),
        lane_id="unknown-lane",
        run_id="unknown-run",
    )
    store.transition(
        unknown_owner,
        "unknown-attention",
        "attention-required",
        reason="attention-required",
    )
    packet = observe(store_root, unknown_owner)
    check(
        "unknown attention delegates only compact read-only triage",
        packet["status"] == "needs-model"
        and packet["model_required"] is True
        and packet["model_policy"]
        == {
            "role": "diagnostic-fast",
            "context": "minimal",
            "write": False,
        },
    )

    drive_owner = "review-drive-diagnostic"
    store.create(
        OperationSpec(
            drive_owner,
            "review-drive-key",
            "dispatch",
            drive_owner,
            RuntimeRoute(
                "codex",
                "gpt-5.6-sol",
                "high",
                "executor",
                "c" * 64,
            ),
            "packets/task.json",
            "scoped",
        ),
        lane_id="review-drive-lane",
        run_id=f"run-{drive_owner}",
    )
    store.transition(
        drive_owner,
        drive_owner,
        "attention-required",
        reason="attention-required",
    )
    drive_receipt = (
        store_root
        / "owners"
        / drive_owner
        / "runtime"
        / drive_owner
        / "review-continuation-recovery"
        / f"{'d' * 64}.json"
    )
    drive_receipt.parent.mkdir(parents=True, exist_ok=True)
    drive_receipt.write_text(
        json.dumps(
            recovery_receipt(
                recovery_class="review-drive",
                operation_id=drive_owner,
                status="finalized",
                outcome="advanced",
            ),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    packet = observe(store_root, drive_owner)
    check(
        "review drive receipt replaces unclassified attention",
        [signal["code"] for signal in packet["signals"]]
        == ["review-drive-recovery-advanced"]
        and packet["model_required"] is False,
    )
    refused = recovery_receipt(
        recovery_class="review-drive",
        operation_id=drive_owner,
        status="finalized",
        outcome="refused",
    )
    drive_receipt.write_text(
        json.dumps(refused, sort_keys=True) + "\n", encoding="utf-8"
    )
    packet = observe(store_root, drive_owner)
    check(
        "review drive refusal remains typed and receipt-bound",
        [signal["code"] for signal in packet["signals"]]
        == ["review-drive-recovery-refused"],
    )

    unrelated = "unrelated-attention"
    store.create(
        OperationSpec(
            unrelated,
            "unrelated-attention-key",
            "dispatch",
            drive_owner,
            RuntimeRoute(
                "codex",
                "gpt-5.6-sol",
                "high",
                "executor",
                "d" * 64,
            ),
            "packets/task.json",
            "scoped",
        ),
        lane_id="unrelated-attention-lane",
        run_id="unrelated-attention-run",
    )
    store.transition(
        drive_owner,
        unrelated,
        "attention-required",
        reason="attention-required",
    )
    packet = observe(store_root, drive_owner)
    signals = {
        (signal["operation_id"], signal["code"])
        for signal in packet["signals"]
    }
    check(
        "a recovery receipt cannot suppress unrelated owner attention",
        (drive_owner, "review-drive-recovery-refused") in signals
        and (unrelated, "operation-attention-unclassified") in signals
        and packet["model_required"] is True
        and packet["status"] == "needs-model",
    )

    superseded_owner = "superseded-verification-diagnostic"
    compiled = compiled_builtin("engineering/change")
    root_spec = OperationSpec(
        superseded_owner,
        "superseded-verification-key",
        "dispatch",
        superseded_owner,
        RuntimeRoute(
            "codex", "gpt-5.6-sol", "high", "executor", "e" * 64
        ),
        "packets/task.json",
        "scoped",
        contract_sha256=compiled.definition_sha256,
    )
    parent = store.create(
        root_spec,
        lane_id="superseded-root-lane",
        run_id="superseded-root-run",
    )
    for state in ("preflight", "starting", "running"):
        store.transition(superseded_owner, superseded_owner, state)
    head_sha = "9" * 40
    profile_sha256 = "7" * 64
    input_sha256 = verification_input_sha256(
        compiled.definition_sha256, head_sha, profile_sha256, 1
    )
    predecessor, predecessor_lane, predecessor_run = _pipeline_verify_identity(
        parent.spec,
        definition_sha256=compiled.definition_sha256,
        input_sha256=input_sha256,
        profile="scoped",
        attempt_index=0,
    )
    successor, successor_lane, successor_run = _pipeline_verify_identity(
        parent.spec,
        definition_sha256=compiled.definition_sha256,
        input_sha256=input_sha256,
        profile="scoped",
        attempt_index=1,
    )
    store.create(
        predecessor,
        lane_id=predecessor_lane,
        run_id=predecessor_run,
    )
    store.create(successor, lane_id=successor_lane, run_id=successor_run)
    for operation_id in (predecessor.operation_id, successor.operation_id):
        for state in ("preflight", "starting", "running", "verifying"):
            store.transition(superseded_owner, operation_id, state)
    store.transition(
        superseded_owner,
        predecessor.operation_id,
        "attention-required",
        reason="attention-required",
    )
    successor_effect_id = pipeline_verify_effect_id(input_sha256, 1)
    store.begin_effect(
        superseded_owner, successor.operation_id, successor_effect_id
    )
    store.resolve_effect(
        superseded_owner,
        successor.operation_id,
        EffectOutcome.SUCCEEDED,
    )
    for state in ("finalizing", "exiting", "complete"):
        store.transition(superseded_owner, successor.operation_id, state)
    predecessor_attempt = VerificationAttempt(
        superseded_owner, "scoped", profile_sha256, head_sha, 0
    )
    successor_attempt = predecessor_attempt.same_head_retry()
    invalidation_path = (
        store_root
        / "owners"
        / superseded_owner
        / "runtime"
        / superseded_owner
        / "pipeline-verification"
        / predecessor.operation_id
        / "invalidation.json"
    )
    invalidation_path.parent.mkdir(parents=True, exist_ok=True)
    invalidation = {
        "schema_version": 1,
        "operation_id": predecessor.operation_id,
        "parent_operation_id": superseded_owner,
        "profile_sha256": profile_sha256,
        "predecessor_attempt_sha256": predecessor_attempt.sha256,
        "predecessor_effect_id": pipeline_verify_effect_id(input_sha256, 0),
        "successor_operation_id": successor.operation_id,
        "successor_attempt_sha256": successor_attempt.sha256,
        "successor_effect_id": pipeline_verify_effect_id(input_sha256, 1),
        "current_head_sha": head_sha,
        "status": "invalidated",
    }
    invalidation_path.write_text(
        json.dumps(invalidation, sort_keys=True) + "\n", encoding="utf-8"
    )
    receiptless_packet = observe(store_root, superseded_owner)
    verification_runtime = invalidation_path.parents[2]
    output_path = verification_runtime / "successor-output.log"
    output_path.write_bytes(b"ok\n")
    authority = VerificationAuthority.issue(
        store=store,
        parent=store.read(superseded_owner, superseded_owner),
        runtime_root=verification_runtime,
        definition_sha256=compiled.definition_sha256,
        input_sha256=input_sha256,
        profile="scoped",
        profile_sha256=profile_sha256,
        attempt=successor_attempt,
        evidence=(
            VerificationEvidence(
                "scoped",
                profile_sha256,
                head_sha,
                "scoped-1",
                ".",
                0,
                "1",
                "2",
                "successor-output.log",
                hashlib.sha256(b"ok\n").hexdigest(),
                3,
                2,
            ),
        ),
        expected_command_ids=("scoped-1",),
    )
    successor_receipt = (
        verification_runtime
        / "pipeline-verification"
        / successor.operation_id
        / "receipt.json"
    )
    successor_receipt.parent.mkdir(parents=True, exist_ok=True)
    successor_receipt.write_text(
        json.dumps(authority.to_dict(), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    superseded_packet = observe(store_root, superseded_owner)
    successor_receipt_bytes = successor_receipt.read_bytes()
    successor_receipt.write_text("{malformed receipt", encoding="utf-8")
    malformed_receipt_packet = observe(store_root, superseded_owner)
    successor_receipt.unlink()
    successor_receipt.symlink_to(successor_receipt.parent / "missing.json")
    symlinked_receipt_packet = observe(store_root, superseded_owner)
    successor_receipt.unlink()
    successor_receipt.write_bytes(successor_receipt_bytes)
    successor_record_path = (
        store_root
        / "owners"
        / superseded_owner
        / "operations"
        / f"{successor.operation_id}.json"
    )
    successor_record_bytes = successor_record_path.read_bytes()
    effectless_record = json.loads(successor_record_bytes)
    effectless_record["effect_id"] = ""
    effectless_record["effect_outcome"] = "none"
    successor_record_path.write_text(
        json.dumps(effectless_record, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    effectless_packet = observe(store_root, superseded_owner)
    successor_record_path.write_bytes(successor_record_bytes)
    invalidation_path.write_text(
        json.dumps(
            {**invalidation, "successor_attempt_sha256": "0" * 64},
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    drifted_packet = observe(store_root, superseded_owner)
    check(
        "only an exact completed verification successor suppresses predecessor attention",
        any(
            signal["operation_id"] == predecessor.operation_id
            for signal in receiptless_packet["signals"]
        )
        and not any(
            signal["operation_id"] == predecessor.operation_id
            for signal in superseded_packet["signals"]
        )
        and all(
            any(
                signal["operation_id"] == predecessor.operation_id
                for signal in packet["signals"]
            )
            for packet in (
                malformed_receipt_packet,
                symlinked_receipt_packet,
                effectless_packet,
            )
        )
        and any(
            signal["operation_id"] == predecessor.operation_id
            and signal["code"] == "operation-attention-unclassified"
            for signal in drifted_packet["signals"]
        ),
    )

print("harness diagnostics tests passed")
