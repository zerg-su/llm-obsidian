#!/usr/bin/env python3
"""Hermetic task-session identity, concurrency, lifecycle, and cmux tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from task_sessions import (
    TaskSessionError,
    TaskSessionStore,
    capture_resume,
    cmux_capabilities,
    lane_id_for,
    project_id_for,
    spawn_right,
)
from task_contract import v3_session_is_bound


passed = 0


def check(label: str, condition: bool) -> None:
    global passed
    if not condition:
        raise AssertionError(label)
    passed += 1


class Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


with tempfile.TemporaryDirectory() as raw:
    tmp = Path(raw)
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "tests@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Task Session Tests"], check=True)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "fixture"], check=True)
    vault = tmp / "vault"
    (vault / "wiki").mkdir(parents=True)
    project = project_id_for(repo, create=True)
    check("project UUID", str(uuid.UUID(project)) == project)
    linked = tmp / "linked"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(linked)], check=True, capture_output=True)
    check("linked worktree shares project", project_id_for(linked, create=False) == project)

    store = TaskSessionStore(vault)
    task = str(uuid.uuid4())
    store.create_task(project, task, worktree=repo)
    binding_a = store.bind_session(project, task, runtime="codex", session_id="session-a", explicit=True)
    binding_b = store.bind_session(project, task, runtime="claude", session_id="session-b", explicit=True)
    check("multiple coordinator bindings", binding_a["task_id"] == binding_b["task_id"] == task)

    cli_task = str(uuid.uuid4())
    initialized = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "task_sessions.py"),
         "--vault-root", str(vault), "init-task", "--worktree", str(linked),
         "--task-id", cli_task, "--runtime", "codex", "--session-id", "session-cli"],
        text=True, capture_output=True, check=True,
    )
    initialized_value = json.loads(initialized.stdout)
    check("init-task returns exact identities", initialized_value == {"project_id": project, "task_id": cli_task})
    pointer = json.loads((linked / ".task-session-binding.json").read_text(encoding="utf-8"))
    check("init-task writes exact pointer", pointer["project_id"] == project and pointer["task_id"] == cli_task)
    binding_meta = {"version": 3, "vault_root": str(vault), "project_id": project, "task_id": cli_task}
    check("v3 exact coordinator binding accepted", v3_session_is_bound(binding_meta, "session-cli"))
    check("v3 unrelated coordinator rejected", not v3_session_is_bound(binding_meta, "session-other"))
    ensured = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "task_sessions.py"),
         "--vault-root", str(vault), "ensure-session-task", "--worktree", str(repo),
         "--runtime", "codex", "--session-id", "session-lazy"],
        text=True, capture_output=True, check=True,
    )
    ensured_again = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "task_sessions.py"),
         "--vault-root", str(vault), "ensure-session-task", "--worktree", str(repo),
         "--runtime", "codex", "--session-id", "session-lazy"],
        text=True, capture_output=True, check=True,
    )
    check("primary session lazily keeps exact task", json.loads(ensured.stdout) == json.loads(ensured_again.stdout))

    race_task = str(uuid.uuid4())
    create_errors: list[Exception] = []
    def create_same_task() -> None:
        try:
            store.create_task(project, race_task, worktree=repo)
        except Exception as exc:
            create_errors.append(exc)
    create_threads = [threading.Thread(target=create_same_task) for _ in range(8)]
    for thread in create_threads:
        thread.start()
    for thread in create_threads:
        thread.join()
    check("concurrent task creation is idempotent", not create_errors)
    store.archive_task(project, race_task)

    enqueue_reap_task = str(uuid.uuid4())
    store.create_task(project, enqueue_reap_task, worktree=repo)
    race_barrier = threading.Barrier(2)
    race_outcomes: list[str] = []
    race_operation = str(uuid.uuid4())

    def race_enqueue() -> None:
        race_barrier.wait()
        try:
            store.enqueue_operation(
                project, enqueue_reap_task, domain="review", runtime="claude",
                model="fable", effort="high", operation_type="review",
                coordinator_surface="surface-race", operation_id=race_operation,
            )
            race_outcomes.append("enqueue")
        except TaskSessionError:
            race_outcomes.append("enqueue-rejected")

    def race_reap() -> None:
        race_barrier.wait()
        try:
            store.archive_task(project, enqueue_reap_task)
            race_outcomes.append("archive")
        except TaskSessionError:
            race_outcomes.append("archive-rejected")

    enqueue_thread = threading.Thread(target=race_enqueue)
    reap_thread = threading.Thread(target=race_reap)
    enqueue_thread.start()
    reap_thread.start()
    enqueue_thread.join()
    reap_thread.join()
    race_state = json.loads(
        store.task_path(project, enqueue_reap_task).read_text(encoding="utf-8")
    )
    check(
        "enqueue versus reap is serialized without lost work",
        (
            set(race_outcomes) == {"archive", "enqueue-rejected"}
            and race_state["status"] == "archived"
        )
        or (
            set(race_outcomes) == {"enqueue", "archive-rejected"}
            and race_state["status"] == "active"
        ),
    )

    review_lane = lane_id_for(project, task, "review", "claude", "fable")
    review_lane_xhigh = lane_id_for(project, task, "review", "claude", "fable")
    other_model_lane = lane_id_for(project, task, "review", "claude", "opus")
    check("effort excluded from lane", review_lane == review_lane_xhigh)
    check("model changes lane", review_lane != other_model_lane)
    check("permission domain changes lane", review_lane != lane_id_for(project, task, "normal", "claude", "fable"))

    operation_id = str(uuid.uuid4())
    first = store.enqueue_operation(
        project, task, domain="review", runtime="claude", model="fable", effort="high",
        operation_type="review", coordinator_surface="surface-a", operation_id=operation_id,
    )
    duplicate = store.enqueue_operation(
        project, task, domain="review", runtime="claude", model="fable", effort="high",
        operation_type="review", coordinator_surface="surface-a", operation_id=operation_id,
    )
    check("duplicate operation idempotent", first == duplicate)
    second_id = str(uuid.uuid4())
    store.enqueue_operation(
        project, task, domain="review", runtime="claude", model="fable", effort="xhigh",
        operation_type="review", coordinator_surface="surface-b", operation_id=second_id,
    )
    claimed = store.claim_next(project, task, review_lane)
    check("FIFO claims first", claimed is not None and claimed["operation_id"] == operation_id)
    check("busy lane does not double claim", store.claim_next(project, task, review_lane) is None)
    store.transition_operation(project, task, review_lane, operation_id, "failed", degradation="process exited")
    claimed_second = store.claim_next(project, task, review_lane)
    check("failed operation drains queue", claimed_second is not None and claimed_second["operation_id"] == second_id)
    stale = store.transition_operation(project, task, review_lane, operation_id, "failed")
    still_active = store.lane_state(project, task, review_lane)
    check(
        "stale terminal callback cannot release the next operation",
        stale["status"] == "failed" and still_active["active_operation_id"] == second_id,
    )
    try:
        store.transition_operation(project, task, review_lane, operation_id, "complete")
    except TaskSessionError:
        pass
    else:
        raise AssertionError("terminal failed operation changed to complete")
    passed += 1
    store.transition_operation(project, task, review_lane, second_id, "complete")

    concurrent_id = str(uuid.uuid4())
    values: list[str] = []
    errors: list[Exception] = []
    def enqueue_same() -> None:
        try:
            result = store.enqueue_operation(
                project, task, domain="review", runtime="codex", model="gpt-test", effort="high",
                operation_type="review", coordinator_surface="surface-c", operation_id=concurrent_id,
            )
            values.append(result["operation_id"])
        except Exception as exc:  # pragma: no cover - diagnostic path
            errors.append(exc)
    threads = [threading.Thread(target=enqueue_same) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check("concurrent enqueue has no errors", not errors)
    check("concurrent enqueue idempotent", values == [concurrent_id] * 8)
    codex_lane = lane_id_for(project, task, "review", "codex", "gpt-test")
    claimed_concurrent = store.claim_next(project, task, codex_lane)
    check("concurrent operation queued once", claimed_concurrent is not None and claimed_concurrent["operation_id"] == concurrent_id)
    store.transition_operation(project, task, codex_lane, concurrent_id, "complete")

    archived = store.archive_task(project, task)
    check("reap archives task", archived["status"] == "archived")
    try:
        store.enqueue_operation(
            project, task, domain="review", runtime="claude", model="fable", effort="high",
            operation_type="review", coordinator_surface="surface-a",
        )
    except TaskSessionError:
        pass
    else:
        raise AssertionError("archived task accepted enqueue")
    passed += 1
    archived_values: list[str] = []
    archive_errors: list[Exception] = []
    def archive_again() -> None:
        try:
            archived_values.append(store.archive_task(project, task)["status"])
        except Exception as exc:
            archive_errors.append(exc)
    archive_threads = [threading.Thread(target=archive_again) for _ in range(6)]
    for thread in archive_threads:
        thread.start()
    for thread in archive_threads:
        thread.join()
    check("concurrent reap is idempotent", not archive_errors and archived_values == ["archived"] * 6)

    corrupt_task = str(uuid.uuid4())
    store.create_task(project, corrupt_task, worktree=repo)
    corrupt_op = store.enqueue_operation(
        project, corrupt_task, domain="review", runtime="codex", model="gpt-corrupt",
        effort="high", operation_type="review", coordinator_surface="surface-corrupt",
    )
    corrupt_lane = store.lane_dir(project, corrupt_task, corrupt_op["lane_id"])
    (corrupt_lane / "lane.json").write_text("{not-json", encoding="utf-8")
    try:
        store.lane_state(project, corrupt_task, corrupt_op["lane_id"])
    except TaskSessionError:
        pass
    else:
        raise AssertionError("corrupt lane JSON was accepted")
    passed += 1


def fake_cmux(args: list[str], **_: object) -> Result:
    joined = " ".join(args)
    if args[-2:] == ["new-split", "--help"] or args == ["cmux", "new-split", "--help"]:
        return Result(stdout="Usage --surface <id> --focus")
    if args == ["cmux", "surface", "resume", "--help"]:
        return Result(stdout="resume get resume set resume show resume clear")
    if "new-split" in args:
        check("anchored argv", "--surface" in args and args[args.index("--surface") + 1] == "origin-1")
        return Result(stdout="OK surface:9 (11111111-1111-4111-8111-111111111111)")
    if args[:5] == ["cmux", "surface", "resume", "get", "--json"]:
        return Result(stdout=json.dumps({"resume_binding": {
            "kind": "codex", "checkpoint_id": "checkpoint-1", "cwd": "/tmp",
            "command": "malicious shell text must be ignored",
        }}))
    return Result(returncode=1)


caps = cmux_capabilities(fake_cmux)
check("cmux capabilities", caps["anchored_split"] and caps["typed_resume"])
spawn = spawn_right("origin-1", fake_cmux)
check("spawn returns exact surface", spawn["surface"] == "11111111-1111-4111-8111-111111111111")
checkpoint = capture_resume("surface-1", "codex", fake_cmux)
check(
    "checkpoint ignores stored command",
    checkpoint == {"kind": "codex", "checkpoint_id": "checkpoint-1", "cwd": str(Path("/tmp").resolve())},
)

print(f"task session tests passed: {passed}")
