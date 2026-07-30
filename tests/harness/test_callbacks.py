#!/usr/bin/env python3
"""Callback, prompt, retry, and reconciliation replay tests."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from harness.callbacks import CallbackBroker, CallbackError
from harness.contracts import (
    AttentionReason,
    CallbackEnvelope,
    EffectOutcome,
    OperationRecord,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.prompts import classify
from harness.reconciliation import decide, reconcile
from harness.store import OperationStore
from harness.supervisor import (
    OperationSupervisor,
    RetryBudget,
    next_action_after_uncertain_effect,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


route = RuntimeRoute("claude", "fable", "xhigh", "reviewer-readonly", "a" * 64)
spec = OperationSpec("op-1", "key-1", "review", "owner-1", route, "packet.json", "full")
payload = {"findings": [], "verdict": "approve"}
encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
envelope = CallbackEnvelope("cb-1", "op-1", "run-1", "review", payload, hashlib.sha256(encoded).hexdigest())
with tempfile.TemporaryDirectory(prefix="harness-callback.") as raw:
    store = OperationStore(Path(raw) / "store")
    store.create(spec, lane_id="lane-1", run_id="run-1")
    store.transition("owner-1", "op-1", "preflight")
    store.transition("owner-1", "op-1", "starting")
    store.transition("owner-1", "op-1", "running")
    store.transition("owner-1", "op-1", "awaiting-callback")
    broker = CallbackBroker(store, "owner-1")
    first = broker.accept(envelope)
    after_first = store.read("owner-1", "op-1")
    second = broker.accept(envelope)
    after_second = store.read("owner-1", "op-1")
    check(
        "callback commits one legal transition",
        first.accepted and first.next_state == "finalizing" and after_first.state == "finalizing",
    )
    check(
        "duplicate callback is an idempotent no-op",
        second.duplicate and not second.accepted and after_second == after_first,
    )
    wrong_payload = {"findings": ["changed"], "verdict": "approve"}
    wrong_encoded = json.dumps(wrong_payload, sort_keys=True, separators=(",", ":")).encode()
    wrong = CallbackEnvelope("cb-1", "op-1", "run-1", "review", wrong_payload, hashlib.sha256(wrong_encoded).hexdigest())
    try:
        broker.accept(wrong)
    except CallbackError:
        check("callback identity mutation rejected", True)
    else:
        check("callback identity mutation rejected", False)

    wrong_run = CallbackEnvelope(
        "cb-1",
        "op-1",
        "run-other",
        "review",
        payload,
        hashlib.sha256(encoded).hexdigest(),
    )
    try:
        broker.accept(wrong_run)
    except CallbackError:
        check("callback from a different run is rejected", True)
    else:
        check("callback from a different run is rejected", False)

    store.transition("owner-1", "op-1", "exiting")
    store.transition("owner-1", "op-1", "complete")
    terminal_duplicate = broker.accept(envelope)
    check(
        "exact duplicate stays idempotent after terminal transition",
        terminal_duplicate.duplicate and not terminal_duplicate.accepted,
    )

    terminal_spec = OperationSpec(
        "op-terminal",
        "key-terminal",
        "review",
        "owner-1",
        route,
        "packet.json",
        "full",
    )
    store.create(terminal_spec, lane_id="lane-1", run_id="run-terminal")
    for state in ("preflight", "starting", "running", "finalizing", "exiting", "complete"):
        store.transition("owner-1", "op-terminal", state)
    terminal_envelope = CallbackEnvelope(
        "cb-terminal",
        "op-terminal",
        "run-terminal",
        "review",
        payload,
        hashlib.sha256(encoded).hexdigest(),
    )
    try:
        broker.accept(terminal_envelope)
    except CallbackError:
        check("fresh callback for a terminal operation is rejected", True)
    else:
        check("fresh callback for a terminal operation is rejected", False)

    late_spec = OperationSpec(
        "op-late",
        "key-late",
        "review",
        "owner-1",
        route,
        "packet.json",
        "full",
    )
    store.create(late_spec, lane_id="lane-1", run_id="run-late")
    for state in ("preflight", "starting", "running"):
        store.transition("owner-1", "op-late", state)
    late_envelope = CallbackEnvelope(
        "cb-late",
        "op-late",
        "run-late",
        "review",
        payload,
        hashlib.sha256(encoded).hexdigest(),
    )
    try:
        broker.accept(late_envelope)
    except CallbackError:
        check("callback outside awaiting state is rejected", True)
    else:
        check("callback outside awaiting state is rejected", False)

    concurrent_spec = OperationSpec(
        "op-concurrent",
        "key-concurrent",
        "review",
        "owner-1",
        route,
        "packet.json",
        "full",
    )
    store.create(concurrent_spec, lane_id="lane-1", run_id="run-concurrent")
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition("owner-1", "op-concurrent", state)
    concurrent_envelope = CallbackEnvelope(
        "cb-concurrent",
        "op-concurrent",
        "run-concurrent",
        "review",
        payload,
        hashlib.sha256(encoded).hexdigest(),
    )
    barrier = threading.Barrier(2)

    def accept_concurrently() -> object:
        barrier.wait()
        return CallbackBroker(store, "owner-1").accept(concurrent_envelope)

    with ThreadPoolExecutor(max_workers=2) as pool:
        concurrent_results = list(pool.map(lambda _index: accept_concurrently(), range(2)))
    check(
        "store lock serializes concurrent callback acceptance",
        sum(result.accepted for result in concurrent_results) == 1
        and sum(result.duplicate for result in concurrent_results) == 1,
    )

    expired_parent_spec = OperationSpec(
        "op-expired-parent",
        "key-expired-parent",
        "review",
        "owner-1",
        route,
        "packet.json",
        "full",
    )
    store.create(
        expired_parent_spec,
        lane_id="lane-expired",
        run_id="run-expired-parent",
    )
    OperationSupervisor(
        store, "owner-1", "op-expired-parent"
    ).configure_budget(
        attempt_limit=1,
        model_restart_limit=0,
        time_budget_seconds=1,
        token_limit=100,
        now=1.0,
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition("owner-1", "op-expired-parent", state)
    expired_round_spec = OperationSpec(
        "op-expired-round",
        "key-expired-round",
        "review-round",
        "owner-1",
        route,
        "packet.json",
        "full",
    )
    store.create(
        expired_round_spec,
        lane_id="lane-expired",
        run_id="run-expired-round",
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition("owner-1", "op-expired-round", state)
    expired_payload = {
        **payload,
        "parent_session_operation_id": "op-expired-parent",
    }
    expired_encoded = json.dumps(
        expired_payload, sort_keys=True, separators=(",", ":")
    ).encode()
    expired_envelope = CallbackEnvelope(
        "cb-expired",
        "op-expired-round",
        "run-expired-round",
        "review",
        expired_payload,
        hashlib.sha256(expired_encoded).hexdigest(),
    )
    try:
        CallbackBroker(store, "owner-1").accept(
            expired_envelope,
            deadline_operation_id="op-expired-parent",
        )
    except CallbackError:
        expired_parent = store.read(
            "owner-1", "op-expired-parent"
        )
        expired_round = store.read(
            "owner-1", "op-expired-round"
        )
        check(
            "expired review round loses atomically to its parent deadline",
            expired_parent.state == "attention-required"
            and expired_parent.attention_reason
            == AttentionReason.CALLBACK_TIMEOUT
            and expired_round.state == "awaiting-callback"
            and not expired_round.accepted_callback_id,
        )
    else:
        check(
            "expired review round loses atomically to its parent deadline",
            False,
        )

claude = (
    "Accessing workspace:\nQuick safety check: Is this a project you created or one you trust?\n"
    "1. Yes, I trust this folder\n2. No, exit\nEnter to confirm · Esc to cancel\n"
)
codex = "Do you trust the contents of this directory?\n1. Yes, continue\n2. No, quit\nPress enter to continue\n"
check("exact Claude trust prompt recognized", classify("claude", claude).recognized)
check("exact Codex trust prompt recognized", classify("codex", codex).recognized)
check("near-match receives no input", not classify("claude", claude.replace("Quick safety check:", "Safety check:")).recognized)
check("background exit requires closure arm", not classify("claude", "Background work is running\n1. Exit anyway\nEnter to confirm").recognized)

budget = RetryBudget(read_probe_limit=2).next_probe().next_probe()
try:
    budget.next_probe()
except RuntimeError:
    check("read probe budget is bounded", True)
else:
    check("read probe budget is bounded", False)
check(
    "uncertain effect reconciles before retry",
    next_action_after_uncertain_effect(
        OperationRecord(
            spec,
            "starting",
            1,
            "lane-1",
            "run-1",
            pending_effect="open-surface",
            effect_id="open-surface",
            effect_outcome=EffectOutcome.PENDING,
        )
    )
    == "reconcile",
)

check("dead process closes exact surface", decide("dead", "alive").action == "close-exact")
check(
    "orphan process without guardian fails closed",
    decide("alive", "missing").action == "none"
    and decide("alive", "missing").reason
    == AttentionReason.PROCESS_ORPHANED,
)
check("unknown ownership never mutates", decide("unknown", "alive").action == "none")


class FakeProcess:
    def __init__(self) -> None:
        self.terminated: list[int] = []
    def process_status(self, _pgid: int, _identity: str) -> str:
        return "alive"
    def terminate_exact(self, pgid: int, _identity: str) -> None:
        self.terminated.append(pgid)


class FakeCmux:
    def status(self, _surface: str) -> str:
        return "missing"


owned = OperationRecord(
    spec,
    "running",
    1,
    "lane-1",
    "run-1",
    OwnedResources(
        "11111111-1111-1111-1111-111111111111",
        42,
        43,
        "a" * 64,
        "b" * 64,
    ),
)
process = FakeProcess()
result = reconcile(owned, process, FakeCmux())
check(
    "reconcile never bypasses the sole-parent guardian",
    result.action == "none"
    and result.reason == AttentionReason.PROCESS_ORPHANED
    and process.terminated == [],
)
