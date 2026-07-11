#!/usr/bin/env python3
"""Schemas for crossing the untrusted web-fetch / private-vault boundary."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from urllib.parse import urlparse


SCHEMA_VERSION = 1
SOURCE_CLASSES = {"official", "internal", "third-party"}
MAX_SOURCES = 50
MAX_SOURCE_CHARS = 500_000
MAX_TOTAL_CHARS = 5_000_000


class ResearchContractError(ValueError):
    pass


def required_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchContractError(f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > limit:
        raise ResearchContractError(f"{field} exceeds {limit} characters")
    if "\x00" in value:
        raise ResearchContractError(f"{field} contains NUL")
    return value


def validate_artifact(
    raw: Any, *, expected_run_id: str | None = None, expected_topic: str | None = None
) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise ResearchContractError(f"artifact schema_version must be {SCHEMA_VERSION}")
    run_id = required_text(raw.get("run_id"), "run_id", 100)
    topic = required_text(raw.get("topic"), "topic", 2000)
    if expected_run_id is not None and run_id != expected_run_id:
        raise ResearchContractError("run_id does not match active research run")
    if expected_topic is not None and topic != expected_topic:
        raise ResearchContractError("topic does not match active research run")
    fetched_at = required_text(raw.get("fetched_at"), "fetched_at", 100)
    try:
        datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchContractError("fetched_at must be ISO-8601") from exc

    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list) or not 1 <= len(raw_sources) <= MAX_SOURCES:
        raise ResearchContractError(f"sources must contain 1-{MAX_SOURCES} items")
    sources: list[dict[str, str]] = []
    total = 0
    for index, source in enumerate(raw_sources):
        field = f"sources[{index}]"
        if not isinstance(source, dict):
            raise ResearchContractError(f"{field} must be an object")
        url = required_text(source.get("url"), f"{field}.url", 4000)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ResearchContractError(f"{field}.url must be HTTP(S)")
        content = required_text(source.get("clean_markdown"), f"{field}.clean_markdown", MAX_SOURCE_CHARS)
        total += len(content)
        digest = required_text(source.get("content_sha256"), f"{field}.content_sha256", 64)
        actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest != actual:
            raise ResearchContractError(f"{field}.content_sha256 mismatch")
        source_class = required_text(source.get("source_class"), f"{field}.source_class", 40)
        if source_class not in SOURCE_CLASSES:
            raise ResearchContractError(f"{field}.source_class must be one of {sorted(SOURCE_CLASSES)}")
        sources.append(
            {
                "url": url,
                "title": required_text(source.get("title"), f"{field}.title", 1000),
                "content_sha256": digest,
                "source_class": source_class,
                "trust": "untrusted",
                "clean_markdown": content,
            }
        )
    if total > MAX_TOTAL_CHARS:
        raise ResearchContractError(f"source content exceeds {MAX_TOTAL_CHARS} total characters")
    errors = raw.get("fetch_errors", [])
    if not isinstance(errors, list) or len(errors) > 100:
        raise ResearchContractError("fetch_errors must be a list with at most 100 items")
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "topic": topic,
        "fetched_at": fetched_at,
        "sources": sources,
        "fetch_errors": [required_text(item, "fetch_errors[]", 2000) for item in errors],
    }


def load_artifact(path: str, **expected: str | None) -> dict[str, Any]:
    try:
        raw = json.loads(open(path, encoding="utf-8").read())
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchContractError(f"cannot read artifact JSON: {exc}") from exc
    return validate_artifact(raw, **expected)
