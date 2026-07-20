#!/usr/bin/env python3
"""Hermetic task-session identity, concurrency, lifecycle, and cmux tests."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import task_sessions as task_sessions_module
from task_sessions import (
    TaskSessionError,
    TaskSessionStore,
    capture_resume,
    close_surface_exact,
    cmux_capabilities,
    lane_id_for,
    project_id_for,
    spawn_right,
    surface_context,
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
    existing_owner_only = tmp / "existing-owner-only"
    existing_owner_only.mkdir(mode=0o700)
    with mock.patch.object(Path, "mkdir", side_effect=AssertionError("unexpected mkdir")), \
         mock.patch.object(Path, "chmod", side_effect=AssertionError("unexpected chmod")):
        check(
            "existing owner-only registry needs no parent mutation",
            task_sessions_module.ensure_owner_only_dir(existing_owner_only) == existing_owner_only,
        )
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
    linked_mode = stat.S_IMODE(linked.stat().st_mode)
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
    check("init-task preserves product worktree mode", stat.S_IMODE(linked.stat().st_mode) == linked_mode)
    check(
        "init-task pointer is owner-only",
        stat.S_IMODE((linked / ".task-session-binding.json").stat().st_mode) == 0o600,
    )
    binding_meta = {"version": 3, "vault_root": str(vault), "project_id": project, "task_id": cli_task}
    check("v3 exact coordinator binding accepted", v3_session_is_bound(binding_meta, "session-cli"))
    check("v3 unrelated coordinator rejected", not v3_session_is_bound(binding_meta, "session-other"))
    parallel_task = str(uuid.uuid4())
    store.create_task(project, parallel_task, worktree=repo)
    store.bind_session(
        project, cli_task, runtime="codex", session_id="session-parallel", explicit=True
    )
    store.bind_session(
        project, parallel_task, runtime="codex", session_id="session-parallel", explicit=True
    )
    parallel_meta = {
        "version": 3, "vault_root": str(vault), "project_id": project,
        "task_id": parallel_task,
    }
    check(
        "one coordinator session binds multiple explicit tasks",
        v3_session_is_bound(
            {**binding_meta, "task_id": cli_task}, "session-parallel"
        )
        and v3_session_is_bound(parallel_meta, "session-parallel")
        and len(store.session_bindings(runtime="codex", session_id="session-parallel")) == 2,
    )
    ambiguous = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "task_sessions.py"),
         "--vault-root", str(vault), "ensure-session-task", "--worktree", str(repo),
         "--runtime", "codex", "--session-id", "session-parallel"],
        text=True, capture_output=True, check=False,
    )
    check(
        "implicit routing fails closed for multiple active tasks",
        ambiguous.returncode == 3 and "multiple active tasks" in ambiguous.stderr,
    )
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
    check(
        "exact claimant does not steal a foreign FIFO head",
        store.claim_next(project, task, review_lane, second_id) is None
        and store.lane_state(project, task, review_lane)["queue"] == [operation_id, second_id],
    )
    try:
        store.transition_operation(project, task, review_lane, second_id, "running")
    except TaskSessionError:
        pass
    else:
        raise AssertionError("non-active queued operation transitioned")
    passed += 1
    claimed = store.claim_next(project, task, review_lane, operation_id)
    check("FIFO claims first", claimed is not None and claimed["operation_id"] == operation_id)
    check("busy lane does not double claim", store.claim_next(project, task, review_lane) is None)
    store.transition_operation(project, task, review_lane, operation_id, "failed", degradation="process exited")
    claimed_second = store.claim_next(project, task, review_lane, second_id)
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

    recovery_task = str(uuid.uuid4())
    store.create_task(project, recovery_task, worktree=repo)
    recovery_first = store.enqueue_operation(
        project, recovery_task, domain="review", runtime="claude", model="fable",
        effort="high", operation_type="review", coordinator_surface="surface-recovery",
    )
    recovery_second = store.enqueue_operation(
        project, recovery_task, domain="review", runtime="claude", model="fable",
        effort="high", operation_type="review", coordinator_surface="surface-recovery",
    )
    recovery_lane = str(recovery_first["lane_id"])
    store.claim_next(
        project, recovery_task, recovery_lane, str(recovery_first["operation_id"])
    )
    recovered = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "task_sessions.py"),
            "--vault-root",
            str(vault),
            "fail-operation",
            "--project-id",
            project,
            "--task-id",
            recovery_task,
            "--lane-id",
            recovery_lane,
            "--operation-id",
            str(recovery_first["operation_id"]),
            "--reason",
            "launcher failed",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    recovered_value = json.loads(recovered.stdout)
    recovered_lane = store.lane_state(project, recovery_task, recovery_lane)
    check(
        "exact fail-operation releases only the claimed operation",
        recovered_value["status"] == "failed"
        and recovered_value["next_operation_id"] == recovery_second["operation_id"]
        and recovered_lane["active_operation_id"] is None
        and recovered_lane["queue"] == [recovery_second["operation_id"]],
    )
    recovered_again = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "task_sessions.py"),
            "--vault-root",
            str(vault),
            "fail-operation",
            "--project-id",
            project,
            "--task-id",
            recovery_task,
            "--lane-id",
            recovery_lane,
            "--operation-id",
            str(recovery_first["operation_id"]),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    check("exact fail-operation retry is idempotent", json.loads(recovered_again.stdout)["status"] == "failed")
    claimed_recovery_second = store.claim_next(
        project, recovery_task, recovery_lane, str(recovery_second["operation_id"])
    )
    check(
        "released lane accepts the next queued operation",
        claimed_recovery_second is not None
        and claimed_recovery_second["operation_id"] == recovery_second["operation_id"],
    )
    store.transition_operation(
        project, recovery_task, recovery_lane, str(recovery_second["operation_id"]), "complete"
    )

    interrupted_transition_task = str(uuid.uuid4())
    store.create_task(project, interrupted_transition_task, worktree=repo)
    interrupted_operation = store.enqueue_operation(
        project, interrupted_transition_task, domain="review", runtime="codex",
        model="gpt-test", effort="high", operation_type="review",
        coordinator_surface="surface-interrupted",
    )
    interrupted_lane = str(interrupted_operation["lane_id"])
    store.claim_next(
        project, interrupted_transition_task, interrupted_lane,
        str(interrupted_operation["operation_id"]),
    )
    real_atomic_write = task_sessions_module.atomic_write
    failed_terminal_lane_write = False

    def fail_terminal_lane_write(path: Path, value: dict[str, object]) -> None:
        global failed_terminal_lane_write
        if (
            path.name == "lane.json"
            and value.get("active_operation_id") is None
            and not failed_terminal_lane_write
        ):
            failed_terminal_lane_write = True
            raise OSError("injected terminal lane write failure")
        real_atomic_write(path, value)

    task_sessions_module.atomic_write = fail_terminal_lane_write
    try:
        try:
            store.transition_operation(
                project, interrupted_transition_task, interrupted_lane,
                str(interrupted_operation["operation_id"]), "complete",
            )
        except OSError:
            pass
        else:
            raise AssertionError("injected terminal lane write failure was accepted")
    finally:
        task_sessions_module.atomic_write = real_atomic_write
    interrupted_before_retry = store.lane_state(
        project, interrupted_transition_task, interrupted_lane
    )
    check(
        "interrupted terminal transition preserves exact active identity",
        interrupted_before_retry["active_operation_id"] == interrupted_operation["operation_id"],
    )
    store.transition_operation(
        project, interrupted_transition_task, interrupted_lane,
        str(interrupted_operation["operation_id"]), "complete",
    )
    interrupted_after_retry = store.lane_state(
        project, interrupted_transition_task, interrupted_lane
    )
    check(
        "terminal transition retry repairs the lane",
        interrupted_after_retry["active_operation_id"] is None
        and interrupted_after_retry["status"] == "idle",
    )

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
    claimed_concurrent = store.claim_next(project, task, codex_lane, concurrent_id)
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
    rebound = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "task_sessions.py"),
         "--vault-root", str(vault), "ensure-session-task", "--worktree", str(repo),
         "--runtime", "codex", "--session-id", "session-a"],
        text=True, capture_output=True, check=True,
    )
    rebound_value = json.loads(rebound.stdout)
    check(
        "archived session starts a fresh task without guessing",
        rebound_value["project_id"] == project and rebound_value["task_id"] != task,
    )

    partial_task = str(uuid.uuid4())
    store.create_task(project, partial_task, worktree=repo)
    partial_op = store.enqueue_operation(
        project, partial_task, domain="review", runtime="claude", model="fable",
        effort="high", operation_type="review", coordinator_surface="surface-partial",
    )
    partial_lane = str(partial_op["lane_id"])
    claimed_partial = store.claim_next(project, partial_task, partial_lane)
    assert claimed_partial is not None
    store.transition_operation(
        project, partial_task, partial_lane, str(partial_op["operation_id"]), "complete"
    )
    real_atomic_write = task_sessions_module.atomic_write
    failed_once = False

    def fail_first_lane_archive(path: Path, value: dict[str, object]) -> None:
        global failed_once
        if path.name == "lane.json" and value.get("status") == "archived" and not failed_once:
            failed_once = True
            raise OSError("injected lane archive failure")
        real_atomic_write(path, value)

    task_sessions_module.atomic_write = fail_first_lane_archive
    try:
        try:
            store.archive_task(project, partial_task)
        except TaskSessionError:
            pass
        else:
            raise AssertionError("injected archive failure was accepted")
    finally:
        task_sessions_module.atomic_write = real_atomic_write
    partial_state = json.loads(store.task_path(project, partial_task).read_text(encoding="utf-8"))
    check(
        "archive failure returns task to a retryable contained state",
        partial_state["status"] == "active" and partial_state["archive_failure"] == "pre-lane-archive",
    )
    check("contained archive retry succeeds", store.archive_task(project, partial_task)["status"] == "archived")

    interrupted_task = str(uuid.uuid4())
    store.create_task(project, interrupted_task, worktree=repo)
    interrupted_path = store.task_path(project, interrupted_task)
    interrupted_state = json.loads(interrupted_path.read_text(encoding="utf-8"))
    interrupted_state["status"] = "archiving"
    task_sessions_module.atomic_write(interrupted_path, interrupted_state)
    check(
        "archive resumes after process loss in archiving state",
        store.archive_task(project, interrupted_task)["status"] == "archived",
    )

    for broken_kind in ("missing", "corrupt"):
        broken_task = str(uuid.uuid4())
        store.create_task(project, broken_task, worktree=repo)
        broken_first = store.enqueue_operation(
            project, broken_task, domain="review", runtime="codex",
            model=f"gpt-broken-{broken_kind}", effort="high", operation_type="review",
            coordinator_surface="surface-broken",
        )
        broken_second = store.enqueue_operation(
            project, broken_task, domain="review", runtime="codex",
            model=f"gpt-broken-{broken_kind}", effort="high", operation_type="review",
            coordinator_surface="surface-broken",
        )
        broken_first_path = Path(str(broken_first["operation_dir"])) / "operation.json"
        if broken_kind == "missing":
            broken_first_path.unlink()
        else:
            broken_first_path.write_text("{not-json", encoding="utf-8")
        recovered = store.claim_next(
            project, broken_task, str(broken_second["lane_id"]),
            str(broken_second["operation_id"]),
        )
        recovered_lane = store.lane_state(project, broken_task, str(broken_second["lane_id"]))
        check(
            f"{broken_kind} FIFO head is tombstoned without blocking the lane",
            recovered is not None
            and recovered["operation_id"] == broken_second["operation_id"]
            and recovered_lane["discarded_queue_entries"][-1]["operation_id"]
            == broken_first["operation_id"],
        )
        store.transition_operation(
            project, broken_task, str(broken_second["lane_id"]),
            str(broken_second["operation_id"]), "complete",
        )

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


def cmux_workspace_retry(args: list[str], **_: object) -> Result:
    if args == ["cmux", "new-split", "--help"]:
        return Result(stdout="Usage --surface <id> --focus")
    if args[:5] == ["cmux", "--id-format", "both", "new-split", "right"]:
        if "--workspace" not in args:
            return Result(returncode=1, stderr="Error: not_found: Surface not found")
        check(
            "workspace retry keeps exact anchors",
            args[args.index("--workspace") + 1] == "workspace-1"
            and args[args.index("--surface") + 1] == "origin-1",
        )
        return Result(stdout="OK surface:10 (22222222-2222-4222-8222-222222222222)")
    if args == ["cmux", "rpc", "system.tree", '{"all":true}']:
        return Result(stdout=json.dumps({
            "windows": [{"workspaces": [{
                "id": "workspace-1",
                "panes": [{"surfaces": [{"id": "origin-1", "ref": "surface:1"}]}],
            }]}],
        }))
    return Result(returncode=1)


retry_spawn = spawn_right("origin-1", cmux_workspace_retry)
check(
    "spawn retries with exact workspace for current cmux",
    retry_spawn["surface"] == "22222222-2222-4222-8222-222222222222",
)

exact_surface = "33333333-3333-4333-8333-333333333333"
exact_open = True
exact_close_attempts = 0
exact_calls: list[list[str]] = []


def exact_close_runner(args: list[str], **_: object) -> Result:
    global exact_close_attempts, exact_open
    exact_calls.append(args)
    if args == ["cmux", "rpc", "system.tree", '{"all":true}']:
        surfaces = [{"id": exact_surface, "ref": "surface:33"}] if exact_open else []
        return Result(stdout=json.dumps({
            "windows": [{
                "id": "window-id",
                "ref": "window:7",
                "workspaces": [{
                    "id": "workspace-id",
                    "ref": "workspace:8",
                    "panes": [{"surfaces": surfaces}],
                }],
            }],
        }))
    if args[:2] == ["cmux", "close-surface"]:
        exact_close_attempts += 1
        check(
            "exact close carries surface workspace and window anchors",
            args == [
                "cmux", "close-surface", "--surface", "surface:33",
                "--workspace", "workspace:8", "--window", "window:7",
            ],
        )
        if exact_close_attempts == 1:
            return Result(returncode=1, stderr="not_found: surface")
        exact_open = False
        return Result(stdout="OK")
    return Result(returncode=1)


context = surface_context(exact_surface, exact_close_runner)
check(
    "surface context resolves exact vertical workspace",
    context is not None
    and context["surface_ref"] == "surface:33"
    and context["workspace_ref"] == "workspace:8"
    and context["window_ref"] == "window:7",
)
check("exact close proves disappearance", close_surface_exact(exact_surface, exact_close_runner) == "closed")
check("not-found without tree disappearance receives one bounded retry", exact_close_attempts == 2)
check("exact close is idempotent only after tree proof", close_surface_exact(exact_surface, exact_close_runner) == "already-gone")

replacement_original = "aaaaaaaa-1111-4111-8111-111111111111"
replacement_surface = "bbbbbbbb-2222-4222-8222-222222222222"
replacement_original_open = True
replacement_shell_open = False
replacement_calls: list[list[str]] = []
def replacement_close_runner(args, **_kwargs):
    global replacement_original_open, replacement_shell_open
    replacement_calls.append(list(args))
    if args[:3] == ["cmux", "rpc", "system.tree"]:
        panes = [{
            "id": "origin-pane", "ref": "pane:40",
            "surfaces": [{"id": "origin-surface", "ref": "surface:40"}],
        }]
        if replacement_original_open:
            panes.append({
                "id": "task-pane", "ref": "pane:41",
                "surfaces": [{"id": replacement_original, "ref": "surface:41"}],
            })
        elif replacement_shell_open:
            panes.append({
                "id": "replacement-pane", "ref": "pane:42",
                "surfaces": [{"id": replacement_surface, "ref": "surface:42"}],
            })
        return Result(stdout=json.dumps({
            "windows": [{
                "id": "window-replacement", "ref": "window:9",
                "workspaces": [{
                    "id": "workspace-replacement", "ref": "workspace:9", "panes": panes,
                }],
            }],
        }))
    if args[:2] == ["cmux", "close-surface"]:
        replacement_original_open = False
        replacement_shell_open = True
        return Result(stdout="OK")
    if args[:2] == ["cmux", "send-key"]:
        replacement_shell_open = False
        return Result(stdout="OK")
    if args[:2] == ["cmux", "send"]:
        return Result(stdout="OK")
    return Result(returncode=1)

check(
    "last-surface close collapses deterministic replacement shell",
    close_surface_exact(replacement_original, replacement_close_runner) == "closed"
    and not replacement_shell_open,
)
check(
    "replacement cleanup targets only the newly created auxiliary surface",
    [
        "cmux", "send", "--surface", "surface:42", "--workspace", "workspace:9",
        "--window", "window:9", "exit",
    ] in replacement_calls,
)
checkpoint = capture_resume("surface-1", "codex", fake_cmux)
check(
    "checkpoint ignores stored command",
    checkpoint == {"kind": "codex", "checkpoint_id": "checkpoint-1", "cwd": str(Path("/tmp").resolve())},
)

print(f"task session tests passed: {passed}")
