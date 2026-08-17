#!/usr/bin/env python3
"""Strict executor input and harness-owned review resolution evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from review_contract import MATERIAL_SEVERITIES
from review_contract import review_axis_provider


SCHEMA_VERSION = 1
DISPOSITIONS = frozenset({"applied", "rejected", "out-of-scope"})
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
GIT_HEAD = re.compile(r"[0-9a-f]{40,64}\Z")
MAX_RESOLUTIONS = 50
MAX_RATIONALE_CHARS = 2_000
MAX_FOLLOW_UP_CHARS = 500
MAX_FIX_DELTA_BYTES = 65_536
MAX_FIX_DELTA_TOTAL_BYTES = 131_072
MAX_FIX_DELTA_CANONICAL_BYTES = 1_048_576
# Committed acceptance-evidence bytes are reviewer-readable at the exact HEAD;
# they are excluded from the bounded resolution fix delta so they can never
# crowd the product fix out of the transport cap. Product paths keep the cap,
# and an evidence-only resolution still fails closed as an empty fix delta.
FIX_DELTA_EXCLUDED_PATHSPECS = (":(exclude)docs/acceptance/evidence",)


def fix_delta_command(reviewed_head: str, resolved_head: str) -> tuple[str, ...]:
    """Return the single registered git argv tail for the resolution fix delta."""

    if not GIT_HEAD.fullmatch(reviewed_head) or not GIT_HEAD.fullmatch(resolved_head):
        raise ResolutionError("fix delta heads must be exact git object ids")
    return (
        "diff",
        "--binary",
        "--no-ext-diff",
        reviewed_head,
        resolved_head,
        "--",
        *FIX_DELTA_EXCLUDED_PATHSPECS,
    )


class ResolutionError(ValueError):
    pass


def _exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ResolutionError(f"{label} has invalid fields")


def _text(value: Any, field: str, *, limit: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ResolutionError(f"{field} must be a string")
    normalized = value.strip()
    if required and not normalized:
        raise ResolutionError(f"{field} must not be empty")
    if len(normalized) > limit:
        raise ResolutionError(f"{field} exceeds {limit} characters")
    return normalized


def _identifier(value: Any, field: str) -> str:
    normalized = _text(value, field, limit=128)
    if not IDENTIFIER.fullmatch(normalized):
        raise ResolutionError(f"{field} must be a bounded identifier")
    return normalized


def _head(value: Any, field: str) -> str:
    normalized = _text(value, field, limit=64)
    if not GIT_HEAD.fullmatch(normalized):
        raise ResolutionError(f"{field} must be an exact Git object id")
    return normalized


def review_transport_identity_sha256(
    review_operation_id: str,
    callbacks: Sequence[Mapping[str, Any]],
) -> str:
    """Hash one ordered review operation and its exact accepted callbacks."""

    operation_id = _identifier(
        review_operation_id, "review_operation_id"
    )
    normalized: list[dict[str, str]] = []
    for index, callback in enumerate(callbacks):
        if not isinstance(callback, Mapping):
            raise ResolutionError(
                f"review_callbacks[{index}] must be an object"
            )
        _exact_fields(
            callback,
            {
                "axis",
                "round_operation_id",
                "round_run_id",
                "callback_id",
                "callback_sha256",
            },
            f"review_callbacks[{index}]",
        )
        axis = _text(
            callback.get("axis"),
            f"review_callbacks[{index}].axis",
            limit=64,
        )
        try:
            review_axis_provider(axis)
        except ValueError as exc:
            raise ResolutionError("review callback axis is invalid") from exc
        callback_sha256 = _text(
            callback.get("callback_sha256"),
            f"review_callbacks[{index}].callback_sha256",
            limit=64,
        )
        if re.fullmatch(r"[0-9a-f]{64}", callback_sha256) is None:
            raise ResolutionError("review callback digest is invalid")
        normalized.append(
            {
                "axis": axis,
                "round_operation_id": _identifier(
                    callback.get("round_operation_id"),
                    f"review_callbacks[{index}].round_operation_id",
                ),
                "round_run_id": _identifier(
                    callback.get("round_run_id"),
                    f"review_callbacks[{index}].round_run_id",
                ),
                "callback_id": _identifier(
                    callback.get("callback_id"),
                    f"review_callbacks[{index}].callback_id",
                ),
                "callback_sha256": callback_sha256,
            }
        )
    axes = [callback["axis"] for callback in normalized]
    if not axes or len(axes) != len(set(axes)):
        raise ResolutionError(
            "review callbacks must cover unique ordered axes"
        )
    payload = {
        "review_operation_id": operation_id,
        "review_callbacks": normalized,
    }
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _follow_up(value: Any, field: str) -> str:
    normalized = _text(value, field, limit=MAX_FOLLOW_UP_CHARS)
    if normalized.startswith("https://"):
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ResolutionError(
                f"{field} must be an https URL, wikilink, or repository-relative path"
            )
        return normalized
    if normalized[:2] == "[[" and normalized[-2:] == "]]":
        target = normalized[2:-2].strip()
        if target and "\n" not in target and "\r" not in target:
            return normalized
    path = PurePosixPath(normalized)
    if (
        "\\" in normalized
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() == "."
    ):
        raise ResolutionError(
            f"{field} must be an https URL, wikilink, or repository-relative path"
        )
    return path.as_posix()


@dataclass(frozen=True)
class FindingResolution:
    finding_id: str
    disposition: str
    rationale: str
    follow_up: str = ""

    def __post_init__(self) -> None:
        if not IDENTIFIER.fullmatch(self.finding_id):
            raise ResolutionError("finding_id must be a bounded identifier")
        if self.disposition not in DISPOSITIONS:
            raise ResolutionError("resolution disposition is not terminal")
        if not self.rationale or len(self.rationale) > MAX_RATIONALE_CHARS:
            raise ResolutionError("resolution rationale is required and bounded")
        if self.disposition == "out-of-scope":
            _follow_up(self.follow_up, "follow_up")
        elif self.follow_up:
            raise ResolutionError(
                "follow_up is allowed only for out-of-scope findings"
            )

    def payload(self) -> dict[str, str]:
        return {
            "finding_id": self.finding_id,
            "disposition": self.disposition,
            "rationale": self.rationale,
            "follow_up": self.follow_up,
        }


@dataclass(frozen=True)
class ReviewResolution:
    operation_id: str
    reviewed_head_sha: str
    resolved_head_sha: str
    resolutions: tuple[FindingResolution, ...]
    review_identity_sha256: str = ""

    def __post_init__(self) -> None:
        if not IDENTIFIER.fullmatch(self.operation_id):
            raise ResolutionError("operation_id must be a bounded identifier")
        if (
            not GIT_HEAD.fullmatch(self.reviewed_head_sha)
            or not GIT_HEAD.fullmatch(self.resolved_head_sha)
            or self.reviewed_head_sha == self.resolved_head_sha
        ):
            raise ResolutionError("review resolution requires two distinct exact HEADs")
        ids = [item.finding_id for item in self.resolutions]
        if not ids or len(ids) > MAX_RESOLUTIONS or len(ids) != len(set(ids)):
            raise ResolutionError("review resolutions must be non-empty and unique")
        if self.review_identity_sha256 and re.fullmatch(
            r"[0-9a-f]{64}", self.review_identity_sha256
        ) is None:
            raise ResolutionError("review resolution identity digest is invalid")


@dataclass(frozen=True)
class ReviewResolutionEvidence:
    operation_id: str
    axis: str
    reviewed_head_sha: str
    resolved_head_sha: str
    fix_delta_sha256: str
    previous_finding_ids: tuple[str, ...]
    resolutions: Mapping[str, FindingResolution]

    def __post_init__(self) -> None:
        if not IDENTIFIER.fullmatch(self.operation_id):
            raise ResolutionError("resolution evidence operation is invalid")
        try:
            review_axis_provider(self.axis)
        except ValueError as exc:
            raise ResolutionError("resolution evidence axis is invalid") from exc
        if (
            not GIT_HEAD.fullmatch(self.reviewed_head_sha)
            or not GIT_HEAD.fullmatch(self.resolved_head_sha)
            or self.reviewed_head_sha == self.resolved_head_sha
        ):
            raise ResolutionError("resolution evidence HEADs are invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.fix_delta_sha256):
            raise ResolutionError("resolution evidence delta digest is invalid")
        mapping = dict(self.resolutions)
        if (
            len(mapping) > MAX_RESOLUTIONS
            or len(self.previous_finding_ids)
            != len(set(self.previous_finding_ids))
            or tuple(mapping) != self.previous_finding_ids
        ):
            raise ResolutionError("resolution evidence finding order changed")
        if any(
            finding_id != item.finding_id
            for finding_id, item in mapping.items()
        ):
            raise ResolutionError("resolution evidence finding identity changed")
        object.__setattr__(self, "resolutions", MappingProxyType(mapping))

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "axis": self.axis,
            "reviewed_head_sha": self.reviewed_head_sha,
            "resolved_head_sha": self.resolved_head_sha,
            "fix_delta_sha256": self.fix_delta_sha256,
            "previous_finding_ids": list(self.previous_finding_ids),
            "resolutions": [
                self.resolutions[finding_id].payload()
                for finding_id in self.previous_finding_ids
            ],
        }


def validate_resolution(
    raw: Any,
    *,
    expected_operation_id: str,
    expected_reviewed_head_sha: str,
    expected_resolved_head_sha: str,
    expected_finding_ids: Sequence[str],
    expected_review_identity_sha256: str = "",
) -> ReviewResolution:
    if not isinstance(raw, dict):
        raise ResolutionError("review resolution must be an object")
    expected_fields = {
        "schema_version",
        "operation_id",
        "reviewed_head_sha",
        "resolved_head_sha",
        "resolutions",
    }
    if "review_identity_sha256" in raw:
        expected_fields.add("review_identity_sha256")
    _exact_fields(raw, expected_fields, "review resolution")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ResolutionError("review resolution schema is unsupported")
    operation_id = _identifier(raw.get("operation_id"), "operation_id")
    reviewed_head = _head(raw.get("reviewed_head_sha"), "reviewed_head_sha")
    resolved_head = _head(raw.get("resolved_head_sha"), "resolved_head_sha")
    if operation_id != expected_operation_id:
        raise ResolutionError("review resolution operation identity changed")
    if reviewed_head != expected_reviewed_head_sha:
        raise ResolutionError("review resolution reviewed HEAD changed")
    if resolved_head != expected_resolved_head_sha:
        raise ResolutionError("review resolution resolved HEAD changed")
    if reviewed_head == resolved_head:
        raise ResolutionError("review resolution requires a new HEAD")
    review_identity_sha256 = str(
        raw.get("review_identity_sha256") or ""
    )
    if review_identity_sha256 and re.fullmatch(
        r"[0-9a-f]{64}", review_identity_sha256
    ) is None:
        raise ResolutionError("review resolution identity digest is invalid")
    if (
        expected_review_identity_sha256
        and review_identity_sha256 != expected_review_identity_sha256
    ):
        raise ResolutionError("review resolution boundary identity changed")
    rows = raw.get("resolutions")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_RESOLUTIONS:
        raise ResolutionError("review resolutions must be a bounded non-empty list")
    items: list[FindingResolution] = []
    for index, item in enumerate(rows):
        if not isinstance(item, dict):
            raise ResolutionError(f"resolutions[{index}] must be an object")
        _exact_fields(
            item,
            {"finding_id", "disposition", "rationale", "follow_up"},
            f"resolutions[{index}]",
        )
        disposition = _text(
            item.get("disposition"),
            f"resolutions[{index}].disposition",
            limit=20,
        )
        if disposition not in DISPOSITIONS:
            raise ResolutionError("resolution disposition is not terminal")
        follow_up = _text(
            item.get("follow_up"),
            f"resolutions[{index}].follow_up",
            limit=MAX_FOLLOW_UP_CHARS,
            required=False,
        )
        if disposition == "out-of-scope":
            follow_up = _follow_up(
                follow_up, f"resolutions[{index}].follow_up"
            )
        items.append(
            FindingResolution(
                finding_id=_identifier(
                    item.get("finding_id"),
                    f"resolutions[{index}].finding_id",
                ),
                disposition=disposition,
                rationale=_text(
                    item.get("rationale"),
                    f"resolutions[{index}].rationale",
                    limit=MAX_RATIONALE_CHARS,
                ),
                follow_up=follow_up,
            )
        )
    expected = tuple(expected_finding_ids)
    actual = tuple(item.finding_id for item in items)
    if actual != expected:
        raise ResolutionError(
            "review resolution must cover every material finding in exact order"
        )
    return ReviewResolution(
        operation_id,
        reviewed_head,
        resolved_head,
        tuple(items),
        review_identity_sha256,
    )


def build_resolution_evidence(
    resolution: ReviewResolution,
    *,
    axis: str,
    fix_delta: bytes,
    finding_ids: Sequence[str] | None = None,
) -> ReviewResolutionEvidence:
    if not fix_delta or len(fix_delta) > MAX_FIX_DELTA_CANONICAL_BYTES:
        raise ResolutionError(
            "fix delta must be non-empty and at most 1048576 bytes"
        )
    if (
        len(fix_delta) > MAX_FIX_DELTA_TOTAL_BYTES
        and not resolution.review_identity_sha256
    ):
        raise ResolutionError(
            "large fix delta requires the exact review identity"
        )
    all_items = {item.finding_id: item for item in resolution.resolutions}
    ids = (
        tuple(all_items)
        if finding_ids is None
        else tuple(finding_ids)
    )
    if any(finding_id not in all_items for finding_id in ids):
        raise ResolutionError("axis resolution names an unknown material finding")
    mapping = {finding_id: all_items[finding_id] for finding_id in ids}
    return ReviewResolutionEvidence(
        operation_id=resolution.operation_id,
        axis=axis,
        reviewed_head_sha=resolution.reviewed_head_sha,
        resolved_head_sha=resolution.resolved_head_sha,
        fix_delta_sha256=hashlib.sha256(fix_delta).hexdigest(),
        previous_finding_ids=ids,
        resolutions=mapping,
    )


def validate_resolution_evidence(raw: Any) -> ReviewResolutionEvidence:
    if not isinstance(raw, dict):
        raise ResolutionError("review resolution evidence must be an object")
    _exact_fields(
        raw,
        {
            "schema_version",
            "operation_id",
            "axis",
            "reviewed_head_sha",
            "resolved_head_sha",
            "fix_delta_sha256",
            "previous_finding_ids",
            "resolutions",
        },
        "review resolution evidence",
    )
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ResolutionError("review resolution evidence schema is unsupported")
    rows = raw.get("resolutions")
    previous = raw.get("previous_finding_ids")
    if (
        not isinstance(rows, list)
        or not isinstance(previous, list)
        or len(rows) > MAX_RESOLUTIONS
        or len(previous) > MAX_RESOLUTIONS
    ):
        raise ResolutionError("review resolution evidence findings are invalid")
    items: dict[str, FindingResolution] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ResolutionError(f"resolutions[{index}] must be an object")
        _exact_fields(
            row,
            {"finding_id", "disposition", "rationale", "follow_up"},
            f"resolutions[{index}]",
        )
        disposition = _text(
            row.get("disposition"),
            f"resolutions[{index}].disposition",
            limit=20,
        )
        if disposition not in DISPOSITIONS:
            raise ResolutionError("resolution disposition is not terminal")
        follow_up = _text(
            row.get("follow_up"),
            f"resolutions[{index}].follow_up",
            limit=MAX_FOLLOW_UP_CHARS,
            required=False,
        )
        if disposition == "out-of-scope":
            follow_up = _follow_up(
                follow_up, f"resolutions[{index}].follow_up"
            )
        item = FindingResolution(
            finding_id=_identifier(
                row.get("finding_id"),
                f"resolutions[{index}].finding_id",
            ),
            disposition=disposition,
            rationale=_text(
                row.get("rationale"),
                f"resolutions[{index}].rationale",
                limit=MAX_RATIONALE_CHARS,
            ),
            follow_up=follow_up,
        )
        if item.finding_id in items:
            raise ResolutionError("review resolution evidence findings repeat")
        items[item.finding_id] = item
    previous_ids = tuple(
        _identifier(value, f"previous_finding_ids[{index}]")
        for index, value in enumerate(previous)
    )
    return ReviewResolutionEvidence(
        operation_id=_identifier(raw.get("operation_id"), "operation_id"),
        axis=_text(raw.get("axis"), "axis", limit=64),
        reviewed_head_sha=_head(
            raw.get("reviewed_head_sha"), "reviewed_head_sha"
        ),
        resolved_head_sha=_head(
            raw.get("resolved_head_sha"), "resolved_head_sha"
        ),
        fix_delta_sha256=_text(
            raw.get("fix_delta_sha256"), "fix_delta_sha256", limit=64
        ),
        previous_finding_ids=previous_ids,
        resolutions=items,
    )
