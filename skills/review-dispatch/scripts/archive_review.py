#!/usr/bin/env python3
"""Archive a validated cross-model review cycle into the coordinator wiki."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NoReturn


DEFAULT_VAULT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(DEFAULT_VAULT / "scripts"))
from review_contract import ReviewContractError, validate_review
from vault_schema import FrontmatterError, parse_frontmatter, split_frontmatter


HISTORY_FILE = ".review-history.json"
ARCHIVE_MARKER = ".review-archive.json"
ARCHIVE_REQUEST = ".review-archive-request.json"
MAX_RESOLUTION_CHARS = 20_000
MAX_REQUEST_CHARS = 6_000
MAX_ROUNDS = 10


class ArchiveError(ValueError):
    pass


def die(message: str, code: int = 1) -> NoReturn:
    print(f"review-archive: {message}", file=sys.stderr)
    raise SystemExit(code)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_object(path: Path, *, required: bool = False) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise ArchiveError(f"{path.name} is missing")
        return {}
    except json.JSONDecodeError as exc:
        raise ArchiveError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ArchiveError(f"{path.name} must contain a JSON object")
    return value


def bounded_text(value: object, *, limit: int, fallback: str = "") -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    return (text or fallback)[:limit]


def yaml_quote(value: object) -> str:
    return json.dumps(bounded_text(value, limit=500), ensure_ascii=False)


def markdown_inline(value: object, *, limit: int = 1000) -> str:
    text = bounded_text(value, limit=limit, fallback="-")
    for char in ("\\", "`", "*", "_", "[", "]", "<", ">", "|"):
        text = text.replace(char, "\\" + char)
    return text


def inert_review_text(value: object, *, fallback: str = "None") -> str:
    """Keep reviewer/executor prose from creating durable graph edges."""
    text = str(value or "").replace("\x00", "").strip() or fallback
    return text.replace("[", r"\[").replace("]", r"\]")


def quote_block(value: object, *, fallback: str = "None") -> list[str]:
    text = inert_review_text(value, fallback=fallback)
    return [f"> {line}" if line else ">" for line in text.splitlines()]


def bullet_block(value: object, *, fallback: str = "None") -> list[str]:
    text = inert_review_text(value, fallback=fallback)
    lines = text.splitlines()
    return [f"- {lines[0]}", *(f"  {line}" if line else "" for line in lines[1:])]


def iso_date(value: object, fallback: str) -> str:
    raw = str(value or "")[:10]
    return raw if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) else fallback


def page_component(value: str) -> str:
    """Return an Obsidian-safe filename/title component without link syntax."""
    normalized = re.sub(r'[\\/:*?"<>|#^\[\]]+', "-", value)
    normalized = " ".join(normalized.split()).strip(" .-")
    raw = (normalized or "review").encode("utf-8")[:160]
    return raw.decode("utf-8", errors="ignore").strip(" .-") or "review"


def validated_round(raw: object, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ArchiveError(f"history round {index} must be an object")
    iteration = raw.get("iteration")
    if not isinstance(iteration, int) or isinstance(iteration, bool) or iteration < 1:
        raise ArchiveError(f"history round {index} has invalid iteration")
    phase = str(raw.get("phase") or "")
    if phase not in {"initial-review", "verify-fixes"}:
        raise ArchiveError(f"history round {index} has invalid phase")
    try:
        review = validate_review(raw.get("review"))
    except ReviewContractError as exc:
        raise ArchiveError(f"history round {index} is invalid: {exc}") from exc
    resolution = raw.get("resolution")
    if resolution is not None:
        if not isinstance(resolution, str):
            raise ArchiveError(f"history round {index} resolution must be text or null")
        resolution = resolution.strip()
        if len(resolution) > MAX_RESOLUTION_CHARS:
            raise ArchiveError(f"history round {index} resolution exceeds {MAX_RESOLUTION_CHARS} characters")
    return {
        "iteration": iteration,
        "phase": phase,
        "received_at": bounded_text(raw.get("received_at"), limit=40),
        "review": review,
        "resolution": resolution or None,
    }


def fallback_history(worktree: Path, meta: dict[str, Any]) -> dict[str, Any]:
    rounds: list[dict[str, Any]] = []
    for iteration, phase, name in (
        (1, "initial-review", ".task-review.json"),
        (2, "verify-fixes", ".task-review-verify.json"),
    ):
        raw = read_object(worktree / name)
        if not raw:
            continue
        try:
            review = validate_review(raw)
        except ReviewContractError as exc:
            raise ArchiveError(f"{name} is invalid: {exc}") from exc
        received_at = datetime.fromtimestamp(
            (worktree / name).stat().st_mtime, timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        rounds.append(
            {
                "iteration": iteration,
                "phase": phase,
                "received_at": received_at,
                "review": review,
                "resolution": None,
            }
        )
    if not rounds:
        return {}
    resolution_path = worktree / ".task-review-resolution.md"
    if resolution_path.is_file():
        resolution = resolution_path.read_text(encoding="utf-8", errors="replace").strip()
        if len(resolution) > MAX_RESOLUTION_CHARS:
            raise ArchiveError(f".task-review-resolution.md exceeds {MAX_RESOLUTION_CHARS} characters")
        rounds[0]["resolution"] = resolution or None
    return {
        "schema_version": 1,
        "review_id": rounds[0]["review"]["run_id"],
        "task_name": bounded_text(meta.get("task_name"), limit=200, fallback="review"),
        "request": {"description": task_request_description(worktree)},
        "rounds": rounds,
        "legacy_fallback": True,
    }


def task_request_description(worktree: Path) -> str:
    """Extract only the human task description, never the review transport prompt."""
    try:
        text = (worktree / ".task-prompt.md").read_text(
            encoding="utf-8", errors="replace"
        ).replace("\x00", "").strip()
    except FileNotFoundError:
        return ""
    lines = text.splitlines()
    description_heading = next(
        (index for index, line in enumerate(lines) if line.strip() == "## Task description"),
        None,
    )
    if description_heading is not None:
        start = description_heading + 1
        end = next(
            (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
            len(lines),
        )
    else:
        start = 1 if lines and lines[0].startswith("# Task:") else 0
        # Legacy/custom prompts contain only human-authored task scope after
        # the H1; preserve their subsections instead of truncating at H2.
        end = len(lines)
    description = "\n".join(lines[start:end]).strip()
    if len(description) > MAX_REQUEST_CHARS:
        description = description[: MAX_REQUEST_CHARS - 1].rstrip() + "…"
    return description


def load_history(worktree: Path, meta: dict[str, Any]) -> dict[str, Any]:
    history = read_object(worktree / HISTORY_FILE)
    if not history:
        return fallback_history(worktree, meta)
    if history.get("schema_version") != 1:
        raise ArchiveError(".review-history.json schema_version must be 1")
    review_id = bounded_text(history.get("review_id"), limit=100)
    if not review_id:
        raise ArchiveError(".review-history.json review_id is missing")
    raw_rounds = history.get("rounds")
    if not isinstance(raw_rounds, list) or len(raw_rounds) > MAX_ROUNDS:
        raise ArchiveError(f".review-history.json must contain at most {MAX_ROUNDS} rounds")
    if not raw_rounds:
        return {}
    rounds = [validated_round(raw, index) for index, raw in enumerate(raw_rounds, 1)]
    for expected, round_ in enumerate(rounds, 1):
        expected_phase = "initial-review" if expected == 1 else "verify-fixes"
        if round_["iteration"] != expected or round_["phase"] != expected_phase:
            raise ArchiveError(".review-history.json rounds must be ordered initial-review then verify-fixes")
    run_ids = [round_["review"]["run_id"] for round_ in rounds]
    if len(run_ids) != len(set(run_ids)):
        raise ArchiveError(".review-history.json contains duplicate round run_id values")
    if run_ids[0] != review_id:
        raise ArchiveError(".review-history.json review_id must match the initial round run_id")
    request = history.get("request") or {}
    if not isinstance(request, dict):
        raise ArchiveError(".review-history.json request must be an object")
    description = request.get("description") or task_request_description(worktree)
    if not isinstance(description, str) or len(description) > MAX_REQUEST_CHARS:
        raise ArchiveError(
            f".review-history.json request description must be at most {MAX_REQUEST_CHARS} characters"
        )
    return {
        "schema_version": 1,
        "review_id": review_id,
        "task_name": bounded_text(history.get("task_name"), limit=200, fallback="review"),
        "request": {
            "description": description.strip(),
            "base_branch": bounded_text(request.get("base_branch"), limit=300),
            "branch": bounded_text(request.get("branch"), limit=300),
            "review_mode": bounded_text(request.get("review_mode"), limit=20),
        },
        "rounds": rounds,
        "legacy_fallback": bool(history.get("legacy_fallback")),
    }


def plan_wikilink(task_meta: dict[str, Any], worktree: Path, vault: Path) -> str:
    raw = str(task_meta.get("plan_file") or "").strip()
    if not raw:
        return ""
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (worktree / candidate).resolve()
    else:
        candidate = candidate.resolve()
    wiki = (vault / "wiki").resolve()
    if candidate.suffix.lower() != ".md" or wiki not in candidate.parents:
        return ""
    return f"[[{candidate.stem}]]"


def render_review_page(
    *,
    meta: dict[str, Any],
    task_meta: dict[str, Any],
    history: dict[str, Any],
    title: str,
    address: str,
    created: str,
    updated: str,
    sessions: list[str],
    plan_link: str,
) -> str:
    rounds = history["rounds"]
    final = rounds[-1]["review"]
    verdict = final["verdict"]
    status = {"approve": "resolved", "changes-requested": "developing", "blocked": "blocked"}[verdict]
    lines = [
        "---",
        "type: review",
        f"title: {yaml_quote(title)}",
        f"address: {address}",
        f"created: {created}",
        f"updated: {updated}",
        "tags:",
        "  - review",
        "  - cross-model",
        f"status: {status}",
        "sessions:",
    ]
    lines.extend(f"  - {yaml_quote(session)}" for session in sessions)
    if not sessions:
        lines[-1] = "sessions: []"
    lines.extend(
        [
            f"review_id: {yaml_quote(history['review_id'])}",
            f"reviewer_runtime: {yaml_quote(meta.get('reviewer_runtime') or 'unknown')}",
            f"reviewer_model: {yaml_quote(meta.get('reviewer_model') or 'default')}",
            f"reviewer_effort: {yaml_quote(meta.get('reviewer_effort') or 'default')}",
            f"review_mode: {yaml_quote(meta.get('review_mode') or final['mode'])}",
            f"rounds: {len(rounds)}",
            f"verdict: {verdict}",
        ]
    )
    if plan_link:
        lines.extend(["related:", f"  - {yaml_quote(plan_link)}"])
    lines.extend(
        [
            "---",
            "",
            f"# {title}",
            "",
            "> [!abstract] Outcome",
            f"> **Task:** {markdown_inline(history['task_name'])}",
            f"> **Final verdict:** `{verdict}`",
            f"> **Reviewer:** {markdown_inline(meta.get('reviewer_runtime') or 'unknown')} · "
            f"{markdown_inline(meta.get('reviewer_model') or 'default')} · "
            f"effort `{markdown_inline(meta.get('reviewer_effort') or 'default')}`",
            f"> **Executor:** {markdown_inline(meta.get('executor_runtime') or task_meta.get('executor_runtime') or 'unknown')}",
            f"> **Mode:** `{markdown_inline(meta.get('review_mode') or final['mode'])}` · "
            f"**rounds:** {len(rounds)}",
            f"> **Started:** {markdown_inline(meta.get('started_at') or created)}",
            f"> **Updated:** {markdown_inline(rounds[-1].get('received_at') or updated)}",
        ]
    )
    if plan_link:
        lines.append(f"> **Plan:** {plan_link}")
    if history.get("legacy_fallback") and int(meta.get("iteration") or len(rounds)) > len(rounds):
        lines.extend(
            [
                ">",
                "> [!warning] Legacy backfill",
                "> One or more overwritten intermediate verification rounds could not be reconstructed.",
            ]
        )

    lines.extend(
        [
            "",
            "## Review request",
            "",
            f"Review the implementation for **{markdown_inline(history['task_name'])}** "
            f"in `{markdown_inline(meta.get('branch') or task_meta.get('branch') or 'current branch')}` "
            f"against `{markdown_inline(meta.get('base_branch') or task_meta.get('base_branch') or 'the approved baseline')}` "
            f"using the `{markdown_inline(meta.get('review_mode') or final['mode'])}` cross-model gate.",
        ]
    )
    request_description = str(history.get("request", {}).get("description") or "").strip()
    if request_description:
        lines.extend(
            [
                "",
                "> [!quote] Original task request",
                *quote_block(request_description),
            ]
        )
    if plan_link:
        lines.append(f"Approved scope and rationale: {plan_link}.")

    for round_ in rounds:
        review = round_["review"]
        lines.extend(
            [
                "",
                f"## Round {round_['iteration']} — {review['verdict']}",
                "",
                f"- Phase: `{round_['phase']}`",
                f"- Run ID: `{markdown_inline(review['run_id'])}`",
                f"- Received: {markdown_inline(round_.get('received_at') or '-')}",
                "",
                "### Findings",
                "",
            ]
        )
        if not review["findings"]:
            lines.append("No findings.")
        for index, finding in enumerate(review["findings"], 1):
            location = finding["file"] + (f":{finding['line']}" if finding.get("line") else "")
            lines.extend(
                [
                    f"#### {index}. {finding['severity']} — {markdown_inline(finding['title'], limit=300)}",
                    "",
                    f"- File: `{markdown_inline(location)}`",
                    "- Evidence:",
                    *quote_block(finding["evidence"]),
                    "- Recommendation:",
                    *quote_block(finding["recommendation"]),
                    "",
                ]
            )
        lines.extend(["### Executor resolution", ""])
        if round_.get("resolution"):
            lines.extend(["> [!note] Resolution snapshot", *quote_block(round_["resolution"])])
        else:
            lines.append("No resolution was required or recorded for this round.")
        for heading, key in (
            ("Verification gaps", "verification_gaps"),
            ("Residual risks", "residual_risks"),
            ("Notes for executor", "notes_for_executor"),
        ):
            lines.extend(["", f"### {heading}", ""])
            values = review[key]
            if not values:
                lines.append("- None")
            else:
                for value in values:
                    lines.extend(bullet_block(value))

    lines.extend(
        [
            "",
            "## Archive boundary",
            "",
            "This page keeps validated review findings, executor resolutions, and final verification. "
            "Dedicated raw prompts, compressed callback payloads, command logs, sockets, and cmux identifiers are intentionally excluded. "
            "Validated findings and executor resolutions are retained as review evidence.",
            "",
        ]
    )
    return "\n".join(lines)


def existing_page_state(path: Path, review_id: str) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    block = split_frontmatter(text)
    if block is None:
        raise ArchiveError(f"existing archive {path.name} has no frontmatter")
    try:
        frontmatter = parse_frontmatter(block)
    except FrontmatterError as exc:
        raise ArchiveError(f"existing archive {path.name} has invalid frontmatter: {exc}") from exc
    if str(frontmatter.get("review_id") or "") != review_id:
        raise ArchiveError(f"existing archive {path.name} belongs to another review_id")
    address = str(frontmatter.get("address") or "")
    created = str(frontmatter.get("created") or "")
    if not re.fullmatch(r"c-\d{6}", address):
        raise ArchiveError(f"existing archive {path.name} has invalid address")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", created):
        raise ArchiveError(f"existing archive {path.name} has invalid created date")
    return address, created


def default_allocate(vault: Path) -> str:
    result = subprocess.run(
        [str(vault / "scripts" / "allocate-address.sh")],
        cwd=vault,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ArchiveError((result.stderr or result.stdout).strip() or "address allocation failed")
    address = result.stdout.strip()
    if not re.fullmatch(r"c-\d{6}", address):
        raise ArchiveError(f"address allocator returned {address!r}")
    return address


def default_write(vault: Path, payload: dict[str, Any]) -> None:
    result = subprocess.run(
        [sys.executable, str(vault / "scripts" / "vault-write.py"), "--output", "json"],
        input=json.dumps(payload, ensure_ascii=False),
        cwd=vault,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ArchiveError((result.stderr or result.stdout).strip() or "vault-write failed")


def archive_review(
    worktree: Path,
    vault: Path,
    *,
    session: str,
    allocate: Callable[[Path], str] = default_allocate,
    write: Callable[[Path, dict[str, Any]], None] = default_write,
    dry_run: bool = False,
) -> dict[str, Any]:
    worktree = worktree.expanduser().resolve()
    vault = vault.expanduser().resolve()
    meta = read_object(worktree / ".review-meta.json", required=True)
    task_meta = read_object(worktree / ".task-meta.json")
    history = load_history(worktree, meta)
    if not history:
        return {"schema_version": 1, "status": "no-review"}

    review_id = history["review_id"]
    task_name = history["task_name"]
    started_fallback = datetime.now().date().isoformat()
    created = iso_date(meta.get("started_at"), started_fallback)
    final_received_at = history["rounds"][-1].get("received_at")
    updated = iso_date(final_received_at, created)
    short_id = hashlib.sha256(review_id.encode("utf-8")).hexdigest()[:12]
    title_task = page_component(bounded_text(task_name, limit=140, fallback="review"))
    title = f"Cross-model review — {title_task} — {short_id}"
    filename = f"{title}.md"
    relative = Path("wiki") / "meta" / "reviews" / filename
    page = vault / relative
    exists = page.is_file()
    if exists:
        address, created = existing_page_state(page, review_id)
    else:
        address = "c-000001" if dry_run else allocate(vault)

    sessions: list[str] = []
    for candidate in (task_meta.get("origin_session"), session):
        value = bounded_text(candidate, limit=200)
        if value and value != "unknown" and value not in sessions:
            sessions.append(value)
    plan_link = plan_wikilink(task_meta, worktree, vault)
    content = render_review_page(
        meta=meta,
        task_meta=task_meta,
        history=history,
        title=title,
        address=address,
        created=created,
        updated=updated,
        sessions=sessions,
        plan_link=plan_link,
    )
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    final = history["rounds"][-1]["review"]
    wikilink = f"[[{title}]]"
    result = {
        "schema_version": 1,
        "status": "dry-run" if dry_run else "archived",
        "review_id": review_id,
        "path": relative.as_posix(),
        "title": title,
        "wikilink": wikilink,
        "address": address,
        "verdict": final["verdict"],
        "rounds": len(history["rounds"]),
        "content_sha256": content_hash,
    }
    if dry_run:
        return result
    if exists and hashlib.sha256(page.read_bytes()).hexdigest() == content_hash:
        result["status"] = "already-current"
    else:
        spec: dict[str, Any] = {
            "op": "update" if exists else "create",
            "path": relative.as_posix(),
            "content": content,
        }
        if exists:
            spec["expected_sha256"] = hashlib.sha256(page.read_bytes()).hexdigest()
        payload: dict[str, Any] = {
            "schema_version": 1,
            "request_id": f"review-archive:{short_id}:{content_hash[:12]}",
            "actor": "review-archive",
            "session": session or "unknown",
            "pages": [spec],
        }
        if not exists:
            log_title = bounded_text(task_name, limit=180, fallback="cross-model review").replace("|", "-")
            payload["log_entry"] = (
                f"## [{updated}] review | {log_title}\n\n"
                f"`{address}` {wikilink}. {len(history['rounds'])} round(s), "
                f"final verdict `{final['verdict']}`; reviewer "
                f"{bounded_text(meta.get('reviewer_runtime'), limit=40, fallback='unknown')}/"
                f"{bounded_text(meta.get('reviewer_model'), limit=80, fallback='default')}."
            )
        write(vault, payload)

    marker = dict(result)
    marker["updated_at"] = utc_now()
    atomic_json(worktree / ARCHIVE_MARKER, marker)
    (worktree / ARCHIVE_REQUEST).unlink(missing_ok=True)
    return result


def current_session(vault: Path) -> str:
    result = subprocess.run(
        [str(vault / "scripts" / "current-session-id.sh")],
        cwd=vault,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", required=True, help="reviewed task worktree")
    parser.add_argument("--vault-root", default=str(DEFAULT_VAULT), help="coordinator vault root")
    parser.add_argument("--session", default="", help="coordinator/executor session provenance")
    parser.add_argument("--dry-run", action="store_true", help="validate and render metadata without writing")
    parser.add_argument("--json", action="store_true", help="print canonical JSON result")
    args = parser.parse_args(argv)
    worktree = Path(args.worktree)
    vault = Path(args.vault_root)
    try:
        result = archive_review(
            worktree,
            vault,
            session=args.session or current_session(vault),
            dry_run=args.dry_run,
        )
    except (ArchiveError, OSError) as exc:
        die(str(exc), 3)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    elif result["status"] == "no-review":
        print("review-archive: no validated review artifacts")
    else:
        print(f"review-archive: {result['status']} {result['wikilink']} ({result['path']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
