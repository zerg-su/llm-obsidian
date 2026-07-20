#!/usr/bin/env python3
"""Hermetic checks for the deterministic task-to-coordinator reap callback."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import tempfile
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/reap-send/scripts/send_reap.py"
spec = importlib.util.spec_from_file_location("send_reap", SCRIPT)
assert spec and spec.loader
sender = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sender)
failures: list[str] = []


def check(name: str, value: bool) -> None:
    print(("ok" if value else "not ok") + " - " + name)
    if not value:
        failures.append(name)


with tempfile.TemporaryDirectory(prefix="reap-send-runner.") as raw:
    tmp = Path(raw)
    vault = tmp / "vault with spaces"
    worktree = tmp / "task worktree"
    (vault / "scripts").mkdir(parents=True)
    (vault / "wiki/plans").mkdir(parents=True)
    worktree.mkdir()
    for name in ("task_contract.py", "vault_schema.py", "wiki_summary_contract.py"):
        shutil.copy2(ROOT / "scripts" / name, vault / "scripts" / name)
    (vault / "scripts/reap-runner.py").write_text("# fixture\n", encoding="utf-8")
    plan = vault / "wiki/plans/approved.md"
    plan.write_text("---\nstatus: pending\n---\n", encoding="utf-8")
    surface = "11111111-1111-4111-8111-111111111111"
    meta = {
        "version": 3,
        "project_id": str(uuid.uuid4()),
        "task_id": str(uuid.uuid4()),
        "task_name": "typed-reap",
        "origin_session": "origin",
        "executor_runtime": "codex",
        "interaction_policy": "unattended",
        "plan_file": str(plan),
        "approved_plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "vault_root": str(vault),
        "review_policy": {"mode": "light", "max_verify_iterations": 2, "auto_resolve_severities": ["warning", "nit"], "escalate_severities": ["blocking"]},
        "reap_policy": {"mode": "final", "auto_file": True, "allowed_types": ["session"], "title": "Typed result"},
        "surface_policy": {"auto_close": True},
        "watchdog_policy": {"enabled": True, "poll_seconds": 30, "warn_after_seconds": 900, "alert_after_seconds": 1200},
        "forbidden_actions": ["push", "deploy", "publish", "delete-worktree", "delete-branch", "expand-scope"],
        "wiki_surface": surface,
    }
    summary = {"schema_version": 1, "type": "session", "title": "Typed result", "session": "executor", "body": "Done."}
    (worktree / ".task-meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (worktree / ".task-summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (worktree / ".wiki-cmux-surface").write_text(surface + "\n", encoding="utf-8")
    value = sender.callback(worktree.resolve())
    check("v3 callback selects runner mode", value["mode"] == "runner")
    check("callback targets exact coordinator surface", value["surface"] == surface)
    check("callback contains one exact reap runner command", value["message"].count("reap-runner.py") == 1)
    check("callback shell-quotes paths with spaces", "'" in value["command"] and str(worktree) in value["command"])
    sent_result = sender.public_result(value, dry_run=False)
    dry_result = sender.public_result(value, dry_run=True)
    check(
        "sent result does not echo coordinator executable instructions",
        sent_result == {
            "schema_version": 1,
            "status": "sent",
            "mode": "runner",
            "neutralized_wikilinks": 0,
        },
    )
    check(
        "dry-run retains inspectable coordinator instructions",
        dry_result["status"] == "validated"
        and dry_result["surface"] == surface
        and dry_result["message"] == value["message"]
        and dry_result["command"] == value["command"],
    )
    check(
        "callback always renders the canonical Markdown view",
        (worktree / ".task-summary.md").read_text(encoding="utf-8").endswith("Done.\n"),
    )
    linked = dict(
        summary,
        body="Keep [[approved]] and `[[Example]]`; de-link [[Invented plan|the plan]].",
    )
    (worktree / ".task-summary.json").write_text(json.dumps(linked), encoding="utf-8")
    value = sender.callback(worktree.resolve())
    repaired = json.loads((worktree / ".task-summary.json").read_text(encoding="utf-8"))
    rendered = (worktree / ".task-summary.md").read_text(encoding="utf-8")
    check(
        "callback neutralizes unresolved prose links before delivery",
        value["neutralized_wikilinks"] == 1
        and "[[approved]]" in repaired["body"]
        and "`[[Example]]`" in repaired["body"]
        and "de-link the plan" in repaired["body"]
        and "[[Invented plan" not in repaired["body"]
        and repaired["body"] in rendered,
    )
    (worktree / ".task-summary.json").write_text(json.dumps(linked), encoding="utf-8")
    sender.callback(worktree.resolve(), persist_repairs=False)
    check(
        "dry callback reports repair without mutating canonical summary",
        "[[Invented plan|the plan]]"
        in json.loads((worktree / ".task-summary.json").read_text(encoding="utf-8"))["body"],
    )
    (worktree / ".task-summary.json").write_text(json.dumps(summary), encoding="utf-8")
    send_calls: list[list[str]] = []
    sleeps: list[float] = []
    original_run = sender.subprocess.run
    original_sleep = sender.time.sleep
    sender.subprocess.run = lambda argv, **_kwargs: (
        send_calls.append(list(argv))
        or sender.subprocess.CompletedProcess(argv, 0, stdout="OK", stderr="")
    )
    sender.time.sleep = lambda seconds: sleeps.append(seconds)
    try:
        sender.send(value)
    finally:
        sender.subprocess.run = original_run
        sender.time.sleep = original_sleep
    check(
        "callback settles paste before Enter",
        [call[1] for call in send_calls] == ["send", "send-key"]
        and sleeps == [sender.CMUX_PASTE_SETTLE_SECONDS],
    )
    drifted = dict(summary, title="Different")
    (worktree / ".task-summary.json").write_text(json.dumps(drifted), encoding="utf-8")
    try:
        sender.callback(worktree.resolve())
    except sender.SendError:
        check("summary type/title drift fails closed", True)
    else:
        check("summary type/title drift fails closed", False)
    missing_session = dict(summary, session="")
    (worktree / ".task-summary.json").write_text(json.dumps(missing_session), encoding="utf-8")
    try:
        sender.callback(worktree.resolve())
    except sender.SendError:
        check("missing executor provenance fails closed", True)
    else:
        check("missing executor provenance fails closed", False)

if failures:
    raise SystemExit(f"{len(failures)} reap send runner test(s) failed")
print("All reap send runner tests passed.")
