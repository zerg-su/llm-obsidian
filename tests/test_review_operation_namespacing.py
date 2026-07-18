#!/usr/bin/env python3
"""Regression coverage for concurrent v3 reviews in one project/worktree."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPAWN = ROOT / "skills" / "review-dispatch" / "scripts" / "spawn_review.py"
sys.path.insert(0, str(ROOT / "scripts"))
from task_sessions import TaskSessionStore, project_id_for


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SPAWN), *args], text=True, capture_output=True, env=env
    )


def operation_dir(output: str) -> Path:
    for line in output.splitlines():
        if line.startswith("review operation: "):
            return Path(line.removeprefix("review operation: ")).resolve()
    raise AssertionError(f"operation directory missing from output: {output}")


with tempfile.TemporaryDirectory(prefix="review-operation-test.") as raw:
    tmp = Path(raw)
    worktree = tmp / "project"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.email", "tests@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "Review Tests"], check=True)
    (worktree / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(worktree), "commit", "-qm", "fixture"], check=True)

    vault = tmp / "vault"
    plan = vault / "wiki" / "plans" / "approved.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Approved\n", encoding="utf-8")
    archive_stub = vault / "skills" / "review-dispatch" / "scripts" / "archive_review.py"
    archive_stub.parent.mkdir(parents=True)
    archive_stub.write_text("# fixture\n", encoding="utf-8")
    writer_stub = vault / "scripts" / "vault-write.py"
    writer_stub.parent.mkdir(parents=True)
    writer_stub.write_text("# fixture\n", encoding="utf-8")

    meta = {
        "version": 3,
        "project_id": project_id_for(worktree, create=True),
        "task_id": str(uuid.uuid4()),
        "task_name": "parallel-review",
        "origin_session": "coordinator-a",
        "executor_runtime": "codex",
        "runtime": "codex",
        "task_surface": "executor-surface",
        "vault_root": str(vault),
        "branch": "task/parallel-review",
        "base_branch": "HEAD",
        "plan_file": str(plan),
        "approved_plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "interaction_policy": "unattended",
        "review_policy": {
            "mode": "light",
            "max_verify_iterations": 2,
            "auto_resolve_severities": ["warning", "nit"],
            "escalate_severities": ["blocking"],
        },
        "reap_policy": {
            "mode": "final", "auto_file": True,
            "allowed_types": ["session"], "title": "Parallel review",
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
    (worktree / ".task-meta.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
    (worktree / ".task-prompt.md").write_text("# Task: parallel-review\n", encoding="utf-8")
    (worktree / ".task-cmux-surface").write_text("executor-surface\n", encoding="utf-8")

    first = run(
        "start", "--no-spawn", "--worktree", str(worktree), "--vault-root", str(vault),
        "--reviewer-runtime", "claude", "--model", "fable",
    )
    assert first.returncode == 0, first.stderr
    first_dir = operation_dir(first.stdout)
    second = run(
        "start", "--no-spawn", "--worktree", str(worktree), "--vault-root", str(vault),
        "--reviewer-runtime", "claude", "--model", "opus",
    )
    assert second.returncode == 0, second.stderr
    second_dir = operation_dir(second.stdout)
    assert first_dir != second_dir
    assert (first_dir / ".review-meta.json").is_file()
    assert (second_dir / ".review-meta.json").is_file()
    assert not (worktree / ".review-meta.json").exists()

    first_meta = json.loads((first_dir / ".review-meta.json").read_text(encoding="utf-8"))
    second_meta = json.loads((second_dir / ".review-meta.json").read_text(encoding="utf-8"))
    assert first_meta["reviewer_model"] == "fable"
    assert second_meta["reviewer_model"] == "opus"
    assert first_meta["operation_id"] != second_meta["operation_id"]
    assert f"--operation-dir {first_dir}" in first_meta["executor_callback_command"]
    assert f"--operation-dir {second_dir}" in second_meta["executor_callback_command"]

    fake_bin = tmp / "bin"
    fake_bin.mkdir()
    fake_cmux = fake_bin / "cmux"
    fake_cmux.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"read-screen\" ]; then echo 'surface not found' >&2; exit 1; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_cmux.chmod(0o755)
    watchdog_env = dict(os.environ)
    watchdog_env["PATH"] = str(fake_bin) + os.pathsep + watchdog_env.get("PATH", "")
    gone = subprocess.run(
        [
            sys.executable, str(ROOT / "scripts" / "cmux_task_watchdog.py"), "sample",
            "--worktree", str(worktree), "--state-dir", str(second_dir),
            "--kind", "reviewer", "--surface", second_meta["review_surface"], "--now", "1000",
        ],
        text=True, capture_output=True, env=watchdog_env,
    )
    assert gone.returncode == 0 and gone.stdout.strip() == "stop", (
        f"stdout={gone.stdout!r} stderr={gone.stderr!r}"
    )
    gone_operation = json.loads((second_dir / "operation.json").read_text(encoding="utf-8"))
    gone_lane = TaskSessionStore(vault).lane_state(
        meta["project_id"], meta["task_id"], second_meta["lane_id"]
    )
    assert gone_operation["status"] == "failed"
    assert gone_lane["active_operation_id"] is None

    queued = run(
        "start", "--no-spawn", "--worktree", str(worktree), "--vault-root", str(vault),
        "--reviewer-runtime", "claude", "--model", "fable",
    )
    assert queued.returncode == 0, queued.stderr
    assert "review queued on busy lane:" in queued.stdout
    queued_dir = operation_dir(queued.stdout)
    assert queued_dir != first_dir
    assert (first_dir / ".review-meta.json").read_text(encoding="utf-8") == json.dumps(first_meta, ensure_ascii=False, indent=2) + "\n"
    assert not (queued_dir / ".review-meta.json").exists()

    payload = {
        "schema_version": 1,
        "run_id": first_meta["run_id"],
        "mode": "light",
        "verdict": "approve",
        "findings": [],
        "verification_gaps": [],
        "notes_for_executor": [],
        "residual_risks": [],
    }
    relay = first_dir / ".review-callback.json"
    relay.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    received = run(
        "receive", "--worktree", str(worktree), "--operation-dir", str(first_dir),
        "--relay-file", str(relay),
    )
    assert received.returncode == 0, received.stderr
    assert (first_dir / ".task-review.json").is_file()
    assert not (second_dir / ".task-review.json").exists()

    store = TaskSessionStore(vault)
    store.transition_operation(
        meta["project_id"], meta["task_id"], first_meta["lane_id"],
        first_meta["operation_id"], "complete",
        checkpoint={
            "kind": "claude", "checkpoint_id": "checkpoint-review-1",
            "cwd": first_meta["review_runtime_dir"],
        },
    )
    resumed = run(
        "start", "--no-spawn", "--worktree", str(worktree), "--vault-root", str(vault),
        "--operation-id", queued_dir.name,
        "--reviewer-runtime", "claude", "--model", "fable",
    )
    assert resumed.returncode == 0, resumed.stderr
    resumed_spec = json.loads((queued_dir / ".review-agent-command.json").read_text(encoding="utf-8"))
    resume_index = resumed_spec["argv"].index("--resume")
    assert resumed_spec["argv"][resume_index + 1] == "checkpoint-review-1"
    resumed_meta = json.loads((queued_dir / ".review-meta.json").read_text(encoding="utf-8"))
    assert resumed_meta["reviewer_effort"] == "high"

print("review operation namespacing tests passed")
