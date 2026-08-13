#!/usr/bin/env python3
"""Named durable-prefix crashes restart through production constructors."""

from __future__ import annotations

import inspect
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "harness"))

from harness.runtime_sessions import RuntimeSessionManager  # noqa: E402
from harness.runtime_worker import run as runtime_worker_run  # noqa: E402
from harness.runtime_worker_execution import RuntimeWorkerExecution  # noqa: E402
from harness.runtime_worker_custom import RuntimeWorkerCustomMixin  # noqa: E402
from harness.review_continuation_recovery import (  # noqa: E402
    RecoverySnapshot,
    classify_review_continuation,
)
from harness.contracts import (  # noqa: E402
    AttentionReason,
    OperationSpec,
    RuntimeRoute,
)
from harness.store import OperationStore  # noqa: E402
from lifecycle_simulator import (  # noqa: E402
    LifecycleWorld,
    SimulatedCrash,
)
from lifecycle_simulator_oracle import assert_snapshot  # noqa: E402


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def expect_crash(label: str, callback, boundary: str) -> None:
    try:
        callback()
    except SimulatedCrash as exc:
        check(label, str(exc) == boundary, exc)
    else:
        raise AssertionError(f"{label}: failpoint did not crash")


def prepare_before_boundary(world: LifecycleWorld, boundary: str) -> None:
    if boundary == "effect-resolved":
        world.apply({"action": "reserve-effect", "effect_id": "before-effect"})
    elif boundary == "cleanup-receipt-published":
        world.apply({"action": "start-worker"})
        world.apply({"action": "publish-provider-event", "kind": "result-published"})
        world.apply({"action": "publish-callback"})
        result = world.manager.request_exit("sim-owner", "sim-operation")
        if result.action != "exit-requested":
            raise AssertionError("cleanup precondition did not enter exiting")
        world.cmux.disappear()
        world._publish_liveness()


def invoke_boundary(world: LifecycleWorld, boundary: str) -> None:
    if boundary == "operation-transition-published":
        world.store.transition("sim-owner", "sim-operation", "preflight")
    elif boundary == "liveness-published":
        world.apply({"action": "publish-liveness"})
    elif boundary == "error-latch-published":
        world.apply({"action": "publish-error-latch"})
    elif boundary == "effect-reserved":
        world.apply({"action": "reserve-effect", "effect_id": "before-effect"})
    elif boundary == "effect-resolved":
        world.apply({"action": "resolve-effect", "outcome": "succeeded"})
    elif boundary == "cleanup-receipt-published":
        world.apply({"action": "close"})
    else:
        raise AssertionError(f"unknown crash boundary: {boundary}")


for boundary in (
    "operation-transition-published",
    "liveness-published",
    "error-latch-published",
    "effect-reserved",
    "effect-resolved",
    "cleanup-receipt-published",
):
    with tempfile.TemporaryDirectory(
        prefix=f"lifecycle-crash-before-{boundary}."
    ) as raw:
        root = Path(raw)
        world = LifecycleWorld.fresh(root)
        prepare_before_boundary(world, boundary)
        durable_prefix = world.durable_digest()
        world.apply(
            {"action": "crash-at", "failpoint": boundary, "phase": "before"}
        )
        expect_crash(
            f"crash before {boundary} is reached through its production owner",
            lambda world=world, boundary=boundary: invoke_boundary(world, boundary),
            boundary,
        )
        check(
            f"crash before {boundary} preserves the prerequisite durable prefix",
            world.durable_digest() == durable_prefix,
        )
        world = LifecycleWorld.restart(root)
        invoke_boundary(world, boundary)
        if boundary == "operation-transition-published":
            world._publish_liveness()
        check(
            f"restart before {boundary} converges through the same owner",
            world.record().state
            in {"created", "preflight", "exiting", "complete"}
            and world.real_effect_counts()
            == {"provider": 0, "model": 0, "cmux": 0, "network": 0},
            world.snapshot(),
        )


