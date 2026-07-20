#!/usr/bin/env python3
"""Arm agent exit and close only the exact cmux surface after process return."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

from lifecycle_telemetry import elapsed_ms, emit_lifecycle_event, nonnegative_int, read_object
from plan_lifecycle import PlanCloseError, render_plan_close
from task_contract import (
    ContractError,
    normalize,
    normalize_for_runtime,
    read_json as read_contract_json,
    validate_handoff,
    v3_session_is_bound,
)
from task_sessions import (
    TaskSessionError,
    TaskSessionStore,
    capture_resume,
    close_surface_exact,
    validate_checkpoint,
)


HANDOFF_PREFIXES = (".task-", ".review-", ".wiki-")
SCRIPT_DIR = Path(__file__).resolve().parent
_STATE_DIR: Path | None = None


def lifecycle_file(worktree: Path, name: str, kind: str = "reviewer") -> Path:
    if kind == "reviewer" and _STATE_DIR is not None:
        return _STATE_DIR / name
    return worktree / name


def die(message: str, code: int = 2) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        die(f"cannot read {path}: {exc}")
    if not isinstance(data, dict):
        die(f"{path} must contain an object")
    return data


def write_marker(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def current_session_id() -> str:
    acceptance = str(os.environ.get("LLM_OBSIDIAN_ACCEPTANCE") or "") == "1"
    acceptance_session = str(
        os.environ.get("LLM_OBSIDIAN_ACCEPTANCE_SESSION_ID") or ""
    ).strip()
    if acceptance and re.fullmatch(r"[A-Za-z0-9._:-]+", acceptance_session):
        return acceptance_session
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "unknown"


def require_origin_session(worktree: Path, supplied: str = "") -> None:
    meta = read_json(worktree / ".task-meta.json")
    origin = str(meta.get("origin_session") or "")
    actual = current_session_id()
    if meta.get("version") == 3:
        valid = actual != "unknown" and v3_session_is_bound(meta, actual)
    else:
        valid = actual != "unknown" and bool(origin) and actual == origin
    if not valid or (supplied and supplied != actual):
        die("only the originating coordinator session may finalize or close this task", 3)


def run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


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
    task_meta = read_json(worktree / ".task-meta.json")
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
        attention_path = worktree / ".task-needs-attention.json"
        if attention_path.is_file() and read_json(attention_path).get("status") != "resolved":
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
    if kind == "reviewer":
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
        cleared = run(["cmux", "send-key", "--surface", surface, "ctrl+u"])
        if cleared.returncode != 0:
            sentinel.unlink(missing_ok=True)
            die((cleared.stdout + cleared.stderr).strip() or "cmux composer clear failed")
    else:
        for _ in range(40):
            run(["cmux", "send-key", "--surface", surface, "backspace"])
    sent = run(["cmux", "send", "--surface", surface, "/exit"])
    if sent.returncode != 0:
        sentinel.unlink(missing_ok=True)
        die((sent.stdout + sent.stderr).strip() or "cmux send failed")
    time.sleep(0.2)
    if runtime == "codex":
        accepted = run(["cmux", "send-key", "--surface", surface, "tab"])
        if accepted.returncode != 0:
            sentinel.unlink(missing_ok=True)
            die((accepted.stdout + accepted.stderr).strip() or "cmux send-key tab failed")
        time.sleep(0.1)
    entered = run(["cmux", "send-key", "--surface", surface, "Enter"])
    if entered.returncode != 0:
        sentinel.unlink(missing_ok=True)
        die((entered.stdout + entered.stderr).strip() or "cmux send-key failed")
    print(f"armed and sent /exit to {kind} surface {surface}")
    return 0


def after_exit(worktree: Path, kind: str, surface: str) -> int:
    sentinel_name, _, _ = names(kind)
    sentinel = lifecycle_file(worktree, sentinel_name, kind)
    if not sentinel.exists():
        if kind == "reviewer" and _STATE_DIR is not None:
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
    if kind == "reviewer" and _STATE_DIR is not None:
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
    if kind == "reviewer" and _STATE_DIR is not None:
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
        operation = read_object((_STATE_DIR or worktree) / "operation.json")
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
        expected_script = str(
            SCRIPT_DIR.parent / "skills" / "review-dispatch" / "scripts" / "spawn_review.py"
        )
        if (
            not isinstance(argv, list) or len(argv) > 32
            or argv[:3] != ["python3", expected_script, "start"]
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


def validated_review_archive(
    worktree: Path, vault: Path, state_dir: Path | None = None
) -> dict[str, Any] | None:
    """Require a completed, immutable archive whenever a review cycle exists."""
    root = state_dir or worktree
    review_meta_path = root / ".review-meta.json"
    if not review_meta_path.is_file():
        return None
    review_meta = read_json(review_meta_path)
    marker_path = root / ".review-archive.json"
    marker = read_json(marker_path)
    if marker.get("schema_version") != 1 or marker.get("status") not in {"archived", "already-current"}:
        die("review archive marker is not complete", 3)
    review_id = str(marker.get("review_id") or "")
    if not review_id or (review_meta.get("review_id") and review_meta.get("review_id") != review_id):
        die("review archive marker does not match the review cycle", 3)
    if marker.get("verdict") != "approve":
        die("final reap requires an approved durable review archive", 3)
    raw_path = str(marker.get("path") or "")
    title = str(marker.get("title") or "")
    wikilink = str(marker.get("wikilink") or "")
    rel = Path(raw_path)
    if rel.is_absolute() or rel.suffix != ".md" or rel.stem != title or wikilink != f"[[{title}]]":
        die("review archive marker has inconsistent path/title/wikilink", 3)
    archive_page = (vault / rel).resolve()
    try:
        archive_page.relative_to((vault / "wiki" / "meta" / "reviews").resolve())
    except ValueError:
        die("review archive marker points outside wiki/meta/reviews", 3)
    expected_hash = str(marker.get("content_sha256") or "")
    if not archive_page.is_file() or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        die("review archive page or content hash is missing", 3)
    if hashlib.sha256(archive_page.read_bytes()).hexdigest() != expected_hash:
        die("durable review archive changed after archival", 3)
    return marker


def validated_review_archives(
    worktree: Path, vault: Path, meta: dict[str, Any]
) -> list[dict[str, Any]]:
    if meta.get("version") != 3:
        value = validated_review_archive(worktree, vault)
        if value is None:
            return []
        return [{**value, "marker_path": str(worktree / ".review-archive.json")}]
    try:
        operations = TaskSessionStore(vault).list_operations(
            str(meta["project_id"]), str(meta["task_id"]), domain="review"
        )
    except (KeyError, TaskSessionError, OSError) as exc:
        die(f"cannot enumerate exact v3 review operations: {exc}", 3)
    archives: list[dict[str, Any]] = []
    failed_operations = 0
    for operation in operations:
        state_dir = Path(str(operation["operation_dir"])).resolve()
        if operation.get("status") == "failed":
            failed_operations += 1
            continue
        if not (state_dir / ".review-meta.json").is_file():
            die(
                f"v3 review operation {operation.get('operation_id')} has no completed review metadata",
                3,
            )
        if operation.get("status") != "complete":
            die(f"v3 review operation {operation.get('operation_id')} is not complete", 3)
        value = validated_review_archive(worktree, vault, state_dir)
        if value is None:
            die("started v3 review has no durable archive", 3)
        archives.append({**value, "marker_path": str(state_dir / ".review-archive.json")})
    if failed_operations and not archives:
        die(
            "failed v3 review cycles are accounted for, but final reap still requires "
            "at least one approved durable review archive",
            3,
        )
    return archives


def review_archive_records(archives: list[dict[str, Any]]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for archive in archives:
        marker_path = Path(str(archive["marker_path"])).resolve()
        records.append({
            "marker_path": str(marker_path),
            "marker_sha256": hashlib.sha256(marker_path.read_bytes()).hexdigest(),
            "path": str(archive["path"]),
            "wikilink": str(archive["wikilink"]),
        })
    return records


def result_wikilink(summary_title: str, result_path: Path) -> str:
    title = summary_title.strip()
    stem = result_path.stem.strip()
    if not title or not stem:
        raise ValueError("reap result title and filename must be non-empty")
    return f"[[{title}]]" if stem == title else f"[[{stem}|{title}]]"


def collision_safe_result_path(vault: Path, intended: Path) -> Path:
    """Route a new result around a vault-wide filename collision.

    Obsidian resolves pathless wikilinks by filename, and the vault validator
    intentionally rejects duplicate Markdown names across folders.  The
    common collision is an exact-title plan plus a not-yet-created session
    result.  Existing targets are never silently rerouted because that would
    turn an update into a create and leave the original collision in place.
    """

    wiki = (vault / "wiki").resolve()
    result = intended.expanduser().resolve()
    collisions = [
        path.resolve()
        for path in wiki.rglob("*.md")
        if path.resolve() != result and path.name.casefold() == result.name.casefold()
    ]
    if not collisions:
        return result
    if result.exists():
        raise ValueError(
            "existing reap result has a vault-wide filename collision; repair the vault first"
        )
    candidate = result.with_name(f"{result.stem} — Result.md")
    candidate_collides = candidate.exists() or any(
        path.resolve() != candidate
        and path.name.casefold() == candidate.name.casefold()
        for path in wiki.rglob("*.md")
    )
    if candidate_collides:
        raise ValueError(
            "collision-safe reap filename is already occupied; choose an explicit unique route"
        )
    return candidate


def reroute_closed_plan(text: str, old_link: str, new_link: str, *, label: str) -> str:
    if not old_link or not new_link:
        raise PlanCloseError(f"reap reroute: {label} requires old and new result links")
    if old_link == new_link:
        return text
    pattern = re.compile(
        rf"^(Результат:\s*){re.escape(old_link)}(\s+\(reaped\s+\d{{4}}-\d{{2}}-\d{{2}}\)\s*)$",
        flags=re.M,
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise PlanCloseError(
            f"reap reroute: {label} has {len(matches)} exact prior result lines (expected 1)"
        )
    return pattern.sub(rf"\g<1>{new_link}\g<2>", text, count=1)


def prepare_reap(worktree: Path, current_session: str, result_path: Path, vault_root: Path) -> int:
    require_origin_session(worktree, current_session)
    attention_path = worktree / ".task-needs-attention.json"
    if attention_path.is_file() and read_json(attention_path).get("status") != "resolved":
        die("task has an unresolved coordinator escalation", 3)
    meta_path = worktree / ".task-meta.json"
    meta = read_contract_json(meta_path)
    summary_path = worktree / ".task-summary.json"
    summary = read_contract_json(summary_path)
    try:
        validate_handoff(meta, summary, current_session, verify_plan_hash=False)
    except ContractError as exc:
        die(str(exc))
    result = result_path.expanduser().resolve()
    vault = vault_root.expanduser().resolve()
    review_archives = validated_review_archives(worktree, vault, meta)
    archive_records = review_archive_records(review_archives)
    try:
        result.relative_to(vault / "wiki")
    except ValueError:
        die("validated reap result must be inside the selected vault wiki", 3)
    if result.suffix != ".md":
        die("prepared reap result must be a wiki Markdown page", 3)
    try:
        result = collision_safe_result_path(vault, result)
    except ValueError as exc:
        die(str(exc), 3)
    plan = Path(str(meta.get("plan_file") or "")).expanduser().resolve()
    try:
        plan.relative_to(vault / "wiki" / "plans")
    except ValueError:
        die("approved task plan must be inside the selected vault plans directory", 3)
    try:
        result_link = result_wikilink(str(summary.get("title") or ""), result)
    except ValueError as exc:
        die(str(exc), 3)
    exec_session = str(summary.get("session") or "").strip() or None
    prepared_date = time.strftime("%Y-%m-%d")
    plan_text = plan.read_text(encoding="utf-8")
    plan_hash = hashlib.sha256(plan_text.encode("utf-8")).hexdigest()
    approved_hash = str(meta.get("approved_plan_sha256") or "")
    prior_marker: dict[str, Any] = {}
    try:
        if plan_hash == approved_hash:
            closed_plan = render_plan_close(
                plan_text,
                today=prepared_date,
                result_link=result_link,
                exec_session=exec_session,
                label=str(plan.relative_to(vault)),
            )
        else:
            prior_marker = read_json(worktree / ".task-reap-prepared.json")
            immutable = {
                "task_name": meta.get("task_name"),
                "current_session": current_session,
                "vault_root": str(vault),
                "summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
                "meta_sha256": hashlib.sha256(meta_path.read_bytes()).hexdigest(),
                "plan_path": str(plan),
                "approved_plan_sha256": approved_hash,
            }
            for field, expected in immutable.items():
                if prior_marker.get(field) != expected:
                    die(f"prior reap preparation no longer matches {field}", 3)
            if prior_marker.get("closed_plan_sha256") != plan_hash:
                die("approved plan is neither pending nor the prior prepared close", 3)
            if prior_marker.get("review_archives", []) != archive_records:
                die("prior reap preparation no longer matches review archive markers", 3)
            closed_plan = reroute_closed_plan(
                plan_text,
                str(prior_marker.get("result_link") or ""),
                result_link,
                label=str(plan.relative_to(vault)),
            )
    except (OSError, PlanCloseError) as exc:
        die(str(exc), 3)
    marker = {
        "version": 1,
        "task_name": meta.get("task_name"),
        "current_session": current_session,
        "result_path": str(result),
        "vault_root": str(vault),
        "summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
        "meta_sha256": hashlib.sha256(meta_path.read_bytes()).hexdigest(),
        "plan_path": str(plan),
        "approved_plan_sha256": meta.get("approved_plan_sha256"),
        "closed_plan_sha256": hashlib.sha256(closed_plan.encode("utf-8")).hexdigest(),
        "result_link": result_link,
        "exec_session": exec_session,
        "prepared_date": prepared_date,
        "prepared_at": utc_now(),
    }
    if prior_marker:
        marker["previous_closed_plan_sha256"] = plan_hash
        marker["previous_result_link"] = str(prior_marker.get("result_link") or "")
    marker["review_archives"] = archive_records
    if len(archive_records) == 1:
        marker["review_archive_marker_sha256"] = archive_records[0]["marker_sha256"]
        marker["review_archive_path"] = archive_records[0]["path"]
        marker["review_archive_wikilink"] = archive_records[0]["wikilink"]
    write_marker(worktree / ".task-reap-prepared.json", marker)
    print(f"prepared contract-bound final reap: {result}")
    return 0


def complete_reap(worktree: Path, current_session: str, result_path: Path, vault_root: Path) -> int:
    require_origin_session(worktree, current_session)
    attention_path = worktree / ".task-needs-attention.json"
    if attention_path.is_file() and read_json(attention_path).get("status") != "resolved":
        die("task has an unresolved coordinator escalation", 3)
    meta_path = worktree / ".task-meta.json"
    summary_path = worktree / ".task-summary.json"
    meta = read_contract_json(meta_path)
    summary = read_contract_json(summary_path)
    prepared = read_json(worktree / ".task-reap-prepared.json")
    try:
        validate_handoff(meta, summary, current_session, verify_plan_hash=False)
    except ContractError as exc:
        die(str(exc))
    result = result_path.expanduser().resolve()
    vault = vault_root.expanduser().resolve()
    review_archives = validated_review_archives(worktree, vault, meta)
    archive_records = review_archive_records(review_archives)
    expected_fields = {
        "task_name": meta.get("task_name"),
        "current_session": current_session,
        "result_path": str(result),
        "vault_root": str(vault),
        "summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
        "meta_sha256": hashlib.sha256(meta_path.read_bytes()).hexdigest(),
        "approved_plan_sha256": meta.get("approved_plan_sha256"),
    }
    for field, expected in expected_fields.items():
        if prepared.get(field) != expected:
            die(f"reap preparation no longer matches {field}", 3)
    if prepared.get("review_archives", []) != archive_records:
        die("reap preparation no longer matches review archive markers", 3)
    try:
        result.relative_to(vault / "wiki")
    except ValueError:
        die("validated reap result must be inside the selected vault wiki", 3)
    if not result.is_file() or result.suffix != ".md":
        die("validated reap result must be an existing wiki Markdown page", 3)
    result_text = result.read_text(encoding="utf-8", errors="replace")
    missing_links = [record["wikilink"] for record in archive_records if record["wikilink"] not in result_text]
    if missing_links:
        die("validated reap result does not link durable review archives: " + ", ".join(missing_links), 3)
    plan = Path(str(meta.get("plan_file") or "")).expanduser().resolve()
    if str(plan) != prepared.get("plan_path"):
        die("reap preparation points at a different approved plan", 3)
    expected_closed = str(prepared.get("closed_plan_sha256") or "")
    if not plan.is_file() or hashlib.sha256(plan.read_bytes()).hexdigest() != expected_closed:
        die("approved plan does not match the coordinator-prepared closed state", 3)
    marker = {
        "version": 1,
        "task_name": meta.get("task_name"),
        "current_session": current_session,
        "result_path": str(result),
        "vault_root": str(vault),
        "summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
        "meta_sha256": hashlib.sha256(meta_path.read_bytes()).hexdigest(),
        "plan_path": str(plan),
        "closed_plan_sha256": expected_closed,
        "result_sha256": hashlib.sha256(result.read_bytes()).hexdigest(),
        "validated": True,
        "completed_at": utc_now(),
    }
    if meta.get("version") == 3:
        try:
            broker_task = TaskSessionStore(vault).archive_task(
                str(meta["project_id"]), str(meta["task_id"])
            )
        except (KeyError, TaskSessionError, OSError) as exc:
            die(f"task-session archive failed before final reap completion: {exc}", 3)
        marker["task_session_status"] = broker_task.get("status")
    write_marker(worktree / ".task-reap-complete.json", marker)
    if meta.get("version") == 3:
        (worktree / ".task-session-binding.json").unlink(missing_ok=True)
    review_meta = read_object(worktree / ".review-meta.json")
    attention = read_object(worktree / ".task-needs-attention.json")
    duration = elapsed_ms(meta.get("spawned_at"), marker["completed_at"])
    emit_lifecycle_event(
        worktree,
        "task-complete",
        actor="reap",
        counts={
            "tasks": 1,
            "review_iterations": (
                sum(nonnegative_int(archive.get("rounds")) for archive in review_archives)
                if review_archives else nonnegative_int(review_meta.get("iteration"))
            ),
            "escalations": 1 if attention else 0,
            **({"duration_ms": duration} if duration is not None else {}),
        },
        vault_root=vault,
    )
    print(f"recorded validated final reap: {result}")
    return 0


def main() -> int:
    global _STATE_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    request = sub.add_parser("request-exit")
    request.add_argument("--worktree", default=".")
    request.add_argument("--state-dir", default="")
    request.add_argument("--kind", choices=["reviewer", "task"], required=True)
    after = sub.add_parser("after-exit")
    after.add_argument("--worktree", default=".")
    after.add_argument("--state-dir", default="")
    after.add_argument("--kind", choices=["reviewer", "task"], required=True)
    after.add_argument("--surface", required=True)
    complete = sub.add_parser("complete-reap")
    complete.add_argument("--worktree", default=".")
    complete.add_argument("--current-session", required=True)
    complete.add_argument("--result-path", required=True)
    complete.add_argument("--vault-root", default=str(Path(__file__).resolve().parents[1]))
    prepare = sub.add_parser("prepare-reap")
    prepare.add_argument("--worktree", default=".")
    prepare.add_argument("--current-session", required=True)
    prepare.add_argument("--result-path", required=True)
    prepare.add_argument("--vault-root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    worktree = Path(args.worktree).expanduser().resolve()
    raw_state = str(getattr(args, "state_dir", "") or "").strip()
    _STATE_DIR = Path(raw_state).expanduser().resolve() if raw_state else None
    if args.command == "request-exit":
        return request_exit(worktree, args.kind)
    if args.command == "after-exit":
        return after_exit(worktree, args.kind, args.surface)
    if args.command == "prepare-reap":
        return prepare_reap(worktree, args.current_session, Path(args.result_path), Path(args.vault_root))
    return complete_reap(worktree, args.current_session, Path(args.result_path), Path(args.vault_root))


if __name__ == "__main__":
    raise SystemExit(main())
