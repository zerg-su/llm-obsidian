#!/usr/bin/env python3
"""Pure rendering and cap enforcement for writer-owned hot/log surfaces."""

from __future__ import annotations

import re

from vault_schema import DATE_RX, LOG_ENTRY_RX
from vault_write_contract import CapViolation, PayloadError


# Mirrored in scripts/validate-vault.py — keep in sync.
HOT_TOTAL_WORDS = 800
RC_MAX_BULLETS = 15
RC_BULLET_CHARS = 160
THREADS_MAX = 8
NARRATIVE_WORDS = 120

RC_HEADING = "## Recent Changes"
THREADS_HEADING = "## Active Threads"
NARRATIVE_HEADING = "## Last Updated"
HOT_LINK_RX = re.compile(r"\[\[[^\]\r\n]+\]\]")
HOT_ADDRESS_TOKEN_RX = re.compile(r"(?<![A-Za-z0-9])c-\d{6}(?!\d)")


def one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def safe_hot_bullet(value: object) -> tuple[str, bool]:
    """Validate one recent-change bullet and truncate only its prose."""

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
        raise PayloadError(
            "hot_bullet must contain exactly one c-NNNNNN address after its wikilink"
        )
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
    if len(rendered) > RC_BULLET_CHARS:
        raise PayloadError("hot_bullet could not be safely truncated")
    return rendered, True


def section_bounds(lines: list[str], heading: str) -> tuple[int, int] | None:
    """Return body indexes for one second-level Markdown section."""

    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return start + 1, end


def bullets_of(lines: list[str]) -> list[str]:
    return [line for line in lines if line.lstrip().startswith("- ")]


def replace_section(
    lines: list[str], heading: str, new_body: list[str]
) -> list[str]:
    bounds = section_bounds(lines, heading)
    if bounds is None:
        raise ValueError(f"section not found: {heading}")
    start, end = bounds
    return lines[:start] + [""] + new_body + [""] + lines[end:]


def set_frontmatter_updated(text: str, today: str) -> str:
    return re.sub(r"^updated: .*$", f"updated: {today}", text, count=1, flags=re.M)


def apply_hot(payload: dict, hot_text: str, today: str) -> tuple[str, list[str]]:
    """Render hot.md and return its stable warning list."""

    warnings: list[str] = []
    lines = hot_text.split("\n")

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
        existing = bullets_of(lines[bounds[0] : bounds[1]])
        kept = [
            item
            for item in existing
            if not any(
                address in HOT_ADDRESS_TOKEN_RX.findall(item) for address in removals
            )
        ]
        removed = len(existing) - len(kept)
        if removals and removed == 0:
            warnings.append(
                "hot_recent_remove_addresses matched no Recent Changes bullets"
            )
        if bullet:
            rendered, truncated = safe_hot_bullet(bullet)
            if truncated:
                warnings.append(
                    f"hot_bullet essence truncated to {RC_BULLET_CHARS} chars"
                )
            kept.insert(0, rendered)
        if len(kept) > RC_MAX_BULLETS:
            evicted = len(kept) - RC_MAX_BULLETS
            kept = kept[:RC_MAX_BULLETS]
            warnings.append(f"Recent Changes: evicted {evicted} oldest bullet(s)")
        lines = replace_section(lines, RC_HEADING, kept)

    narrative = payload.get("hot_narrative")
    if narrative:
        word_count = len(narrative.split())
        if word_count > NARRATIVE_WORDS:
            raise CapViolation(
                f"hot_narrative is {word_count} words (cap {NARRATIVE_WORDS}) — shorten it"
            )
        lines = replace_section(
            lines, NARRATIVE_HEADING, narrative.strip().split("\n")
        )

    threads = payload.get("hot_threads") or {}
    if threads:
        bounds = section_bounds(lines, THREADS_HEADING)
        if bounds is None:
            raise CapViolation(f"hot.md has no '{THREADS_HEADING}' section")
        current = bullets_of(lines[bounds[0] : bounds[1]])
        for pattern in threads.get("resolve", []):
            before = len(current)
            current = [thread for thread in current if pattern not in thread]
            if len(current) == before:
                warnings.append(
                    f"hot_threads.resolve: no thread matched {pattern!r}"
                )
        for addition in threads.get("add", []):
            rendered = one_line(addition)
            if not rendered.startswith("- "):
                rendered = "- " + rendered
            current.insert(0, rendered)
        if len(current) > THREADS_MAX:
            evicted = len(current) - THREADS_MAX
            current = current[:THREADS_MAX]
            warnings.append(
                f"Active Threads: evicted {evicted} oldest cache entry(s)"
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
    match = re.search(r"^## \[", log_text, flags=re.M)
    if match:
        new_text = log_text[: match.start()] + entry + "\n\n" + log_text[match.start() :]
    else:
        new_text = log_text.rstrip("\n") + "\n\n" + entry + "\n"
    return set_frontmatter_updated(new_text, today)
