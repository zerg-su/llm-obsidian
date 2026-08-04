"""Exact cmux surface exit and reviewer broker transition lifecycle."""

from __future__ import annotations

import hashlib
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from lifecycle_telemetry import emit_lifecycle_event, read_object
from task_contract import (
    ContractError,
    normalize,
    normalize_for_runtime,
    read_json as read_contract_json,
    validate_handoff,
)
from task_escalation_records import EscalationRecordError, load_attention
from task_sessions import (
    TaskSessionError,
    TaskSessionStore,
    capture_resume,
    close_surface_exact,
    validate_checkpoint,
)
from cmux_workspace_lifecycle import close_task_container
from harness.adapters.cmux import run_cmux
from task_lifecycle_state import (
    die,
    lifecycle_file,
    read_json,
    require_origin_session,
    reviewer_captures_checkpoint,
    reviewer_uses_broker_state,
    root_coordinator_reviewer,
    state_dir,
    utc_now,
    write_marker,
)


HANDOFF_PREFIXES = (".task-", ".review-", ".wiki-")
SCRIPT_DIR = Path(__file__).resolve().parent


def run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


def cmux(args: list[str]) -> subprocess.CompletedProcess[str]:
    return run_cmux(args, runner=lambda argv, **_kwargs: run(argv))


def names(kind: str) -> tuple[str, str, str]:
    if kind == "reviewer":
        return ".review-close-armed.json", "review_surface", "reviewer_runtime"
    return ".task-close-armed.json", "task_surface", "executor_runtime"


def telemetry_surface_context(worktree: Path, kind: str) -> tuple[str, int]:
    """Read a non-authoritative telemetry label without invoking contract checks."""

    task_meta = read_object(worktree / ".task-meta.json")
    source = read_object(lifecycle_file(worktree, ".review-meta.json")) if kind == "reviewer" else task_meta
    runtime = str(
        source.get("reviewer_runtime")
        if kind == "reviewer"
        else source.get("executor_runtime") or source.get("runtime")
    ).strip()
    if runtime not in {"claude", "codex"}:
        runtime = "unknown"
    surface_policy = task_meta.get("surface_policy")
    expected = int(
        task_meta.get("interaction_policy") == "unattended"
        and isinstance(surface_policy, dict)
        and surface_policy.get("auto_close") is True
    )
    return runtime, expected


def surface_and_runtime(worktree: Path, kind: str) -> tuple[str, str]:
    coordinator_review = root_coordinator_reviewer(worktree, kind)
    task_meta = {} if coordinator_review else read_json(worktree / ".task-meta.json")
    if not coordinator_review:
        try:
            # A task plan is intentionally executed before its final /exit.  Only
            # the exact coordinator-prepared close is valid during that phase.
            policy = (
                normalize_for_runtime(task_meta, worktree)
                if kind == "task"
                else normalize(task_meta)
            )
        except ContractError as exc:
            die(str(exc), 3 if kind == "task" else 2)
        if policy["interaction_policy"] != "unattended":
            die("surface auto-close is allowed only for unattended tasks")
        if policy["surface_policy"].get("auto_close") is not True:
            die("surface auto-close is not approved by the task contract")
    _, surface_key, runtime_key = names(kind)
    source = task_meta
    if kind == "reviewer":
        source = read_json(lifecycle_file(worktree, ".review-meta.json"))
    surface = str(source.get(surface_key) or "").strip()
    runtime = str(source.get(runtime_key) or source.get("runtime") or "").strip()
    if not surface or runtime not in {"claude", "codex"}:
        die(f"missing {kind} surface/runtime metadata")
    return surface, runtime


def non_handoff_dirty(worktree: Path) -> list[str]:
    result = run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=worktree)
    if result.returncode != 0:
        die(result.stderr.strip() or "git status failed")
    dirty: list[str] = []
    for line in result.stdout.splitlines():
        path = line[3:].split(" -> ")[-1]
        if path.startswith(HANDOFF_PREFIXES) or path in {
            ".obsidian/workspace.json", ".obsidian/workspace-mobile.json"
        }:
            continue
        dirty.append(path)
    return dirty


