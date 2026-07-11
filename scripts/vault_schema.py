#!/usr/bin/env python3
"""Strict, dependency-free schema helpers for the Obsidian vault.

The vault intentionally supports a small YAML subset so every runtime can
validate it without PyYAML.  The parser accepts top-level scalar fields,
flow lists, block lists, and lists of small dictionaries (the ``sessions``
shape).  Anything outside that contract fails closed instead of becoming an
invisible field in a retrieval index.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Any, Iterable


ADDRESS_RX = re.compile(r"^c-(\d{6})$")
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LOG_ENTRY_RX = re.compile(
    r"^## \[(\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?)]\s+([^|\n]+?)\s*\|\s*(\S.*)\s*$",
    re.MULTILINE,
)
KEY_RX = re.compile(r"^([A-Za-z_][\w-]*):(?:[ \t]*(.*))$")
NESTED_KEY_RX = re.compile(r"^ {4}([A-Za-z_][\w-]*):(?:[ \t]*(.*))$")
LIST_DICT_RX = re.compile(r"^  - ([A-Za-z_][\w-]*):(?:[ \t]*(.*))$")
LIST_ITEM_RX = re.compile(r"^  -(?:[ \t]+(.*))?$")
WIKILINK_RX = re.compile(r"(?<!!)\[\[([^\]\n]+)\]\]|!\[\[([^\]\n]+)\]\]")

REQUIRED_KEYS = ("type", "status", "created", "updated", "tags", "sessions")
ADDRESS_CUTOFF = date(2026, 4, 23)
ADDRESS_EXEMPT_TYPES = {"meta", "overview", "fold", "daily", "plan", "session"}
CONTENT_EXTENSIONS = {".md", ".canvas", ".base"}


class FrontmatterError(ValueError):
    """A frontmatter block is outside the supported strict YAML subset."""


@dataclass(frozen=True)
class SchemaIssue:
    level: str
    code: str
    message: str


def split_frontmatter(text: str) -> str | None:
    """Return the first YAML block, or ``None`` when it is absent/unclosed."""
    if not text.startswith("---\n"):
        return None
    match = re.search(r"^---[ \t]*$", text[4:], flags=re.M)
    if match is None:
        return None
    return text[4 : 4 + match.start()].rstrip("\n")


def split_document(text: str) -> tuple[str, str] | None:
    """Return ``(frontmatter, body)`` for a complete Markdown document."""
    if not text.startswith("---\n"):
        return None
    match = re.search(r"^---[ \t]*\n?", text[4:], flags=re.M)
    if match is None:
        return None
    return text[4 : 4 + match.start()].rstrip("\n"), text[4 + match.end() :]


def _scalar(value: str, *, line: int) -> str:
    value = value.strip()
    if not value:
        return ""
    if value[0] in "\"'":
        if len(value) < 2 or value[-1] != value[0]:
            raise FrontmatterError(f"line {line}: unterminated quoted scalar")
        return value[1:-1]
    if value[-1:] == ":" or re.search(r":\s", value):
        raise FrontmatterError(
            f"line {line}: colon in an unquoted scalar; quote it or split joined fields"
        )
    return value


def _flow_list(value: str, *, line: int) -> list[str]:
    if not value.endswith("]"):
        raise FrontmatterError(f"line {line}: unterminated flow list")
    inner = value[1:-1].strip()
    if not inner:
        return []
    try:
        row = next(csv.reader(StringIO(inner), skipinitialspace=True))
    except (csv.Error, StopIteration) as exc:
        raise FrontmatterError(f"line {line}: invalid flow list: {exc}") from exc
    return [_scalar(item, line=line) for item in row]


def parse_frontmatter(block: str) -> dict[str, Any]:
    """Parse and strictly validate the repository's supported YAML subset."""
    out: dict[str, Any] = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line_no = i + 1
        if "\t" in raw:
            raise FrontmatterError(f"line {line_no}: tabs are not allowed")
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        if raw.startswith(" "):
            raise FrontmatterError(f"line {line_no}: unexpected indentation")
        match = KEY_RX.match(raw)
        if match is None:
            raise FrontmatterError(f"line {line_no}: expected 'key: value'")
        key, value = match.group(1), (match.group(2) or "").strip()
        if key in out:
            raise FrontmatterError(f"line {line_no}: duplicate key {key!r}")

        if value:
            if value.startswith("["):
                out[key] = _flow_list(value, line=line_no)
            else:
                out[key] = _scalar(value, line=line_no)
            i += 1
            if i < len(lines) and lines[i].startswith(" "):
                raise FrontmatterError(
                    f"line {i + 1}: indented continuation is not supported for scalar {key!r}"
                )
            continue

        items: list[Any] = []
        i += 1
        current: dict[str, str] | None = None
        while i < len(lines):
            nested = lines[i]
            nested_no = i + 1
            if "\t" in nested:
                raise FrontmatterError(f"line {nested_no}: tabs are not allowed")
            if not nested.strip():
                i += 1
                continue
            if not nested.startswith(" "):
                break
            dict_start = LIST_DICT_RX.match(nested)
            scalar_item = LIST_ITEM_RX.match(nested)
            continuation = NESTED_KEY_RX.match(nested)
            if dict_start:
                nested_key = dict_start.group(1)
                current = {nested_key: _scalar(dict_start.group(2) or "", line=nested_no)}
                items.append(current)
            elif continuation and current is not None:
                nested_key = continuation.group(1)
                if nested_key in current:
                    raise FrontmatterError(
                        f"line {nested_no}: duplicate nested key {nested_key!r}"
                    )
                current[nested_key] = _scalar(
                    continuation.group(2) or "", line=nested_no
                )
            elif scalar_item:
                item = scalar_item.group(1)
                if item is None or not item.strip():
                    raise FrontmatterError(f"line {nested_no}: empty block-list item")
                items.append(_scalar(item, line=nested_no))
                current = None
            else:
                raise FrontmatterError(
                    f"line {nested_no}: expected two-space list item or four-space nested key"
                )
            i += 1
        out[key] = items
    return out


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def extract_sessions(frontmatter: dict[str, Any]) -> list[str]:
    sessions: list[str] = []
    for item in _as_list(frontmatter.get("sessions")):
        sid = item.get("id") if isinstance(item, dict) else item
        if isinstance(sid, str) and sid:
            sessions.append(sid)
    return sessions


