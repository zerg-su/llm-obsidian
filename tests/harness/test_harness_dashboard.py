#!/usr/bin/env python3
"""Read-only dashboard projection, English view, and CLI/cmux boundaries."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
# macOS exposes the default temporary root through /var -> /private/var. Use
# its canonical spelling so ordinary fixtures exercise the non-symlink path.
tempfile.tempdir = str(Path(tempfile.gettempdir()).resolve())

from harness.cli import main as cli_main
from harness.callbacks import CallbackBroker
from harness import dashboard_receipts, verification as harness_verification
from harness.adapters.cmux import CmuxAdapter, Surface, SurfaceWorkspaceIndex
from harness.contracts import AttentionReason, CallbackEnvelope, OperationSpec, OwnedResources, RuntimeRoute, VerificationEvidence, to_dict
from harness.dashboard_projection import (
    ACTIVE,
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
    project_root,
)
from harness.dashboard_view import MAX_LINE, _colorize, render
from harness.dashboard_policy import (
    ProgramView,
    ReviewSummaryView,
    RouteView,
    StepView,
    TimingView,
)
from harness.cli_readonly import dashboard as readonly_dashboard
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
from harness.runtime_worker import _pipeline_verify_identity
from harness.verification_attempt import pipeline_verify_effect_id
from harness.review_attempt import (
    ReviewAttempt,
    ReviewAttemptIdentity,
    ReviewAttemptLaneIdentity,
    ReviewAttemptLaneResult,
    ReviewAttemptPolicy,
    ReviewAttemptTerminal,
    ReviewAttemptTerminalResult,
)
from harness.verification_attempt import (
    VerificationAttempt,
    verification_input_sha256,
)
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


REGRESSION_FAILURES: list[str] = []


def regression_check(label: str, condition: bool) -> None:
    """Run the whole new regression matrix before reporting its red members."""

    if condition:
        print(f"OK   {label}")
        return
    REGRESSION_FAILURES.append(label)
    print(f"RED  {label}")


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
    head_char: str = "8",
    attempt_index: int = 0,
    owner: str = OWNER,
    started_at: str = "2026-08-11T00:01:00Z",
    finished_at: str = "2026-08-11T00:03:00Z",
) -> str:
    parent_record = store.read(owner, parent)
    head_sha = head_char * 40
    input_sha = verification_input_sha256(
        definition,
        head_sha,
        "7" * 64,
        1,
    )
    child_spec, lane_id, run_id = _pipeline_verify_identity(
        parent_record.spec,
        definition_sha256=definition,
        input_sha256=input_sha,
        profile=parent_record.spec.verification_profile,
        attempt_index=attempt_index,
    )
    operation_id = child_spec.operation_id
    effect_id = pipeline_verify_effect_id(input_sha, attempt_index)
    store.create(child_spec, lane_id=lane_id, run_id=run_id)
    output = runtime / "verification-output.log"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"ok\n")
    evidence = VerificationEvidence(
        "scoped", "7" * 64, head_sha, "scoped-1", ".",
        0 if status == "complete" else 1, started_at, finished_at,
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


def _running_current_verification(
    store: OperationStore,
    parent: str,
    compiled: object,
    *,
    head_sha: str,
    profile_sha256: str = "7" * 64,
    owner: str = OWNER,
) -> str:
    """Create the exact production-shaped child for the current verify attempt."""

    parent_record = store.read(owner, parent)
    verify_step = next(
        step for step in compiled.definition.steps if step.primitive_id == "verify"
    )
    input_sha256 = verification_input_sha256(
        compiled.definition_sha256,
        head_sha,
        profile_sha256,
        verify_step.schema_version,
    )
    child, lane_id, run_id = _pipeline_verify_identity(
        parent_record.spec,
        definition_sha256=compiled.definition_sha256,
        input_sha256=input_sha256,
        profile=parent_record.spec.verification_profile,
    )
    store.create(child, lane_id=lane_id, run_id=run_id)
    _advance(
        store,
        child.operation_id,
        "preflight",
        "starting",
        "running",
        "verifying",
        owner=owner,
    )
    return child.operation_id


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
                "context": {
                    "head_sha": head_sha,
                    "verification_profile": "scoped",
                    "verification_profile_sha256": "7" * 64,
                },
                "lanes": [{"axis": "openai-holistic"}, {"axis": "claude-spec"}],
                "round_results": {},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _accepted_review_round(
    store: OperationStore,
    *,
    owner: str,
    parent: str,
    axis: str,
) -> tuple[str, str, str, str]:
    """Create one exact accepted review child for scalar dashboard evidence."""

    operation_id = f"{parent}-round-0"
    _create(
        store,
        operation_id,
        "review-round",
        lane_id=f"{axis}-lane",
        parent=parent,
        owner=owner,
        route=_reviewer_route(),
    )
    _advance(
        store,
        operation_id,
        "preflight",
        "starting",
        "running",
        "awaiting-callback",
        owner=owner,
    )
    payload = {"axis": axis, "verdict": "changes-requested"}
    payload_sha256 = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    callback_id = f"review-{payload_sha256[:24]}"
    CallbackBroker(store, owner).accept(
        CallbackEnvelope(
            callback_id,
            operation_id,
            f"{operation_id}-run",
            "review",
            payload,
            payload_sha256,
        )
    )
    _advance(store, operation_id, "finalizing", "exiting", "complete", owner=owner)
    return operation_id, f"{operation_id}-run", callback_id, payload_sha256


def _tree_bytes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _liveness(
    store: OperationStore,
    owner: str,
    operation_id: str,
    *,
    started_at: float,
    last_progress_at: float,
    revision: int | None = None,
) -> Path:
    record = store.read(owner, operation_id)
    path = (
        store.root
        / "owners"
        / owner
        / "runtime"
        / operation_id
        / "liveness"
        / "state.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "started_at": started_at,
                "last_progress_at": last_progress_at,
                "operation_revision": (
                    record.revision if revision is None else revision
                ),
                "operation_state": record.state,
                "screen_sha256": "",
                "typed_result_sha256": "",
                "callback_sha256": "",
                "receipt_sha256": "",
                "stable_result_reads": 0,
                "nudge_count": 0,
                "restart_count": 0,
                "callback_submit_binding": "",
                "callback_submit_status": "",
                "schema_version": 1,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


isolated_import = subprocess.run(
    [
        sys.executable,
        "-c",
        (
            "import sys; "
            f"sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
            "import harness.dashboard_projection; "
            "raise SystemExit('harness.runtime_worker' in sys.modules)"
        ),
    ],
    cwd=ROOT,
    check=False,
)
check(
    "read-only dashboard projection does not import the provider runtime worker",
    isolated_import.returncode == 0,
)

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
    colored = render(projection, color=True)
    attention_projection = replace(projection, classification=ATTENTION)
    attention_colored = render(attention_projection, color=True)
    retry_projection = replace(
        projection,
        issues=(
            IssueView(
                "verification-receipt-failed",
                DISPATCH,
                "verify durable evidence is not accepted",
                ATTENTION,
            ),
        ),
    )
    retry_colored = render(retry_projection, color=True)
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    regression_check(
        "the TTY renderer uses the approved truecolor palette without changing plain bytes",
        "\x1b[38;2;85;230;139m[x]\x1b[0m" in colored
        and "\x1b[38;2;91;217;238m[>]\x1b[0m" in colored
        and "\x1b[38;2;240;196;84mwaiting\x1b[0m" in colored
        and "\x1b[38;2;255;156;74mverification-receipt-failed\x1b[0m"
        in retry_colored
        and "\x1b[38;2;255;101;122mattention-required\x1b[0m" in attention_colored
        and "\x1b[38;2;216;120;238mclaude-opus-5\x1b[0m" in colored
        and "\x1b[38;2;244;244;247mHARNESS PIPELINES\x1b[0m" in colored
        and "\x1b[1m" in colored
        and "\x1b[48;" not in colored
        and _colorize("awaiting-transition", color=True)
        == "\x1b[38;2;240;196;84mawaiting-transition\x1b[0m"
        and _colorize("awaiting-callback", color=True)
        == "\x1b[38;2;240;196;84mawaiting-callback\x1b[0m"
        and ansi.sub("", colored) == text
        and ansi.sub("", attention_colored) == render(attention_projection)
        and ansi.sub("", retry_colored) == render(retry_projection),
    )
    text_lines = text.splitlines()
    regression_check(
        "the approved hierarchy expands only current work and keeps timing on every row",
        text_lines[0] == "HARNESS PIPELINES"
        and text_lines.index("  steps")
        < next(index for index, line in enumerate(text_lines) if "lanes" in line)
        and sum(
            line.lstrip().startswith("[x] minimal-fix") for line in text_lines
        ) == 1
        and sum("review" in line for line in text_lines) >= 2
        and "time unknown" in text
        and "review cycle unknown/3" in text,
    )
    viewport_matrix = {
        rows: (
            render(projection, rows=rows),
            render(projection, rows=rows, color=True),
        )
        for rows in range(len(text_lines) + 1)
    }
    narrow = viewport_matrix[10][0]
    regression_check(
        "every viewport budget is bounded, equivalent, and truthfully prioritized",
        all(
            len(plain.splitlines()) <= rows
            and all(len(line) <= MAX_LINE for line in plain.splitlines())
            and ansi.sub("", colored) == plain
            for rows, (plain, colored) in viewport_matrix.items()
        )
        and DISPATCH in narrow
        and "[>] review" in narrow
        and "Viewport truncated +" in narrow,
    )
    formatted = tuple(
        render(
            replace(
                projection,
                programs=(
                    replace(
                        projection.programs[0],
                        timing=TimingView("elapsed", seconds),
                    ),
                ),
            )
        )
        for seconds in (0, 59, 60, 3600)
    )
    regression_check(
        "display timing uses deterministic whole-second terminal formatting",
        all(
            expected in frame
            for expected, frame in zip(
                ("elapsed 0s", "elapsed 59s", "elapsed 1m 00s", "elapsed 1h 00m"),
                formatted,
                strict=True,
            )
        ),
    )

    executor_route = RouteView("codex", "gpt-5.6-terra", "high", "executor")
    reviewer_route = RouteView(
        "codex", "gpt-5.6-sol", "high", "reviewer-callback"
    )
    root_steps = (
        StepView(
            "implement-dashboard",
            "model_step@1.0.0",
            "reuse",
            "complete",
            1,
            route=executor_route,
            timing=TimingView("duration", 125),
        ),
        StepView(
            "review-candidate",
            "review@1.0.0",
            "fresh",
            "running",
            1,
            route=reviewer_route,
            children=(
                ChildView(
                    "review-round-abcdef",
                    "review-round",
                    "awaiting-callback",
                    "running",
                    reviewer_route,
                    timing=TimingView("elapsed", 75),
                ),
            ),
            timing=TimingView("elapsed", 75),
            review=ReviewSummaryView(2, 3, 4, 1),
        ),
        StepView(
            "apply-fixes",
            "model_step@1.0.0",
            "reuse",
            "pending",
            0,
        ),
        StepView(
            "verify-candidate",
            "verify@1.0.0",
            "reuse",
            "pending",
            0,
        ),
    )
    root_program = replace(
        projection.programs[0],
        operation_id="a1b2c3d4-dispatch",
        kind="human-readable-dashboard",
        state="running",
        classification=ACTIVE,
        executor=executor_route,
        executor_status="running",
        steps=root_steps,
        children=(),
        lanes=(),
        timing=TimingView("elapsed", 300),
    )
    recent_program = replace(
        root_program,
        operation_id="deadbeef-terminal",
        kind="prior-dashboard-task",
        state="complete",
        classification=HEALTHY,
        executor_status="complete",
        steps=(),
        timing=TimingView("duration", 600),
    )
    root_projection = replace(
        projection,
        owner_id="a1b2c3d4-dispatch",
        classification=ATTENTION,
        programs=(root_program, recent_program),
        issues=(
            IssueView(
                "verification-receipt-failed",
                root_program.operation_id,
                "bounded issue detail",
                ATTENTION,
            ),
        ),
        truncated={},
        observed_at=3_723.0,
    )
    golden_100 = """HARNESS PIPELINES  store: .vault-meta/harness  updated 01:02:03
────────────────────────────────────────────────────────────────
● ACTIVE Human readable dashboard  dispatch a1b2c3d4  elapsed 5m 00s
  Executor codex/gpt-5.6-terra · high  running  elapsed 5m 00s
  ├─ ✓ Implement dashboard  complete  duration 2m 05s
  ├─ ● Review candidate  running  elapsed 1m 15s
  │    Reviewer codex/gpt-5.6-sol · high  cycle 2/3 · findings 4 · material 1
  │    ↳ review-round-abcdef  review round  awaiting-callback  elapsed 1m 15s
  ├─ ○ Apply fixes  pending
  └─ ○ Verify candidate  pending

RECENT
  ✓ Prior dashboard task  dispatch deadbeef  duration 10m 00s

ISSUES (1)
  ! verification-receipt-failed  a1b2c3d4  attention-required
