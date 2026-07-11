#!/usr/bin/env python3
"""Versioned, model-independent contract for cross-model review handoffs."""

from __future__ import annotations

import base64
import json
import zlib
from pathlib import PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
VERDICTS = {"approve", "changes-requested", "blocked"}
SEVERITIES = {"blocking", "warning", "nit"}
MODES = {"full", "light"}


class ReviewContractError(ValueError):
    pass


def text(value: Any, field: str, *, required: bool = True, limit: int = 4000) -> str:
    if not isinstance(value, str):
        raise ReviewContractError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ReviewContractError(f"{field} must not be empty")
    if len(value) > limit:
        raise ReviewContractError(f"{field} exceeds {limit} characters")
    return value


def string_list(value: Any, field: str, *, limit: int = 50) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise ReviewContractError(f"{field} must be a list with at most {limit} items")
    return [text(item, f"{field}[{index}]", limit=2000) for index, item in enumerate(value)]


def safe_file(value: Any, field: str) -> str:
    path = text(value, field, limit=1000)
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts:
        raise ReviewContractError(f"{field} must be a repository-relative path")
    return path


def validate_review(
    raw: Any, *, expected_run_id: str | None = None, expected_mode: str | None = None
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ReviewContractError("review payload must be an object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ReviewContractError(f"schema_version must be {SCHEMA_VERSION}")
    run_id = text(raw.get("run_id"), "run_id", limit=100)
    if expected_run_id is not None and run_id != expected_run_id:
        raise ReviewContractError("run_id does not match the active review")
    mode = text(raw.get("mode"), "mode", limit=20)
    if mode not in MODES:
        raise ReviewContractError(f"mode must be one of {sorted(MODES)}")
    if expected_mode is not None and mode != expected_mode:
        raise ReviewContractError("mode does not match the active review")
    verdict = text(raw.get("verdict"), "verdict", limit=40)
    if verdict not in VERDICTS:
        raise ReviewContractError(f"verdict must be one of {sorted(VERDICTS)}")

    raw_findings = raw.get("findings")
    if not isinstance(raw_findings, list) or len(raw_findings) > 50:
        raise ReviewContractError("findings must be a list with at most 50 items")
    findings: list[dict[str, Any]] = []
    for index, item in enumerate(raw_findings):
        field = f"findings[{index}]"
        if not isinstance(item, dict):
            raise ReviewContractError(f"{field} must be an object")
        severity = text(item.get("severity"), f"{field}.severity", limit=20)
        if severity not in SEVERITIES:
            raise ReviewContractError(f"{field}.severity must be one of {sorted(SEVERITIES)}")
        line = item.get("line")
        if line is not None and (not isinstance(line, int) or isinstance(line, bool) or line < 1):
            raise ReviewContractError(f"{field}.line must be a positive integer or null")
        findings.append(
            {
                "severity": severity,
                "file": safe_file(item.get("file"), f"{field}.file"),
                "line": line,
                "title": text(item.get("title"), f"{field}.title", limit=300),
                "evidence": text(item.get("evidence"), f"{field}.evidence"),
                "recommendation": text(item.get("recommendation"), f"{field}.recommendation"),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "mode": mode,
        "verdict": verdict,
        "findings": findings,
        "verification_gaps": string_list(raw.get("verification_gaps", []), "verification_gaps"),
        "notes_for_executor": string_list(raw.get("notes_for_executor", []), "notes_for_executor"),
        "residual_risks": string_list(raw.get("residual_risks", []), "residual_risks"),
    }


def parse_review_json(value: str, **expected: str | None) -> dict[str, Any]:
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ReviewContractError(f"review payload is not valid JSON: {exc}") from exc
    return validate_review(raw, **expected)


def encode_review(review: dict[str, Any]) -> str:
    raw = json.dumps(review, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(zlib.compress(raw, level=9)).decode("ascii")


def decode_review(token: str, **expected: str | None) -> dict[str, Any]:
    try:
        raw = zlib.decompress(base64.urlsafe_b64decode(token.encode("ascii"))).decode("utf-8")
    except (ValueError, UnicodeError, zlib.error) as exc:
        raise ReviewContractError(f"invalid review payload token: {exc}") from exc
    return parse_review_json(raw, **expected)


def render_markdown(review: dict[str, Any], task_name: str) -> str:
    lines = [
        f"# Cross-Model Review: {task_name}",
        "",
        f"Verdict: {review['verdict']}",
        f"Mode: {review['mode']}",
        f"Run: {review['run_id']}",
        "",
        "## Findings",
        "",
    ]
    if not review["findings"]:
        lines.append("Findings: none")
    for index, finding in enumerate(review["findings"], 1):
        location = finding["file"] + (f":{finding['line']}" if finding["line"] else "")
        lines.extend(
            [
                f"{index}. Severity: {finding['severity']}",
                f"   File: {location}",
                f"   Issue: {finding['title']}",
                f"   Evidence: {finding['evidence']}",
                f"   Suggested fix: {finding['recommendation']}",
            ]
        )
    for heading, key in (
        ("Verification Gaps", "verification_gaps"),
        ("Residual Risks", "residual_risks"),
        ("Notes For Executor", "notes_for_executor"),
    ):
        lines.extend(["", f"## {heading}", ""])
        values = review[key]
        lines.extend(f"- {value}" for value in values) if values else lines.append("- None")
    return "\n".join(lines) + "\n"
