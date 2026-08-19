#!/usr/bin/env python3
"""Product-cycle route policy and the third-failure structural pivot."""

from __future__ import annotations

import json
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.finalization_ledger import FinalizationLedger  # noqa: E402
from harness.callbacks import CallbackBroker  # noqa: E402
from harness.contracts import to_dict  # noqa: E402
from harness.finalization_pivot import (  # noqa: E402
    FinalizationPivotError,
    compile_pivot_packet,
    load_accepted_pivot_receipt,
    pivot_packet_sha256,
    pivot_receipt_path,
    pivot_required,
    validate_pivot_receipt,
)
from harness.finalization_policy import (  # noqa: E402
    FinalizationPolicy,
    compile_finalization_routes,
)
from harness.review_finalization import (  # noqa: E402
    StructuralPivotPending,
    reserve_task_finalization_cycle,
)
from harness.store import OperationStore  # noqa: E402
from harness.review_workspace import ReviewWorkspaceBinding  # noqa: E402
from harness.workflows.review import (  # noqa: E402
    ReviewResult,
    ReviewRound,
    review_round_envelope,
)
from model_routing_config import load_tracked_config  # noqa: E402
from outcome_contract import extract_from_bytes  # noqa: E402


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def identity(number: int) -> str:
    return str(uuid.UUID(int=number))


class PivotRuntime:
    """Fake provider transport around the real pivot store and callback broker."""

    def __init__(self, store: OperationStore, owner: str):
        self.store = store
        self.owner = owner
        self.starts = 0
        self.exits = 0
        self.cleanups = 0

    def start(self, request: object, *, on_surface_opened=None) -> object:
        record = self.store.read(self.owner, request.spec.operation_id)
        if record.state == "created":
            record = replace(
                record,
                resources=replace(
                    record.resources,
                    surface_id="55555555-5555-4555-8555-555555555555",
                ),
            )
            self.store.save(record, expected_revision=record.revision)
            if on_surface_opened is not None:
                on_surface_opened(
                    SimpleNamespace(
                        record=self.store.read(
                            self.owner, record.spec.operation_id
                        ),
                        workspace_id="22222222-2222-4222-8222-222222222222",
                        workspace_ref="workspace:2",
                        window_id="33333333-3333-4333-8333-333333333333",
                        window_ref="window:3",
                        surface_ref="surface:5",
                    )
                )
            self.starts += 1
            for state in ("preflight", "starting", "running", "awaiting-callback"):
                self.store.transition(self.owner, record.spec.operation_id, state)
            record = self.store.read(self.owner, record.spec.operation_id)
        return SimpleNamespace(record=record)

    def accept_callback(self, envelope: object) -> object:
        CallbackBroker(self.store, self.owner).accept(
            envelope,
            deadline_operation_id=str(envelope.payload["parent_session_operation_id"]),
        )
        return SimpleNamespace(record=self.store.read(self.owner, envelope.operation_id))

    def request_exit(self, owner: str, operation_id: str) -> object:
        self.exits += 1
        record = self.store.read(owner, operation_id)
        if record.state != "finalizing":
            self.store.transition(owner, operation_id, "finalizing")
        self.store.transition(owner, operation_id, "exiting")
        return SimpleNamespace(record=self.store.read(owner, operation_id))

    def cleanup(self, owner: str, operation_id: str) -> object:
        self.cleanups += 1
        record = self.store.read(owner, operation_id)
        self.store.save(
            replace(
                record,
                resources=replace(record.resources, surface_id=""),
            ),
            expected_revision=record.revision,
        )
        self.store.transition(owner, operation_id, "complete")
        return SimpleNamespace(record=self.store.read(owner, operation_id))


config = load_tracked_config(ROOT)
policy = FinalizationPolicy()

primary = config.finalization_route("finalization-primary")
independent = config.finalization_route("finalization-independent")
check(
    "registered primary route is the Fable High product reviewer",
    primary["runtime"] == "claude"
    and primary["model"] == "fable"
    and primary["effort"] == "high",
)
check(
    "registered structural route is Sol X-High on the other provider",
    independent["runtime"] == "codex"
    and independent["model"] == "gpt-5.6-sol"
    and independent["effort"] == "xhigh",
)

for cycle in (1, 2, 3):
    decision = compile_finalization_routes(
        config=config,
        policy=policy,
        cycle_number=cycle,
        structural_pivot_accepted=True,
        now_epoch=1_000,
    )
    check(
        f"cycle {cycle} stays on the primary route even after a pivot",
        tuple(route.logical_alias for route in decision.routes)
        == ("finalization-primary",)
        and decision.routes[0].effort == "high"
        and decision.reason == "primary-only",
    )

