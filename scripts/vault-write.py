#!/usr/bin/env python3
"""Transactional vault mutation dispatcher.

The model generates ONE JSON payload per save-operation; this script fans it
out to wiki/log.md and wiki/hot.md with hard cap enforcement. Replaces the
old multi-Edit choreography (page -> index -> log -> hot) where each edit was
a drift point and caps depended on model discipline.

Payload (stdin or --file), all mutation keys optional:

    {
      "schema_version": 1,
      "request_id": "optional-idempotency/correlation-id",
      "log_entry":     "## [YYYY-MM-DD] verb | Title\\n- body...",
      "hot_bullet":    "YYYY-MM-DD: [[Page]] — one-liner (`c-NNNNNN`)",
      "hot_recent_remove_addresses": ["c-NNNNNN"],
      "hot_narrative": "replaces ## Last Updated body, <=120 words",
      "hot_threads":   {"add": ["- **Open**: ..."], "resolve": ["substring"]},
      "plan_close":    {"file": "wiki/plans/<name>.md",
                        "result_link": "[[Title]]",
                        "exec_session": "<id>|null",
                        "expected_sha256": "<approved-plan-hash>|null"},
      "pages": [
        {"op": "create", "path": "wiki/concepts/New.md", "content": "..."},
        {"op": "update", "path": "wiki/index.md", "content": "...",
         "expected_sha256": "<hash of current file>"}
      ],
      "moves": [
        {"from": "wiki/old.md", "to": "wiki/New.md",
         "expected_sha256": "<hash of source file>"}
      ],
      "manifest_update": {"path": ".raw/.manifest.json",
                           "expected_sha256": "<hash>",
                           "merge": {"address_map": {"wiki/...": "c-000001"}}},
      "actor": "save|ingest|reap|hook|...",
      "session": "<runtime session id>"
    }

plan_close (reap final): strict lifecycle close of a plan page. Preconditions
(file inside wiki/plans/, single status line, status == pending) violated ->
exit 2, nothing written. Applies: status -> executed, updated bump, executor
session appended to sessions: (plan-capture format), 'Результат: <link>'
line appended to body.

Ownership contract for hot.md sections:
  - ## Recent Changes    — THIS SCRIPT (prepend bullet, evict >15, truncate essence only)
  - ## Last Updated      — model via hot_narrative (cap 120 words, FAIL if over)
  - ## Active Threads    — model via hot_threads (cap 8, FAIL with listing if over)
  - ## Key Recent Facts  — model-curated durable facts; script never touches it

All file contents are built and validated before mutation. A durable journal
makes a multi-file operation recoverable by roll-forward after process death;
normal validation/conflict failures write nothing.

Usage:
  echo '{"hot_bullet": "YYYY-MM-DD: [[Page]] — essence (`c-NNNNNN`)"}' | ./scripts/vault-write.py
  ./scripts/vault-write.py --file payload.json [--dry-run]
  ./scripts/vault-write.py --file payload.json --output json
  ./scripts/vault-write.py --sha256 wiki/path.md
  ./scripts/vault-write.py --recover

Exit codes: 0 ok, 1 lock/io failure, 2 invariant/cap violation, 3 bad payload,
4 optimistic-concurrency conflict.

`--output json` returns the stable v1 response contract from
schemas/vault-write-response-v1.schema.json. Existing text output remains the
default for shell and skill compatibility.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from pipeline_events import emit_event
from plan_lifecycle import PlanCloseError, render_plan_close
from vault_schema import (
    ADDRESS_CUTOFF,
    ADDRESS_EXEMPT_TYPES,
    ADDRESS_RX,
    DATE_RX,
    FrontmatterError,
    LOG_ENTRY_RX,
    REQUIRED_KEYS,
    parse_frontmatter,
    split_frontmatter,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HOT_FILE = REPO_ROOT / "wiki" / "hot.md"
LOG_FILE = REPO_ROOT / "wiki" / "log.md"
LOCK_FILE = REPO_ROOT / ".vault-meta" / ".vault-write.lock"
JOURNAL_FILE = REPO_ROOT / ".vault-meta" / ".vault-write-journal.json"


def atomic_write(path: Path, text: str) -> None:
    """Same-dir tmp + os.replace: a crash between the hot.md and log.md writes
    can no longer leave a half-written file (readers see old or new, never a
    torn write). ".tmp." infix keeps strays covered by the *.tmp.* gitignore."""
    tmp = path.parent / f"{path.name}.tmp.{os.getpid()}"
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

# Caps (mirrored in scripts/validate-vault.py — keep in sync)
HOT_TOTAL_WORDS = 800
RC_MAX_BULLETS = 15
RC_BULLET_CHARS = 160
THREADS_MAX = 8
NARRATIVE_WORDS = 120

MUTATION_KEYS = {
    "log_entry",
    "hot_bullet",
    "hot_recent_remove_addresses",
    "hot_narrative",
    "hot_threads",
    "plan_close",
    "pages",
    "moves",
    "manifest_update",
}
KNOWN_KEYS = MUTATION_KEYS | {"actor", "session", "schema_version", "request_id"}

RC_HEADING = "## Recent Changes"
THREADS_HEADING = "## Active Threads"
NARRATIVE_HEADING = "## Last Updated"
HOT_LINK_RX = re.compile(r"\[\[[^\]\r\n]+\]\]")
HOT_ADDRESS_TOKEN_RX = re.compile(r"(?<![A-Za-z0-9])c-\d{6}(?!\d)")
OUTPUT_JSON = False
TRANSACTION_ID = ""
REQUEST_ID: str | None = None


def result_json(
    status: str,
    *,
    written_paths: list[str] | None = None,
    warnings: list[str] | None = None,
    error: dict | None = None,
    extra: dict | None = None,
) -> None:
    payload = {
        "schema_version": 1,
        "transaction_id": TRANSACTION_ID or str(uuid.uuid4()),
        "request_id": REQUEST_ID,
        "status": status,
        "written_paths": written_paths or [],
        "warnings": warnings or [],
    }
    if error is not None:
        payload["error"] = error
    if extra:
        payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))


def fail(code: int, msg: str, *, paths: list[str] | None = None) -> int:
    if OUTPUT_JSON:
        categories = {1: "io", 2: "invariant", 3: "invalid_request", 4: "conflict"}
        result_json(
            "error",
            error={
                "category": categories.get(code, "unknown"),
                "retryable": code in {1, 4},
                "message": msg,
                "paths": paths or [],
            },
        )
        return code
    print(f"vault-write: {msg}", file=sys.stderr)
    return code


def one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def safe_hot_bullet(value: object) -> tuple[str, bool]:
    """Validate a Recent Changes bullet and truncate only its prose essence.

    Date, wikilink, and DragonScale address are structural metadata.  Blind
    character truncation used to turn addresses such as ``c-000047`` into
    ``c-00004…``.  Long bullets are normalized only when truncation is needed;
    short valid caller formatting remains untouched.
    """
    if not isinstance(value, str) or not value.strip():
        raise PayloadError("hot_bullet must be a non-empty string")
    bullet = one_line(value)
    if not bullet.startswith("- "):
        bullet = "- " + bullet

    date_match = re.match(r"^- (\d{4}-\d{2}-\d{2}):\s+", bullet)
    if not date_match or not DATE_RX.fullmatch(date_match.group(1)):
        raise PayloadError("hot_bullet must start with 'YYYY-MM-DD: '")
    link = HOT_LINK_RX.search(bullet, date_match.end())
    if link is None:
        raise PayloadError("hot_bullet must contain one [[wikilink]]")
    addresses = list(HOT_ADDRESS_TOKEN_RX.finditer(bullet, link.end()))
    if len(addresses) != 1:
        raise PayloadError("hot_bullet must contain exactly one c-NNNNNN address after its wikilink")
    address = addresses[0].group(0)
    if len(bullet) <= RC_BULLET_CHARS:
        return bullet, False

    prefix = bullet[: link.end()].rstrip(" —-")
    raw_essence = bullet[link.end() : addresses[0].start()]
    essence = raw_essence.strip(" `()—-:;")
    suffix = f" (`{address}`)"
    separator = " — "
    available = RC_BULLET_CHARS - len(prefix) - len(separator) - len(suffix)
    if available < 2:
        raise PayloadError(
            "hot_bullet structural date/wikilink/address exceed the 160-character cap"
        )
    if not essence:
        essence = "update"
    if len(essence) > available:
        essence = essence[: available - 1].rstrip() + "…"
    rendered = prefix + separator + essence + suffix
    if len(rendered) > RC_BULLET_CHARS:  # defensive: should be impossible above
        raise PayloadError("hot_bullet could not be safely truncated")
    return rendered, True


def section_bounds(lines: list[str], heading: str) -> tuple[int, int] | None:
    """Return (start, end) line indexes of a section's BODY (heading excluded).
    end is the index of the next '## ' heading or len(lines)."""
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == heading)
    except StopIteration:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return start + 1, end


def bullets_of(lines: list[str]) -> list[str]:
    return [l for l in lines if l.lstrip().startswith("- ")]


def replace_section(lines: list[str], heading: str, new_body: list[str]) -> list[str]:
    bounds = section_bounds(lines, heading)
    if bounds is None:
        raise ValueError(f"section not found: {heading}")
    start, end = bounds
    return lines[:start] + [""] + new_body + [""] + lines[end:]


def set_frontmatter_updated(text: str, today: str) -> str:
    return re.sub(r"^updated: .*$", f"updated: {today}", text, count=1, flags=re.M)


def apply_hot(payload: dict, hot_text: str, today: str) -> tuple[str, list[str]]:
    """Return (new_hot_text, warnings). Raises CapViolation on hard failures."""
    warnings: list[str] = []
    lines = hot_text.split("\n")

    # Recent Changes: targeted cache correction, prepend, truncate, and evict.
    removals = payload.get("hot_recent_remove_addresses") or []
    if not isinstance(removals, list) or any(
        not isinstance(value, str) or HOT_ADDRESS_TOKEN_RX.fullmatch(value) is None
        for value in removals
    ):
        raise PayloadError("hot_recent_remove_addresses must contain c-NNNNNN strings")
    if len(removals) != len(set(removals)) or len(removals) > 5:
        raise PayloadError(
            "hot_recent_remove_addresses must be unique and contain at most 5 items"
        )
    bullet = payload.get("hot_bullet")
    if bullet or removals:
        bounds = section_bounds(lines, RC_HEADING)
        if bounds is None:
            raise CapViolation(f"hot.md has no '{RC_HEADING}' section")
        existing = bullets_of(lines[bounds[0]:bounds[1]])
        kept = [
            item
            for item in existing
            if not any(address in HOT_ADDRESS_TOKEN_RX.findall(item) for address in removals)
        ]
        removed = len(existing) - len(kept)
        if removals and removed == 0:
            warnings.append("hot_recent_remove_addresses matched no Recent Changes bullets")
        if bullet:
            b, truncated = safe_hot_bullet(bullet)
            if truncated:
                warnings.append(f"hot_bullet essence truncated to {RC_BULLET_CHARS} chars")
            kept.insert(0, b)
        if len(kept) > RC_MAX_BULLETS:
            evicted = len(kept) - RC_MAX_BULLETS
            kept = kept[:RC_MAX_BULLETS]
            warnings.append(f"Recent Changes: evicted {evicted} oldest bullet(s)")
        lines = replace_section(lines, RC_HEADING, kept)

    # Last Updated narrative
    narrative = payload.get("hot_narrative")
    if narrative:
        n_words = len(narrative.split())
        if n_words > NARRATIVE_WORDS:
            raise CapViolation(
                f"hot_narrative is {n_words} words (cap {NARRATIVE_WORDS}) — shorten it"
            )
        lines = replace_section(lines, NARRATIVE_HEADING, narrative.strip().split("\n"))

    # Active Threads
    threads = payload.get("hot_threads") or {}
    if threads:
        bounds = section_bounds(lines, THREADS_HEADING)
        if bounds is None:
            raise CapViolation(f"hot.md has no '{THREADS_HEADING}' section")
        current = bullets_of(lines[bounds[0]:bounds[1]])
        for pat in threads.get("resolve", []):
            before = len(current)
            current = [t for t in current if pat not in t]
            if len(current) == before:
                warnings.append(f"hot_threads.resolve: no thread matched {pat!r}")
        for add in threads.get("add", []):
            a = one_line(add)
            if not a.startswith("- "):
                a = "- " + a
            current.insert(0, a)
        if len(current) > THREADS_MAX:
            listing = "\n".join(f"  {t[:120]}" for t in current)
            raise CapViolation(
                f"Active Threads would be {len(current)} (cap {THREADS_MAX}). "
                f"Resolve some first:\n{listing}"
            )
        lines = replace_section(lines, THREADS_HEADING, current)

    new_text = set_frontmatter_updated("\n".join(lines), today)
    total_words = len(new_text.split())
    if total_words > HOT_TOTAL_WORDS:
        raise CapViolation(
            f"hot.md would be {total_words} words (cap {HOT_TOTAL_WORDS}). "
            "Model-owned sections (Last Updated / Key Recent Facts / Active Threads) "
            "are too fat — trim them (or run the one-time hot rebuild)."
        )
    return new_text, warnings


def apply_log(log_entry: str, log_text: str, today: str) -> str:
    entry = log_entry.strip()
    heading = entry.splitlines()[0] if entry else ""
    if LOG_ENTRY_RX.fullmatch(heading) is None:
        raise CapViolation(
            "log_entry heading must match "
            "'## [YYYY-MM-DD[ HH:MM]] operation | title'"
        )
    m = re.search(r"^## \[", log_text, flags=re.M)
    if m:
        idx = m.start()
        new_text = log_text[:idx] + entry + "\n\n" + log_text[idx:]
    else:
        new_text = log_text.rstrip("\n") + "\n\n" + entry + "\n"
    return set_frontmatter_updated(new_text, today)


class CapViolation(Exception):
    pass


class PayloadError(ValueError):
    pass


class ConflictError(ValueError):
    pass


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_repo_path(rel: str, *, prefix: str | None = None) -> Path:
    if not rel or Path(rel).is_absolute():
        raise PayloadError(f"path must be repository-relative (got {rel!r})")
    path = (REPO_ROOT / rel).resolve()
    if REPO_ROOT not in path.parents:
        raise PayloadError(f"path escapes repository: {rel!r}")
    if prefix:
        allowed = (REPO_ROOT / prefix.rstrip("/")).resolve()
        if not rel.startswith(prefix) or allowed not in path.parents:
            raise PayloadError(f"path must stay inside {prefix!r}: {rel!r}")
    return path


def validate_page_content(rel: str, content: str) -> None:
    if not rel.endswith(".md"):
        return
    block = split_frontmatter(content)
    if block is None:
        raise PayloadError(f"{rel}: missing or unclosed frontmatter")
    try:
        fm = parse_frontmatter(block)
    except FrontmatterError as exc:
        raise PayloadError(f"{rel}: invalid frontmatter: {exc}") from exc
    missing = [key for key in REQUIRED_KEYS if key not in fm]
    if missing:
        raise PayloadError(f"{rel}: missing required frontmatter: {', '.join(missing)}")
    if not isinstance(fm.get("tags"), list) or not fm["tags"]:
        raise PayloadError(f"{rel}: tags must be a non-empty list")
    if not isinstance(fm.get("sessions"), list):
        raise PayloadError(f"{rel}: sessions must be a list")
    for key in ("created", "updated"):
        if not DATE_RX.fullmatch(str(fm.get(key) or "")):
            raise PayloadError(f"{rel}: {key} must be YYYY-MM-DD")
    address = fm.get("address")
    if address is not None:
        match = ADDRESS_RX.fullmatch(str(address))
        if match is None or int(match.group(1)) == 0:
            raise PayloadError(f"{rel}: invalid non-zero c-NNNNNN address")
    created = str(fm.get("created") or "")
    requires_address = (
        str(fm.get("type") or "") not in ADDRESS_EXEMPT_TYPES
        and DATE_RX.fullmatch(created)
        and time.strptime(created, "%Y-%m-%d")[:3]
        >= (ADDRESS_CUTOFF.year, ADDRESS_CUTOFF.month, ADDRESS_CUTOFF.day)
    )
    if requires_address and address is None:
        raise PayloadError(f"{rel}: post-rollout content page requires address")
    if str(fm.get("type") or "") == "source":
        source_class = str(fm.get("source_class") or "")
        if source_class not in {"official", "internal", "third-party"}:
            raise PayloadError(
                f"{rel}: source_class must be official|internal|third-party"
            )
        if not DATE_RX.fullmatch(str(fm.get("verified_at") or "")):
            raise PayloadError(f"{rel}: source verified_at must be YYYY-MM-DD")
        if not re.fullmatch(r"[0-9a-f]{64}", str(fm.get("content_sha256") or "")):
            raise PayloadError(f"{rel}: source content_sha256 must be lowercase SHA-256")


def page_writes(specs: object) -> list[tuple[Path, str]]:
    if specs is None:
        return []
    if not isinstance(specs, list):
        raise PayloadError("pages must be an array")
    writes: list[tuple[Path, str]] = []
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise PayloadError(f"pages[{index}] must be an object")
        unknown = set(spec) - {"op", "path", "content", "expected_sha256"}
        if unknown:
            raise PayloadError(f"pages[{index}] unknown keys: {sorted(unknown)}")
        op = spec.get("op")
        rel = str(spec.get("path") or "")
        content = spec.get("content")
        if op not in {"create", "update"}:
            raise PayloadError(f"pages[{index}].op must be create|update")
        if not isinstance(content, str):
            raise PayloadError(f"pages[{index}].content must be a string")
        path = safe_repo_path(rel, prefix="wiki/")
        if path in {HOT_FILE.resolve(), LOG_FILE.resolve()}:
            key = "hot_*" if path == HOT_FILE.resolve() else "log_entry"
            raise PayloadError(
                f"pages[{index}].path {rel!r} is writer-owned; use the dedicated {key} payload"
            )
        validate_page_content(rel, content)
        if op == "create":
            if path.exists():
                raise ConflictError(f"create collision: {rel} already exists")
            if spec.get("expected_sha256") is not None:
                raise PayloadError(f"pages[{index}]: create must not carry expected_sha256")
        else:
            if not path.is_file():
                raise ConflictError(f"update target missing: {rel}")
            expected = str(spec.get("expected_sha256") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise PayloadError(f"pages[{index}]: update requires lowercase SHA-256")
            actual = sha256_text(path.read_text(encoding="utf-8"))
            if actual != expected:
                raise ConflictError(
                    f"update conflict: {rel} is {actual}, expected {expected}"
                )
        writes.append((path, content))
    return writes


def page_moves(specs: object) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
    """Validate optimistic page renames and render them as write + delete.

    The transaction journal writes the destination before deleting the source,
    so recovery can always roll a partially completed rename forward.
    """
    if specs is None:
        return [], []
    if not isinstance(specs, list):
        raise PayloadError("moves must be an array")
    writes: list[tuple[Path, str]] = []
    deletes: list[tuple[Path, str]] = []
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise PayloadError(f"moves[{index}] must be an object")
        unknown = set(spec) - {"from", "to", "expected_sha256"}
        if unknown:
            raise PayloadError(f"moves[{index}] unknown keys: {sorted(unknown)}")
        source_rel = str(spec.get("from") or "")
        target_rel = str(spec.get("to") or "")
        source = safe_repo_path(source_rel, prefix="wiki/")
        target = safe_repo_path(target_rel, prefix="wiki/")
        if source in {HOT_FILE.resolve(), LOG_FILE.resolve()} or target in {
            HOT_FILE.resolve(), LOG_FILE.resolve()
        }:
            raise PayloadError(f"moves[{index}] cannot rename writer-owned log/hot files")
        if source == target:
            raise PayloadError(f"moves[{index}] source and target are identical")
        if not source.is_file():
            raise ConflictError(f"move source missing: {source_rel}")
        if target.exists():
            raise ConflictError(f"move target exists: {target_rel}")
        expected = str(spec.get("expected_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise PayloadError(f"moves[{index}] requires lowercase expected_sha256")
        content = source.read_text(encoding="utf-8")
        actual = sha256_text(content)
        if actual != expected:
            raise ConflictError(
                f"move conflict: {source_rel} is {actual}, expected {expected}"
            )
        validate_page_content(target_rel, content)
        writes.append((target, content))
        deletes.append((source, expected))
    return writes, deletes


def deep_merge(base: dict, patch: dict) -> dict:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def manifest_write(spec: object) -> tuple[Path, str] | None:
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise PayloadError("manifest_update must be an object")
    unknown = set(spec) - {"path", "expected_sha256", "merge"}
    if unknown:
        raise PayloadError(f"manifest_update unknown keys: {sorted(unknown)}")
    rel = str(spec.get("path") or ".raw/.manifest.json")
    if rel != ".raw/.manifest.json":
        raise PayloadError("manifest_update.path must be .raw/.manifest.json")
    path = safe_repo_path(rel)
    expected = str(spec.get("expected_sha256") or "")
    if not path.is_file():
        raise ConflictError(f"manifest target missing: {rel}")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise PayloadError("manifest_update requires lowercase expected_sha256")
    current_text = path.read_text(encoding="utf-8")
    actual = sha256_text(current_text)
    if actual != expected:
        raise ConflictError(f"manifest conflict: {actual}, expected {expected}")
    merge = spec.get("merge")
    if not isinstance(merge, dict):
        raise PayloadError("manifest_update.merge must be an object")
    try:
        current = json.loads(current_text)
    except json.JSONDecodeError as exc:
        raise PayloadError(f"manifest is invalid JSON: {exc}") from exc
    if not isinstance(current, dict):
        raise PayloadError("manifest root must be an object")
    return path, json.dumps(deep_merge(current, merge), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def ensure_unique_writes(
    writes: list[tuple[Path, str]], deletes: list[tuple[Path, str]] | None = None
) -> None:
    seen: set[Path] = set()
    for path, _ in writes:
        if path in seen:
            raise PayloadError(f"payload writes {path.relative_to(REPO_ROOT)} more than once")
        seen.add(path)
    for path, _ in deletes or []:
        if path in seen:
            raise PayloadError(f"payload mutates {path.relative_to(REPO_ROOT)} more than once")
        seen.add(path)


def write_journal(
    writes: list[tuple[Path, str]], deletes: list[tuple[Path, str]] | None = None
) -> None:
    deletes = deletes or []
    payload = {
        "version": 2 if deletes else 1,
        "entries": [
            {
                **({"op": "write"} if deletes else {}),
                "path": str(path.relative_to(REPO_ROOT)),
                "sha256": sha256_text(content),
                "content": content,
            }
            for path, content in writes
        ] + [
            {
                "op": "delete",
                "path": str(path.relative_to(REPO_ROOT)),
                "sha256": expected,
            }
            for path, expected in deletes
        ],
    }
    atomic_write(JOURNAL_FILE, json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def recover_journal() -> int:
    if not JOURNAL_FILE.exists():
        return 0
    try:
        journal = json.loads(JOURNAL_FILE.read_text(encoding="utf-8"))
        if not isinstance(journal, dict):
            raise PayloadError("journal root must be an object")
        entries = journal.get("entries")
        version = journal.get("version")
        if version not in {1, 2} or not isinstance(entries, list):
            raise PayloadError("unsupported or corrupt journal")
        recovered = 0
        for entry in entries:
            if not isinstance(entry, dict):
                raise PayloadError("corrupt journal entry")
            rel = str(entry.get("path") or "")
            if not (rel.startswith("wiki/") or rel == ".raw/.manifest.json"):
                raise PayloadError(f"journal path is outside mutation scope: {rel!r}")
            path = safe_repo_path(rel, prefix="wiki/") if rel.startswith("wiki/") else safe_repo_path(rel)
            op = "write" if version == 1 else entry.get("op")
            if op not in {"write", "delete"}:
                raise PayloadError("corrupt journal operation")
            content = entry.get("content")
            expected = entry.get("sha256")
            if op == "write":
                if not isinstance(content, str) or expected != sha256_text(content):
                    raise PayloadError("journal content checksum mismatch")
                actual = sha256_text(path.read_text(encoding="utf-8")) if path.is_file() else None
                if actual != expected:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(path, content)
                    recovered += 1
            else:
                if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise PayloadError("journal delete checksum is invalid")
                if path.exists():
                    actual = sha256_text(path.read_text(encoding="utf-8")) if path.is_file() else None
                    if actual != expected:
                        raise PayloadError(f"journal delete conflict for {rel}")
                    path.unlink()
                    recovered += 1
        JOURNAL_FILE.unlink()
        return recovered
    except (OSError, json.JSONDecodeError, PayloadError) as exc:
        raise OSError(f"cannot recover transaction journal: {exc}") from exc


def apply_plan_close(spec: dict, today: str) -> tuple[Path, str]:
    """Strictly close a plan page: pending -> executed + provenance.

    Any precondition miss raises CapViolation (exit 2, nothing written):
    re-closing an executed plan is a logic error (wrong plan_file or a
    double reap), not something to paper over."""
    if not isinstance(spec, dict):
        raise CapViolation("plan_close must be an object {file, result_link, exec_session}")
    rel = str(spec.get("file") or "")
    result_link = str(spec.get("result_link") or "").strip()
    exec_session = spec.get("exec_session") or None
    if not result_link:
        raise CapViolation("plan_close.result_link is required, e.g. '[[Page Title]]'")

    path = (REPO_ROOT / rel).resolve()
    plans_dir = (REPO_ROOT / "wiki" / "plans").resolve()
    if plans_dir not in path.parents:
        raise CapViolation(f"plan_close.file must live in wiki/plans/ (got {rel!r})")
    if not path.is_file():
        raise CapViolation(f"plan_close.file not found: {rel}")

    expected_sha256 = spec.get("expected_sha256")
    if expected_sha256 is not None:
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_sha256)
        ):
            raise CapViolation("plan_close.expected_sha256 must be a lowercase SHA-256")
        actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_sha256 != expected_sha256:
            raise ConflictError(
                f"plan_close conflict for {rel}: expected {expected_sha256}, got {actual_sha256}"
            )

    try:
        new_text = render_plan_close(
            path.read_text(encoding="utf-8"),
            today=today,
            result_link=result_link,
            exec_session=exec_session,
            label=rel,
        )
    except PlanCloseError as exc:
        raise CapViolation(str(exc)) from exc
    return path, new_text


def main(argv: list[str]) -> int:
    global OUTPUT_JSON, REQUEST_ID, TRANSACTION_ID
    TRANSACTION_ID = str(uuid.uuid4())
    if "--output" in argv:
        try:
            output_mode = argv[argv.index("--output") + 1]
        except IndexError:
            return fail(3, "--output requires text|json")
        if output_mode not in {"text", "json"}:
            return fail(3, "--output must be text or json")
        OUTPUT_JSON = output_mode == "json"
    if "--sha256" in argv:
        try:
            rel = argv[argv.index("--sha256") + 1]
            path = safe_repo_path(rel)
            if not path.is_file():
                return fail(3, f"cannot hash missing file: {rel}")
            digest = sha256_text(path.read_text(encoding="utf-8"))
            if OUTPUT_JSON:
                result_json("ok", extra={"path": rel, "sha256": digest})
            else:
                print(digest)
            return 0
        except (IndexError, OSError, PayloadError) as exc:
            return fail(3, f"cannot hash path: {exc}")
    dry = "--dry-run" in argv
    recover_only = "--recover" in argv
    payload: dict = {}
    if not recover_only:
        if "--file" in argv:
            try:
                raw = Path(argv[argv.index("--file") + 1]).read_text(encoding="utf-8")
            except (IndexError, OSError) as e:
                return fail(3, f"cannot read --file: {e}")
        else:
            raw = sys.stdin.read()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            return fail(3, f"payload is not valid JSON: {e}")
        if not isinstance(parsed, dict):
            return fail(3, "payload must be a JSON object")
        payload = parsed
        schema_version = payload.get("schema_version", 1)
        if schema_version != 1:
            return fail(3, f"unsupported schema_version {schema_version!r}; expected 1")
        request_id = payload.get("request_id")
        if request_id is not None:
            if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id):
                return fail(3, "request_id must match [A-Za-z0-9._:-]{1,128}")
            REQUEST_ID = request_id
            TRANSACTION_ID = request_id
        unknown = payload.keys() - KNOWN_KEYS
        if "index_line" in unknown:
            print(
                "vault-write: WARN index_line is not supported — index.md is a curated "
                "map; folder listings autogenerate via reindex.py --folder-indexes",
                file=sys.stderr,
            )
            unknown = unknown - {"index_line"}
        if unknown:
            return fail(3, f"unknown payload keys: {sorted(unknown)}")
        if not payload.keys() & MUTATION_KEYS:
            return fail(3, "payload has no actionable keys")
        if "actor" in payload and not isinstance(payload["actor"], str):
            return fail(3, "actor must be a string")
        if "session" in payload and not isinstance(payload["session"], str):
            return fail(3, "session must be a string")

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = LOCK_FILE.open("w")
    deadline = time.time() + 5
    while True:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() > deadline:
                return fail(1, "could not acquire vault-write lock within 5s")
            time.sleep(0.2)

    try:
        recovered = recover_journal()
        if recovered:
            print(f"vault-write: RECOVERED {recovered} file(s) by roll-forward", file=sys.stderr)
            emit_event(
                "vault-recover",
                actor="recovery",
                counts={"writes": recovered},
                root=REPO_ROOT,
            )
        if recover_only:
            if OUTPUT_JSON:
                result_json("ok", extra={"recovered_writes": recovered})
            else:
                print(f"vault-write: OK recovery ({recovered} file(s))")
            return 0

        today = time.strftime("%Y-%m-%d")
        writes = page_writes(payload.get("pages"))
        move_writes, deletes = page_moves(payload.get("moves"))
        writes.extend(move_writes)
        warnings: list[str] = []

        manifest = manifest_write(payload.get("manifest_update"))
        if manifest:
            writes.append(manifest)

        if payload.keys() & {
            "hot_bullet",
            "hot_recent_remove_addresses",
            "hot_narrative",
            "hot_threads",
        }:
            hot_text = HOT_FILE.read_text(encoding="utf-8")
            new_hot, w = apply_hot(payload, hot_text, today)
            writes.append((HOT_FILE, new_hot))
            warnings.extend(w)

        if payload.get("log_entry"):
            log_text = LOG_FILE.read_text(encoding="utf-8")
            writes.append((LOG_FILE, apply_log(payload["log_entry"], log_text, today)))

        if payload.get("plan_close"):
            writes.append(apply_plan_close(payload["plan_close"], today))

        ensure_unique_writes(writes, deletes)
        if not writes and not deletes:
            raise PayloadError("payload produced no writes")
        if not OUTPUT_JSON:
            for w in warnings:
                print(f"vault-write: WARN {w}", file=sys.stderr)
        if dry:
            paths = [str(path.relative_to(REPO_ROOT)) for path, _ in writes]
            paths.extend(str(path.relative_to(REPO_ROOT)) for path, _ in deletes)
            if OUTPUT_JSON:
                result_json("dry-run", written_paths=paths, warnings=warnings)
            else:
                for path in paths:
                    print(f"vault-write: DRY would write {path}")
            return 0
        write_journal(writes, deletes)
        for path, text in writes:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, text)
        for path, expected in deletes:
            actual = sha256_text(path.read_text(encoding="utf-8")) if path.is_file() else None
            if actual != expected:
                raise ConflictError(
                    f"delete conflict during move: {path.relative_to(REPO_ROOT)} is {actual}, expected {expected}"
                )
            path.unlink()
        JOURNAL_FILE.unlink()
        page_specs = payload.get("pages") or []
        emit_event(
            "vault-write",
            actor=payload.get("actor") or "unknown",
            session=payload.get("session"),
            paths=[str(path.relative_to(REPO_ROOT)) for path, _ in writes]
            + [str(path.relative_to(REPO_ROOT)) for path, _ in deletes],
            counts={
                "writes": len(writes) + len(deletes),
                "page_creates": sum(1 for spec in page_specs if spec.get("op") == "create"),
                "page_updates": sum(1 for spec in page_specs if spec.get("op") == "update"),
                "page_moves": len(payload.get("moves") or []),
                "manifest_updates": int(bool(payload.get("manifest_update"))),
                "hot_updates": int(
                    bool(
                        payload.keys()
                        & {
                            "hot_bullet",
                            "hot_recent_remove_addresses",
                            "hot_narrative",
                            "hot_threads",
                        }
                    )
                ),
                "log_updates": int(bool(payload.get("log_entry"))),
                "plan_closes": int(bool(payload.get("plan_close"))),
                "recovered_writes": recovered,
            },
            root=REPO_ROOT,
        )
        written_paths = [str(path.relative_to(REPO_ROOT)) for path, _ in writes]
        written_paths.extend(str(path.relative_to(REPO_ROOT)) for path, _ in deletes)
        if OUTPUT_JSON:
            result_json("ok", written_paths=written_paths, warnings=warnings)
        else:
            print("vault-write: OK " + ", ".join(written_paths))
        return 0
    except CapViolation as e:
        return fail(2, f"CAP VIOLATION — nothing written. {e}")
    except ConflictError as e:
        return fail(4, f"CONFLICT — nothing written. {e}")
    except PayloadError as e:
        return fail(3, f"bad payload — nothing written. {e}")
    except (OSError, ValueError) as e:
        return fail(1, f"io/recovery error — transaction may require --recover. {e}")
    finally:
        lock_fh.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
