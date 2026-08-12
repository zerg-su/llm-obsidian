#!/usr/bin/env python3
"""Pure regression checks for deterministic reap routing and page rendering."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import subprocess
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
from outcome_contract import extract_from_bytes
from approved_plan_snapshot import bind_approved_plan_snapshot

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
    outcome_summary = {
        "schema_version": 2,
        "type": "session",
        "title": "Typed outcome result",
        "session": "executor-session",
        "body": "The bounded portion is complete.",
        "outcome_disposition": "partially-achieved",
        "outcome_evidence_ids": ["digest-stable"],
        "residual_gap_pointers": ["[[Release verification follow-up]]"],
    }
    runner.run = lambda *_args, **_kwargs: "c-000124\n"
    try:
        outcome_page = runner.frontmatter_page(
            vault,
            {
                "origin_session": "origin-session",
                "executor_runtime": "codex",
                "routing": {"effective": {"model": "gpt-5.6-sol"}},
                "suggested_agents": [],
            },
            outcome_summary,
            "reap-session",
        )
    finally:
        runner.run = original_run
    check(
        "reap page preserves Wiki Summary v2 disposition and evidence",
        "outcome_disposition: partially-achieved" in outcome_page
        and "  - digest-stable" in outcome_page
        and '  - "[[Release verification follow-up]]"' in outcome_page,
    )
    outcome_existing = vault / "wiki/meta/sessions/outcome-existing.md"
    outcome_existing.write_text(
        "---\nupdated: 2026-01-01\n---\n# Existing\n",
        encoding="utf-8",
    )
    updated_outcome, _ = runner.update_page(
        outcome_existing, outcome_summary, "outcome-task"
    )
    check(
        "reap update archive preserves v2 outcome fields",
        "Outcome disposition: `partially-achieved`" in updated_outcome
        and "[[Release verification follow-up]]" in updated_outcome,
    )
    outcome_plan = vault / "wiki/plans/outcome.md"
    outcome_plan.parent.mkdir(parents=True, exist_ok=True)
    outcome_plan.write_text(
        "# Plan\n\n```json\n"
        '{"schema_version":1,"desired_outcome":"Ship the typed summary.",'
        '"success_evidence":[{"evidence_id":"digest-stable",'
        '"observable":"The typed summary parser accepts declared evidence."}],'
        '"non_goals":["No authority expansion."]}\n```\n',
        encoding="utf-8",
    )
    outcome_meta = vault / ".task-meta.json"
    (vault / ".vault-meta").mkdir(exist_ok=True)
    outcome_worktree = vault / "outcome-worktree"
    outcome_worktree.mkdir()
    outcome_snapshot = bind_approved_plan_snapshot(
        {"vault_root": vault, "plan_file": outcome_plan}
    )
    outcome_meta.write_text(
        json.dumps(
            {
                "version": 4,
                "plan_file": str(outcome_plan),
                "plan_snapshot_file": str(
                    outcome_snapshot["_approved_plan_file"]
                ),
                "approved_plan_sha256": outcome_snapshot[
                    "_approved_plan_sha256"
                ],
                "vault_root": str(vault),
                "worktree": str(outcome_worktree),
                "outcome_contract_sha256": extract_from_bytes(
                    outcome_plan.read_bytes()
                ).sha256,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    outcome_summary_path = vault / ".task-summary.json"
    outcome_summary_path.write_text(
        json.dumps(outcome_summary) + "\n", encoding="utf-8"
    )
    parsed_v2 = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "parse-wiki-summary.py"),
            "--json-file",
            str(outcome_summary_path),
            "--task-meta",
            str(outcome_meta),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    check(
        "archive parser reads Wiki Summary v2 with exact task evidence binding",
        parsed_v2.returncode == 0
        and json.loads(parsed_v2.stdout)["outcome_disposition"]
        == "partially-achieved",
    )
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
    plan.parent.mkdir(parents=True, exist_ok=True)
    pending = "---\nstatus: pending\n---\n"
    plan.write_text(pending, encoding="utf-8")
    meta = {"plan_file": str(plan), "approved_plan_sha256": hashlib.sha256(pending.encode()).hexdigest()}
    check("pending plan hash validates", runner.approved_plan_state(meta)[1] == "pending")
    plan.write_text(pending + "Concurrent user edit.\n", encoding="utf-8")
    check(
        "pending concurrent plan edit becomes an independent close conflict",
        runner.approved_plan_state(meta)[1] == "conflict",
    )
    plan.write_text(pending, encoding="utf-8")
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
        == meta["approved_plan_sha256"]
        and final_payload["plan_close"]["on_conflict"] == "preserve",
    )
    conflict_worktree = vault / "conflict-worktree"
    conflict_worktree.mkdir()
    conflict_plan = vault / "wiki/plans/conflict-approved.md"
    conflict_approved = "---\nstatus: pending\n---\n\n# Conflict plan\n"
    conflict_edited = conflict_approved + "\nConcurrent user edit.\n"
    conflict_plan.write_text(conflict_approved, encoding="utf-8")
    conflict_result = vault / "wiki/meta/sessions/Conflict Result.md"
    conflict_meta = {
        "version": 3,
        "interaction_policy": "unattended",
        "task_id": "conflict-task",
        "task_name": "conflict-task",
        "plan_file": str(conflict_plan),
        "approved_plan_sha256": hashlib.sha256(
            conflict_approved.encode()
        ).hexdigest(),
        "reap_policy": {"mode": "final"},
    }
    conflict_summary = {
        "schema_version": 1,
        "type": "session",
        "title": "Conflict Result",
        "session": "executor-session",
        "body": "The result remains durable.",
    }
    (conflict_worktree / ".task-meta.json").write_text(
        json.dumps(conflict_meta) + "\n", encoding="utf-8"
    )
    (conflict_worktree / ".task-summary.json").write_text(
        json.dumps(conflict_summary) + "\n", encoding="utf-8"
    )
    saved_finalize_edges = {
        name: getattr(runner, name)
        for name in (
            "authorize_review",
            "validate_summary_for_task",
            "validate_handoff",
            "validate_summary_wikilinks",
            "archive_reviews",
            "summary_with_reviews",
            "frontmatter_page",
            "emit_lifecycle_event",
            "run",
        )
    }

    def conflict_run(_argv, *, label, **_kwargs):
        if label == "reap preparation":
            (conflict_worktree / ".task-reap-prepared.json").write_text(
                json.dumps(
                    {
                        "result_path": str(conflict_result),
                        "result_link": "[[Conflict Result]]",
                        "plan_path": str(conflict_plan),
                        "plan_close_status": "closed",
                        "closed_plan_sha256": "0" * 64,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            conflict_plan.write_text(conflict_edited, encoding="utf-8")
        elif label == "reap vault transaction":
            conflict_result.write_text("durable result\n", encoding="utf-8")
            return json.dumps(
                {
                    "warnings": [
                        "plan_close conflict preserved for "
                        "wiki/plans/conflict-approved.md"
                    ]
                }
            )
        return ""

    try:
        runner.authorize_review = lambda *_args, **_kwargs: None
        runner.validate_summary_for_task = lambda value, *_args, **_kwargs: value
        runner.validate_handoff = lambda *_args, **_kwargs: None
        runner.validate_summary_wikilinks = lambda *_args, **_kwargs: None
        runner.archive_reviews = lambda *_args, **_kwargs: []
        runner.summary_with_reviews = (
            lambda *_args, **_kwargs: conflict_summary
        )
        runner.frontmatter_page = (
            lambda *_args, **_kwargs: "---\naddress: c-000125\n---\n"
        )
        runner.emit_lifecycle_event = lambda *_args, **_kwargs: None
        runner.run = conflict_run
        conflict_public = runner._finalize_reap(
            vault.resolve(), conflict_worktree, "reap-session"
        )
    finally:
        for name, value in saved_finalize_edges.items():
            setattr(runner, name, value)
    check(
        "public reap JSON reports a preserved plan-close conflict",
        conflict_public.get("plan_close_status") == "conflict"
        and conflict_public.get("warnings") == ["plan-close-conflict"]
        and conflict_result.is_file()
        and conflict_plan.read_text(encoding="utf-8") == conflict_edited,
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
            supervisor = OperationSupervisor(
                runtime_store, owner_id, operation_id
            )
            current_record = runtime_store.read(owner_id, operation_id)
            if current_record.state == "complete":
                return SimpleNamespace(
                    action="terminal", record=current_record
                )
            runtime_actions.append("request-exit")
            record = supervisor.transition("exiting")
            return SimpleNamespace(action="exit-requested", record=record)

        def cleanup(self, owner_id: str, operation_id: str):
            supervisor = OperationSupervisor(
                runtime_store, owner_id, operation_id
            )
            current_record = runtime_store.read(owner_id, operation_id)
            if current_record.state == "complete":
                return SimpleNamespace(
                    action="terminal", record=current_record
                )
            runtime_actions.append("cleanup")
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
    finalize_calls = [0]

    def fake_finalize(_vault, _worktree, _current):
        finalize_calls[0] += 1
        return {
            "schema_version": 1,
            "status": "complete",
            "result_path": str(
                vault / "wiki/meta/sessions/Typed Result.md"
            ),
            "result_link": "[[Typed Result]]",
            "plan_close_status": "closed",
            "warnings": [],
            "duration_ms": 1,
        }

    runner._finalize_reap = fake_finalize
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
        first_terminal = runtime_store.read(task_id, task_id)
        replayed: dict[str, dict[str, object]] = {}
        for close_status in ("closed", "conflict", "retained"):
            (worktree / ".task-reap-prepared.json").write_text(
                json.dumps(
                    {
                        "result_path": str(
                            vault / "wiki/meta/sessions/Typed Result.md"
                        ),
                        "result_link": "[[Typed Result]]",
                        "plan_close_status": close_status,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            replayed[close_status] = runner.apply_reap(
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
        and reaped["plan_close_status"] == "closed"
        and reaped["warnings"] == []
        and dispatched.record.state == "awaiting-callback"
        and durable.state == "complete"
        and durable.accepted_callback_kind == "wiki-summary"
        and durable.accepted_callback_id == "wiki-summary-runtime-worker",
    )
    check(
        "reap authorizes the exact review gate before consuming the callback",
        review_actions == ["authorized"] * 4,
    )
    check(
        "reap exits provider before exact cleanup and clears ownership",
        runtime_actions == ["request-exit", "cleanup"]
        and durable.resources == OwnedResources(),
    )
    check(
        "idempotent reap preserves every typed plan-close projection",
        all(
            replayed[status]["plan_close_status"] == status
            and replayed[status]["warnings"]
            == (["plan-close-conflict"] if status == "conflict" else [])
            and replayed[status]["idempotent"] is True
            for status in ("closed", "conflict", "retained")
        )
        and finalize_calls == [1]
        and runtime_actions == ["request-exit", "cleanup"]
        and durable.revision == first_terminal.revision,
    )

straddle_link = (
    "[[Cross-model review — f20f7bc1-d469-53bd-91c2-e67758312758 — f1e5dbafe036]]"
)
enriched = ("x" * 474) + straddle_link
excerpt = runner.log_excerpt(enriched)
check("excerpt never exceeds the log cap", len(excerpt) <= 500)
check("excerpt never cuts inside a straddling wikilink", excerpt == "x" * 474)
check(
    "excerpt keeps wikilinks balanced",
    excerpt.count("[[") == excerpt.count("]]"),
)
check(
    "short bodies pass through unchanged",
    runner.log_excerpt("done [[A]]") == "done [[A]]",
)
check(
    "plain text still truncates exactly at the cap",
    runner.log_excerpt("a" * 600) == "a" * 500,
)
check(
    "unclosed opener in the source never leaks unmatched brackets",
    "[[" not in runner.log_excerpt(("y" * 100) + "[[unclosed " + ("z" * 500)),
)
check(
    "wikilink closing exactly at the cap is preserved",
    runner.log_excerpt(("w" * 490) + "[[ABCDEF]]" + ("v" * 100))
    == ("w" * 490) + "[[ABCDEF]]",
)
entry = runner.reap_log_entry(
    today="2026-08-10",
    task_name="v267-rc1-cell-1f-interval-merge",
    address="c-000150",
    link="[[V267 RC1 Cell 1f - Terra interval-merge corridor]]",
    body=enriched,
)
entry_heading, _, entry_body = entry.partition("\n\n")
check(
    "reap log entry preserves the heading and address format",
    entry_heading == "## [2026-08-10] reap | v267-rc1-cell-1f-interval-merge"
    and entry_body.startswith(
        "`c-000150` [[V267 RC1 Cell 1f - Terra interval-merge corridor]]. "
    ),
)
check(
    "reap log entry body uses the wikilink-safe excerpt",
    entry_body.endswith("x" * 474)
    and entry.count("[[") == entry.count("]]"),
)

if failures:
    raise SystemExit(f"{len(failures)} reap runner test(s) failed")
print("All reap runner tests passed.")