def arm(worktree: Path, kind: str) -> tuple[Path, str, str]:
    surface, runtime = surface_and_runtime(worktree, kind)
    if kind == "task":
        try:
            attention = load_attention(worktree)
        except EscalationRecordError as exc:
            die(f"invalid task escalation record: {exc}", 3)
        if attention is not None and attention.get("status") != "resolved":
            die("task has an unresolved coordinator escalation", 3)
        complete = read_json(worktree / ".task-reap-complete.json")
        summary = worktree / ".task-summary.json"
        try:
            validate_handoff(
                read_contract_json(worktree / ".task-meta.json"),
                read_contract_json(summary),
                str(complete.get("current_session") or ""),
                verify_plan_hash=False,
            )
        except ContractError as exc:
            die(str(exc), 3)
        if complete.get("summary_sha256") != hashlib.sha256(summary.read_bytes()).hexdigest():
            die("task reap completion marker does not match the current summary", 3)
        meta_path = worktree / ".task-meta.json"
        if complete.get("meta_sha256") != hashlib.sha256(meta_path.read_bytes()).hexdigest():
            die("task reap completion marker does not match the current metadata", 3)
        result_path = Path(str(complete.get("result_path") or "")).expanduser().resolve()
        vault_root = Path(str(complete.get("vault_root") or "")).expanduser().resolve()
        try:
            result_path.relative_to(vault_root / "wiki")
        except ValueError:
            die("task reap result is outside the recorded vault wiki", 3)
        if complete.get("validated") is not True or not result_path.is_file() or result_path.suffix != ".md":
            die("task reap completion marker is not validated or result page is missing", 3)
        if complete.get("result_sha256") != hashlib.sha256(result_path.read_bytes()).hexdigest():
            die("task reap result changed after coordinator validation", 3)
        plan_path = Path(str(complete.get("plan_path") or "")).expanduser().resolve()
        expected_plan = str(complete.get("closed_plan_sha256") or "")
        if not plan_path.is_file() or hashlib.sha256(plan_path.read_bytes()).hexdigest() != expected_plan:
            die("task plan no longer matches the coordinator-prepared closed state", 3)
        dirty = non_handoff_dirty(worktree)
        if dirty:
            die("task worktree has non-handoff changes: " + ", ".join(dirty), 3)
    sentinel_name, _, _ = names(kind)
    sentinel = lifecycle_file(worktree, sentinel_name, kind)
    payload: dict[str, Any] = {
        "version": 1, "kind": kind, "surface": surface, "armed_at": utc_now()
    }
    if kind == "reviewer" and reviewer_captures_checkpoint(worktree):
        try:
            # Provider hooks may clear their cmux resume binding as the agent
            # exits. Capture it while the interactive process is still alive,
            # before request_exit submits /exit.
            payload["checkpoint"] = capture_resume(surface, runtime)
        except (TaskSessionError, OSError) as exc:
            payload["degradation"] = f"resume checkpoint unavailable: {exc}"
            print(
                f"review session context could not be retained; the next round will start fresh: {exc}",
                file=sys.stderr,
            )
    write_marker(sentinel, payload)
    return sentinel, surface, runtime


