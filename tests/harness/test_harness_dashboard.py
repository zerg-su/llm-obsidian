#!/usr/bin/env python3
"""Read-only dashboard projection, English view, and CLI/cmux boundaries."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.cli import main as cli_main
from harness.contracts import OperationSpec, OwnedResources, RuntimeRoute
from harness.dashboard_projection import (
    ATTENTION,
    COORDINATOR,
    HEALTHY,
    MAX_ISSUES,
    WAITING,
    escalate,
    project,
)
from harness.dashboard_view import MAX_LINE, render
from harness.pipeline_builtins import compiled_builtin
from harness.status_segment import LiveInventory
from harness.store import OperationStore
from harness.supervisor import OperationSupervisor


OWNER = "dashboard-owner"
DISPATCH = "dashboard-dispatch"
SURFACE = "8C1A5B60-1111-4A00-9E00-0F0F0F0F0F0F"
FIX_STEPS = ("root-cause", "regression-test", "minimal-fix")
ROOT = "dashboard-root"
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


def _write_gate(store: OperationStore, status: str, *, subject: str) -> None:
    gate_root = store.root / "review-data" / OWNER / OWNER
    gate_root.mkdir(parents=True, exist_ok=True)
    (gate_root / "review-gate.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner_id": OWNER,
                "dispatch_operation_id": subject,
                "status": status,
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
    _receipt(runtime / "pipeline-fix" / "pass-0" / "reproduce" / "receipt.json")
    for pass_index in (0, 1):
        for step_id in FIX_STEPS:
            _receipt(
                runtime
                / "pipeline-fix"
                / f"pass-{pass_index}"
                / step_id
                / "receipt.json"
            )
    _receipt(runtime / "pipeline-verification" / "verify-child" / "receipt.json")
    _create(
        store,
        "dashboard-child",
        "verification",
        lane_id="lane-secondary",
        parent=DISPATCH,
    )
    _advance(store, "dashboard-child", "preflight", "starting", "running")
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
        and lanes["lane-secondary"].members == ("dashboard-child",)
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

    for index in range(MAX_ISSUES + 1):
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
        "recent issues stay bounded while the classification still escalates",
        len(projection.issues) == MAX_ISSUES
        and projection.truncated["issues"] > 0
        and projection.truncated["programs"] > 0
        and projection.classification == COORDINATOR
        and "+" in render(projection),
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

with tempfile.TemporaryDirectory(prefix="harness-dashboard-tree.") as raw:
    # The production shape: the dispatch operation id is the owner id, the
    # verification child carries an exact parent, and the review parent carries
    # only its owner. All three belong to one dispatch, not three programs.
    store_root = Path(raw) / "harness"
    store = OperationStore(store_root)
    compiled = compiled_builtin("engineering/change")
    _create(
        store,
        ROOT,
        "dispatch",
        lane_id="lane-root",
        contract_sha256=compiled.definition_sha256,
        owner=ROOT,
    )
    _advance(store, ROOT, "preflight", "starting", "running", owner=ROOT)
    OperationSupervisor(store, ROOT, ROOT).bind_resources(
        OwnedResources(surface_id=ROOT_SURFACE)
    )
    _create(
        store,
        VERIFY_CHILD,
        "pipeline-verify",
        lane_id="lane-verify",
        contract_sha256=compiled.definition_sha256,
        parent=ROOT,
        owner=ROOT,
    )
    _advance(
        store,
        VERIFY_CHILD,
        "preflight",
        "starting",
        "running",
        "verifying",
        owner=ROOT,
    )
    _create(
        store,
        REVIEW_PARENT,
        "simple-review-holistic",
        lane_id="lane-review",
        owner=ROOT,
        route=_reviewer_route(),
    )
    _advance(store, REVIEW_PARENT, "preflight", owner=ROOT)
    _create(
        store,
        REVIEW_ROUND,
        "review-round",
        lane_id="lane-review",
        parent=REVIEW_PARENT,
        owner=ROOT,
        route=_reviewer_route(),
    )
    _advance(store, REVIEW_ROUND, "preflight", owner=ROOT)

    projection = project(
        store_root,
        ROOT,
        inventory=LiveInventory({ROOT_SURFACE.casefold(): "workspace-9"}),
        surface_probe="observed",
    )
    check(
        "one dispatch renders as exactly one root program, not three",
        len(projection.programs) == 1
        and projection.programs[0].operation_id == ROOT,
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

with tempfile.TemporaryDirectory(prefix="harness-dashboard-closed.") as raw:
    # The harness closes a review by cancelling the parent and retaining the
    # completed round. That is the normal terminal shape, not an alarm.
    store_root = Path(raw) / "harness"
    store = OperationStore(store_root)
    compiled = compiled_builtin("engineering/change")
    _create(
        store,
        ROOT,
        "dispatch",
        lane_id="lane-root",
        contract_sha256=compiled.definition_sha256,
        owner=ROOT,
    )
    _advance(store, ROOT, "preflight", "starting", "running", "finalizing", owner=ROOT)
    OperationSupervisor(store, ROOT, ROOT).bind_resources(
        OwnedResources(surface_id=ROOT_SURFACE)
    )
    _create(
        store,
        VERIFY_CHILD,
        "pipeline-verify",
        lane_id="lane-verify",
        parent=ROOT,
        owner=ROOT,
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
        owner=ROOT,
    )
    _create(
        store,
        REVIEW_PARENT,
        "simple-review-holistic",
        lane_id="lane-review",
        owner=ROOT,
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
        owner=ROOT,
    )
    _create(
        store,
        REVIEW_ROUND,
        "review-round",
        lane_id="lane-review",
        parent=REVIEW_PARENT,
        owner=ROOT,
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
        owner=ROOT,
    )
    projection = project(
        store_root,
        ROOT,
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

with tempfile.TemporaryDirectory(prefix="harness-dashboard-unknown-route.") as raw:
    store_root = Path(raw) / "harness"
    store = OperationStore(store_root)
    compiled = compiled_builtin("engineering/change")
    _create(
        store,
        ROOT,
        "dispatch",
        lane_id="lane-root",
        contract_sha256=compiled.definition_sha256,
        owner=ROOT,
    )
    _advance(store, ROOT, "preflight", "starting", "running", owner=ROOT)
    projection = project(store_root, ROOT, inventory=LiveInventory({}))
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

print("harness dashboard tests passed")
