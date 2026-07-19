#!/usr/bin/env python3
"""Pure regression checks for deterministic reap routing and page rendering."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("reap_runner", ROOT / "scripts/reap-runner.py")
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

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

if failures:
    raise SystemExit(f"{len(failures)} reap runner test(s) failed")
print("All reap runner tests passed.")