def request_exit(worktree: Path, kind: str) -> int:
    if kind == "task":
        require_origin_session(worktree)
    sentinel, surface, runtime = arm(worktree, kind)
    if runtime == "claude":
        cleared = cmux(["send-key", "--surface", surface, "ctrl+u"])
        if cleared.returncode != 0:
            sentinel.unlink(missing_ok=True)
            die((cleared.stdout + cleared.stderr).strip() or "cmux composer clear failed")
    else:
        for _ in range(40):
            cmux(["send-key", "--surface", surface, "backspace"])
    sent = cmux(["send", "--surface", surface, "/exit"])
    if sent.returncode != 0:
        sentinel.unlink(missing_ok=True)
        die((sent.stdout + sent.stderr).strip() or "cmux send failed")
    time.sleep(0.2)
    if runtime == "codex":
        accepted = cmux(["send-key", "--surface", surface, "tab"])
        if accepted.returncode != 0:
            sentinel.unlink(missing_ok=True)
            die((accepted.stdout + accepted.stderr).strip() or "cmux send-key tab failed")
        time.sleep(0.1)
    entered = cmux(["send-key", "--surface", surface, "Enter"])
    if entered.returncode != 0:
        sentinel.unlink(missing_ok=True)
        die((entered.stdout + entered.stderr).strip() or "cmux send-key failed")
    print(f"armed and sent /exit to {kind} surface {surface}")
    return 0


def after_exit(worktree: Path, kind: str, surface: str) -> int:
    sentinel_name, _, _ = names(kind)
    sentinel = lifecycle_file(worktree, sentinel_name, kind)
    if not sentinel.exists():
        if kind == "reviewer" and reviewer_uses_broker_state(worktree):
            if transition_broker_review(
                worktree, "failed", degradation="reviewer exited without an armed completion"
            ):
                start_next_broker_review(worktree)
        runtime, expected = telemetry_surface_context(worktree, kind)
        emit_lifecycle_event(
            worktree,
            "surface-lifecycle",
            actor=f"{kind}:{runtime}",
            counts={"left_open": 1, "auto_close_expected": expected},
            status="degraded" if expected else "noop",
        )
        print(f"{kind} surface left open: close was not armed")
        return 0
    payload = read_json(sentinel)
    if payload.get("kind") != kind or payload.get("surface") != surface:
        die("close sentinel does not match the exiting surface", 3)
    checkpoint: dict[str, str] | None = None
    degradation = ""
    if kind == "reviewer" and reviewer_uses_broker_state(worktree):
        meta = read_json(lifecycle_file(worktree, ".review-meta.json"))
        runtime = str(meta.get("reviewer_runtime") or "")
        raw_checkpoint = payload.get("checkpoint")
        degradation = str(payload.get("degradation") or "")
        if raw_checkpoint is not None:
            try:
                checkpoint = validate_checkpoint(raw_checkpoint, runtime)
            except TaskSessionError as exc:
                degradation = f"resume checkpoint unavailable: {exc}"
        elif not degradation:
            # Backward compatibility for a close armed by an older script.
            try:
                checkpoint = capture_resume(surface, runtime)
            except (TaskSessionError, OSError) as exc:
                degradation = f"resume checkpoint unavailable: {exc}"
        if degradation:
            print(
                "review session context could not be retained; "
                f"the next round will start fresh: {degradation.removeprefix('resume checkpoint unavailable: ')}",
                file=sys.stderr,
            )
    broker_transitioned = True
    if kind == "reviewer" and reviewer_uses_broker_state(worktree):
        broker_transitioned = transition_broker_review(
            worktree, "complete", checkpoint=checkpoint, degradation=degradation
        )
        if broker_transitioned:
            start_next_broker_review(worktree)
    if not broker_transitioned:
        emit_lifecycle_event(
            worktree,
            "surface-lifecycle",
            actor=f"{kind}:{runtime}",
            counts={
                "closed": 0,
                "auto_close_expected": 1,
                "broker_transition_pending": 1,
            },
            status="degraded",
        )
        print(
            "reviewer surface remains open because its exact broker transition is pending; "
            "the close sentinel was preserved, so rerun this after-exit command or use "
            "the printed fail-operation recovery command",
            file=sys.stderr,
        )
        return 3

    # The supervisor runs inside the surface being closed. Current cmux may
    # terminate that process as soon as close-surface succeeds, so persist the
    # broker transition and remove the armed marker before self-close. Restore
    # the marker only when close returns a real error and this process survives.
    sentinel.unlink(missing_ok=True)
    try:
        if kind == "task":
            close_task_container(worktree, surface)
        else:
            close_surface_exact(surface)
    except (TaskSessionError, OSError) as exc:
        write_marker(sentinel, payload)
        die(str(exc) or "cmux close-surface failed")
    runtime, expected = telemetry_surface_context(worktree, kind)
    emit_lifecycle_event(
        worktree,
        "surface-lifecycle",
        actor=f"{kind}:{runtime}",
        counts={
            "closed": 1,
            "auto_close_expected": expected,
            "broker_transition_pending": 0 if broker_transitioned else 1,
        },
        status="ok" if broker_transitioned else "degraded",
    )
    print(f"closed {kind} surface {surface}")
    return 0


