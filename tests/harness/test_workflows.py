#!/usr/bin/env python3
"""Workflow policy and clean composition tests."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from harness.contracts import RuntimeRoute
from harness.pipeline_builtins import builtin_definitions, builtin_registry
from harness.pipelines import compile_pipeline
from harness.store import OperationStore
from harness.supervisor import OperationSupervisor, SupervisorError
from harness.workflows.dispatch import (
    DispatchRequest,
    ReviewPolicy,
    operation_spec,
    run_dispatch,
)
from harness.workflows.reap import run_reap, summary_callback
from harness.workflows.review import (
    ReviewFinding,
    ReviewRequest,
    ReviewResult,
    aggregate,
    namespace_review_result,
    resolution_required,
    verify_lane,
)
from harness.workflows.research import ResearchRequest


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


route = RuntimeRoute("codex", "gpt-5.6-sol", "high", "executor", "a" * 64)
request = DispatchRequest("task-1", "owner-1", "b" * 64, "packets/one/manifest.json", route)
spec = operation_spec(request)
lifecycle = compile_pipeline(
    builtin_definitions()["lifecycle/default"],
    builtin_registry(),
    capabilities=("route:resolved",),
)
check(
    "dispatch binds the executable compiled lifecycle",
    spec.kind == "dispatch"
    and len(spec.idempotency_key) == 64
    and spec.contract_sha256 == lifecycle.definition_sha256,
)
check("dispatch defaults to automatic simple review", request.review == ReviewPolicy())
change_request = DispatchRequest(
    "task-change",
    "owner-1",
    "b" * 64,
    "packets/change/manifest.json",
    route,
    pipeline_name="engineering/change",
)
change = compile_pipeline(
    builtin_definitions()["engineering/change"],
    builtin_registry(),
    capabilities=("route:resolved",),
)
change_spec = operation_spec(change_request)
check(
    "engineering change reuses the dispatch identity and binds its exact contract",
    change_spec.operation_id == change_request.task_id
    and change_spec.kind == "dispatch"
    and change_spec.contract_sha256 == change.definition_sha256
    and change_spec.idempotency_key != spec.idempotency_key,
)
workspace = DispatchRequest("task-1", "owner-1", "b" * 64, "packets/one/manifest.json", route, placement="workspace")
check("workspace is a dispatch placement", workspace.placement == "workspace")

simple = ReviewRequest("review-1", selected_provider="openai")
deep = ReviewRequest("review-2", depth="deep", cross_model=True, max_verify_iterations=2)
check("simple review is one holistic session", simple.axes == ("openai-holistic",))
check("deep review preserves two independent axes", deep.axes == ("anthropic-holistic", "openai-holistic"))
try:
    ReviewRequest(
        "review-3",
        depth="simple",
        max_verify_iterations=2,
        selected_provider="openai",
    )
except ValueError:
    check("simple review verify is bounded", True)
else:
    check("simple review verify is bounded", False)
spec_result = ReviewResult(
    "anthropic-holistic",
    "changes-requested",
    (
        ReviewFinding(
            "F-1",
            "anthropic-holistic",
            "important",
            "contract gap",
            "tests fail",
        ),
    ),
)
standards_result = ReviewResult("openai-holistic", "approve")
aggregate_result = aggregate(
    deep,
    {"anthropic-holistic": spec_result, "openai-holistic": standards_result},
)
check("deep aggregation preserves axes and material verdict", aggregate_result["verdict"] == "changes-requested" and len(aggregate_result["axes"]) == 2)
check("important findings require same-session resolution", resolution_required(spec_result))

shared_id_results = {
    axis: namespace_review_result(
        deep,
        ReviewResult(
            axis,
            "changes-requested",
            (
                ReviewFinding(
                    "SHARED-001",
                    axis,
                    "important",
                    "independent issue",
                    "each lane owns separate evidence",
                ),
            ),
        ),
    )
    for axis in deep.axes
}
shared_id_aggregate = aggregate(deep, shared_id_results)
shared_ids = [
    finding["finding_id"]
    for axis in shared_id_aggregate["axes"]
    for finding in axis["findings"]
]
check(
    "multi-lane resolution identities are axis-qualified before aggregation",
    len(shared_ids) == len(set(shared_ids))
    and shared_ids
    == [f"{axis}:SHARED-001" for axis in deep.axes],
)


def check_aggregate_finding_rejected(label: str, **changes: object) -> None:
    values: dict[str, object] = {
        "finding_id": "F-canonical",
        "axis": "openai-holistic",
        "severity": "important",
        "summary": "canonical issue",
        "evidence": "the failing path is reachable",
        "file": "scripts/example.py",
        "line": 1,
        "recommendation": "fix the reachable path",
    }
    values.update(changes)
    finding = ReviewFinding(**values)  # type: ignore[arg-type]
    result = ReviewResult("openai-holistic", "changes-requested", (finding,))
    try:
        aggregate(
            ReviewRequest("finding-validation", selected_provider="openai"),
            {"openai-holistic": result},
        )
    except ValueError:
        check(label, True)
    else:
        check(label, False)


check_aggregate_finding_rejected(
    "terminal aggregation rejects a canonically invalid finding",
    finding_id="not a bounded id",
)
for field in ("finding_id", "file", "summary", "evidence", "recommendation"):
    check_aggregate_finding_rejected(
        f"terminal aggregation rejects whitespace-only finding {field}",
        **{field: " \t "},
    )
for field, value in (
    ("finding_id", "F" + "x" * 100),
    ("file", "x" * 1001),
    ("summary", "x" * 301),
    ("evidence", "x" * 4001),
    ("recommendation", "x" * 4001),
):
    check_aggregate_finding_rejected(
        f"terminal aggregation rejects oversized finding {field}",
        **{field: value},
    )
duplicate_finding = ReviewFinding(
    "F-duplicate",
    "openai-holistic",
    "important",
    "duplicate issue",
    "the same issue was emitted twice",
)
try:
    aggregate(
        ReviewRequest("finding-duplicate", selected_provider="openai"),
        {
            "openai-holistic": ReviewResult(
                "openai-holistic",
                "changes-requested",
                (duplicate_finding, duplicate_finding),
            )
        },
    )
except ValueError:
    check("terminal aggregation rejects duplicate finding ids", True)
else:
    check("terminal aggregation rejects duplicate finding ids", False)
try:
    verify_lane("surface-1", "surface-2")
except ValueError:
    check("verification cannot open a second surface", True)
else:
    check("verification cannot open a second surface", False)

safe = ResearchRequest(
    "research-1",
    "packets/research/question.md",
    "packets/research/manifest.json",
)
check("safe research is minimal-context", not safe.unsafe and safe.context_scope == "minimal")
for label, call in (
    (
        "safe research rejects full context",
        lambda: ResearchRequest(
            "r",
            "packets/research/question.md",
            "packets/research/manifest.json",
            context_scope="full-explicit",
        ),
    ),
    (
        "unsafe research is never implicit",
        lambda: ResearchRequest(
            "r",
            "packets/research/question.md",
            "packets/research/manifest.json",
            unsafe=True,
        ),
    ),
):
    try:
        call()
    except ValueError:
        check(label, True)
    else:
        check(label, False)

summary = {"type": "repo-touch", "title": "Result", "body": "Done"}
callback = summary_callback(callback_id="cb-1", operation_id="op-1", run_id="run-1", summary=summary)
check("Wiki Summary uses internal callback transport", callback.kind == "wiki-summary" and callback.payload["title"] == "Result")

with tempfile.TemporaryDirectory(prefix="harness-lifecycle.") as raw:
    store = OperationStore(Path(raw) / "store")
    lifecycle_request = DispatchRequest(
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "b" * 64,
        "packets/one/manifest.json",
        route,
    )
    dispatch_calls: list[str] = []
    persisted: list[dict[str, object]] = []

    def launch(record):
        dispatch_calls.append(record.run_id)
        return {"status": "launched", "task_surface": "surface-1"}

    dispatched = run_dispatch(
        lifecycle_request,
        store,
        launch=launch,
        persist_result=lambda record, result: persisted.append(
            {"run_id": record.run_id, **result}
        ),
    )
    durable = store.read(
        lifecycle_request.owner_id, lifecycle_request.task_id
    )
    check(
        "dispatch public seam persists and awaits one typed callback",
        dispatched.record == durable
        and durable.state == "awaiting-callback"
        and dispatch_calls == [durable.run_id]
        and persisted == [
            {
                "run_id": durable.run_id,
                "status": "launched",
                "task_surface": "surface-1",
            }
        ],
    )
    replay = run_dispatch(
        lifecycle_request,
        store,
        launch=lambda _record: (_ for _ in ()).throw(
            AssertionError("completed launch effect repeated")
        ),
        persist_result=lambda _record, _result: (_ for _ in ()).throw(
            AssertionError("completed launch result repeated")
        ),
    )
    check(
        "dispatch restart does not repeat a completed external effect",
        replay.record.state == "awaiting-callback" and replay.result is None,
    )

    failing_request = DispatchRequest(
        "33333333-3333-4333-8333-333333333333",
        "44444444-4444-4444-8444-444444444444",
        "c" * 64,
        "packets/two/manifest.json",
        route,
    )
    failed_attempts: list[str] = []

    def uncertain_launch(record):
        failed_attempts.append(record.run_id)
        raise RuntimeError("surface result was not observed")

    try:
        run_dispatch(
            failing_request,
            store,
            launch=uncertain_launch,
            persist_result=lambda _record, _result: None,
        )
    except RuntimeError:
        pass
    else:
        check("uncertain dispatch fixture fails", False)
    try:
        run_dispatch(
            failing_request,
            store,
            launch=uncertain_launch,
            persist_result=lambda _record, _result: None,
        )
    except SupervisorError:
        check(
            "uncertain dispatch effect reconciles instead of spawning again",
            len(failed_attempts) == 1,
        )
    else:
        check("uncertain dispatch effect reconciles instead of spawning again", False)

    reap_attempts: list[str] = []

    def interrupted_reap(record):
        reap_attempts.append(record.run_id)
        if len(reap_attempts) == 1:
            raise RuntimeError("vault transaction result was not observed")
        return {"status": "complete", "result_link": "[[Result]]"}

    try:
        run_reap(
            store,
            owner_id=lifecycle_request.owner_id,
            operation_id=lifecycle_request.task_id,
            summary=summary,
            finalize=interrupted_reap,
        )
    except RuntimeError:
        pass
    else:
        check("interrupted reap fixture fails", False)
    pending_reap = store.read(
        lifecycle_request.owner_id, lifecycle_request.task_id
    )
    check(
        "reap keeps an interrupted recoverable finalization durable",
        pending_reap.state == "finalizing"
        and pending_reap.pending_effect == "reap-finalize",
    )
    reaped = run_reap(
        store,
        owner_id=lifecycle_request.owner_id,
        operation_id=lifecycle_request.task_id,
        summary=summary,
        finalize=interrupted_reap,
    )
    check(
        "reap retries only its recovery-safe finalization before runtime cleanup",
        reaped.record.state == "finalizing"
        and reaped.result == {"status": "complete", "result_link": "[[Result]]"}
        and len(reap_attempts) == 2,
    )
    reap_supervisor = OperationSupervisor(
        store, lifecycle_request.owner_id, lifecycle_request.task_id
    )
    reap_supervisor.transition("exiting")
    reap_supervisor.transition("complete")
    final_replay = run_reap(
        store,
        owner_id=lifecycle_request.owner_id,
        operation_id=lifecycle_request.task_id,
        summary=summary,
        finalize=lambda _record: (_ for _ in ()).throw(
            AssertionError("terminal reap effect repeated")
        ),
    )
    check(
        "terminal reap replay is a no-op",
        final_replay.record.state == "complete" and final_replay.result is None,
    )
