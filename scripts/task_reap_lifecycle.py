"""Contract-bound final reap evidence, plan, and archive lifecycle."""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any

from lifecycle_telemetry import elapsed_ms, emit_lifecycle_event, nonnegative_int, read_object
from plan_lifecycle import PlanCloseError, render_plan_close
from task_contract import (
    ContractError,
    read_json as read_contract_json,
    validate_handoff,
)
from task_sessions import TaskSessionError, TaskSessionStore
from task_lifecycle_state import (
    die,
    read_json,
    require_origin_session,
    utc_now,
    write_marker,
)


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
    if meta.get("version") not in {3, 4}:
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


def prepared_reap_plan(
    meta: dict[str, Any],
    text: str,
    *,
    today: str,
    result_link: str,
    exec_session: str | None,
    label: str,
) -> str:
    """Return the exact plan state bound to one prepared reap."""

    policy = meta.get("reap_policy")
    mode = policy.get("mode") if isinstance(policy, dict) else ""
    if mode == "shared":
        return text
    if mode == "final":
        return render_plan_close(
            text,
            today=today,
            result_link=result_link,
            exec_session=exec_session,
            label=label,
        )
    raise PlanCloseError(f"reap preparation: {label} has invalid plan mode")


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
            closed_plan = prepared_reap_plan(
                meta,
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
    if meta.get("version") in {3, 4}:
        try:
            broker_task = TaskSessionStore(vault).archive_task(
                str(meta["project_id"]), str(meta["task_id"])
            )
        except (KeyError, TaskSessionError, OSError) as exc:
            die(f"task-session archive failed before final reap completion: {exc}", 3)
        marker["task_session_status"] = broker_task.get("status")
    write_marker(worktree / ".task-reap-complete.json", marker)
    if meta.get("version") in {3, 4}:
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
