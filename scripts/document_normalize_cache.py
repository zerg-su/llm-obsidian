"""Cache manifest restoration and typed normalization status exits."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from document_normalize_config import (
    EXIT_CONVERSION_FAILURE,
    EXIT_LOW_QUALITY,
    EXIT_NEEDS_SEMANTIC_CLEANUP,
    EXIT_NEEDS_USER_ACTION,
    EXIT_OK,
    EXIT_RUNTIME_UNAVAILABLE,
)


def cached_payload(
    target: Path, source_hash: str, profile_hash: str
) -> dict[str, Any] | None:
    manifest = target / "manifest.json"
    markdown = target / "document.md"
    if not manifest.is_file() or not markdown.is_file():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("source", {}).get("sha256") != source_hash:
        return None
    if payload.get("processor", {}).get("profile_sha256") != profile_hash:
        return None
    payload["cached"] = True
    stored_status = str(payload.get("status", "low_quality"))
    payload["status"] = "cached" if stored_status == "ok" else stored_status
    payload["artifacts"] = {
        "root": str(target),
        "markdown": str(markdown),
        "raw_markdown": (
            str(target / "document.raw.md")
            if (target / "document.raw.md").is_file()
            else str(markdown)
        ),
        "docling_json": (
            str(target / "document.docling.json")
            if (target / "document.docling.json").is_file()
            else None
        ),
        "adapter_metadata": (
            str(target / "document.adapter.json")
            if (target / "document.adapter.json").is_file()
            else None
        ),
        "repair_bundle": (
            str(target / "repair-bundle.json")
            if (target / "repair-bundle.json").is_file()
            else None
        ),
    }
    return payload


def status_exit_code(status: str) -> int:
    return {
        "ok": EXIT_OK,
        "cached": EXIT_OK,
        "runtime_unavailable": EXIT_RUNTIME_UNAVAILABLE,
        "dependency_missing": EXIT_RUNTIME_UNAVAILABLE,
        "low_quality": EXIT_LOW_QUALITY,
        "unsupported": EXIT_CONVERSION_FAILURE,
        "conversion_failed": EXIT_CONVERSION_FAILURE,
        "needs_semantic_cleanup": EXIT_NEEDS_SEMANTIC_CLEANUP,
        "needs_user_action": EXIT_NEEDS_USER_ACTION,
    }.get(status, EXIT_CONVERSION_FAILURE)
