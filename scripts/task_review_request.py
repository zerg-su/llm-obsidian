"""Review routing, prompts, callback paths, and callback envelopes."""

from __future__ import annotations

import hashlib
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Mapping

from harness.contracts import CallbackEnvelope, RuntimeRoute
from harness.review_program import QUESTIONS as REVIEW_QUESTIONS
from harness.review_submit import round_schema_lines
from harness.verification import load_profiles
from harness.workflows.review import (
    ReviewContext,
    ReviewFinding,
    ReviewOperationRequest,
    ReviewResult,
    ReviewRound,
)
from harness.workflows.review_gate import ReviewPreset
from model_routing import load_config, resolve, session_from_meta
from task_review_shared import (
    StaleRoundCallbackError,
    TaskReviewError,
    _atomic_text,
    _read_json,
)


def _route(value: Mapping[str, Any]) -> RuntimeRoute:
    return RuntimeRoute(
        str(value["runtime"]),
        str(value["model"]),
        str(value["effort"]),
        "reviewer-callback",
        str(value["config_sha256"]),
    )


def _request(
    meta: Mapping[str, Any],
    vault: Path,
    task_id: str,
    context: ReviewContext,
) -> tuple[ReviewPreset, ReviewOperationRequest | None]:
    raw = meta["review_policy"]
    preset = ReviewPreset.from_flags(
        deep=raw["mode"] == "deep",
        cross_model=raw["cross_model"],
        runtime=raw["runtime"],
        model=raw["model"],
        effort=raw["effort"],
        no_review=raw["mode"] == "skip",
    )
    if not preset.enabled:
        return preset, None
    review_request = preset.request(
        task_id,
        purpose=str(raw.get("purpose") or "implementation"),
        max_verify_iterations=int(raw["max_verify_iterations"]),
    )
    config = load_config(vault)
    profiles = load_profiles(vault / "config/verification-profiles.toml")
    profile = profiles.get(context.verification_profile)
    if (
        profile is None
        or profile.sha256 != context.verification_profile_sha256
    ):
        raise TaskReviewError("verification profile binding is stale")
    session = session_from_meta(dict(meta))
    if session is None:
        raise TaskReviewError("task has no captured session route")
    selected = resolve(
        config,
        "review",
        session=session,
        explicit_runtime=raw["runtime"],
        explicit_model=raw["model"],
        explicit_effort=raw["effort"],
        same_model=not raw["cross_model"],
        review_profile=preset.depth,
    )
    primary = _route(selected)
    axis_routes: dict[str, RuntimeRoute] | None = None
    if preset.depth == "deep":
        if any((raw["runtime"], raw["model"], raw["effort"])):
            axis_routes = {axis: primary for axis in review_request.axes}
        else:
            axis_routes = {
                "spec": _route(
                    resolve(
                        config,
                        "review",
                        session=session,
                        explicit_runtime="claude",
                        same_model=False,
                        review_profile="deep",
                    )
                ),
                "standards-correctness-architecture-security": _route(
                    resolve(
                        config,
                        "review",
                        session=session,
                        explicit_runtime="codex",
                        same_model=False,
                        review_profile="deep",
                    )
                ),
            }
    return (
        preset,
        ReviewOperationRequest(
            review_request,
            task_id,
            primary,
            context,
            axis_routes=axis_routes,
        ),
    )


def _axis_name(axis: str) -> str:
    return (
        "standards"
        if axis == "standards-correctness-architecture-security"
        else axis
    )


def _callback_path(runtime_root: Path, axis: str) -> Path:
    return (
        runtime_root
        / "callbacks"
        / _axis_name(axis)
        / ".review-callback.json"
    )


