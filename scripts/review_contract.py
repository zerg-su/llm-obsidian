#!/usr/bin/env python3
"""Versioned, model-independent contract for cross-model review handoffs."""

from __future__ import annotations

import base64
import json
import re
import zlib
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
VERDICTS = {"approve", "changes-requested", "blocked"}
SEVERITIES = frozenset({"critical", "important", "minor"})
MATERIAL_SEVERITIES = SEVERITIES - {"minor"}
MODES = {"simple", "deep"}
AXES = {
    "simple": ("holistic",),
    "deep": ("spec", "standards-correctness-architecture-security"),
}
VERIFY_BUDGETS = {"simple": 1, "deep": 2}
IDENTIFIER_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:-]*"
IDENTIFIER_RE = re.compile(rf"{IDENTIFIER_PATTERN}\Z")
FINDING_ID_LIMIT = 100
FINDING_FILE_LIMIT = 1000
FINDING_SUMMARY_LIMIT = 300
FINDING_DETAIL_LIMIT = 4000


class ReviewContractError(ValueError):
    pass


def exact_keys(value: dict[str, Any], expected: set[str], field: str) -> None:
    missing = expected - set(value)
    extra = set(value) - expected
    if missing or extra:
        detail = []
        if missing:
            detail.append(f"missing {sorted(missing)}")
        if extra:
            detail.append(f"unknown {sorted(extra)}")
        raise ReviewContractError(f"{field} has invalid fields: {', '.join(detail)}")


def text(value: Any, field: str, *, required: bool = True, limit: int = 4000) -> str:
    if not isinstance(value, str):
        raise ReviewContractError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ReviewContractError(f"{field} must not be empty")
    if len(value) > limit:
        raise ReviewContractError(f"{field} exceeds {limit} characters")
    return value


def identifier(value: Any, field: str, *, limit: int = 100) -> str:
    value = text(value, field, limit=limit)
    if not IDENTIFIER_RE.fullmatch(value):
        raise ReviewContractError(f"{field} must be a bounded identifier")
    return value


def string_list(value: Any, field: str, *, limit: int = 50) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise ReviewContractError(f"{field} must be a list with at most {limit} items")
    return [text(item, f"{field}[{index}]", limit=2000) for index, item in enumerate(value)]


def safe_file(value: Any, field: str, *, limit: int = FINDING_FILE_LIMIT) -> str:
    path = text(value, field, limit=limit)
    pure = PurePosixPath(path)
    if "\\" in path or pure.is_absolute() or ".." in pure.parts or pure.as_posix() == ".":
        raise ReviewContractError(f"{field} must be a repository-relative path")
    return pure.as_posix()


def sha256(value: Any, field: str) -> str:
    value = text(value, field, limit=64)
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ReviewContractError(f"{field} must be a lowercase sha256")
    return value


def git_head(value: Any, field: str = "head_sha") -> str:
    value = text(value, field, limit=64)
    if not re.fullmatch(r"[0-9a-f]{40,64}", value):
        raise ReviewContractError(f"{field} must be a git object id")
    return value


def validate_finding(item: Any, field: str = "finding") -> dict[str, Any]:
    """Validate and normalize one finding at every review trust boundary."""

    if not isinstance(item, dict):
        raise ReviewContractError(f"{field} must be an object")
    exact_keys(
        item,
        {
            "finding_id",
            "severity",
            "file",
            "line",
            "summary",
            "evidence",
            "recommendation",
        },
        field,
    )
    severity = text(item.get("severity"), f"{field}.severity", limit=20)
    if severity not in SEVERITIES:
        raise ReviewContractError(f"{field}.severity must be one of {sorted(SEVERITIES)}")
    line = item.get("line")
    if line is not None and (not isinstance(line, int) or isinstance(line, bool) or line < 1):
        raise ReviewContractError(f"{field}.line must be a positive integer or null")
    return {
        "finding_id": identifier(
            item.get("finding_id"),
            f"{field}.finding_id",
            limit=FINDING_ID_LIMIT,
        ),
        "severity": severity,
        "file": safe_file(
            item.get("file"),
            f"{field}.file",
            limit=FINDING_FILE_LIMIT,
        ),
        "line": line,
        "summary": text(
            item.get("summary"),
            f"{field}.summary",
            limit=FINDING_SUMMARY_LIMIT,
        ),
        "evidence": text(
            item.get("evidence"),
            f"{field}.evidence",
            limit=FINDING_DETAIL_LIMIT,
        ),
        "recommendation": text(
            item.get("recommendation"),
            f"{field}.recommendation",
            limit=FINDING_DETAIL_LIMIT,
        ),
    }


