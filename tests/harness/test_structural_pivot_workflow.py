#!/usr/bin/env python3
"""Store-backed structural-pivot reservation and recovery matrix."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.callbacks import CallbackBroker  # noqa: E402
from harness.contracts import (  # noqa: E402
    OperationSpec,
    RuntimeRoute,
    to_dict,
)
from harness.finalization_pivot import (  # noqa: E402
    MAX_RECOMMENDATION_BYTES,
    compile_pivot_packet,
    load_accepted_pivot_receipt,
    pivot_packet_sha256,
    pivot_receipt_path,
)
from harness.store import OperationStore  # noqa: E402
from harness.workflows.structural_pivot import (  # noqa: E402
    StructuralPivotWorkflow,
    pivot_operation_id,
)
from harness.workflows.review import (  # noqa: E402
    ReviewFinding,
    ReviewResult,
    ReviewRound,
    review_round_envelope,
)
from model_routing_config import RoutingConfig, load_tracked_config  # noqa: E402


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def identity(number: int) -> str:
    return str(uuid.UUID(int=number))


def snapshot(count: int = 3) -> dict[str, object]:
    return {
        "schema_version": 1,
        "lineage_id": identity(900),
        "origin_task_id": identity(900),
        "plan_sha256": "a" * 64,
        "outcome_contract_sha256": "b" * 64,
        "max_cycles": 5,
        "terminal_disposition": "",
        "attempts": [],
        "cycles": [
            {
                "number": index,
                "attempt_id": identity(910 + index),
                "exact_head": f"{index:040x}",
                "task_id": identity(900),
                "worktree": f"/tmp/pivot-workflow-{index}",
                "provider_policy": {
                    "routes": ["finalization-primary"],
                    "reason": "primary-only",
                },
                "terminal_result": "changes-requested",
            }
            for index in range(1, count + 1)
        ],
    }


def workflow(
    root: Path,
    config: RoutingConfig | None = None,
    *,
    fault_observer: object | None = None,
) -> StructuralPivotWorkflow:
    return StructuralPivotWorkflow(
        OperationStore(root),
        config or load_tracked_config(ROOT),
        verification_profile="scoped",
        verification_profile_sha256="c" * 64,
        fault_observer=fault_observer,
    )


class FakeRuntime:
    """Provider/transport double; the real OperationStore owns every transition."""

    def __init__(self, store: OperationStore):
        self.store = store
        self.provider_starts = 0
        self.exit_requests = 0
        self.cleanups = 0

    def start(self, request: object) -> object:
        spec = request.spec
        record = self.store.read(spec.owner_id, spec.operation_id)
        if record.state == "created":
            self.provider_starts += 1
            for state_name in ("preflight", "starting", "running", "awaiting-callback"):
                self.store.transition(spec.owner_id, spec.operation_id, state_name)
            record = self.store.read(spec.owner_id, spec.operation_id)
            return SimpleNamespace(record=record, action="started")
        return SimpleNamespace(record=record, action="already-started")

    def status(self, owner_id: str, operation_id: str) -> object:
        return SimpleNamespace(
            record=self.store.read(owner_id, operation_id), action="observed"
        )

    def accept_callback(self, envelope: object) -> object:
        owner = next(
            record.spec.owner_id
            for record in self.store.list(identity(900))
            if record.spec.operation_id == envelope.operation_id
        )
        accepted = CallbackBroker(self.store, owner).accept(
            envelope,
            deadline_operation_id=str(
                envelope.payload["parent_session_operation_id"]
            ),
        )
        return SimpleNamespace(
            record=self.store.read(owner, envelope.operation_id),
            action="callback-duplicate" if accepted.duplicate else "callback-accepted",
        )

    def request_exit(self, owner_id: str, operation_id: str) -> object:
        record = self.store.read(owner_id, operation_id)
        if record.state == "complete":
            return SimpleNamespace(record=record, action="terminal")
        self.exit_requests += 1
        if record.state != "finalizing":
            self.store.transition(owner_id, operation_id, "finalizing")
        self.store.transition(owner_id, operation_id, "exiting")
        return SimpleNamespace(
            record=self.store.read(owner_id, operation_id), action="exit-requested"
        )

    def cleanup(self, owner_id: str, operation_id: str) -> object:
        record = self.store.read(owner_id, operation_id)
        if record.state == "complete":
            return SimpleNamespace(record=record, action="terminal")
        self.cleanups += 1
        self.store.transition(owner_id, operation_id, "complete")
        return SimpleNamespace(
            record=self.store.read(owner_id, operation_id), action="complete"
        )


class AmbiguousStartRuntime(FakeRuntime):
    """Crash after the provider effect is reserved but before owned identity."""

    def start(self, request: object) -> object:
        spec = request.spec
        record = self.store.read(spec.owner_id, spec.operation_id)
        if record.state == "created":
            self.provider_starts += 1
            self.store.transition(spec.owner_id, spec.operation_id, "preflight")
            self.store.transition(spec.owner_id, spec.operation_id, "starting")
            raise RuntimeError("injected ambiguous provider start")
        return SimpleNamespace(record=record, action="attention-required")


class CrashAt:
    def __init__(self, boundary: str):
        self.boundary = boundary
        self.triggered = False

    def __call__(self, boundary: str) -> None:
        if boundary == self.boundary and not self.triggered:
            self.triggered = True
            raise RuntimeError(f"injected crash at {boundary}")


def active_round(store: OperationStore) -> tuple[object, ReviewRound]:
    owner = identity(900)
    records = store.list(owner)
    parent = next(row for row in records if row.spec.kind == "structural-pivot")
    child = next(row for row in records if row.spec.kind == "review-round")
    return parent, ReviewRound(
        parent_operation_id=parent.spec.operation_id,
        operation_id=child.spec.operation_id,
        owner_id=owner,
        lane_id=child.lane_id,
        run_id=child.run_id,
        axis="openai-holistic",
        verification_iteration=0,
        spec=child.spec,
    )


def publish_callback(
    state: Path,
    store: OperationStore,
    *,
    verdict: str = "approve",
    findings: tuple[ReviewFinding, ...] = (),
) -> object:
    _parent, round_ = active_round(store)
    envelope = review_round_envelope(
        round_,
        ReviewResult(
            axis="openai-holistic",
            verdict=verdict,
            findings=findings,
        ),
    )
    callback = (
        state
        / "structural-pivots"
        / identity(900)
        / "callbacks"
        / ".review-callback.json"
    )
    callback.write_text(
        json.dumps(to_dict(envelope), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return envelope


with tempfile.TemporaryDirectory(prefix="structural-pivot-reserve.") as raw:
    state = Path(raw) / "store"
    owner = identity(900)
    current = snapshot()

    early = workflow(state).reserve(snapshot(2), root_operation_id=owner)
    check(
        "two failures reserve no packet, operation, or provider effect",
        early.status == "not-required"
        and early.operation is None
        and not state.exists(),
        early,
    )

    reserved = workflow(state).reserve(current, root_operation_id=owner)
    check(
        "the exact third failure publishes one immutable packet and operation",
        reserved.status == "reserved"
        and reserved.operation is not None
        and reserved.packet_path is not None
        and reserved.packet_path.is_file()
        and reserved.operation.spec.kind == "structural-pivot"
        and reserved.operation.spec.owner_id == owner
        and reserved.operation.spec.parent_operation_id == owner
        and reserved.operation.spec.root_operation_id == owner
        and reserved.operation.spec.route.profile == "reviewer-callback"
        and reserved.operation.spec.route.runtime == "codex"
        and reserved.operation.spec.route.model == "gpt-5.6-sol"
        and reserved.operation.spec.route.effort == "xhigh",
        reserved,
    )
    packet_bytes = reserved.packet_path.read_bytes()
    operation_bytes = (
        state / "owners" / owner / "operations" / f"{reserved.operation.spec.operation_id}.json"
    ).read_bytes()
    repeated = workflow(state).reserve(current, root_operation_id=owner)
    check(
        "duplicate reservation reuses the exact packet and operation bytes",
        repeated.status == "reserved"
        and repeated.operation == reserved.operation
        and repeated.packet_path.read_bytes() == packet_bytes
        and (
            state
            / "owners"
            / owner
            / "operations"
            / f"{reserved.operation.spec.operation_id}.json"
        ).read_bytes()
        == operation_bytes
        and len(OperationStore(state).list(owner)) == 1,
        repeated,
    )

    for label, mutate in (
        (
            "plan digest drift",
            lambda value: value.update({"plan_sha256": "d" * 64}),
        ),
        (
            "Outcome Contract digest drift",
            lambda value: value.update({"outcome_contract_sha256": "e" * 64}),
        ),
        (
            "terminal HEAD drift",
            lambda value: value["cycles"][2].update({"exact_head": "f" * 40}),
        ),
    ):
        changed = copy.deepcopy(current)
        mutate(changed)
        result = workflow(state).reserve(changed, root_operation_id=owner)
        check(
            f"{label} cannot fork the frozen pivot",
            result.status == "attention"
            and len(OperationStore(state).list(owner)) == 1
            and reserved.packet_path.read_bytes() == packet_bytes,
            result,
        )

with tempfile.TemporaryDirectory(prefix="structural-pivot-artifacts.") as raw:
    state = Path(raw) / "store"
    owner = identity(900)
    expected_id = pivot_operation_id(snapshot())
    packet_dir = state / "structural-pivots" / owner
    packet_dir.mkdir(parents=True)
    target = Path(raw) / "foreign.json"
    target.write_text("{}\n", encoding="utf-8")
    os.symlink(target, packet_dir / "packet.json")
    symlinked = workflow(state).reserve(snapshot(), root_operation_id=owner)
    check(
        "a symlinked packet fails closed before operation creation",
        symlinked.status == "attention"
        and not OperationStore(state).list(owner),
        symlinked,
    )
    (packet_dir / "packet.json").unlink()
    (packet_dir / "packet.json").write_text("{torn\n", encoding="utf-8")
    torn = workflow(state).reserve(snapshot(), root_operation_id=owner)
    check(
        "a torn packet fails closed before operation creation",
        torn.status == "attention" and not OperationStore(state).list(owner),
        torn,
    )
    (packet_dir / "packet.json").unlink()
    conflicting = OperationSpec(
        operation_id=expected_id,
        idempotency_key="f" * 64,
        kind="structural-pivot",
        owner_id=owner,
        route=RuntimeRoute(
            "codex", "gpt-5.6-sol", "xhigh", "reviewer-callback", "1" * 64
        ),
        context_manifest="foreign/packet.json",
        verification_profile="scoped",
        contract_sha256="2" * 64,
        parent_operation_id=owner,
        root_operation_id=owner,
    )
    OperationStore(state).create(conflicting, lane_id="foreign-lane", run_id="foreign-run")
    conflict = workflow(state).reserve(snapshot(), root_operation_id=owner)
    check(
        "a pre-existing conflicting operation identity fails closed",
        conflict.status == "attention"
        and len(OperationStore(state).list(owner)) == 1,
        conflict,
    )

with tempfile.TemporaryDirectory(prefix="structural-pivot-route.") as raw:
    tracked = load_tracked_config(ROOT)
    data = copy.deepcopy(tracked.data)
    data["finalization_routes"]["finalization-independent"]["model"] = "terra"
    alternate = RoutingConfig(ROOT, data, "3" * 64, False)
    routed = workflow(Path(raw) / "store", alternate).reserve(
        snapshot(), root_operation_id=identity(900)
    )
    check(
        "the operation resolves concrete transport only from the logical alias",
        routed.operation is not None
        and routed.operation.spec.route.runtime == "codex"
        and routed.operation.spec.route.model == "gpt-5.6-terra"
        and routed.operation.spec.route.effort == "xhigh",
        routed,
    )

with tempfile.TemporaryDirectory(prefix="structural-pivot-runtime.") as raw:
    state = Path(raw) / "store"
    store = OperationStore(state)
    runtime = FakeRuntime(store)
    flow = workflow(state)
    owner = identity(900)
    started = flow.start(
        snapshot(),
        root_operation_id=owner,
        runtime=runtime,
        origin_surface="11111111-1111-1111-1111-111111111111",
        worktree=ROOT,
    )
    records = store.list(owner)
    parent = next(row for row in records if row.spec.kind == "structural-pivot")
    child = next(row for row in records if row.spec.kind == "review-round")
    callback_dir = state / "structural-pivots" / owner / "callbacks"
    meta = json.loads((callback_dir / ".review-meta.json").read_text(encoding="utf-8"))
    check(
        "start uses one registered read-only review-input session and child round",
        started.status == "in-flight"
        and runtime.provider_starts == 1
        and parent.state == "awaiting-callback"
        and child.state == "awaiting-callback"
        and meta["transport"] == "review-round"
        and meta["operation_id"] == child.spec.operation_id
        and meta["run_id"] == child.run_id
        and meta["parent_session_operation_id"] == parent.spec.operation_id
        and meta["axis"] == "openai-holistic"
        and (callback_dir / ".review-input.json").is_file(),
        started,
    )
    restarted = flow.start(
        snapshot(),
        root_operation_id=owner,
        runtime=runtime,
        origin_surface="11111111-1111-1111-1111-111111111111",
        worktree=ROOT,
    )
    check(
        "repeated start observes the same operation without another provider effect",
        restarted.status == "in-flight"
        and runtime.provider_starts == 1
        and len(store.list(owner)) == 2,
        restarted,
    )

    round_ = ReviewRound(
        parent_operation_id=parent.spec.operation_id,
        operation_id=child.spec.operation_id,
        owner_id=owner,
        lane_id=child.lane_id,
        run_id=child.run_id,
        axis="openai-holistic",
        verification_iteration=0,
        spec=child.spec,
    )
    findings = tuple(
        ReviewFinding(
            finding_id=f"pivot-{index}",
            axis="openai-holistic",
            severity="important",
            file="scripts/harness/review_finalization.py",
            line=index + 1,
            summary=(f"Structural finding {index} " + "s" * 250),
            evidence=("e" * 250),
            recommendation=("r" * 250),
        )
        for index in range(50)
    )
    envelope = review_round_envelope(
        round_,
        ReviewResult(
            axis="openai-holistic",
            verdict="changes-requested",
            findings=findings,
        ),
    )
    (callback_dir / ".review-callback.json").write_text(
        json.dumps(to_dict(envelope), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    accepted = flow.reconcile(
        snapshot(), root_operation_id=owner, runtime=runtime
    )
    receipt = load_accepted_pivot_receipt(
        state / "finalization-ledger", snapshot=snapshot()
    )
    recommendation = json.loads(receipt["structural_recommendation"])
    check(
        "the accepted callback projects one bounded total receipt and cleans resources",
        accepted.status == "accepted"
        and accepted.operation is not None
        and accepted.operation.state == "complete"
        and accepted.operation.resources.surface_id == ""
        and receipt["status"] == "accepted"
        and len(receipt["structural_recommendation"].encode())
        <= MAX_RECOMMENDATION_BYTES
        and recommendation["verdict"] == "changes-requested"
        and recommendation["accepted_review_sha256"] == envelope.payload_sha256
        and recommendation["finding_count"] == 50
        and recommendation["omitted_findings"] > 0
        and len(recommendation["findings"]) + recommendation["omitted_findings"]
        == 50
        and runtime.exit_requests == 1
        and runtime.cleanups == 1,
        accepted,
    )
    duplicate = flow.reconcile(
        snapshot(), root_operation_id=owner, runtime=runtime
    )
    check(
        "duplicate callback reconciliation reuses receipt and cleanup evidence",
        duplicate.status == "accepted"
        and runtime.provider_starts == 1
        and runtime.exit_requests == 1
        and runtime.cleanups == 1,
        duplicate,
    )

with tempfile.TemporaryDirectory(prefix="structural-pivot-ambiguous-start.") as raw:
    state = Path(raw) / "store"
    store = OperationStore(state)
    runtime = AmbiguousStartRuntime(store)
    flow = workflow(state)
    first = flow.start(
        snapshot(),
        root_operation_id=identity(900),
        runtime=runtime,
        origin_surface="11111111-1111-1111-1111-111111111111",
        worktree=ROOT,
    )
    replay = flow.start(
        snapshot(),
        root_operation_id=identity(900),
        runtime=runtime,
        origin_surface="11111111-1111-1111-1111-111111111111",
        worktree=ROOT,
    )
    check(
        "ambiguous start retains one provider effect and never infers replay",
        first.status == "attention"
        and replay.status == "in-flight"
        and runtime.provider_starts == 1
        and next(
            row for row in store.list(identity(900)) if row.spec.kind == "structural-pivot"
        ).state
        == "starting",
    )

with tempfile.TemporaryDirectory(prefix="structural-pivot-manual-receipt.") as raw:
    state = Path(raw) / "store"
    store = OperationStore(state)
    flow = workflow(state)
    flow.reserve(snapshot(), root_operation_id=identity(900))
    packet = compile_pivot_packet(snapshot())
    manual = {
        "schema_version": 1,
        "kind": "structural-pivot-receipt",
        "lineage_id": identity(900),
        "packet_sha256": pivot_packet_sha256(packet),
        "route_alias": "finalization-independent",
        "read_only": True,
        "structural_recommendation": json.dumps(
            {
                "verdict": "approve",
                "accepted_review_sha256": "d" * 64,
                "finding_count": 0,
                "findings": [],
                "omitted_findings": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        "status": "accepted",
    }
    manual_path = pivot_receipt_path(state / "finalization-ledger", identity(900))
    manual_path.parent.mkdir(parents=True)
    manual_path.write_text(
        json.dumps(manual, sort_keys=True) + "\n", encoding="utf-8"
    )
    rejected = flow.reconcile(
        snapshot(), root_operation_id=identity(900), runtime=FakeRuntime(store)
    )
    check(
        "a test-written receipt has no authority without the accepted child operation",
        rejected.status == "attention"
        and "accepted callback operation" in rejected.reason,
        rejected,
    )

with tempfile.TemporaryDirectory(prefix="structural-pivot-foreign-callback.") as raw:
    state = Path(raw) / "store"
    store = OperationStore(state)
    runtime = FakeRuntime(store)
    flow = workflow(state)
    flow.start(
        snapshot(),
        root_operation_id=identity(900),
        runtime=runtime,
        origin_surface="11111111-1111-1111-1111-111111111111",
        worktree=ROOT,
    )
    envelope = publish_callback(state, store)
    callback = state / "structural-pivots" / identity(900) / "callbacks" / ".review-callback.json"
    foreign = to_dict(envelope)
    foreign["run_id"] = "foreign-run"
    callback.write_text(json.dumps(foreign) + "\n", encoding="utf-8")
    mismatch = flow.reconcile(
        snapshot(), root_operation_id=identity(900), runtime=runtime
    )
    check(
        "a callback with foreign operation/run identity fails closed",
        mismatch.status == "attention"
        and runtime.exit_requests == 0
        and not pivot_receipt_path(
            state / "finalization-ledger", identity(900)
        ).exists(),
        mismatch,
    )

for boundary in ("callback-accepted", "receipt-published"):
    with tempfile.TemporaryDirectory(prefix=f"structural-pivot-{boundary}.") as raw:
        state = Path(raw) / "store"
        store = OperationStore(state)
        runtime = FakeRuntime(store)
        crash = CrashAt(boundary)
        faulting = workflow(state, fault_observer=crash)
        faulting.start(
            snapshot(),
            root_operation_id=identity(900),
            runtime=runtime,
            origin_surface="11111111-1111-1111-1111-111111111111",
            worktree=ROOT,
        )
        publish_callback(state, store)
        interrupted = faulting.reconcile(
            snapshot(), root_operation_id=identity(900), runtime=runtime
        )
        recovered = workflow(state).reconcile(
            snapshot(), root_operation_id=identity(900), runtime=runtime
        )
        check(
            f"restart after {boundary} converges without duplicate provider effect",
            crash.triggered
            and interrupted.status == "attention"
            and recovered.status == "accepted"
            and runtime.provider_starts == 1
            and runtime.exit_requests == 1
            and runtime.cleanups == 1,
            recovered,
        )

with tempfile.TemporaryDirectory(prefix="structural-pivot-provider-exit.") as raw:
    state = Path(raw) / "store"
    store = OperationStore(state)
    runtime = FakeRuntime(store)
    flow = workflow(state)
    flow.start(
        snapshot(),
        root_operation_id=identity(900),
        runtime=runtime,
        origin_surface="11111111-1111-1111-1111-111111111111",
        worktree=ROOT,
    )
    parent, _round = active_round(store)
    store.transition(identity(900), parent.spec.operation_id, "failed")
    exited = flow.reconcile(
        snapshot(), root_operation_id=identity(900), runtime=runtime
    )
    check(
        "provider exit without callback becomes typed attention with no replay",
        exited.status == "attention"
        and runtime.provider_starts == 1
        and runtime.exit_requests == 0,
        exited,
    )

print("\nAll structural pivot workflow tests passed.")
