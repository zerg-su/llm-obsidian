#!/usr/bin/env python3
"""Bounded canonical Outcome Contract extraction and identity."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


SCHEMA_VERSION = 1
MAX_CONTRACT_BYTES = 32_768
MAX_PURPOSE_CHARS = 2_000
MAX_OUTCOME_CHARS = 4_000
MAX_EVIDENCE_ITEMS = 32
MAX_OBSERVABLE_CHARS = 2_000
MAX_NON_GOALS = 32
MAX_NON_GOAL_CHARS = 2_000
FIELDS = frozenset(
    {
        "schema_version",
        "purpose",
        "desired_outcome",
        "success_evidence",
        "non_goals",
    }
)
REQUIRED_FIELDS = frozenset(
    {"schema_version", "desired_outcome", "success_evidence", "non_goals"}
)
EVIDENCE_FIELDS = frozenset({"evidence_id", "observable"})
EVIDENCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
JSON_FENCE_RE = re.compile(
    r"(?ms)^[ \t]*```json[ \t]*\r?\n(.*?)[ \t]*^```[ \t]*$"
)
CONTRACT_KEY_RE = re.compile(
    r'"(?:desired_outcome|success_evidence|non_goals)"\s*:'
)


class OutcomeContractError(ValueError):
    """A plan does not contain one exact bounded Outcome Contract."""


@dataclass(frozen=True)
class OutcomeContract:
    value: Mapping[str, Any]
    canonical: bytes
    sha256: str
    evidence_ids: tuple[str, ...]


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise OutcomeContractError(f"duplicate JSON member: {key}")
        value[key] = item
    return value


def _bounded_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise OutcomeContractError(f"{field} must be a string")
    if not value.strip():
        raise OutcomeContractError(f"{field} must not be empty")
    if len(value) > maximum or "\x00" in value:
        raise OutcomeContractError(f"{field} is too large or contains NUL")
    return value


def validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OutcomeContractError("Outcome Contract must be a JSON object")
    unknown = set(value) - FIELDS
    missing = REQUIRED_FIELDS - set(value)
    if unknown:
        raise OutcomeContractError(
            "Outcome Contract has unknown fields: " + ", ".join(sorted(unknown))
        )
    if missing:
        raise OutcomeContractError(
            "Outcome Contract is missing fields: " + ", ".join(sorted(missing))
        )
    if value.get("schema_version") != SCHEMA_VERSION:
        raise OutcomeContractError(
            f"Outcome Contract schema_version must be {SCHEMA_VERSION}"
        )

    normalized: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "desired_outcome": _bounded_text(
            value.get("desired_outcome"), "desired_outcome", MAX_OUTCOME_CHARS
        ),
    }
    if "purpose" in value:
        normalized["purpose"] = _bounded_text(
            value["purpose"], "purpose", MAX_PURPOSE_CHARS
        )

    raw_evidence = value.get("success_evidence")
    if (
        not isinstance(raw_evidence, list)
        or not raw_evidence
        or len(raw_evidence) > MAX_EVIDENCE_ITEMS
    ):
        raise OutcomeContractError(
            f"success_evidence must contain 1..{MAX_EVIDENCE_ITEMS} items"
        )
    evidence: list[dict[str, str]] = []
    evidence_ids: list[str] = []
    for index, raw_item in enumerate(raw_evidence):
        if not isinstance(raw_item, dict) or set(raw_item) != EVIDENCE_FIELDS:
            raise OutcomeContractError(
                f"success_evidence[{index}] must contain only evidence_id and observable"
            )
        evidence_id = raw_item.get("evidence_id")
        if not isinstance(evidence_id, str) or not EVIDENCE_ID_RE.fullmatch(
            evidence_id
        ):
            raise OutcomeContractError(
                f"success_evidence[{index}].evidence_id must be a bounded identifier"
            )
        evidence_ids.append(evidence_id)
        evidence.append(
            {
                "evidence_id": evidence_id,
                "observable": _bounded_text(
                    raw_item.get("observable"),
                    f"success_evidence[{index}].observable",
                    MAX_OBSERVABLE_CHARS,
                ),
            }
        )
    if len(evidence_ids) != len(set(evidence_ids)):
        raise OutcomeContractError("success_evidence evidence_id values must be unique")
    normalized["success_evidence"] = evidence

    raw_non_goals = value.get("non_goals")
    if (
        not isinstance(raw_non_goals, list)
        or not raw_non_goals
        or len(raw_non_goals) > MAX_NON_GOALS
    ):
        raise OutcomeContractError(
            f"non_goals must contain 1..{MAX_NON_GOALS} items"
        )
    non_goals = [
        _bounded_text(item, f"non_goals[{index}]", MAX_NON_GOAL_CHARS)
        for index, item in enumerate(raw_non_goals)
    ]
    if len(non_goals) != len(set(non_goals)):
        raise OutcomeContractError("non_goals values must be unique")
    normalized["non_goals"] = non_goals
    if len(canonical_bytes(normalized, validated=True)) > MAX_CONTRACT_BYTES:
        raise OutcomeContractError("Outcome Contract is too large")
    return normalized


def canonical_bytes(value: Mapping[str, Any], *, validated: bool = False) -> bytes:
    normalized = dict(value) if validated else validate(dict(value))
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def extract_from_plan(plan_text: str) -> OutcomeContract:
    if not isinstance(plan_text, str):
        raise OutcomeContractError("plan text must be a string")
    candidates = [
        match.group(1).strip()
        for match in JSON_FENCE_RE.finditer(plan_text)
        if CONTRACT_KEY_RE.search(match.group(1))
    ]
    if len(candidates) != 1:
        raise OutcomeContractError(
            "plan must contain exactly one Outcome Contract JSON block"
        )
    raw = candidates[0]
    if len(raw.encode("utf-8")) > MAX_CONTRACT_BYTES:
        raise OutcomeContractError("Outcome Contract block is too large")
    try:
        decoded = json.loads(raw, object_pairs_hook=_pairs)
    except OutcomeContractError:
        raise
    except json.JSONDecodeError as exc:
        raise OutcomeContractError(f"Outcome Contract JSON is invalid: {exc}") from exc
    normalized = validate(decoded)
    canonical = canonical_bytes(normalized, validated=True)
    evidence_ids = tuple(
        item["evidence_id"] for item in normalized["success_evidence"]
    )
    return OutcomeContract(
        MappingProxyType(normalized),
        canonical,
        hashlib.sha256(canonical).hexdigest(),
        evidence_ids,
    )


def extract_from_bytes(plan_bytes: bytes) -> OutcomeContract:
    try:
        text = plan_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OutcomeContractError("approved plan must be UTF-8") from exc
    return extract_from_plan(text)