with tempfile.TemporaryDirectory(prefix="lifecycle-operation-crash.") as raw:
    root = Path(raw)
    world = LifecycleWorld.fresh(root)
    world.apply(
        {
            "action": "crash-at",
            "failpoint": "operation-transition-published",
            "phase": "after",
        }
    )
    expect_crash(
        "operation transition crashes only after its durable record",
        lambda: world.store.transition(
            "sim-owner", "sim-operation", "preflight"
        ),
        "operation-transition-published",
    )
    world.apply({"action": "restart-worker"})
    restarted = world.snapshot()
    check(
        "restart worker converges the published operation through its production poll",
        restarted["operation"]["state"] == "preflight"
        and restarted["liveness"]["operation_state"] == "preflight"
        and restarted["liveness"]["operation_revision"]
        == restarted["operation"]["revision"],
        restarted,
    )


with tempfile.TemporaryDirectory(prefix="lifecycle-effect-crash.") as raw:
    root = Path(raw)
    world = LifecycleWorld.fresh(root)
    world.apply({"action": "start-worker"})
    provider_effects = world.provider.effects()
    world.apply(
        {"action": "crash-at", "failpoint": "effect-reserved", "phase": "after"}
    )
    expect_crash(
        "effect reservation crashes after the pending identity is durable",
        lambda: world.apply(
            {"action": "reserve-effect", "effect_id": "matrix-effect"}
        ),
        "effect-reserved",
    )
    world = LifecycleWorld.restart(root)
    check("restart rehydrates the pending effect", world.record().pending_effect == "matrix-effect")
    world.apply({"action": "reserve-effect", "effect_id": "matrix-effect"})
    world.apply(
        {"action": "crash-at", "failpoint": "effect-resolved", "phase": "after"}
    )
    expect_crash(
        "effect resolution crashes after the terminal disposition is durable",
        lambda: world.apply({"action": "resolve-effect", "outcome": "succeeded"}),
        "effect-resolved",
    )
    world = LifecycleWorld.restart(root)
    world.apply({"action": "resolve-effect", "outcome": "succeeded"})
    check(
        "effect replay preserves the single provider-facing identity",
        world.provider.effects() == provider_effects,
        world.provider.effects(),
    )


with tempfile.TemporaryDirectory(prefix="lifecycle-liveness-crash.") as raw:
    root = Path(raw)
    world = LifecycleWorld.fresh(root)
    world.apply(
        {"action": "crash-at", "failpoint": "liveness-published", "phase": "after"}
    )
    expect_crash(
        "liveness publication crashes after atomic state replacement",
        lambda: world.apply({"action": "publish-liveness"}),
        "liveness-published",
    )
    world = LifecycleWorld.restart(root)
    assert_snapshot(world.snapshot())


with tempfile.TemporaryDirectory(prefix="lifecycle-latch-crash.") as raw:
    root = Path(raw)
    world = LifecycleWorld.fresh(root)
    world.apply(
        {"action": "crash-at", "failpoint": "error-latch-published", "phase": "after"}
    )
    expect_crash(
        "error latch crashes after the immutable failure fact is durable",
        lambda: world.apply({"action": "publish-error-latch"}),
        "error-latch-published",
    )
    latch = world.runtime_root / "callback-error.json"
    before = latch.read_bytes()
    world = LifecycleWorld.restart(root)
    world.apply({"action": "worker-tick"})
    check("restart preserves the original error marker bytes", latch.read_bytes() == before)


with tempfile.TemporaryDirectory(prefix="lifecycle-cleanup-crash.") as raw:
    root = Path(raw)
    world = LifecycleWorld.fresh(root)
    world.apply({"action": "start-worker"})
    world.apply({"action": "publish-provider-event", "kind": "result-published"})
    world.apply({"action": "publish-callback"})
    provider_effects = world.provider.effects()
    world.apply(
        {
            "action": "crash-at",
            "failpoint": "cleanup-receipt-published",
            "phase": "after",
        }
    )
    expect_crash(
        "cleanup crashes after the exact close receipt is durable",
        lambda: world.apply({"action": "close"}),
        "cleanup-receipt-published",
    )
    world = LifecycleWorld.restart(root)
    world.apply({"action": "close"})
    check(
        "cleanup restart converges once without replaying provider input",
        world.record().state == "complete"
        and world.resource_close_count() == 1
        and world.provider.effects() == provider_effects,
        world.snapshot(),
    )