def transition_broker_review(
    worktree: Path,
    status: str,
    *,
    checkpoint: dict[str, str] | None = None,
    degradation: str = "",
) -> bool:
    meta = read_object(lifecycle_file(worktree, ".review-meta.json"))
    required = ("project_id", "task_id", "lane_id", "operation_id", "vault_root")
    if not all(str(meta.get(key) or "").strip() for key in required):
        task_meta = read_object(worktree / ".task-meta.json")
        operation = read_object((state_dir() or worktree) / "operation.json")
        meta = {
            "project_id": operation.get("project_id"),
            "task_id": operation.get("task_id"),
            "lane_id": operation.get("lane_id"),
            "operation_id": operation.get("operation_id"),
            "vault_root": task_meta.get("vault_root"),
        }
    if not all(str(meta.get(key) or "").strip() for key in required):
        print("review task-session transition lacks exact broker identity", file=sys.stderr)
        return False
    try:
        store = TaskSessionStore(Path(str(meta["vault_root"])))
        store.transition_operation(
            str(meta["project_id"]), str(meta["task_id"]), str(meta["lane_id"]),
            str(meta["operation_id"]), status, checkpoint=checkpoint,
            degradation=degradation,
        )
    except (TaskSessionError, OSError) as exc:
        command = shlex.join([
            sys.executable,
            str(SCRIPT_DIR / "task_sessions.py"),
            "--vault-root",
            str(meta["vault_root"]),
            "fail-operation",
            "--project-id",
            str(meta["project_id"]),
            "--task-id",
            str(meta["task_id"]),
            "--lane-id",
            str(meta["lane_id"]),
            "--operation-id",
            str(meta["operation_id"]),
        ])
        print(
            f"review task-session transition failed visibly: {exc}; "
            f"exact coordinator recovery: {command}",
            file=sys.stderr,
        )
        return False
    return True


def start_next_broker_review(worktree: Path) -> None:
    meta = read_object(lifecycle_file(worktree, ".review-meta.json"))
    required = ("project_id", "task_id", "lane_id", "vault_root")
    if not all(str(meta.get(key) or "").strip() for key in required):
        return
    try:
        store = TaskSessionStore(Path(str(meta["vault_root"])))
        lane = store.lane_state(str(meta["project_id"]), str(meta["task_id"]), str(meta["lane_id"]))
        queue = lane.get("queue")
        if not isinstance(queue, list) or not queue:
            return
        next_id = str(queue[0])
        operation_dir = store.lane_dir(
            str(meta["project_id"]), str(meta["task_id"]), str(meta["lane_id"])
        ) / "operations" / next_id
        launch = read_json(operation_dir / "launch.json")
        argv = launch.get("argv")
        expected_script = str(SCRIPT_DIR / "review-runner.py")
        if (
            not isinstance(argv, list) or len(argv) > 32
            or argv[:2] != ["python3", expected_script]
            or "--operation-id" not in argv
            or argv[argv.index("--operation-id") + 1] != next_id
            or any(not isinstance(item, str) or not item or "\0" in item for item in argv)
        ):
            raise ValueError("queued review launch packet is invalid")
        subprocess.Popen(
            argv, cwd=worktree, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, TaskSessionError, ValueError, IndexError) as exc:
        print(f"queued review could not auto-start; continuing visibly: {exc}", file=sys.stderr)
