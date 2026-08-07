#!/usr/bin/env python3
"""Versioned, model-independent contract for cross-model review handoffs."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
VERDICTS = {"approve", "changes-requested", "blocked"}
SEVERITIES = frozenset({"critical", "important", "minor"})
MATERIAL_SEVERITIES = SEVERITIES - {"minor"}
MODES = {"simple", "deep", "full"}
PROVIDERS = ("anthropic", "openai")
PROVIDER_RUNTIMES = {"anthropic": "claude", "openai": "codex"}
RUNTIME_PROVIDERS = {
    runtime: provider for provider, runtime in PROVIDER_RUNTIMES.items()
}
VERIFY_BUDGETS = {"simple": 1, "deep": 2, "full": 2}
REVIEW_RESPONSIBILITIES = ("holistic", "intent", "engineering")
REVIEW_PARENT_KIND_BY_RESPONSIBILITY = {
    "holistic": "simple-review-holistic",
    "intent": "deep-review-spec",
    "engineering": "deep-review-correctness",
}
REVIEW_PARENT_KINDS = frozenset(
    REVIEW_PARENT_KIND_BY_RESPONSIBILITY.values()
)
IDENTIFIER_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:-]*"
IDENTIFIER_RE = re.compile(rf"{IDENTIFIER_PATTERN}\Z")
FINDING_ID_LIMIT = 100
FINDING_FILE_LIMIT = 1000
FINDING_SUMMARY_LIMIT = 300
FINDING_DETAIL_LIMIT = 4000


class ReviewContractError(ValueError):
    pass


@dataclass(frozen=True)
class EffectiveReviewLane:
    """One exact provider route bound to one public review responsibility."""

    axis: str
    provider: str
    responsibility: str
    runtime: str
    model: str
    effort: str
    profile: str
    routing_sha256: str

    def payload(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "provider": self.provider,
            "responsibility": self.responsibility,
            "runtime": self.runtime,
            "model": self.model,
            "effort": self.effort,
            "profile": self.profile,
            "routing_sha256": self.routing_sha256,
        }


@dataclass(frozen=True)
class EffectiveReviewTopology:
    """Canonical effect-free review topology shared by every call site."""

    requested_mode: str
    mode: str
    cross_model: bool
    max_verify_iterations: int
    verification_profile: str
    verification_profile_sha256: str
    lanes: tuple[EffectiveReviewLane, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "requested_mode": self.requested_mode,
            "mode": self.mode,
            "cross_model": self.cross_model,
            "max_verify_iterations": self.max_verify_iterations,
            "verification_profile": {
                "name": self.verification_profile,
                "sha256": self.verification_profile_sha256,
            },
            "session_count": len(self.lanes),
            "lanes": [lane.payload() for lane in self.lanes],
        }

    @property
    def sha256(self) -> str:
        raw = json.dumps(
            self.payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(raw).hexdigest()


def _route_value(route: object, field: str) -> object:
    if isinstance(route, Mapping):
        if field == "routing_sha256":
            return route.get(field) or route.get("config_sha256")
        return route.get(field)
    return getattr(route, field, None)


def compile_effective_review_topology(
    *,
    mode: str,
    cross_model: bool,
    max_verify_iterations: int,
    verification_profile: str,
    verification_profile_sha256: str,
    routes: Mapping[str, object],
    selected_provider: str = "",
) -> EffectiveReviewTopology:
    """Compile exact axes and routes once without starting a provider effect."""

    if type(cross_model) is not bool:
        raise ReviewContractError("review cross_model must be boolean")
    if (
        type(max_verify_iterations) is not int
        or mode not in VERIFY_BUDGETS
        or not 0 <= max_verify_iterations <= VERIFY_BUDGETS[mode]
    ):
        raise ReviewContractError("review verification budget is invalid")
    if not isinstance(verification_profile, str) or not IDENTIFIER_RE.fullmatch(
        verification_profile
    ):
        raise ReviewContractError("review verification profile is invalid")
    if not isinstance(verification_profile_sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", verification_profile_sha256
    ) is None:
        raise ReviewContractError(
            "review verification profile digest is invalid"
        )
    providers = set(routes)
    effective_mode = mode
    if selected_provider:
        if providers != {selected_provider}:
            raise ReviewContractError(
                "selected review provider must own the only route"
            )
    elif len(providers) == 1:
        if mode == "full":
            raise ReviewContractError(
                "Full review requires both provider routes"
            )
        selected_provider = next(iter(providers))
    elif providers == set(PROVIDERS):
        if mode == "simple":
            effective_mode = "deep"
    else:
        raise ReviewContractError(
            "review routes must select one provider or both providers"
        )
    try:
        axes = compile_review_axes(
            effective_mode, selected_provider=selected_provider
        )
    except ValueError as exc:
        raise ReviewContractError(str(exc)) from exc
    expected_providers = {review_axis_provider(axis) for axis in axes}
    if set(routes) != expected_providers:
        raise ReviewContractError(
            "review routes must cover every exact topology provider"
        )
    lanes: list[EffectiveReviewLane] = []
    for axis in axes:
        provider = review_axis_provider(axis)
        route = routes[provider]
        runtime = _route_value(route, "runtime")
        model = _route_value(route, "model")
        effort = _route_value(route, "effort")
        profile = _route_value(route, "profile")
        routing_sha256 = _route_value(route, "routing_sha256")
        if (
            not all(
                isinstance(value, str) and value
                for value in (
                    runtime,
                    model,
                    effort,
                    profile,
                    routing_sha256,
                )
            )
            or runtime != review_provider_runtime(provider)
            or IDENTIFIER_RE.fullmatch(model) is None
            or IDENTIFIER_RE.fullmatch(effort) is None
            or profile not in {"reviewer-readonly", "reviewer-callback"}
            or re.fullmatch(r"[0-9a-f]{64}", routing_sha256) is None
        ):
            raise ReviewContractError(
                f"review route for {provider} is invalid"
            )
        lanes.append(
            EffectiveReviewLane(
                axis=axis,
                provider=provider,
                responsibility=review_axis_responsibility(axis),
                runtime=runtime,
                model=model,
                effort=effort,
                profile=profile,
                routing_sha256=routing_sha256,
            )
        )
    return EffectiveReviewTopology(
        requested_mode=mode,
        mode=effective_mode,
        cross_model=cross_model,
        max_verify_iterations=max_verify_iterations,
        verification_profile=verification_profile,
        verification_profile_sha256=verification_profile_sha256,
        lanes=tuple(lanes),
    )


def review_provider_runtime(provider: str) -> str:
    try:
        return PROVIDER_RUNTIMES[provider]
    except KeyError as exc:
        raise ValueError("review provider must be anthropic or openai") from exc


def review_runtime_provider(runtime: str) -> str:
    try:
        return RUNTIME_PROVIDERS[runtime]
    except KeyError as exc:
        raise ValueError("review runtime must be claude or codex") from exc


def review_axis_responsibility(axis: str) -> str:
    """Return the bounded responsibility encoded in one exact lane identity."""

    for responsibility in REVIEW_RESPONSIBILITIES:
        if axis.endswith(f"-{responsibility}") and axis != f"-{responsibility}":
            return responsibility
    raise ValueError("review lane must end in holistic, intent, or engineering")


def review_parent_kind(axis: str) -> str:
    """Map a lane responsibility onto the existing lifecycle vocabulary."""

    return REVIEW_PARENT_KIND_BY_RESPONSIBILITY[
        review_axis_responsibility(axis)
    ]


def review_axis_provider(axis: str) -> str:
    """Return the stable provider encoded in a public lane identity."""

    responsibility = review_axis_responsibility(axis)
    provider = axis[: -(len(responsibility) + 1)]
    if provider not in PROVIDERS:
        raise ValueError("review lane provider must be anthropic or openai")
    return provider


def axis_finding_id(axis: str, finding_id: str) -> str:
    """Return one bounded aggregate identity for a lane-local finding."""

    review_axis_provider(axis)
    if (
        not IDENTIFIER_RE.fullmatch(finding_id)
        or len(finding_id) > FINDING_ID_LIMIT
    ):
        raise ReviewContractError("finding_id must be a bounded identifier")
    prefix = f"{axis}:"
    if finding_id.startswith(prefix):
        return finding_id
    candidate = f"{prefix}{finding_id}"
    if len(candidate) <= FINDING_ID_LIMIT:
        return candidate
    return f"{prefix}{hashlib.sha256(finding_id.encode()).hexdigest()[:32]}"


def compile_review_axes(
    mode: str,
    *,
    selected_provider: str = "",
) -> tuple[str, ...]:
    """Compile the exact ordered reviewer lanes without provider effects."""

    if mode not in MODES:
        raise ValueError("review mode must be simple, deep, or full")
    if selected_provider and selected_provider not in PROVIDERS:
        raise ValueError("selected review provider must be anthropic or openai")
    if mode == "simple":
        if not selected_provider:
            raise ValueError("simple review requires its selected provider")
        return (f"{selected_provider}-holistic",)
    if mode == "deep":
        if selected_provider:
            return (
                f"{selected_provider}-intent",
                f"{selected_provider}-engineering",
            )
        return ("anthropic-holistic", "openai-holistic")
    if selected_provider:
        raise ValueError(
            "Full review requires Anthropic and OpenAI; use Deep for a "
            "single-model intent and engineering review"
        )
    return (
        "anthropic-intent",
        "anthropic-engineering",
        "openai-intent",
        "openai-engineering",
    )


def validate_review_axes(mode: str, axes: Iterable[str]) -> tuple[str, ...]:
    """Validate an exact compiled topology without collapsing lane identity."""

    actual = tuple(axes)
    try:
        for axis in actual:
            review_axis_provider(axis)
        if mode == "simple":
            valid = (
                len(actual) == 1
                and review_axis_responsibility(actual[0]) == "holistic"
            )
        elif mode == "deep":
            valid = actual == compile_review_axes("deep") or (
                len(actual) == 2
                and tuple(review_axis_responsibility(axis) for axis in actual)
                == ("intent", "engineering")
                and len({review_axis_provider(axis) for axis in actual}) == 1
            )
        elif mode == "full":
            valid = actual == compile_review_axes("full")
        else:
            valid = False
    except ValueError:
        valid = False
    if not valid:
        raise ReviewContractError(
            f"{mode} review axes do not match an approved ordered topology"
        )
    return actual


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
        "`finding_id` must not start with the current lane's reserved `<axis>:` "
        "aggregate prefix.",
        "`file` must be a repository-relative POSIX path (not absolute, `.`, or "
        f"containing `..` or `\\`) of at most {FINDING_FILE_LIMIT} characters.",
        f"`summary` is at most {FINDING_SUMMARY_LIMIT} characters; `evidence` and "
        f"`recommendation` are at most {FINDING_DETAIL_LIMIT} characters each.",
        "All finding string fields must be non-empty after trimming surrounding "
        "whitespace.",
    )


def require_unqualified_finding_ids(
    axis: str,
    finding_ids: Iterable[str],
) -> None:
    """Reserve aggregate identities for the harness, never reviewer input."""

    prefix = f"{axis}:"
    if any(finding_id.startswith(prefix) for finding_id in finding_ids):
        raise ReviewContractError(
            f"review finding_id must not use reserved aggregate prefix {prefix}"
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
    if not isinstance(raw_axes, list):
        raise ReviewContractError("review axes must be a list")
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

    actual_axes = validate_review_axes(
        mode, (item["axis"] for item in axes)
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
