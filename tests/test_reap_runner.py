#!/usr/bin/env python3
"""Pure regression checks for deterministic reap routing and page rendering."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("reap_runner", ROOT / "scripts/reap-runner.py")
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
from harness.contracts import OwnedResources, RuntimeRoute
from harness.callbacks import CallbackBroker
from harness.store import OperationStore
from harness.supervisor import OperationSupervisor
from harness.workflows.dispatch import DispatchRequest, run_dispatch
from harness.workflows.reap import summary_callback

failures: list[str] = []


def check(name: str, value: bool) -> None:
    print(("ok" if value else "not ok") + " - " + name)
    if not value:
        failures.append(name)


with tempfile.TemporaryDirectory(prefix="reap-runner-test.") as raw:
    vault = Path(raw)
    (vault / "wiki/meta/sessions").mkdir(parents=True)
    summary = {
        "schema_version": 1,
        "type": "session",
        "title": "Typed Result",
        "session": "executor-session",
        "body": "Implemented [[Dispatch safety]].\nCross-model review: passed",
    }
    path = runner.proposed_path(vault, summary)
    check("session route uses title filename", path == (vault / "wiki/meta/sessions/Typed Result.md").resolve())
    original_run = runner.run
    runner.run = lambda *_args, **_kwargs: "c-000123\n"
    try:
        page = runner.frontmatter_page(
            vault,
            {
                "origin_session": "origin-session",
                "executor_runtime": "codex",
                "routing": {"effective": {"model": "gpt-5.6-sol"}},
                "suggested_agents": [],
            },
            summary,
            "reap-session",
        )
    finally:
        runner.run = original_run
    check("page preserves provenance chain", all(item in page for item in ("origin-session", "executor-session", "reap-session")))
    check("page records effective model", 'executor_model: "gpt-5.6-sol"' in page)
    check("page address is reusable by log and hot payload", runner.page_address(page) == "c-000123")
    check("page derives bounded related links", '"[[Dispatch safety]]"' in page)
    concepts = vault / "wiki/concepts"
    concepts.mkdir()
    (concepts / "Dispatch safety.md").write_text("# Dispatch safety\n", encoding="utf-8")
    try:
        runner.validate_summary_wikilinks(vault, summary)
    except runner.ReapError:
        check("existing summary wikilink passes before mutation", False)
    else:
        check("existing summary wikilink passes before mutation", True)
    try:
        runner.validate_summary_wikilinks(
            vault, {**summary, "body": "Invented [[Display title without alias]]."}
        )
    except runner.ReapError as exc:
        check(
            "unresolved summary wikilink fails before mutation",
            "[[Display title without alias]]" in str(exc),
        )
    else:
        check("unresolved summary wikilink fails before mutation", False)
    existing = vault / "wiki/meta/sessions/existing.md"
    existing.write_text("---\nupdated: 2026-01-01\n---\n# Existing\n", encoding="utf-8")
    updated, expected = runner.update_page(existing, summary, "task-one")
    check("update uses optimistic old hash", len(expected) == 64)
    check("update appends dated task section", "task-one" in updated and summary["body"] in updated)
    bad = dict(summary, title="../escape")
    try:
        runner.proposed_path(vault, bad)
    except runner.ReapError:
        check("unsafe title fails closed", True)
    else:
        check("unsafe title fails closed", False)
    plan = vault / "wiki/plans/approved.md"
    plan.parent.mkdir(parents=True)
    pending = "---\nstatus: pending\n---\n"
    plan.write_text(pending, encoding="utf-8")
    import hashlib
    meta = {"plan_file": str(plan), "approved_plan_sha256": hashlib.sha256(pending.encode()).hexdigest()}
    check("pending plan hash validates", runner.approved_plan_state(meta)[1] == "pending")
    check(
        "shared task reap retains its approved master plan",
        not runner.reap_closes_plan(
            {"reap_policy": {"mode": "shared"}}
        ),
    )
    check(
        "final task reap closes its approved plan",
        runner.reap_closes_plan(
            {"reap_policy": {"mode": "final"}}
        ),
    )
    shared_payload = runner.with_plan_close(
        {"pages": []},
        {
            **meta,
            "reap_policy": {"mode": "shared"},
        },
        vault=vault,
        plan=plan,
        result_link="[[Shared result]]",
        exec_session="executor-session",
    )
    final_payload = runner.with_plan_close(
        {"pages": []},
        {
            **meta,
            "reap_policy": {"mode": "final"},
        },
        vault=vault,
        plan=plan,
        result_link="[[Final result]]",
        exec_session="executor-session",
    )
    check(
        "shared reap vault transaction omits plan_close",
        "plan_close" not in shared_payload,
    )
    check(
        "final reap vault transaction binds exact plan_close",
        final_payload["plan_close"]["file"] == "wiki/plans/approved.md"
        and final_payload["plan_close"]["expected_sha256"]
        == meta["approved_plan_sha256"],
    )
    plan.write_text("---\nstatus: executed\n---\n", encoding="utf-8")
    check("executed plan is accepted only as recovery", runner.approved_plan_state(meta)[1] == "executed")
    try:
        runner.page_address("---\ntype: session\n---\n")
    except runner.ReapError:
        check("missing result address fails before vault write", True)
    else:
        check("missing result address fails before vault write", False)
    structured = json.dumps({"error": {"message": "exact writer validation reason"}})
    try:
        runner.run(
            [sys.executable, "-c", f"import sys; print({structured!r}); sys.exit(3)"],
            cwd=vault,
            label="writer",
        )
    except runner.ReapError as exc:
        check("structured writer error remains actionable", "exact writer validation reason" in str(exc))
    else:
        check("structured writer error remains actionable", False)

    archive_meta = {"task_id": "archive-task"}
    archive_product = vault / "archive-product"
    archive_product.mkdir()
    gate_root = vault / ".vault-meta/harness/review-data/archive-task/archive-task"
    gate_root.mkdir(parents=True)
    archive_calls: list[list[str]] = []
    original_require_task_review = runner.require_task_review
    original_review_gate_root = runner.review_gate_root
    original_archive_run = runner.run
    try:
        runner.review_gate_root = lambda *_args, **_kwargs: gate_root
        runner.require_task_review = lambda *_args, **_kwargs: SimpleNamespace(
            approved=False, skipped=True
        )
        check(
            "typed no-review skip requires zero archive marker",
            runner.archive_reviews(vault, archive_product, archive_meta) == [],
        )
        runner.require_task_review = lambda *_args, **_kwargs: SimpleNamespace(
            approved=True, skipped=False
        )

        def archive_run(argv, **_kwargs):
            archive_calls.append(list(argv))
            (gate_root / ".review-archive.json").write_text(
                '{"schema_version":1,"status":"archived"}\n',
                encoding="utf-8",
            )
            return json.dumps(
                {
                    "schema_version": 1,
                    "status": "archived",
                    "review_id": "review-1",
                }
            )

        runner.run = archive_run
        markers = runner.archive_reviews(vault, archive_product, archive_meta)
        check(
            "approved review archives only its exact derived gate root",
            markers == [str((gate_root / ".review-archive.json").resolve())]
            and len(archive_calls) == 1
            and "scripts/harness/review_archive.py"
            in " ".join(archive_calls[0])
            and str(gate_root) in archive_calls[0]
            and "archive_task_reviews.py" not in " ".join(archive_calls[0]),
        )
    finally:
        runner.require_task_review = original_require_task_review
        runner.review_gate_root = original_review_gate_root
        runner.run = original_archive_run

    task_id = "11111111-1111-4111-8111-111111111111"
    worktree = vault / "task-worktree"
    worktree.mkdir()
    (worktree / ".task-meta.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "version": 3,
                "interaction_policy": "unattended",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (worktree / ".task-summary.json").write_text(
        json.dumps(summary) + "\n",
        encoding="utf-8",
    )
    route = RuntimeRoute(
        "codex", "gpt-5.6-sol", "high", "executor", "a" * 64
    )
    dispatched = run_dispatch(
        DispatchRequest(
            task_id,
            task_id,
            "b" * 64,
            "packets/task/manifest.json",
            route,
        ),
        vault / ".vault-meta/harness",
        launch=lambda _record: {"status": "launched"},
        persist_result=lambda _record, _result: None,
    )
    runtime_store = OperationStore(vault / ".vault-meta/harness")
    OperationSupervisor(runtime_store, task_id, task_id).bind_resources(
        OwnedResources(
            "22222222-2222-4222-8222-222222222222",
            4242,
            4343,
        )
    )
    runtime_actions: list[str] = []

    class FakeRuntime:
        def request_exit(self, owner_id: str, operation_id: str):
            runtime_actions.append("request-exit")
            supervisor = OperationSupervisor(
                runtime_store, owner_id, operation_id
            )
            record = supervisor.transition("exiting")
            return SimpleNamespace(action="exit-requested", record=record)

        def cleanup(self, owner_id: str, operation_id: str):
            runtime_actions.append("cleanup")
            supervisor = OperationSupervisor(
                runtime_store, owner_id, operation_id
            )
            supervisor.bind_resources(OwnedResources())
            record = supervisor.transition("complete")
            return SimpleNamespace(action="cleaned", record=record)

    fake_runtime = FakeRuntime()
    original_finalize = runner._finalize_reap
    original_handoff = runner.validate_handoff
    original_links = runner.validate_summary_wikilinks
    original_plan_state = runner.approved_plan_state
    original_review = runner.authorize_review
    review_actions: list[str] = []
    runner._finalize_reap = lambda _vault, _worktree, _current: {
        "schema_version": 1,
        "status": "complete",
        "result_path": str(vault / "wiki/meta/sessions/Typed Result.md"),
        "result_link": "[[Typed Result]]",
        "duration_ms": 1,
    }
    runner.validate_summary_wikilinks = lambda *_args, **_kwargs: None
    runner.approved_plan_state = lambda _meta: (
        vault / "wiki/plans/approved.md",
        "pending",
    )
    runner.authorize_review = (
        lambda *_args, **_kwargs: review_actions.append("authorized")
    )
    try:
        def reject_handoff(*_args, **_kwargs):
            raise runner.ContractError("wrong task session")

        runner.validate_handoff = reject_handoff
        try:
            runner.apply_reap(vault, worktree, "wrong-session")
        except runner.ReapError:
            before_callback = OperationStore(
                vault / ".vault-meta/harness"
            ).read(task_id, task_id)
            check(
                "invalid reap preflight cannot consume the Wiki Summary callback",
                before_callback.state == "awaiting-callback"
                and not before_callback.accepted_callback_id,
            )
        else:
            check(
                "invalid reap preflight cannot consume the Wiki Summary callback",
                False,
            )
        runner.validate_handoff = lambda *_args, **_kwargs: None
        runner.authorize_review = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            runner.ReapError("review pending")
        )
        try:
            runner.apply_reap(vault, worktree, "reap-session")
        except runner.ReapError:
            review_blocked = OperationStore(
                vault / ".vault-meta/harness"
            ).read(task_id, task_id)
            check(
                "direct reap review rejection precedes callback and vault mutation",
                review_blocked.state == "awaiting-callback"
                and not review_blocked.accepted_callback_id,
            )
        else:
            check(
                "direct reap review rejection precedes callback and vault mutation",
                False,
            )
        runner.authorize_review = (
            lambda *_args, **_kwargs: review_actions.append("authorized")
        )
        CallbackBroker(runtime_store, task_id).accept(
            summary_callback(
                callback_id="wiki-summary-runtime-worker",
                operation_id=task_id,
                run_id=dispatched.record.run_id,
                summary=summary,
            )
        )
        reaped = runner.apply_reap(
            vault,
            worktree,
            "reap-session",
            runtime_manager=fake_runtime,
        )
    finally:
        runner._finalize_reap = original_finalize
        runner.validate_handoff = original_handoff
        runner.validate_summary_wikilinks = original_links
        runner.approved_plan_state = original_plan_state
        runner.authorize_review = original_review
    durable = OperationStore(vault / ".vault-meta/harness").read(
        task_id, task_id
    )
    check(
        "public reap runner accepts callback and completes the dispatch operation",
        reaped["status"] == "complete"
        and dispatched.record.state == "awaiting-callback"
        and durable.state == "complete"
        and durable.accepted_callback_kind == "wiki-summary"
        and durable.accepted_callback_id == "wiki-summary-runtime-worker",
    )
    check(
        "reap authorizes the exact review gate before consuming the callback",
        review_actions == ["authorized"],
    )
    check(
        "reap exits provider before exact cleanup and clears ownership",
        runtime_actions == ["request-exit", "cleanup"]
        and durable.resources == OwnedResources(),
    )

if failures:
    raise SystemExit(f"{len(failures)} reap runner test(s) failed")
print("All reap runner tests passed.")