with tempfile.TemporaryDirectory(prefix="lifecycle-unconsumed.") as raw:
    world = LifecycleWorld.fresh(Path(raw))
    world.apply(
        {"action": "crash-at", "failpoint": "effect-reserved", "phase": "after"}
    )
    world.apply({"action": "advance-clock", "delta": 1})
    try:
        world.crashes.assert_consumed()
    except AssertionError:
        check("an armed but unconsumed failpoint makes the gate red", True)
    else:
        raise AssertionError("unconsumed failpoint unexpectedly passed")


check(
    "fault injection is test-only and absent from production CLI entrypoints",
    "fault_observer" in inspect.signature(OperationStore).parameters
    and "fault_observer" in inspect.signature(RuntimeWorkerExecution.execute).parameters
    and "fault_observer" not in inspect.signature(runtime_worker_run).parameters
    and "fault_observer" not in inspect.signature(RuntimeSessionManager.for_root).parameters,
)
with tempfile.TemporaryDirectory(prefix="lifecycle-crash-audit.") as raw:
    world = LifecycleWorld.fresh(Path(raw))
    check(
        "the simulator performed no real external effects",
        world.real_effect_counts()
        == {"provider": 0, "model": 0, "cmux": 0, "network": 0},
    )


# --- Corridor durable-boundary crash matrix (E267.RC1.CRASH_MATRIX) ---------
#
# Named before/after crash points across the supported engineering/change
# corridor: verification receipt, review callback acceptance, findings
# publication, refreshed-summary notification, approval acceptance, and
# reap-finalize.  Every crash kills one worker generation at the armed seam;
# the restarted generation must converge the complete corridor without a
# coordinator resume, a duplicated provider identity, or an extra ledger
# cycle.

import json  # noqa: E402

from harness.finalization_ledger import FinalizationLedger  # noqa: E402
from harness.supervisor import OperationSupervisor  # noqa: E402
from harness.workflows.reap import run_reap  # noqa: E402
from lifecycle_simulator_world import (  # noqa: E402
    SimulatedWorkerCrash,
    build_corridor_world,
    corridor_autopilot,
    passing_verification_runner,
)


CORRIDOR_BOUNDARIES = (
    ("summary-consumed-before-verification", "verification-command", 1),
    ("verification-receipt-published", "review-session-start", 1),
    ("review-callback-accepted-before", "review-callback-acceptance:before", 1),
    ("review-callback-accepted-after", "review-callback-acceptance", 1),
    ("findings-notification-sent", "findings-notification-send", 1),
    ("resolution-reverification", "verification-command", 4),
    ("refresh-notification-sent", "refresh-notification-send", 1),
    ("approval-callback-accepted", "review-callback-acceptance", 2),
    ("reap-notification-sent", "reap-notification-send", 1),
)


def corridor_case(index: int) -> str:
    return f"cccc0267-0267-4267-8267-00000000c{index:03d}"


def run_generation(world, calls, *, expect_crash_at: str = "") -> int | None:
    try:
        return world.run_worker_generation(
            verification_runner=passing_verification_runner(
                calls, world.crashes
            ),
            during=lambda w: corridor_autopilot(
                w, initial_head=world.initial_head, timeout=120.0
            ),
            timeout=150.0,
        )
    except SimulatedWorkerCrash as crash:
        if str(crash) != expect_crash_at:
            raise
        return None