def extract_tags(frontmatter: dict[str, Any]) -> list[str]:
    return [item for item in _as_list(frontmatter.get("tags")) if isinstance(item, str)]


def extract_related(frontmatter: dict[str, Any]) -> list[str]:
    return [item for item in _as_list(frontmatter.get("related")) if isinstance(item, str)]


def _strip_code(text: str) -> str:
    text = re.sub(r"^```.*?^```[ \t]*$", "", text, flags=re.M | re.S)
    text = re.sub(r"^~~~.*?^~~~[ \t]*$", "", text, flags=re.M | re.S)
    return re.sub(r"`[^`\n]*`", "", text)


def iter_wikilinks(text: str) -> Iterable[str]:
    for match in WIKILINK_RX.finditer(_strip_code(text)):
        raw = (match.group(1) or match.group(2) or "").replace(r"\|", "|")
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            yield target


def _normal_target(value: str) -> str:
    value = value.strip().replace("\\", "/")
    if value.startswith("wiki/"):
        value = value[5:]
    suffix = Path(value).suffix.lower()
    if suffix in CONTENT_EXTENSIONS:
        value = value[: -len(suffix)]
    return value.strip("/").casefold()


def _catalog(wiki: Path) -> tuple[set[str], dict[str, list[str]], set[str]]:
    exact: set[str] = set()
    by_stem: dict[str, list[str]] = {}
    aliases: set[str] = set()
    for path in sorted(wiki.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in CONTENT_EXTENSIONS:
            continue
        rel = path.relative_to(wiki)
        without_suffix = str(rel.with_suffix(""))
        exact.add(_normal_target(without_suffix))
        by_stem.setdefault(rel.stem.casefold(), []).append(str(rel))
        if path.suffix.lower() == ".md":
            block = split_frontmatter(path.read_text(encoding="utf-8"))
            if block is None:
                continue
            try:
                fm = parse_frontmatter(block)
            except FrontmatterError:
                continue
            for alias in _as_list(fm.get("aliases")):
                if isinstance(alias, str) and alias.strip():
                    aliases.add(alias.strip().casefold())
    return exact, by_stem, aliases


def validate_schema(repo_root: Path) -> list[SchemaIssue]:
    """Validate frontmatter, addresses, pathless links, and derived address state."""
    wiki = repo_root / "wiki"
    meta = repo_root / ".vault-meta"
    issues: list[SchemaIssue] = []
    pages: list[tuple[Path, dict[str, Any], str]] = []

    for path in sorted(wiki.rglob("*.md")):
        rel = path.relative_to(repo_root)
        text = path.read_text(encoding="utf-8")
        block = split_frontmatter(text)
        if block is None:
            issues.append(SchemaIssue("fail", "frontmatter", f"{rel}: missing or unclosed frontmatter"))
            continue
        try:
            fm = parse_frontmatter(block)
        except FrontmatterError as exc:
            issues.append(SchemaIssue("fail", "frontmatter", f"{rel}: {exc}"))
            continue
        pages.append((path, fm, text))
        missing = [key for key in REQUIRED_KEYS if key not in fm]
        if missing:
            issues.append(
                SchemaIssue("fail", "frontmatter", f"{rel}: missing {', '.join(missing)}")
            )
        for key in ("created", "updated"):
            raw = str(fm.get(key) or "")
            if raw and not DATE_RX.fullmatch(raw):
                issues.append(SchemaIssue("fail", "frontmatter", f"{rel}: {key} must be YYYY-MM-DD"))
        tags = fm.get("tags")
        if "tags" in fm and (not isinstance(tags, list) or not extract_tags(fm)):
            issues.append(SchemaIssue("fail", "frontmatter", f"{rel}: tags must be a non-empty list"))
        sessions = fm.get("sessions")
        if "sessions" in fm and not isinstance(sessions, list):
            issues.append(SchemaIssue("fail", "frontmatter", f"{rel}: sessions must be a list"))
        elif sessions == []:
            issues.append(SchemaIssue("warn", "provenance", f"{rel}: sessions is legacy-unknown []"))
        if str(fm.get("type") or "") == "source":
            if str(fm.get("source_class") or "") not in {"official", "internal", "third-party"}:
                issues.append(SchemaIssue("fail", "provenance", f"{rel}: invalid or missing source_class"))
            if not DATE_RX.fullmatch(str(fm.get("verified_at") or "")):
                issues.append(SchemaIssue("fail", "provenance", f"{rel}: verified_at must be YYYY-MM-DD"))
            if not re.fullmatch(r"[0-9a-f]{64}", str(fm.get("content_sha256") or "")):
                issues.append(SchemaIssue("fail", "provenance", f"{rel}: content_sha256 must be lowercase SHA-256"))

    addresses: dict[str, list[str]] = {}
    expected_map: list[tuple[str, str]] = []
    highest = 0
    for path, fm, _ in pages:
        rel_repo = str(path.relative_to(repo_root))
        raw = fm.get("address")
        ptype = str(fm.get("type") or "")
        created = str(fm.get("created") or "")
        requires_address = False
        if ptype not in ADDRESS_EXEMPT_TYPES and DATE_RX.fullmatch(created):
            requires_address = date.fromisoformat(created) >= ADDRESS_CUTOFF
        if requires_address and not raw:
            issues.append(SchemaIssue("fail", "address", f"{rel_repo}: address required"))
        if raw is None:
            continue
        match = ADDRESS_RX.fullmatch(str(raw))
        if match is None or int(match.group(1)) == 0:
            issues.append(SchemaIssue("fail", "address", f"{rel_repo}: invalid address {raw!r}"))
            continue
        number = int(match.group(1))
        highest = max(highest, number)
        address = str(raw)
        addresses.setdefault(address, []).append(rel_repo)
        expected_map.append((address, rel_repo))
    for address, paths in sorted(addresses.items()):
        if len(paths) > 1:
            issues.append(SchemaIssue("fail", "address", f"{address} is duplicated: {', '.join(paths)}"))

    counter = meta / "address-counter.txt"
    if not counter.is_file():
        issues.append(SchemaIssue("fail", "address", ".vault-meta/address-counter.txt missing"))
    else:
        raw_counter = counter.read_text(encoding="utf-8").strip()
        minimum_counter = highest + 1
        if not raw_counter.isdigit() or int(raw_counter) < minimum_counter:
            issues.append(
                SchemaIssue(
                    "fail",
                    "address",
                    f"address counter is {raw_counter!r}; expected at least {minimum_counter}",
                )
            )
        elif int(raw_counter) > minimum_counter:
            issues.append(
                SchemaIssue(
                    "warn",
                    "address",
                    f"address counter is {raw_counter!r}; next observed address is "
                    f"{minimum_counter} (reserved/deleted address gaps are allowed)",
                )
            )

    address_map = meta / "address-map.tsv"
    expected_text = "".join(f"{addr}\t{path}\n" for addr, path in sorted(expected_map))
    if not address_map.is_file() or address_map.read_text(encoding="utf-8") != expected_text:
        issues.append(SchemaIssue("fail", "address", ".vault-meta/address-map.tsv is stale; run reindex.py"))

    exact, by_stem, aliases = _catalog(wiki)
    for stem, paths in sorted(by_stem.items()):
        md_paths = [path for path in paths if path.lower().endswith(".md")]
        if stem != "_index" and len(md_paths) > 1:
            issues.append(
                SchemaIssue("fail", "filename", f"non-unique Markdown filename {stem!r}: {', '.join(md_paths)}")
            )
    for path, _, text in pages:
        rel = path.relative_to(repo_root)
        for target in iter_wikilinks(text):
            normalized = _normal_target(target)
            stem = Path(normalized).name
            if normalized in exact or stem in by_stem or normalized in aliases:
                continue
            issues.append(SchemaIssue("fail", "wikilink", f"{rel}: unresolved [[{target}]]"))
    return issues
