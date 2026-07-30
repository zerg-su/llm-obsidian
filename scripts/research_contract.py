#!/usr/bin/env python3
"""Content-free manifests for the untrusted fetch and cited-result seams."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse


SCHEMA_VERSION = 2
SOURCE_CLASSES = {"official", "internal", "third-party"}
MAX_SOURCES = 50
MAX_SOURCE_BYTES = 500_000
MAX_TOTAL_BYTES = 5_000_000
MAX_RESULT_BYTES = 500_000


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


def required_sha256(value: Any, field: str) -> str:
    digest = required_text(value, field, 64)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ResearchContractError(f"{field} must be a lowercase sha256")
    return digest


def relative_pointer(value: Any, field: str, *, prefix: str | None = None) -> str:
    raw = required_text(value, field, 1000)
    path = PurePosixPath(raw)
    if (
        "\\" in raw
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or path.as_posix() != raw
    ):
        raise ResearchContractError(f"{field} must be a normalized relative path")
    if prefix is not None and (not path.parts or path.parts[0] != prefix):
        raise ResearchContractError(f"{field} must stay under {prefix}/")
    return raw


def _read_owned_file(root: Path, pointer: str, field: str, limit: int) -> bytes:
    path = root.joinpath(*PurePosixPath(pointer).parts)
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise ResearchContractError(f"{field} escapes its artifact root") from exc
    current = root
    has_symlink = False
    for part in PurePosixPath(pointer).parts:
        current = current / part
        has_symlink = has_symlink or current.is_symlink()
    if has_symlink or not path.is_file():
        raise ResearchContractError(f"{field} must reference a regular non-symlink file")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ResearchContractError(f"{field} cannot be read") from exc
    if not content or len(content) > limit or b"\0" in content:
        raise ResearchContractError(f"{field} content is empty, oversized, or contains NUL")
    return content


def validate_artifact(
    raw: Any,
    *,
    root: Path,
    expected_run_id: str | None = None,
    expected_request_sha256: str | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise ResearchContractError(
            f"artifact schema_version must be {SCHEMA_VERSION}"
        )
    allowed_top = {
        "schema_version",
        "run_id",
        "request_sha256",
        "fetched_at",
        "sources",
        "fetch_errors",
    }
    if set(raw) - allowed_top:
        raise ResearchContractError("artifact contains unrecognized or inline content fields")
    run_id = required_text(raw.get("run_id"), "run_id", 100)
    request_sha256 = required_sha256(raw.get("request_sha256"), "request_sha256")
    if expected_run_id is not None and run_id != expected_run_id:
        raise ResearchContractError("run_id does not match active research run")
    if (
        expected_request_sha256 is not None
        and request_sha256 != expected_request_sha256
    ):
        raise ResearchContractError("request digest does not match active research run")
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
    seen_paths: set[str] = set()
    for index, source in enumerate(raw_sources):
        field = f"sources[{index}]"
        if not isinstance(source, dict) or set(source) != {
            "url",
            "title",
            "content_path",
            "content_sha256",
            "source_class",
        }:
            raise ResearchContractError(
                f"{field} must be a content-free source manifest"
            )
        url = required_text(source.get("url"), f"{field}.url", 4000)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ResearchContractError(f"{field}.url must be HTTP(S)")
        content_path = relative_pointer(
            source.get("content_path"), f"{field}.content_path", prefix="sources"
        )
        content_parts = PurePosixPath(content_path).parts
        if (
            len(content_parts) != 2
            or not content_parts[1].endswith(".md")
            or content_path in seen_paths
        ):
            raise ResearchContractError(
                f"{field}.content_path must be a unique direct sources/*.md path"
            )
        seen_paths.add(content_path)
        content = _read_owned_file(
            root, content_path, f"{field}.content_path", MAX_SOURCE_BYTES
        )
        total += len(content)
        digest = required_sha256(
            source.get("content_sha256"), f"{field}.content_sha256"
        )
        if digest != hashlib.sha256(content).hexdigest():
            raise ResearchContractError(f"{field}.content_sha256 mismatch")
        source_class = required_text(
            source.get("source_class"), f"{field}.source_class", 40
        )
        if source_class not in SOURCE_CLASSES:
            raise ResearchContractError(
                f"{field}.source_class must be one of {sorted(SOURCE_CLASSES)}"
            )
        sources.append(
            {
                "url": url,
                "title": required_text(
                    source.get("title"), f"{field}.title", 1000
                ),
                "content_path": content_path,
                "content_sha256": digest,
                "source_class": source_class,
            }
        )
    if total > MAX_TOTAL_BYTES:
        raise ResearchContractError(
            f"source content exceeds {MAX_TOTAL_BYTES} total bytes"
        )
    errors = raw.get("fetch_errors", [])
    if not isinstance(errors, list) or len(errors) > 100:
        raise ResearchContractError(
            "fetch_errors must be a list with at most 100 items"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "request_sha256": request_sha256,
        "fetched_at": fetched_at,
        "sources": sources,
        "fetch_errors": [
            required_text(item, "fetch_errors[]", 2000) for item in errors
        ],
    }


def load_artifact(path: str, **expected: str | None) -> dict[str, Any]:
    artifact_path = Path(path)
    try:
        raw = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchContractError(f"cannot read artifact JSON: {exc}") from exc
    return validate_artifact(raw, root=artifact_path.parent, **expected)


def validate_result_artifact(
    raw: Any,
    *,
    root: Path,
    expected_run_id: str,
    source_urls: set[str],
) -> dict[str, Any]:
    """Validate one cited Markdown artifact without copying its body into state."""

    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "run_id",
        "status",
        "artifact",
    }:
        raise ResearchContractError("result envelope has an invalid shape")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ResearchContractError(
            f"result schema_version must be {SCHEMA_VERSION}"
        )
    if raw.get("run_id") != expected_run_id or raw.get("status") != "complete":
        raise ResearchContractError("result identity or status is invalid")
    artifact = raw.get("artifact")
    if not isinstance(artifact, dict) or set(artifact) != {
        "kind",
        "path",
        "sha256",
        "citations",
    }:
        raise ResearchContractError("result artifact has an invalid shape")
    if artifact.get("kind") != "cited-markdown":
        raise ResearchContractError("result artifact kind must be cited-markdown")
    path = relative_pointer(artifact.get("path"), "artifact.path")
    if path != "answer.md":
        raise ResearchContractError("result artifact path must be answer.md")
    body = _read_owned_file(root, path, "artifact.path", MAX_RESULT_BYTES)
    digest = required_sha256(artifact.get("sha256"), "artifact.sha256")
    if hashlib.sha256(body).hexdigest() != digest:
        raise ResearchContractError("artifact.sha256 mismatch")
    try:
        answer = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ResearchContractError("answer.md must be UTF-8") from exc
    citations = artifact.get("citations")
    if not isinstance(citations, list) or not 1 <= len(citations) <= MAX_SOURCES:
        raise ResearchContractError("artifact.citations must be a bounded non-empty list")
    validated: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, citation in enumerate(citations):
        field = f"artifact.citations[{index}]"
        if not isinstance(citation, dict) or set(citation) != {
            "url",
            "title",
            "source_class",
        }:
            raise ResearchContractError(f"{field} has an invalid shape")
        url = required_text(citation.get("url"), f"{field}.url", 4000)
        source_class = required_text(
            citation.get("source_class"), f"{field}.source_class", 40
        )
        if (
            url not in source_urls
            or url in seen
            or url not in answer
            or source_class not in SOURCE_CLASSES
        ):
            raise ResearchContractError(
                f"{field} must cite one unique fetched source visible in answer.md"
            )
        seen.add(url)
        validated.append(
            {
                "url": url,
                "title": required_text(
                    citation.get("title"), f"{field}.title", 1000
                ),
                "source_class": source_class,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": expected_run_id,
        "status": "complete",
        "artifact": {
            "kind": "cited-markdown",
            "path": path,
            "sha256": digest,
            "citations": validated,
        },
    }
