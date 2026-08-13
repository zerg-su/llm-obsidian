#!/usr/bin/env python3
"""Content-free deterministic diagnostics before any model triage."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import OperationSpec, RuntimeRoute
from harness.diagnostics import observe
from harness.store import OperationStore


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
        and len(encoded.encode()) < 4096
        and packet["signals"][0]["evidence"]
        == [
            "review-data/diagnostic-owner/diagnostic-owner/review-gate.json",
            "review-runtime/diagnostic-owner/callbacks",
        ],
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

print("harness diagnostics tests passed")
