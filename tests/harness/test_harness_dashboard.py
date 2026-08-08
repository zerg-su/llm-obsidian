#!/usr/bin/env python3
"""Read-only dashboard projection, English view, and CLI/cmux boundaries."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.cli import main as cli_main
from harness.callbacks import CallbackBroker
from harness import dashboard_receipts
from harness.adapters.cmux import CmuxAdapter, Surface, SurfaceWorkspaceIndex
from harness.contracts import CallbackEnvelope, OperationSpec, OwnedResources, RuntimeRoute, VerificationEvidence, to_dict
from harness.dashboard_projection import (
    ATTENTION,
    COORDINATOR,
    HEALTHY,
    MAX_CHILDREN,
    MAX_ISSUES,
    MAX_LANES,
    MAX_PROGRAMS,
    UNKNOWN_ROUTE,
    WAITING,
    ChildView,
    DashboardProjection,
    IssueView,
    _bind_children,
    escalate,
    project,
)
from harness.dashboard_view import MAX_LINE, render
from harness.custom_pipelines import (
    CustomPipelinePolicy,
    ExplicitPipelineApproval,
    FrozenPipelineStore,
    compile_custom_spec,
    freeze_custom_pipeline,
    parse_pipeline_spec,
    render_custom_approval,
)
from harness.pipeline_builtins import builtin_registry, compiled_builtin
from harness.pipelines import (
    PipelineDefinition,
    PipelineStep,
    compile_pipeline,
)
from harness.status_segment import LiveInventory
from harness.store import OperationStore
from harness.supervisor import OperationSupervisor
from harness.verification_attempt import VerificationAttempt
from harness.workflows.engineering_fix import FixStepReceipt
from harness.workflows.engineering_fix_model import PHASE_SCHEMAS


OWNER = "dashboard-owner"
DISPATCH = "dashboard-dispatch"
SURFACE = "8C1A5B60-1111-4A00-9E00-0F0F0F0F0F0F"
FIX_STEPS = ("root-cause", "regression-test", "minimal-fix")
TREE_ROOT = "dashboard-root"
ROOT_SURFACE = "9D2B6C71-2222-4B11-8F11-1A1A1A1A1A1A"
VERIFY_CHILD = "dashboard-root-verify-0"
REVIEW_PARENT = "dashboard-root-review-holistic"
REVIEW_ROUND = "dashboard-root-review-round-0"


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"OK   {label}")


def _route_of(view: object) -> tuple[str, str, str, str]:
    route = view.route
    return (route.runtime, route.model, route.effort, route.preset)


def _route() -> RuntimeRoute:
    return RuntimeRoute("claude", "claude-opus-5", "medium", "executor", "a" * 64)


def _reviewer_route() -> RuntimeRoute:
    return RuntimeRoute(
        "claude", "claude-opus-5", "high", "reviewer-callback", "b" * 64
    )


def _create(
    store: OperationStore,
    operation_id: str,
    kind: str,
    *,
    lane_id: str,
    contract_sha256: str = "",
    parent: str = "",
    owner: str = OWNER,
    route: RuntimeRoute | None = None,
    verification_profile: str = "scoped",
) -> None:
    store.create(
        OperationSpec(
            operation_id,
            f"{operation_id}-key",
            kind,
            owner,
            route or _route(),
            "packets/task.json",
            verification_profile,
            contract_sha256=contract_sha256,
            parent_operation_id=parent,
        ),
        lane_id=lane_id,
        run_id=f"{operation_id}-run",
    )


def _advance(
    store: OperationStore,
    operation_id: str,
    *states: str,
    owner: str = OWNER,
) -> None:
    for state in states:
        store.transition(owner, operation_id, state)


def _receipt(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema_version":1}\n', encoding="utf-8")


def _fix_receipt(
    store: OperationStore,
    parent: str,
    runtime: Path,
    definition: str,
    step_id: str,
    iteration: int,
    *,
    accept: bool = True,
) -> None:
    operation_id = f"{parent}-{step_id}-{iteration}"
    lane_id = "lane-primary"
    run_id = f"fix-run-{step_id}-{iteration}"
    _create(
        store,
        operation_id,
        "pipeline-model-step",
        lane_id=lane_id,
        contract_sha256=definition,
        parent=parent,
    )
    _advance(store, operation_id, "preflight", "starting", "running", "awaiting-callback")
    input_schema, output_schema = PHASE_SCHEMAS[step_id]
    payload = {
        "schema_version": 1,
        "parent_operation_id": parent,
        "definition_sha256": definition,
        "step_id": step_id,
        "iteration": iteration,
        "input_schema": input_schema,
        "input_sha256": "1" * 64,
        "input_head_sha": "2" * 40,
        "prior_receipt_sha256": "" if iteration == 0 else "3" * 64,
        "verification_sha256": "" if iteration == 0 else "4" * 64,
        "output_schema": output_schema,
        "output_pointer": f"results/{step_id}-{iteration}.json",
        "output_sha256": "5" * 64,
        "head_sha": "2" * 40,
        "status": "complete",
    }
    payload_sha256 = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    callback_id = f"result-{payload_sha256[:24]}"
    receipt = FixStepReceipt(
        callback_id=callback_id,
        operation_id=operation_id,
        parent_operation_id=parent,
        lane_id=lane_id,
        run_id=f"{operation_id}-run",
        definition_sha256=definition,
        step_id=step_id,
        iteration=iteration,
        input_schema=input_schema,
        input_sha256=payload["input_sha256"],
        input_head_sha=payload["input_head_sha"],
        prior_receipt_sha256=payload["prior_receipt_sha256"],
        verification_sha256=payload["verification_sha256"],
        output_schema=output_schema,
        output_pointer=payload["output_pointer"],
        output_sha256=payload["output_sha256"],
        head_sha=payload["head_sha"],
        status=payload["status"],
    )
    path = runtime / "pipeline-fix" / f"pass-{iteration}" / step_id / "receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt.to_dict(), sort_keys=True) + "\n", encoding="utf-8")
    if accept:
        CallbackBroker(store, OWNER).accept(
            CallbackEnvelope(
                callback_id,
                operation_id,
                f"{operation_id}-run",
                "result",
                payload,
                payload_sha256,
            )
        )
        _advance(store, operation_id, "exiting", "complete")


def _verification_receipt(
    store: OperationStore,
    parent: str,
    runtime: Path,
    definition: str,
    *,
    status: str = "complete",
    input_char: str = "6",
    head_char: str = "8",
    attempt_index: int = 0,
) -> str:
    parent_record = store.read(OWNER, parent)
    input_sha = input_char * 64
    operation_id, lane_id, run_id, effect_id = dashboard_receipts.verification_identity(
        parent_record.spec, definition, input_sha, attempt_index
    )
    store.create(
        OperationSpec(
            operation_id,
            f"{operation_id}-key",
            "pipeline-verify",
            OWNER,
            _route(),
            "packets/task.json",
            "scoped",
            contract_sha256=definition,
            parent_operation_id=parent,
        ),
        lane_id=lane_id,
        run_id=run_id,
    )
    output = runtime / "verification-output.log"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"ok\n")
    head_sha = head_char * 40
    evidence = VerificationEvidence(
        "scoped", "7" * 64, head_sha, "scoped-1", ".",
        0 if status == "complete" else 1, "1", "2",
        "verification-output.log", hashlib.sha256(b"ok\n").hexdigest(), 3, 2,
    )
    attempt = VerificationAttempt(
        parent,
        "scoped",
        "7" * 64,
        head_sha,
        attempt_index,
    )
    payload = {
        "schema_version": 2,
        "parent_operation_id": parent,
        "definition_sha256": definition,
        "step_id": "verify",
        "profile": "scoped",
        "profile_sha256": "7" * 64,
        "head_sha": head_sha,
        "status": status,
        "operation_id": operation_id,
        "lane_id": lane_id,
        "run_id": run_id,
        "effect_id": effect_id,
        "input_sha256": input_sha,
        "evidence": [to_dict(evidence)],
        "verification_attempt": attempt.as_dict(),
        "verification_attempt_sha256": attempt.sha256,
    }
    path = runtime / "pipeline-verification" / operation_id / "receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return operation_id


def _write_gate(
    store: OperationStore,
    status: str,
    *,
    subject: str,
    active_review: str = "",
    head_sha: str = "8" * 40,
    owner: str = OWNER,
) -> None:
    gate_root = store.root / "review-data" / owner / owner
    gate_root.mkdir(parents=True, exist_ok=True)
    (gate_root / "review-gate.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner_id": owner,
                "dispatch_operation_id": subject,
                "status": status,
                "active_review_operation_id": active_review,
                "context": {"head_sha": head_sha},
                "lanes": [{"axis": "openai-holistic"}, {"axis": "claude-spec"}],
                "round_results": {},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _tree_bytes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


check(
    "classification escalation is ordered and fails closed on unknown values",
    escalate(HEALTHY, WAITING) == WAITING
    and escalate(ATTENTION, WAITING) == ATTENTION
    and escalate(ATTENTION, COORDINATOR) == COORDINATOR
    and escalate(HEALTHY, "invented") == COORDINATOR,
)

with tempfile.TemporaryDirectory(prefix="harness-dashboard.") as raw:
    store_root = Path(raw) / "harness"
    store = OperationStore(store_root)
    compiled = compiled_builtin("engineering/fix")
    _create(
        store,
        DISPATCH,
        "dispatch",
        lane_id="lane-primary",
        contract_sha256=compiled.definition_sha256,
    )
    _advance(store, DISPATCH, "preflight", "starting", "running", "awaiting-callback")
    OperationSupervisor(store, OWNER, DISPATCH).bind_resources(
        OwnedResources(surface_id=SURFACE)
    )
    runtime = store_root / "owners" / OWNER / "runtime" / DISPATCH
    _fix_receipt(store, DISPATCH, runtime, compiled.definition_sha256, "reproduce", 0)
    for pass_index in (0, 1):
        for step_id in FIX_STEPS:
            _fix_receipt(
                store,
                DISPATCH,
                runtime,
                compiled.definition_sha256,
                step_id,
                pass_index,
            )
    verification_child = _verification_receipt(
        store, DISPATCH, runtime, compiled.definition_sha256
    )
    _advance(store, verification_child, "preflight", "starting", "running")
    _write_gate(store, "reviewing", subject=DISPATCH)

    inventory = LiveInventory({SURFACE.casefold(): "workspace-1"})
    projection = project(
        store_root,
        OWNER,
        inventory=inventory,
        surface_probe="observed",
    )
    program = projection.programs[0]
    steps = {step.step_id: step for step in program.steps}
    check(
        "projection resolves the real compiled pipeline from the operation contract",
        len(projection.programs) == 1
        and program.pipeline == "engineering/fix@1.0.0"
        and program.definition_sha256 == compiled.definition_sha256
        and program.controls
        == ("bounded_loop@1.0.0", "human_gate@1.0.0")
        and tuple(steps) == tuple(
            step.step_id for step in compiled.definition.steps
        ),
    )
    check(
        "loop visits come from durable per-pass receipts",
        steps["reproduce"].visits == 1
        and all(steps[step_id].visits == 2 for step_id in FIX_STEPS)
        and program.loop_passes == 2
        and program.loop_limit == 3,
    )
    check(
        "compiled reconciliation drives step status and the next action",
        steps["minimal-fix"].status == "complete"
        and steps["verify"].status == "complete"
        and steps["review"].status == "running"
        and program.next_action == "wait"
        and program.classification == WAITING,
    )
    lanes = {lane.lane_id: lane for lane in program.lanes}
    check(
        "parallel operation lanes and review axes are both projected",
        lanes["lane-primary"].scope == "operation"
        and lanes["lane-primary"].status == "active"
        and any(verification_child in lane.members for lane in lanes.values())
        and lanes["openai-holistic"].scope == "review-axis"
        and lanes["claude-spec"].status == "active",
    )
    check(
        "a live recorded surface is proven against the bounded cmux probe",
        program.surface == "live"
        and projection.surface_probe == "observed"
        and projection.issues == (),
    )

    baseline = _tree_bytes(store_root)
    missing = project(store_root, OWNER, inventory=LiveInventory({}))
    check(
        "a surface missing from the cmux tree is attention, never silent cleanup",
        missing.programs[0].surface == "missing"
        and missing.classification == ATTENTION
        and [issue.code for issue in missing.issues] == ["surface-missing"],
    )
    unknown = project(store_root, OWNER, inventory=None)
    check(
        "an unreadable cmux tree requests coordinator classification",
        unknown.programs[0].surface == "unknown"
        and unknown.classification == COORDINATOR
        and unknown.surface_probe == "unavailable",
    )
    ambiguous = project(
        store_root,
        OWNER,
        inventory=LiveInventory(
            {SURFACE.casefold(): "workspace-1"},
            frozenset({SURFACE.casefold()}),
        ),
    )
    check(
        "an ambiguous surface requests coordinator classification",
        ambiguous.programs[0].surface == "ambiguous"
        and ambiguous.classification == COORDINATOR,
    )
    check(
        "projection never mutates durable state",
        _tree_bytes(store_root) == baseline,
    )

    text = render(projection)
    check(
        "the rendered dashboard is bounded English terminal text",
        all(len(line) <= MAX_LINE for line in text.splitlines())
        and "Harness dashboard" in text
        and "pipeline engineering/fix@1.0.0" in text
        and "parallel lanes: 4 (4 active)" in text
        and "wait for the running step" in text
        and "[x] minimal-fix" in text,
    )

    captured = io.StringIO()
    with redirect_stdout(captured):
        code = cli_main(
            [
                "--store",
                str(store_root),
                "--owner",
                OWNER,
                "dashboard",
            ],
            inventory_probe=lambda **_kwargs: inventory,
        )
    check(
        "the CLI renders the dashboard without touching lifecycle authority",
        code == 0
        and "Harness dashboard" in captured.getvalue()
        and _tree_bytes(store_root) == baseline,
    )

    captured = io.StringIO()
    with redirect_stdout(captured):
        code = cli_main(
            [
                "--store",
                str(store_root),
                "--owner",
                OWNER,
                "--json",
                "dashboard",
            ],
            inventory_probe=lambda **_kwargs: None,
        )
    payload = json.loads(captured.getvalue())
    check(
        "the CLI emits one typed JSON projection with an unavailable probe",
        code == 0
        and payload["schema_version"] == 1
        and payload["owner_id"] == OWNER
        and payload["surface_probe"] == "unavailable"
        and payload["programs"][0]["pipeline"] == "engineering/fix@1.0.0",
    )
    broken_fix = (
        runtime / "pipeline-fix" / "pass-1" / "minimal-fix" / "receipt.json"
    )
    broken_fix.write_text('{"schema_version":1}\n', encoding="utf-8")
    invalid_fix = project(store_root, OWNER, inventory=inventory)
    check(
        "malformed fix evidence is attention and never a completed visit",
        any(issue.code == "fix-receipt-invalid" for issue in invalid_fix.issues)
        and next(
            step
            for step in invalid_fix.programs[0].steps
            if step.step_id == "minimal-fix"
        ).status
        == "attention",
    )

with tempfile.TemporaryDirectory(prefix="harness-dashboard-unaccepted-fix.") as raw:
    store_root = Path(raw) / "harness"
    store = OperationStore(store_root)
    compiled = compiled_builtin("engineering/fix")
    _create(
        store,
        DISPATCH,
        "dispatch",
        lane_id="lane-primary",
        contract_sha256=compiled.definition_sha256,
    )
    _advance(store, DISPATCH, "preflight", "starting", "running")
    runtime = store_root / "owners" / OWNER / "runtime" / DISPATCH
    _fix_receipt(
        store,
        DISPATCH,
        runtime,
        compiled.definition_sha256,
        "reproduce",
        0,
        accept=False,
    )
    fabricated = project(store_root, OWNER, inventory=LiveInventory({}))
    reproduce = next(
        step for step in fabricated.programs[0].steps if step.step_id == "reproduce"
    )
    check(
        "a valid-looking receipt without durable callback acceptance is invalid",
        reproduce.visits == 0
        and reproduce.status == "attention"
        and any(issue.code == "fix-receipt-invalid" for issue in fabricated.issues),
    )
    _fix_receipt(
        store, DISPATCH, runtime, compiled.definition_sha256, "root-cause", 0
    )
    accepted_path = (
        runtime / "pipeline-fix" / "pass-0" / "root-cause" / "receipt.json"
    )
    changed = json.loads(accepted_path.read_text(encoding="utf-8"))
    changed["callback_id"] = "result-" + "0" * 24
    accepted_path.write_text(json.dumps(changed) + "\n", encoding="utf-8")
    drifted = project(store_root, OWNER, inventory=LiveInventory({}))
    check(
        "a receipt drifting from its accepted callback identity is invalid",
        any(issue.code == "fix-receipt-invalid" for issue in drifted.issues)
        and next(
            step
            for step in drifted.programs[0].steps
            if step.step_id == "root-cause"
        ).visits
        == 0,
    )

with tempfile.TemporaryDirectory(prefix="harness-dashboard-unknown.") as raw:
    store_root = Path(raw) / "harness"
    store = OperationStore(store_root)
    _create(
        store,
        DISPATCH,
        "dispatch",
        lane_id="lane-primary",
        contract_sha256="b" * 64,
    )
    _advance(store, DISPATCH, "preflight", "starting", "running")
    projection = project(store_root, OWNER, inventory=LiveInventory({}))
    check(
        "an unknown pipeline contract never invents progress",
        projection.programs[0].pipeline == "unresolved"
        and projection.programs[0].steps == ()
        and projection.classification == COORDINATOR
        and [issue.code for issue in projection.issues]
        == ["pipeline-contract-unresolved", "operation-resources-absent"],
    )

    for index in range(max(MAX_ISSUES, MAX_PROGRAMS) + 1):
        operation_id = f"dashboard-extra-{index}"
        _create(
            store,
            operation_id,
            "dispatch",
            lane_id=f"lane-{index}",
            contract_sha256="c" * 64,
        )
        _advance(store, operation_id, "preflight", "starting", "running")
        OperationSupervisor(store, OWNER, operation_id).bind_resources(
            OwnedResources(surface_id=f"dashboard-surface-{index}")
        )
    projection = project(store_root, OWNER, inventory=LiveInventory({}))
    check(
        "all active programs remain visible while issues stay bounded",
        len(projection.issues) == MAX_ISSUES == 5
        and projection.truncated["issues"] > 0
        and projection.truncated["programs"] == 0
        and len(projection.programs) == max(MAX_ISSUES, MAX_PROGRAMS) + 2
        and projection.classification == COORDINATOR
        and "+" in render(projection),
    )
    non_english = DashboardProjection(
        OWNER,
        ATTENTION,
        "unavailable",
        (),
        (
            IssueView(
                "typed-attention",
                DISPATCH,
                "необработанная причина",
                ATTENTION,
            ),
        ),
    )
    check(
        "a non-English persisted reason is omitted rather than translated",
        "typed-attention" in render(non_english)
        and "причина" not in render(non_english)
        and render(non_english).isascii(),
    )

    unreadable = store_root / "owners" / OWNER / "operations" / "broken.json"
    unreadable.write_text("{not json", encoding="utf-8")
    projection = project(store_root, OWNER, inventory=LiveInventory({}))
    check(
        "an unparseable durable record is reported, never skipped in silence",
        any(
            issue.code == "operation-record-invalid"
            for issue in projection.issues
        )
        and projection.classification == COORDINATOR,
    )

with tempfile.TemporaryDirectory(prefix="harness-dashboard-receipts.") as raw:
    store_root = Path(raw) / "harness"
    store = OperationStore(store_root)
    compiled = compiled_builtin("engineering/change")
    _create(
        store,
        DISPATCH,
        "dispatch",
        lane_id="lane-primary",
        contract_sha256=compiled.definition_sha256,
    )
    _advance(store, DISPATCH, "preflight", "starting", "running")
    runtime = store_root / "owners" / OWNER / "runtime" / DISPATCH
    first = _verification_receipt(
        store, DISPATCH, runtime, compiled.definition_sha256, input_char="6"
    )
    _verification_receipt(
        store, DISPATCH, runtime, compiled.definition_sha256, input_char="9"
    )
    projection = project(store_root, OWNER, inventory=LiveInventory({}))
    verify = next(step for step in projection.programs[0].steps if step.step_id == "verify")
    check(
        "accepted complete verification receipts count without inventing a loop",
        verify.status == "complete"
        and verify.visits == 2
        and projection.programs[0].loop_passes == 0
        and projection.programs[0].loop_limit == 0,
    )

    receipt_path = runtime / "pipeline-verification" / first / "receipt.json"
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["status"] = "failed"
    payload["evidence"][0]["exit_code"] = 7
    receipt_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    failed = project(store_root, OWNER, inventory=LiveInventory({}))
    check(
        "valid failed verification evidence becomes bounded attention",
        any(issue.code == "verification-receipt-failed" for issue in failed.issues)
        and next(step for step in failed.programs[0].steps if step.step_id == "verify").status
        == "attention",
    )

    payload["parent_operation_id"] = "wrong-parent"
    receipt_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    wrong = project(store_root, OWNER, inventory=LiveInventory({}))
    check(
        "wrong-identity verification evidence is invalid, never complete",
        any(issue.code == "verification-receipt-invalid" for issue in wrong.issues),
    )
    receipt_path.write_text("{partial", encoding="utf-8")
    malformed = project(store_root, OWNER, inventory=LiveInventory({}))
    check(
        "malformed verification evidence is contained as one bounded issue",
        sum(
            issue.code == "verification-receipt-invalid"
            for issue in malformed.issues
        )
        == 1,
    )
    receipt_path.unlink()
    check(
        "a receipt disappearing during a read is invalid rather than passed",
        dashboard_receipts.verification_receipt_status(
            store, store.read(OWNER, DISPATCH), runtime, receipt_path
        )
        == "invalid",
    )

with tempfile.TemporaryDirectory(prefix="harness-dashboard-stale-verify.") as raw:
    store_root = Path(raw) / "harness"
    store = OperationStore(store_root)
    compiled = compiled_builtin("engineering/change")
    _create(
        store,
        DISPATCH,
        "dispatch",
        lane_id="lane-primary",
        contract_sha256=compiled.definition_sha256,
    )
    _advance(store, DISPATCH, "preflight", "starting", "running")
    runtime = store_root / "owners" / OWNER / "runtime" / DISPATCH
    _verification_receipt(
        store,
        DISPATCH,
        runtime,
        compiled.definition_sha256,
        status="failed",
        input_char="6",
        head_char="8",
    )
    current_verification = _verification_receipt(
        store,
        DISPATCH,
        runtime,
        compiled.definition_sha256,
        input_char="9",
        head_char="a",
    )
    _write_gate(
        store,
        "reviewing",
        subject=DISPATCH,
        head_sha="a" * 40,
    )

    recovered = project(store_root, OWNER, inventory=LiveInventory({}))
    recovered_steps = {
        step.step_id: step for step in recovered.programs[0].steps
    }
    check(
        "an old failed HEAD cannot poison successful exact-HEAD verification",
        recovered_steps["verify"].status == "complete"
        and recovered_steps["review"].status == "running"
        and recovered.programs[0].next_action == "wait"
        and not any(
            issue.code.startswith("verification-receipt-")
            for issue in recovered.issues
        ),
    )
    current_path = (
        runtime
        / "pipeline-verification"
        / current_verification
        / "receipt.json"
    )
    current_payload = json.loads(current_path.read_text(encoding="utf-8"))
    current_payload["verification_attempt_sha256"] = "0" * 64
    current_path.write_text(json.dumps(current_payload) + "\n", encoding="utf-8")
    tampered_current = project(store_root, OWNER, inventory=LiveInventory({}))
    check(
        "tampered current-HEAD attempt identity remains visible attention",
        next(
            step
            for step in tampered_current.programs[0].steps
            if step.step_id == "verify"
        ).status
        == "attention"
        and any(
            issue.code == "verification-receipt-invalid"
            for issue in tampered_current.issues
        ),
    )

with tempfile.TemporaryDirectory(prefix="harness-dashboard-terminal-truth.") as raw:
    for terminal in ("failed", "cancelled"):
        store_root = Path(raw) / terminal / "harness"
        store = OperationStore(store_root)
        compiled = compiled_builtin("engineering/change")
        owner = f"terminal-{terminal}"
        _create(
            store,
            owner,
            "dispatch",
            lane_id="lane-primary",
            contract_sha256=compiled.definition_sha256,
            owner=owner,
        )
        states = (
            ("preflight", "starting", "running", "failed")
            if terminal == "failed"
            else (
                "preflight",
                "starting",
                "running",
                "cancelling",
                "exiting",
                "cancelled",
            )
        )
        _advance(store, owner, *states, owner=owner)
        projection = project(store_root, owner, inventory=LiveInventory({}))
        steps = projection.programs[0].steps
        check(
            f"a {terminal} root stops only its interrupted frontier",
            steps[0].status == "stopped"
            and all(step.status == "pending" for step in steps[1:])
            and projection.programs[0].classification == ATTENTION
            and any(
                issue.code == f"terminal-{terminal}"
                for issue in projection.issues
            ),
        )

with tempfile.TemporaryDirectory(prefix="harness-dashboard-tree.") as raw:
    # The production shape: the dispatch operation id is the owner id, the
    # verification child carries an exact parent, and the review parent carries
    # only its owner. All three belong to one dispatch, not three programs.
    store_root = Path(raw) / "harness"
    store = OperationStore(store_root)
    compiled = compiled_builtin("engineering/change")
    _create(
        store,
        TREE_ROOT,
        "dispatch",
        lane_id="lane-root",
        contract_sha256=compiled.definition_sha256,
        owner=TREE_ROOT,
    )
    _advance(store, TREE_ROOT, "preflight", "starting", "running", owner=TREE_ROOT)
    OperationSupervisor(store, TREE_ROOT, TREE_ROOT).bind_resources(
        OwnedResources(surface_id=ROOT_SURFACE)
    )
    _create(
        store,
        VERIFY_CHILD,
        "pipeline-verify",
        lane_id="lane-verify",
        contract_sha256=compiled.definition_sha256,
        parent=TREE_ROOT,
        owner=TREE_ROOT,
    )
    _advance(
        store,
        VERIFY_CHILD,
        "preflight",
        "starting",
        "running",
        "verifying",
        owner=TREE_ROOT,
    )
    _create(
        store,
        REVIEW_PARENT,
        "simple-review-holistic",
        lane_id="lane-review",
        owner=TREE_ROOT,
        route=_reviewer_route(),
    )
    _advance(store, REVIEW_PARENT, "preflight", owner=TREE_ROOT)
    _create(
        store,
        REVIEW_ROUND,
        "review-round",
        lane_id="lane-review",
        parent=REVIEW_PARENT,
        owner=TREE_ROOT,
        route=_reviewer_route(),
    )
    _advance(store, REVIEW_ROUND, "preflight", owner=TREE_ROOT)

    projection = project(
        store_root,
        TREE_ROOT,
        inventory=LiveInventory({ROOT_SURFACE.casefold(): "workspace-9"}),
        surface_probe="observed",
    )
    check(
        "one dispatch renders as exactly one root program, not three",
        len(projection.programs) == 1
        and projection.programs[0].operation_id == TREE_ROOT,
    )
    program = projection.programs[0]
    steps = {step.step_id: step for step in program.steps}
    check(
        "verification and review children nest under their owning pipeline step",
        tuple(child.operation_id for child in steps["verify"].children)
        == (VERIFY_CHILD,)
        and tuple(child.operation_id for child in steps["review"].children)
        == (REVIEW_PARENT,)
        and tuple(
            child.operation_id
            for child in steps["review"].children[0].children
        )
        == (REVIEW_ROUND,)
        and program.children == (),
    )
    check(
        "active verification never leaves the finished implementation highlighted",
        steps["tdd-slices"].status == "complete"
        and steps["verify"].status == "running"
        and steps["review"].status == "pending"
        and program.executor_status == "awaiting-transition",
    )
    check(
        "pipelines without bounded-loop control report no loop counter",
        program.loop_passes == 0 and program.loop_limit == 0,
    )
    check(
        "each step shows the frozen route of the record that executes it",
        _route_of(steps["tdd-slices"])
        == ("claude", "claude-opus-5", "medium", "executor")
        and _route_of(steps["verify"])
        == ("claude", "claude-opus-5", "medium", "scoped")
        and _route_of(steps["review"])
        == ("claude", "claude-opus-5", "high", "reviewer-callback"),
    )

    text = render(projection)
    check(
        "the rendered tree shows one program with nested route-annotated steps",
        all(len(line) <= MAX_LINE for line in text.splitlines())
        and "Programs: 1" in text
        and "route claude/claude-opus-5/high  preset reviewer-callback" in text
        and "review-round" in text
        and "executor claude/claude-opus-5/medium" in text
        and "awaiting-transition" in text,
    )

with tempfile.TemporaryDirectory(prefix="harness-dashboard-current-tree.") as raw:
    store_root = Path(raw) / "harness"
    store = OperationStore(store_root)
    compiled = compiled_builtin("engineering/change")
    _create(
        store,
        TREE_ROOT,
        "dispatch",
        lane_id="lane-root",
        contract_sha256=compiled.definition_sha256,
        owner=TREE_ROOT,
    )
    _advance(store, TREE_ROOT, "preflight", "starting", "running", owner=TREE_ROOT)
    OperationSupervisor(store, TREE_ROOT, TREE_ROOT).bind_resources(
        OwnedResources(surface_id=ROOT_SURFACE)
    )
    for index in range(MAX_CHILDREN + 1):
        child = f"history-verify-{index:02d}"
        _create(
            store,
            child,
            "pipeline-verify",
            lane_id=f"history-lane-{index:02d}",
            contract_sha256=compiled.definition_sha256,
            parent=TREE_ROOT,
            owner=TREE_ROOT,
        )
        _advance(
            store,
            child,
            "preflight",
            "starting",
            "running",
            "verifying",
            "finalizing",
            "exiting",
            "complete",
            owner=TREE_ROOT,
        )
    active_parent = "zz-current-review-parent"
    active_round = f"{active_parent}-round"
    _create(
        store,
        active_parent,
        "simple-review-holistic",
        lane_id="zz-current-review-lane",
        owner=TREE_ROOT,
        route=_reviewer_route(),
    )
    _advance(
        store,
        active_parent,
        "preflight",
        "starting",
        "running",
        "awaiting-callback",
        owner=TREE_ROOT,
    )
    OperationSupervisor(store, TREE_ROOT, active_parent).bind_resources(
        OwnedResources(surface_id="current-review-surface")
    )
    _create(
        store,
        active_round,
        "review-round",
        lane_id="zz-current-review-lane",
        parent=active_parent,
        owner=TREE_ROOT,
        route=_reviewer_route(),
    )
    _advance(
        store,
        active_round,
        "preflight",
        "starting",
        "running",
        "awaiting-callback",
        owner=TREE_ROOT,
    )
    OperationSupervisor(store, TREE_ROOT, active_round).bind_resources(
        OwnedResources(surface_id="current-round-surface")
    )
    _write_gate(
        store,
        "reviewing",
        subject=TREE_ROOT,
        active_review=active_parent,
        owner=TREE_ROOT,
    )

    current = project(
        store_root,
        TREE_ROOT,
        inventory=LiveInventory({ROOT_SURFACE.casefold(): "workspace-9"}),
    )
    current_program = current.programs[0]
    current_steps = {step.step_id: step for step in current_program.steps}
    visible_parent = next(
        child
        for child in current_steps["review"].children
        if child.operation_id == active_parent
    )
    visible_lanes = {lane.lane_id: lane for lane in current_program.lanes}
    check(
        "history caps retain the current review lineage axes and drop counts",
        visible_parent.children[0].operation_id == active_round
        and current_steps["review"].status == "running"
        and current_program.next_action == "wait"
        and _route_of(current_steps["review"])
        == ("claude", "claude-opus-5", "high", "reviewer-callback")
        and visible_lanes["openai-holistic"].scope == "review-axis"
        and visible_lanes["claude-spec"].scope == "review-axis"
        and len(current_program.lanes) == MAX_LANES
        and current.truncated["children"] > 0
        and current.truncated["lanes"] > 0,
    )

with tempfile.TemporaryDirectory(prefix="harness-dashboard-closed.") as raw:
    # The harness closes a review by cancelling the parent and retaining the
    # completed round. That is the normal terminal shape, not an alarm.
    store_root = Path(raw) / "harness"
    store = OperationStore(store_root)
    compiled = compiled_builtin("engineering/change")
    _create(
        store,
        TREE_ROOT,
        "dispatch",
        lane_id="lane-root",
        contract_sha256=compiled.definition_sha256,
        owner=TREE_ROOT,
    )
    _advance(store, TREE_ROOT, "preflight", "starting", "running", "finalizing", owner=TREE_ROOT)
    OperationSupervisor(store, TREE_ROOT, TREE_ROOT).bind_resources(
        OwnedResources(surface_id=ROOT_SURFACE)
    )
    _create(
        store,
        VERIFY_CHILD,
        "pipeline-verify",
        lane_id="lane-verify",
        parent=TREE_ROOT,
        owner=TREE_ROOT,
    )
    _advance(
        store,
        VERIFY_CHILD,
        "preflight",
        "starting",
        "running",
        "verifying",
        "finalizing",
        "exiting",
        "complete",
        owner=TREE_ROOT,
    )
    _create(
        store,
        REVIEW_PARENT,
        "simple-review-holistic",
        lane_id="lane-review",
        owner=TREE_ROOT,
        route=_reviewer_route(),
    )
    _advance(
        store,
        REVIEW_PARENT,
        "preflight",
        "starting",
        "cancelling",
        "exiting",
        "cancelled",
        owner=TREE_ROOT,
    )
    _create(
        store,
        REVIEW_ROUND,
        "review-round",
        lane_id="lane-review",
        parent=REVIEW_PARENT,
        owner=TREE_ROOT,
        route=_reviewer_route(),
    )
    _advance(
        store,
        REVIEW_ROUND,
        "preflight",
        "starting",
        "running",
        "verifying",
        "finalizing",
        "exiting",
        "complete",
        owner=TREE_ROOT,
    )
    projection = project(
        store_root,
        TREE_ROOT,
        inventory=LiveInventory({ROOT_SURFACE.casefold(): "workspace-9"}),
    )
    program = projection.programs[0]
    steps = {step.step_id: step for step in program.steps}
    check(
        "a cancelled review parent with a complete round is finished, not attention",
        steps["verify"].status == "complete"
        and steps["review"].status == "complete"
        and program.next_action == "reap-ready"
        and program.classification != ATTENTION
        and projection.issues == (),
    )
    check(
        "cancelled nested operations render with the explicit stopped marker",
        "[-]" in render(projection) and "cancelled" in render(projection),
    )

with tempfile.TemporaryDirectory(prefix="harness-dashboard-unknown-route.") as raw:
    store_root = Path(raw) / "harness"
    store = OperationStore(store_root)
    compiled = compiled_builtin("engineering/change")
    _create(
        store,
        TREE_ROOT,
        "dispatch",
        lane_id="lane-root",
        contract_sha256=compiled.definition_sha256,
        owner=TREE_ROOT,
    )
    _advance(store, TREE_ROOT, "preflight", "starting", "running", owner=TREE_ROOT)
    projection = project(store_root, TREE_ROOT, inventory=LiveInventory({}))
    program = projection.programs[0]
    steps = {step.step_id: step for step in program.steps}
    check(
        "absent step metadata is labeled unknown rather than inferred",
        _route_of(steps["review"])
        == ("unknown", "unknown", "unknown", "unknown")
        and _route_of(steps["verify"])
        == ("unknown", "unknown", "unknown", "scoped"),
    )
    check(
        "a running record owning no runtime resource is unresolved, not live",
        program.surface == "unbound"
        and program.classification == ATTENTION
        and any(
            issue.code == "operation-resources-absent"
            for issue in projection.issues
        )
        and projection.programs[0].lanes[0].status == "attention",
    )

with tempfile.TemporaryDirectory(prefix="harness-dashboard-identity.") as raw:
    # Production ids share a long UUID prefix and differ only in a derived
    # suffix, so a leading slice would render parent and round identically.
    store_root = Path(raw) / "harness"
    store = OperationStore(store_root)
    compiled = compiled_builtin("engineering/change")
    parent_id = "16d0cef4-59f0-5625-98c9-1ed8934c0d2c-holistic-99908aeb"
    round_id = f"{parent_id}-round-799f0261"
    _create(
        store,
        TREE_ROOT,
        "dispatch",
        lane_id="lane-root",
        contract_sha256=compiled.definition_sha256,
        owner=TREE_ROOT,
    )
    _advance(store, TREE_ROOT, "preflight", "starting", "running", owner=TREE_ROOT)
    OperationSupervisor(store, TREE_ROOT, TREE_ROOT).bind_resources(
        OwnedResources(surface_id=ROOT_SURFACE)
    )
    _create(
        store,
        parent_id,
        "simple-review-holistic",
        lane_id="lane-review",
        owner=TREE_ROOT,
        route=_reviewer_route(),
    )
    _advance(store, parent_id, "preflight", "starting", "running", owner=TREE_ROOT)
    _create(
        store,
        round_id,
        "review-round",
        lane_id="lane-review",
        parent=parent_id,
        owner=TREE_ROOT,
        route=_reviewer_route(),
    )
    _advance(store, round_id, "preflight", "starting", "running", owner=TREE_ROOT)
    text = render(
        project(
            store_root,
            TREE_ROOT,
            inventory=LiveInventory({ROOT_SURFACE.casefold(): "workspace-9"}),
        )
    )
    rendered = [line.strip() for line in text.splitlines() if "..." in line]
    check(
        "nested operations stay distinguishable on production-shaped ids",
        len(rendered) == 2
        and rendered[0].split()[1] != rendered[1].split()[1]
        and all(len(line) <= MAX_LINE for line in text.splitlines()),
    )

# A durable child records the pipeline it belongs to, not which step of that
# pipeline ran it. A custom definition may declare two steps of one primitive,
# which makes that binding ambiguous — and an ambiguous binding is not a guess.
# Only built-in contracts resolve through compiled_executable_for_contract, so
# the guard is pinned directly on the pure binder rather than through a store.
_ambiguous = PipelineDefinition(
    pipeline_id="dashboard",
    version="1.0.0",
    profile="twoverify",
    input_schema="approved-plan/v1",
    output_schema="reap-ready/v1",
    steps=(
        PipelineStep(
            "build", "model_step", "1.0.0",
            "approved-plan/v1", "change/v1", "worktree",
        ),
        PipelineStep(
            "verify-early", "verify", "1.0.0",
            "change/v1", "change/v1", "verification",
        ),
        PipelineStep(
            "verify-late", "verify", "1.0.0",
            "change/v1", "reap-ready/v1", "verification",
        ),
    ),
)
_compiled_ambiguous = compile_pipeline(
    _ambiguous, builtin_registry(), capabilities=("route:resolved",)
)
_verify_child = ChildView(
    VERIFY_CHILD, "pipeline-verify", "running", "running", UNKNOWN_ROUTE
)
_by_step, _loose = _bind_children((_verify_child,), _compiled_ambiguous)
_single_step, _single_loose = _bind_children(
    (_verify_child,), compiled_builtin("engineering/change")
)
check(
    "an ambiguous step binding stays at the program level, never guessed",
    _by_step == {}
    and _loose == (_verify_child,)
    and _single_step == {"verify": [_verify_child]}
    and _single_loose == (),
)

with tempfile.TemporaryDirectory(prefix="harness-dashboard-custom.") as raw:
    store_root = Path(raw) / "harness"
    store = OperationStore(store_root)
    spec = parse_pipeline_spec(
        (ROOT / "examples" / "pipelines" / "document-project-v1.json").read_text(
            encoding="utf-8"
        )
    )
    policy = CustomPipelinePolicy.default()
    compiled = compile_custom_spec(
        spec,
        builtin_registry(),
        policy=policy,
        capabilities=("route:resolved",),
    )
    card = render_custom_approval(spec, compiled, policy=policy)
    approval = ExplicitPipelineApproval.for_card(
        definition_sha256=compiled.definition_sha256,
        approval_card=card,
        actor="user",
        decision="approve",
    )
    frozen = freeze_custom_pipeline(spec, compiled, approval, card)
    owner = "dashboard-custom-root"
    _create(
        store,
        owner,
        "dispatch",
        lane_id="lane-custom",
        contract_sha256=compiled.definition_sha256,
        owner=owner,
    )
    _advance(store, owner, "preflight", "starting", "running", owner=owner)
    FrozenPipelineStore(
        store_root / "owners" / owner / "runtime"
    ).save(operation_id=owner, spec=spec, frozen=frozen, approval=approval)
    custom = project(store_root, owner, inventory=LiveInventory({}))
    program = custom.programs[0]
    check(
        "a frozen custom dispatch projects its exact compiled graph",
        program.pipeline == f"custom/{spec.spec_id}@{compiled.definition.version}"
        and tuple(step.step_id for step in program.steps)
        == tuple(step.step_id for step in compiled.definition.steps)
        and program.controls == compiled.resolved_control_primitives
        and program.steps[0].status == "running"
        and all(step.status == "pending" for step in program.steps[1:])
        and _route_of(program.steps[0])
        == ("claude", "claude-opus-5", "medium", "executor")
        and program.pipeline != "unresolved",
    )


def _load_dashboard_script() -> object:
    path = ROOT / "scripts" / "harness-dashboard.py"
    spec = importlib.util.spec_from_file_location("harness_dashboard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dashboard_script = _load_dashboard_script()

with tempfile.TemporaryDirectory(prefix="harness-dashboard-cli.") as raw:
    store_root = Path(raw) / "harness"
    compiled = compiled_builtin("engineering/change")
    active_owner = "dashboard-active"
    active_store = OperationStore(store_root)
    _create(
        active_store,
        active_owner,
        "dispatch",
        lane_id="lane-active",
        contract_sha256=compiled.definition_sha256,
        owner=active_owner,
    )
    _advance(
        active_store,
        active_owner,
        "preflight",
        "starting",
        owner=active_owner,
    )
    for index in range(4):
        owner = f"dashboard-terminal-{index}"
        _create(
            active_store,
            owner,
            "dispatch",
            lane_id=f"lane-terminal-{index}",
            contract_sha256=compiled.definition_sha256,
            owner=owner,
        )
        _advance(
            active_store,
            owner,
            "preflight",
            "starting",
            "running",
            "finalizing",
            "exiting",
            "complete",
            owner=owner,
        )
        record_path = store_root / "owners" / owner / "operations" / f"{owner}.json"
        os.utime(record_path, (100 + index, 100 + index))

    baseline = _tree_bytes(store_root)
    command = [
        sys.executable,
        str(ROOT / "scripts" / "harness-dashboard.py"),
        "--store",
        str(store_root),
        "--once",
        "--recent",
        "2",
        "--no-color",
    ]
    cli_environment = {
        **os.environ,
        "CMUX_BUNDLED_CLI_PATH": str(Path(raw) / "missing-cmux"),
    }
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        env=cli_environment,
    )
    check(
        "the standalone CLI shows every active pipeline and bounded terminal history",
        result.returncode == 0
        and "Active pipelines: 1" in result.stdout
        and "Terminal history: 2" in result.stdout
        and "dashboard-terminal-3" in result.stdout
        and "dashboard-terminal-2" in result.stdout
        and "dashboard-terminal-1" not in result.stdout
        and "\x1b[" not in result.stdout
        and result.stdout.isascii()
        and _tree_bytes(store_root) == baseline,
    )
    for option, value in (("--interval", "0"), ("--recent", "4")):
        rejected = subprocess.run(
            [*command[:4], option, value, "--once"],
            text=True,
            capture_output=True,
            check=False,
            env=cli_environment,
        )
        check(
            f"the standalone CLI bounds {option}",
            rejected.returncode == 2,
        )

    rendered: list[str] = []

    def interrupt(_interval: float) -> None:
        raise KeyboardInterrupt

    code = dashboard_script.main(
        ["--store", str(store_root), "--interval", "0.1", "--no-color"],
        inventory_probe=lambda **_kwargs: None,
        sleeper=interrupt,
        output=rendered.append,
    )
    check(
        "live mode redraws locally and exits cleanly on Ctrl-C",
        code == 0
        and len(rendered) == 1
        and rendered[0].startswith("\x1b[2J\x1b[H")
        and _tree_bytes(store_root) == baseline,
    )


class FakeCmuxRunner:
    def __init__(self, caller: str, dashboard: str, workspace: str) -> None:
        self.caller = caller
        self.dashboard = dashboard
        self.workspace = workspace
        self.created = False
        self.fail_once = ""
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        args = argv[1:]
        if args[-3:] == ["tree", "--all", "--json"]:
            surfaces = [{"id": self.caller}]
            if self.created:
                surfaces.append({"id": self.dashboard})
            payload = {
                "windows": [
                    {
                        "id": "6D3C2B1A-3333-4C22-9D22-2B2B2B2B2B2B",
                        "workspaces": [
                            {
                                "id": self.workspace,
                                "panes": [{"surfaces": surfaces}],
                            }
                        ],
                    }
                ]
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        if "new-split" in args:
            if self.fail_once == "open":
                self.fail_once = ""
                return subprocess.CompletedProcess(argv, 7, "", "open failed")
            self.created = True
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "surface_id": self.dashboard,
                        "workspace_id": self.workspace,
                    }
                ),
                "",
            )
        if args[:1] == ["send"] and self.fail_once == "send":
            self.fail_once = ""
            return subprocess.CompletedProcess(argv, 7, "", "send failed")
        if args[:1] == ["send-key"] and self.fail_once == "enter":
            self.fail_once = ""
            return subprocess.CompletedProcess(argv, 7, "", "enter failed")
        if args[:1] == ["close-surface"]:
            self.created = False
        return subprocess.CompletedProcess(argv, 0, "", "")


class AmbiguousCmux:
    def __init__(self, caller: str) -> None:
        from harness.adapters.cmux import SurfaceWorkspaceIndex

        self.inventory = SurfaceWorkspaceIndex(
            {}, frozenset({caller.casefold()}), frozenset({caller.casefold()})
        )
        self.created = False

    def surface_workspaces(self) -> object:
        return self.inventory

    def open_split(self, _caller: str) -> object:
        self.created = True
        raise AssertionError("ambiguous identity reached a cmux effect")


class MovingCmux:
    def __init__(self, caller: str, dashboard: str, workspaces: tuple[str, str]) -> None:
        self.caller = caller
        self.dashboard = dashboard
        self.workspaces = workspaces
        self.probes = 0
        self.created = 0

    def surface_workspaces(self) -> SurfaceWorkspaceIndex:
        workspace = self.workspaces[min(self.probes, 1)]
        self.probes += 1
        return SurfaceWorkspaceIndex({self.caller.casefold(): workspace})

    def open_split(self, _caller: str) -> Surface:
        self.created += 1
        workspace = self.workspaces[min(self.probes - 1, 1)]
        return Surface(self.dashboard, workspace_id=workspace)

    def send(self, _surface: str, _text: str) -> None:
        pass

    def send_key(self, _surface: str, _key: str) -> None:
        pass

    def close_exact(self, _surface: str) -> None:
        pass


with tempfile.TemporaryDirectory(prefix="harness-dashboard-open.") as raw:
    root = Path(raw)
    vault = root / "vault"
    store_root = vault / ".vault-meta" / "harness"
    store_root.mkdir(parents=True)
    caller = "1A2B3C4D-4444-4D33-8E33-3C3C3C3C3C3C"
    dashboard = "2B3C4D5E-5555-4E44-9F44-4D4D4D4D4D4D"
    workspace = "3C4D5E6F-6666-4F55-8A55-5E5E5E5E5E5E"
    fake = FakeCmuxRunner(caller, dashboard, workspace)
    adapter = CmuxAdapter(runner=fake, binary="cmux")
    marker_root = root / "markers"
    baseline = _tree_bytes(store_root)
    first = dashboard_script.open_dashboard(
        vault=vault,
        store=store_root,
        caller_surface=caller,
        adapter=adapter,
        marker_root=marker_root,
    )
    second = dashboard_script.open_dashboard(
        vault=vault,
        store=store_root,
        caller_surface=caller,
        adapter=adapter,
        marker_root=marker_root,
    )
    fake.created = False
    replacement = dashboard_script.open_dashboard(
        vault=vault,
        store=store_root,
        caller_surface=caller,
        adapter=adapter,
        marker_root=marker_root,
    )
    create_calls = [call for call in fake.calls if "new-split" in call]
    send_calls = [call for call in fake.calls if "send" in call]
    enter_calls = [call for call in fake.calls if "send-key" in call]
    check(
        "the external launcher creates once, reuses exactly, and never owns Harness state",
        first.surface_id == dashboard
        and not first.reused
        and second.surface_id == dashboard
        and second.reused
        and replacement.surface_id == dashboard
        and not replacement.reused
        and create_calls
        == [[
            "cmux",
            "--id-format",
            "both",
            "new-split",
            "right",
            "--surface",
            caller,
            "--focus",
            "false",
            "--json",
        ]] * 2
        and len(send_calls) == 2
        and send_calls[0][2:4] == ["--surface", dashboard]
        and "harness-dashboard.py" in send_calls[0][-1]
        and enter_calls
        == [["cmux", "send-key", "--surface", dashboard, "Enter"]] * 2
        and not any("focus" in call and "new-split" not in call for call in fake.calls)
        and _tree_bytes(store_root) == baseline,
    )
    race_fake = FakeCmuxRunner(caller, dashboard, workspace)
    race_adapter = CmuxAdapter(runner=race_fake, binary="cmux")
    race_markers = root / "race-markers"
    dashboard_script.open_dashboard(
        vault=vault,
        store=store_root,
        caller_surface=caller,
        adapter=race_adapter,
        marker_root=race_markers,
    )
    race_fake.created = False
    start = threading.Barrier(2)

    def recover_dead_ready() -> object:
        start.wait()
        return dashboard_script.open_dashboard(
            vault=vault,
            store=store_root,
            caller_surface=caller,
            adapter=race_adapter,
            marker_root=race_markers,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        recoveries = tuple(pool.map(lambda _index: recover_dead_ready(), range(2)))
    race_creates = [call for call in race_fake.calls if "new-split" in call]
    check(
        "simultaneous dead-ready recovery owns exactly one replacement split",
        len(race_creates) == 2
        and sum(not result.reused for result in recoveries) == 1
        and sum(result.reused for result in recoveries) == 1,
    )
    race_fake.created = False
    race_marker = next(race_markers.glob("*.json"))
    retryable = json.loads(race_marker.read_text(encoding="ascii"))
    retryable.update(state="retryable", surface_id="", reserved_at=0)
    race_marker.write_text(json.dumps(retryable), encoding="ascii")
    start = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        retries = tuple(pool.map(lambda _index: recover_dead_ready(), range(2)))
    race_creates = [call for call in race_fake.calls if "new-split" in call]
    check(
        "simultaneous retryable recovery owns exactly one replacement split",
        len(race_creates) == 3
        and sum(not result.reused for result in retries) == 1
        and sum(result.reused for result in retries) == 1,
    )
    for failure in ("open", "send", "enter"):
        failed_fake = FakeCmuxRunner(caller, dashboard, workspace)
        failed_fake.fail_once = failure
        failed_adapter = CmuxAdapter(runner=failed_fake, binary="cmux")
        failed_markers = root / f"{failure}-markers"
        try:
            dashboard_script.open_dashboard(
                vault=vault,
                store=store_root,
                caller_surface=caller,
                adapter=failed_adapter,
                marker_root=failed_markers,
            )
        except Exception:
            pass
        else:
            raise AssertionError(f"{failure} failure was accepted")
        recovered = dashboard_script.open_dashboard(
            vault=vault,
            store=store_root,
            caller_surface=caller,
            adapter=failed_adapter,
            marker_root=failed_markers,
        )
        check(
            f"the external launcher recovers after {failure} failure",
            recovered.surface_id == dashboard
            and not recovered.reused
            and failed_fake.created,
        )

    reserved_fake = FakeCmuxRunner(caller, dashboard, workspace)
    reserved_fake.fail_once = "open"
    reserved_adapter = CmuxAdapter(runner=reserved_fake, binary="cmux")
    reserved_markers = root / "reserved-markers"
    try:
        dashboard_script.open_dashboard(
            vault=vault,
            store=store_root,
            caller_surface=caller,
            adapter=reserved_adapter,
            marker_root=reserved_markers,
            clock=lambda: 100.0,
        )
    except Exception:
        pass
    marker_path = next(reserved_markers.glob("*.json"))
    reservation = json.loads(marker_path.read_text(encoding="ascii"))
    reservation.update(state="reserved", reserved_at=100.0, surface_id="")
    marker_path.write_text(json.dumps(reservation), encoding="ascii")
    try:
        dashboard_script.open_dashboard(
            vault=vault,
            store=store_root,
            caller_surface=caller,
            adapter=reserved_adapter,
            marker_root=reserved_markers,
            clock=lambda: 100.0,
        )
    except Exception as exc:
        concurrent_rejected = "concurrently" in str(exc)
    else:
        concurrent_rejected = False
    stale = dashboard_script.open_dashboard(
        vault=vault,
        store=store_root,
        caller_surface=caller,
        adapter=reserved_adapter,
        marker_root=reserved_markers,
        clock=lambda: 131.0,
    )
    check(
        "fresh reservations reject concurrency while stale reservations recover once",
        concurrent_rejected and stale.surface_id == dashboard and reserved_fake.created,
    )
    moved_workspace = "4D5E6F70-7777-4066-8B66-6F6F6F6F6F6F"
    moving = MovingCmux(caller, dashboard, (workspace, moved_workspace))
    moved = dashboard_script.open_dashboard(
        vault=vault,
        store=store_root,
        caller_surface=caller,
        adapter=moving,
        marker_root=root / "moving-markers",
    )
    check(
        "one critical section binds one immutable caller placement probe",
        moving.probes == 1
        and moving.created == 1
        and moved.workspace_id == workspace,
    )

    for crash_stage in ("starting-published", "startup-delivered"):
        crash_fake = FakeCmuxRunner(caller, dashboard, workspace)
        crash_adapter = CmuxAdapter(runner=crash_fake, binary="cmux")
        crash_markers = root / f"crash-{crash_stage}"

        def stop_after(stage: str, expected: str = crash_stage) -> None:
            if stage == expected:
                raise SystemExit(91)

        try:
            dashboard_script.open_dashboard(
                vault=vault,
                store=store_root,
                caller_surface=caller,
                adapter=crash_adapter,
                marker_root=crash_markers,
                clock=lambda: 100.0,
                crash_hook=stop_after,
            )
        except SystemExit as exc:
            crashed = exc.code == 91
        else:
            crashed = False
        recovered = dashboard_script.open_dashboard(
            vault=vault,
            store=store_root,
            caller_surface=caller,
            adapter=crash_adapter,
            marker_root=crash_markers,
            clock=lambda: 131.0,
        )
        check(
            f"stale {crash_stage} state closes and replaces its exact split",
            crashed
            and recovered.surface_id == dashboard
            and not recovered.reused
            and len([call for call in crash_fake.calls if "new-split" in call])
            == 2
            and len([call for call in crash_fake.calls if "close-surface" in call])
            == 1,
        )
    ambiguous = AmbiguousCmux(caller)
    try:
        dashboard_script.open_dashboard(
            vault=vault,
            store=store_root,
            caller_surface=caller,
            adapter=ambiguous,
            marker_root=root / "ambiguous-markers",
        )
    except Exception as exc:
        rejected = "ambiguous" in str(exc)
    else:
        rejected = False
    check(
        "ambiguous caller placement is rejected before any cmux effect",
        rejected and not ambiguous.created,
    )

print("harness dashboard tests passed")
