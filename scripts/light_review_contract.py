#!/usr/bin/env python3
"""Strict advisory-only contract for one native Light Review result."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, NoReturn, Sequence


class LightReviewError(ValueError):
    pass


SECTIONS = (
    "quality",
    "implementation",
    "testing",
    "simplification",
    "documentation",
    "security",
)
STATUSES = {"no-findings-observed", "findings-observed", "incomplete"}
SECTION_STATUSES = {"clean", "findings", "incomplete"}
SEVERITIES = {"critical", "high", "medium", "low"}
VERIFY_STATUSES = {"passed", "failed", "not-run"}
HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
HEX_32 = re.compile(r"[0-9a-f]{32}\Z")
GIT_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


def _exact(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise LightReviewError(f"{label} fields are not exact")


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LightReviewError(f"{label} must be an object")
    return value


def _text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise LightReviewError(f"{label} must be text")
    if len(value) > 4000 or "\x00" in value:
        raise LightReviewError(f"{label} is not bounded text")
    return value


def _enum(value: object, choices: set[str] | tuple[str, ...], label: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise LightReviewError(f"{label} is invalid")
    return value


def _strings(value: object, label: str, *, limit: int = 100) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise LightReviewError(f"{label} must be a bounded array")
    return [_text(item, f"{label} item") for item in value]


def _relative_path(value: object, label: str, *, allow_empty: bool = False) -> str:
    text = _text(value, label, allow_empty=allow_empty)
    if not text and allow_empty:
        return text
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or text.startswith(":"):
        raise LightReviewError(f"{label} must stay inside the review target")
    return text


def _origin_scope(
    snapshot_value: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    snapshot = _mapping(snapshot_value, "expected Light Review snapshot")
    snapshot_sha256 = str(snapshot.get("snapshot_sha256") or "")
    raw_paths = snapshot.get("paths")
    if (
        not HEX_64.fullmatch(snapshot_sha256)
        or not isinstance(raw_paths, (list, tuple))
        or type(snapshot.get("include_untracked")) is not bool
    ):
        raise LightReviewError("expected Light Review snapshot is invalid")
    paths = [
        _relative_path(item, f"expected Light Review path {index}")
        for index, item in enumerate(raw_paths)
    ]
    return snapshot_sha256, {
        "target_key": str(snapshot.get("target_key") or ""),
        "head": str(snapshot.get("head") or ""),
        "base": str(snapshot.get("base") or ""),
        "paths": paths,
        "included_untracked": snapshot["include_untracked"],
    }


def _finding_is_in_scope(path: str, scope_paths: Sequence[str]) -> bool:
    """Match exact or descendant paths without sibling-prefix ambiguity."""

    if not path or not scope_paths:
        return True
    finding_parts = PurePosixPath(path).parts
    return any(
        finding_parts[: len(scope_parts)] == scope_parts
        for scope_parts in (
            PurePosixPath(scope_path).parts for scope_path in scope_paths
        )
    )


def _verification_incomplete(
    value: object,
    *,
    requested: bool,
    coverage_gaps: list[str],
) -> bool:
    if not isinstance(value, list) or len(value) > 20:
        raise LightReviewError("Light Review verification must be a bounded array")
    if not requested and value:
        raise LightReviewError("Light Review includes unrequested verification")
    statuses: list[str] = []
    for raw in value:
        item = _mapping(raw, "Light Review verification item")
        _exact(item, {"command", "status", "detail"}, "Light Review verification item")
        _text(item["command"], "Light Review verification command")
        _text(item["detail"], "Light Review verification detail", allow_empty=True)
        statuses.append(
            _enum(
                item["status"],
                VERIFY_STATUSES,
                "Light Review verification status",
            )
        )
    if "not-run" in statuses and not coverage_gaps:
        raise LightReviewError(
            "Light Review not-run verification requires a coverage gap"
        )
    return requested and (
        not statuses or any(value != "passed" for value in statuses)
    )


def validate_light_review(
    payload: object,
    *,
    expected_snapshot: Mapping[str, Any],
    expected_route: Mapping[str, Any],
    verification_requested: bool,
) -> dict[str, Any]:
    expected_snapshot_sha256, expected_scope = _origin_scope(expected_snapshot)
    result = _mapping(payload, "Light Review result")
    _exact(
        result,
        {
            "schema_version",
            "kind",
            "snapshot_sha256",
            "status",
            "route",
            "scope",
            "sections",
            "findings",
            "coverage_gaps",
            "verification",
        },
        "Light Review result",
    )
    if (
        type(result["schema_version"]) is not int
        or result["schema_version"] != 1
        or not isinstance(result["kind"], str)
        or result["kind"] != "light-review"
    ):
        raise LightReviewError("Light Review schema identity is invalid")
    snapshot_sha = str(result["snapshot_sha256"])
    if not HEX_64.fullmatch(snapshot_sha) or snapshot_sha != expected_snapshot_sha256:
        raise LightReviewError("Light Review snapshot identity is stale")
    status = _enum(result["status"], STATUSES, "Light Review status")

    route = _mapping(result["route"], "Light Review route")
    _exact(route, {"runtime", "model", "effort", "isolation"}, "Light Review route")
    expected_route_values = {
        "runtime": str(expected_route.get("runtime") or ""),
        "model": str(expected_route.get("model") or ""),
        "effort": str(expected_route.get("effort") or ""),
        "isolation": "native-subagent",
    }
    if dict(route) != expected_route_values:
        raise LightReviewError("Light Review route does not match its native child")

    scope = _mapping(result["scope"], "Light Review scope")
    _exact(
        scope,
        {"target_key", "head", "base", "paths", "included_untracked"},
        "Light Review scope",
    )
    if not HEX_32.fullmatch(str(scope["target_key"])):
        raise LightReviewError("Light Review target identity is invalid")
    if not GIT_OID.fullmatch(str(scope["head"])) or not GIT_OID.fullmatch(str(scope["base"])):
        raise LightReviewError("Light Review Git identity is invalid")
    paths = _strings(scope["paths"], "Light Review paths")
    for index, path in enumerate(paths):
        _relative_path(path, f"Light Review path {index}")
    if not isinstance(scope["included_untracked"], bool):
        raise LightReviewError("Light Review untracked scope must be boolean")
    if dict(scope) != expected_scope:
        raise LightReviewError(
            "Light Review scope does not match its originating snapshot"
        )

    raw_findings = result["findings"]
    if not isinstance(raw_findings, list) or len(raw_findings) > 100:
        raise LightReviewError("Light Review findings must be a bounded array")
    findings_by_section: dict[str, list[str]] = {name: [] for name in SECTIONS}
    finding_ids: set[str] = set()
    for raw in raw_findings:
        finding = _mapping(raw, "Light Review finding")
        _exact(
            finding,
            {"id", "section", "severity", "path", "line", "summary", "evidence", "recommendation"},
            "Light Review finding",
        )
        finding_id = _text(finding["id"], "Light Review finding id")
        section = _enum(
            finding["section"],
            SECTIONS,
            "Light Review finding section",
        )
        _enum(
            finding["severity"],
            SEVERITIES,
            "Light Review finding severity",
        )
        if finding_id in finding_ids or not re.fullmatch(r"L-[0-9]{3}", finding_id):
            raise LightReviewError("Light Review finding identity is invalid")
        finding_path = _relative_path(
            finding["path"], "Light Review finding path", allow_empty=True
        )
        if not _finding_is_in_scope(finding_path, paths):
            raise LightReviewError(
                "Light Review finding path is outside its requested scope"
            )
        if finding["line"] is not None and (
            type(finding["line"]) is not int or finding["line"] < 1
        ):
            raise LightReviewError("Light Review finding line is invalid")
        for field in ("summary", "evidence", "recommendation"):
            _text(finding[field], f"Light Review finding {field}")
        finding_ids.add(finding_id)
        findings_by_section[section].append(finding_id)

    raw_sections = result["sections"]
    if not isinstance(raw_sections, list) or len(raw_sections) != len(SECTIONS):
        raise LightReviewError("Light Review must report all six sections")
    section_incomplete = False
    for expected_name, raw in zip(SECTIONS, raw_sections, strict=True):
        section = _mapping(raw, "Light Review section")
        _exact(section, {"name", "status", "finding_ids"}, "Light Review section")
        name = _enum(section["name"], SECTIONS, "Light Review section name")
        section_status = _enum(
            section["status"],
            SECTION_STATUSES,
            "Light Review section status",
        )
        if name != expected_name:
            raise LightReviewError("Light Review section order or status is invalid")
        ids = _strings(section["finding_ids"], "Light Review section finding IDs")
        if ids != findings_by_section[expected_name]:
            raise LightReviewError("Light Review finding IDs are not section-owned")
        if section_status == "clean" and ids or section_status == "findings" and not ids:
            raise LightReviewError("Light Review section status contradicts its findings")
        section_incomplete = section_incomplete or section_status == "incomplete"

    gaps = _strings(result["coverage_gaps"], "Light Review coverage gaps")
    verification_incomplete = _verification_incomplete(
        result["verification"],
        requested=verification_requested,
        coverage_gaps=gaps,
    )
    expected_status = (
        "incomplete"
        if gaps or section_incomplete or verification_incomplete
        else "findings-observed"
        if finding_ids
        else "no-findings-observed"
    )
    if status != expected_status:
        raise LightReviewError("Light Review status contradicts its evidence")
    return dict(result)


def die(message: str) -> NoReturn:
    print(f"light-review-contract: {message}", file=sys.stderr)
    raise SystemExit(3)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-file", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    args = parser.parse_args(argv)
    try:
        if args.snapshot_file.is_symlink() or not args.snapshot_file.is_file():
            raise LightReviewError("expected Light Review snapshot file is invalid")
        expected_snapshot = json.loads(
            args.snapshot_file.read_text(encoding="utf-8")
        )
        payload = json.load(sys.stdin)
        validated = validate_light_review(
            payload,
            expected_snapshot=expected_snapshot,
            expected_route={
                "runtime": args.runtime,
                "model": args.model,
                "effort": args.effort,
            },
            verification_requested=args.verify,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, LightReviewError) as exc:
        die(str(exc))
    print(json.dumps(validated, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
