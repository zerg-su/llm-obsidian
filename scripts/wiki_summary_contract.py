#!/usr/bin/env python3
"""Versioned contract for dispatch task -> wiki summary handoffs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Collection, Mapping

from outcome_contract import OutcomeContractError, extract_from_bytes


SCHEMA_VERSION = 2
TYPES = {"session", "decision", "runbook", "incident", "service-update", "repo-touch"}
OUTCOME_DISPOSITIONS = {
    "achieved",
    "partially-achieved",
    "not-achieved",
}
MAX_OUTCOME_EVIDENCE_IDS = 32
MAX_RESIDUAL_GAPS = 32
SUMMARY_V1_FIELDS = {"schema_version", "type", "title", "session", "body"}
SUMMARY_V2_FIELDS = SUMMARY_V1_FIELDS | {
    "outcome_disposition",
    "outcome_evidence_ids",
    "residual_gap_pointers",
}
EVIDENCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


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


def _bounded_unique_strings(
    value: Any,
    field: str,
    *,
    maximum: int,
    identifier: bool = False,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise WikiSummaryError(f"{field} must be an array with at most {maximum} items")
    result: list[str] = []
    for index, item in enumerate(value):
        text = clean_text(item, f"{field}[{index}]", limit=500)
        if "\n" in text or "\r" in text:
            raise WikiSummaryError(f"{field}[{index}] must be one bounded pointer")
        if identifier and not EVIDENCE_ID_RE.fullmatch(text):
            raise WikiSummaryError(f"{field}[{index}] must be a bounded evidence ID")
        result.append(text)
    if len(result) != len(set(result)):
        raise WikiSummaryError(f"{field} values must be unique")
    return result


def validate_summary(
    raw: Any,
    *,
    allow_missing_session: bool = True,
    require_schema: bool = False,
    declared_evidence_ids: Collection[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise WikiSummaryError("summary must be an object")
    if require_schema and "schema_version" not in raw:
        raise WikiSummaryError("schema_version is required for canonical JSON")
    version = raw.get("schema_version", 1)
    if version not in {1, 2}:
        raise WikiSummaryError("schema_version must be 1 or 2")
    allowed = SUMMARY_V2_FIELDS if version == 2 else SUMMARY_V1_FIELDS
    unknown = set(raw) - allowed
    if unknown:
        raise WikiSummaryError(
            "summary has unknown fields: " + ", ".join(sorted(unknown))
        )
    if version == 2:
        missing = SUMMARY_V2_FIELDS - set(raw)
        if missing:
            raise WikiSummaryError(
                "Wiki Summary v2 is missing fields: " + ", ".join(sorted(missing))
            )

    typ = clean_text(raw.get("type"), "type", limit=40)
    if typ not in TYPES:
        raise WikiSummaryError(f"type {typ!r} is not one of {sorted(TYPES)}")
    title = clean_text(raw.get("title"), "title", limit=500)
    session = clean_text(raw.get("session"), "session", required=False, limit=200)
    if not session and not allow_missing_session:
        raise WikiSummaryError("session must not be empty")
    result: dict[str, Any] = {
        "schema_version": version,
        "type": typ,
        "title": title,
        "session": session or None,
        "body": clean_text(raw.get("body"), "body"),
    }
    if version == 1:
        return result

    disposition = raw.get("outcome_disposition")
    if disposition not in OUTCOME_DISPOSITIONS:
        raise WikiSummaryError(
            "outcome_disposition must be achieved, partially-achieved, or not-achieved"
        )
    evidence_ids = _bounded_unique_strings(
        raw.get("outcome_evidence_ids"),
        "outcome_evidence_ids",
        maximum=MAX_OUTCOME_EVIDENCE_IDS,
        identifier=True,
    )
    if declared_evidence_ids is None:
        raise WikiSummaryError(
            "Wiki Summary v2 requires declared Outcome Contract evidence IDs"
        )
    declared_evidence = set(declared_evidence_ids)
    submitted_evidence = set(evidence_ids)
    undeclared = submitted_evidence - declared_evidence
    if undeclared:
        raise WikiSummaryError(
            "outcome_evidence_ids are not declared by the Outcome Contract: "
            + ", ".join(sorted(undeclared))
        )
    residual = _bounded_unique_strings(
        raw.get("residual_gap_pointers"),
        "residual_gap_pointers",
        maximum=MAX_RESIDUAL_GAPS,
    )
    if disposition == "achieved":
        if submitted_evidence != declared_evidence:
            raise WikiSummaryError(
                "achieved requires every declared Outcome Contract evidence ID"
            )
        if residual:
            raise WikiSummaryError("achieved cannot carry residual gap pointers")
    elif not residual:
        raise WikiSummaryError(
            f"{disposition} requires at least one residual gap pointer"
        )
    result.update(
        {
            "outcome_disposition": disposition,
            "outcome_evidence_ids": evidence_ids,
            "residual_gap_pointers": residual,
        }
    )
    return result


def validate_summary_for_task(
    raw: Any,
    meta: Mapping[str, Any],
    *,
    allow_missing_session: bool = True,
    require_schema: bool = False,
) -> dict[str, Any]:
    version = meta.get("version")
    if version != 4:
        summary = validate_summary(
            raw,
            allow_missing_session=allow_missing_session,
            require_schema=require_schema,
        )
        if version == 3 and summary["schema_version"] != 1:
            raise WikiSummaryError("v3 tasks require Wiki Summary v1")
        return summary
    plan = Path(str(meta.get("plan_file") or "")).expanduser().resolve()
    expected = str(meta.get("outcome_contract_sha256") or "")
    try:
        contract = extract_from_bytes(plan.read_bytes())
    except (OSError, OutcomeContractError) as exc:
        raise WikiSummaryError(f"task Outcome Contract is invalid: {exc}") from exc
    if contract.sha256 != expected:
        raise WikiSummaryError("task Outcome Contract digest changed")
    summary = validate_summary(
        raw,
        allow_missing_session=allow_missing_session,
        require_schema=require_schema,
        declared_evidence_ids=contract.evidence_ids,
    )
    if summary["schema_version"] != 2:
        raise WikiSummaryError("v4 tasks require Wiki Summary v2")
    return summary


def render_markdown(summary: dict[str, Any]) -> str:
    session = summary.get("session") or ""
    outcome = ""
    if summary.get("schema_version") == 2:
        evidence = ", ".join(summary["outcome_evidence_ids"]) or "none"
        gaps = "\n".join(
            f"- {pointer}" for pointer in summary["residual_gap_pointers"]
        ) or "- none"
        outcome = (
            "\n## Outcome\n\n"
            f"Outcome disposition: `{summary['outcome_disposition']}`\n\n"
            f"Outcome evidence IDs: {evidence}\n\n"
            f"Residual gaps:\n{gaps}\n"
        )
    return (
        "## Wiki Summary\n\n"
        f"type: {summary['type']}\n"
        f"title: {summary['title']}\n"
        f"session: {session}\n\n"
        f"{summary['body'].rstrip()}\n"
        f"{outcome}"
    )