for cycle in (4, 5):
    pivoted = compile_finalization_routes(
        config=config,
        policy=policy,
        cycle_number=cycle,
        structural_pivot_accepted=True,
        now_epoch=1_000,
    )
    check(
        f"cycle {cycle} adds the structural route from the accepted pivot",
        tuple(route.logical_alias for route in pivoted.routes)
        == ("finalization-primary", "finalization-independent")
        and pivoted.reason == "structural-pivot"
        and pivoted.routes[1].effort == "xhigh"
        and pivoted.availability is None,
    )
    unpivoted = compile_finalization_routes(
        config=config,
        policy=policy,
        cycle_number=cycle,
        now_epoch=1_000,
    )
    check(
        f"cycle {cycle} without a pivot performs no availability probe effect",
        tuple(route.logical_alias for route in unpivoted.routes)
        == ("finalization-primary",)
        and unpivoted.reason == "availability-unknown",
    )


# --- Pivot packet and receipt ------------------------------------------------

def material_snapshot(count: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "lineage_id": identity(700),
        "origin_task_id": identity(701),
        "plan_sha256": "a" * 64,
        "outcome_contract_sha256": "b" * 64,
        "max_cycles": 5,
        "terminal_disposition": "",
        "attempts": [],
        "cycles": [
            {
                "number": index,
                "attempt_id": identity(710 + index),
                "exact_head": f"{index:040x}",
                "task_id": identity(720 + index),
                "worktree": f"/tmp/pivot-{index}",
                "provider_policy": {"routes": ["finalization-primary"], "reason": "primary-only"},
                "terminal_result": "changes-requested",
            }
            for index in range(1, count + 1)
        ],
    }


check(
    "two material failures do not require a pivot",
    pivot_required(material_snapshot(2)) is False,
)
check(
    "the exact third material failure requires the pivot",
    pivot_required(material_snapshot(3)) is True,
)
packet = compile_pivot_packet(material_snapshot(3))
check(
    "the pivot packet freezes exactly the three failed attempts",
    packet["kind"] == "structural-pivot-packet"
    and [row["number"] for row in packet["material_cycles"]] == [1, 2, 3]
    and packet["lineage_id"] == identity(700),
)

receipt = {
    "schema_version": 1,
    "kind": "structural-pivot-receipt",
    "lineage_id": identity(700),
    "packet_sha256": pivot_packet_sha256(packet),
    "route_alias": "finalization-independent",
    "read_only": True,
    "structural_recommendation": "Consolidate the callback owner seam.",
    "status": "accepted",
}
check(
    "an accepted receipt binds the exact packet and structural route",
    validate_pivot_receipt(receipt, snapshot=material_snapshot(3)) == receipt,
)
for mutation in (
    {"packet_sha256": "0" * 64},
    {"route_alias": "finalization-primary"},
    {"status": "proposed"},
    {"read_only": False},
    {"structural_recommendation": ""},
):
    try:
        validate_pivot_receipt(
            {**receipt, **mutation}, snapshot=material_snapshot(3)
        )
    except FinalizationPivotError:
        print(f"OK   receipt mutation fails closed: {sorted(mutation)}")
    else:
        raise AssertionError(f"receipt mutation passed: {mutation}")


# --- Reservation boundary ----------------------------------------------------

