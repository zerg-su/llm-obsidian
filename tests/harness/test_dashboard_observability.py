#!/usr/bin/env python3
"""Content-free repair and terminal task-result dashboard projection."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.contracts import OperationSpec, RuntimeRoute  # noqa: E402
from harness.dashboard_projection import project_root  # noqa: E402
from harness.dashboard_view import render  # noqa: E402
from harness.store import OperationStore  # noqa: E402


failures: list[str] = []


def check(label: str, condition: bool) -> None:
    if condition:
        print(f"OK   {label}")
    else:
        print(f"FAIL {label}")
        failures.append(label)


with tempfile.TemporaryDirectory(prefix="dashboard-observability.") as raw:
    base = Path(raw).resolve()
    vault = base / "vault"
    store_root = vault / ".vault-meta" / "harness"
    worktree = base / "worktree"
    worktree.mkdir()
    store = OperationStore(store_root)
    root = "observable-root"
    route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "high", "executor", "a" * 64
    )
    spec = OperationSpec(
        operation_id=root,
        idempotency_key="observable-root-key",
        kind="dispatch",
        owner_id=root,
        route=route,
        context_manifest="packets/root/manifest.json",
        verification_profile="scoped",
        root_operation_id=root,
    )
    store.create(spec, lane_id="root-lane", run_id="root-run")
    for state in (
        "preflight",
        "starting",
        "running",
        "finalizing",
        "exiting",
        "complete",
    ):
        store.transition(root, root, state)
    pivot_spec = OperationSpec(
        operation_id="structural-pivot-child",
        idempotency_key="structural-pivot-child-key",
        kind="structural-pivot",
        owner_id=root,
        route=RuntimeRoute(
            "codex", "gpt-5.6-sol", "xhigh", "reviewer-callback", "f" * 64
        ),
        context_manifest="structural-pivots/root/packet.json",
        verification_profile="scoped",
        contract_sha256="e" * 64,
        parent_operation_id=root,
        root_operation_id=root,
    )
    pivot = store.create(
        pivot_spec, lane_id="pivot-lane", run_id="pivot-run"
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(root, pivot.spec.operation_id, state)
    pivot = store.read(root, pivot.spec.operation_id)
    round_spec = OperationSpec(
        operation_id="structural-pivot-round",
        idempotency_key="structural-pivot-round-key",
        kind="review-round",
        owner_id=root,
        route=pivot_spec.route,
        context_manifest=pivot_spec.context_manifest,
        verification_profile="scoped",
        contract_sha256="e" * 64,
        parent_operation_id=pivot.spec.operation_id,
        root_operation_id=root,
    )
    pivot_round = store.create(
        round_spec, lane_id="pivot-lane", run_id="pivot-round-run"
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition(root, pivot_round.spec.operation_id, state)
    liveness = (
        store_root
        / "owners"
        / root
        / "runtime"
        / pivot.spec.operation_id
        / "liveness"
    )
    liveness.mkdir(parents=True)
    (liveness / "state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "started_at": 1_799_999_940.0,
                "last_progress_at": 1_799_999_980.0,
                "operation_revision": pivot.revision,
                "operation_state": pivot.state,
                "screen_sha256": "",
                "typed_result_sha256": "",
                "callback_sha256": "",
                "receipt_sha256": "",
                "stable_result_reads": 0,
                "nudge_count": 0,
                "restart_count": 0,
                "callback_submit_binding": "",
                "callback_submit_status": "",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    runtime = store_root / "owners" / root / "runtime" / root
    runtime.mkdir(parents=True)
    (runtime / "session.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": root,
                "run_id": "root-run",
                "cwd": str(worktree),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    plan = vault / "wiki" / "plans" / "plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("plan\n", encoding="utf-8")
    meta = {
        "version": 4,
        "task_id": root,
        "task_name": "observable task",
        "worktree": str(worktree),
        "vault_root": str(vault),
        "plan_file": str(plan),
        "spawned_at": "2026-08-12T10:00:00Z",
    }
    meta_path = worktree / ".task-meta.json"
    meta_path.write_text(json.dumps(meta, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        "schema_version": 2,
        "type": "repo-touch",
        "title": "Secret final title",
        "session": "session-1",
        "body": "SECRET BODY MUST NEVER REACH DASHBOARD",
        "outcome_disposition": "partially-achieved",
        "outcome_evidence_ids": ["E267.RC5.DASHBOARD", "E267.RC5.CONTRACTS"],
        "residual_gap_pointers": ["plan.md"],
    }
    summary_path = worktree / ".task-summary.json"
    summary_path.write_text(
        json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8"
    )
    complete = {
        "version": 1,
        "task_name": meta["task_name"],
        "vault_root": str(vault),
        "plan_path": str(plan),
        "meta_sha256": hashlib.sha256(meta_path.read_bytes()).hexdigest(),
        "summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
        "validated": True,
        "completed_at": "2026-08-12T10:05:00Z",
        "task_session_status": "archived",
        "plan_close_status": "retained",
        "result_path": str(vault / "wiki" / "results" / "result.md"),
    }
    result_path = Path(complete["result_path"])
    result_path.parent.mkdir(parents=True)
    result_path.write_text("result\n", encoding="utf-8")
    (worktree / ".task-reap-complete.json").write_text(
        json.dumps(complete, sort_keys=True) + "\n", encoding="utf-8"
    )
    receipt_root = (
        runtime
        / "fresh-artifact-repair"
        / "task-summary"
        / root
    )
    receipt_root.mkdir(parents=True)
    repair_receipt = {
        "status": "self-healed",
        "family": "task-summary",
        "stage": "fresh-context",
        "repair_id": "b" * 64,
        "input_sha256": "c" * 64,
        "output_sha256": "d" * 64,
        "route_sha256": "e" * 64,
    }
    (receipt_root / "receipt.json").write_text(
        json.dumps(repair_receipt, sort_keys=True) + "\n", encoding="utf-8"
    )
    projection = project_root(store_root, root, observed_at=1_800_000_000.0)
    program = projection.programs[0]
    text = render(projection, scope="root", columns=120)
    pivot_view = next(
        child for child in program.children if child.kind == "structural-pivot"
    )
    check(
        "root projection shows pivot route, elapsed wait, callback child, and lineage",
        pivot_view.state == "awaiting-callback"
        and pivot_view.status == "running"
        and pivot_view.route.runtime == "codex"
        and pivot_view.route.model == "gpt-5.6-sol"
        and pivot_view.route.effort == "xhigh"
        and pivot_view.timing.mode == "elapsed"
        and pivot_view.timing.seconds == 60
        and len(pivot_view.children) == 1
        and pivot_view.children[0].kind == "review-round"
        and pivot_view.children[0].state == "awaiting-callback",
    )
    check(
        "dashboard projects compact self-heal count and current stage",
        program.self_healed_count == 1
        and program.current_stage == "structural-pivot"
        and "self-healed 1" in text
        and "stage structural-pivot" in text
        and "Executor codex/gpt-5.6-sol · xhigh  structural-pivot" in text,
    )
    check(
        "terminal result is exact-summary-bound and content-free",
        program.task_result.status == "complete"
        and program.task_result.disposition == "partially-achieved"
        and program.task_result.evidence_count == 2
        and program.task_result.gap_count == 1
        and program.task_result.plan_close_status == "retained"
        and "partially-achieved" in text
        and "evidence 2" in text
        and "SECRET" not in text
        and "Secret final title" not in text,
    )
    repair_receipt["output_sha256"] = "foreign"
    (receipt_root / "receipt.json").write_text(
        json.dumps(repair_receipt, sort_keys=True) + "\n", encoding="utf-8"
    )
    rejected = project_root(store_root, root, observed_at=1_800_000_000.0)
    check(
        "malformed or foreign repair evidence never increments self-healed",
        rejected.programs[0].self_healed_count == 0,
    )
    for state in ("finalizing", "exiting", "complete"):
        store.transition(root, pivot.spec.operation_id, state)
    cleaned = project_root(store_root, root, observed_at=1_800_000_000.0)
    cleaned_pivot = next(
        child
        for child in cleaned.programs[0].children
        if child.kind == "structural-pivot"
    )
    check(
        "root projection shows terminal resource-free pivot cleanup",
        cleaned_pivot.state == "complete"
        and cleaned_pivot.status == "complete",
    )

if failures:
    raise SystemExit(f"{len(failures)} dashboard observability test(s) failed")
print("All dashboard observability tests passed.")
