#!/usr/bin/env python3
"""Hermetic RC1 gate proof over real lifecycle stores and fake external ports."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "harness"))

import live_acceptance_rc1_gate as rc1  # noqa: E402
import v267_stabilization as stab  # noqa: E402
from harness.contracts import (  # noqa: E402
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from harness.store import OperationStore  # noqa: E402
from harness.supervisor import OperationSupervisor  # noqa: E402
from harness.workflows.reap import run_reap  # noqa: E402
from lifecycle_simulator_world import (  # noqa: E402
    build_corridor_world,
    corridor_autopilot,
    git,
    passing_verification_runner,
    write_json,
)


DIGEST_A = "a" * 64
TASK_IDS = tuple(
    f"cccc0267-0267-4267-8267-{index:012d}" for index in range(11, 14)
)


def check(label: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def dispatch_spec(path: Path, request_id: str) -> Path:
    write_json(
        path,
        {
            "request_id": request_id,
            "pipeline": "engineering/change",
            "executor": {
                "runtime": "claude",
                "model": "fable",
                "effort": "high",
            },
            "review": {
                "mode": "simple",
                "runtime": "claude",
                "model": "fable",
                "effort": "high",
            },
        },
    )
    return path


def launch_record(world) -> dict[str, object]:
    record = world.record()
    return {
        "schema_version": 1,
        "status": "launched",
        "request_id": world.task_id,
        "worktree": str(world.worktree),
        "harness": {
            "owner_id": world.owner_id,
            "operation_id": world.task_id,
            "lane_id": record.lane_id,
            "run_id": record.run_id,
        },
    }


def reserve_world(gate, world, state_path: Path, spec_path: Path) -> dict[str, object]:
    launch = launch_record(world)

    def launcher(*_args: object, **_kwargs: object) -> dict[str, object]:
        write_json(
            world.vault
            / ".vault-meta"
            / "dispatch-runs"
            / f"{world.task_id}.json",
            {
                "schema_version": 1,
                "request_id": world.task_id,
                "status": "launched",
                "result": launch,
            },
        )
        return launch

    return gate.reserve_and_launch(
        coordinator_authorized=True,
        expected_digest=DIGEST_A,
        state_path=state_path,
        launcher=launcher,
        spec_path=spec_path,
        evidence_root=world.vault,
    )


def complete_world(world) -> tuple[object, list[object]]:
    initial_head = world.head()
    calls: list[tuple[str, ...]] = []
    exit_code = world.run_worker_generation(
        verification_runner=passing_verification_runner(calls),
        during=lambda active: corridor_autopilot(
            active, initial_head=initial_head
        ),
        timeout=90.0,
    )
    summary = json.loads(world.summary_path.read_text(encoding="utf-8"))
    reaped = run_reap(
        world.store,
        owner_id=world.owner_id,
        operation_id=world.task_id,
        summary=summary,
        finalize=lambda _record: {"schema_version": 1, "status": "filed"},
    )
    # Live corridors close their provider session through the cleanup owner,
    # so the terminal record carries request-exit as its final effect; the
    # accepted reap remains durable in the callback and wiki artifacts.
    OperationSupervisor(
        world.store, world.owner_id, world.task_id
    ).effect("request-exit", lambda _record: None)
    world.store.transition(world.owner_id, world.task_id, "exiting")
    world.store.transition(world.owner_id, world.task_id, "complete")
    terminal = world.record()
    rounds = [
        row
        for row in world.store.list(world.task_id)
        if row.spec.kind == "review-round"
    ]
    check(
        f"corridor {world.task_id} completes through real owners",
        exit_code == 0
        and reaped.result == {"schema_version": 1, "status": "filed"}
        and terminal.state == "complete"
        and not terminal.pending_effect
        and len(rounds) == 2,
        terminal,
    )
    return terminal, rounds


def receipt_for(
    world,
    template: dict[str, object],
    terminal,
    rounds: list[object],
) -> dict[str, object]:
    material: dict[str, object] = {"fix_head": world.head()}
    artifact_types = {
        "findings_artifact": "findings",
        "refreshed_summary_artifact": "refreshed-summary",
        "second_verification_artifact": "second-verification",
        "re_review_artifact": "re-review",
    }
    for field, artifact_type in artifact_types.items():
        payload = {
            "schema_version": 1,
            "type": artifact_type,
            "cell_id": template["cell_id"],
            "head_sha": world.head(),
        }
        if field == "re_review_artifact":
            payload["verdict"] = "approve"
        encoded = json.dumps(payload, sort_keys=True).encode()
        relative = (
            "docs/acceptance/evidence/v2.6.7/"
            f"{world.task_id}-{artifact_type}.json"
        )
        path = world.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
        material[field] = {
            "path": relative,
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    return {
        **template,
        "run_id": terminal.run_id,
        "request_id": world.task_id,
        "owner_id": world.owner_id,
        "store_id": f"{world.store.root.resolve()}#owners/{world.owner_id}",
        "worktree_id": str(world.worktree),
        "provider_session_ids": sorted(
            {terminal.run_id, *(row.run_id for row in rounds)}
        ),
        "result": "success",
        "material_cycle": material,
        "resource_free": True,
        "coordinator_recovery": False,
    }


def build_negative_dispatch(
    root: Path, task_id: str, *, terminal_state: str
) -> tuple[Path, OperationStore, dict[str, object]]:
    vault = root / f"negative-vault-{terminal_state}"
    worktree = root / f"negative-worktree-{terminal_state}"
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    git(worktree, "config", "user.email", "corridor@example.invalid")
    git(worktree, "config", "user.name", "Corridor World")
    (worktree / "product.txt").write_text("ready\n", encoding="utf-8")
    git(worktree, "add", "product.txt")
    git(worktree, "commit", "-m", "ready")
    write_json(
        worktree / ".task-meta.json",
        {
            "task_id": task_id,
            "worktree": str(worktree),
            "vault_root": str(vault),
            "review_policy": {
                "mode": "simple",
                "runtime": "claude",
                "model": "fable",
                "effort": "high",
            },
        },
    )
    store = OperationStore(vault / ".vault-meta" / "harness")
    store.create(
        OperationSpec(
            task_id,
            f"key-{task_id}",
            "dispatch",
            task_id,
            RuntimeRoute("claude", "fable", "high", "executor", "a" * 64),
            "packets/task.json",
            "scoped",
        ),
        lane_id=f"lane-{terminal_state}",
        run_id=f"run-{terminal_state}",
    )
    if terminal_state == "failed":
        store.transition(task_id, task_id, "failed")
    else:
        store.transition(task_id, task_id, "cancelling")
        store.transition(task_id, task_id, "exiting")
        store.transition(task_id, task_id, "cancelled")
    record = store.read(task_id, task_id)
    launch = {
        "schema_version": 1,
        "status": "launched",
        "request_id": task_id,
        "worktree": str(worktree),
        "harness": {
            "owner_id": task_id,
            "operation_id": task_id,
            "lane_id": record.lane_id,
            "run_id": record.run_id,
        },
    }
    write_json(
        vault / ".vault-meta" / "dispatch-runs" / f"{task_id}.json",
        {
            "schema_version": 1,
            "request_id": task_id,
            "status": "launched",
            "result": launch,
        },
    )
    return vault, store, launch


def build_reserved_never_launched(
    root: Path,
    task_id: str,
    *,
    name: str,
    spec_sha256: str,
    root_state: str = "cancelled",
    effect_outcome: object = None,
    dispatch_status: str = "failed",
    extra_event_kind: str = "",
    resources: OwnedResources | None = None,
) -> tuple[Path, OperationStore, Path]:
    """One durable pre-launch failure: reserved claim, failed run, closed root."""

    from harness.contracts import EffectOutcome

    vault = root / f"reserved-vault-{name}"
    write_json(
        vault / ".vault-meta" / "dispatch-runs" / f"{task_id}.json",
        {
            "schema_version": 1,
            "request_id": task_id,
            "request_sha256": spec_sha256,
            "status": dispatch_status,
            "stage": "provider-runtime",
            "failure": "provider start requires attention: attention-required",
        },
    )
    store = OperationStore(vault / ".vault-meta" / "harness")
    store.create(
        OperationSpec(
            task_id,
            f"key-{task_id}",
            "dispatch",
            task_id,
            RuntimeRoute("claude", "fable", "high", "executor", "a" * 64),
            "packets/task.json",
            "scoped",
        ),
        lane_id=f"lane-{name}",
        run_id=f"run-{name}",
    )
    record = store.read(task_id, task_id)
    shaped = replace(
        record,
        state=root_state,
        effect_id="start-provider",
        effect_outcome=(
            effect_outcome if effect_outcome is not None else EffectOutcome.FAILED
        ),
        pending_effect="",
        resources=resources if resources is not None else OwnedResources(),
        revision=record.revision + 1,
    )
    store.save(shaped, expected_revision=record.revision)
    events = (
        store.root
        / "owners"
        / task_id
        / "runtime"
        / task_id
        / "provider-events"
        / "generation-1"
        / "events"
    )
    write_json(events / "0001.json", {"schema_version": 1, "kind": "provider-started"})
    if extra_event_kind:
        write_json(
            events / "0002.json", {"schema_version": 1, "kind": extra_event_kind}
        )
    state_path = vault / ".vault-meta/acceptance/rc1-streak-state.json"
    write_json(
        state_path,
        {
            "schema_version": 1,
            "expected_digest": DIGEST_A,
            "receipts": [],
            "reservation": {
                "cell_id": "rc1-corridor-run-1",
                "status": "reserved",
                "spec_sha256": spec_sha256,
                "request_id": task_id,
                "lifecycle_subject_sha256": DIGEST_A,
            },
        },
    )
    return vault, store, state_path


def check_reserved_never_launched_closure(root: Path, gate) -> None:
    """The exact eecdb3d8 shape closes fail-closed and idempotently."""

    task_id = "cccc0267-0267-4267-8267-000000000031"
    spec_sha256 = "3" * 64
    vault, _store, state_path = build_reserved_never_launched(
        root, task_id, name="closure", spec_sha256=spec_sha256
    )
    closure = gate.abandon_reserved(
        request_id=task_id,
        expected_digest=DIGEST_A,
        state_path=state_path,
        evidence_root=vault,
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    check(
        "a never-launched reserved claim closes as an invalidated negative",
        closure["result"] == "invalidated"
        and closure["request_id"] == task_id
        and closure["cell_id"] == "rc1-corridor-run-1"
        and state["reservation"] is None
        and state["receipts"] == [],
        (closure, state),
    )
    plan = gate.plan([], expected_digest=DIGEST_A, evidence_root=vault)
    check(
        "reserved closure advances no sequence or streak evidence",
        plan["next_cell"] == "rc1-corridor-run-1" and plan["streak"] == 0,
        plan,
    )
    repeat = gate.abandon_reserved(
        request_id=task_id,
        expected_digest=DIGEST_A,
        state_path=state_path,
        evidence_root=vault,
    )
    check(
        "repeating the same reserved closure is idempotent",
        repeat["result"] == "invalidated"
        and repeat["request_id"] == task_id
        and json.loads(state_path.read_text(encoding="utf-8"))["reservation"] is None,
        repeat,
    )
    dispatch_run = json.loads(
        (
            vault / ".vault-meta" / "dispatch-runs" / f"{task_id}.json"
        ).read_text(encoding="utf-8")
    )
    check(
        "reserved closure retains the durable failure artifacts",
        dispatch_run["status"] == "failed"
        and _store.read(task_id, task_id).state == "cancelled",
        dispatch_run,
    )

    def replacement_launcher(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": "launched",
            "request_id": "cccc0267-0267-4267-8267-000000000032",
            "worktree": str(vault),
            "harness": {
                "owner_id": "cccc0267-0267-4267-8267-000000000032",
                "operation_id": "cccc0267-0267-4267-8267-000000000032",
                "lane_id": "closure-replacement",
                "run_id": "closure-replacement",
            },
        }

    replacement = gate.reserve_and_launch(
        coordinator_authorized=True,
        expected_digest="b" * 64,
        state_path=state_path,
        launcher=replacement_launcher,
        spec_path=dispatch_spec(
            root / "closure-replacement.json",
            "cccc0267-0267-4267-8267-000000000032",
        ),
        evidence_root=vault,
    )
    check(
        "reserved closure permits cell 1 on the post-repair digest",
        replacement["cell_id"] == "rc1-corridor-run-1"
        and replacement["request_id"] == "cccc0267-0267-4267-8267-000000000032",
        replacement,
    )


def check_reserved_closure_rejections(root: Path, gate) -> None:
    """Every ambiguous or non-negative shape leaves the claim untouched."""

    from harness.contracts import EffectOutcome

    rejections = (
        (
            "a live root cannot close a reserved claim",
            dict(root_state="attention-required"),
            {},
        ),
        (
            "an accepted input forbids reserved closure",
            dict(extra_event_kind="input-accepted"),
            {},
        ),
        (
            "a provider result forbids reserved closure",
            dict(extra_event_kind="provider-result"),
            {},
        ),
        (
            "a succeeded start effect forbids reserved closure",
            dict(effect_outcome=EffectOutcome.SUCCEEDED),
            {},
        ),
        (
            "owned resources forbid reserved closure",
            dict(resources=OwnedResources(surface_id="leaked-surface")),
            {},
        ),
        (
            "a launched dispatch-run record forbids reserved closure",
            dict(dispatch_status="launched"),
            {},
        ),
        (
            "a foreign request cannot close the reserved claim",
            {},
            dict(request_id="cccc0267-0267-4267-8267-000000000099"),
        ),
    )
    for index, (label, shape, closure_kwargs) in enumerate(rejections, start=41):
        task_id = f"cccc0267-0267-4267-8267-0000000000{index}"
        vault, _store, state_path = build_reserved_never_launched(
            root,
            task_id,
            name=f"reject-{index}",
            spec_sha256="4" * 64,
            **shape,
        )
        unchanged = state_path.read_bytes()
        try:
            gate.abandon_reserved(
                request_id=str(closure_kwargs.get("request_id", task_id)),
                expected_digest=DIGEST_A,
                state_path=state_path,
                evidence_root=vault,
            )
        except stab.StabilizationError:
            check(label, state_path.read_bytes() == unchanged)
        else:
            raise AssertionError(label)

    task_id = "cccc0267-0267-4267-8267-000000000039"
    vault, _store, state_path = build_reserved_never_launched(
        root, task_id, name="launched-claim", spec_sha256="5" * 64
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["reservation"]["status"] = "launched"
    state["reservation"]["launch"] = {
        "schema_version": 1,
        "status": "launched",
        "request_id": task_id,
        "worktree": str(vault),
        "harness": {
            "owner_id": task_id,
            "operation_id": task_id,
            "lane_id": "lane-launched-claim",
            "run_id": "run-launched-claim",
        },
    }
    write_json(state_path, state)
    unchanged = state_path.read_bytes()
    try:
        gate.abandon_reserved(
            request_id=task_id,
            expected_digest=DIGEST_A,
            state_path=state_path,
            evidence_root=vault,
        )
    except stab.StabilizationError:
        check(
            "a launched reservation must be completed by record, not closed",
            state_path.read_bytes() == unchanged,
        )
    else:
        raise AssertionError(
            "a launched reservation must be completed by record, not closed"
        )


with tempfile.TemporaryDirectory(prefix="rc1-live-streak-gate.") as raw:
    root = Path(raw)
    gate = rc1.load_gate(ROOT)

    check_reserved_never_launched_closure(root, gate)
    check_reserved_closure_rejections(root, gate)

    for terminal_state, result, recovery in (
        ("failed", "failed", False),
        ("cancelled", "invalidated", True),
    ):
        task_id = (
            "cccc0267-0267-4267-8267-000000000021"
            if terminal_state == "failed"
            else "cccc0267-0267-4267-8267-000000000022"
        )
        vault, store, launch = build_negative_dispatch(
            root, task_id, terminal_state=terminal_state
        )
        state_path = vault / ".vault-meta/acceptance/rc1-streak-state.json"
        write_json(
            state_path,
            {
                "schema_version": 1,
                "expected_digest": DIGEST_A,
                "receipts": [],
                "reservation": {
                    "cell_id": "rc1-corridor-run-1",
                    "status": "launched",
                    "spec_sha256": "1" * 64,
                    "request_id": task_id,
                    "lifecycle_subject_sha256": DIGEST_A,
                    "launch": launch,
                },
            },
        )
        receipt = {
            **gate.receipt_template(
                "rc1-corridor-run-1", expected_digest=DIGEST_A
            ),
            "run_id": launch["harness"]["run_id"],
            "request_id": task_id,
            "owner_id": task_id,
            "store_id": f"{store.root.resolve()}#owners/{task_id}",
            "worktree_id": launch["worktree"],
            "provider_session_ids": [launch["harness"]["run_id"]],
            "result": result,
            "material_cycle": None,
            "resource_free": True,
            "coordinator_recovery": recovery,
        }
        if terminal_state == "failed":
            record = store.read(task_id, task_id)
            leaked = replace(
                record,
                resources=OwnedResources(surface_id="leaked-surface"),
                revision=record.revision + 1,
            )
            store.save(leaked, expected_revision=record.revision)
            unchanged = state_path.read_bytes()
            try:
                gate.record_receipt(
                    receipt,
                    expected_digest=DIGEST_A,
                    state_path=state_path,
                    evidence_root=vault,
                )
            except stab.StabilizationError:
                check(
                    "failed run with a durable resource leak stays reserved",
                    state_path.read_bytes() == unchanged,
                )
            else:
                raise AssertionError(
                    "failed run with a durable resource leak stays reserved"
                )
            cleaned = replace(
                leaked,
                resources=OwnedResources(),
                revision=leaked.revision + 1,
            )
            store.save(cleaned, expected_revision=leaked.revision)
        verdict = gate.record_receipt(
            receipt,
            expected_digest=DIGEST_A,
            state_path=state_path,
            evidence_root=vault,
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        check(
            f"{terminal_state} durable run closes and resets the reservation",
            verdict["streak"] == 0
            and state["receipts"] == []
            and state["reservation"] is None,
            state,
        )
        replacement_id = task_id[:-3] + "1" + task_id[-2:]

        def replacement_launcher(
            *_args: object, **_kwargs: object
        ) -> dict[str, object]:
            return {
                "schema_version": 1,
                "status": "launched",
                "request_id": replacement_id,
                "worktree": launch["worktree"],
                "harness": {
                    "owner_id": replacement_id,
                    "operation_id": replacement_id,
                    "lane_id": f"replacement-{terminal_state}",
                    "run_id": f"replacement-{terminal_state}",
                },
            }

        replacement = gate.reserve_and_launch(
            coordinator_authorized=True,
            expected_digest="b" * 64,
            state_path=state_path,
            launcher=replacement_launcher,
            spec_path=dispatch_spec(
                root / f"replacement-{terminal_state}.json", replacement_id
            ),
            evidence_root=vault,
        )
        check(
            f"{terminal_state} closure permits cell 1 on a changed digest",
            replacement["cell_id"] == "rc1-corridor-run-1"
            and replacement["request_id"] == replacement_id,
            replacement,
        )

    shared_vault = root / "shared-vault"
    state_path = shared_vault / ".vault-meta/acceptance/rc1-streak-state.json"
    verdicts: list[dict[str, object]] = []
    for sequence, task_id in enumerate(TASK_IDS, start=1):
        world = build_corridor_world(
            root,
            task_id,
            shared_vault=shared_vault,
            owner_id=task_id,
            executor_runtime="claude",
            executor_model="fable",
            review_runtime="claude",
            review_model="fable",
        )
        report = reserve_world(
            gate,
            world,
            state_path,
            dispatch_spec(root / f"dispatch-{sequence}.json", task_id),
        )
        terminal, rounds = complete_world(world)
        if sequence == 1:
            # A complete root whose final effect is neither the accepted reap
            # nor the expected cleanup effect stays rejected.
            shaped = replace(
                terminal,
                effect_id="start-provider",
                revision=terminal.revision + 1,
            )
            world.store.save(shaped, expected_revision=terminal.revision)
            foreign_receipt = receipt_for(
                world,
                dict(report["receipt_template"]),
                shaped,
                rounds,
            )
            unchanged = state_path.read_bytes()
            try:
                gate.record_receipt(
                    foreign_receipt,
                    expected_digest=DIGEST_A,
                    state_path=state_path,
                    evidence_root=shared_vault,
                )
            except stab.StabilizationError:
                check(
                    "an unexpected terminal effect identity stays rejected",
                    state_path.read_bytes() == unchanged,
                )
            else:
                raise AssertionError(
                    "an unexpected terminal effect identity stays rejected"
                )
            restored = replace(shaped, effect_id="request-exit", revision=shaped.revision + 1)
            world.store.save(restored, expected_revision=shaped.revision)
            terminal = world.record()
        verdicts.append(
            gate.record_receipt(
                receipt_for(
                    world,
                    dict(report["receipt_template"]),
                    terminal,
                    rounds,
                ),
                expected_digest=DIGEST_A,
                state_path=state_path,
                evidence_root=shared_vault,
            )
        )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    check(
        "three persisted real-store corridors complete the RC1 streak",
        [verdict["streak"] for verdict in verdicts] == [1, 2, 3]
        and verdicts[-1]["complete"] is True
        and verdicts[-1]["material_finding_cycle"] is True
        and len(state["receipts"]) == 3
        and state["reservation"] is None,
        verdicts,
    )

print("RC1 live streak gate tests passed")