with tempfile.TemporaryDirectory(prefix="finalization-pivot-reserve.") as raw:
    root = Path(raw)
    plan_sha256 = "c" * 64
    outcome_sha256 = "d" * 64
    lineage = identity(800)
    ledger = FinalizationLedger(
        root / "ledger",
        lineage_id=lineage,
        origin_task_id=lineage,
        plan_sha256=plan_sha256,
        outcome_contract_sha256=outcome_sha256,
    )
    meta = {
        "version": 4,
        "task_id": lineage,
        "approved_plan_sha256": plan_sha256,
        "outcome_contract_sha256": outcome_sha256,
        "review_policy": {
            "mode": "simple",
            "cross_model": False,
            "runtime": "",
            "model": "",
            "effort": "",
            "max_verify_iterations": 1,
            "verification_profile": "scoped",
            "verification_profile_sha256": "9" * 64,
        },
        "finalization_policy": {
            "max_cycles": 5,
            "add_independent_model_after": 3,
            "execution": "ephemeral",
            "primary_route_alias": "finalization-primary",
            "independent_route_alias": "finalization-independent",
        },
    }

    def reserve(number: int, **pivot: object):
        if pivot:
            pivot.setdefault(
                "review_workspace",
                ReviewWorkspaceBinding(
                    review_operation_id="review-cycle-3",
                    workspace_id="22222222-2222-4222-8222-222222222222",
                    workspace_ref="workspace:2",
                    window_id="33333333-3333-4333-8333-333333333333",
                    window_ref="window:3",
                    anchor_surface_id="44444444-4444-4444-8444-444444444444",
                    anchor_surface_ref="surface:4",
                ),
            )
        return reserve_task_finalization_cycle(
            meta,
            ledger=ledger,
            config=config,
            attempt_id=identity(810 + number),
            exact_head=f"{number:040x}",
            task_id=lineage,
            worktree=f"/tmp/pivot-reserve-{number}",
            independent_permitted=True,
            availability=None,
            now_epoch=1_000,
            **pivot,
        )

    for number in (1, 2, 3):
        reservation = reserve(number)
        check(
            f"product cycle {number} reserves on the primary route",
            reservation is not None
            and reservation.cycle.allowed
            and reservation.routes is not None
            and tuple(
                route.logical_alias for route in reservation.routes.routes
            )
            == ("finalization-primary",),
        )
        ledger.record_terminal(
            attempt_id=identity(810 + number),
            terminal_result="changes-requested",
        )
    packet = compile_pivot_packet(ledger.snapshot())
    pivot_receipt_path(ledger.root, lineage).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "structural-pivot-receipt",
                "lineage_id": lineage,
                "packet_sha256": pivot_packet_sha256(packet),
                "route_alias": "finalization-independent",
                "read_only": True,
                "structural_recommendation": (
                    "Consolidate the callback ingestion owner."
                ),
                "status": "accepted",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    class PivotStub:
        def __init__(self, status: str):
            self.status = status
            self.reconciles = 0
            self.starts = 0

        def reconcile(self, *_args: object, **_kwargs: object):
            self.reconciles += 1
            return type("PivotResult", (), {"status": self.status, "reason": ""})()

        def start(self, *_args: object, **_kwargs: object):
            self.starts += 1
            return type("PivotResult", (), {"status": self.status, "reason": ""})()

    pending = PivotStub("in-flight")
    before_pending = ledger.path.read_bytes()
    try:
        reserve(
            4,
            pivot_workflow=pending,
            pivot_runtime=object(),
        )
    except StructuralPivotPending:
        check(
            "a disk-only receipt cannot bypass workflow cleanup before cycle four",
            pending.reconciles == 1
            and pending.starts == 0
            and ledger.path.read_bytes() == before_pending,
        )
    else:
        check(
            "a disk-only receipt cannot bypass workflow cleanup before cycle four",
            False,
        )

    accepted = PivotStub("accepted")
    fourth = reserve(
        4,
        pivot_workflow=accepted,
        pivot_runtime=object(),
    )
    check(
        "the accepted and cleaned pivot workflow opens cycle four with both routes",
        fourth is not None
        and fourth.cycle.allowed
        and fourth.cycle.cycle_number == 4
        and fourth.routes is not None
        and tuple(route.logical_alias for route in fourth.routes.routes)
        == ("finalization-primary", "finalization-independent")
        and fourth.routes.reason == "structural-pivot",
    )
    ledger.record_terminal(
        attempt_id=identity(814), terminal_result="changes-requested"
    )
    fifth = reserve(
        5,
        pivot_workflow=accepted,
        pivot_runtime=object(),
    )
    check(
        "the fifth cycle keeps the structural route",
        fifth is not None
        and fifth.cycle.allowed
        and fifth.cycle.cycle_number == 5
        and fifth.routes is not None
        and fifth.routes.reason == "structural-pivot",
    )
    exhausted = ledger.record_terminal(
        attempt_id=identity(815), terminal_result="changes-requested"
    )
    check(
        "the fifth material failure exhausts the lineage",
        exhausted.terminal_disposition == "finalization-budget-exhausted",
    )
    before_sixth = ledger.path.read_bytes()
    sixth = reserve(6)
    check(
        "a sixth attempt has zero route, session, and ledger effect",
        sixth is not None
        and not sixth.cycle.allowed
        and sixth.cycle.reason == "finalization-budget-exhausted"
        and ledger.path.read_bytes() == before_sixth,
    )


