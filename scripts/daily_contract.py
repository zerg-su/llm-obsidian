#!/usr/bin/env python3
"""Executable contracts shared by the deterministic daily pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


EVIDENCE_VERSION = 1
SUMMARY_VERSION = 1
EVIDENCE_KINDS = {"session", "log", "git", "hot", "current"}
SESSION_RUNTIMES = {"claude", "codex", "other"}
DATE_RX = re.compile(r"\d{4}-\d{2}-\d{2}")
ITEM_ID_RX = re.compile(r"[a-z]+:[0-9]{3}")
SESSION_ID_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
HASH_RX = re.compile(r"(?<![0-9a-f])[0-9a-f]{7,40}(?![0-9a-f])", re.I)
PATH_RX = re.compile(
    r"(?:^|[\s(])(?:"
    r"/Users/|wiki/|\.vault-meta/|"
    r"(?:scripts|skills|tests|docs|schemas|evals|config|\.codex)/\S+|"
    r"[A-Za-z0-9_.-]+\.(?:toml|json|ya?ml|py|sh|md)\b"
    r")",
    re.I,
)
FLAG_RX = re.compile(r"(?:^|\s)--[a-z0-9-]+\b", re.I)
YAML_RX = re.compile(r"(?:^|\s)(?:sessions|updated|created|status|tags|address):\s", re.I)
MARKUP_RX = re.compile(r"\[\[|\]\]|\]\(|`")
TOKEN_RX = re.compile(r"[^\W_]{3,}", re.UNICODE)
GROUNDING_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "work", "update",
    "для", "как", "это", "при", "или", "работа", "обновление",
}

DAILY_TASK_SECTIONS = {"plans": "## Планы", "reminders": "## Напоминания"}
TASK_STATUS_NAMES = {
    " ": "open",
    "/": "in_progress",
    "x": "done",
    "X": "done",
    "-": "cancelled",
    ">": "migrated",
}
TASK_LINE_RX = re.compile(
    r"^(?P<indent>[ \t]*)-\s+\[(?P<status>[ /xX>\-])\]\s+(?P<body>\S.*)$"
)
LEGACY_REMINDER_RX = re.compile(r"^(?P<indent>[ \t]*)-\s+(?!\[[^]]*\])(?P<body>\S.*)$")
AGENDA_BLOCK_RX = re.compile(r"(?:^|\s)\^(?P<id>agenda-[0-9a-f]{12})\s*$")
AGENDA_MIGRATION_RX = re.compile(
    r"(?:\s+)?#agenda/migrated\s+↪\s+\[\[[^\]\n]+\]\]",
    re.IGNORECASE,
)
TASK_METADATA_MARKER_RX = re.compile(r"(?<!\S)(✅|➕|📅|⏳|🛫|❌|🔁|🆔|⛔|🏁|⏫|🔼|🔽|⏬|🔺)(?=\s|\d|$)")
TASK_DATE_METADATA_RX = re.compile(r"(✅|➕|📅|⏳|🛫|❌)\s*(\d{4}-\d{2}-\d{2})")
TASK_DONE_METADATA_RX = re.compile(r"(?:^|\s)✅\s*\d{4}-\d{2}-\d{2}(?=\s|$)")
TASK_CANCELLED_METADATA_RX = re.compile(r"(?:^|\s)❌\s*\d{4}-\d{2}-\d{2}(?=\s|$)")
TASK_METADATA_NAMES = {
    "✅": "done",
    "➕": "created",
    "📅": "due",
    "⏳": "scheduled",
    "🛫": "start",
    "❌": "cancelled",
    "🔁": "recurrence",
    "🆔": "id",
    "⛔": "depends_on",
    "🏁": "on_completion",
    "⏫": "priority_highest",
    "🔼": "priority_high",
    "🔽": "priority_low",
    "⏬": "priority_lowest",
    "🔺": "priority_urgent",
}


@dataclass(frozen=True)
class DailyTask:
    """One task occurrence parsed from a daily Plans or Reminders section."""

    date: str
    section: str
    line_no: int
    raw: str
    indent: str
    symbol: str
    status: str
    description: str
    metadata_suffix: str
    metadata: dict[str, tuple[str, ...]]
    block_id: str | None
    legacy_plain_reminder: bool = False

    @property
    def normalized_text(self) -> str:
        return normalize_task_text(self.description)

    @property
    def identity(self) -> str:
        if self.block_id:
            return f"id:{self.block_id}"
        digest = hashlib.sha256(
            f"{self.section}\0{self.normalized_text}".encode("utf-8")
        ).hexdigest()[:16]
        return f"legacy:{digest}"


class DailyContractError(ValueError):
    """A daily evidence or summary payload violates its public contract."""


def normalize_task_text(text: str) -> str:
    """Return a stable, human-text identity without migration bookkeeping."""

    clean = AGENDA_MIGRATION_RX.sub(" ", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean.casefold()


def agenda_block_id(date: str, section: str, text: str) -> str:
    """Create the stable ID used by new daily plans and reminders."""

    validate_date(date)
    if section not in DAILY_TASK_SECTIONS:
        raise DailyContractError(f"unknown daily task section: {section}")
    description, _metadata, _existing_id = _split_task_body(text)
    normalized = normalize_task_text(description)
    if not normalized:
        raise DailyContractError("task text must not be empty")
    digest = hashlib.sha256(f"{date}\0{section}\0{normalized}".encode("utf-8")).hexdigest()
    return f"agenda-{digest[:12]}"


def _split_task_body(body: str) -> tuple[str, str, str | None]:
    """Split description, Tasks metadata suffix, and our terminal block ID."""

    working = body.strip()
    block_id: str | None = None
    block_match = AGENDA_BLOCK_RX.search(working)
    if block_match:
        block_id = block_match.group("id")
        working = working[: block_match.start()].rstrip()
    working = AGENDA_MIGRATION_RX.sub(" ", working).strip()
    markers = list(TASK_METADATA_MARKER_RX.finditer(working))
    if not markers:
        return working, "", block_id
    first = markers[0].start()
    description = working[:first].rstrip()
    if not description:
        return working, "", block_id
    return description, working[first:].strip(), block_id


def _metadata_dictionary(suffix: str) -> dict[str, tuple[str, ...]]:
    """Parse the complete Tasks 8.x trailing metadata marker dictionary."""

    values: dict[str, list[str]] = {}
    markers = list(TASK_METADATA_MARKER_RX.finditer(suffix))
    for index, match in enumerate(markers):
        marker = match.group(1)
        start = match.end()
        end = markers[index + 1].start() if index + 1 < len(markers) else len(suffix)
        value = suffix[start:end].strip()
        if marker in {"⏫", "🔼", "🔽", "⏬", "🔺"}:
            value = marker
        elif marker in {"✅", "➕", "📅", "⏳", "🛫", "❌"}:
            date = TASK_DATE_METADATA_RX.search(marker + value)
            value = date.group(2) if date else value
        values.setdefault(TASK_METADATA_NAMES[marker], []).append(value)
    return {key: tuple(items) for key, items in values.items()}


def _active_metadata_suffix(suffix: str) -> str:
    """Remove terminal dates while preserving reusable Tasks metadata."""

    suffix = TASK_DONE_METADATA_RX.sub(" ", suffix)
    suffix = TASK_CANCELLED_METADATA_RX.sub(" ", suffix)
    return re.sub(r"\s+", " ", suffix).strip()


def parse_daily_task(
    line: str,
    *,
    date: str,
    section: str,
    line_no: int,
) -> DailyTask | None:
    """Parse a task line, including legacy plain reminder bullets."""

    validate_date(date)
    if section not in DAILY_TASK_SECTIONS:
        raise DailyContractError(f"unknown daily task section: {section}")
    match = TASK_LINE_RX.fullmatch(line)
    legacy = False
    if match is None and section == "reminders":
        match = LEGACY_REMINDER_RX.fullmatch(line)
        legacy = match is not None
    if match is None:
        return None
    symbol = " " if legacy else match.group("status")
    body = match.group("body")
    description, metadata_suffix, block_id = _split_task_body(body)
    if not description:
        return None
    return DailyTask(
        date=date,
        section=section,
        line_no=line_no,
        raw=line,
        indent=match.group("indent"),
        symbol=symbol,
        status=TASK_STATUS_NAMES[symbol],
        description=description,
        metadata_suffix=metadata_suffix,
        metadata=_metadata_dictionary(metadata_suffix),
        block_id=block_id,
        legacy_plain_reminder=legacy,
    )


def task_open_line(task: DailyTask, block_id: str) -> str:
    """Render one open target occurrence while preserving useful Tasks metadata."""

    suffix = _active_metadata_suffix(task.metadata_suffix)
    parts = [f"{task.indent}- [ ] {task.description}"]
    if suffix:
        parts.append(suffix)
    parts.append(f"^{block_id}")
    return " ".join(parts)


def task_input_open_line(date: str, section: str, text: str) -> str:
    """Normalize user task input into one canonical open occurrence."""

    validate_date(date)
    if section not in DAILY_TASK_SECTIONS:
        raise DailyContractError(f"unknown daily task section: {section}")
    description, metadata_suffix, existing_id = _split_task_body(text)
    if not description:
        raise DailyContractError("task text must not be empty")
    block_id = existing_id or agenda_block_id(date, section, description)
    suffix = _active_metadata_suffix(metadata_suffix)
    parts = [f"- [ ] {description}"]
    if suffix:
        parts.append(suffix)
    parts.append(f"^{block_id}")
    return " ".join(parts)


def task_done_line(task: DailyTask, done_date: str) -> str:
    """Render a completed occurrence with a canonical done date."""

    validate_date(done_date, "done_date")
    suffix = _active_metadata_suffix(task.metadata_suffix)
    block_id = task.block_id or agenda_block_id(task.date, task.section, task.description)
    parts = [f"{task.indent}- [x] {task.description}"]
    if suffix:
        parts.append(suffix)
    parts.extend((f"✅ {done_date}", f"^{block_id}"))
    return " ".join(parts)


def task_migrated_line(task: DailyTask, target_date: str, block_id: str) -> str:
    """Close a source occurrence as migrated and point at the target day."""

    validate_date(target_date, "target_date")
    suffix = _active_metadata_suffix(task.metadata_suffix)
    parts = [f"{task.indent}- [>] {task.description}"]
    if suffix:
        parts.append(suffix)
    parts.extend((f"#agenda/migrated ↪ [[{target_date}]]", f"^{block_id}"))
    return " ".join(parts)


def h2_section_span(text: str, heading: str) -> tuple[int, int]:
    """Return the zero-based half-open body span for an H2 section."""

    lines = text.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == heading) + 1
    except StopIteration as exc:
        raise DailyContractError(f"required section missing: {heading}") from exc
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return start, end


def section_has_indented_children(lines: list[str], line_index: int, indent: str) -> bool:
    """Return true when a task owns an indented Markdown subtree."""

    parent_width = len(indent.expandtabs(4))
    for candidate in lines[line_index + 1 :]:
        if not candidate.strip():
            continue
        candidate_indent = re.match(r"^[ \t]*", candidate).group(0)
        width = len(candidate_indent.expandtabs(4))
        if width <= parent_width:
            return False
        return True
    return False


def validate_date(value: object, field: str = "date") -> str:
    if not isinstance(value, str) or DATE_RX.fullmatch(value) is None:
        raise DailyContractError(f"{field} must be YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise DailyContractError(f"{field} is not a real calendar date") from exc
    return value


def require_keys(value: dict[str, Any], *, required: set[str], allowed: set[str], label: str) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    if missing:
        raise DailyContractError(f"{label} missing keys: {', '.join(missing)}")
    if unknown:
        raise DailyContractError(f"{label} unknown keys: {', '.join(unknown)}")


def bounded_text(
    value: object,
    field: str,
    *,
    minimum: int = 1,
    maximum: int,
    single_line: bool = False,
) -> str:
    if not isinstance(value, str):
        raise DailyContractError(f"{field} must be a string")
    text = re.sub(r"[ \t]+", " ", value.strip())
    if not minimum <= len(text) <= maximum:
        raise DailyContractError(f"{field} length must be {minimum}..{maximum}")
    if "\x00" in text:
        raise DailyContractError(f"{field} contains a NUL byte")
    if single_line and ("\n" in text or "\r" in text):
        raise DailyContractError(f"{field} must be a single line")
    return text


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DailyContractError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DailyContractError(f"{path}: JSON root must be an object")
    return value


def atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        tmp.unlink(missing_ok=True)


def validate_evidence(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DailyContractError("evidence root must be an object")
    require_keys(
        payload,
        required={"schema_version", "date", "generated_at", "bundle_id", "items", "session_map"},
        allowed={"schema_version", "date", "generated_at", "bundle_id", "items", "session_map"},
        label="evidence",
    )
    if payload["schema_version"] != EVIDENCE_VERSION:
        raise DailyContractError("unsupported daily evidence schema_version")
    validate_date(payload["date"])
    if not isinstance(payload["generated_at"], str) or "T" not in payload["generated_at"]:
        raise DailyContractError("generated_at must be an ISO timestamp")
    bundle_id = payload["bundle_id"]
    if not isinstance(bundle_id, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", bundle_id) is None:
        raise DailyContractError("bundle_id must be sha256:<lowercase digest>")
    items = payload["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise DailyContractError("items must contain 1..100 evidence objects")
    seen: set[str] = set()
    for index, item in enumerate(items):
        label = f"items[{index}]"
        if not isinstance(item, dict):
            raise DailyContractError(f"{label} must be an object")
        require_keys(
            item,
            required={"id", "kind", "title", "text"},
            allowed={"id", "kind", "title", "text", "source"},
            label=label,
        )
        item_id = item["id"]
        if not isinstance(item_id, str) or ITEM_ID_RX.fullmatch(item_id) is None:
            raise DailyContractError(f"{label}.id must match kind:NNN")
        if item_id in seen:
            raise DailyContractError(f"duplicate evidence id {item_id}")
        seen.add(item_id)
        if item["kind"] not in EVIDENCE_KINDS or not item_id.startswith(f"{item['kind']}:"):
            raise DailyContractError(f"{label}.kind/id mismatch")
        bounded_text(item["title"], f"{label}.title", maximum=160, single_line=True)
        bounded_text(item["text"], f"{label}.text", maximum=2000)
        if "source" in item:
            source = bounded_text(item["source"], f"{label}.source", maximum=240, single_line=True)
            if source.startswith("/") or ".." in Path(source).parts:
                raise DailyContractError(f"{label}.source must be repository-relative")
    session_map = payload["session_map"]
    if not isinstance(session_map, list) or len(session_map) > 100:
        raise DailyContractError("session_map must be an array with at most 100 entries")
    session_ids: set[str] = set()
    for index, item in enumerate(session_map):
        label = f"session_map[{index}]"
        if not isinstance(item, dict):
            raise DailyContractError(f"{label} must be an object")
        require_keys(
            item,
            required={"session_id", "label"},
            allowed={"session_id", "label", "runtime"},
            label=label,
        )
        session_id = item["session_id"]
        if not isinstance(session_id, str) or SESSION_ID_RX.fullmatch(session_id) is None:
            raise DailyContractError(f"{label}.session_id is invalid")
        if session_id in session_ids:
            raise DailyContractError(f"duplicate session id {session_id}")
        session_ids.add(session_id)
        bounded_text(item["label"], f"{label}.label", maximum=160, single_line=True)
        if "runtime" in item and item["runtime"] not in SESSION_RUNTIMES:
            raise DailyContractError(f"{label}.runtime must be claude, codex, or other")
    canonical = json.dumps(
        {"date": payload["date"], "items": items, "session_map": session_map},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    expected_bundle = "sha256:" + hashlib.sha256(canonical).hexdigest()
    if bundle_id != expected_bundle:
        raise DailyContractError("bundle_id does not match evidence content")
    return payload


def forbidden_output_reason(text: str) -> str | None:
    checks = (
        (HASH_RX, "commit hash"),
        (PATH_RX, "repository path or config filename"),
        (FLAG_RX, "CLI flag"),
        (YAML_RX, "YAML field"),
        (MARKUP_RX, "Markdown link or code markup"),
    )
    for pattern, label in checks:
        if pattern.search(text):
            return label
    return None


def normalized_tokens(text: str) -> set[str]:
    return {token.casefold() for token in TOKEN_RX.findall(text)}


def validate_summary(payload: object, evidence: dict[str, Any]) -> dict[str, Any]:
    validate_evidence(evidence)
    if not isinstance(payload, dict):
        raise DailyContractError("summary root must be an object")
    require_keys(
        payload,
        required={"schema_version", "date", "evidence_bundle_id", "bullets"},
        allowed={"schema_version", "date", "evidence_bundle_id", "bullets", "session_labels"},
        label="summary",
    )
    if payload["schema_version"] != SUMMARY_VERSION:
        raise DailyContractError("unsupported daily summary schema_version")
    if validate_date(payload["date"]) != evidence["date"]:
        raise DailyContractError("summary date does not match evidence date")
    if payload["evidence_bundle_id"] != evidence["bundle_id"]:
        raise DailyContractError("summary evidence_bundle_id does not match evidence")
    items = {item["id"]: item for item in evidence["items"]}
    bullets = payload["bullets"]
    if not isinstance(bullets, list) or not 1 <= len(bullets) <= 7:
        raise DailyContractError("bullets must contain 1..7 items")
    seen_bullets: set[str] = set()
    for index, bullet in enumerate(bullets):
        label = f"bullets[{index}]"
        if not isinstance(bullet, dict):
            raise DailyContractError(f"{label} must be an object")
        require_keys(
            bullet,
            required={"subject", "outcome", "compact", "evidence_ids"},
            allowed={"subject", "outcome", "compact", "evidence_ids"},
            label=label,
        )
        subject = bounded_text(bullet["subject"], f"{label}.subject", maximum=80, single_line=True)
        outcome = bounded_text(
            bullet["outcome"], f"{label}.outcome", minimum=10, maximum=320, single_line=True
        )
        compact = bounded_text(bullet["compact"], f"{label}.compact", maximum=160, single_line=True)
        rendered = f"{subject}: {outcome} {compact}"
        reason = forbidden_output_reason(rendered)
        if reason:
            raise DailyContractError(f"{label} contains forbidden {reason}")
        ids = bullet["evidence_ids"]
        if not isinstance(ids, list) or not ids or any(not isinstance(value, str) or value not in items for value in ids):
            raise DailyContractError(f"{label}.evidence_ids must reference existing evidence")
        if len(set(ids)) != len(ids):
            raise DailyContractError(f"{label}.evidence_ids contains duplicates")
        selected = [items[value] for value in ids]
        if not any(item["kind"] != "hot" for item in selected):
            raise DailyContractError(f"{label} cannot rely on hot cache alone")
        source_text = " ".join(f"{item['title']} {item['text']}" for item in selected)
        subject_tokens = normalized_tokens(subject) - GROUNDING_STOPWORDS
        if not subject_tokens or not subject_tokens.issubset(normalized_tokens(source_text)):
            raise DailyContractError(f"{label}.subject is not grounded in its evidence")
        identity = re.sub(r"\W+", " ", f"{subject} {outcome}").casefold().strip()
        if identity in seen_bullets:
            raise DailyContractError(f"duplicate daily bullet at {label}")
        seen_bullets.add(identity)
    labels = payload.get("session_labels", [])
    if not isinstance(labels, list):
        raise DailyContractError("session_labels must be an array")
    known_sessions = {item["session_id"]: item["label"] for item in evidence["session_map"]}
    seen_sessions: set[str] = set()
    for index, item in enumerate(labels):
        label = f"session_labels[{index}]"
        if not isinstance(item, dict):
            raise DailyContractError(f"{label} must be an object")
        require_keys(item, required={"session_id", "label"}, allowed={"session_id", "label"}, label=label)
        session_id = item["session_id"]
        if session_id not in known_sessions or session_id in seen_sessions:
            raise DailyContractError(f"{label}.session_id is unknown or duplicated")
        seen_sessions.add(session_id)
        text = bounded_text(item["label"], f"{label}.label", maximum=100, single_line=True)
        reason = forbidden_output_reason(text)
        if reason:
            raise DailyContractError(f"{label} contains forbidden {reason}")
        if normalized_tokens(text).isdisjoint(normalized_tokens(known_sessions[session_id])):
            raise DailyContractError(f"{label}.label is not grounded in the original session label")
    return payload


def replace_h2(text: str, heading: str, body_lines: list[str]) -> str:
    lines = text.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == heading)
    except StopIteration as exc:
        raise DailyContractError(f"required section missing: {heading}") from exc
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    rendered = lines[: start + 1] + [""] + body_lines + [""] + lines[end:]
    return "\n".join(rendered).rstrip() + "\n"


def update_frontmatter(text: str, date: str, session_id: str | None, *, last_done: str | None = None) -> str:
    match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.S)
    if match is None:
        raise DailyContractError("page has no valid frontmatter")
    lines = match.group(1).splitlines()

    def set_scalar(key: str, value: str) -> None:
        for index, line in enumerate(lines):
            if re.match(rf"^{re.escape(key)}:\s*", line):
                lines[index] = f"{key}: {value}"
                return
        lines.append(f"{key}: {value}")

    set_scalar("updated", date)
    if last_done is not None:
        set_scalar("last_done", last_done)
    if session_id and session_id != "unknown":
        empty_index = next((i for i, line in enumerate(lines) if re.fullmatch(r"sessions:\s*\[\s*\]", line)), None)
        if empty_index is not None:
            lines[empty_index : empty_index + 1] = ["sessions:", f"  - {session_id}"]
        else:
            start = next((i for i, line in enumerate(lines) if line.strip() == "sessions:"), None)
            if start is None:
                lines.extend(["sessions:", f"  - {session_id}"])
            else:
                end = start + 1
                existing: set[str] = set()
                while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "\t"))):
                    item = re.match(r"^\s*-\s+(\S+)", lines[end])
                    if item:
                        existing.add(item.group(1))
                    end += 1
                if session_id not in existing:
                    lines.insert(end, f"  - {session_id}")
    return "---\n" + "\n".join(lines) + "\n---\n" + text[match.end() :]
