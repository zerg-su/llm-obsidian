#!/usr/bin/env python3
"""Hermetic task-summary callback and coordinator wake regressions."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.adapters.process import ProcessAdapter
from harness.contracts import OperationSpec, RuntimeRoute
from harness.runtime_sessions import RuntimeSessionRequest
from harness.runtime_worker import run as run_worker
from harness.store import OperationStore
from harness.verification import load_profiles
from harness.workflows.review import ReviewContext
from harness.workflows.review_gate import ReviewGateController, ReviewPreset


ORIGIN = "11111111-1111-1111-1111-111111111111"
CHILD = "22222222-2222-2222-2222-222222222222"
PROJECT = "33333333-3333-4333-8333-333333333333"
TASK = "44444444-4444-4444-8444-444444444444"
INVALID_TASK = "55555555-5555-4555-8555-555555555555"
BLOCKED_TASK = "66666666-6666-4666-8666-666666666666"


def check(label: str, value: bool, detail: object = "") -> None:
    if not value:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


class FakeCmux:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.keys: list[tuple[str, str]] = []

    def send(self, surface_id: str, text: str) -> None:
        self.sent.append((surface_id, text))

    def send_key(self, surface_id: str, key: str) -> None:
        self.keys.append((surface_id, key))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def dispatch_record(store: OperationStore, operation_id: str) -> None:
    route = RuntimeRoute("codex", "gpt-5.6-sol", "high", "executor", "a" * 64)
    store.create(
        OperationSpec(
            operation_id,
            f"key-{operation_id}",
            "dispatch",
            "owner-1",
            route,
            "packets/task.json",
            "scoped",
        ),
        lane_id="lane-1",
        run_id=f"run-{operation_id}",
    )
    for state in ("preflight", "starting", "running", "awaiting-callback"):
        store.transition("owner-1", operation_id, state)


def task_meta(
    vault: Path,
    worktree: Path,
    plan: Path,
    task_id: str,
    profile_sha: str,
) -> dict[str, object]:
    return {
        "version": 3,
        "project_id": PROJECT,
        "task_id": task_id,
        "task_name": "Runtime summary",
        "origin_session": "coordinator-session",
        "executor_runtime": "codex",
        "interaction_policy": "unattended",
        "plan_file": str(plan),
        "approved_plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "vault_root": str(vault),
        "review_policy": {
            "mode": "skip",
            "cross_model": False,
            "runtime": "",
            "model": "",
            "effort": "",
            "max_verify_iterations": 0,
            "verification_profile": "scoped",
            "verification_profile_sha256": profile_sha,
            "auto_resolve_severities": ["warning", "nit"],
            "escalate_severities": ["blocking"],
        },
        "reap_policy": {
            "mode": "final",
            "auto_file": True,
            "allowed_types": ["session"],
            "title": "Runtime Result",
        },
        "surface_policy": {"auto_close": True, "placement": "split"},
        "watchdog_policy": {
            "enabled": True,
            "poll_seconds": 30,
            "warn_after_seconds": 900,
            "alert_after_seconds": 1200,
        },
        "forbidden_actions": [
            "push",
            "deploy",
            "publish",
            "delete-worktree",
            "delete-branch",
            "expand-scope",
        ],
        "task_surface": CHILD,
        "worktree": str(worktree),
    }


def run_case(
    root: Path,
    operation_id: str,
    summary: object,
    *,
    review_state: str = "skipped",
) -> tuple[
    OperationStore, FakeCmux, Path, int
]:
    vault = root / f"vault-{operation_id}"
    worktree = root / f"worktree-{operation_id}"
    (vault / "wiki" / "plans").mkdir(parents=True)
    (vault / "scripts").mkdir()
    (vault / "config").mkdir()
    shutil.copy2(
        ROOT / "config" / "verification-profiles.toml",
        vault / "config" / "verification-profiles.toml",
    )
    (vault / "scripts" / "reap-runner.py").write_text(
        "#!/usr/bin/env python3\n", encoding="utf-8"
    )
    worktree.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "runtime@example.invalid"],
        cwd=worktree,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Runtime Summary Test"],
        cwd=worktree,
        check=True,
    )
    (worktree / "product.txt").write_text("ready\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.txt"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "ready"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    plan = vault / "wiki" / "plans" / "approved.md"
    plan.write_text("# Approved\n", encoding="utf-8")
    write_json(
        vault
        / ".vault-meta"
        / "task-sessions"
        / "session-bindings"
        / "coordinator-session"
        / "binding.json",
        {
            "session_id": "coordinator-session",
            "project_id": PROJECT,
            "task_id": operation_id,
        },
    )
    store = OperationStore(vault / ".vault-meta" / "harness")
    dispatch_record(store, operation_id)
    profile_sha = load_profiles(
        vault / "config" / "verification-profiles.toml"
    )["scoped"].sha256
    meta = task_meta(vault, worktree, plan, operation_id, profile_sha)
    write_json(worktree / ".task-meta.json", meta)
    if review_state == "skipped":
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        ReviewGateController.skip(
            store.root / "review-data" / operation_id / operation_id,
            dispatch_operation_id=operation_id,
            owner_id=operation_id,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                profile_sha,
            ),
            product_root=worktree,
        )
    (worktree / ".task-prompt.md").write_text(
        "Complete the approved task and write the canonical summary.",
        encoding="utf-8",
    )
    parent = store.read("owner-1", operation_id)
    request = RuntimeSessionRequest(
        parent.spec,
        parent.lane_id,
        parent.run_id,
        ORIGIN,
        worktree,
        ".task-prompt.md",
        ".task-summary.json",
        callback_mode="task-summary",
        task_summary_pointer=".task-summary.json",
    )
    check(
        "request carries canonical task-summary mode without a wake command",
        request.callback_mode == "task-summary"
        and request.task_summary_pointer == ".task-summary.json"
        and not hasattr(request, "wake_message"),
    )
    provider = root / f"provider-{operation_id}.py"
    provider.write_text(
        "import json,pathlib,sys,time\n"
        "pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')\n"
        "time.sleep(0.3)\n",
        encoding="utf-8",
    )
    summary_path = worktree / ".task-summary.json"
    launch = ProcessAdapter().prepare_surface_launch(
        argv=(
            str(Path(sys.executable).resolve()),
            str(provider),
            str(summary_path),
            json.dumps(summary, sort_keys=True),
        ),
        cwd=worktree,
        state_root=root / f"state-{operation_id}",
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=summary_path,
        store_root=store.root,
        owner_id="owner-1",
        operation_id=operation_id,
        run_id=f"run-{operation_id}",
        surface_id=CHILD,
        runtime="codex",
        callback_mode="task-summary",
        task_summary_pointer=summary_path,
        origin_surface=ORIGIN,
    )
    cmux = FakeCmux()
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            run_worker(
                launch.spec_path,
                poll_seconds=0.02,
                checkpoint_probe=lambda _surface, _runtime: "checkpoint-1",
                cmux_adapter=cmux,
            )
        )
    )
    thread.start()
    if review_state == "delayed-skip":
        import time

        # The provider exits after 0.3s; approval arrives later. The runtime
        # worker must remain alive as the code-owned finalization watcher.
        time.sleep(0.45)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        ReviewGateController.skip(
            store.root / "review-data" / operation_id / operation_id,
            dispatch_operation_id=operation_id,
            owner_id=operation_id,
            preset=ReviewPreset.from_flags(no_review=True),
            context=ReviewContext(
                "packets/task/manifest.json",
                head,
                "scoped",
                profile_sha,
            ),
            product_root=worktree,
        )
    thread.join(timeout=3)
    return store, cmux, launch.spec_path.parent, result[0]


with tempfile.TemporaryDirectory(prefix="runtime-task-summary.") as raw:
    root = Path(raw)
    valid_summary = {
        "schema_version": 1,
        "type": "session",
        "title": "Runtime Result",
        "session": "executor-session",
        "body": "Bounded completed task.",
    }
    store, cmux, state, rc = run_case(root, TASK, valid_summary)
    record = store.read("owner-1", TASK)
    check(
        "valid canonical v3 summary becomes one durable parent callback",
        rc == 0
        and record.state == "finalizing"
        and record.accepted_callback_kind == "wiki-summary"
        and record.accepted_callback_id.startswith("wiki-summary-"),
        record,
    )
    check(
        "accepted receipt wakes only the exact origin with code-owned reap command",
        len(cmux.sent) == 1
        and cmux.sent[0][0] == ORIGIN
        and "Typed final task summary callback was accepted" in cmux.sent[0][1]
        and "scripts/reap-runner.py" in cmux.sent[0][1]
        and str(root / f"vault-{TASK}") in cmux.sent[0][1]
        and str(root / f"worktree-{TASK}") in cmux.sent[0][1]
        and "Bounded completed task" not in cmux.sent[0][1]
        and cmux.keys == [(ORIGIN, "Enter")],
        (cmux.sent, cmux.keys),
    )
    sent_marker = json.loads(
        (state / "task-summary-notify.json").read_text(encoding="utf-8")
    )
    check(
        "notification marker records exact durable callback before send completion",
        sent_marker["status"] == "sent"
        and sent_marker["callback_id"] == record.accepted_callback_id,
        sent_marker,
    )

    # A restarted worker sees a duplicate durable callback and the sent marker;
    # it must not wake the coordinator twice.
    launch = ProcessAdapter().prepare_surface_launch(
        argv=(
            str(Path(sys.executable).resolve()),
            "-c",
            "import time; time.sleep(0.15)",
        ),
        cwd=root / f"worktree-{TASK}",
        state_root=state,
        worker=ROOT / "scripts" / "harness-runtime-worker.py",
        callback_pointer=root
        / f"worktree-{TASK}"
        / ".task-summary.json",
        store_root=store.root,
        owner_id="owner-1",
        operation_id=TASK,
        run_id=f"run-{TASK}",
        surface_id=CHILD,
        runtime="codex",
        callback_mode="task-summary",
        task_summary_pointer=root
        / f"worktree-{TASK}"
        / ".task-summary.json",
        origin_surface=ORIGIN,
    )
    second_rc = run_worker(
        launch.spec_path,
        poll_seconds=0.02,
        checkpoint_probe=lambda _surface, _runtime: "checkpoint-1",
        cmux_adapter=cmux,
    )
    check(
        "duplicate summary callback never duplicates coordinator notification",
        second_rc == 0 and len(cmux.sent) == 1 and len(cmux.keys) == 1,
        (cmux.sent, cmux.keys),
    )

    invalid = {**valid_summary, "title": "Unapproved title"}
    invalid_store, invalid_cmux, invalid_state, invalid_rc = run_case(
        root, INVALID_TASK, invalid
    )
    invalid_record = invalid_store.read("owner-1", INVALID_TASK)
    check(
        "invalid handoff becomes attention and never notifies coordinator",
        invalid_rc == 0
        and invalid_record.state == "attention-required"
        and not invalid_record.accepted_callback_id
        and invalid_cmux.sent == []
        and invalid_cmux.keys == []
        and not (invalid_state / "task-summary-notify.json").exists(),
        invalid_record,
    )

    delayed_task = BLOCKED_TASK
    delayed_store, delayed_cmux, _delayed_state, delayed_rc = run_case(
        root, delayed_task, valid_summary, review_state="delayed-skip"
    )
    delayed_record = delayed_store.read("owner-1", delayed_task)
    check(
        "worker outlives provider and accepts once typed review state arrives",
        delayed_rc == 0
        and delayed_record.state == "finalizing"
        and delayed_record.accepted_callback_kind == "wiki-summary"
        and len(delayed_cmux.sent) == 1,
        delayed_record,
    )