# Production wiring: real workflow construction, store, callback, and continuation.
with tempfile.TemporaryDirectory(prefix="finalization-pivot-wiring.") as raw:
    vault = Path(raw)
    product = vault / "product"
    product.mkdir()
    store_root = vault / ".vault-meta" / "harness"
    lineage = identity(900)
    wired_ledger = FinalizationLedger(
        store_root / "finalization-ledger",
        lineage_id=lineage,
        origin_task_id=lineage,
        plan_sha256="e" * 64,
        outcome_contract_sha256="f" * 64,
    )
    wired_meta = {
        **meta,
        "task_id": lineage,
        "vault_root": str(vault),
        "task_surface": "11111111-1111-4111-8111-111111111111",
        "approved_plan_sha256": "e" * 64,
        "outcome_contract_sha256": "f" * 64,
    }
    for number in (1, 2, 3):
        attempt = identity(920 + number)
        reserved = reserve_task_finalization_cycle(
            wired_meta,
            ledger=wired_ledger,
            config=config,
            attempt_id=attempt,
            exact_head=f"{number:040x}",
            task_id=lineage,
            worktree=str(product),
            independent_permitted=True,
            availability=None,
            now_epoch=1_000,
        )
        assert reserved is not None and reserved.cycle.allowed
        wired_ledger.record_terminal(
            attempt_id=attempt, terminal_result="changes-requested"
        )
    store = OperationStore(store_root)
    gate_root = store_root / "review-data" / lineage / lineage
    gate_root.mkdir(parents=True)
    (gate_root / "review-gate.json").write_text(
        json.dumps(
            {
                "review_workspace": ReviewWorkspaceBinding(
                    review_operation_id="review-cycle-3",
                    workspace_id="22222222-2222-4222-8222-222222222222",
                    workspace_ref="workspace:2",
                    window_id="33333333-3333-4333-8333-333333333333",
                    window_ref="window:3",
                    anchor_surface_id="44444444-4444-4444-8444-444444444444",
                    anchor_surface_ref="surface:4",
                ).payload()
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = PivotRuntime(store, lineage)
    try:
        reserve_task_finalization_cycle(
            wired_meta,
            ledger=wired_ledger,
            config=config,
            attempt_id=identity(924),
            exact_head=f"{4:040x}",
            task_id=lineage,
            worktree=str(product),
            independent_permitted=True,
            availability=None,
            now_epoch=1_000,
            pivot_runtime=runtime,
        )
    except StructuralPivotPending:
        pass
    else:
        raise AssertionError("production pivot wiring did not wait for callback")
    records = store.list(lineage)
    parent = next(row for row in records if row.spec.kind == "structural-pivot")
    child = next(row for row in records if row.spec.kind == "review-round")
    round_ = ReviewRound(
        parent.spec.operation_id,
        child.spec.operation_id,
        lineage,
        child.lane_id,
        child.run_id,
        "openai-holistic",
        0,
        child.spec,
    )
    envelope = review_round_envelope(
        round_, ReviewResult("openai-holistic", "approve", ())
    )
    callback = (
        store_root
        / "structural-pivots"
        / lineage
        / "callbacks"
        / ".review-callback.json"
    )
    callback.write_text(json.dumps(to_dict(envelope), sort_keys=True) + "\n")
    fourth = reserve_task_finalization_cycle(
        wired_meta,
        ledger=wired_ledger,
        config=config,
        attempt_id=identity(924),
        exact_head=f"{4:040x}",
        task_id=lineage,
        worktree=str(product),
        independent_permitted=True,
        availability=None,
        now_epoch=1_000,
        pivot_runtime=runtime,
    )
    repeated = reserve_task_finalization_cycle(
        wired_meta,
        ledger=wired_ledger,
        config=config,
        attempt_id=identity(924),
        exact_head=f"{4:040x}",
        task_id=lineage,
        worktree=str(product),
        independent_permitted=True,
        availability=None,
        now_epoch=1_000,
        pivot_runtime=runtime,
    )
    check(
        "production pivot wiring opens cycle four exactly once after callback",
        fourth is not None
        and fourth.routes is not None
        and fourth.routes.reason == "structural-pivot"
        and repeated is not None
        and repeated.cycle.reason == "already-reserved"
        and runtime.starts == 1
        and runtime.exits == 1
        and runtime.cleanups == 1,
    )

print("\nAll finalization policy tests passed.")