for index, (boundary, seam, occurrence) in enumerate(CORRIDOR_BOUNDARIES, 1):
    with tempfile.TemporaryDirectory(
        prefix=f"corridor-crash-{boundary}."
    ) as raw:
        root = Path(raw)
        world = build_corridor_world(root, corridor_case(index))
        world.initial_head = world.head()
        calls: list[tuple[str, ...]] = []
        world.crashes.arm(seam, occurrence=occurrence)
        run_generation(world, calls, expect_crash_at=seam)
        world.crashes.assert_consumed()
        crashed = True
        check(
            f"crash at {boundary} kills the worker generation at its seam",
            crashed and not world.worker_alive(),
            {"observed": world.crashes.observed},
        )
        # Restart: a fresh worker generation and the durable world alone
        # must converge the complete corridor.
        exit_code = run_generation(world, calls)
        record = world.record()
        check(
            f"restart after {boundary} converges to the accepted wiki summary",
            exit_code == 0
            and record.state == "finalizing"
            and record.accepted_callback_kind == "wiki-summary",
            {
                "exit_code": exit_code,
                "state": record.state,
                "attention": (
                    record.attention_reason.value
                    if record.attention_reason
                    else None
                ),
                "gate": world.gate_state().get("status"),
                "faults": [repr(fault) for fault in world.worker_faults],
            },
        )
        gate_state = world.gate_state()
        ledger = FinalizationLedger(
            world.vault / ".vault-meta" / "harness" / "finalization-ledger",
            lineage_id=world.task_id,
            origin_task_id=world.task_id,
            plan_sha256=str(world.meta["approved_plan_sha256"]),
            outcome_contract_sha256=str(
                world.meta["outcome_contract_sha256"]
            ),
        )
        lineage = ledger.snapshot()
        round_records = [
            row
            for row in world.store.list(world.task_id)
            if row.spec.kind == "review-round"
        ]
        accepted_ids = [
            row.accepted_callback_id
            for row in round_records
            if row.accepted_callback_id
        ]
        check(
            f"restart after {boundary} duplicates no callback or ledger effect",
            gate_state.get("status") == "approved"
            and [cycle["terminal_result"] for cycle in lineage["cycles"]]
            == ["changes-requested", "approved"]
            and lineage["terminal_disposition"] == "approved"
            and len(accepted_ids) == len(set(accepted_ids))
            and len(round_records) == 2,
            {
                "gate": gate_state.get("status"),
                "cycles": [
                    cycle["terminal_result"] for cycle in lineage["cycles"]
                ],
                "rounds": len(round_records),
            },
        )


# Reap-finalize crash: the pending effect must survive the crash and resume
# exactly once without a second accepted summary callback.
with tempfile.TemporaryDirectory(prefix="corridor-crash-reap.") as raw:
    root = Path(raw)
    world = build_corridor_world(root, corridor_case(900))
    world.initial_head = world.head()
    calls: list[tuple[str, ...]] = []
    exit_code = run_generation(world, calls)
    record = world.record()
    check(
        "reap crash precondition reaches the accepted wiki summary",
        exit_code == 0 and record.state == "finalizing",
        {"exit_code": exit_code, "state": record.state},
    )
    summary = json.loads(world.summary_path.read_text(encoding="utf-8"))

    def crashing_finalize(_record):
        raise SimulatedWorkerCrash("reap-finalize")

    try:
        run_reap(
            world.store,
            owner_id=world.owner_id,
            operation_id=world.task_id,
            summary=summary,
            finalize=crashing_finalize,
        )
    except SimulatedWorkerCrash:
        pass
    else:
        raise AssertionError("reap finalize crash did not propagate")
    pending = world.record()
    check(
        "reap-finalize crash leaves one durable pending effect",
        pending.pending_effect == "reap-finalize"
        and pending.accepted_callback_kind == "wiki-summary",
        pending,
    )
    resumed = run_reap(
        world.store,
        owner_id=world.owner_id,
        operation_id=world.task_id,
        summary=summary,
        finalize=lambda _record: {"schema_version": 1, "status": "filed"},
    )
    final = world.record()
    check(
        "reap restart resumes the exact pending effect once",
        resumed.result == {"schema_version": 1, "status": "filed"}
        and not final.pending_effect
        and final.accepted_callback_sha256 == pending.accepted_callback_sha256,
        final,
    )

print("\nAll lifecycle crash-matrix tests passed.")


def continuation_decision(name: str, record) -> object:
    fixture = (
        ROOT
        / "tests/harness/fixtures/review-continuation"
        / name
    )
    captured = RecoverySnapshot.from_mapping(
        json.loads(fixture.read_text(encoding="utf-8"))
    )
    return classify_review_continuation(
        replace(
            captured,
            root=replace(
                captured.root,
                owner_id=record.spec.owner_id,
                operation_id=record.spec.operation_id,
                run_id=record.run_id,
                revision=record.revision,
            ),
        )
    )