def _prompt(
    *,
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    context: ReviewContext,
    axis: str,
    verification: bool,
) -> str:
    name = (
        f"verify-{_axis_name(axis)}.md"
        if verification
        else f"review-{_axis_name(axis)}.md"
    )
    callback_directory = _callback_path(runtime_root, axis).parent
    if callback_directory.exists() and (
        callback_directory.is_symlink()
        or not callback_directory.is_dir()
    ):
        raise TaskReviewError("review callback directory is invalid")
    callback_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    callback_directory.chmod(0o700)
    review_input = callback_directory / ".review-input.json"
    pointer = f"prompts/{name}"
    submit = shlex.join(
        (
            str(Path(sys.executable).resolve()),
            str(vault / "scripts/harness/review_submit.py"),
            "--worktree",
            str(worktree),
            "--state-dir",
            str(_callback_path(runtime_root, axis).parent),
            "--input-file",
            str(review_input),
        )
    )
    outcome_instructions = (
        (
            "Start with the Outcome Contract before implementation mechanics.",
            (
                "Treat the implementer summary and every implementer report as "
                "unverified claims, never as evidence."
            ),
            (
                "Classify every declared success-evidence item as exactly "
                "established, missing, or contradicted from independently "
                "inspected evidence."
            ),
            (
                "Check every declared non-goal for scope creep and emit a "
                "finding when the implementation crosses it."
            ),
            (
                "Do not approve when missing or contradicted outcome evidence, "
                "or observed scope creep, prevents the approved outcome."
            ),
            "A callback, clean diff, or locally green check is not outcome proof.",
            "",
        )
        if context.implementer_summary_sha256
        and axis in {"holistic", "spec"}
        else ()
    )
    _atomic_text(
        runtime_root / pointer,
        "\n".join(
            (
                "# Harness-owned review verification"
                if verification
                else "# Harness-owned review",
                "",
                f"Axis: `{axis}`.",
                f"Purpose: `{context.purpose}`.",
                f"Boundary question: {REVIEW_QUESTIONS[context.purpose]}",
                f"Exact product HEAD: `{context.head_sha}`.",
                f"Product worktree (read-only): `{worktree}`.",
                f"ContextPacket: `{runtime_root / context.manifest}`.",
                "The review standard and approved plan are inside the ContextPacket.",
                "",
                *outcome_instructions,
                "Inspect the exact ContextPacket and product HEAD. Do not edit product files.",
                *(
                    (
                        "This release boundary is approval-or-stop only. Do not "
                        "open or recommend a hidden fix loop inside this review.",
                    )
                    if context.purpose == "release"
                    else ()
                ),
                "Use Read, Glob, and Grep with absolute paths for inspection.",
                (
                    "Use the product's scripts/review-inspect.py facade for every "
                    "Git query; direct Git or shell composition is not permitted."
                ),
                "Do not run cd or copy packet files; they are readable in place.",
                *round_schema_lines(),
                f"Write that exact JSON to `{review_input}`.",
                "Then submit it through this exact command:",
                "",
                f"`{submit}`",
                "",
            )
        ),
    )
    return pointer


def _envelope(path: Path, round_: ReviewRound) -> tuple[CallbackEnvelope, ReviewResult]:
    raw = _read_json(path, "review callback")
    envelope = CallbackEnvelope(
        callback_id=raw.get("callback_id", ""),
        operation_id=raw.get("operation_id", ""),
        run_id=raw.get("run_id", ""),
        kind=raw.get("kind", ""),
        payload=raw.get("payload", {}),
        payload_sha256=raw.get("payload_sha256", ""),
        schema_version=raw.get("schema_version", 0),
    )
    payload = envelope.payload
    if (
        envelope.operation_id != round_.operation_id
        or envelope.run_id != round_.run_id
        or envelope.kind != "review"
        or payload.get("parent_session_operation_id")
        != round_.parent_operation_id
        or payload.get("axis") != round_.axis
        or payload.get("verification_iteration")
        != round_.verification_iteration
    ):
        raise StaleRoundCallbackError(
            "review callback does not match the active round"
        )
    findings = tuple(
        ReviewFinding(
            finding_id=str(item.get("finding_id") or ""),
            axis=round_.axis,
            severity=str(item.get("severity") or ""),
            summary=str(item.get("summary") or ""),
            evidence=str(item.get("evidence") or ""),
            file=str(item.get("file") or ""),
            line=item.get("line"),
            recommendation=str(item.get("recommendation") or ""),
        )
        for item in payload.get("findings", [])
        if isinstance(item, dict)
    )
    if len(findings) != len(payload.get("findings", [])):
        raise TaskReviewError("review callback findings are invalid")
    result = ReviewResult(
        round_.axis,
        str(payload.get("verdict") or ""),
        findings,
        int(payload.get("verification_iteration", -1)),
    )
    return envelope, result


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
