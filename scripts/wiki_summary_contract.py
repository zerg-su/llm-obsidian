#!/usr/bin/env python3
"""Versioned contract for dispatch task -> wiki summary handoffs."""

from __future__ import annotations

from typing import Any


SCHEMA_VERSION = 1
TYPES = {"session", "decision", "runbook", "incident", "service-update", "repo-touch"}


class WikiSummaryError(ValueError):
    pass


def clean_text(value: Any, field: str, *, required: bool = True, limit: int = 200_000) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise WikiSummaryError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise WikiSummaryError(f"{field} must not be empty")
    if value.startswith("<"):
        raise WikiSummaryError(f"{field} is an unresolved placeholder: {value!r}")
    if len(value) > limit or "\x00" in value:
        raise WikiSummaryError(f"{field} is too large or contains NUL")
    return value


def validate_summary(
    raw: Any, *, allow_missing_session: bool = True, require_schema: bool = False
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise WikiSummaryError("summary must be an object")
    if require_schema and "schema_version" not in raw:
        raise WikiSummaryError("schema_version is required for canonical JSON")
    if raw.get("schema_version", 1) != SCHEMA_VERSION:
        raise WikiSummaryError(f"schema_version must be {SCHEMA_VERSION}")
    typ = clean_text(raw.get("type"), "type", limit=40)
    if typ not in TYPES:
        raise WikiSummaryError(f"type {typ!r} is not one of {sorted(TYPES)}")
    title = clean_text(raw.get("title"), "title", limit=500)
    session = clean_text(raw.get("session"), "session", required=False, limit=200)
    if not session and not allow_missing_session:
        raise WikiSummaryError("session must not be empty")
    return {
        "schema_version": SCHEMA_VERSION,
        "type": typ,
        "title": title,
        "session": session or None,
        "body": clean_text(raw.get("body"), "body"),
    }


def render_markdown(summary: dict[str, Any]) -> str:
    session = summary.get("session") or ""
    return (
        "## Wiki Summary\n\n"
        f"type: {summary['type']}\n"
        f"title: {summary['title']}\n"
        f"session: {session}\n\n"
        f"{summary['body'].rstrip()}\n"
    )