────────────────────────────────────────────────────────────────
✓ complete  ● running  ○ pending  ! attention  ↻ retry
"""
    golden_80 = golden_100
    golden_frames = {
        columns: render(
            root_projection,
            scope="root",
            columns=columns,
            recent=3,
        )
        for columns in (80, 100, 120)
    }
    regression_check(
        "the root product view matches the exact 80 100 and 120 column goldens",
        golden_frames[80] == golden_80
        and golden_frames[100] == golden_100
        and golden_frames[120] == golden_100
        and all(
            hidden not in golden_frames[100]
            for hidden in (
                "classification",
                "Programs:",
                "rev ",
                "definition",
                "controls",
                "loop",
                "lanes",
                "children with no exact step lineage",
            )
        )
        and golden_frames[100].count("time unavailable") == 0,
    )
    colored_root = render(
        root_projection,
        scope="root",
        columns=100,
        color=True,
    )
    regression_check(
        "the root product maps every semantic RGB role and preserves plain bytes",
        ansi.sub("", colored_root) == golden_frames[100]
        and "\x1b[38;2;244;244;247mHARNESS PIPELINES" in colored_root
        and "\x1b[38;2;68;71;88m────────────────" in colored_root
        and "\x1b[38;2;85;230;139m✓\x1b[0m" in colored_root
        and "\x1b[38;2;91;217;238m●\x1b[0m" in colored_root
        and "\x1b[38;2;119;122;140m○\x1b[0m" in colored_root
        and "\x1b[38;2;240;196;84mawaiting-callback\x1b[0m" in colored_root
        and "\x1b[38;2;255;156;74mverification-receipt-failed\x1b[0m"
        in colored_root
        and "\x1b[38;2;255;101;122mattention-required\x1b[0m"
        in colored_root
        and "\x1b[38;2;216;120;238mgpt-5.6-sol\x1b[0m" in colored_root
        and "\x1b[38;2;112;168;255ma1b2c3d4\x1b[0m" in colored_root
        and "\x1b[1m" in next(
            line for line in colored_root.splitlines() if "Review candidate" in line
        )
        and "\x1b[48;" not in colored_root,
    )
    root_rows = {
        rows: (
            render(root_projection, scope="root", rows=rows, columns=100),
            render(
                root_projection,
                scope="root",
                rows=rows,
                columns=100,
                color=True,
            ),
        )
        for rows in range(len(golden_100.splitlines()) + 1)
    }
    regression_check(
        "the root viewport matrix preserves identity current work and explicit truncation",
        all(
            len(plain.splitlines()) <= rows
            and all(len(line) <= 100 for line in plain.splitlines())
            and ansi.sub("", colored_frame) == plain
            and (
                rows == 0
                or rows >= len(golden_100.splitlines())
                or "Viewport truncated +" in plain
            )
            for rows, (plain, colored_frame) in root_rows.items()
        )
        and root_program.operation_id[:8] in root_rows[6][0]
        and "Review candidate" in root_rows[6][0],
    )
    long_identity_frame = render(
        replace(
            root_projection,
            programs=(
                replace(
                    root_program,
                    task_name=(
                        "Human-readable terminal dashboard corrective visual "
                        "rework candidate with a deliberately long task name"
                    ),
                ),
            ),
            issues=(),
        ),
        scope="root",
        columns=80,
    )
    long_root_line = next(
        line for line in long_identity_frame.splitlines() if "ACTIVE" in line
    )
    regression_check(
        "root clipping retains task marker short identity timing and stable width",
        len(long_root_line) == 80
        and "...  dispatch a1b2c3d4  elapsed 5m 00s" in long_root_line
        and long_root_line.startswith("● ACTIVE Human-readable"),
    )

    maximum_step_id = "step-" + "x" * 123
    current_step_cases = {
        "pending": (
            "running",
            "pending",
            TimingView(),
            "○",
            "time unavailable",
        ),
        "running": (
            "running",
            "running",
            TimingView("elapsed", 75),
            "●",
            "elapsed 1m 15s",
        ),
        "attention": (
            "running",
            "attention",
            TimingView("elapsed", 75),
            "!",
            "elapsed 1m 15s",
        ),
        "failed-terminal": (
            "failed",
            "stopped",
            TimingView("duration", 125),
            "!",
            "duration 2m 05s",
        ),
        "cancelled-terminal": (
            "cancelled",
            "stopped",
            TimingView("duration", 125),
            "!",
            "duration 2m 05s",
        ),
    }
    current_step_width_results: list[bool] = []
    for (
        case,
        (program_state, step_status, timing, marker, suffix),
    ) in current_step_cases.items():
        long_step = StepView(
            maximum_step_id,
            "model_step@1.0.0",
            "reuse",
            step_status,
            1,
            route=executor_route,
            timing=timing,
        )
        long_step_program = replace(
            root_program,
            state=program_state,
            classification=(
                ATTENTION
                if case in {"attention", "failed-terminal", "cancelled-terminal"}
                else ACTIVE
            ),
            executor_status=program_state,
            steps=(long_step,),
        )
        long_step_projection = replace(
            root_projection,
            classification=long_step_program.classification,
            programs=(long_step_program,),
            issues=(),
        )
        for columns in (80, 100, 120):
            plain = render(
                long_step_projection,
                scope="root",
                columns=columns,
            )
            colored_frame = render(
                long_step_projection,
                scope="root",
                columns=columns,
                color=True,
            )
            row = next(
                line
                for line in plain.splitlines()
                if line.lstrip().startswith("└─")
            )
            current_step_width_results.append(
                len(row) <= columns
                and row.startswith(f"  └─ {marker} ")
                and f"  {step_status}  {suffix}" in row
                and "..." in row
                and maximum_step_id.replace("-", " ").capitalize()
                not in row
                and ansi.sub("", colored_frame) == plain
            )
    regression_check(
        "maximum current step identities retain state timing and ANSI equivalence",
        len(maximum_step_id) == 128
        and len(current_step_width_results)
        == len(current_step_cases) * 3
        and all(current_step_width_results),
    )

    cli_width_projection = replace(
        root_projection,
        programs=(
            replace(
                root_program,
                task_name=(
                    "Human-readable terminal dashboard corrective visual "
                    "rework candidate with a deliberately long task name"
                ),
            ),
        ),
        issues=(),
    )
    cli_width_frames: dict[tuple[int, bool], str] = {}
    cli_width_samples: dict[tuple[int, bool], int] = {}
    cli_width_codes: dict[tuple[int, bool], int] = {}
    cli_width_spec = importlib.util.spec_from_file_location(
        "harness_dashboard_cli_width_red",
        ROOT / "scripts" / "harness-dashboard.py",
    )
    assert cli_width_spec is not None and cli_width_spec.loader is not None
    cli_width_script = importlib.util.module_from_spec(cli_width_spec)
    sys.modules[cli_width_spec.name] = cli_width_script
    cli_width_spec.loader.exec_module(cli_width_script)
    original_project_root = cli_width_script.project_root
    cli_width_script.project_root = lambda *_args, **_kwargs: cli_width_projection
    try:
        for columns in (80, 120):
            for color in (False, True):
                size_samples: list[os.terminal_size] = []
                output: list[str] = []

                def terminal_size_probe(
                    width: int = columns,
                ) -> os.terminal_size:
                    size = os.terminal_size((width, 6))
                    size_samples.append(size)
                    return size

                values = [
                    "--store",
                    str(store_root),
                    "--root",
                    root_program.operation_id,
                ]
                if not color:
                    values.append("--no-color")
                try:
                    cli_width_codes[(columns, color)] = cli_width_script.main(
                        values,
                        inventory_probe=lambda **_kwargs: None,
                        sleeper=lambda _interval: (_ for _ in ()).throw(
                            KeyboardInterrupt()
                        ),
                        output=output.append,
                        tty_probe=lambda: True,
                        terminal_size=terminal_size_probe,
                        clock=lambda: 3_723.0,
                    )
                except TypeError:
                    cli_width_codes[(columns, color)] = -1
                cli_width_samples[(columns, color)] = len(size_samples)
                cli_width_frames[(columns, color)] = (
                    output[0].removeprefix(cli_width_script.CLEAR)
                    if len(output) == 1
                    else ""
                )
    finally:
        cli_width_script.project_root = original_project_root

    cli_width_plain = {
        columns: cli_width_frames[(columns, False)]
        for columns in (80, 120)
    }
    cli_width_colored = {
        columns: cli_width_frames[(columns, True)]
        for columns in (80, 120)
    }
    regression_check(
        "the live root CLI samples one terminal size and wires 80 and 120 column priority",
        all(code == 0 for code in cli_width_codes.values())
        and all(count == 1 for count in cli_width_samples.values())
        and all(
            len(frame.splitlines()) <= 6
            and all(len(line) <= columns for line in frame.splitlines())
            and root_program.operation_id[:8] in frame
            and "Review candidate" in frame
            and "Viewport truncated +" in frame
            for columns, frame in cli_width_plain.items()
        )
        and all(
            ansi.search(cli_width_plain[columns]) is None
            and ansi.search(cli_width_colored[columns]) is not None
            and ansi.sub("", cli_width_colored[columns])
            == cli_width_plain[columns]
            for columns in (80, 120)
        )
        and max(map(len, cli_width_plain[80].splitlines())) == 80
        and max(map(len, cli_width_plain[120].splitlines())) > 80,
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
        store, DISPATCH, runtime, compiled.definition_sha256
    )
    _verification_receipt(
        store, DISPATCH, runtime, compiled.definition_sha256, attempt_index=1
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
        head_char="8",
    )
    current_verification = _verification_receipt(
        store,
        DISPATCH,
        runtime,
        compiled.definition_sha256,
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

with tempfile.TemporaryDirectory(prefix="harness-dashboard-missing-current-verify.") as raw:
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
        head_char="8",
    )
    _write_gate(
        store,
        "verifying",
        subject=DISPATCH,
        head_sha="a" * 40,
    )

    missing = project(store_root, OWNER, inventory=LiveInventory({}))
    missing_program = missing.programs[0]
    missing_verify = next(
        step for step in missing_program.steps if step.step_id == "verify"
    )
    missing_evidence_issues = [
        issue.code
        for issue in missing.issues
        if issue.code.startswith(("verification-receipt-", "pipeline-progress-"))
    ]
    regression_check(
        "missing exact-HEAD verification has one accurate program-level outcome",
        missing_verify.visits == 1
        and missing_verify.status == "attention"
        and missing_program.next_action == "attention"
        and missing_program.classification == ATTENTION
        and missing_evidence_issues == ["verification-receipt-missing"],
    )
    unrelated_child = "dashboard-unrelated-live-verify"
    _create(
        store,
        unrelated_child,
        "pipeline-verify",
        lane_id="unrelated-verify-lane",
        contract_sha256=compiled.definition_sha256,
        parent=DISPATCH,
    )
    _advance(
        store,
        unrelated_child,
        "preflight",
        "starting",
        "running",
        "verifying",
    )
    unrelated = project(store_root, OWNER, inventory=LiveInventory({}))
    unrelated_program = unrelated.programs[0]
    unrelated_verify = next(
        step for step in unrelated_program.steps if step.step_id == "verify"
    )
    regression_check(
        "an unrelated running verification child cannot hide missing exact-HEAD evidence",
        unrelated_verify.status == "attention"
        and unrelated_program.next_action == "attention"
        and unrelated_program.classification == ATTENTION
        and any(
            issue.code == "verification-receipt-missing"
            for issue in unrelated.issues
        ),
    )
    current_child = _running_current_verification(
        store,
        DISPATCH,
        compiled,
        head_sha="a" * 40,
    )
    running = project(store_root, OWNER, inventory=LiveInventory({}))
    running_program = running.programs[0]
    running_verify = next(
        step for step in running_program.steps if step.step_id == "verify"
    )
    regression_check(
        "only the durably matching current verification attempt defers the missing receipt",
        running_verify.visits == 1
        and running_verify.status == "running"
        and current_child in {child.operation_id for child in running_verify.children}
        and running_program.next_action == "wait"
        and running_program.classification == ACTIVE
        and not any(
            issue.code.startswith(("verification-receipt-", "pipeline-progress-"))
            for issue in running.issues
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
        later_frontier = (
            replace(steps[0], status="complete"),
            replace(
                steps[1],
                status="stopped",
                evidence_issue="interrupted-frontier-evidence",
            ),
            *(
                replace(step, status="pending", evidence_issue="")
                for step in steps[2:]
            ),
        )
        frontier_projection = replace(
            projection,
            programs=(
                replace(projection.programs[0], steps=later_frontier),
            ),
        )
        frontier_plain = render(
            frontier_projection,
            scope="root",
            columns=100,
        )
        frontier_colored = render(
            frontier_projection,
            scope="root",
            columns=100,
            color=True,
        )
        stopped_line = next(
            line for line in frontier_plain.splitlines() if "Verify" in line
        )
        future_line = next(
            line for line in frontier_plain.splitlines() if "Review" in line
        )
        colored_stopped_line = next(
            line for line in frontier_colored.splitlines() if "Verify" in line
        )
        regression_check(
            f"a {terminal} root expands its stopped frontier before future pending work",
            "stopped  time unavailable" in stopped_line
            and "interrupted-frontier-evidence" in frontier_plain
            and "pending" in future_line
            and "time unavailable" not in future_line
            and "\x1b[1m" in colored_stopped_line
            and ansi.sub("", frontier_colored) == frontier_plain,
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
        and "claude/claude-opus-5/high" in text
        and "[ ] review" in text
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
    card = (
        render_custom_approval(spec, compiled, policy=policy)
        + "Host-reviewed coordinator authority: exact\n"
    )
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

with tempfile.TemporaryDirectory(prefix="harness-dashboard-viewport.") as raw:
    store_root = Path(raw) / "harness"
    store = OperationStore(store_root)
    compiled = compiled_builtin("engineering/change")
    attention_owners = tuple(
        f"dashboard-a-old-attention-{index}" for index in range(3)
    )
    for index, owner in enumerate(attention_owners):
        _create(
            store,
            owner,
            "dispatch",
            lane_id=f"lane-old-attention-{index}",
            contract_sha256=compiled.definition_sha256,
            owner=owner,
        )
        _advance(store, owner, "preflight", "starting", owner=owner)
        store.transition(
            owner,
            owner,
            "attention-required",
            reason=AttentionReason.ATTENTION_REQUIRED,
        )
        record_path = store_root / "owners" / owner / "operations" / f"{owner}.json"
        os.utime(record_path, (100 + index, 100 + index))

    live_owner = "dashboard-z-new-live"
    live_surface = "7A2B6C71-3333-4C22-8A22-2B2B2B2B2B2B"
    _create(
        store,
        live_owner,
        "dispatch",
        lane_id="lane-new-live",
        contract_sha256=compiled.definition_sha256,
        owner=live_owner,
    )
    _advance(store, live_owner, "preflight", "starting", "running", owner=live_owner)
    OperationSupervisor(store, live_owner, live_owner).bind_resources(
        OwnedResources(surface_id=live_surface)
    )
    _liveness(
        store,
        live_owner,
        live_owner,
        started_at=1_000.0,
        last_progress_at=1_100.0,
    )
    live_path = (
        store_root / "owners" / live_owner / "operations" / f"{live_owner}.json"
    )
    os.utime(live_path, (200, 200))

    viewport_rows = 36
    projection = dashboard_script.snapshot(
        store_root,
        recent=3,
        inventory=LiveInventory({live_surface.casefold(): "workspace-live"}),
    )
    supports_rows = "rows" in inspect.signature(render).parameters
    frame = (
        render(projection, rows=viewport_rows)
        if supports_rows
        else render(projection)
    )
    frame_lines = frame.splitlines()
    live_line = next(
        (index for index, line in enumerate(frame_lines) if live_owner in line),
        len(frame_lines),
    )
    attention_lines = [
        next(
            (index for index, line in enumerate(frame_lines) if owner in line),
            len(frame_lines),
        )
        for owner in attention_owners
    ]
    regression_check(
        "a bounded redraw keeps the newest live program above compact old attention summaries",
        supports_rows
        and len(frame_lines) <= viewport_rows
        and live_line < min(attention_lines)
        and max(
            later - earlier
            for earlier, later in zip(attention_lines, attention_lines[1:])
        )
        <= 2,
    )
    live_program = next(
        program for program in projection.programs if program.operation_id == live_owner
    )
    live_projection = replace(
        projection,
        classification=live_program.classification,
        programs=(live_program,),
        issues=(),
    )
    full_live = render(live_projection)
    exact_rows = len(full_live.splitlines())
    exact_fit = render(live_projection, rows=exact_rows)
    one_short = render(live_projection, rows=exact_rows - 1)
    check(
        "viewport exact-fit preserves full detail while one-short truncates truthfully",
        exact_fit == full_live
        and len(one_short.splitlines()) == exact_rows - 1
        and live_owner in one_short
        and "Viewport truncated" in one_short,
    )

    second_live = replace(live_program, operation_id="dashboard-y-second-live")
    multiple = render(
        replace(
            live_projection,
            programs=(second_live, live_program),
        ),
        rows=18,
    )
    attention_only = render(
        replace(
            projection,
            programs=tuple(
                program
                for program in projection.programs
                if program.operation_id in attention_owners
            ),
            issues=(),
        ),
        rows=12,
    )
    check(
        "bounded rendering retains multiple live identities and compacts attention-only views",
        len(multiple.splitlines()) <= 18
        and second_live.operation_id in multiple
        and live_owner in multiple
        and len(attention_only.splitlines()) <= 12
        and all(owner in attention_only for owner in attention_owners),
    )

    terminal_history = tuple(
        replace(
            live_program,
            operation_id=f"dashboard-terminal-{index}",
            state="complete",
            classification=HEALTHY,
        )
        for index in range(3)
    )
    full_footer_projection = replace(
        live_projection,
        programs=(live_program, *terminal_history),
        issues=tuple(
            IssueView(
                f"dashboard-issue-{index}",
                f"dashboard-terminal-{index % 3}",
                "bounded issue detail",
                ATTENTION,
            )
            for index in range(MAX_ISSUES)
        ),
    )
    tight_frames = {
        rows: render(full_footer_projection, recent=3, rows=rows)
        for rows in range(16, 21)
    }

    def truthful_section_count(
        frame: str,
        label: str,
        detail_prefix: str,
        total: int,
        omitted_word: str,
    ) -> bool:
        header = re.search(
            rf"^{re.escape(label)}: (\d+)(?: \(\+(\d+) {omitted_word}\))?$",
            frame,
            re.MULTILINE,
        )
        if header is None:
            return False
        shown = int(header.group(1))
        omitted = int(header.group(2) or 0)
        detail_rows = sum(
            line.startswith(detail_prefix) for line in frame.splitlines()
        )
        return shown == detail_rows and shown + omitted == total

    check(
        "small full-footer viewports retain the newest live program before old evidence",
        all(len(frame.splitlines()) <= rows for rows, frame in tight_frames.items())
        and all(live_owner in frame for frame in tight_frames.values())
        and all(
            truthful_section_count(
                frame,
                "Terminal history",
                "  dashboard-terminal-",
                len(terminal_history),
                "hidden",
            )
            and truthful_section_count(
                frame,
                "Recent issues",
                "  - dashboard-issue-",
                len(full_footer_projection.issues),
                "more",
            )
            for frame in tight_frames.values()
        ),
    )

    rendered_frames: list[str] = []
    row_values = iter((36, 24))
    clock_values = iter((1_300.0, 1_360.0))
    clock_samples: list[float] = []

    def frame_clock() -> float:
        value = next(clock_values)
        clock_samples.append(value)
        return value

    def stop_after_two_frames(_interval: float) -> None:
        if len(rendered_frames) == 2:
            raise KeyboardInterrupt

    baseline = _tree_bytes(store_root)
    resize_code = dashboard_script.main(
        ["--store", str(store_root), "--all", "--interval", "0.1", "--no-color"],
        inventory_probe=lambda **_kwargs: LiveInventory(
            {live_surface.casefold(): "workspace-live"}
        ),
        sleeper=stop_after_two_frames,
        output=rendered_frames.append,
        tty_probe=lambda: True,
        terminal_rows=lambda: next(row_values),
        clock=frame_clock,
    )
    once_output: list[str] = []
    once_code = dashboard_script.main(
        ["--store", str(store_root), "--all", "--once", "--no-color"],
        inventory_probe=lambda **_kwargs: LiveInventory(
            {live_surface.casefold(): "workspace-live"}
        ),
        output=once_output.append,
        tty_probe=lambda: True,
        terminal_rows=lambda: (_ for _ in ()).throw(
            AssertionError("--once must not inspect terminal rows")
        ),
        clock=lambda: 1_300.0,
    )
    frame_counts = [
        len(frame.removeprefix(dashboard_script.CLEAR).splitlines())
        for frame in rendered_frames
    ]
    check(
        "live redraw re-reads terminal rows while TTY --once stays byte-stable and unbounded",
        resize_code == 0
        and frame_counts[0] <= 36
        and frame_counts[1] <= 24
        and frame_counts[1] < frame_counts[0]
        and all(live_owner in frame for frame in rendered_frames)
        and clock_samples == [1_300.0, 1_360.0]
        and "elapsed 5m 00s" in rendered_frames[0]
        and "elapsed 6m 00s" in rendered_frames[1]
        and once_code == 0
        and len(once_output) == 1
        and not once_output[0].startswith(dashboard_script.CLEAR)
        and len(once_output[0].splitlines()) > 36
        and _tree_bytes(store_root) == baseline,
    )
    diagnostic_samples: list[float] = []
    diagnostic = readonly_dashboard(
        store_root,
        live_owner,
        inventory_probe=lambda **_kwargs: LiveInventory(
            {live_surface.casefold(): "workspace-live"}
        ),
        clock=lambda: diagnostic_samples.append(1_330.0) or 1_330.0,
    )
    regression_check(
        "the owner-wide diagnostic samples one display clock without authority",
        diagnostic_samples == [1_330.0]
        and diagnostic.programs[0].timing == TimingView("elapsed", 330)
        and _tree_bytes(store_root) == baseline,
    )

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
        "--all",
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
            [*command[:4], "--all", option, value, "--once"],
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
        ["--store", str(store_root), "--all", "--interval", "0.1", "--no-color"],
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


ROOT_SCOPE_A = "root-scope-alpha"
ROOT_SCOPE_B = "root-scope-beta"
ROOT_SCOPE_HISTORICAL = "root-scope-historical"

with tempfile.TemporaryDirectory(prefix="harness-dashboard-root.") as raw:
    store_root = Path(raw) / "harness"
    store = OperationStore(store_root)
    compiled = compiled_builtin("engineering/change")
    for owner in (ROOT_SCOPE_A, ROOT_SCOPE_B):
        _create(
            store,
            owner,
            "dispatch",
            lane_id=f"lane-{owner}",
            contract_sha256=compiled.definition_sha256,
            owner=owner,
        )
        _advance(store, owner, "preflight", "starting", owner=owner)
        _liveness(
            store,
            owner,
            owner,
            started_at=1_000.0,
            last_progress_at=1_050.0,
        )
    scoped_child = f"{ROOT_SCOPE_A}-verify-0"
    _create(
        store,
        scoped_child,
        "pipeline-verify",
        lane_id=f"lane-{ROOT_SCOPE_A}",
        parent=ROOT_SCOPE_A,
        owner=ROOT_SCOPE_A,
    )
    _advance(store, scoped_child, "preflight", "starting", "running", owner=ROOT_SCOPE_A)
    _create(
        store,
        ROOT_SCOPE_HISTORICAL,
        "dispatch",
        lane_id="lane-historical",
        contract_sha256=compiled.definition_sha256,
        owner=ROOT_SCOPE_HISTORICAL,
    )
    _advance(store, ROOT_SCOPE_HISTORICAL, "preflight", "starting", owner=ROOT_SCOPE_HISTORICAL)
    store.transition(
        ROOT_SCOPE_HISTORICAL,
        ROOT_SCOPE_HISTORICAL,
        "attention-required",
        reason=AttentionReason.ATTENTION_REQUIRED,
    )

    baseline = _tree_bytes(store_root)
    scoped = project_root(store_root, ROOT_SCOPE_A, observed_at=1_300.0)
    scoped_frame = render(scoped, scope="root")
    check(
        "root projection renders exactly one root with its descendants and no unrelated owner",
        scoped.owner_id == ROOT_SCOPE_A
        and tuple(program.operation_id for program in scoped.programs)
        == (ROOT_SCOPE_A,)
        and scoped.classification in {HEALTHY, ACTIVE, WAITING}
        and all(
            issue.operation_id
            not in {ROOT_SCOPE_B, ROOT_SCOPE_HISTORICAL}
            for issue in scoped.issues
        )
        and "HARNESS PIPELINES  store: .vault-meta/harness" in scoped_frame
        and f"dispatch {ROOT_SCOPE_A[:8]}" in scoped_frame
        and scoped_child in scoped_frame
        and ROOT_SCOPE_B not in scoped_frame
        and ROOT_SCOPE_HISTORICAL not in scoped_frame
        and _tree_bytes(store_root) == baseline,
    )
    record_path = (
        store_root
        / "owners"
        / ROOT_SCOPE_A
        / "operations"
        / f"{ROOT_SCOPE_A}.json"
    )
    os.utime(record_path, (9_999, 9_999))
    mtime_only = project_root(
        store_root, ROOT_SCOPE_A, observed_at=1_300.0
    )
    regression_check(
        "record mtime never enters displayed timing or lifecycle truth",
        mtime_only.programs[0].timing == scoped.programs[0].timing
        == TimingView("elapsed", 300)
        and mtime_only.classification == scoped.classification,
    )

    pre = project_root(store_root, "root-scope-unseen")
    pre_frame = render(pre, scope="root")
    check(
        "a pre-start root observer is empty, waiting, and read-only",
        pre.owner_id == "root-scope-unseen"
        and pre.programs == ()
        and pre.issues == ()
        and pre.classification == WAITING
        and "root-sco" in pre_frame
        and "waiting" in pre_frame.lower()
        and ROOT_SCOPE_A not in pre_frame
        and _tree_bytes(store_root) == baseline,
    )

    early = "root-scope-early-failure"
    _create(
        store,
        early,
        "dispatch",
        lane_id="lane-early",
        contract_sha256=compiled.definition_sha256,
        owner=early,
    )
    _advance(store, early, "preflight", owner=early)
    store.transition(early, early, "failed")
    failed_scope = project_root(store_root, early)
    failed_frame = render(failed_scope, scope="root")
    check(
        "an early start failure is visible inside its root observer",
        tuple(program.state for program in failed_scope.programs) == ("failed",)
        and any(issue.code == "terminal-failed" for issue in failed_scope.issues)
        and early[:8] in failed_frame,
    )

    unseen_child_scope = project_root(store_root, scoped_child)
    nested_owner = "root-scope-nested"
    nested_parent = "root-scope-nested-parent"
    _create(
        store,
        nested_parent,
        "dispatch",
        lane_id="lane-nested",
        contract_sha256=compiled.definition_sha256,
        owner=nested_owner,
    )
    _create(
        store,
        nested_owner,
        "pipeline-verify",
        lane_id="lane-nested",
        parent=nested_parent,
        owner=nested_owner,
    )
    child_scope = project_root(store_root, nested_owner)
    check(
        "a non-root identity fails closed without rendering its parent tree",
        unseen_child_scope.programs == ()
        and unseen_child_scope.classification == WAITING
        and child_scope.programs == ()
        and any(
            issue.code == "root-scope-not-a-root"
            and issue.operation_id == nested_owner
            and issue.classification == COORDINATOR
            for issue in child_scope.issues
        )
        and child_scope.classification == COORDINATOR
        and nested_parent
        not in {program.operation_id for program in child_scope.programs},
    )

    scope_errors = []
    for argv in (
        ["--store", str(store_root), "--once", "--no-color"],
        [
            "--store",
            str(store_root),
            "--once",
            "--no-color",
            "--root",
            ROOT_SCOPE_A,
            "--all",
        ],
        ["--store", str(store_root), "--once", "--no-color", "--root", "../evil"],
        ["--store", str(store_root), "--once", "--no-color", "--root", ""],
    ):
        try:
            dashboard_script.main(argv, inventory_probe=lambda **_kwargs: None)
        except SystemExit as exc:
            scope_errors.append(exc.code)
        else:
            scope_errors.append(None)
    check(
        "normal mode requires exactly one exact --root and --all stays a separate diagnostic",
        scope_errors == [2, 2, 2, 2],
    )

    baseline = _tree_bytes(store_root)
    root_once: list[str] = []
    root_code = dashboard_script.main(
        ["--store", str(store_root), "--root", ROOT_SCOPE_A, "--once", "--no-color"],
        inventory_probe=lambda **_kwargs: None,
        output=root_once.append,
        tty_probe=lambda: False,
        clock=lambda: 1_300.0,
    )
    root_once_repeat: list[str] = []
    root_repeat_code = dashboard_script.main(
        ["--store", str(store_root), "--root", ROOT_SCOPE_A, "--once", "--no-color"],
        inventory_probe=lambda **_kwargs: None,
        output=root_once_repeat.append,
        tty_probe=lambda: True,
        clock=lambda: 1_300.0,
    )
    all_once: list[str] = []
    all_code = dashboard_script.main(
        ["--store", str(store_root), "--all", "--once", "--no-color"],
        inventory_probe=lambda **_kwargs: None,
        output=all_once.append,
        tty_probe=lambda: False,
    )
    check(
        "the CLI renders one root in normal mode while --all keeps the global diagnostic",
        root_code == 0
        and len(root_once) == 1
        and root_repeat_code == 0
        and root_once_repeat == root_once
        and "\x1b[" not in root_once[0]
        and ROOT_SCOPE_A in root_once[0]
        and ROOT_SCOPE_B not in root_once[0]
        and ROOT_SCOPE_HISTORICAL not in root_once[0]
        and all_code == 0
        and all(
            owner in all_once[0]
            for owner in (ROOT_SCOPE_A, ROOT_SCOPE_B, ROOT_SCOPE_HISTORICAL)
        )
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


OPEN_ROOT = "root-open-alpha"
OPEN_ROOT_B = "root-open-beta"

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
        root=OPEN_ROOT,
        store=store_root,
        caller_surface=caller,
        adapter=adapter,
        marker_root=marker_root,
    )
    second = dashboard_script.open_dashboard(
        vault=vault,
        root=OPEN_ROOT,
        store=store_root,
        caller_surface=caller,
        adapter=adapter,
        marker_root=marker_root,
    )
    fake.created = False
    replacement = dashboard_script.open_dashboard(
        vault=vault,
        root=OPEN_ROOT,
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

    fake.created = True
    second_root_result = dashboard_script.open_dashboard(
        vault=vault,
        root=OPEN_ROOT_B,
        store=store_root,
        caller_surface=caller,
        adapter=adapter,
        marker_root=marker_root,
    )
    second_root_repeat = dashboard_script.open_dashboard(
        vault=vault,
        root=OPEN_ROOT_B,
        store=store_root,
        caller_surface=caller,
        adapter=adapter,
        marker_root=marker_root,
    )
    marker_values = [
        json.loads(path.read_text(encoding="ascii"))
        for path in sorted(marker_root.glob("*.json"))
    ]
    send_texts = [call[-1] for call in fake.calls if call[1:2] == ["send"]]
    check(
        "dashboard split identity binds vault, workspace, and exact root",
        not second_root_result.reused
        and second_root_repeat.reused
        and sorted(value["root_id"] for value in marker_values)
        == sorted((OPEN_ROOT, OPEN_ROOT_B))
        and all(value["schema_version"] == 3 for value in marker_values)
        and any(f"--root {OPEN_ROOT}" in text for text in send_texts)
        and any(f"--root {OPEN_ROOT_B}" in text for text in send_texts)
        and all("--all" not in text for text in send_texts),
    )

    def cmux_effects() -> int:
        return len(
            [
                call
                for call in fake.calls
                if any(
                    op in call
                    for op in ("new-split", "send", "send-key", "close-surface")
                )
            ]
        )

    foreign_marker = next(
        path
        for path in marker_root.glob("*.json")
        if json.loads(path.read_text(encoding="ascii"))["root_id"] == OPEN_ROOT_B
    )
    foreign_value = json.loads(foreign_marker.read_text(encoding="ascii"))
    foreign_value["root_id"] = "root-open-foreign"
    foreign_marker.write_text(json.dumps(foreign_value), encoding="ascii")
    effects_before = cmux_effects()
    try:
        dashboard_script.open_dashboard(
            vault=vault,
            root=OPEN_ROOT_B,
            store=store_root,
            caller_surface=caller,
            adapter=adapter,
            marker_root=marker_root,
        )
    except Exception as exc:
        foreign_rejected = "identity" in str(exc)
    else:
        foreign_rejected = False
    check(
        "a foreign root marker fails closed without touching another surface",
        foreign_rejected and cmux_effects() == effects_before,
    )

    invalid_root_effects = []
    for bad_root in ("", "../evil", "root open space", "x" * 129):
        bad_fake = FakeCmuxRunner(caller, dashboard, workspace)
        try:
            dashboard_script.open_dashboard(
                vault=vault,
                root=bad_root,
                store=store_root,
                caller_surface=caller,
                adapter=CmuxAdapter(runner=bad_fake, binary="cmux"),
                marker_root=root / "bad-root-markers",
            )
        except Exception:
            invalid_root_effects.append(len(bad_fake.calls))
        else:
            invalid_root_effects.append(-1)
    try:
        dashboard_script.main(
            [
                "open",
                "--vault",
                str(vault),
                "--store",
                str(store_root),
                "--surface",
                caller,
            ]
        )
    except SystemExit as exc:
        open_requires_root = exc.code == 2
    else:
        open_requires_root = False
    check(
        "an inexact or missing root identity is rejected before any cmux effect",
        invalid_root_effects == [0, 0, 0, 0] and open_requires_root,
    )

    race_fake = FakeCmuxRunner(caller, dashboard, workspace)
    race_adapter = CmuxAdapter(runner=race_fake, binary="cmux")
    race_markers = root / "race-markers"
    dashboard_script.open_dashboard(
        vault=vault,
        root=OPEN_ROOT,
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
            root=OPEN_ROOT,
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
                root=OPEN_ROOT,
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
            root=OPEN_ROOT,
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
            root=OPEN_ROOT,
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
            root=OPEN_ROOT,
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
        root=OPEN_ROOT,
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
        root=OPEN_ROOT,
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
                root=OPEN_ROOT,
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
            root=OPEN_ROOT,
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
            root=OPEN_ROOT,
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
    alias_fake = FakeCmuxRunner(caller, caller, workspace)
    alias_adapter = CmuxAdapter(runner=alias_fake, binary="cmux")
    alias_markers = root / "alias-markers"
    alias_baseline = _tree_bytes(store_root)
    try:
        dashboard_script.open_dashboard(
            vault=vault,
            root=OPEN_ROOT,
            store=store_root,
            caller_surface=caller,
            adapter=alias_adapter,
            marker_root=alias_markers,
        )
    except Exception as exc:
        alias_rejected = "caller surface" in str(exc)
    else:
        alias_rejected = False
    alias_marker = json.loads(
        next(alias_markers.glob("*.json")).read_text(encoding="ascii")
    )
    check(
        "a split response cannot alias or mutate the coordinator surface",
        alias_rejected
        and not any(call[1:2] == ["send"] for call in alias_fake.calls)
        and not any(call[1:2] == ["send-key"] for call in alias_fake.calls)
        and not any(call[1:2] == ["close-surface"] for call in alias_fake.calls)
        and alias_marker["state"] == "retryable"
        and alias_marker["surface_id"] == ""
        and _tree_bytes(store_root) == alias_baseline,
    )

    def recover_persisted_caller_alias(
        state: str,
    ) -> tuple[object, list[list[str]], dict[str, object]]:
        fake = FakeCmuxRunner(caller, dashboard, workspace)
        adapter = CmuxAdapter(runner=fake, binary="cmux")
        markers = root / f"persisted-alias-{state}"
        dashboard_script.open_dashboard(
            vault=vault,
            root=OPEN_ROOT,
            store=store_root,
            caller_surface=caller,
            adapter=adapter,
            marker_root=markers,
            clock=lambda: 100.0,
        )
        marker_path = next(markers.glob("*.json"))
        marker = json.loads(marker_path.read_text(encoding="ascii"))
        marker.update(
            state=state,
            surface_id=caller,
            reserved_at=100.0 if state == "starting" else 0,
        )
        marker_path.write_text(json.dumps(marker), encoding="ascii")
        fake.created = False
        call_count = len(fake.calls)
        result = dashboard_script.open_dashboard(
            vault=vault,
            root=OPEN_ROOT,
            store=store_root,
            caller_surface=caller,
            adapter=adapter,
            marker_root=markers,
            clock=lambda: 131.0,
        )
        final_marker = json.loads(marker_path.read_text(encoding="ascii"))
        return result, fake.calls[call_count:], final_marker

    ready_result, ready_calls, ready_marker = recover_persisted_caller_alias(
        "ready"
    )
    starting_result, starting_calls, starting_marker = (
        recover_persisted_caller_alias("starting")
    )
    regression_check(
        "persisted caller aliases are retryable without reuse or caller cleanup",
        ready_result.surface_id == dashboard
        and not ready_result.reused
        and starting_result.surface_id == dashboard
        and not starting_result.reused
        and not any(
            call[1:2] == ["close-surface"] and caller in call
            for call in ready_calls + starting_calls
        )
        and ready_marker["state"] == "ready"
        and ready_marker["surface_id"] == dashboard
        and starting_marker["state"] == "ready"
        and starting_marker["surface_id"] == dashboard,
    )

# E267.RC4.DASH.TIME.RED: one fixed frame clock must expose only accepted,
# identity-bound durable timing and terminal review summary evidence.
with tempfile.TemporaryDirectory(prefix="harness-dashboard-time.") as raw:
    vault = Path(raw) / "vault"
    store_root = vault / ".vault-meta" / "harness"
    worktree = vault / "task-worktree"
    worktree.mkdir(parents=True)
    store = OperationStore(store_root)
    compiled = compiled_builtin("engineering/change")
    timed_root = "dashboard-timed-root"
    _create(
        store,
        timed_root,
        "dispatch",
        lane_id="timed-root-lane",
        contract_sha256=compiled.definition_sha256,
        owner=timed_root,
    )
    _advance(
        store,
        timed_root,
        "preflight",
        "starting",
        "running",
        owner=timed_root,
    )
    _liveness(
        store,
        timed_root,
        timed_root,
        started_at=1_000.0,
        last_progress_at=1_100.0,
    )
    timed_child = "dashboard-timed-verify"
    _create(
        store,
        timed_child,
        "pipeline-verify",
        lane_id="timed-child-lane",
        contract_sha256=compiled.definition_sha256,
        parent=timed_root,
        owner=timed_root,
    )
    _advance(
        store,
        timed_child,
        "preflight",
        "starting",
        "running",
        owner=timed_root,
    )
    _liveness(
        store,
        timed_root,
        timed_child,
        started_at=1_150.0,
        last_progress_at=1_200.0,
    )
    observed_at = 1_300.0
    timed = project_root(store_root, timed_root, observed_at=observed_at)
    timed_program = timed.programs[0]
    timed_tdd = next(
        step for step in timed_program.steps if step.step_id == "tdd-slices"
    )
    timed_verify = next(
        step for step in timed_program.steps if step.step_id == "verify"
    )
    regression_check(
        "live root and exact active child expose fixed-clock elapsed timing",
        timed_program.timing.mode == "elapsed"
        and timed_program.timing.seconds == 300
        and timed_verify.timing.mode == "elapsed"
        and timed_verify.timing.seconds == 150
        and timed_verify.children[0].timing.mode == "elapsed"
        and timed_verify.children[0].timing.seconds == 150,
    )
    regression_check(
        "the root-owned TDD step freezes when exact verification begins",
        timed_tdd.timing == TimingView("duration", 150),
    )
    timed_tdd_line = next(
        (line for line in render(timed).splitlines() if "tdd-slices" in line),
        "",
    )
    regression_check(
        "the frozen TDD token renders as duration with complete semantics",
        "complete" in timed_tdd_line and "duration 2m 30s" in timed_tdd_line,
    )
    later_timed = project_root(store_root, timed_root, observed_at=1_600.0)
    later_tdd = next(
        step
        for step in later_timed.programs[0].steps
        if step.step_id == "tdd-slices"
    )
    regression_check(
        "later dashboard frames cannot increase completed TDD duration",
        later_tdd.timing == TimingView("duration", 150),
    )
    timed_child_liveness = (
        store_root
        / "owners"
        / timed_root
        / "runtime"
        / timed_child
        / "liveness"
        / "state.json"
    )
    invalid_liveness = json.loads(
        timed_child_liveness.read_text(encoding="utf-8")
    )
    invalid_liveness["operation_revision"] = (
        store.read(timed_root, timed_child).revision + 1
    )
    timed_child_liveness.write_text(
        json.dumps(invalid_liveness, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    invalid_timed = project_root(store_root, timed_root, observed_at=1_600.0)
    invalid_tdd = next(
        step
        for step in invalid_timed.programs[0].steps
        if step.step_id == "tdd-slices"
    )
    regression_check(
        "invalid later-step liveness makes the TDD freeze unavailable",
        invalid_tdd.timing == TimingView(),
    )
    invalid_liveness["operation_revision"] = store.read(
        timed_root, timed_child
    ).revision
    invalid_liveness["started_at"] = 1_250.0
    invalid_liveness["last_progress_at"] = 1_200.0
    timed_child_liveness.write_text(
        json.dumps(invalid_liveness, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    reversed_timed = project_root(store_root, timed_root, observed_at=1_600.0)
    reversed_tdd = next(
        step
        for step in reversed_timed.programs[0].steps
        if step.step_id == "tdd-slices"
    )
    regression_check(
        "reversed later-step timestamps cannot freeze TDD",
        reversed_tdd.timing == TimingView(),
    )
    child_session = timed_child_liveness.parents[1] / "session.json"
    child_session.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": timed_child,
                "run_id": "foreign-run",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _liveness(
        store,
        timed_root,
        timed_child,
        started_at=1_150.0,
        last_progress_at=1_200.0,
    )
    foreign_run_timed = project_root(
        store_root, timed_root, observed_at=1_600.0
    )
    foreign_run_tdd = next(
        step
        for step in foreign_run_timed.programs[0].steps
        if step.step_id == "tdd-slices"
    )
    regression_check(
        "non-current child run identity cannot freeze TDD",
        foreign_run_tdd.timing == TimingView(),
    )
    child_session.unlink()
    _liveness(
        store,
        timed_root,
        timed_child,
        started_at=1_150.0,
        last_progress_at=1_200.0,
    )

    receipt_operation = _verification_receipt(
        store,
        timed_root,
        store_root / "owners" / timed_root / "runtime" / timed_root,
        compiled.definition_sha256,
        owner=timed_root,
    )
    _advance(
        store,
        receipt_operation,
        "preflight",
        "starting",
        "running",
        "finalizing",
        "exiting",
        "complete",
        owner=timed_root,
    )
    verified = project_root(store_root, timed_root, observed_at=1_800_000_000.0)
    verified_step = next(
        step for step in verified.programs[0].steps if step.step_id == "verify"
    )
    regression_check(
        "accepted verification evidence freezes the exact step duration",
        verified_step.timing.mode == "duration"
        and verified_step.timing.seconds == 120,
    )

    verify_runtime = (
        store_root / "owners" / timed_root / "runtime" / timed_root
    )
    producer_receipt_path = (
        verify_runtime
        / "pipeline-verification"
        / receipt_operation
        / "receipt.json"
    )
    rfc3339_receipt = json.loads(
        producer_receipt_path.read_text(encoding="utf-8")
    )
    producer_clock = iter((1_700_000_000.25, 1_700_000_002.75))
    original_verification_clock = harness_verification.time.time

    def verification_runner(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(
                command, 0, "8" * 40 + "\n", ""
            )
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    harness_verification.time.time = lambda: next(producer_clock)
    try:
        producer_evidence = harness_verification.run_profile(
            harness_verification.VerificationProfile(
                "scoped", ("true",), "7" * 64
            ),
            root=worktree,
            evidence_dir=verify_runtime / "producer-evidence",
            runner=verification_runner,
            pointer_root=verify_runtime,
        )
    finally:
        harness_verification.time.time = original_verification_clock
    producer_receipt = {
        **rfc3339_receipt,
        "evidence": [to_dict(item) for item in producer_evidence],
    }
    producer_receipt_path.write_text(
        json.dumps(producer_receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    producer_projected = project_root(
        store_root,
        timed_root,
        observed_at=1_700_000_010.0,
    )
    producer_step = next(
        step
        for step in producer_projected.programs[0].steps
        if step.step_id == "verify"
    )
    regression_check(
        "run_profile numeric epoch evidence freezes the accepted verification duration",
        producer_evidence[0].started_at == "1700000000.25"
        and producer_evidence[0].finished_at == "1700000002.75"
        and dashboard_receipts.verification_receipt_status(
            store,
            store.read(timed_root, timed_root),
            verify_runtime,
            producer_receipt_path,
        )
        == "complete"
        and producer_step.timing == TimingView("duration", 2),
    )
    producer_receipt_path.write_text(
        json.dumps(rfc3339_receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    reaped_root = "dashboard-reaped-root"
    _create(
        store,
        reaped_root,
        "dispatch",
        lane_id="reaped-root-lane",
        contract_sha256=compiled.definition_sha256,
        owner=reaped_root,
    )
    _advance(
        store,
        reaped_root,
        "preflight",
        "starting",
        "running",
        "finalizing",
        "exiting",
        "complete",
        owner=reaped_root,
    )
    reaped_verify = "dashboard-reaped-verify"
    _create(
        store,
        reaped_verify,
        "pipeline-verify",
        lane_id="reaped-verify-lane",
        contract_sha256=compiled.definition_sha256,
        parent=reaped_root,
        owner=reaped_root,
    )
    _advance(
        store,
        reaped_verify,
        "preflight",
        "starting",
        "running",
        "finalizing",
        "exiting",
        "complete",
        owner=reaped_root,
    )
    _liveness(
        store,
        reaped_root,
        reaped_verify,
        started_at=1_786_406_550.0,
        last_progress_at=1_786_406_600.0,
    )
    reaped_worktree = vault / "reaped-worktree"
    reaped_worktree.mkdir()
    plan_path = vault / "plans" / "approved.md"
    plan_path.parent.mkdir()
    plan_path.write_text("approved\n", encoding="utf-8")
    record = store.read(reaped_root, reaped_root)
    runtime = store_root / "owners" / reaped_root / "runtime" / reaped_root
    runtime.mkdir(parents=True)
    (runtime / "session.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": reaped_root,
                "run_id": record.run_id,
                "cwd": str(reaped_worktree),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    meta = {
        "version": 4,
        "task_id": reaped_root,
        "task_name": "dashboard-reaped-task",
        "worktree": str(reaped_worktree),
        "vault_root": str(vault),
        "plan_file": str(plan_path),
        "spawned_at": "2026-08-11T00:00:00Z",
    }
    meta_path = reaped_worktree / ".task-meta.json"
    meta_path.write_text(json.dumps(meta, sort_keys=True) + "\n", encoding="utf-8")
    (reaped_worktree / ".task-reap-complete.json").write_text(
        json.dumps(
            {
                "version": 1,
                "task_name": meta["task_name"],
                "vault_root": str(vault),
                "plan_path": str(plan_path),
                "meta_sha256": hashlib.sha256(meta_path.read_bytes()).hexdigest(),
                "validated": True,
                "completed_at": "2026-08-11T00:05:00Z",
                "task_session_status": "archived",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    reaped = project_root(store_root, reaped_root, observed_at=1_800_000_000.0)
    reaped_tdd = next(
        step
        for step in reaped.programs[0].steps
        if step.step_id == "tdd-slices"
    )
    regression_check(
        "validated reap evidence freezes terminal root duration",
        reaped.programs[0].timing.mode == "duration"
        and reaped.programs[0].timing.seconds == 300
        and reaped.programs[0].task_name == "dashboard-reaped-task"
        and reaped_tdd.timing == TimingView("duration", 150),
    )

    task_control_points = (
        *range(0x20),
        *range(0x7F, 0xA0),
    )
    rejected_task_controls: list[int] = []
    for codepoint in task_control_points:
        supplied_name = f"dashboard{chr(codepoint)}task"
        meta_path.write_text(
            json.dumps(
                {**meta, "task_name": supplied_name},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        control_projection = project_root(
            store_root,
            reaped_root,
            observed_at=1_800_000_000.0,
        )
        plain_control = render(
            control_projection,
            scope="root",
            columns=120,
        )
        colored_control = render(
            control_projection,
            scope="root",
            columns=120,
            color=True,
        )
        if (
            control_projection.programs[0].task_name == "unknown"
            and supplied_name not in plain_control
            and supplied_name not in colored_control
            and ansi.sub("", colored_control) == plain_control
        ):
            rejected_task_controls.append(codepoint)
    meta_path.write_text(
        json.dumps(meta, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    regression_check(
        "task display evidence rejects C0 C1 and DEL before colored or plain rendering",
        rejected_task_controls == list(task_control_points)
        and {0x1B, 0x08, 0x0D, 0x7F}.issubset(task_control_points),
    )

    session_path = runtime / "session.json"
    direct_session = session_path.read_text(encoding="utf-8")
    cwd_parent_alias = Path(raw) / "cwd-parent-alias"
    cwd_parent_alias.symlink_to(vault, target_is_directory=True)
    session = json.loads(direct_session)
    session["cwd"] = str(cwd_parent_alias / reaped_worktree.name)
    session_path.write_text(
        json.dumps(session, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    cwd_parent_projection = project_root(
        store_root,
        reaped_root,
        observed_at=1_800_000_000.0,
    )
    session_path.write_text(direct_session, encoding="utf-8")
    cwd_parent_alias.unlink()
    regression_check(
        "task timing rejects a symlinked parent above the original session cwd",
        cwd_parent_projection.programs[0].timing.mode == "unknown"
        and cwd_parent_projection.programs[0].task_name == "unknown",
    )

    store_parent_alias = Path(raw) / "store-parent-alias"
    store_parent_alias.symlink_to(vault, target_is_directory=True)
    store_parent_rejected = False
    try:
        project_root(
            store_parent_alias / ".vault-meta" / "harness",
            reaped_root,
            observed_at=1_800_000_000.0,
        )
    except ValueError:
        store_parent_rejected = True
    store_parent_alias.unlink()
    regression_check(
        "projection rejects a symlinked parent above the original store boundary",
        store_parent_rejected,
    )

    review_root = "dashboard-review-summary-root"
    _create(
        store,
        review_root,
        "dispatch",
        lane_id="review-summary-lane",
        contract_sha256=compiled.definition_sha256,
        owner=review_root,
    )
    _advance(
        store,
        review_root,
        "preflight",
        "starting",
        "running",
        owner=review_root,
    )
    policy = ReviewAttemptPolicy(
        "deep", False, "codex", "gpt-5.6-sol", "high", 2,
        "implementation", "openai",
    )
    lanes = tuple(
        ReviewAttemptLaneIdentity(
            axis,
            review_root,
            f"{review_root}-{index}",
            f"review-lane-{index}",
            f"review-run-{index}",
            "codex",
            "gpt-5.6-sol",
            "high",
            "reviewer-callback",
            str(index + 1) * 64,
        )
        for index, axis in enumerate(("openai-intent", "openai-engineering"))
    )
    identity = ReviewAttemptIdentity(
        "review-attempt-2",
        "review-lineage",
        2,
        "a" * 64,
        "b" * 64,
        "c" * 40,
        policy,
        lanes,
    )
    lane_results = tuple(
        ReviewAttemptLaneResult(
            lane.axis,
            "approve",
            str(index + 3) * 64,
            ("finding-shared", f"finding-{index}"),
        )
        for index, lane in enumerate(lanes)
    )
    attempt = ReviewAttempt(
        identity,
        "terminal",
        ReviewAttemptTerminal(
            ReviewAttemptTerminalResult.APPROVED,
            identity.exact_head_sha,
            lane_results,
        ),
    )
    _write_gate(store, "approved", subject=review_root, owner=review_root)
    gate_path = (
        store_root
        / "review-data"
        / review_root
        / review_root
        / "review-gate.json"
    )
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    gate["attempt"] = attempt.payload()
    gate_path.write_text(json.dumps(gate, sort_keys=True) + "\n", encoding="utf-8")
    reviewed = project_root(store_root, review_root, observed_at=observed_at)
    review_step = next(
        step for step in reviewed.programs[0].steps if step.step_id == "review"
    )
    regression_check(
        "stale review HEAD axes lanes runs and attempt expose no summary metrics",
        review_step.review.cycle is None
        and review_step.review.limit == 2
        and review_step.review.findings is None
        and review_step.review.material_findings is None,
    )

    for lane in lanes:
        store.create(
            OperationSpec(
                lane.operation_id,
                f"{lane.operation_id}-key",
                "implementation-review",
                review_root,
                RuntimeRoute(
                    lane.runtime,
                    lane.model,
                    lane.effort,
                    lane.profile,
                    lane.routing_sha256,
                ),
                "packets/review.json",
                "scoped",
                parent_operation_id=review_root,
            ),
            lane_id=lane.lane_id,
            run_id=lane.run_id,
        )
    gate.update(
        active_review_operation_id=identity.attempt_id,
        context={
            **gate["context"],
            "head_sha": identity.exact_head_sha,
        },
        lanes=[
            {
                "axis": lane.axis,
                "operation_id": lane.operation_id,
                "lane_id": lane.lane_id,
                "run_id": lane.run_id,
                "surface_id": "",
                "checkpoint": "",
                "verification_iteration": 0,
                "state": "complete",
            }
            for lane in lanes
        ],
    )
    gate_path.write_text(json.dumps(gate, sort_keys=True) + "\n", encoding="utf-8")
    reviewed = project_root(store_root, review_root, observed_at=observed_at)
    review_step = next(
        step for step in reviewed.programs[0].steps if step.step_id == "review"
    )
    regression_check(
        "terminal review summary validates exact gate HEAD axes lanes runs and attempt",
        review_step.review.cycle == 2
        and review_step.review.limit == 2
        and review_step.review.findings == 3
        and review_step.review.material_findings == 0,
    )

    gate_directory = gate_path.parent
    real_gate_directory = gate_directory.with_name(gate_directory.name + "-real")
    gate_directory.rename(real_gate_directory)
    gate_directory.symlink_to(real_gate_directory, target_is_directory=True)
    symlinked_gate = project_root(
        store_root, review_root, observed_at=observed_at
    )
    symlinked_review = next(
        step
        for step in symlinked_gate.programs[0].steps
        if step.step_id == "review"
    )
    regression_check(
        "review summary rejects a gate reached through a symlinked ancestor",
        symlinked_review.review.cycle is None
        and symlinked_review.review.findings is None,
    )
    gate_directory.unlink()
    real_gate_directory.rename(gate_directory)

    # E267.RC62.CYCLE_HISTORY.RED: archived exact-HEAD attempts and their
    # current-root operation records must remain a complete correction story.
    first_lanes = tuple(
        ReviewAttemptLaneIdentity(
            axis,
            review_root,
            f"{review_root}-cycle-1-{index}",
            f"review-cycle-1-lane-{index}",
            f"review-cycle-1-run-{index}",
            "claude",
            "claude-opus-5",
            "high",
            "reviewer-callback",
            "b" * 64,
        )
        for index, axis in enumerate(("openai-intent", "openai-engineering"))
    )
    first_identity = ReviewAttemptIdentity(
        "review-attempt-1",
        "review-lineage",
        1,
        "a" * 64,
        "b" * 64,
        "b" * 40,
        ReviewAttemptPolicy(
            "deep", False, "claude", "claude-opus-5", "high", 2,
            "implementation", "openai",
        ),
        first_lanes,
    )
    first_attempt = ReviewAttempt(
        first_identity,
        "terminal",
        ReviewAttemptTerminal(
            ReviewAttemptTerminalResult.CHANGES_REQUESTED,
            first_identity.exact_head_sha,
            (
                ReviewAttemptLaneResult(
                    first_lanes[0].axis,
                    "changes-requested",
                    "d" * 64,
                    ("finding-shared", "finding-material"),
                ),
                ReviewAttemptLaneResult(
                    first_lanes[1].axis,
                    "approve",
                    "e" * 64,
                    ("finding-shared", "finding-info"),
                ),
            ),
        ),
    )
    first_rows: list[dict[str, object]] = []
    first_notifications: dict[str, dict[str, object]] = {}
    for lane in first_lanes:
        store.create(
            OperationSpec(
                lane.operation_id,
                f"{lane.operation_id}-key",
                "implementation-review",
                review_root,
                RuntimeRoute(
                    lane.runtime,
                    lane.model,
                    lane.effort,
                    lane.profile,
                    lane.routing_sha256,
                ),
                "packets/review.json",
                "scoped",
                parent_operation_id=review_root,
            ),
            lane_id=lane.lane_id,
            run_id=lane.run_id,
        )
        _advance(
            store,
            lane.operation_id,
            "preflight",
            "starting",
            "running",
            "awaiting-callback",
            "finalizing",
            "exiting",
            "complete",
            owner=review_root,
        )
        round_id, round_run, callback_id, callback_sha = _accepted_review_round(
            store,
            owner=review_root,
            parent=lane.operation_id,
            axis=lane.axis,
        )
        first_rows.append(
            {
                "axis": lane.axis,
                "operation_id": lane.operation_id,
                "lane_id": lane.lane_id,
                "run_id": lane.run_id,
                "surface_id": "",
                "checkpoint": "",
                "verification_iteration": 0,
                "state": "complete",
            }
        )
        first_notifications[lane.axis] = {
            "reviewed_head_sha": first_identity.exact_head_sha,
            "review_operation_id": first_identity.attempt_id,
            "round_operation_id": round_id,
            "round_run_id": round_run,
            "callback_id": callback_id,
            "callback_sha256": callback_sha,
            "material_finding_ids": (
                ["finding-material"] if lane is first_lanes[0] else []
            ),
        }
    first_gate = {
        "schema_version": 1,
        "owner_id": review_root,
        "dispatch_operation_id": review_root,
        "status": "changes-requested",
        "active_review_operation_id": first_identity.attempt_id,
        "context": {
            "head_sha": first_identity.exact_head_sha,
            "verification_profile": "scoped",
            "verification_profile_sha256": "7" * 64,
        },
        "lanes": first_rows,
        "review_notification_evidence": first_notifications,
        "attempt": first_attempt.payload(),
    }
    archive_path = gate_directory / "attempts" / "cycle-1.json"
    archive_path.parent.mkdir(mode=0o700)
    archive_path.write_text(
        json.dumps(first_gate, sort_keys=True) + "\n", encoding="utf-8"
    )
    current_gate = json.loads(gate_path.read_text(encoding="utf-8"))
    current_gate["context"]["head_sha"] = identity.exact_head_sha
    current_gate["active_review_operation_id"] = identity.attempt_id
    current_gate["status"] = "approved"
    gate_path.write_text(
        json.dumps(current_gate, sort_keys=True) + "\n", encoding="utf-8"
    )
    review_runtime = (
        store_root / "owners" / review_root / "runtime" / review_root
    )
    corrected_verification = _verification_receipt(
        store,
        review_root,
        review_runtime,
        compiled.definition_sha256,
        head_char="c",
        owner=review_root,
    )
    _advance(
        store,
        corrected_verification,
        "preflight",
        "starting",
        "running",
        "finalizing",
        "exiting",
        "complete",
        owner=review_root,
    )
    foreign_root = "dashboard-foreign-history-root"
    _create(
        store,
        foreign_root,
        "dispatch",
        lane_id="foreign-history-lane",
        contract_sha256=compiled.definition_sha256,
        owner=foreign_root,
    )
    history_projection = project_root(
        store_root, review_root, observed_at=1_800_000_000.0
    )
    history_program = history_projection.programs[0]
    history = getattr(history_program, "history", ())
    history_render = render(
        history_projection, scope="root", columns=120, color=False
    )
    history_narrow = render(
        history_projection, scope="root", columns=80, color=False
    )
    history_colored = render(
        history_projection, scope="root", columns=120, color=True
    )
    regression_check(
        "two exact review cycles retain ordered review fix and re-verification history",
        tuple((item.kind, item.cycle) for item in history)
        == (("review", 1), ("fix", 1), ("reverify", 1), ("review", 2)),
    )
    regression_check(
        "terminal cycle counts and every current-root reviewer identity remain visible",
        bool(history)
        and history[0].review == ReviewSummaryView(1, 2, 3, 1)
        and {
            child.operation_id
            for phase in history
            for child in phase.children
            if child.kind == "implementation-review"
        }
        == {lane.operation_id for lane in (*first_lanes, *lanes)}
        and foreign_root not in history_render,
    )
    regression_check(
        "root rendering tells the complete compact correction story",
        all(
            label in history_render
            for label in (
                "Review 1",
                "Fix 1",
                "Re-verify 1",
                "Review 2",
                "findings 3",
                "material 1",
            )
        ),
    )
    regression_check(
        "history preserves narrow color and no-color terminal contracts",
        all(len(line) <= 80 for line in history_narrow.splitlines())
        and all(len(line) <= 120 for line in history_render.splitlines())
        and ansi.sub("", history_colored) == history_render
        and "Review 1" in history_narrow
        and "Review 2" in history_narrow,
    )

    archive_directory = archive_path.parent
    archive_real = archive_directory.with_name("attempts-real")
    archive_directory.rename(archive_real)
    archive_directory.symlink_to(archive_real, target_is_directory=True)
    symlinked_history = getattr(
        project_root(
            store_root, review_root, observed_at=1_800_000_000.0
        ).programs[0],
        "history",
        (),
    )
    archive_directory.unlink()
    archive_real.rename(archive_directory)
    mismatched_archive = json.loads(archive_path.read_text(encoding="utf-8"))
    mismatched_archive["attempt"]["identity"]["plan_sha256"] = "0" * 64
    archive_path.write_text(
        json.dumps(mismatched_archive, sort_keys=True) + "\n", encoding="utf-8"
    )
    mismatched_history = getattr(
        project_root(
            store_root, review_root, observed_at=1_800_000_000.0
        ).programs[0],
        "history",
        (),
    )
    archive_path.write_text(
        json.dumps(first_gate, sort_keys=True) + "\n", encoding="utf-8"
    )
    over_cap_gate = json.loads(gate_path.read_text(encoding="utf-8"))
    over_cap_gate["attempt"]["identity"]["cycle"] = 6
    gate_path.write_text(
        json.dumps(over_cap_gate, sort_keys=True) + "\n", encoding="utf-8"
    )
    over_cap_history = getattr(
        project_root(
            store_root, review_root, observed_at=1_800_000_000.0
        ).programs[0],
        "history",
        (),
    )
    gate_path.write_text(
        json.dumps(current_gate, sort_keys=True) + "\n", encoding="utf-8"
    )
    regression_check(
        "symlinked mismatched and over-cap cycle evidence never becomes history authority",
        tuple(item.cycle for item in symlinked_history) == (2,)
        and tuple(item.cycle for item in mismatched_history) == (2,)
        and over_cap_history == (),
    )

    fixing_gate = {**first_gate, "status": "changes-requested"}
    gate_path.write_text(
        json.dumps(fixing_gate, sort_keys=True) + "\n", encoding="utf-8"
    )
    fixing_render = render(
        project_root(store_root, review_root, observed_at=1_800_000_000.0),
        scope="root",
        columns=120,
    )
    resolved_head = "d" * 40
    active_verification = _running_current_verification(
        store,
        review_root,
        compiled,
        head_sha=resolved_head,
        owner=review_root,
    )
    reverifying_gate = {
        **first_gate,
        "status": "verifying",
        "context": {**first_gate["context"], "head_sha": resolved_head},
    }
    gate_path.write_text(
        json.dumps(reverifying_gate, sort_keys=True) + "\n", encoding="utf-8"
    )
    reverifying_projection = project_root(
        store_root, review_root, observed_at=1_800_000_000.0
    )
    reverifying_program = reverifying_projection.programs[0]
    reverifying_render = render(
        reverifying_projection, scope="root", columns=120
    )
    gate_path.write_text(
        json.dumps(current_gate, sort_keys=True) + "\n", encoding="utf-8"
    )
    regression_check(
        "review remediation renders explicit fixing and re-verifying subphases",
        "Fixing review findings" in fixing_render
        and "Re-verifying" in reverifying_render
        and reverifying_program.current_stage == "Re-verifying"
        and any(
            phase.kind == "reverify"
            and phase.status == "running"
            and active_verification
            in {child.operation_id for child in phase.children}
            for phase in reverifying_program.history
        ),
    )

    bad_liveness = _liveness(
        store,
        timed_root,
        timed_root,
        started_at=1_400.0,
        last_progress_at=1_400.0,
    )
    rejected = project_root(store_root, timed_root, observed_at=observed_at)
    regression_check(
        "future durable liveness is rejected instead of becoming display truth",
        rejected.programs[0].timing.mode == "unknown",
    )
    bad_liveness.unlink()

    liveness_path = _liveness(
        store,
        timed_root,
        timed_root,
        started_at=1_000.0,
        last_progress_at=1_100.0,
    )
    valid_liveness = json.loads(liveness_path.read_text(encoding="utf-8"))
    liveness_cases = {
        "malformed object": "[]\n",
        "non-finite start": json.dumps(
            {**valid_liveness, "started_at": float("nan")}
        ) + "\n",
        "negative start": json.dumps(
            {**valid_liveness, "started_at": -1.0}
        ) + "\n",
        "reversed progress": json.dumps(
            {**valid_liveness, "last_progress_at": 900.0}
        ) + "\n",
        "future progress": json.dumps(
            {**valid_liveness, "last_progress_at": 1_301.0}
        ) + "\n",
        "revision ahead": json.dumps(
            {
                **valid_liveness,
                "operation_revision": store.read(timed_root, timed_root).revision + 1,
            }
        ) + "\n",
    }
    rejected_liveness: list[str] = []
    for label, payload in liveness_cases.items():
        liveness_path.write_text(payload, encoding="utf-8")
        if project_root(
            store_root, timed_root, observed_at=observed_at
        ).programs[0].timing.mode == "unknown":
            rejected_liveness.append(label)
    liveness_path.write_text(
        json.dumps(valid_liveness, sort_keys=True) + "\n", encoding="utf-8"
    )
    liveness_real = liveness_path.parent.with_name("liveness-real")
    liveness_path.parent.rename(liveness_real)
    liveness_path.parent.symlink_to(liveness_real, target_is_directory=True)
    ancestor_symlink_rejected = project_root(
        store_root, timed_root, observed_at=observed_at
    ).programs[0].timing.mode == "unknown"
    liveness_path.parent.unlink()
    liveness_real.rename(liveness_path.parent)
    liveness_leaf_real = liveness_path.with_name("state-real.json")
    liveness_path.rename(liveness_leaf_real)
    liveness_path.symlink_to(liveness_leaf_real)
    leaf_symlink_rejected = project_root(
        store_root, timed_root, observed_at=observed_at
    ).programs[0].timing.mode == "unknown"
    liveness_path.unlink()
    liveness_leaf_real.rename(liveness_path)
    regression_check(
        "liveness rejects the complete malformed timestamp revision and symlink matrix",
        rejected_liveness == list(liveness_cases)
        and ancestor_symlink_rejected
        and leaf_symlink_rejected,
    )

    reaped_runtime = (
        store_root / "owners" / reaped_root / "runtime" / reaped_root
    )
    session_path = reaped_runtime / "session.json"
    valid_session = json.loads(session_path.read_text(encoding="utf-8"))
    meta_path = reaped_worktree / ".task-meta.json"
    valid_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    complete_path = reaped_worktree / ".task-reap-complete.json"
    valid_complete = json.loads(complete_path.read_text(encoding="utf-8"))

    def root_duration_unknown(path: Path, payload: str) -> bool:
        original = path.read_text(encoding="utf-8")
        path.write_text(payload, encoding="utf-8")
        try:
            return project_root(
                store_root, reaped_root, observed_at=1_800_000_000.0
            ).programs[0].timing.mode == "unknown"
        finally:
            path.write_text(original, encoding="utf-8")

    bound_task_cases = {
        "session malformed": (session_path, "{\n"),
        "session operation": (
            session_path,
            json.dumps({**valid_session, "operation_id": "wrong-operation"}),
        ),
        "session run": (
            session_path,
            json.dumps({**valid_session, "run_id": "wrong-run"}),
        ),
        "session cwd": (
            session_path,
            json.dumps({**valid_session, "cwd": "relative-worktree"}),
        ),
        "meta task": (
            meta_path,
            json.dumps({**valid_meta, "task_id": "wrong-task"}),
        ),
        "meta worktree": (
            meta_path,
            json.dumps({**valid_meta, "worktree": str(vault)}),
        ),
        "meta vault": (
            meta_path,
            json.dumps({**valid_meta, "vault_root": str(worktree)}),
        ),
        "meta timestamp": (
            meta_path,
            json.dumps({**valid_meta, "spawned_at": "not-rfc3339"}),
        ),
        "reap malformed": (complete_path, "[]\n"),
        "reap reversed": (
            complete_path,
            json.dumps(
                {**valid_complete, "completed_at": "2026-08-10T23:59:59Z"}
            ),
        ),
        "reap future": (
            complete_path,
            json.dumps(
                {**valid_complete, "completed_at": "2099-08-11T00:05:00Z"}
            ),
        ),
        "reap digest": (
            complete_path,
            json.dumps({**valid_complete, "meta_sha256": "0" * 64}),
        ),
        "reap task": (
            complete_path,
            json.dumps({**valid_complete, "task_name": "wrong-task"}),
        ),
        "reap vault": (
            complete_path,
            json.dumps({**valid_complete, "vault_root": str(worktree)}),
        ),
        "reap plan": (
            complete_path,
            json.dumps({**valid_complete, "plan_path": str(worktree)}),
        ),
        "reap status": (
            complete_path,
            json.dumps({**valid_complete, "task_session_status": "active"}),
        ),
        "reap validation": (
            complete_path,
            json.dumps({**valid_complete, "validated": False}),
        ),
    }
    rejected_bound_task = [
        label
        for label, (path, payload) in bound_task_cases.items()
        if root_duration_unknown(path, payload + ("" if payload.endswith("\n") else "\n"))
    ]

    meta_real = meta_path.with_name(".task-meta-real.json")
    meta_path.rename(meta_real)
    meta_path.symlink_to(meta_real)
    meta_leaf_rejected = project_root(
        store_root, reaped_root, observed_at=1_800_000_000.0
    ).programs[0].timing.mode == "unknown"
    meta_path.unlink()
    meta_real.rename(meta_path)
    worktree_real = reaped_worktree.with_name("reaped-worktree-real")
    reaped_worktree.rename(worktree_real)
    reaped_worktree.symlink_to(worktree_real, target_is_directory=True)
    session_path.write_text(
        json.dumps({**valid_session, "cwd": str(reaped_worktree)}) + "\n",
        encoding="utf-8",
    )
    meta_ancestor_rejected = project_root(
        store_root, reaped_root, observed_at=1_800_000_000.0
    ).programs[0].timing.mode == "unknown"
    reaped_worktree.unlink()
    worktree_real.rename(reaped_worktree)
    session_path.write_text(
        json.dumps(valid_session, sort_keys=True) + "\n", encoding="utf-8"
    )
    regression_check(
        "task timing rejects the complete malformed identity timestamp and symlink matrix",
        rejected_bound_task == list(bound_task_cases)
        and meta_leaf_rejected
        and meta_ancestor_rejected,
    )

    verify_runtime = (
        store_root / "owners" / timed_root / "runtime" / timed_root
    )
    receipt_path = (
        verify_runtime
        / "pipeline-verification"
        / receipt_operation
        / "receipt.json"
    )
    valid_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    def verification_duration_unknown(payload: dict[str, object]) -> bool:
        receipt_path.write_text(
            json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
        )
        projected = project_root(
            store_root, timed_root, observed_at=1_800_000_000.0
        )
        step = next(
            item for item in projected.programs[0].steps if item.step_id == "verify"
        )
        return step.timing.mode == "unknown"

    evidence_row = valid_receipt["evidence"][0]
    verification_time_cases = {
        "malformed start": {
            **valid_receipt,
            "evidence": [{**evidence_row, "started_at": "not-rfc3339"}],
        },
        "malformed numeric start": {
            **valid_receipt,
            "evidence": [
                {**evidence_row, "started_at": "1700000000.25oops"}
            ],
        },
        "non-finite numeric start": {
            **valid_receipt,
            "evidence": [{**evidence_row, "started_at": "nan"}],
        },
        "infinite numeric finish": {
            **valid_receipt,
            "evidence": [{**evidence_row, "finished_at": "inf"}],
        },
        "negative numeric start": {
            **valid_receipt,
            "evidence": [{**evidence_row, "started_at": "-1.0"}],
        },
        "reversed interval": {
            **valid_receipt,
            "evidence": [
                {
                    **evidence_row,
                    "started_at": evidence_row["finished_at"],
                    "finished_at": evidence_row["started_at"],
                }
            ],
        },
        "reversed numeric interval": {
            **valid_receipt,
            "evidence": [
                {
                    **evidence_row,
                    "started_at": "1700000002.75",
                    "finished_at": "1700000000.25",
                }
            ],
        },
        "future finish": {
            **valid_receipt,
            "evidence": [{**evidence_row, "finished_at": "2099-01-01T00:00:00Z"}],
        },
        "future numeric finish": {
            **valid_receipt,
            "evidence": [
                {**evidence_row, "finished_at": "1800000001.0"}
            ],
        },
    }
    rejected_verification = [
        label
        for label, payload in verification_time_cases.items()
        if verification_duration_unknown(payload)
    ]
    receipt_path.write_text(
        json.dumps(valid_receipt, sort_keys=True) + "\n", encoding="utf-8"
    )
    receipt_directory = receipt_path.parent
    receipt_real = verify_runtime / ".verification-receipt-real"
    receipt_directory.rename(receipt_real)
    receipt_directory.symlink_to(receipt_real, target_is_directory=True)
    projected = project_root(
        store_root, timed_root, observed_at=1_800_000_000.0
    )
    verification_ancestor_mode = next(
        item for item in projected.programs[0].steps if item.step_id == "verify"
    ).timing.mode
    verification_ancestor_rejected = verification_ancestor_mode != "duration"
    receipt_directory.unlink()
    receipt_real.rename(receipt_directory)
    regression_check(
        "verification timing rejects malformed reversed future and ancestor symlink evidence",
        rejected_verification == list(verification_time_cases)
        and verification_ancestor_rejected,
    )

    cli_baseline = _tree_bytes(vault)
    live_frames: list[str] = []
    live_clock_values = iter((1_300.0, 1_360.0))
    live_clock_samples: list[float] = []

    def root_frame_clock() -> float:
        value = next(live_clock_values)
        live_clock_samples.append(value)
        return value

    def stop_root_frames(_interval: float) -> None:
        if len(live_frames) == 2:
            raise KeyboardInterrupt

    live_code = dashboard_script.main(
        [
            "--store",
            str(store_root),
            "--root",
            timed_root,
            "--interval",
            "0.1",
            "--no-color",
        ],
        output=live_frames.append,
        sleeper=stop_root_frames,
        tty_probe=lambda: True,
        terminal_rows=lambda: 40,
        clock=root_frame_clock,
    )

    def terminal_once(clock_value: float) -> str:
        output: list[str] = []
        code = dashboard_script.main(
            [
                "--store",
                str(store_root),
                "--root",
                reaped_root,
                "--once",
                "--no-color",
            ],
            output=output.append,
            tty_probe=lambda: True,
            terminal_rows=lambda: (_ for _ in ()).throw(
                AssertionError("--once must not inspect terminal rows")
            ),
            clock=lambda: clock_value,
        )
        return output[0] if code == 0 and len(output) == 1 else ""

    terminal_first = terminal_once(1_800_000_000.0)
    terminal_same = terminal_once(1_800_000_000.0)
    terminal_later = terminal_once(1_800_000_060.0)
    plain_live_frames = [
        frame.removeprefix(dashboard_script.CLEAR) for frame in live_frames
    ]
    regression_check(
        "root CLI samples one clock per frame while active time advances and terminal duration freezes",
        live_code == 0
        and live_clock_samples == [1_300.0, 1_360.0]
        and len(plain_live_frames) == 2
        and "updated 00:21:40" in plain_live_frames[0]
        and "updated 00:22:40" in plain_live_frames[1]
        and "elapsed 5m 00s" in plain_live_frames[0]
        and "elapsed 6m 00s" in plain_live_frames[1]
        and review_root not in "".join(plain_live_frames)
        and terminal_first == terminal_same
        and "duration 5m 00s" in terminal_first
        and "duration 5m 00s" in terminal_later
        and ansi.search("".join(plain_live_frames) + terminal_first) is None
        and _tree_bytes(vault) == cli_baseline,
    )

with tempfile.TemporaryDirectory(prefix="harness-dashboard-normalized-path.") as raw:
    # A raw '..' component is collapsed lexically before any component is
    # stat'ed, so the guard must fail closed on it: the operating system
    # resolves the symlink first and reaches a different directory.
    base = Path(raw).resolve()
    (base / "nest" / "other").mkdir(parents=True)
    (base / "nest" / "vault").mkdir()
    (base / "vault").mkdir()
    (base / "alias").symlink_to(base / "nest" / "other", target_is_directory=True)
    _guard = dashboard_receipts.absolute_path_is_safe
    regression_check(
        "the path guard fails closed on a raw absolute path whose '..' erases a symlink",
        _guard(base / "alias" / ".." / "vault") is False
        and _guard(base / "alias" / ".." / "nest" / "vault") is False
        and _guard(base / "alias" / "vault") is False
        and _guard(base / "vault") is True
        and _guard(base / "vault" / "absent.json") is True
        and _guard(base / "nest" / "vault") is True,
    )

    normalized_root = "dashboard-normalized-store-root"
    store_actual = base / "sdeep" / "svault" / ".vault-meta" / "harness"
    store_actual.mkdir(parents=True)
    (base / "svault" / ".vault-meta" / "harness").mkdir(parents=True)
    (base / "sdeep" / "sother").mkdir(parents=True)
    (base / "salias").symlink_to(base / "sdeep" / "sother", target_is_directory=True)
    _create(
        OperationStore(store_actual),
        normalized_root,
        "dispatch",
        lane_id="normalized-store-lane",
        owner=normalized_root,
    )
    normalized_store = base / "salias" / ".." / "svault" / ".vault-meta" / "harness"
    normalized_root_rejected = False
    try:
        project_root(normalized_store, normalized_root, observed_at=1_800_000_000.0)
    except ValueError:
        normalized_root_rejected = True
    normalized_all_rejected = False
    try:
        dashboard_script.snapshot(
            normalized_store,
            recent=3,
            inventory=None,
            observed_at=1_800_000_000.0,
        )
    except ValueError:
        normalized_all_rejected = True
    ordinary_store_projection = project_root(
        store_actual, normalized_root, observed_at=1_800_000_000.0
    )
    regression_check(
        "projection rejects a caller store whose traversed symlink was erased by '..'",
        normalized_root_rejected
        and normalized_all_rejected
        and [
            program.operation_id for program in ordinary_store_projection.programs
        ]
        == [normalized_root],
    )

    cwd_root = "dashboard-normalized-cwd-root"
    cwd_vault = base / "cvault"
    cwd_store_root = cwd_vault / ".vault-meta" / "harness"
    cwd_store_root.mkdir(parents=True)
    cwd_store = OperationStore(cwd_store_root)
    _create(
        cwd_store,
        cwd_root,
        "dispatch",
        lane_id="normalized-cwd-lane",
        owner=cwd_root,
    )
    cwd_runtime = cwd_store_root / "owners" / cwd_root / "runtime" / cwd_root
    cwd_runtime.mkdir(parents=True)
    (base / "cdeep" / "cother").mkdir(parents=True)
    (base / "calias").symlink_to(base / "cdeep" / "cother", target_is_directory=True)
    traversed_worktree = base / "cdeep" / "wt"
    traversed_worktree.mkdir()
    ordinary_worktree = base / "cwt"
    ordinary_worktree.mkdir()

    def _write_bound_cwd(cwd_value: Path, worktree: Path) -> None:
        (cwd_runtime / "session.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation_id": cwd_root,
                    "run_id": f"{cwd_root}-run",
                    "cwd": str(cwd_value),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (worktree / ".task-meta.json").write_text(
            json.dumps(
                {
                    "version": 4,
                    "task_id": cwd_root,
                    "task_name": "normalized cwd task",
                    "worktree": str(worktree),
                    "vault_root": str(cwd_vault),
                    "plan_file": str(cwd_vault / "wiki" / "plans" / "plan.md"),
                    "spawned_at": "2027-01-15T07:00:00Z",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    _write_bound_cwd(base / "calias" / ".." / "wt", traversed_worktree)
    normalized_cwd_projection = project_root(
        cwd_store_root, cwd_root, observed_at=1_800_000_000.0
    )
    _write_bound_cwd(ordinary_worktree, ordinary_worktree)
    ordinary_cwd_projection = project_root(
        cwd_store_root, cwd_root, observed_at=1_800_000_000.0
    )
    regression_check(
        "task display evidence rejects a bound session cwd whose '..' erases a symlink",
        normalized_cwd_projection.programs[0].task_name == "unknown"
        and normalized_cwd_projection.programs[0].timing.mode == "unknown"
        and ordinary_cwd_projection.programs[0].task_name == "normalized cwd task"
        and ordinary_cwd_projection.programs[0].timing == TimingView("elapsed", 3_600),
    )

with tempfile.TemporaryDirectory(prefix="harness-dashboard-meta-snapshot.") as raw:
    # One frame must observe exactly one task-metadata revision: the parsed
    # mapping and its SHA-256 come from the same bytes, or the fact is unknown.
    base = Path(raw).resolve()
    vault = base / "vault"
    store_root = vault / ".vault-meta" / "harness"
    store_root.mkdir(parents=True)
    store = OperationStore(store_root)
    snapshot_root = "dashboard-meta-snapshot-root"
    _create(
        store,
        snapshot_root,
        "dispatch",
        lane_id="meta-snapshot-lane",
        owner=snapshot_root,
    )
    _advance(
        store,
        snapshot_root,
        "preflight",
        "starting",
        "running",
        "finalizing",
        "exiting",
        "complete",
        owner=snapshot_root,
    )
    snapshot_record = store.read(snapshot_root, snapshot_root)
    snapshot_runtime = (
        store_root / "owners" / snapshot_root / "runtime" / snapshot_root
    )
    snapshot_runtime.mkdir(parents=True)
    snapshot_worktree = base / "wt"
    snapshot_worktree.mkdir()
    (snapshot_runtime / "session.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": snapshot_root,
                "run_id": f"{snapshot_root}-run",
                "cwd": str(snapshot_worktree),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    def _snapshot_meta(spawned_at: str) -> str:
        return (
            json.dumps(
                {
                    "version": 4,
                    "task_id": snapshot_root,
                    "task_name": "meta snapshot task",
                    "worktree": str(snapshot_worktree),
                    "vault_root": str(vault),
                    "plan_file": str(vault / "wiki" / "plans" / "plan.md"),
                    "spawned_at": spawned_at,
                },
                sort_keys=True,
            )
            + "\n"
        )

    first_meta = _snapshot_meta("2027-01-15T00:00:00Z")
    second_meta = _snapshot_meta("2027-01-15T07:00:00Z")
    first_meta_sha = hashlib.sha256(first_meta.encode("utf-8")).hexdigest()
    second_meta_sha = hashlib.sha256(second_meta.encode("utf-8")).hexdigest()
    snapshot_meta_path = snapshot_worktree / ".task-meta.json"
    snapshot_meta_path.write_text(first_meta, encoding="utf-8")
    (snapshot_worktree / ".task-reap-complete.json").write_text(
        json.dumps(
            {
                "validated": True,
                "completed_at": "2027-01-15T08:00:00Z",
                "meta_sha256": second_meta_sha,
                "task_name": "meta snapshot task",
                "vault_root": str(vault),
                "plan_path": str(vault / "wiki" / "plans" / "plan.md"),
                "task_session_status": "archived",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    original_io_open = io.open
    original_os_open = os.open
    meta_opens: list[str] = []

    def _replace_meta() -> None:
        pending = snapshot_meta_path.parent / ".task-meta.json.next"
        pending.write_text(second_meta, encoding="utf-8")
        os.replace(pending, snapshot_meta_path)

    def _observe_meta_open(target: object) -> None:
        if not isinstance(target, int) and str(target).endswith(".task-meta.json"):
            meta_opens.append(str(target))
            if len(meta_opens) == 1:
                _replace_meta()

    def _racing_io_open(*args: object, **kwargs: object) -> object:
        handle = original_io_open(*args, **kwargs)
        _observe_meta_open(args[0])
        return handle

    def _racing_os_open(*args: object, **kwargs: object) -> int:
        descriptor = original_os_open(*args, **kwargs)
        _observe_meta_open(args[0])
        return descriptor

    def _race_one_frame(call):
        io.open = _racing_io_open
        os.open = _racing_os_open
        try:
            return call()
        finally:
            io.open = original_io_open
            os.open = original_os_open

    bound_snapshot = _race_one_frame(
        lambda: dashboard_receipts._bound_task(store, snapshot_record)
    )
    bound_opens = list(meta_opens)
    meta_opens.clear()
    snapshot_meta_path.write_text(first_meta, encoding="utf-8")
    racing_timing = _race_one_frame(
        lambda: dashboard_receipts.root_timing(
            store, snapshot_record, 1_800_000_000.0
        )
    )
    timing_opens = list(meta_opens)
    regression_check(
        "task metadata mapping and digest are derived from one coherent read",
        len(bound_opens) == 1
        and bound_snapshot is not None
        and (str(bound_snapshot[1].get("spawned_at")), bound_snapshot[2])
        in {
            ("2027-01-15T00:00:00Z", first_meta_sha),
            ("2027-01-15T07:00:00Z", second_meta_sha),
        },
    )
    regression_check(
        "atomically replaced task metadata yields one coherent revision or unknown",
        len(timing_opens) == 1
        and (
            racing_timing.mode == "unknown"
            or racing_timing == TimingView("duration", 3_600)
        ),
    )

    snapshot_meta_path.write_text(first_meta, encoding="utf-8")
    coherent_timing = dashboard_receipts.root_timing(
        store, snapshot_record, 1_800_000_000.0
    )
    (snapshot_worktree / ".task-reap-complete.json").write_text(
        json.dumps(
            {
                "validated": True,
                "completed_at": "2027-01-15T08:00:00Z",
                "meta_sha256": first_meta_sha,
                "task_name": "meta snapshot task",
                "vault_root": str(vault),
                "plan_path": str(vault / "wiki" / "plans" / "plan.md"),
                "task_session_status": "archived",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    matched_timing = dashboard_receipts.root_timing(
        store, snapshot_record, 1_800_000_000.0
    )
    real_meta = snapshot_worktree / ".task-meta.real.json"
    real_meta.write_text(first_meta, encoding="utf-8")
    snapshot_meta_path.unlink()
    snapshot_meta_path.symlink_to(real_meta)
    leaf_symlink_bound = dashboard_receipts._bound_task(store, snapshot_record)
    snapshot_meta_path.unlink()
    fifo_rejected = True
    try:
        os.mkfifo(snapshot_meta_path)
    except (AttributeError, NotImplementedError, OSError):
        fifo_rejected = True
    else:
        fifo_rejected = (
            dashboard_receipts._bound_task(store, snapshot_record) is None
        )
        snapshot_meta_path.unlink()
    regression_check(
        "one coherent task metadata read keeps digest binding and actual-file rejection",
        coherent_timing.mode == "unknown"
        and matched_timing == TimingView("duration", 28_800)
        and leaf_symlink_bound is None
        and fifo_rejected,
    )

# The current semantic role is projected state, never a rendered prefix.
IDENTITY_BOLD = "\x1b[1m"
IDENTITY_PRIMARY = "\x1b[38;2;244;244;247m"
IDENTITY_DIM = "\x1b[2m"
IDENTITY_SECONDARY = "\x1b[38;2;119;122;140m"
IDENTITY_WIDTHS = (80, 100, 120)
IDENTITY_EXECUTOR = RouteView("claude", "claude-opus-5", "medium", "executor")


def _identity_steps(first_status: str) -> tuple[StepView, ...]:
    return (
        StepView(
            "reproduce",
            "engineering-fix@1",
            "persistent",
            first_status,
            0,
            route=IDENTITY_EXECUTOR,
            timing=(
                TimingView("elapsed", 42)
                if first_status == "running"
                else TimingView()
            ),
        ),
        StepView("regression-test", "engineering-fix@1", "persistent", "pending", 0),
        StepView("minimal-fix", "engineering-fix@1", "persistent", "pending", 0),
    )


def _identity_projection(
    state: str, classification: str, first_status: str
) -> DashboardProjection:
    program = ProgramView(
        "9d2b6c71-2222-4b11-8f11-1a1a1a1a1a1a",
        "dispatch",
        state,
        7,
        "engineering-fix",
        "c" * 64,
        (),
        _identity_steps(first_status),
        (),
        "wait",
        0,
        2,
        "workspace",
        classification,
        executor=IDENTITY_EXECUTOR,
        executor_status="awaiting-callback",
        timing=TimingView("elapsed", 900),
        task_name="rc4 dashboard repair",
    )
    return DashboardProjection(
        program.operation_id,
        classification,
        "observed",
        (program,),
        (),
        {},
        observed_at=1_800_000_000.0,
    )


def _identity_frames(
    projection: DashboardProjection, columns: int
) -> tuple[list[str], list[str], bool]:
    colored = render(projection, scope="root", color=True, columns=columns)
    plain = render(projection, scope="root", color=False, columns=columns)
    return (
        colored.splitlines(),
        plain.splitlines(),
        ansi.sub("", colored) == plain,
    )


def _identity_row(colored: list[str], plain: list[str], needle: str) -> str:
    return next(
        color_line
        for color_line, plain_line in zip(colored, plain)
        if needle in plain_line
    )


waiting_identity = _identity_projection("awaiting-callback", WAITING, "running")
active_identity = _identity_projection("running", ACTIVE, "running")
pending_identity = _identity_projection("running", ACTIVE, "pending")
regression_check(
    "every nonterminal current root identity including WAITING renders primary and bold",
    all(
        equivalent
        and _identity_row(colored, plain, "WAITING").startswith(
            IDENTITY_BOLD + IDENTITY_PRIMARY
        )
        and any(line.startswith("○ WAITING rc4 dashboard repair") for line in plain)
        for colored, plain, equivalent in (
            _identity_frames(waiting_identity, columns)
            for columns in IDENTITY_WIDTHS
        )
    )
    and all(
        equivalent
        and _identity_row(colored, plain, "ACTIVE").startswith(
            IDENTITY_BOLD + IDENTITY_PRIMARY
        )
        for colored, plain, equivalent in (
            _identity_frames(active_identity, columns)
            for columns in IDENTITY_WIDTHS
        )
    ),
)
regression_check(
    "the selected pending pre-start step is primary and bold while later rows stay dim",
    all(
        equivalent
        and _identity_row(colored, plain, "Reproduce").startswith(
            IDENTITY_BOLD + IDENTITY_PRIMARY
        )
        and _identity_row(colored, plain, "Regression test").startswith(
            IDENTITY_DIM + IDENTITY_SECONDARY
        )
        and _identity_row(colored, plain, "Minimal fix").startswith(
            IDENTITY_DIM + IDENTITY_SECONDARY
        )
        and "  ├─ ○ Reproduce  pending  time unavailable" in plain
        and "  ├─ ○ Regression test  pending" in plain
        for colored, plain, equivalent in (
            _identity_frames(pending_identity, columns)
            for columns in IDENTITY_WIDTHS
        )
    )
    and all(
        _identity_row(colored, plain, "Reproduce").startswith(
            IDENTITY_BOLD + IDENTITY_PRIMARY
        )
        and _identity_row(colored, plain, "Regression test").startswith(
            IDENTITY_DIM + IDENTITY_SECONDARY
        )
        for colored, plain, equivalent in (
            _identity_frames(waiting_identity, columns)
            for columns in IDENTITY_WIDTHS
        )
    ),
)
identity_height = len(
    render(waiting_identity, scope="root", color=False, columns=100).splitlines()
)
regression_check(
    "root identity outranks history for every nonterminal root under row pressure",
    all(
        (
            not any(
                line.lstrip().startswith("● ACTIVE")
                for line in render(
                    active_identity, scope="root", rows=rows, columns=100
                ).splitlines()
            )
            or any(
                line.lstrip().startswith("○ WAITING")
                for line in render(
                    waiting_identity, scope="root", rows=rows, columns=100
                ).splitlines()
            )
        )
        for rows in range(1, identity_height + 1)
    ),
)

if REGRESSION_FAILURES:
    raise AssertionError(
        "dashboard regressions failed: " + "; ".join(REGRESSION_FAILURES)
    )

print("harness dashboard tests passed")