def finding_constraint_lines() -> tuple[str, ...]:
    """Render reviewer-facing constraints from canonical validator values."""

    return (
        f"`finding_id` must match `{IDENTIFIER_PATTERN}`, contain at most "
        f"{FINDING_ID_LIMIT} characters, and be unique within the review round.",
        "`file` must be a repository-relative POSIX path (not absolute, `.`, or "
        f"containing `..` or `\\`) of at most {FINDING_FILE_LIMIT} characters.",
        f"`summary` is at most {FINDING_SUMMARY_LIMIT} characters; `evidence` and "
        f"`recommendation` are at most {FINDING_DETAIL_LIMIT} characters each.",
        "All finding string fields must be non-empty after trimming surrounding "
        "whitespace.",
    )


def require_unique_finding_ids(
    finding_ids: Iterable[str],
    field: str = "finding_id values",
) -> None:
    """Reject duplicate canonical finding identities."""

    values = list(finding_ids)
    if len(values) != len(set(values)):
        raise ReviewContractError(f"{field} must be unique")


def validate_review(
    raw: Any,
    *,
    expected_operation_id: str | None = None,
    expected_run_id: str | None = None,
    expected_mode: str | None = None,
    expected_head_sha: str | None = None,
    expected_profile: str | None = None,
    expected_profile_sha256: str | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ReviewContractError("review payload must be an object")
    exact_keys(
        raw,
        {
            "schema_version",
            "operation_id",
            "run_id",
            "mode",
            "head_sha",
            "verification_profile",
            "verdict",
            "axes",
            "verification_gaps",
            "notes_for_executor",
            "residual_risks",
        },
        "review payload",
    )
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ReviewContractError(f"schema_version must be {SCHEMA_VERSION}")
    operation_id = identifier(raw.get("operation_id"), "operation_id")
    if expected_operation_id is not None and operation_id != expected_operation_id:
        raise ReviewContractError("operation_id does not match the active review")
    run_id = identifier(raw.get("run_id"), "run_id")
    if expected_run_id is not None and run_id != expected_run_id:
        raise ReviewContractError("run_id does not match the active review")
    mode = text(raw.get("mode"), "mode", limit=20)
    if mode not in MODES:
        raise ReviewContractError(f"mode must be one of {sorted(MODES)}")
    if expected_mode is not None and mode != expected_mode:
        raise ReviewContractError("mode does not match the active review")
    head_sha = git_head(raw.get("head_sha"))
    if expected_head_sha is not None and head_sha != expected_head_sha:
        raise ReviewContractError("head_sha does not match the reviewed HEAD")
    raw_profile = raw.get("verification_profile")
    if not isinstance(raw_profile, dict):
        raise ReviewContractError("verification_profile must be an object")
    exact_keys(raw_profile, {"name", "sha256"}, "verification_profile")
    profile = {
        "name": identifier(raw_profile.get("name"), "verification_profile.name"),
        "sha256": sha256(raw_profile.get("sha256"), "verification_profile.sha256"),
    }
    if expected_profile is not None and profile["name"] != expected_profile:
        raise ReviewContractError("verification profile does not match the active review")
    if (
        expected_profile_sha256 is not None
        and profile["sha256"] != expected_profile_sha256
    ):
        raise ReviewContractError("verification profile digest does not match the active review")
    verdict = text(raw.get("verdict"), "verdict", limit=40)
    if verdict not in VERDICTS:
        raise ReviewContractError(f"verdict must be one of {sorted(VERDICTS)}")

    raw_axes = raw.get("axes")
    if not isinstance(raw_axes, list) or len(raw_axes) != len(AXES[mode]):
        raise ReviewContractError(f"{mode} review must contain exactly {len(AXES[mode])} axes")
    axes: list[dict[str, Any]] = []
    for index, item in enumerate(raw_axes):
        field = f"axes[{index}]"
        if not isinstance(item, dict):
            raise ReviewContractError(f"{field} must be an object")
        exact_keys(
            item,
            {"axis", "verdict", "verification_iteration", "findings"},
            field,
        )
        axis = text(item.get("axis"), f"{field}.axis", limit=100)
        axis_verdict = text(item.get("verdict"), f"{field}.verdict", limit=40)
        if axis_verdict not in VERDICTS:
            raise ReviewContractError(f"{field}.verdict must be one of {sorted(VERDICTS)}")
        iteration = item.get("verification_iteration")
        if not isinstance(iteration, int) or isinstance(iteration, bool) or iteration < 0:
            raise ReviewContractError(f"{field}.verification_iteration must be a non-negative integer")
        if iteration > VERIFY_BUDGETS[mode]:
            raise ReviewContractError(
                f"{field}.verification_iteration exceeds the {mode} review budget"
            )
        raw_findings = item.get("findings")
        if not isinstance(raw_findings, list) or len(raw_findings) > 50:
            raise ReviewContractError(f"{field}.findings must be a list with at most 50 items")
        findings = [
            validate_finding(finding, f"{field}.findings[{finding_index}]")
            for finding_index, finding in enumerate(raw_findings)
        ]
        if axis_verdict == "approve" and any(
            finding["severity"] in MATERIAL_SEVERITIES for finding in findings
        ):
            raise ReviewContractError(f"{field} cannot approve with material findings")
        axes.append(
            {
                "axis": axis,
                "verdict": axis_verdict,
                "verification_iteration": iteration,
                "findings": findings,
            }
        )

    actual_axes = tuple(item["axis"] for item in axes)
    if actual_axes != AXES[mode]:
        raise ReviewContractError(
            f"{mode} review axes must be ordered as {list(AXES[mode])}"
        )
    finding_ids = [
        finding["finding_id"]
        for axis_result in axes
        for finding in axis_result["findings"]
    ]
    require_unique_finding_ids(
        finding_ids,
        "finding_id values across review axes",
    )
    expected_verdict = (
        "blocked"
        if any(item["verdict"] == "blocked" for item in axes)
        else "changes-requested"
        if any(item["verdict"] == "changes-requested" for item in axes)
        else "approve"
    )
    if verdict != expected_verdict:
        raise ReviewContractError("top-level verdict must equal the aggregate axis verdict")

    return {
        "schema_version": SCHEMA_VERSION,
        "operation_id": operation_id,
        "run_id": run_id,
        "mode": mode,
        "head_sha": head_sha,
        "verification_profile": profile,
        "verdict": verdict,
        "axes": axes,
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
    validated = validate_review(review)
    raw = json.dumps(validated, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(zlib.compress(raw, level=9)).decode("ascii")


def decode_review(token: str, **expected: str | None) -> dict[str, Any]:
    try:
        raw = zlib.decompress(base64.urlsafe_b64decode(token.encode("ascii"))).decode("utf-8")
    except (ValueError, UnicodeError, zlib.error) as exc:
        raise ReviewContractError(f"invalid review payload token: {exc}") from exc
    return parse_review_json(raw, **expected)


def render_markdown(review: dict[str, Any], task_name: str) -> str:
    review = validate_review(review)
    lines = [
        f"# Cross-Model Review: {task_name}",
        "",
        f"Verdict: {review['verdict']}",
        f"Mode: {review['mode']}",
        f"Operation: {review['operation_id']}",
        f"Run: {review['run_id']}",
        f"HEAD: {review['head_sha']}",
        (
            "Verification profile: "
            f"{review['verification_profile']['name']} "
            f"({review['verification_profile']['sha256']})"
        ),
        "",
    ]
    for axis in review["axes"]:
        lines.extend(
            [
                "",
                f"## Axis: {axis['axis']}",
                "",
                f"Verdict: {axis['verdict']}",
                f"Verification iteration: {axis['verification_iteration']}",
                "",
                "### Findings",
                "",
            ]
        )
        if not axis["findings"]:
            lines.append("Findings: none")
        for index, finding in enumerate(axis["findings"], 1):
            location = finding["file"] + (
                f":{finding['line']}" if finding["line"] else ""
            )
            lines.extend(
                [
                    f"{index}. ID: {finding['finding_id']}",
                    f"   Severity: {finding['severity']}",
                    f"   File: {location}",
                    f"   Issue: {finding['summary']}",
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