def continuation_world(base: Path, suffix: str):
    store = OperationStore(base / "harness")
    owner = f"continuation-owner-{suffix}"
    operation_id = f"continuation-root-{suffix}"
    run_id = f"continuation-run-{suffix}"
    store.create(
        OperationSpec(
            operation_id,
            f"continuation-key-{suffix}",
            "dispatch",
            owner,
            RuntimeRoute(
                "codex", "gpt-5.6-sol", "high", "executor", "d" * 64
            ),
            "packets/task.json",
            "scoped",
        ),
        lane_id=f"continuation-lane-{suffix}",
        run_id=run_id,
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(owner, operation_id, state)
    store.transition(
        owner,
        operation_id,
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )
    return store, store.read(owner, operation_id)


for recovery_name, fixture_name in (
    ("review-drive", "review-drive-failed-after-reverify.json"),
    ("accepted-callback", "accepted-callback-pending-ingestion.json"),
):
    with tempfile.TemporaryDirectory(
        prefix=f"continuation-crash-{recovery_name}."
    ) as raw:
        base = Path(raw)
        store, record = continuation_world(base, recovery_name)
        decision = continuation_decision(fixture_name, record)
        state_root = base / "runtime"
        state_root.mkdir()
        receipt_path = state_root / "review-continuation-recovery.json"
        receipt_path.write_text(
            json.dumps(decision.receipt.payload(), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        durable_completed = {"value": False}
        executions: list[str] = []

        class CrashRecoveryWorker(RuntimeWorkerCustomMixin):
            def __init__(self) -> None:
                self.spec = {
                    "callback_mode": "task-summary",
                    "owner_id": record.spec.owner_id,
                    "operation_id": record.spec.operation_id,
                }
                self.spec_path = state_root / "launch.json"
                self.store = store
                self.callback_handled = True
                self.summary_attention_revision = record.revision

            def review_continuation_decision(self):
                return decision

            def execute_review_continuation(self, _decision) -> bool:
                executions.append(recovery_name)
                durable_completed["value"] = True
                raise RuntimeError("crash after registered workflow")

            def review_continuation_recovery_completed(self, _identity) -> bool:
                return durable_completed["value"]

        worker = CrashRecoveryWorker()
        worker.recover_review_continuation()
        transitioned = store.read(record.spec.owner_id, record.spec.operation_id)
        prepared = json.loads(receipt_path.read_text(encoding="utf-8"))
        check(
            f"{recovery_name} crash after workflow preserves one prepared receipt",
            transitioned.state == "awaiting-callback"
            and transitioned.revision == record.revision + 1
            and prepared["status"] == "prepared"
            and executions == [recovery_name],
            (transitioned, prepared, executions),
        )
        restarted = CrashRecoveryWorker()
        restarted.recover_review_continuation()
        finalized = json.loads(receipt_path.read_text(encoding="utf-8"))
        check(
            f"{recovery_name} restart observes durable progress without replay",
            finalized["status"] == "finalized"
            and finalized["outcome"] == "advanced"
            and executions == [recovery_name],
            (finalized, executions),
        )
        restarted.recover_review_continuation()
        check(
            f"{recovery_name} finalized receipt never re-attempts",
            executions == [recovery_name]
            and store.read(
                record.spec.owner_id, record.spec.operation_id
            ).revision
            == transitioned.revision,
        )

    with tempfile.TemporaryDirectory(
        prefix=f"continuation-drift-{recovery_name}."
    ) as raw:
        base = Path(raw)
        store, record = continuation_world(base, f"drift-{recovery_name}")
        decision = continuation_decision(fixture_name, record)
        state_root = base / "runtime"
        state_root.mkdir()
        receipt_path = state_root / "review-continuation-recovery.json"
        prepared_bytes = (
            json.dumps(decision.receipt.payload(), sort_keys=True) + "\n"
        ).encode()
        receipt_path.write_bytes(prepared_bytes)
        store.transition(
            record.spec.owner_id,
            record.spec.operation_id,
            "awaiting-callback",
        )
        store.transition(
            record.spec.owner_id,
            record.spec.operation_id,
            "attention-required",
            reason=AttentionReason.ATTENTION_REQUIRED,
        )
        attempts: list[str] = []

        class DriftWorker(RuntimeWorkerCustomMixin):
            def __init__(self) -> None:
                self.spec = {
                    "callback_mode": "task-summary",
                    "owner_id": record.spec.owner_id,
                    "operation_id": record.spec.operation_id,
                }
                self.spec_path = state_root / "launch.json"
                self.store = store

            def review_continuation_decision(self):
                return decision

            def execute_review_continuation(self, _decision) -> bool:
                attempts.append("executed")
                return True

        DriftWorker().recover_review_continuation()
        check(
            f"{recovery_name} prepared receipt refuses revision drift mutation-free",
            not attempts and receipt_path.read_bytes() == prepared_bytes,
        )
