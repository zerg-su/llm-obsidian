#!/usr/bin/env python3
"""Contract and cmux lifecycle regression tests for unattended dispatch."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "scripts" / "task_contract.py"
LIFECYCLE = ROOT / "scripts" / "cmux_surface_lifecycle.py"
ESCALATION = ROOT / "scripts" / "task_escalation.py"
WATCHDOG = ROOT / "scripts" / "cmux_task_watchdog.py"
SUPERVISOR = ROOT / "scripts" / "cmux_agent_supervisor.py"
sys.path.insert(0, str(ROOT / "scripts"))
import cmux_agent_supervisor as supervisor_module
import cmux_surface_lifecycle as lifecycle_module
import cmux_task_watchdog as watchdog_module
from plan_lifecycle import render_plan_close


def run(script: Path, *args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args], cwd=cwd, env=env,
        text=True, capture_output=True, check=False,
    )


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def telemetry(worktree: Path, op: str) -> list[dict]:
    path = worktree / ".vault-meta/pipeline-events.jsonl"
    if not path.is_file():
        return []
    return [
        record for record in (
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        )
        if record.get("op") == op
    ]


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"OK   {name}")


with tempfile.TemporaryDirectory(prefix="task-lifecycle-test.") as raw:
    worktree = Path(raw)
    check("watchdog atomic tmp keeps handoff prefix", watchdog_module.atomic_tmp_path(worktree / ".task-watchdog.json").name.startswith(".task-"))
    check("supervisor atomic tmp keeps handoff prefix", supervisor_module.atomic_tmp_path(worktree / ".review-agent-command.json").name.startswith(".review-"))
    plan = worktree / "wiki" / "plans" / "approved-plan.md"
    plan.parent.mkdir(parents=True)
    approved_plan_text = (
        "---\n"
        "type: plan\n"
        "status: pending\n"
        "updated: 2026-07-11\n"
        "sessions:\n"
        "  - id: origin-1\n"
        "    date: 2026-07-11\n"
        "---\n"
        "# Approved plan\n"
    )
    plan.write_text(approved_plan_text, encoding="utf-8")
    plan_hash = hashlib.sha256(plan.read_bytes()).hexdigest()
    archive_fixture = worktree / "archive-fixture"
    archive_fixture.mkdir()
    archive_page = worktree / "wiki" / "meta" / "reviews" / "Review fixture.md"
    archive_page.parent.mkdir(parents=True)
    archive_page.write_text("# Review fixture\n", encoding="utf-8")
    write_json(archive_fixture / ".review-meta.json", {"review_id": "review-fixture"})
    archive_marker = {
        "schema_version": 1,
        "status": "archived",
        "review_id": "review-fixture",
        "path": "wiki/meta/reviews/Review fixture.md",
        "title": "Review fixture",
        "wikilink": "[[Review fixture]]",
        "verdict": "approve",
        "content_sha256": hashlib.sha256(archive_page.read_bytes()).hexdigest(),
    }
    write_json(archive_fixture / ".review-archive.json", archive_marker)
    validated = lifecycle_module.validated_review_archive(archive_fixture, worktree)
    check("review archive marker validates", validated is not None and validated["review_id"] == "review-fixture")
    archive_page.write_text("# Tampered review fixture\n", encoding="utf-8")
    try:
        lifecycle_module.validated_review_archive(archive_fixture, worktree)
    except SystemExit as exc:
        check("review archive tamper blocks finalization", exc.code == 3)
    else:
        check("review archive tamper blocks finalization", False)
    archive_page.write_text("# Review fixture\n", encoding="utf-8")
    meta = {
        "version": 2,
        "task_name": "demo",
        "origin_session": "origin-1",
        "executor_runtime": "codex",
        "task_surface": "11111111-1111-1111-1111-111111111111",
        "wiki_surface": "33333333-3333-3333-3333-333333333333",
        "plan_file": str(plan),
        "approved_plan_sha256": plan_hash,
        "interaction_policy": "unattended",
        "review_policy": {
            "mode": "light",
            "max_verify_iterations": 2,
            "auto_resolve_severities": ["warning", "nit"],
            "escalate_severities": ["blocking"],
        },
        "reap_policy": {
            "mode": "final", "auto_file": True,
            "allowed_types": ["session"], "title": "Demo Result",
        },
        "surface_policy": {"auto_close": True},
        "watchdog_policy": {
            "enabled": True, "poll_seconds": 30,
            "warn_after_seconds": 900, "alert_after_seconds": 1200,
        },
        "forbidden_actions": [
            "push", "deploy", "publish", "delete-worktree", "delete-branch", "expand-scope",
        ],
    }
    write_json(worktree / ".task-meta.json", meta)
    (worktree / ".task-prompt.md").write_text("# Task: demo\n\nImplement the approved plan.\n", encoding="utf-8")
    summary = {"schema_version": 1, "type": "session", "title": "Demo Result", "session": "exec-1", "body": "done"}
    write_json(worktree / ".task-summary.json", summary)

    result = run(CONTRACT, "validate", cwd=worktree)
    check("v2 contract valid", result.returncode == 0, result.stderr)
    invalid = dict(meta)
    invalid["version"] = True
    write_json(worktree / "invalid-version.json", invalid)
    result = run(CONTRACT, "validate", "--meta", "invalid-version.json", cwd=worktree)
    check("boolean contract version rejected", result.returncode == 2)
    invalid = dict(meta)
    invalid["origin_session"] = ""
    write_json(worktree / "invalid-origin.json", invalid)
    result = run(CONTRACT, "validate", "--meta", "invalid-origin.json", cwd=worktree)
    check("missing origin rejected", result.returncode == 2)
    invalid = json.loads(json.dumps(meta))
    invalid["review_policy"]["max_verify_iterations"] = True
    write_json(worktree / "invalid-max-verify.json", invalid)
    result = run(CONTRACT, "validate", "--meta", "invalid-max-verify.json", cwd=worktree)
    check("boolean verify limit rejected", result.returncode == 2)
    invalid = json.loads(json.dumps(meta))
    invalid["forbidden_actions"].remove("push")
    write_json(worktree / "invalid-forbidden-actions.json", invalid)
    result = run(CONTRACT, "validate", "--meta", "invalid-forbidden-actions.json", cwd=worktree)
    check("weakened forbidden actions rejected", result.returncode == 2)
    invalid = json.loads(json.dumps(meta))
    invalid["watchdog_policy"]["alert_after_seconds"] = 900
    write_json(worktree / "invalid-watchdog.json", invalid)
    result = run(CONTRACT, "validate", "--meta", "invalid-watchdog.json", cwd=worktree)
    check("unordered watchdog thresholds rejected", result.returncode == 2)
    result = run(CONTRACT, "check-handoff", "--current-session", "origin-1", cwd=worktree)
    check("approved handoff accepted", result.returncode == 0, result.stderr)
    result = run(CONTRACT, "check-handoff", "--current-session", "other", cwd=worktree)
    check("session mismatch rejected", result.returncode == 2)

    summary["title"] = "Changed target"
    write_json(worktree / ".task-summary.json", summary)
    result = run(CONTRACT, "check-handoff", "--current-session", "origin-1", cwd=worktree)
    check("target drift rejected", result.returncode == 2)
    summary["title"] = "Demo Result"
    write_json(worktree / ".task-summary.json", summary)

    warning_review = {
        "verdict": "changes-requested",
        "findings": [{"severity": "warning"}],
    }
    blocking_review = {
        "verdict": "changes-requested",
        "findings": [{"severity": "blocking"}],
    }
    inconsistent_review = {"verdict": "changes-requested", "findings": []}
    write_json(worktree / "warning.json", warning_review)
    write_json(worktree / "blocking.json", blocking_review)
    write_json(worktree / "inconsistent.json", inconsistent_review)
    result = run(CONTRACT, "review-action", "--review", "warning.json", "--iteration", "0", cwd=worktree)
    check("warning auto-resolves", result.stdout.strip() == "resolve")
    result = run(CONTRACT, "review-action", "--review", "warning.json", "--iteration", "2", cwd=worktree)
    check("verify limit escalates", result.stdout.strip() == "escalate")
    result = run(CONTRACT, "review-action", "--review", "blocking.json", "--iteration", "0", cwd=worktree)
    check("blocking escalates", result.stdout.strip() == "escalate")
    result = run(CONTRACT, "review-action", "--review", "inconsistent.json", "--iteration", "0", cwd=worktree)
    check("inconsistent review escalates", result.stdout.strip() == "escalate")

    legacy = dict(meta)
    legacy["version"] = 1
    write_json(worktree / "legacy.json", legacy)
    result = run(CONTRACT, "validate", "--meta", "legacy.json", cwd=worktree)
    check("legacy metadata remains interactive", '"interaction_policy": "interactive"' in result.stdout)

    plan.write_text("# Mutated plan\n", encoding="utf-8")
    result = run(CONTRACT, "validate", cwd=worktree)
    check("plan hash drift rejected", result.returncode == 2)
    plan.write_text(approved_plan_text, encoding="utf-8")

    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, check=True)
    (worktree / ".gitignore").write_text(
        ".vault-meta/pipeline-events.jsonl\n"
        ".vault-meta/pipeline-events.jsonl.1\n"
        ".vault-meta/.pipeline-events.lock\n",
        encoding="utf-8",
    )
    (worktree / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt", ".gitignore"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=worktree, check=True)

    fake_bin = worktree / "fake-bin"
    fake_bin.mkdir()
    cmux_log = worktree / "cmux.log"
    fake_cmux = fake_bin / "cmux"
    fake_cmux.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  read-screen)\n"
        "    if [ \"${CMUX_SCREEN_GONE:-0}\" = 1 ]; then echo 'not_found: surface' >&2; exit 1; fi\n"
        "    if [ \"${CMUX_SCREEN_FAIL:-0}\" = 1 ]; then echo 'temporary failure' >&2; exit 2; fi\n"
        "    cat \"$CMUX_SCREEN_FILE\"; exit 0 ;;\n"
        "  top) printf 'top\\n' >> \"$CMUX_TEST_LOG\"; cat \"$CMUX_TOP_FILE\"; exit 0 ;;\n"
        "  notify) if [ \"${CMUX_NOTIFY_FAIL:-0}\" = 1 ]; then echo 'notify failed' >&2; exit 2; fi; "
        "printf '%s\\n' \"$*\" >> \"$CMUX_TEST_LOG\"; exit 0 ;;\n"
        "  *) printf '%s\\n' \"$*\" >> \"$CMUX_TEST_LOG\"; exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_cmux.chmod(0o755)
    agent_log = worktree / "agent.log"
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_AGENT_LOG\"\n"
        "exit \"${FAKE_AGENT_EXIT:-0}\"\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    screen_file = worktree / "cmux-screen.txt"
    screen_file.write_text("task alpha\n5H 20% used | reset in 2h\n", encoding="utf-8")
    top_file = worktree / "cmux-top.json"
    write_json(top_file, {
        "windows": [{"workspaces": [{"panes": [{"surfaces": [{"processes": [{
            "cmux_surface_id": meta["task_surface"], "name": "codex", "path": "/bin/codex",
            "resources": {"cpu_percent": 0.75}, "children": [],
        }]}]}]}]}],
    })
    env = dict(
        os.environ, PATH=f"{fake_bin}:{os.environ.get('PATH', '')}",
        CMUX_TEST_LOG=str(cmux_log), CMUX_SCREEN_FILE=str(screen_file), CMUX_TOP_FILE=str(top_file),
        FAKE_AGENT_LOG=str(agent_log),
    )
    clean_env = {k: v for k, v in env.items() if k not in {"CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"}}
    wrong_env = dict(clean_env, CODEX_THREAD_ID="other")
    origin_env = dict(clean_env, CODEX_THREAD_ID="origin-1")
    result_page = worktree / "wiki" / "Demo Result.md"
    result_page.parent.mkdir(exist_ok=True)
    result_page.write_text("# Demo Result\n", encoding="utf-8")

    result = run(
        SUPERVISOR, "prepare-task", "--worktree", str(worktree),
        "--surface", meta["task_surface"], cwd=worktree, env=env,
    )
    check("supervisor prepares task argv", result.returncode == 0, result.stderr)
    agent_spec = json.loads((worktree / ".task-agent-command.json").read_text(encoding="utf-8"))
    check("supervisor pins unattended Codex approvals", agent_spec["argv"][-4:] == ["-a", "never", "-s", "workspace-write"])
    add_dir_index = agent_spec["argv"].index("--add-dir")
    check(
        "supervisor grants only the task Git metadata root",
        agent_spec["argv"][add_dir_index + 1] == str((worktree / ".git").resolve()),
    )
    result = run(
        SUPERVISOR, "validate", "--worktree", str(worktree), "--kind", "task",
        "--surface", meta["task_surface"], cwd=worktree, env=env,
    )
    check("supervisor validates exact task routing", result.returncode == 0, result.stderr)
    safe_agent_spec = json.loads(json.dumps(agent_spec))
    agent_spec["argv"].extend(["-s", "danger-full-access"])
    write_json(worktree / ".task-agent-command.json", agent_spec)
    result = run(
        SUPERVISOR, "validate", "--worktree", str(worktree), "--kind", "task",
        "--surface", meta["task_surface"], cwd=worktree, env=env,
    )
    check("supervisor rejects sandbox tampering", result.returncode == 2)
    write_json(worktree / ".task-agent-command.json", safe_agent_spec)
    agent_spec = json.loads(json.dumps(safe_agent_spec))
    add_dir_index = agent_spec["argv"].index("--add-dir")
    agent_spec["argv"][add_dir_index + 1] = "/tmp"
    write_json(worktree / ".task-agent-command.json", agent_spec)
    result = run(
        SUPERVISOR, "validate", "--worktree", str(worktree), "--kind", "task",
        "--surface", meta["task_surface"], cwd=worktree, env=env,
    )
    check("supervisor rejects arbitrary task writable roots", result.returncode == 2)
    write_json(worktree / ".task-agent-command.json", safe_agent_spec)
    result = run(
        SUPERVISOR, "validate", "--worktree", str(worktree), "--kind", "task",
        "--surface", "22222222-2222-2222-2222-222222222222", cwd=worktree, env=env,
    )
    check("supervisor rejects surface drift", result.returncode == 2)
    result = run(
        SUPERVISOR, "run", "--worktree", str(worktree), "--kind", "task",
        "--surface", meta["task_surface"], cwd=worktree, env=env,
    )
    check("supervisor runs agent and lifecycle", result.returncode == 0, result.stderr)
    check("supervisor appends prompt as one argv value", "Implement the approved plan." in agent_log.read_text(encoding="utf-8"))
    failed_env = dict(env, FAKE_AGENT_EXIT="7")
    result = run(
        SUPERVISOR, "run", "--worktree", str(worktree), "--kind", "task",
        "--surface", meta["task_surface"], cwd=worktree, env=failed_env,
    )
    check("supervisor preserves agent failure status", result.returncode == 7)
    agent_events = telemetry(worktree, "agent-run")
    check(
        "supervisor emits content-free run outcomes",
        len(agent_events) == 2
        and agent_events[0]["actor"] == "task:codex"
        and agent_events[0]["status"] == "ok"
        and agent_events[1]["status"] == "error"
        and agent_events[1]["counts"]["agent_exit_code"] == 7,
    )
    check(
        "supervisor reserves numeric signal counters",
        agent_events[0]["counts"]["agent_signal"] == 0
        and agent_events[0]["counts"]["lifecycle_signal"] == 0,
    )
    class StubbornWatchdog:
        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

        def wait(self, timeout: int) -> None:
            raise subprocess.TimeoutExpired("watchdog", timeout)

    supervisor_module.stop_watchdog(StubbornWatchdog())  # type: ignore[arg-type]
    check("supervisor cannot skip lifecycle on watchdog reap timeout", True)

    subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "test fixtures"], cwd=worktree, check=True)

    result = run(LIFECYCLE, "request-exit", "--kind", "task", cwd=worktree, env=origin_env)
    check("task cannot close before final reap", result.returncode == 2)
    result = run(
        LIFECYCLE, "complete-reap", "--current-session", "other",
        "--result-path", str(result_page), "--vault-root", str(worktree),
        cwd=worktree, env=origin_env,
    )
    check("reap marker rejects session drift", result.returncode == 3)
    result = run(
        LIFECYCLE, "prepare-reap", "--current-session", "origin-1",
        "--result-path", str(result_page), "--vault-root", str(worktree),
        cwd=worktree, env=origin_env,
    )
    check("final reap preparation recorded", result.returncode == 0, result.stderr)
    check("reap preparation marker written", (worktree / ".task-reap-prepared.json").is_file())
    check(
        "reap preparation marker is owner-only",
        (worktree / ".task-reap-prepared.json").stat().st_mode & 0o777 == 0o600,
    )
    result = run(
        LIFECYCLE, "complete-reap", "--current-session", "origin-1",
        "--result-path", str(result_page), "--vault-root", str(worktree),
        cwd=worktree, env=origin_env,
    )
    check("pending plan cannot complete prepared reap", result.returncode == 3)
    prepared = json.loads((worktree / ".task-reap-prepared.json").read_text(encoding="utf-8"))
    closed_plan_text = render_plan_close(
        plan.read_text(encoding="utf-8"),
        today=prepared["prepared_date"],
        result_link="[[Demo Result]]",
        exec_session="exec-1",
        label="wiki/plans/approved-plan.md",
    )
    plan.write_text(closed_plan_text, encoding="utf-8")
    result = run(
        WATCHDOG, "sample", "--kind", "task", "--surface", meta["task_surface"],
        "--now", "900", "--reset", cwd=worktree, env=env,
    )
    check("prepared plan close keeps task watchdog valid", result.returncode == 0, result.stderr)
    result = run(CONTRACT, "validate", cwd=worktree)
    check("generic contract still rejects a closed approved plan", result.returncode == 2)
    result = run(
        LIFECYCLE, "complete-reap", "--current-session", "origin-1",
        "--result-path", str(result_page), "--vault-root", str(worktree),
        cwd=worktree, env=origin_env,
    )
    check("validated final reap recorded", result.returncode == 0, result.stderr)
    check("reap completion marker written", (worktree / ".task-reap-complete.json").is_file())
    check(
        "validated reap emits task completion",
        len(telemetry(worktree, "task-complete")) == 1
        and telemetry(worktree, "task-complete")[0]["counts"]["tasks"] == 1,
    )
    check(
        "reap completion marker is owner-only",
        (worktree / ".task-reap-complete.json").stat().st_mode & 0o777 == 0o600,
    )
    subprocess.run(["git", "add", str(plan)], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "close plan fixture"], cwd=worktree, check=True)

    summary["body"] = "changed after reap"
    write_json(worktree / ".task-summary.json", summary)
    result = run(LIFECYCLE, "request-exit", "--kind", "task", cwd=worktree, env=origin_env)
    check("changed summary cannot reuse reap marker", result.returncode == 3)
    summary["body"] = "done"
    write_json(worktree / ".task-summary.json", summary)

    result_page.write_text("# Changed after reap\n", encoding="utf-8")
    result = run(LIFECYCLE, "request-exit", "--kind", "task", cwd=worktree, env=origin_env)
    check("changed result page cannot reuse reap marker", result.returncode == 3)
    result_page.write_text("# Demo Result\n", encoding="utf-8")

    plan.write_text(closed_plan_text + "tampered\n", encoding="utf-8")
    result = run(
        WATCHDOG, "sample", "--kind", "task", "--surface", meta["task_surface"],
        "--now", "901", cwd=worktree, env=env,
    )
    check("watchdog rejects unprepared closed-plan drift", result.returncode == 2)
    result = run(LIFECYCLE, "request-exit", "--kind", "task", cwd=worktree, env=origin_env)
    check("changed closed plan cannot reuse reap marker", result.returncode == 3)
    plan.write_text(closed_plan_text, encoding="utf-8")

    result = run(LIFECYCLE, "request-exit", "--kind", "task", cwd=worktree, env=wrong_env)
    check("non-origin session cannot close task", result.returncode == 3)
    tracked_state = worktree / ".vault-meta" / "address-counter.txt"
    tracked_state.parent.mkdir(exist_ok=True)
    tracked_state.write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "add", str(tracked_state)], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "track vault state fixture"], cwd=worktree, check=True)
    tracked_state.write_text("2\n", encoding="utf-8")
    result = run(LIFECYCLE, "request-exit", "--kind", "task", cwd=worktree, env=origin_env)
    check("modified tracked vault state blocks close", result.returncode == 3)
    tracked_state.write_text("1\n", encoding="utf-8")
    runtime_tmp = worktree / ".task-watchdog.json.tmp.999"
    runtime_tmp.write_text("{}\n", encoding="utf-8")
    result = run(LIFECYCLE, "request-exit", "--kind", "task", cwd=worktree, env=origin_env)
    check("task exit armed", result.returncode == 0, result.stderr)
    runtime_tmp.unlink()
    check("task sentinel written", (worktree / ".task-close-armed.json").is_file())
    result = run(
        LIFECYCLE, "after-exit", "--kind", "task",
        "--surface", meta["task_surface"], cwd=worktree, env=env,
    )
    check("armed surface closes", result.returncode == 0, result.stderr)
    log = cmux_log.read_text(encoding="utf-8")
    check("exact surface targeted", f"close-surface --surface {meta['task_surface']}" in log)
    check("Codex exit accepts slash command", f"send-key --surface {meta['task_surface']} tab" in log)
    check("Codex exit submits command", f"send-key --surface {meta['task_surface']} Enter" in log)
    check("sentinel removed", not (worktree / ".task-close-armed.json").exists())

    # Reviewer lifecycle is normally finished before final plan close. Restore
    # that phase boundary for the remaining independent lifecycle assertions.
    plan.write_text(approved_plan_text, encoding="utf-8")
    subprocess.run(["git", "add", str(plan)], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "restore reviewer phase fixture"], cwd=worktree, check=True)

    review_surface = "22222222-2222-2222-2222-222222222222"
    write_json(worktree / ".review-meta.json", {
        "review_surface": review_surface,
        "reviewer_runtime": "claude",
        "executor_surface": meta["task_surface"],
    })
    result = run(LIFECYCLE, "request-exit", "--kind", "reviewer", cwd=worktree, env=env)
    check("reviewer exit armed", result.returncode == 0, result.stderr)
    result = run(
        LIFECYCLE, "after-exit", "--kind", "reviewer",
        "--surface", review_surface, cwd=worktree, env=env,
    )
    check("reviewer surface closes", result.returncode == 0, result.stderr)
    check("reviewer exact surface targeted", f"close-surface --surface {review_surface}" in cmux_log.read_text())
    surface_events = telemetry(worktree, "surface-lifecycle")
    check(
        "surface lifecycle distinguishes close and left-open",
        any(event["counts"].get("closed") == 1 for event in surface_events)
        and any(event["counts"].get("left_open") == 1 for event in surface_events),
    )

    result = run(
        ESCALATION, "raise", "--category", "scope",
        "--reason", "A new public endpoint is required",
        "--question", "May the task expand the approved interface?",
        cwd=worktree, env=env,
    )
    check("task escalation notifies coordinator", result.returncode == 0, result.stderr)
    attention = json.loads((worktree / ".task-needs-attention.json").read_text(encoding="utf-8"))
    check("task escalation remains pending", attention["status"] == "pending")
    check("coordinator exact surface notified", f"notify --surface {meta['wiki_surface']}" in cmux_log.read_text())
    result = run(LIFECYCLE, "request-exit", "--kind", "task", cwd=worktree, env=origin_env)
    check("pending escalation prevents task close", result.returncode == 3)
    result = run(CONTRACT, "check-handoff", "--current-session", "origin-1", cwd=worktree, env=env)
    check("pending escalation prevents final handoff", result.returncode == 2)
    cmux_log.write_text("", encoding="utf-8")
    result = run(
        ESCALATION, "resolve", "--decision", "Keep the current private interface",
        cwd=worktree, env=wrong_env,
    )
    check("only origin coordinator can resolve", result.returncode == 3)
    result = run(
        ESCALATION, "resolve", "--decision", "Keep the current private interface",
        cwd=worktree, env=origin_env,
    )
    check("coordinator decision reaches task", result.returncode == 0, result.stderr)
    attention = json.loads((worktree / ".task-needs-attention.json").read_text(encoding="utf-8"))
    check("task escalation marked resolved", attention["status"] == "resolved")
    escalation_events = telemetry(worktree, "task-escalation")
    check(
        "escalation lifecycle counted",
        len(escalation_events) == 2
        and escalation_events[0]["counts"].get("raised") == 1
        and escalation_events[1]["counts"].get("resolved") == 1,
    )
    resolved_attention = dict(attention)
    failed_notify_env = dict(env, CMUX_NOTIFY_FAIL="1")
    result = run(
        ESCALATION, "raise", "--category", "permission",
        "--reason", "The reviewer needs an unavailable permission",
        "--question", "Should the coordinator grant it?",
        cwd=worktree, env=failed_notify_env,
    )
    check("failed escalation delivery is reported", result.returncode == 3)
    failed_attention = json.loads((worktree / ".task-needs-attention.json").read_text(encoding="utf-8"))
    check("failed escalation delivery stays explicit", failed_attention["status"] == "delivery-failed")
    escalation_events = telemetry(worktree, "task-escalation")
    check(
        "failed escalation delivery counted",
        escalation_events[-1]["status"] == "error"
        and escalation_events[-1]["counts"].get("raised") == 1
        and escalation_events[-1]["counts"].get("delivery_failures") == 1,
    )
    cmux_log.write_text("", encoding="utf-8")
    result = run(
        ESCALATION, "resolve", "--decision", "Continue after coordinator inspection",
        cwd=worktree, env=origin_env,
    )
    check("delivery-failed escalation can be recovered", result.returncode == 0, result.stderr)
    recovered_attention = json.loads(
        (worktree / ".task-needs-attention.json").read_text(encoding="utf-8")
    )
    check(
        "recovered escalation preserves failed delivery provenance",
        recovered_attention["status"] == "resolved"
        and recovered_attention["resolved_from"] == "delivery-failed",
    )
    escalation_events = telemetry(worktree, "task-escalation")
    check(
        "recovered escalation resolution counted",
        escalation_events[-1]["counts"].get("resolved") == 1,
    )
    check("task exact surface targeted", f"--surface {meta['task_surface']}" in cmux_log.read_text())
    check("Codex task composer cleared", f"send-key --surface {meta['task_surface']} backspace" in cmux_log.read_text())

    cmux_log.write_text("", encoding="utf-8")
    result = run(
        LIFECYCLE, "after-exit", "--kind", "task",
        "--surface", meta["task_surface"], cwd=worktree, env=env,
    )
    check("unarmed exit leaves surface", result.returncode == 0 and not cmux_log.read_text())

    cmux_log.write_text("", encoding="utf-8")
    result = run(
        WATCHDOG, "sample", "--kind", "task", "--surface", meta["task_surface"],
        "--now", "1000", "--reset", cwd=worktree, env=env,
    )
    check("watchdog initial sample", result.returncode == 0, result.stderr)
    watchdog_state = json.loads((worktree / ".task-watchdog.json").read_text(encoding="utf-8"))
    check("watchdog starts running", watchdog_state["status"] == "running")
    check("watchdog defers expensive CPU sampling", watchdog_state["agent_cpu_percent"] is None)
    failed_screen_env = dict(env, CMUX_SCREEN_FAIL="1")
    for now in (1010, 1020, 1030):
        result = run(
            WATCHDOG, "sample", "--kind", "task", "--surface", meta["task_surface"],
            "--now", str(now), cwd=worktree, env=failed_screen_env,
        )
    check("watchdog reports repeated sampling failure once", result.returncode == 0 and cmux_log.read_text().count("notify ") == 1)
    cmux_log.write_text("", encoding="utf-8")
    result = run(
        WATCHDOG, "sample", "--kind", "task", "--surface", meta["task_surface"],
        "--now", "1040", cwd=worktree, env=env,
    )
    sampling_recovery_log = cmux_log.read_text(encoding="utf-8")
    check("sampling recovery does not claim visible progress", result.returncode == 0 and "visible progress state is unchanged" in sampling_recovery_log and "Visible progress resumed" not in sampling_recovery_log)
    cmux_log.write_text("", encoding="utf-8")
    screen_file.write_text("task alpha\n5H 99% used | reset in 1m\n", encoding="utf-8")
    result = run(
        WATCHDOG, "sample", "--kind", "task", "--surface", meta["task_surface"],
        "--now", "1899", cwd=worktree, env=env,
    )
    check("watchdog stays quiet before warning", result.returncode == 0 and "notify " not in cmux_log.read_text())
    watchdog_state = json.loads((worktree / ".task-watchdog.json").read_text(encoding="utf-8"))
    check("volatile statusline does not count as progress", watchdog_state["last_progress_epoch"] == 1000)
    result = run(
        WATCHDOG, "sample", "--kind", "task", "--surface", meta["task_surface"],
        "--now", "1900", cwd=worktree, env=env,
    )
    check("watchdog sends staged warning", result.returncode == 0 and cmux_log.read_text().count("notify ") == 1)
    watchdog_state = json.loads((worktree / ".task-watchdog.json").read_text(encoding="utf-8"))
    check("watchdog samples advisory CPU only on warning", watchdog_state["agent_cpu_percent"] == 0.75)
    result = run(
        WATCHDOG, "sample", "--kind", "task", "--surface", meta["task_surface"],
        "--now", "1950", cwd=worktree, env=env,
    )
    check("watchdog does not resample CPU inside warning stage", result.returncode == 0 and cmux_log.read_text().splitlines().count("top") == 1)
    result = run(
        WATCHDOG, "sample", "--kind", "task", "--surface", meta["task_surface"],
        "--now", "2200", cwd=worktree, env=env,
    )
    check("watchdog sends one stall alert", result.returncode == 0 and cmux_log.read_text().count("notify ") == 2)
    check("watchdog samples CPU once per notification stage", cmux_log.read_text().splitlines().count("top") == 2)
    run(
        WATCHDOG, "sample", "--kind", "task", "--surface", meta["task_surface"],
        "--now", "2300", cwd=worktree, env=env,
    )
    check("watchdog does not repeat an episode", cmux_log.read_text().count("notify ") == 2)
    check("watchdog does not resample CPU after alert", cmux_log.read_text().splitlines().count("top") == 2)
    screen_file.write_text("task beta\n5H 21% used | reset in 2h\n", encoding="utf-8")
    result = run(
        WATCHDOG, "sample", "--kind", "task", "--surface", meta["task_surface"],
        "--now", "2310", cwd=worktree, env=env,
    )
    watchdog_raw = (worktree / ".task-watchdog.json").read_text(encoding="utf-8")
    watchdog_state = json.loads(watchdog_raw)
    check("watchdog reports recovery once", result.returncode == 0 and cmux_log.read_text().count("notify ") == 3)
    check("watchdog clears stale episode", watchdog_state["status"] == "running" and watchdog_state["recovery_count"] == 1)
    check(
        "watchdog keeps cumulative episode counters",
        watchdog_state["warning_count"] == 1
        and watchdog_state["alert_count"] == 1
        and watchdog_state["degraded_count"] == 1
        and watchdog_state["read_failure_count"] == 3,
    )
    check("watchdog state stores no screen content", "task alpha" not in watchdog_raw and "task beta" not in watchdog_raw)

    screen_file.write_text("reviewer idle\n", encoding="utf-8")
    result = run(
        WATCHDOG, "sample", "--kind", "reviewer", "--surface", review_surface,
        "--now", "3000", "--reset", cwd=worktree, env=env,
    )
    review_watchdog = json.loads((worktree / ".review-watchdog.json").read_text(encoding="utf-8"))
    check("reviewer watchdog routes to executor", result.returncode == 0 and review_watchdog["coordinator_surface"] == meta["task_surface"])
    check("watchdog rejects a different surface", run(
        WATCHDOG, "sample", "--kind", "task", "--surface", review_surface,
        "--now", "3001", cwd=worktree, env=env,
    ).returncode == 2)

    legacy_v2 = json.loads(json.dumps(meta))
    legacy_v2.pop("watchdog_policy")
    write_json(worktree / ".task-meta.json", legacy_v2)
    result = run(
        WATCHDOG, "sample", "--kind", "task", "--surface", meta["task_surface"],
        "--now", "3100", "--reset", cwd=worktree, env=env,
    )
    disabled_state = json.loads((worktree / ".task-watchdog.json").read_text(encoding="utf-8"))
    check("older v2 watchdog stays disabled", result.returncode == 0 and disabled_state["status"] == "disabled")
    write_json(worktree / ".task-meta.json", meta)

    watchdog_process = subprocess.Popen(
        [sys.executable, str(WATCHDOG), "run", "--kind", "task", "--surface", meta["task_surface"]],
        cwd=worktree, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    for _ in range(100):
        try:
            running_state = json.loads((worktree / ".task-watchdog.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            running_state = {}
        if running_state.get("status") == "running":
            break
        time.sleep(0.01)
    watchdog_process.terminate()
    _stdout, watchdog_stderr = watchdog_process.communicate(timeout=3)
    stopped_state = json.loads((worktree / ".task-watchdog.json").read_text(encoding="utf-8"))
    check("watchdog loop starts without blocking agent", running_state.get("status") == "running")
    check("watchdog loop stops promptly with wrapper", watchdog_process.returncode == 0 and not watchdog_stderr and stopped_state["status"] == "stopped")

    gone_env = dict(env, CMUX_SCREEN_GONE="1")
    result = run(
        WATCHDOG, "sample", "--kind", "task", "--surface", meta["task_surface"],
        "--now", "3200", "--reset", cwd=worktree, env=gone_env,
    )
    gone_state = json.loads((worktree / ".task-watchdog.json").read_text(encoding="utf-8"))
    check("watchdog exits when surface is gone", result.returncode == 0 and result.stdout.strip() == "stop" and gone_state["status"] == "surface-gone")

    (worktree / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    result = run(LIFECYCLE, "request-exit", "--kind", "task", cwd=worktree, env=origin_env)
    check("dirty task stays open", result.returncode == 3)

print("\nAll unattended task lifecycle tests passed.")
