#!/usr/bin/env python3
"""Hermetic tests for state-free engineering/fix model-step receipts."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import OperationSpec, RuntimeRoute
from harness.store import OperationStore
from harness.workflows.engineering_fix import (
    FIX_PHASES,
    FixWorkflowError,
    accept_phase,
    load_receipt,
    phase_envelope,
    prepare_next_phase,
    reconcile_fix,
)


failures: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"ok - {name}")
    else:
        failures.append(name)
        print(f"not ok - {name}: {detail}")


def expect_error(name: str, action, needle: str) -> None:
    try:
        action()
    except FixWorkflowError as exc:
        check(name, needle in str(exc), exc)
    else:
        check(name, False, "expected FixWorkflowError")


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def parent(store: OperationStore) -> object:
    route = RuntimeRoute(
        "codex",
        "gpt-5.6-terra",
        "low",
        "executor",
        sha("route"),
    )
    spec = OperationSpec(
        operation_id="fix-parent",
        idempotency_key=sha("parent"),
        kind="dispatch",
        owner_id="fix-owner",
        route=route,
        context_manifest="wiki/plans/fix.md",
        verification_profile="scoped",
        contract_sha256=sha("definition"),
    )
    return store.create(spec, lane_id="fix-lane", run_id="fix-parent-run")


with tempfile.TemporaryDirectory(prefix="engineering-fix-workflow.") as raw:
    root = Path(raw)
    store = OperationStore(root / "store")
    parent_record = parent(store)
    definition = parent_record.spec.contract_sha256
    plan = sha("approved-plan")
    initial_head = "a" * 40
    receipt_root = root / "receipts"

    first = prepare_next_phase(
        store,
        parent_record,
        definition_sha256=definition,
        approved_plan_sha256=plan,
        initial_head_sha=initial_head,
        receipts=(),
        iteration=0,
    )
    first_replay = prepare_next_phase(
        store,
        parent_record,
        definition_sha256=definition,
        approved_plan_sha256=plan,
        initial_head_sha=initial_head,
        receipts=(),
        iteration=0,
    )
    first_record = store.read("fix-owner", first.spec.operation_id)
    check(
        "first phase is one deterministic resource-less child in the parent lane",
        first.step_id == "reproduce"
        and first.parent_operation_id == parent_record.spec.operation_id
        and first.spec == first_replay.spec
        and first.lane_id == parent_record.lane_id
        and first.run_id == first_replay.run_id
        and first_record.state == "awaiting-callback"
        and not first_record.resources.surface_id
        and first.spec.route == parent_record.spec.route
        and first.spec.context_manifest == parent_record.spec.context_manifest
        and first.spec.contract_sha256 == definition,
        (first, first_record),
    )

    long_store = OperationStore(root / "long-parent-store")
    long_spec = replace(
        parent_record.spec,
        operation_id="p" * 128,
        idempotency_key=sha("long-parent"),
        owner_id="long-owner",
    )
    long_parent = long_store.create(
        long_spec, lane_id="long-lane", run_id="long-parent-run"
    )
    long_round = prepare_next_phase(
        long_store,
        long_parent,
        definition_sha256=definition,
        approved_plan_sha256=plan,
        initial_head_sha=initial_head,
        receipts=(),
        iteration=0,
    )
    long_envelope = phase_envelope(
        long_round,
        status="complete",
        output_pointer="long-parent.md",
        output_sha256=sha("long-parent-output"),
        head_sha=initial_head,
    )
    check(
        "bounded child ids retain the full parent identity in their callback",
        len(long_round.spec.operation_id) <= 128
        and long_envelope.payload["parent_operation_id"]
        == long_parent.spec.operation_id,
        (long_round, long_envelope.payload),
    )

    reproduce_output = root / "reproduce.md"
    reproduce_output.write_text("reproduced\n", encoding="utf-8")
    reproduce_envelope = phase_envelope(
        first,
        status="complete",
        output_pointer="reproduce.md",
        output_sha256=hashlib.sha256(reproduce_output.read_bytes()).hexdigest(),
        head_sha=initial_head,
    )
    reproduce_receipt_path = (
        receipt_root / first.spec.operation_id / "receipt.json"
    )
    reproduce_receipt = accept_phase(
        store,
        first,
        reproduce_envelope,
        current_head_sha=initial_head,
        receipt_path=reproduce_receipt_path,
    )
    reproduce_replay = accept_phase(
        store,
        first,
        reproduce_envelope,
        current_head_sha=initial_head,
        receipt_path=reproduce_receipt_path,
    )
    check(
        "accepted callback becomes one immutable typed receipt and terminal child",
        reproduce_receipt == reproduce_replay
        and reproduce_receipt == load_receipt(reproduce_receipt_path)
        and reproduce_receipt.step_id == "reproduce"
        and reproduce_receipt.input_schema == "approved-plan/v1"
        and reproduce_receipt.output_schema == "reproduction/v1"
        and reproduce_receipt.output_sha256
        == hashlib.sha256(reproduce_output.read_bytes()).hexdigest()
        and store.read("fix-owner", first.spec.operation_id).state == "complete",
        reproduce_receipt,
    )

    second = prepare_next_phase(
        store,
        parent_record,
        definition_sha256=definition,
        approved_plan_sha256=plan,
        initial_head_sha=initial_head,
        receipts=(reproduce_receipt,),
        iteration=0,
    )
    check(
        "next child binds the exact prior receipt schema hash and HEAD",
        second.step_id == "root-cause"
        and second.input_schema == reproduce_receipt.output_schema
        and second.input_head_sha == reproduce_receipt.head_sha
        and second.prior_receipt_sha256
        == reproduce_receipt.receipt_sha256
        and second.spec.operation_id != first.spec.operation_id
        and second.lane_id == first.lane_id
        and second.run_id != first.run_id,
        second,
    )

    tampered_payload = dict(reproduce_envelope.payload)
    tampered_payload["output_schema"] = "diagnosis/v1"
    tampered_encoded = json.dumps(
        tampered_payload, sort_keys=True, separators=(",", ":")
    ).encode()
    tampered = replace(
        reproduce_envelope,
        callback_id="result-" + hashlib.sha256(tampered_encoded).hexdigest()[:24],
        payload=tampered_payload,
        payload_sha256=hashlib.sha256(tampered_encoded).hexdigest(),
    )
    expect_error(
        "envelope schema tampering fails before callback acceptance",
        lambda: accept_phase(
            store,
            second,
            tampered,
            current_head_sha=initial_head,
            receipt_path=receipt_root / second.spec.operation_id / "receipt.json",
        ),
        "identity",
    )
    check(
        "rejected envelope leaves its child awaiting the exact callback",
        store.read("fix-owner", second.spec.operation_id).state
        == "awaiting-callback",
    )

    receipts = [reproduce_receipt]
    round_ = second
    heads = iter(("b" * 40, "c" * 40, "d" * 40))
    for expected_step in FIX_PHASES[1:]:
        next_head = next(heads)
        envelope = phase_envelope(
            round_,
            status="complete",
            output_pointer=f"{round_.step_id}.md",
            output_sha256=sha(round_.step_id),
            head_sha=next_head,
        )
        receipt = accept_phase(
            store,
            round_,
            envelope,
            current_head_sha=next_head,
            receipt_path=receipt_root / round_.spec.operation_id / "receipt.json",
        )
        receipts.append(receipt)
        progress = reconcile_fix(
            parent_record,
            definition_sha256=definition,
            approved_plan_sha256=plan,
            initial_head_sha=initial_head,
            receipts=tuple(receipts),
            iteration=0,
        )
        check(
            f"{expected_step} receipt advances an ordered prefix",
            receipt.step_id == expected_step
            and progress.completed_steps
            == tuple(item.step_id for item in receipts),
            (receipt, progress),
        )
        if progress.action == "start":
            round_ = prepare_next_phase(
                store,
                parent_record,
                definition_sha256=definition,
                approved_plan_sha256=plan,
                initial_head_sha=initial_head,
                receipts=tuple(receipts),
                iteration=0,
            )

    complete = reconcile_fix(
        parent_record,
        definition_sha256=definition,
        approved_plan_sha256=plan,
        initial_head_sha=initial_head,
        receipts=tuple(receipts),
        iteration=0,
    )
    check(
        "four exact receipts reconstruct terminal semantic progress without controller state",
        complete.action == "complete"
        and complete.step_id == ""
        and complete.completed_steps == FIX_PHASES,
        complete,
    )

    expect_error(
        "out-of-order receipts fail closed",
        lambda: reconcile_fix(
            parent_record,
            definition_sha256=definition,
            approved_plan_sha256=plan,
            initial_head_sha=initial_head,
            receipts=(receipts[1], receipts[0]),
            iteration=0,
        ),
        "ordered",
    )
    expect_error(
        "receipt definition drift fails closed",
        lambda: reconcile_fix(
            parent_record,
            definition_sha256=sha("other-definition"),
            approved_plan_sha256=plan,
            initial_head_sha=initial_head,
            receipts=(reproduce_receipt,),
            iteration=0,
        ),
        "definition",
    )

    attention_store = OperationStore(root / "attention-store")
    attention_parent = parent(attention_store)
    attention_round = prepare_next_phase(
        attention_store,
        attention_parent,
        definition_sha256=definition,
        approved_plan_sha256=plan,
        initial_head_sha=initial_head,
        receipts=(),
        iteration=1,
    )
    cannot = phase_envelope(
        attention_round,
        status="cannot-reproduce",
        output_pointer="cannot-reproduce.md",
        output_sha256=sha("cannot-reproduce"),
        head_sha=initial_head,
    )
    cannot_receipt = accept_phase(
        attention_store,
        attention_round,
        cannot,
        current_head_sha=initial_head,
        receipt_path=root / "cannot-reproduce-receipt.json",
    )
    attention = reconcile_fix(
        attention_parent,
        definition_sha256=definition,
        approved_plan_sha256=plan,
        initial_head_sha=initial_head,
        receipts=(cannot_receipt,),
        iteration=1,
    )
    check(
        "cannot-reproduce is an accepted typed result that reconstructs attention",
        cannot_receipt.status == "cannot-reproduce"
        and attention.action == "attention"
        and attention.step_id == "reproduce"
        and attention.completed_steps == ("reproduce",),
        attention,
    )
    expect_error(
        "cannot-reproduce is illegal outside the reproduce phase",
        lambda: phase_envelope(
            second,
            status="cannot-reproduce",
            output_pointer="no.md",
            output_sha256=sha("no"),
            head_sha=initial_head,
        ),
        "cannot-reproduce",
    )

    changed = json.loads(reproduce_receipt_path.read_text(encoding="utf-8"))
    changed["output_pointer"] = "changed.md"
    reproduce_receipt_path.write_text(
        json.dumps(changed, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    expect_error(
        "accepted receipt storage is immutable",
        lambda: accept_phase(
            store,
            first,
            reproduce_envelope,
            current_head_sha=initial_head,
            receipt_path=reproduce_receipt_path,
        ),
        "changed",
    )


if failures:
    raise SystemExit(f"{len(failures)} engineering/fix workflow test(s) failed")
