#!/usr/bin/env python3
"""Deterministic v3 unattended final reap orchestration."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, NoReturn


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from lifecycle_telemetry import emit_lifecycle_event  # noqa: E402
from task_contract import ContractError, validate_handoff  # noqa: E402
from vault_schema import unresolved_wikilinks  # noqa: E402
from wiki_summary_contract import (  # noqa: E402
    WikiSummaryError,
    validate_summary_for_task,
)
from harness.contracts import OwnedResources  # noqa: E402
from harness.runtime_sessions import (  # noqa: E402
    RuntimeSessionError,
    RuntimeSessionManager,
)
from harness.review_finalization import (  # noqa: E402
    require_task_review,
    review_gate_root,
)
from harness.workflows.reap import run_reap  # noqa: E402
from task_reap_lifecycle import mark_plan_close_conflict  # noqa: E402


TYPE_FOLDER = {
    "session": ("meta/sessions", "session"),
    "decision": ("decisions", "decision"),
    "runbook": ("runbooks", "runbook"),
    "incident": ("incidents", "incident"),
    "service-update": ("services", "service"),
    "repo-touch": ("repos", "repo"),
}


class ReapError(ValueError):
    pass


def die(message: str) -> NoReturn:
    print(f"reap-runner: {message}", file=sys.stderr)
    raise SystemExit(3)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReapError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReapError(f"JSON root must be an object: {path}")
    return value


def run(argv: list[str], *, cwd: Path, input_text: str | None = None, label: str) -> str:
    result = subprocess.run(argv, cwd=cwd, input=input_text, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raw = (result.stdout or result.stderr).strip()
        detail = ""
        try:
            payload = json.loads(raw)
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                detail = str(error.get("message") or "").strip()
        except json.JSONDecodeError:
            pass
        if not detail:
            lines = raw.splitlines()
            detail = lines[-1] if lines else ""
        raise ReapError(f"{label} failed" + (f": {detail[:1000]}" if detail else ""))
    return result.stdout


def current_session(vault: Path) -> str:
    value = run([str(vault / "scripts/current-session-id.sh")], cwd=vault, label="session lookup").strip()
    if not value or value == "unknown":
        raise ReapError("current coordinator session is unknown")
    return value


def proposed_path(vault: Path, summary: dict[str, Any]) -> Path:
    folder, _page_type = TYPE_FOLDER[summary["type"]]
    title = summary["title"]
    if "/" in title or "\\" in title or title in {".", ".."}:
        raise ReapError("summary title is not a safe filename")
    filename = f"{date.today().isoformat()}-{title}.md" if summary["type"] == "incident" else f"{title}.md"
    return (vault / "wiki" / folder / filename).resolve()


def archive_reviews(
    vault: Path, worktree: Path, meta: dict[str, Any]
) -> list[str]:
    task_id = str(meta.get("task_id") or "")
    try:
        authorization = require_task_review(
            meta,
            worktree,
            expected_vault=vault,
            expected_operation_id=task_id,
        )
        operation = review_gate_root(
            meta,
            worktree,
            expected_vault=vault,
            expected_operation_id=task_id,
        )
    except ValueError as exc:
        raise ReapError(f"review archive authorization failed: {exc}") from exc
    if authorization.skipped:
        return []
    raw = run(
        [
            sys.executable,
            str(vault / "scripts/harness/review_archive.py"),
            "--worktree",
            str(worktree),
            "--operation-dir",
            str(operation),
            "--vault-root",
            str(vault),
            "--json",
        ],
        cwd=vault,
        label="review archive",
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReapError("review archive returned invalid JSON") from exc
    if value.get("status") not in {"archived", "already-current"}:
        raise ReapError("approved review did not produce an archive")
    marker = (operation / ".review-archive.json").resolve()
    if not marker.is_file():
        raise ReapError("approved review archive marker is missing")
    return [str(marker)]


def summary_with_reviews(vault: Path, worktree: Path, markers: list[str]) -> dict[str, Any]:
    meta_path = worktree / ".task-meta.json"
    meta = read_json(meta_path)
    argv = [
        sys.executable,
        str(vault / "scripts/parse-wiki-summary.py"),
        "--json-file",
        str(worktree / ".task-summary.json"),
        "--task-meta",
        str(meta_path),
    ]
    for marker in markers:
        argv.extend(["--review-archive-marker", marker])
    raw = run(argv, cwd=vault, label="summary parsing")
    try:
        return validate_summary_for_task(
            json.loads(raw), meta, allow_missing_session=True
        )
    except (json.JSONDecodeError, WikiSummaryError) as exc:
        raise ReapError(f"summary contract is invalid: {exc}") from exc


def unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


LOG_EXCERPT_CAP = 500


def log_excerpt(body: str, cap: int = LOG_EXCERPT_CAP) -> str:
    """Truncate to the log cap without splitting or orphaning a wikilink."""
    if len(body) <= cap:
        return body
    cut = cap
    for opener in re.finditer(r"\[\[", body):
        start = opener.start()
        if start >= cut:
            break
        close = body.find("]]", opener.end())
        end = close + 2 if close != -1 else len(body)
        if end > cut:
            cut = start
            break
    return body[:cut].rstrip()


def reap_log_entry(
    *, today: str, task_name: str, address: str, link: str, body: str
) -> str:
    return (
        f"## [{today}] reap | {task_name}\n\n"
        f"`{address}` {link}. {log_excerpt(body)}"
    )


def outcome_markdown(summary: dict[str, Any]) -> str:
    if summary.get("schema_version") != 2:
        return ""
    evidence = ", ".join(
        f"`{item}`" for item in summary["outcome_evidence_ids"]
    ) or "none"
    gaps = "\n".join(
        f"- {item}" for item in summary["residual_gap_pointers"]
    ) or "- none"
    return (
        "## Outcome\n\n"
        f"Outcome disposition: `{summary['outcome_disposition']}`\n\n"
        f"Outcome evidence IDs: {evidence}\n\n"
        f"Residual gaps:\n{gaps}"
    )


def archived_summary_body(summary: dict[str, Any]) -> str:
    body = str(summary["body"]).rstrip()
    outcome = outcome_markdown(summary)
    return f"{body}\n\n{outcome}" if outcome else body


def frontmatter_page(
    vault: Path, meta: dict[str, Any], summary: dict[str, Any], current: str,
) -> str:
    address = run([str(vault / "scripts/allocate-address.sh")], cwd=vault, label="address allocation").strip()
    if not re.fullmatch(r"c-\d{6}", address):
        raise ReapError("address allocator returned an invalid address")
    _folder, page_type = TYPE_FOLDER[summary["type"]]
    sessions = unique([str(meta.get("origin_session") or ""), str(summary.get("session") or ""), current])
    route = meta.get("routing", {}).get("effective", {}) if isinstance(meta.get("routing"), dict) else {}
    runtime = str(meta.get("executor_runtime") or meta.get("runtime") or "")
    model = str(route.get("model") or meta.get("model") or "") if isinstance(route, dict) else str(meta.get("model") or "")
    agents = [str(item.get("name") or "") for item in meta.get("suggested_agents", []) if isinstance(item, dict)]
    durable_body = archived_summary_body(summary)
    related = unique(re.findall(r"\[\[([^\]|#]+)", durable_body))[:20]
    today = date.today().isoformat()
    lines = [
        "---", f"type: {page_type}", f"title: {json.dumps(summary['title'], ensure_ascii=False)}",
        f"address: {address}", f"created: {today}", f"updated: {today}",
        "tags:", "  - reap", f"  - {page_type}", "status: active", "sessions:",
        *[f"  - {json.dumps(item, ensure_ascii=False)}" for item in sessions],
    ]
    if runtime:
        lines.append(f"executor_runtime: {runtime}")
    if model:
        lines.append(f"executor_model: {json.dumps(model, ensure_ascii=False)}")
    if agents:
        lines.extend(["suggested_agents:", *[f"  - {json.dumps(item, ensure_ascii=False)}" for item in agents]])
    if summary.get("schema_version") == 2:
        lines.append(f"outcome_disposition: {summary['outcome_disposition']}")
        lines.extend(
            [
                "outcome_evidence_ids:",
                *[
                    f"  - {item}"
                    for item in summary["outcome_evidence_ids"]
                ],
                "residual_gap_pointers:",
                *[
                    f"  - {json.dumps(item, ensure_ascii=False)}"
                    for item in summary["residual_gap_pointers"]
                ],
            ]
        )
    if related:
        lines.extend(["related:", *[f"  - {json.dumps(f'[[{item}]]', ensure_ascii=False)}" for item in related]])
    lines.extend(["---", "", f"# {summary['title']}", "", durable_body, ""])
    return "\n".join(lines)


def update_page(path: Path, summary: dict[str, Any], task_name: str) -> tuple[str, str]:
    old = path.read_text(encoding="utf-8")
    expected = hashlib.sha256(old.encode()).hexdigest()
    today = date.today().isoformat()
    text = re.sub(r"(?m)^updated:\s*\d{4}-\d{2}-\d{2}\s*$", f"updated: {today}", old, count=1)
    text = text.rstrip() + f"\n\n## {today} {task_name}\n\n{archived_summary_body(summary)}\n"
    return text, expected


def page_address(content: str) -> str:
    match = re.search(r"(?m)^address:\s*(c-\d{6})\s*$", content)
    if match is None or match.group(1) == "c-000000":
        raise ReapError("reap result page must carry one non-zero c-NNNNNN address")
    return match.group(1)


def approved_plan_state(meta: dict[str, Any]) -> tuple[Path, str]:
    plan = Path(str(meta.get("plan_file") or "")).expanduser().resolve()
    text = plan.read_text(encoding="utf-8")
    if re.search(r"(?m)^status:\s*pending\s*$", text):
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != meta.get("approved_plan_sha256"):
            return plan, "conflict"
        return plan, "pending"
    if re.search(r"(?m)^status:\s*executed\s*$", text):
        if not reap_closes_plan(meta):
            raise ReapError(
                "shared approved plan was closed before this task reaped"
            )
        return plan, "executed"
    raise ReapError("approved plan must be pending or executed recovery state")


def reap_closes_plan(meta: dict[str, Any]) -> bool:
    policy = meta.get("reap_policy")
    mode = policy.get("mode") if isinstance(policy, dict) else "final"
    if mode not in {"final", "shared"}:
        raise ReapError("reap policy has an invalid plan mode")
    return mode == "final"


def with_plan_close(
    payload: dict[str, Any],
    meta: dict[str, Any],
    *,
    vault: Path,
    plan: Path,
    result_link: str,
    exec_session: object,
) -> dict[str, Any]:
    result = dict(payload)
    if reap_closes_plan(meta):
        result["plan_close"] = {
            "file": plan.relative_to(vault).as_posix(),
            "result_link": result_link,
            "exec_session": exec_session,
            "expected_sha256": meta["approved_plan_sha256"],
            "on_conflict": "preserve",
        }
    return result


def validate_summary_wikilinks(vault: Path, summary: dict[str, Any]) -> None:
    link_source = "\n".join(
        [
            str(summary.get("body") or ""),
            *[
                str(item)
                for item in summary.get("residual_gap_pointers", [])
            ],
        ]
    )
    missing = unresolved_wikilinks(vault / "wiki", link_source)
    if missing:
        rendered = ", ".join(f"[[{target}]]" for target in missing[:10])
        raise ReapError(
            "summary contains unresolved wikilinks before vault mutation: "
            f"{rendered}; use an existing vault filename or alias"
        )


def authorize_review(
    vault: Path, worktree: Path, meta: dict[str, Any]
) -> None:
    task_id = str(meta.get("task_id") or "")
    try:
        require_task_review(
            meta,
            worktree,
            expected_vault=vault,
            expected_operation_id=task_id,
        )
    except ValueError as exc:
        raise ReapError(f"review gate blocked finalization: {exc}") from exc


def _public_reap_result(
    *,
    result_path: str,
    result_link: str,
    plan_close_status: str,
    duration_ms: int,
    idempotent: bool = False,
) -> dict[str, Any]:
    if plan_close_status not in {"closed", "conflict", "retained"}:
        raise ReapError("reap marker has an invalid plan close status")
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "complete",
        "result_path": result_path,
        "result_link": result_link,
        "plan_close_status": plan_close_status,
        "warnings": (
            ["plan-close-conflict"]
            if plan_close_status == "conflict"
            else []
        ),
        "duration_ms": duration_ms,
    }
    if idempotent:
        result["idempotent"] = True
    return result


def _finalize_reap(vault: Path, worktree: Path, current: str) -> dict[str, Any]:
    started = time.monotonic()
    meta = read_json(worktree / ".task-meta.json")
    authorize_review(vault, worktree, meta)
    raw_summary = validate_summary_for_task(
        read_json(worktree / ".task-summary.json"),
        meta,
        allow_missing_session=True,
    )
    if meta.get("version") not in {3, 4} or meta.get("interaction_policy") != "unattended":
        raise ReapError("reap-runner supports v3/v4 unattended final tasks only")
    try:
        validate_handoff(meta, raw_summary, current, verify_plan_hash=False)
    except ContractError as exc:
        raise ReapError(str(exc)) from exc
    validate_summary_wikilinks(vault, raw_summary)
    plan_before, plan_state = approved_plan_state(meta)
    validated_at = time.monotonic()
    markers = archive_reviews(vault, worktree, meta)
    summary = summary_with_reviews(vault, worktree, markers)
    archived_at = time.monotonic()
    proposed = proposed_path(vault, summary)
    if plan_state == "executed":
        prior = read_json(worktree / ".task-reap-prepared.json")
        proposed = Path(str(prior.get("result_path") or "")).resolve()
    run(
        [sys.executable, str(vault / "scripts/cmux_surface_lifecycle.py"), "prepare-reap", "--worktree", str(worktree), "--current-session", current, "--result-path", str(proposed), "--vault-root", str(vault)],
        cwd=vault,
        label="reap preparation",
    )
    prepared = read_json(worktree / ".task-reap-prepared.json")
    plan_close_status = str(prepared.get("plan_close_status") or "closed")
    if plan_close_status not in {"closed", "conflict", "retained"}:
        raise ReapError("reap preparation has an invalid plan close status")
    prepared_at = time.monotonic()
    result = Path(str(prepared.get("result_path") or "")).resolve()
    try:
        rel = result.relative_to(vault).as_posix()
    except ValueError as exc:
        raise ReapError("prepared result escaped the vault") from exc
    link = str(prepared["result_link"])
    today = date.today().isoformat()
    plan = plan_before
    plan_text = plan.read_text(encoding="utf-8")
    pending = re.search(r"(?m)^status:\s*pending\s*$", plan_text) is not None
    if pending:
        update_mode = summary["type"] in {"service-update", "repo-touch"} and result.is_file()
        if update_mode:
            content, expected = update_page(result, summary, str(meta["task_name"]))
            page = {"op": "update", "path": rel, "content": content, "expected_sha256": expected}
        else:
            if result.is_file():
                raise ReapError("prepared new result path already exists")
            page = {"op": "create", "path": rel, "content": frontmatter_page(vault, meta, summary, current)}
        address = page_address(page["content"])
        payload: dict[str, Any] = {
            "schema_version": 1,
            "request_id": f"reap-{meta['task_id']}",
            "actor": "reap",
            "session": current,
            "pages": [page],
            "log_entry": reap_log_entry(
                today=today,
                task_name=str(meta["task_name"]),
                address=address,
                link=link,
                body=str(summary["body"]),
            ),
            "hot_bullet": f"{today}: {link} — finalized task result (`{address}`)",
        }
        payload = with_plan_close(
            payload,
            meta,
            vault=vault,
            plan=plan,
            result_link=link,
            exec_session=summary.get("session"),
        )
        write_raw = run([sys.executable, str(vault / "scripts/vault-write.py"), "--output", "json"], cwd=vault, input_text=json.dumps(payload, ensure_ascii=False), label="reap vault transaction")
        try:
            write_result = json.loads(write_raw)
        except json.JSONDecodeError as exc:
            raise ReapError("reap vault transaction returned invalid JSON") from exc
        warnings = write_result.get("warnings")
        if not isinstance(warnings, list):
            raise ReapError("reap vault transaction omitted its warnings")
        conflict_warning = (
            "plan_close conflict preserved for "
            + plan.relative_to(vault).as_posix()
        )
        if conflict_warning in warnings:
            mark_plan_close_conflict(worktree)
            plan_close_status = "conflict"
    else:
        expected_closed = str(prepared.get("closed_plan_sha256") or "")
        if (
            re.search(r"(?m)^status:\s*executed\s*$", plan_text) is None
            or hashlib.sha256(plan.read_bytes()).hexdigest() != expected_closed
            or not result.is_file()
        ):
            raise ReapError("executed-plan recovery does not match the prior prepared transaction")
    written_at = time.monotonic()
    run([sys.executable, str(vault / "scripts/reindex.py")], cwd=vault, label="vault reindex")
    run([str(vault / "scripts/validate-vault.py"), "--summary"], cwd=vault, label="vault validation")
    run(
        [sys.executable, str(vault / "scripts/cmux_surface_lifecycle.py"), "complete-reap", "--worktree", str(worktree), "--current-session", current, "--result-path", str(result), "--vault-root", str(vault)],
        cwd=vault,
        label="reap completion",
    )
    ended = time.monotonic()
    duration = round((ended - started) * 1000)
    emit_lifecycle_event(worktree, "reap-runner", actor="final", counts={
        "validation_ms": round((validated_at - started) * 1000),
        "review_archive_ms": round((archived_at - validated_at) * 1000),
        "prepare_ms": round((prepared_at - archived_at) * 1000),
        "write_ms": round((written_at - prepared_at) * 1000),
        "verify_ms": round((ended - written_at) * 1000),
        "duration_ms": duration,
    }, vault_root=vault)
    return _public_reap_result(
        result_path=str(result),
        result_link=link,
        plan_close_status=plan_close_status,
        duration_ms=duration,
    )


def _finish_provider_runtime(
    runtime: RuntimeSessionManager,
    task_id: str,
    *,
    timeout_seconds: float = 8.0,
) -> None:
    """Exit the provider, then close only its exact owned surface."""

    requested = runtime.request_exit(task_id, task_id)
    if requested.action == "attention-required":
        raise ReapError("provider exit requires attention")
    deadline = time.monotonic() + timeout_seconds
    while True:
        cleaned = runtime.cleanup(task_id, task_id)
        if cleaned.action in {"cleaned", "terminal"}:
            if cleaned.record.resources != OwnedResources():
                raise ReapError(
                    "completed reap retained provider-owned resources"
                )
            return
        if cleaned.action in {"attention-required", "keep-open"}:
            raise ReapError(
                f"provider cleanup stopped at {cleaned.action}"
            )
        if time.monotonic() >= deadline:
            raise ReapError("provider cleanup timed out before exact close")
        time.sleep(0.05)


def apply_reap(
    vault: Path,
    worktree: Path,
    current: str,
    *,
    runtime_manager: RuntimeSessionManager | None = None,
) -> dict[str, Any]:
    """Route one typed task summary through the durable harness callback seam."""

    meta = read_json(worktree / ".task-meta.json")
    summary = validate_summary_for_task(
        read_json(worktree / ".task-summary.json"),
        meta,
        allow_missing_session=True,
    )
    if meta.get("version") not in {3, 4} or meta.get("interaction_policy") != "unattended":
        raise ReapError("reap-runner supports v3/v4 unattended final tasks only")
    try:
        validate_handoff(meta, summary, current, verify_plan_hash=False)
    except ContractError as exc:
        raise ReapError(str(exc)) from exc
    validate_summary_wikilinks(vault, summary)
    approved_plan_state(meta)
    task_id = str(meta.get("task_id") or "")
    authorize_review(vault, worktree, meta)
    try:
        lifecycle = run_reap(
            vault / ".vault-meta" / "harness",
            owner_id=task_id,
            operation_id=task_id,
            summary=summary,
            finalize=lambda _record: _finalize_reap(vault, worktree, current),
        )
    except RuntimeError as exc:
        raise ReapError(f"harness reap failed: {exc}") from exc
    runtime = runtime_manager or RuntimeSessionManager.for_root(
        vault,
        store_root=vault / ".vault-meta" / "harness",
    )
    try:
        _finish_provider_runtime(runtime, task_id)
    except RuntimeSessionError as exc:
        raise ReapError(f"provider cleanup failed: {exc}") from exc
    if lifecycle.result is not None:
        return dict(lifecycle.result)
    prepared = read_json(worktree / ".task-reap-prepared.json")
    return _public_reap_result(
        result_path=str(prepared.get("result_path") or ""),
        result_link=str(prepared.get("result_link") or ""),
        plan_close_status=str(prepared.get("plan_close_status") or ""),
        duration_ms=0,
        idempotent=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, default=ROOT)
    parser.add_argument("--current-session", default="")
    args = parser.parse_args()
    try:
        vault = args.vault_root.expanduser().resolve()
        worktree = args.worktree.expanduser().resolve()
        session = args.current_session.strip() or current_session(vault)
        print(json.dumps(apply_reap(vault, worktree, session), ensure_ascii=False, sort_keys=True))
        return 0
    except (ReapError, ContractError, WikiSummaryError, OSError, ValueError) as exc:
        emit_lifecycle_event(args.worktree, "reap-runner", actor="final", status="error", vault_root=args.vault_root)
        die(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
