#!/usr/bin/env python3
"""Additive pipeline controller and receipt-ledger contracts."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import RuntimeRoute
from harness.pipeline_builtins import builtin_definitions, builtin_registry
from harness.pipeline_state import PipelineLedger, PipelineLedgerError
from harness.pipelines import bind_step_operation, compile_pipeline
from harness.store import OperationStore


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


compiled = compile_pipeline(
    builtin_definitions()["engineering/change"],
    builtin_registry(),
    capabilities=("provider:authenticated",),
)
route = RuntimeRoute("codex", "gpt-5.6-sol", "high", "executor", "a" * 64)

with tempfile.TemporaryDirectory(prefix="pipeline-ledger.") as raw:
    store = OperationStore(Path(raw) / "harness")
    ledger = PipelineLedger(store)
    state = ledger.start("owner-1", "pipeline-run-1", compiled)
    check(
        "controller stores only semantic operation progress",
        state.definition_sha256 == compiled.definition_sha256
        and state.step_order == ("tdd-slices", "verify", "review")
        and state.completed_steps == ()
        and state.status == "running",
    )

    verify_binding = bind_step_operation(
        compiled,
        step_id="verify",
        operation_id="pipeline-verify-1",
        owner_id="owner-1",
        route=route,
        context_manifest="packets/change/manifest.json",
        verification_profile="scoped",
        input_sha256="b" * 64,
    )
    try:
        ledger.accept(
            "owner-1",
            "pipeline-run-1",
            verify_binding,
            output_sha256="c" * 64,
        )
    except PipelineLedgerError:
        check("controller rejects an out-of-order semantic receipt", True)
    else:
        check("controller rejects an out-of-order semantic receipt", False)

    implement_binding = bind_step_operation(
        compiled,
        step_id="tdd-slices",
        operation_id="pipeline-implement-1",
        owner_id="owner-1",
        route=route,
        context_manifest="packets/change/manifest.json",
        verification_profile="scoped",
        input_sha256="d" * 64,
    )
    accepted, receipt = ledger.accept(
        "owner-1",
        "pipeline-run-1",
        implement_binding,
        output_sha256="e" * 64,
    )
    check(
        "accepted step writes one content-addressed receipt",
        accepted.completed_steps == ("tdd-slices",)
        and accepted.revision == 1
        and receipt.replay_key == implement_binding.replay_key
        and receipt.primitive_id == "model_step"
        and receipt.primitive_version == "1.0.0"
        and receipt.output_sha256 == "e" * 64
        and receipt.output_schema == "implementation-result/v1"
        and ledger.lookup(
            "owner-1", "pipeline-run-1", implement_binding.replay_key
        )
        == receipt,
    )
    check(
        "controller restart preserves accepted progress",
        ledger.start("owner-1", "pipeline-run-1", compiled) == accepted,
    )

    replayed, same_receipt = ledger.accept(
        "owner-1",
        "pipeline-run-1",
        implement_binding,
        output_sha256="e" * 64,
    )
    check(
        "exact receipt replay is idempotent",
        replayed == accepted
        and same_receipt == receipt
        and replayed.revision == 1,
    )

    try:
        ledger.accept(
            "owner-1",
            "pipeline-run-1",
            implement_binding,
            output_sha256="f" * 64,
        )
    except PipelineLedgerError:
        check("same replay key cannot acquire conflicting output", True)
    else:
        check("same replay key cannot acquire conflicting output", False)

    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (Path(raw) / "harness").rglob("*.json")
    )
    check(
        "ledger persists hashes and identities without raw model content",
        "implementation-result/v1" in persisted
        and "pipeline-implement-1" in persisted
        and "raw_output" not in persisted
        and "prompt" not in persisted,
    )
