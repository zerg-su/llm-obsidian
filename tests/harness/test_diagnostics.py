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
                "lanes": [{"axis": "holistic"}],
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
        / "holistic"
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

    gate = json.loads(
        (gate_root / "review-gate.json").read_text(encoding="utf-8")
    )
    gate["status"] = "awaiting-resolution"
    gate["round_results"] = {"holistic": "result.json"}
    gate["awaiting_resolution"] = {
        "holistic": {
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

print("harness diagnostics tests passed")
