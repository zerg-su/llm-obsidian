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
from wiki_summary_contract import WikiSummaryError, validate_summary  # noqa: E402


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


def archive_reviews(vault: Path, worktree: Path) -> list[str]:
    raw = run(
        [sys.executable, str(vault / "scripts/archive_task_reviews.py"), "--worktree", str(worktree), "--vault-root", str(vault)],
        cwd=vault,
        label="review archive",
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReapError("review archive returned invalid JSON") from exc
    markers = value.get("markers")
    if not isinstance(markers, list) or not markers:
        raise ReapError("final unattended reap requires an approved review archive")
    return [str(Path(item).resolve()) for item in markers]


def summary_with_reviews(vault: Path, worktree: Path, markers: list[str]) -> dict[str, Any]:
    argv = [sys.executable, str(vault / "scripts/parse-wiki-summary.py"), "--json-file", str(worktree / ".task-summary.json")]
    for marker in markers:
        argv.extend(["--review-archive-marker", marker])
    raw = run(argv, cwd=vault, label="summary parsing")
    try:
        return validate_summary(json.loads(raw), allow_missing_session=True)
    except (json.JSONDecodeError, WikiSummaryError) as exc:
        raise ReapError(f"summary contract is invalid: {exc}") from exc


def unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


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
    related = unique(re.findall(r"\[\[([^\]|#]+)", summary["body"]))[:20]
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
    if related:
        lines.extend(["related:", *[f"  - {json.dumps(f'[[{item}]]', ensure_ascii=False)}" for item in related]])
    lines.extend(["---", "", f"# {summary['title']}", "", summary["body"].rstrip(), ""])
    return "\n".join(lines)


def update_page(path: Path, summary: dict[str, Any], task_name: str) -> tuple[str, str]:
    old = path.read_text(encoding="utf-8")
    expected = hashlib.sha256(old.encode()).hexdigest()
    today = date.today().isoformat()
    text = re.sub(r"(?m)^updated:\s*\d{4}-\d{2}-\d{2}\s*$", f"updated: {today}", old, count=1)
    text = text.rstrip() + f"\n\n## {today} {task_name}\n\n{summary['body'].rstrip()}\n"
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
            raise ReapError("pending approved plan hash drifted")
        return plan, "pending"
    if re.search(r"(?m)^status:\s*executed\s*$", text):
        return plan, "executed"
    raise ReapError("approved plan must be pending or executed recovery state")


def apply_reap(vault: Path, worktree: Path, current: str) -> dict[str, Any]:
    started = time.monotonic()
    meta = read_json(worktree / ".task-meta.json")
    raw_summary = validate_summary(read_json(worktree / ".task-summary.json"), allow_missing_session=True)
    if meta.get("version") != 3 or meta.get("interaction_policy") != "unattended":
        raise ReapError("reap-runner supports v3 unattended final tasks only")
    try:
        validate_handoff(meta, raw_summary, current, verify_plan_hash=False)
    except ContractError as exc:
        raise ReapError(str(exc)) from exc
    plan_before, plan_state = approved_plan_state(meta)
    validated_at = time.monotonic()
    markers = archive_reviews(vault, worktree)
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
            "log_entry": f"## [{today}] reap | {meta['task_name']}\n\n`{address}` {link}. {summary['body'][:500]}",
            "hot_bullet": f"{today}: {link} — finalized task result (`{address}`)",
        }
        payload["plan_close"] = {
            "file": plan.relative_to(vault).as_posix(),
            "result_link": link,
            "exec_session": summary.get("session"),
            "expected_sha256": meta["approved_plan_sha256"],
        }
        run([sys.executable, str(vault / "scripts/vault-write.py"), "--output", "json"], cwd=vault, input_text=json.dumps(payload, ensure_ascii=False), label="reap vault transaction")
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
    run(
        [sys.executable, str(vault / "scripts/cmux_surface_lifecycle.py"), "request-exit", "--worktree", str(worktree), "--kind", "task"],
        cwd=vault,
        label="task exit arming",
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
    return {"schema_version": 1, "status": "complete", "result_path": str(result), "result_link": link, "duration_ms": duration}


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
