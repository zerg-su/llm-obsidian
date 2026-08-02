#!/usr/bin/env python3
"""Drive the automatic review gate for one exact v3/v4 dispatch worktree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Sequence

from harness.context import ContextBuilder, ContextInput, outcome_contract_input
from harness.contracts import (
    AttentionReason,
    CallbackEnvelope,
    ContractError as HarnessContractError,
    EffectOutcome,
    OwnedResources,
    RuntimeRoute,
    to_dict,
)
from harness.custom_pipelines import (
    CustomPipelinePolicy,
    resolve_custom_executable,
)
from harness.pipeline_builtins import builtin_registry
from harness.runtime_worker import _pipeline_verify_identity
from harness.review_submit import round_schema_lines
from harness.runtime_sessions import RuntimeSessionManager
from harness.state_machine import TERMINAL
from harness.store import OperationStore, StoreError
from harness.verification import (
    VerificationError,
    compose_commands,
    load_profiles,
)
from harness.workflows.review import (
    ReviewContext,
    ReviewFinding,
    ReviewOperationRequest,
    ReviewResult,
    ReviewRound,
    review_round_envelope,
    review_session_specs,
    runtime_status_is_live,
)
from harness.workflows.review_gate import (
    ReviewGateController,
    ReviewGateRun,
    ReviewPreset,
    ReviewScopeBoundary,
    review_context_sha256,
)
from model_routing import (
    load_config,
    resolve,
    routing_from_environment,
    session_from_meta,
)
from review_resolution import (
    MATERIAL_SEVERITIES,
    MAX_FIX_DELTA_BYTES,
    ResolutionError,
    ReviewResolution,
    ReviewResolutionEvidence,
    build_resolution_evidence,
    review_transport_identity_sha256,
    validate_resolution,
    validate_resolution_evidence,
)
from review_telemetry import emit_review_event
from task_contract import normalize


class TaskReviewError(ValueError):
    pass


class StaleRoundCallbackError(TaskReviewError):
    """A callback belonging to another round or verification iteration.

    This is a transport rejection, not a claim that the versioned payload
    schema itself was invalid.
    """


class ActiveReviewRound(NamedTuple):
    run: ReviewGateRun
    lane: object
    round: ReviewRound


class ResolutionBundle(NamedTuple):
    resolution: ReviewResolution
    fix_delta: bytes
    by_axis: Mapping[str, ReviewResolutionEvidence]
    review_identity_sha256: str


class FinalizingRecovery(NamedTuple):
    context: ReviewContext
    context_manifest: Path
    marker_pointer: str
    marker_sha256: str
    response_receipt_path: Path
    response_receipt: Mapping[str, object]
    result: ReviewResult


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(value, encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_bytes(value)
        tmp.chmod(0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_json(path: Path, value: object) -> None:
    _atomic_text(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskReviewError(f"{label} is unavailable") from exc
    if not isinstance(value, dict):
        raise TaskReviewError(f"{label} must be an object")
    return value


def _git(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise TaskReviewError("cannot resolve the exact product revision")
    return result.stdout.strip()


def _git_bytes(worktree: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise TaskReviewError("cannot build the exact review fix delta")
    return result.stdout


def _resolution_bundle(
    worktree: Path,
    gate_root: Path,
    task_id: str,
    awaiting: Mapping[str, object],
    resolved_head: str,
) -> ResolutionBundle:
    reviewed_heads: set[str] = set()
    finding_ids_by_axis: dict[str, tuple[str, ...]] = {}
    all_finding_ids: list[str] = []
    review_operation_ids: set[str] = set()
    review_callbacks: list[dict[str, object]] = []
    for axis in sorted(awaiting):
        raw_boundary = awaiting[axis]
        if not isinstance(raw_boundary, dict):
            raise TaskReviewError("review resolution boundary is invalid")
        pointer = Path(str(raw_boundary.get("pointer") or ""))
        result_path = (gate_root / pointer).resolve()
        if (
            pointer.is_absolute()
            or gate_root not in result_path.parents
            or not result_path.is_file()
            or result_path.is_symlink()
        ):
            raise TaskReviewError("review finding evidence is unavailable")
        result = _read_json(result_path, "review finding evidence")
        findings = result.get("findings")
        if result.get("axis") != axis or not isinstance(findings, list):
            raise TaskReviewError("review finding evidence is invalid")
        material = tuple(
            str(finding.get("finding_id") or "")
            for finding in findings
            if isinstance(finding, dict)
            and finding.get("severity") in MATERIAL_SEVERITIES
        )
        if "" in material:
            raise TaskReviewError("material finding identity is invalid")
        finding_ids_by_axis[str(axis)] = material
        all_finding_ids.extend(material)
        reviewed_heads.add(str(raw_boundary.get("reviewed_head_sha") or ""))
        review_operation_ids.add(
            str(raw_boundary.get("review_operation_id") or "")
        )
        review_callbacks.append(
            {
                "axis": axis,
                "round_operation_id": str(
                    raw_boundary.get("round_operation_id") or ""
                ),
                "round_run_id": str(
                    raw_boundary.get("round_run_id") or ""
                ),
                "callback_id": str(
                    raw_boundary.get("callback_id") or ""
                ),
                "callback_sha256": str(
                    raw_boundary.get("callback_sha256") or ""
                ),
            }
        )
    if len(all_finding_ids) != len(set(all_finding_ids)):
        raise TaskReviewError("material finding identities repeat across axes")
    if len(reviewed_heads) != 1 or "" in reviewed_heads:
        raise TaskReviewError("review resolution heads are inconsistent")
    if len(review_operation_ids) != 1 or "" in review_operation_ids:
        raise TaskReviewError("review resolution operation is inconsistent")
    try:
        review_identity_sha256 = review_transport_identity_sha256(
            next(iter(review_operation_ids)), review_callbacks
        )
    except ResolutionError as exc:
        raise TaskReviewError(
            f"review resolution boundary identity is invalid: {exc}"
        ) from exc
    reviewed_head = next(iter(reviewed_heads))
    resolution_path = worktree / ".task-review-resolution.json"
    if not resolution_path.is_file() or resolution_path.is_symlink():
        raise TaskReviewError("review resolution evidence is unavailable")
    raw_resolution = _read_json(resolution_path, "review resolution evidence")
    try:
        resolution = validate_resolution(
            raw_resolution,
            expected_operation_id=task_id,
            expected_reviewed_head_sha=reviewed_head,
            expected_resolved_head_sha=resolved_head,
            expected_finding_ids=tuple(all_finding_ids),
            expected_review_identity_sha256=review_identity_sha256,
        )
    except ResolutionError as exc:
        raise TaskReviewError(f"review resolution evidence is invalid: {exc}") from exc
    for item in resolution.resolutions:
        if (
            item.disposition == "out-of-scope"
            and not item.follow_up.startswith("https://")
            and not item.follow_up.startswith("[[")
            and _git(
                worktree,
                "cat-file",
                "-t",
                f"{resolved_head}:{item.follow_up}",
            )
            != "blob"
        ):
            raise TaskReviewError(
                "repository follow-up must be a file on the resolved HEAD"
            )
    fix_delta = _git_bytes(
        worktree,
        "diff",
        "--binary",
        "--no-ext-diff",
        reviewed_head,
        resolved_head,
        "--",
    )
    if not fix_delta or len(fix_delta) > MAX_FIX_DELTA_BYTES:
        raise TaskReviewError(
            "review fix delta must be non-empty and at most 65536 bytes"
        )
    try:
        by_axis = {
            axis: build_resolution_evidence(
                resolution,
                axis=axis,
                fix_delta=fix_delta,
                finding_ids=finding_ids,
            )
            for axis, finding_ids in finding_ids_by_axis.items()
        }
    except ResolutionError as exc:
        raise TaskReviewError(f"review resolution evidence is invalid: {exc}") from exc
    return ResolutionBundle(
        resolution,
        fix_delta,
        by_axis,
        review_identity_sha256,
    )


def _recovery_resolution_bundle(
    worktree: Path,
    task_id: str,
    persisted: ReviewResolutionEvidence,
    resolved_head: str,
    review_identity_sha256: str = "",
) -> ResolutionBundle:
    """Rebuild recovery evidence from the durable reviewer-seen finding set."""

    resolution_path = worktree / ".task-review-resolution.json"
    if not resolution_path.is_file() or resolution_path.is_symlink():
        raise TaskReviewError("review resolution evidence is unavailable")
    try:
        resolution = validate_resolution(
            _read_json(resolution_path, "review resolution evidence"),
            expected_operation_id=task_id,
            expected_reviewed_head_sha=persisted.reviewed_head_sha,
            expected_resolved_head_sha=resolved_head,
            expected_finding_ids=persisted.previous_finding_ids,
            expected_review_identity_sha256=review_identity_sha256,
        )
    except ResolutionError as exc:
        raise TaskReviewError(
            f"review resolution evidence is invalid: {exc}"
        ) from exc
    for item in resolution.resolutions:
        if (
            item.disposition == "out-of-scope"
            and not item.follow_up.startswith("https://")
            and not item.follow_up.startswith("[[")
            and _git(
                worktree,
                "cat-file",
                "-t",
                f"{resolved_head}:{item.follow_up}",
            )
            != "blob"
        ):
            raise TaskReviewError(
                "repository follow-up must be a file on the resolved HEAD"
            )
    fix_delta = _git_bytes(
        worktree,
        "diff",
        "--binary",
        "--no-ext-diff",
        persisted.reviewed_head_sha,
        resolved_head,
        "--",
    )
    if not fix_delta or len(fix_delta) > MAX_FIX_DELTA_BYTES:
        raise TaskReviewError(
            "review fix delta must be non-empty and at most 65536 bytes"
        )
    try:
        evidence = build_resolution_evidence(
            resolution,
            axis=persisted.axis,
            fix_delta=fix_delta,
            finding_ids=persisted.previous_finding_ids,
        )
    except ResolutionError as exc:
        raise TaskReviewError(
            f"review resolution evidence is invalid: {exc}"
        ) from exc
    return ResolutionBundle(
        resolution,
        fix_delta,
        {persisted.axis: evidence},
        resolution.review_identity_sha256,
    )


def _bounded_input(
    name: str,
    source: Path,
    *,
    role: str,
    pointer_root: Path,
) -> ContextInput:
    raw = source.read_bytes()
    if len(raw) <= 65_536:
        return ContextInput(name, str(source), raw, role=role)
    pointer = pointer_root / name
    _atomic_bytes(pointer, raw)
    return ContextInput.pointer(
        name,
        str(pointer),
        byte_count=len(raw),
        content_sha256=hashlib.sha256(raw).hexdigest(),
        role=role,
    )


def _validate_task(worktree: Path) -> tuple[dict[str, Any], Path, str]:
    worktree = worktree.expanduser().resolve()
    if not worktree.is_dir():
        raise TaskReviewError("task worktree is unavailable")
    meta = _read_json(worktree / ".task-meta.json", "task metadata")
    version = meta.get("version")
    if version not in {3, 4}:
        raise TaskReviewError("automatic review requires v3 or v4 task metadata")
    normalize(meta)
    try:
        task_id = str(uuid.UUID(str(meta.get("task_id") or "")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise TaskReviewError("task identity is invalid") from exc
    if task_id != meta.get("task_id"):
        raise TaskReviewError("task identity must be canonical")
    declared = Path(str(meta.get("worktree") or "")).expanduser()
    if not declared.is_absolute() or declared.resolve() != worktree:
        raise TaskReviewError("task metadata identifies another worktree")
    vault = Path(str(meta.get("vault_root") or "")).expanduser()
    if (
        not vault.is_absolute()
        or not (vault.resolve() / "wiki").is_dir()
        or not (vault.resolve() / "scripts").is_dir()
    ):
        raise TaskReviewError("coordinator vault is unavailable")
    vault = vault.resolve()
    if vault == worktree:
        raise TaskReviewError(
            "coordinator vault and generic product worktree must be separate"
        )
    policy = meta.get("review_policy")
    if not isinstance(policy, dict):
        raise TaskReviewError("review policy is unavailable")
    required = {
        "mode",
        "cross_model",
        "runtime",
        "model",
        "effort",
        "max_verify_iterations",
        "verification_profile",
        "verification_profile_sha256",
    }
    if version == 3:
        required.update(
            {"auto_resolve_severities", "escalate_severities"}
        )
    if set(policy) != required:
        raise TaskReviewError(f"v{version} review policy fields are not exact")
    mode = str(policy.get("mode") or "")
    budget = policy.get("max_verify_iterations")
    if (
        mode not in {"simple", "deep", "skip"}
        or budget != {"simple": 1, "deep": 2, "skip": 0}[mode]
        or not isinstance(policy.get("cross_model"), bool)
        or not all(
            isinstance(policy.get(field), str)
            for field in (
                "runtime",
                "model",
                "effort",
                "verification_profile",
                "verification_profile_sha256",
            )
        )
    ):
        raise TaskReviewError(f"v{version} review policy values are invalid")
    if mode == "skip" and any(
        (
            policy["cross_model"],
            policy["runtime"],
            policy["model"],
            policy["effort"],
        )
    ):
        raise TaskReviewError("typed review skip cannot carry overrides")
    return meta, vault, task_id


def _runtime_root(vault: Path, task_id: str) -> Path:
    root = (
        vault
        / ".vault-meta"
        / "harness"
        / "review-runtime"
        / task_id
    ).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


def _current_runtime_root(
    worktree: Path,
    task_id: str,
    scratch_root: Path | None,
) -> Path:
    if scratch_root is None:
        checkout_key = hashlib.sha256(
            str(worktree).encode("utf-8")
        ).hexdigest()[:16]
        base = (
            Path(tempfile.gettempdir())
            / "llm-obsidian-current-review"
            / checkout_key
        )
    else:
        base = scratch_root.expanduser().resolve()
    root = (base / task_id).resolve()
    if root == worktree or worktree in root.parents:
        raise TaskReviewError(
            "current review scratch must stay outside the product checkout"
        )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


def _gate_root(vault: Path, task_id: str) -> Path:
    return (
        vault
        / ".vault-meta"
        / "harness"
        / "review-data"
        / task_id
        / task_id
    ).resolve()


def _current_review_is_quiescent(vault: Path, task_id: str) -> bool:
    """Permit a fresh current review only after exact old ownership is gone."""

    rows = OperationStore(vault / ".vault-meta" / "harness").list(task_id)
    return bool(rows) and all(
        row.state in TERMINAL
        and not row.resources.surface_id
        and row.resources.process_group == 0
        and row.resources.supervisor_pid == 0
        for row in rows
    )


def _context(
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    task_id: str,
    resolution_bundle: ResolutionBundle | None = None,
) -> tuple[ReviewContext, Path]:
    head = _git(worktree, "rev-parse", "HEAD")
    plan = Path(str(meta["plan_file"])).expanduser().resolve()
    inputs = [
        _bounded_input(
            "approved-plan.md",
            plan,
            role="plan",
            pointer_root=runtime_root / "pointers",
        ),
        _bounded_input(
            "review-skill.md",
            vault / "skills/review/SKILL.md",
            role="instructions",
            pointer_root=runtime_root / "pointers",
        ),
        ContextInput(
            (
                "current-review.json"
                if meta.get("lifecycle") == "current-checkout"
                else "task-meta.json"
            ),
            (
                str(runtime_root / "current-review.json")
                if meta.get("lifecycle") == "current-checkout"
                else str(worktree / ".task-meta.json")
            ),
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True) + "\n"
            ).encode(),
            role="task",
        ),
        ContextInput(
            "exact-head.txt",
            "git:HEAD",
            (head + "\n").encode(),
            role="head",
        ),
    ]
    implementer_summary_sha256 = ""
    if meta.get("version") == 4 and meta.get("lifecycle") != "current-checkout":
        summary_path = worktree / ".task-summary.json"
        if not summary_path.is_file() or summary_path.is_symlink():
            raise TaskReviewError(
                "v4 review requires the exact implementer summary"
            )
        summary_input = _bounded_input(
            "implementer-summary.json",
            summary_path,
            role="task",
            pointer_root=runtime_root / "pointers",
        )
        inputs.append(summary_input)
        implementer_summary_sha256 = summary_input.content_sha256
        inputs.append(
            outcome_contract_input(
                plan,
                expected_sha256=str(meta.get("outcome_contract_sha256") or ""),
            )
        )
    instructions = worktree / "AGENTS.md"
    if instructions.is_file() and not instructions.is_symlink():
        inputs.append(
            _bounded_input(
                "product-agents.md",
                instructions,
                role="instructions",
                pointer_root=runtime_root / "pointers",
            )
        )
    diff = _git(
        worktree,
        "show",
        "--format=fuller",
        "--stat",
        "--patch",
        "--find-renames",
        "HEAD",
    ).encode()
    if len(diff) > 65_536:
        diff = diff[:65_000] + b"\n[diff truncated; inspect product HEAD]\n"
    inputs.append(
        ContextInput("head-diff.patch", "git:show:HEAD", diff, role="diff")
    )
    if resolution_bundle is not None:
        resolution_payload = {
            "schema_version": 1,
            "operation_id": resolution_bundle.resolution.operation_id,
            "reviewed_head_sha": (
                resolution_bundle.resolution.reviewed_head_sha
            ),
            "resolved_head_sha": (
                resolution_bundle.resolution.resolved_head_sha
            ),
            "previous_finding_ids": [
                item.finding_id
                for item in resolution_bundle.resolution.resolutions
            ],
            "resolutions": [
                item.payload()
                for item in resolution_bundle.resolution.resolutions
            ],
            "fix_delta_sha256": hashlib.sha256(
                resolution_bundle.fix_delta
            ).hexdigest(),
        }
        inputs.extend(
            (
                ContextInput(
                    "resolution-evidence.json",
                    str(worktree / ".task-review-resolution.json"),
                    (
                        json.dumps(
                            resolution_payload,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode(),
                    role="resolution",
                ),
                ContextInput(
                    "fix-delta.patch",
                    (
                        "git:diff:"
                        f"{resolution_bundle.resolution.reviewed_head_sha}.."
                        f"{resolution_bundle.resolution.resolved_head_sha}"
                    ),
                    resolution_bundle.fix_delta,
                    role="fix",
                ),
            )
        )
    builder = ContextBuilder(runtime_root / "packets")
    manifest = builder.build(
        task_id,
        tuple(inputs),
        metadata={
            "task_id": task_id,
            "task_name": str(meta.get("task_name") or ""),
            "head_sha": head,
        },
    )
    manifest_path = (
        runtime_root
        / "packets"
        / manifest.packet_id
        / "manifest.json"
    )
    policy = meta["review_policy"]
    return (
        ReviewContext(
            manifest_path.relative_to(runtime_root).as_posix(),
            head,
            str(policy["verification_profile"]),
            str(policy["verification_profile_sha256"]),
            implementer_summary_sha256,
        ),
        manifest_path,
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
            axis_routes = {axis: primary for axis in preset.request(task_id).axes}
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
            preset.request(task_id),
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


def _write_round_meta(
    *,
    runtime_root: Path,
    vault: Path,
    worktree: Path,
    task_id: str,
    depth: str,
    context: ReviewContext,
    lane_operation_id: str,
    round_: ReviewRound,
) -> None:
    directory = _callback_path(runtime_root, round_.axis).parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    started_at = _round_telemetry_state(runtime_root, round_)["started_at"]
    _atomic_json(
        directory / ".review-meta.json",
        {
            "schema_version": 1,
            "transport": "review-round",
            "operation_id": round_.operation_id,
            "run_id": round_.run_id,
            "review_id": task_id,
            "parent_session_operation_id": lane_operation_id,
            "review_mode": depth,
            "axis": round_.axis,
            "verification_iteration": round_.verification_iteration,
            "started_at": started_at,
            "worktree": str(worktree),
            "task_name": task_id,
            "head_sha": context.head_sha,
            "verification_profile": {
                "name": context.verification_profile,
                "sha256": context.verification_profile_sha256,
            },
        },
    )
    _emit_round_telemetry(
        worktree,
        vault,
        runtime_root,
        round_,
        event="review-round-start",
        terminal_status="started",
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
                f"Exact product HEAD: `{context.head_sha}`.",
                f"Product worktree (read-only): `{worktree}`.",
                f"ContextPacket: `{runtime_root / context.manifest}`.",
                "The review standard and approved plan are inside the ContextPacket.",
                "",
                *outcome_instructions,
                "Inspect the exact ContextPacket and product HEAD. Do not edit product files.",
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


def _durable_verification_resubmit(
    meta: Mapping[str, Any],
    worktree: Path,
    store: OperationStore,
    task_id: str,
    previous_head: str,
    current_head: str,
) -> tuple[Path, dict[str, object], str]:
    """Bind one worktree resubmit to its exact durable failed verification."""

    packet_path = worktree / ".task-verification.json"
    response_path = worktree / ".task-verification-response.json"
    for path, label in (
        (packet_path, "verification attention packet"),
        (response_path, "verification resubmission response"),
    ):
        if not path.is_file() or path.is_symlink():
            raise TaskReviewError(
                f"finalizing review recovery {label} is unavailable"
            )
    packet = _read_json(packet_path, "verification attention packet")
    response = _read_json(
        response_path, "verification resubmission response"
    )
    operation_id = str(packet.get("verification_operation_id") or "")
    owner_runtime = (
        store.root
        / "owners"
        / task_id
        / "runtime"
        / task_id
    ).resolve()
    receipt_path = (
        owner_runtime
        / "pipeline-verification"
        / operation_id
        / "receipt.json"
    ).resolve()
    if (
        not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", operation_id
        )
        or not receipt_path.is_file()
        or receipt_path.is_symlink()
    ):
        raise TaskReviewError(
            "finalizing review recovery verification receipt is unavailable"
        )
    receipt = _read_json(receipt_path, "verification receipt")
    receipt_fields = {
        "schema_version",
        "operation_id",
        "parent_operation_id",
        "lane_id",
        "run_id",
        "definition_sha256",
        "step_id",
        "head_sha",
        "input_sha256",
        "profile",
        "profile_sha256",
        "effect_id",
        "status",
        "evidence",
    }
    policy = meta.get("pipeline_policy")
    review_policy = meta.get("review_policy")
    evidence = receipt.get("evidence")
    input_sha256 = str(receipt.get("input_sha256") or "")
    profile = str(receipt.get("profile") or "")
    if (
        set(receipt) != receipt_fields
        or receipt.get("schema_version") != 1
        or receipt.get("operation_id") != operation_id
        or receipt.get("parent_operation_id") != task_id
        or not isinstance(policy, Mapping)
        or receipt.get("definition_sha256")
        != policy.get("definition_sha256")
        or receipt.get("step_id") != "verify"
        or receipt.get("head_sha") != previous_head
        or not re.fullmatch(r"[0-9a-f]{64}", input_sha256)
        or not isinstance(review_policy, Mapping)
        or profile != review_policy.get("verification_profile")
        or receipt.get("profile_sha256")
        != review_policy.get("verification_profile_sha256")
        or receipt.get("effect_id")
        != f"pipeline-verify-{input_sha256[:32]}"
        or receipt.get("status") != "failed"
        or not isinstance(evidence, list)
        or not evidence
        or len(evidence) > 100
    ):
        raise TaskReviewError(
            "finalizing review recovery verification receipt is invalid"
        )
    try:
        parent = store.read(task_id, task_id)
        expected_spec, expected_lane, expected_run = (
            _pipeline_verify_identity(
                parent.spec,
                definition_sha256=str(receipt["definition_sha256"]),
                input_sha256=input_sha256,
                profile=profile,
            )
        )
        child = store.read(task_id, operation_id)
    except (StoreError, ValueError) as exc:
        raise TaskReviewError(
            "finalizing review recovery verification operation is unavailable"
        ) from exc
    if (
        expected_spec.operation_id != operation_id
        or receipt.get("lane_id") != expected_lane
        or receipt.get("run_id") != expected_run
        or child.spec != expected_spec
        or child.lane_id != expected_lane
        or child.run_id != expected_run
        or child.state not in {"attention-required", "failed"}
        or child.resources != OwnedResources()
        or child.pending_effect
        or child.effect_id != receipt.get("effect_id")
        or child.effect_outcome != EffectOutcome.SUCCEEDED
    ):
        raise TaskReviewError(
            "finalizing review recovery verification operation changed"
        )
    packet_evidence: list[dict[str, object]] = []
    for row in evidence:
        if (
            not isinstance(row, dict)
            or set(row)
            != {
                "command_id",
                "cwd",
                "exit_code",
                "started_at",
                "finished_at",
                "head_sha",
                "profile",
                "profile_sha256",
                "output_pointer",
            }
            or not isinstance(row.get("command_id"), str)
            or not row["command_id"]
            or type(row.get("exit_code")) is not int
            or row.get("head_sha") != previous_head
            or row.get("profile") != profile
            or row.get("profile_sha256")
            != receipt.get("profile_sha256")
        ):
            raise TaskReviewError(
                "finalizing review recovery verification evidence is invalid"
            )
        pointer = Path(str(row.get("output_pointer") or ""))
        output = (owner_runtime / pointer).resolve()
        evidence_root = (owner_runtime / "pipeline-verification").resolve()
        if (
            pointer.is_absolute()
            or evidence_root not in output.parents
            or not output.is_file()
            or output.is_symlink()
        ):
            raise TaskReviewError(
                "finalizing review recovery verification evidence is unavailable"
            )
        packet_evidence.append(
            {
                "command_id": row["command_id"],
                "exit_code": row["exit_code"],
                "output_pointer": str(output),
            }
        )
    expected_packet = {
        "schema_version": 1,
        "operation_id": task_id,
        "verification_operation_id": operation_id,
        "verification_lane_id": expected_lane,
        "verification_run_id": expected_run,
        "definition_sha256": receipt["definition_sha256"],
        "step_id": "verify",
        "head_sha": previous_head,
        "status": "attention-required",
        "reason": "verification-failed",
        "safe_boundary": "tdd-slices-complete",
        "allowed_responses": ["fix-and-resubmit", "escalate"],
        "response_pointer": ".task-verification-response.json",
        "receipt_pointer": str(receipt_path),
        "evidence": packet_evidence,
    }
    packet_sha256 = _canonical_sha256(expected_packet)
    expected_response = {
        "schema_version": 1,
        "operation_id": task_id,
        "verification_operation_id": operation_id,
        "failed_head_sha": previous_head,
        "packet_sha256": packet_sha256,
        "response": "fix-and-resubmit",
        "resubmitted_head_sha": current_head,
    }
    if packet != expected_packet or response != expected_response:
        raise TaskReviewError(
            "finalizing review recovery verification transport changed"
        )
    response_receipt = {
        "schema_version": 1,
        "operation_id": task_id,
        "verification_operation_id": operation_id,
        "failed_head_sha": previous_head,
        "resubmitted_head_sha": current_head,
        "response_sha256": _canonical_sha256(expected_response),
        "status": "accepted",
    }
    response_receipt_path = receipt_path.with_name("response-receipt.json")
    if response_receipt_path.exists() and (
        not response_receipt_path.is_file()
        or response_receipt_path.is_symlink()
        or _read_json(
            response_receipt_path, "verification response receipt"
        )
        != response_receipt
    ):
        raise TaskReviewError(
            "finalizing review recovery response receipt changed"
        )
    return response_receipt_path, response_receipt, hashlib.sha256(
        receipt_path.read_bytes()
    ).hexdigest()


def _durable_successful_verification(
    meta: Mapping[str, Any],
    vault: Path,
    store: OperationStore,
    task_id: str,
    current_head: str,
) -> tuple[str, str] | None:
    """Prove the coordinator reran the exact configured profile at HEAD."""

    owner_runtime = (
        store.root
        / "owners"
        / task_id
        / "runtime"
        / task_id
    ).resolve()
    linked_path = owner_runtime / "pipeline-step-verify.json"
    if not linked_path.exists():
        return None
    if not linked_path.is_file() or linked_path.is_symlink():
        raise TaskReviewError(
            "successful review recovery verification link is invalid"
        )
    receipt = _read_json(linked_path, "successful verification receipt")
    operation_id = str(receipt.get("operation_id") or "")
    receipt_path = (
        owner_runtime
        / "pipeline-verification"
        / operation_id
        / "receipt.json"
    ).resolve()
    if (
        not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", operation_id
        )
        or not receipt_path.is_file()
        or receipt_path.is_symlink()
        or _read_json(receipt_path, "successful verification receipt")
        != receipt
    ):
        return None
    policy = meta.get("pipeline_policy")
    review_policy = meta.get("review_policy")
    evidence = receipt.get("evidence")
    input_sha256 = str(receipt.get("input_sha256") or "")
    profile = str(receipt.get("profile") or "")
    configured_profile = load_profiles(
        vault / "config/verification-profiles.toml"
    ).get(profile)
    if (
        set(receipt)
        != {
            "schema_version",
            "operation_id",
            "parent_operation_id",
            "lane_id",
            "run_id",
            "definition_sha256",
            "step_id",
            "head_sha",
            "input_sha256",
            "profile",
            "profile_sha256",
            "effect_id",
            "status",
            "evidence",
        }
        or receipt.get("schema_version") != 1
        or receipt.get("parent_operation_id") != task_id
        or not isinstance(policy, Mapping)
        or receipt.get("definition_sha256")
        != policy.get("definition_sha256")
        or receipt.get("step_id") != "verify"
        or receipt.get("head_sha") != current_head
        or not re.fullmatch(r"[0-9a-f]{64}", input_sha256)
        or not isinstance(review_policy, Mapping)
        or configured_profile is None
        or profile != review_policy.get("verification_profile")
        or receipt.get("profile_sha256")
        != review_policy.get("verification_profile_sha256")
        or configured_profile.sha256 != receipt.get("profile_sha256")
        or receipt.get("effect_id")
        != f"pipeline-verify-{input_sha256[:32]}"
        or receipt.get("status") != "complete"
        or not isinstance(evidence, list)
        or not evidence
        or len(evidence) > 100
    ):
        return None
    try:
        parent = store.read(task_id, task_id)
        expected_spec, expected_lane, expected_run = (
            _pipeline_verify_identity(
                parent.spec,
                definition_sha256=str(receipt["definition_sha256"]),
                input_sha256=input_sha256,
                profile=profile,
            )
        )
        child = store.read(task_id, operation_id)
    except (StoreError, ValueError) as exc:
        raise TaskReviewError(
            "successful review recovery verification operation is unavailable"
        ) from exc
    if (
        expected_spec.operation_id != operation_id
        or receipt.get("lane_id") != expected_lane
        or receipt.get("run_id") != expected_run
        or child.spec != expected_spec
        or child.lane_id != expected_lane
        or child.run_id != expected_run
        or child.state != "complete"
        or child.resources != OwnedResources()
        or child.pending_effect
        or child.effect_id != receipt.get("effect_id")
        or child.effect_outcome != EffectOutcome.SUCCEEDED
    ):
        raise TaskReviewError(
            "successful review recovery verification operation changed"
        )
    evidence_root = (owner_runtime / "pipeline-verification").resolve()
    extra_commands: tuple[str, ...] = ()
    if policy.get("name") == "custom":
        try:
            baseline, compiled, extra_commands, _custom_spec = (
                resolve_custom_executable(
                    store_root=owner_runtime.parent,
                    operation_id=task_id,
                    definition_sha256=str(receipt["definition_sha256"]),
                    registry=builtin_registry(),
                    policy=CustomPipelinePolicy.default(),
                    capabilities=("route:resolved",),
                )
            )
        except (HarnessContractError, OSError, ValueError) as exc:
            raise TaskReviewError(
                "successful review recovery custom pipeline is unavailable"
            ) from exc
        if (
            baseline != policy.get("baseline")
            or compiled.definition_sha256
            != receipt.get("definition_sha256")
        ):
            raise TaskReviewError(
                "successful review recovery custom pipeline changed"
            )
    try:
        commands = compose_commands(configured_profile, extra_commands)
    except VerificationError as exc:
        raise TaskReviewError(
            "successful review recovery verification commands are invalid"
        ) from exc
    expected_command_ids = [
        f"{profile}-{index + 1}"
        for index in range(len(commands))
    ]
    if not all(isinstance(row, dict) for row in evidence):
        raise TaskReviewError(
            "successful review recovery verification evidence is invalid"
        )
    if [str(row.get("command_id") or "") for row in evidence] != (
        expected_command_ids
    ):
        raise TaskReviewError(
            "successful review recovery verification command set is incomplete"
        )
    for row in evidence:
        if (
            not isinstance(row, dict)
            or set(row)
            != {
                "command_id",
                "cwd",
                "exit_code",
                "started_at",
                "finished_at",
                "head_sha",
                "profile",
                "profile_sha256",
                "output_pointer",
            }
            or type(row.get("exit_code")) is not int
            or row.get("exit_code") != 0
            or row.get("head_sha") != current_head
            or row.get("profile") != profile
            or row.get("profile_sha256")
            != receipt.get("profile_sha256")
        ):
            raise TaskReviewError(
                "successful review recovery verification evidence is invalid"
            )
        pointer = Path(str(row.get("output_pointer") or ""))
        output = (owner_runtime / pointer).resolve()
        if (
            pointer.is_absolute()
            or evidence_root not in output.parents
            or not output.is_file()
            or output.is_symlink()
        ):
            raise TaskReviewError(
                "successful review recovery verification evidence is unavailable"
            )
    return operation_id, hashlib.sha256(receipt_path.read_bytes()).hexdigest()


def _finalizing_resubmit_recovery(
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    task_id: str,
    store: OperationStore,
    gate: ReviewGateController,
    run: ReviewGateRun,
    current_context: ReviewContext,
) -> FinalizingRecovery | None:
    """Validate one accepted approval stranded after verification repair."""

    state = gate.read()
    if (
        state.get("status")
        not in {
            "verifying",
            "recovery-verification-required",
            "fresh-boundary-authorized",
        }
        or run.execution.request.policy.depth != "simple"
        or run.execution.request.policy.axes != ("holistic",)
        or len(run.execution.lanes) != 1
    ):
        return None
    lane = run.execution.lanes[0]
    round_ = run.rounds.get("holistic")
    if round_ is None:
        return None
    child = gate.round_store.read(round_.owner_id, round_.operation_id)
    if (
        child.state not in {"finalizing", "complete"}
        or child.accepted_callback_kind != "review"
    ):
        return None
    previous_context = run.execution.request.context
    if previous_context.head_sha == current_context.head_sha:
        return None
    summary_path = worktree / ".task-summary.json"
    resolution_path = worktree / ".task-review-resolution.json"
    callback_path = _callback_path(runtime_root, "holistic")
    for path, label in (
        (summary_path, "task summary"),
        (resolution_path, "review resolution"),
        (callback_path, "accepted review callback"),
    ):
        if not path.is_file() or path.is_symlink():
            raise TaskReviewError(
                f"finalizing review recovery {label} is unavailable"
            )
    callback_raw = _read_json(callback_path, "accepted review callback")
    envelope, result = _envelope(callback_path, round_)
    expected_envelope = to_dict(review_round_envelope(round_, result))
    if (
        result.verdict != "approve"
        or callback_raw != expected_envelope
        or envelope.callback_id != child.accepted_callback_id
        or envelope.kind != child.accepted_callback_kind
        or envelope.payload_sha256 != child.accepted_callback_sha256
    ):
        raise TaskReviewError(
            "finalizing review recovery callback identity changed"
        )
    (
        response_receipt_path,
        response_receipt,
        verification_receipt_sha256,
    ) = _durable_verification_resubmit(
        meta,
        worktree,
        store,
        task_id,
        previous_context.head_sha,
        current_context.head_sha,
    )
    raw_resolution_evidence = state.get("resolution_evidence")
    if (
        not isinstance(raw_resolution_evidence, dict)
        or len(raw_resolution_evidence) != 1
    ):
        raise TaskReviewError(
            "finalizing review recovery resolution boundary is invalid"
        )
    persisted_pointer = Path(
        str(next(iter(raw_resolution_evidence.values())))
    )
    persisted_path = (gate.root / persisted_pointer).resolve()
    if (
        persisted_pointer.is_absolute()
        or gate.root not in persisted_path.parents
        or not persisted_path.is_file()
        or persisted_path.is_symlink()
    ):
        raise TaskReviewError(
            "finalizing review recovery resolution evidence is unavailable"
        )
    try:
        persisted_resolution = validate_resolution_evidence(
            _read_json(persisted_path, "persisted review resolution")
        )
    except ResolutionError as exc:
        raise TaskReviewError(
            f"finalizing review recovery resolution evidence is invalid: {exc}"
        ) from exc
    if (
        persisted_resolution.operation_id != task_id
        or persisted_resolution.axis != "holistic"
        or persisted_resolution.resolved_head_sha
        != previous_context.head_sha
    ):
        raise TaskReviewError(
            "finalizing review recovery resolution identity changed"
        )
    bundle = _recovery_resolution_bundle(
        worktree,
        task_id,
        persisted_resolution,
        current_context.head_sha,
        str(state.get("resolution_transport_identity_sha256") or ""),
    )
    rebuilt_resolution = bundle.by_axis.get("holistic")
    if (
        rebuilt_resolution is None
        or rebuilt_resolution.operation_id
        != persisted_resolution.operation_id
        or rebuilt_resolution.axis != persisted_resolution.axis
        or rebuilt_resolution.reviewed_head_sha
        != persisted_resolution.reviewed_head_sha
        or rebuilt_resolution.previous_finding_ids
        != persisted_resolution.previous_finding_ids
        or dict(rebuilt_resolution.resolutions)
        != dict(persisted_resolution.resolutions)
    ):
        raise TaskReviewError(
            "finalizing review recovery finding rulings changed"
        )
    summary_bytes = summary_path.read_bytes()
    if not summary_bytes or len(summary_bytes) > 250_000:
        raise TaskReviewError(
            "finalizing review recovery task summary is invalid"
        )
    recovery_context, context_manifest = _context(
        meta,
        vault,
        worktree,
        runtime_root,
        task_id,
        resolution_bundle=bundle,
    )
    marker = {
        "schema_version": 1,
        "operation_id": task_id,
        "round_operation_id": round_.operation_id,
        "round_run_id": round_.run_id,
        "accepted_callback_sha256": envelope.payload_sha256,
        "failed_head_sha": previous_context.head_sha,
        "resubmitted_head_sha": recovery_context.head_sha,
        "verification_receipt_sha256": verification_receipt_sha256,
        "response_receipt_sha256": _canonical_sha256(response_receipt),
        "persisted_resolution_sha256": hashlib.sha256(
            persisted_path.read_bytes()
        ).hexdigest(),
        "resolution_sha256": hashlib.sha256(
            resolution_path.read_bytes()
        ).hexdigest(),
        "summary_sha256": hashlib.sha256(summary_bytes).hexdigest(),
        "status": "validated",
    }
    marker_name = (
        "finalizing-review-recovery-"
        f"{_canonical_sha256(marker)[:16]}.json"
    )
    marker_path = gate.root / marker_name
    if marker_path.exists():
        if (
            marker_path.is_symlink()
            or _read_json(marker_path, "finalizing review recovery marker")
            != marker
        ):
            raise TaskReviewError(
                "finalizing review recovery marker changed"
            )
    else:
        _atomic_json(marker_path, marker)
    if response_receipt_path.exists():
        if _read_json(
            response_receipt_path, "verification response receipt"
        ) != response_receipt:
            raise TaskReviewError(
                "finalizing review recovery response receipt changed"
            )
    else:
        _atomic_json(response_receipt_path, response_receipt)
    verification_child = store.read(
        task_id,
        str(response_receipt["verification_operation_id"]),
    )
    if verification_child.state == "attention-required":
        store.transition(
            task_id,
            verification_child.spec.operation_id,
            "failed",
        )
    elif verification_child.state != "failed":
        raise TaskReviewError(
            "finalizing review recovery verification response is not terminal"
        )
    return FinalizingRecovery(
        recovery_context,
        context_manifest,
        marker_name,
        hashlib.sha256(marker_path.read_bytes()).hexdigest(),
        response_receipt_path,
        response_receipt,
        result,
    )


def _apply_finalizing_recovery(
    *,
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    task_id: str,
    gate: ReviewGateController,
    run: ReviewGateRun,
    recovery: FinalizingRecovery,
) -> dict[str, Any]:
    lane = run.execution.lanes[0]
    round_ = run.rounds[lane.axis]
    initial_status = gate.read().get("status")
    try:
        gate.stage_finalizing_reverification(
            run,
            lane,
            round_,
            recovery.result,
            recovery_pointer=recovery.marker_pointer,
            recovery_sha256=recovery.marker_sha256,
        )
    except (OSError, ValueError) as exc:
        raise TaskReviewError(
            f"finalizing review recovery failed: {exc}"
        ) from exc
    if initial_status == "verifying":
        _emit_round_telemetry(
            worktree,
            vault,
            runtime_root,
            round_,
            event="review-callback",
            terminal_status="accepted",
        )
        _emit_round_telemetry(
            worktree,
            vault,
            runtime_root,
            round_,
            event="review-round-complete",
            terminal_status=recovery.result.verdict,
            severities=tuple(
                finding.severity for finding in recovery.result.findings
            ),
        )
    successful = _durable_successful_verification(
        meta,
        vault,
        gate.round_store,
        task_id,
        recovery.context.head_sha,
    )
    if successful is None:
        return _receipt(
            status="verifying",
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=recovery.context_manifest,
            run=gate.rehydrate(),
        )
    verification_operation_id, verification_receipt_sha256 = successful
    previous_context = run.execution.request.context
    reason = "verified resubmission requires exact-HEAD reviewer inspection"
    boundary = ReviewScopeBoundary(
        "context",
        review_context_sha256(previous_context),
        review_context_sha256(recovery.context),
        reason,
    )
    authorization = {
        "schema_version": 1,
        "operation_id": task_id,
        "kind": boundary.kind,
        "previous_context_sha256": boundary.previous_context_sha256,
        "next_context_sha256": boundary.next_context_sha256,
        "reason": boundary.reason,
        "authorization_provenance": "pipeline-verification",
        "verification_operation_id": verification_operation_id,
        "verification_receipt_sha256": verification_receipt_sha256,
        "status": "authorized",
    }
    authorization_name = (
        "fresh-boundary-authorization-"
        f"{_canonical_sha256(authorization)[:16]}.json"
    )
    authorization_path = gate.root / authorization_name
    if authorization_path.exists():
        if (
            authorization_path.is_symlink()
            or _read_json(
                authorization_path, "fresh boundary authorization"
            )
            != authorization
        ):
            raise TaskReviewError(
                "fresh boundary authorization changed across replay"
            )
    else:
        _atomic_json(authorization_path, authorization)
    authorization_sha256 = hashlib.sha256(
        authorization_path.read_bytes()
    ).hexdigest()
    try:
        gate.authorize_fresh_boundary(
            run,
            boundary=boundary,
            authorization_pointer=authorization_name,
            authorization_sha256=authorization_sha256,
        )
    except (OSError, ValueError) as exc:
        raise TaskReviewError(
            f"finalizing review boundary authorization failed: {exc}"
        ) from exc
    if not _dispatched_review_is_quiescent(gate.round_store, task_id):
        raise TaskReviewError(
            "verified fresh review boundary is not quiescent"
        )
    return _launch_authorized_task_review(
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        task_id=task_id,
        gate=gate,
        run=run,
        context=recovery.context,
        context_manifest=recovery.context_manifest,
        boundary=boundary,
    )


def _telemetry_marker(runtime_root: Path, axis: str) -> Path:
    return _callback_path(runtime_root, axis).parent / ".review-telemetry.json"


def _round_telemetry_state(
    runtime_root: Path, round_: ReviewRound
) -> dict[str, Any]:
    identity = {
        "schema_version": 1,
        "operation_id": round_.operation_id,
        "verification_iteration": round_.verification_iteration,
    }
    try:
        prior = json.loads(
            _telemetry_marker(runtime_root, round_.axis).read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        prior = {}
    if (
        isinstance(prior, dict)
        and all(prior.get(key) == value for key, value in identity.items())
        and isinstance(prior.get("started_at"), str)
        and isinstance(prior.get("emitted"), list)
    ):
        return prior
    return {
        **identity,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "emitted": [],
    }


def _emit_round_telemetry(
    worktree: Path,
    vault: Path,
    runtime_root: Path,
    round_: ReviewRound,
    *,
    event: str,
    terminal_status: str,
    severities: Sequence[str] = (),
) -> None:
    """Emit one replay-bounded event; every failure remains non-fatal."""

    try:
        state = _round_telemetry_state(runtime_root, round_)
        event_key = f"{event}:{terminal_status}"
        emitted = set(str(item) for item in state["emitted"])
        if event_key in emitted:
            return
        if not emit_review_event(
            worktree,
            vault,
            event=event,
            axis=round_.axis,
            reviewer_runtime=round_.spec.route.runtime,
            iteration=round_.verification_iteration,
            terminal_status=terminal_status,
            started_at=str(state["started_at"]),
            severities=severities,
        ):
            return
        _atomic_json(
            _telemetry_marker(runtime_root, round_.axis),
            {**state, "emitted": sorted(emitted | {event_key})},
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return


def load_active_round(
    gate_root: Path,
    store: OperationStore,
    runtime_manager: object,
    *,
    axis: str,
) -> ActiveReviewRound:
    run = ReviewGateController(
        gate_root, runtime_manager, store
    ).rehydrate()
    for lane in run.execution.lanes:
        if lane.axis == axis:
            return ActiveReviewRound(run, lane, run.rounds[axis])
    raise TaskReviewError("review axis is not active")


def _receipt(
    *,
    status: str,
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    context_manifest: Path,
    run: ReviewGateRun | None = None,
) -> dict[str, Any]:
    lanes = []
    if run is not None:
        lanes = [
            {
                "axis": lane.axis,
                "operation_id": lane.operation_id,
                "run_id": lane.run_id,
                "surface_id": lane.surface_id,
                "verification_iteration": lane.verification_iteration,
                "callback_path": str(
                    _callback_path(
                        runtime_root,
                        lane.axis,
                    )
                ),
            }
            for lane in run.execution.lanes
        ]
    return {
        "schema_version": 1,
        "status": status,
        "task_id": meta["task_id"],
        "worktree": str(worktree),
        "vault_root": str(vault),
        "context_manifest": str(context_manifest),
        "lanes": lanes,
    }


def _run_review(
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    task_id: str,
    runtime_root: Path,
    *,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    store_root = vault / ".vault-meta" / "harness"
    store = OperationStore(store_root)
    runtime = runtime_manager or RuntimeSessionManager.for_root(
        vault, store_root=store_root
    )
    gate_root = _gate_root(vault, task_id)
    context, context_manifest = _context(
        meta, vault, worktree, runtime_root, task_id
    )
    preset, request = _request(meta, vault, task_id, context)
    gate = ReviewGateController(gate_root, runtime, store)
    callback_wake = ""
    if meta.get("lifecycle") == "current-checkout":
        raw_policy = meta["review_policy"]
        wake_argv = [
            str(Path(sys.executable).resolve()),
            str(vault / "scripts" / "task-review-runner.py"),
            "current",
            "--worktree",
            str(worktree),
        ]
        if raw_policy["mode"] == "deep":
            wake_argv.append("--deep")
        if raw_policy["cross_model"]:
            wake_argv.append("--cross-model")
        for option in ("runtime", "model", "effort"):
            value = str(raw_policy.get(option) or "")
            if value:
                wake_argv.extend((f"--{option}", value))
        wake_argv.extend(("--plan", str(meta["plan_file"])))
        callback_wake = (
            "Typed current-review callback is ready. Run this exact command: "
            + shlex.join(wake_argv)
        )
    gate_exists = gate.state_path.exists()
    pending_replay = False
    if gate_exists:
        initial_state = gate.read()
        if (
            initial_state.get("status") == "attention-required"
            and initial_state.get("lanes") == []
        ):
            try:
                dispatch_record = store.read(task_id, task_id)
            except StoreError:
                dispatch_record = None
            if (
                dispatch_record is not None
                and dispatch_record.state not in {
                    "attention-required",
                    *TERMINAL,
                }
            ):
                gate.resume_unbound_attention()
                initial_state = gate.read()
        pending_replay = initial_state.get("status") == "pending"
        if pending_replay and initial_state.get("lanes") != []:
            raise TaskReviewError("pending review gate already owns lanes")
    if not gate_exists or pending_replay:
        if not preset.enabled:
            ReviewGateController.skip(
                gate_root,
                dispatch_operation_id=task_id,
                owner_id=task_id,
                preset=preset,
                context=context,
                product_root=worktree,
            )
            return _receipt(
                status="skipped",
                meta=meta,
                vault=vault,
                worktree=worktree,
                runtime_root=runtime_root,
                context_manifest=context_manifest,
            )
        if request is None:
            raise TaskReviewError("enabled review has no request")
        if pending_replay and not _pending_replay_is_safe(
            request, store, gate, runtime
        ):
            return _receipt(
                status="attention-required",
                meta=meta,
                vault=vault,
                worktree=worktree,
                runtime_root=runtime_root,
                context_manifest=context_manifest,
            )
        prompt_pointers = {
            axis: _prompt(
                vault=vault,
                worktree=worktree,
                runtime_root=runtime_root,
                context=context,
                axis=axis,
                verification=False,
            )
            for axis in request.policy.axes
        }

        def prepare_lane(
            axis: str,
            _session_request: object,
            _result: object,
            round_: ReviewRound,
        ) -> None:
            _write_round_meta(
                runtime_root=runtime_root,
                vault=vault,
                worktree=worktree,
                task_id=task_id,
                depth=preset.depth,
                context=context,
                lane_operation_id=round_.parent_operation_id,
                round_=round_,
            )

        try:
            run = gate.begin(
                dispatch_operation_id=task_id,
                request=request,
                origin_surface=str(meta.get("task_surface") or ""),
                cwd=runtime_root,
                product_root=worktree,
                prompt_pointer=prompt_pointers[request.policy.axes[0]],
                prompt_pointers=prompt_pointers,
                callback_root="callbacks",
                callback_wake=callback_wake,
                prepare_lane=prepare_lane,
            )
        except ValueError:
            if pending_replay and not _pending_replay_is_safe(
                request, store, gate, runtime
            ):
                return _receipt(
                    status="attention-required",
                    meta=meta,
                    vault=vault,
                    worktree=worktree,
                    runtime_root=runtime_root,
                    context_manifest=context_manifest,
                )
            raise
        return _receipt(
            status="reviewing",
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=run,
        )

    state = gate.read()
    status = str(state.get("status") or "")
    stored_lanes = state.get("lanes")
    if (
        status == "attention-required"
        and isinstance(stored_lanes, list)
        and stored_lanes
    ):
        owner_id = str(state.get("owner_id") or "")
        recoverable = bool(owner_id)
        for lane in stored_lanes:
            if not isinstance(lane, dict):
                recoverable = False
                break
            axis = str(lane.get("axis") or "")
            operation_id = str(lane.get("operation_id") or "")
            callback = _callback_path(runtime_root, axis)
            if (
                not axis
                or not operation_id
                or not callback.is_file()
                or callback.is_symlink()
            ):
                recoverable = False
                break
            try:
                record = store.read(owner_id, operation_id)
            except StoreError:
                recoverable = False
                break
            if record.state not in {"awaiting-callback", "verifying"}:
                recoverable = False
                break
        if recoverable:
            gate.resume_bound_attention()
            state = gate.read()
            status = str(state.get("status") or "")
    if status in {"approved", "skipped", "attention-required"}:
        bound = state.get("context")
        if (
            status in {"approved", "skipped"}
            and (
                not isinstance(bound, dict)
                or bound.get("head_sha") != context.head_sha
            )
        ):
            raise TaskReviewError(
                "terminal review evidence is stale for the product HEAD"
            )
        stored_lanes = state.get("lanes")
        receipt_run = (
            None
            if status == "skipped"
            or (
                status == "attention-required"
                and stored_lanes == []
            )
            else gate.rehydrate()
        )
        return _receipt(
            status=status,
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=receipt_run,
        )
    run = gate.rehydrate()
    if status == "awaiting-resolution":
        awaiting = state.get("awaiting_resolution")
        if not isinstance(awaiting, dict) or not awaiting:
            raise TaskReviewError("awaiting review has no finding evidence")
        if any(
            not isinstance(value, dict)
            or not str(value.get("reviewed_head_sha") or "")
            for value in awaiting.values()
        ):
            raise TaskReviewError("review resolution boundary is invalid")
        reviewed_heads = {
            str(value["reviewed_head_sha"])
            for value in awaiting.values()
        }
        if reviewed_heads == {context.head_sha}:
            return _receipt(
                status=status,
                meta=meta,
                vault=vault,
                worktree=worktree,
                runtime_root=runtime_root,
                context_manifest=context_manifest,
                run=run,
            )
        bundle = _resolution_bundle(
            worktree,
            gate_root,
            task_id,
            awaiting,
            context.head_sha,
        )
        context, context_manifest = _context(
            meta,
            vault,
            worktree,
            runtime_root,
            task_id,
            resolution_bundle=bundle,
        )
        decision = None
        for lane in run.execution.lanes:
            if lane.axis not in awaiting:
                continue
            pointer = _prompt(
                vault=vault,
                worktree=worktree,
                runtime_root=runtime_root,
                context=context,
                axis=lane.axis,
                verification=True,
            )

            def prepare_round(
                next_lane: object,
                round_: ReviewRound,
            ) -> None:
                _write_round_meta(
                    runtime_root=runtime_root,
                    vault=vault,
                    worktree=worktree,
                    task_id=task_id,
                    depth=preset.depth,
                    context=context,
                    lane_operation_id=round_.parent_operation_id,
                    round_=round_,
                )

            decision = gate.continue_after_resolution(
                run,
                lane,
                context=context,
                resolution=bundle.by_axis[lane.axis],
                review_identity_sha256=(
                    bundle.review_identity_sha256
                ),
                verification_prompt_pointer=pointer,
                callback_pointer=(
                    _callback_path(runtime_root, lane.axis)
                    .relative_to(runtime_root)
                    .as_posix()
                ),
                prepare_round=prepare_round,
            )
            if decision.action == "attention-required":
                break
        next_status = (
            decision.action
            if decision is not None
            and decision.action == "attention-required"
            else str(gate.read().get("status") or "")
        )
        return _receipt(
            status=next_status,
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=gate.rehydrate(),
        )
    if status not in {
        "reviewing",
        "verifying",
        "recovery-verification-required",
        "fresh-boundary-authorized",
    }:
        raise TaskReviewError("review gate has an unsupported state")
    if context.head_sha != run.execution.request.context.head_sha:
        recovery = _finalizing_resubmit_recovery(
            meta,
            vault,
            worktree,
            runtime_root,
            task_id,
            store,
            gate,
            run,
            context,
        )
        if recovery is None:
            raise TaskReviewError(
                "product HEAD changed outside an awaiting-resolution boundary"
            )
        return _apply_finalizing_recovery(
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            task_id=task_id,
            gate=gate,
            run=run,
            recovery=recovery,
        )
    ready: list[tuple[object, ReviewRound, ReviewResult]] = []
    for lane in run.execution.lanes:
        round_ = run.rounds[lane.axis]
        callback = _callback_path(runtime_root, lane.axis)
        if not callback.is_file() or callback.is_symlink():
            continue
        try:
            _unused, result = _envelope(callback, round_)
        except StaleRoundCallbackError:
            _emit_round_telemetry(
                worktree,
                vault,
                runtime_root,
                round_,
                event="review-callback",
                terminal_status="rejected",
            )
            raise
        except (TaskReviewError, OSError, ValueError):
            _emit_round_telemetry(
                worktree,
                vault,
                runtime_root,
                round_,
                event="review-callback",
                terminal_status="rejected",
            )
            raise
        ready.append((lane, round_, result))
    if preset.depth == "deep" and len(ready) != len(
        run.execution.lanes
    ):
        return _receipt(
            status=status,
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=run,
        )
    if preset.depth == "deep" and any(
        result.verdict == "changes-requested"
        and any(
            finding.severity in MATERIAL_SEVERITIES
            for finding in result.findings
        )
        for _lane, _round, result in ready
    ):
        for lane, round_, result in ready:
            decision = gate.defer_round_for_resolution(
                run, lane, round_, result
            )
            _emit_round_telemetry(
                worktree,
                vault,
                runtime_root,
                round_,
                event="review-callback",
                terminal_status="accepted",
            )
            _emit_round_telemetry(
                worktree,
                vault,
                runtime_root,
                round_,
                event="review-round-complete",
                terminal_status=result.verdict,
                severities=tuple(
                    finding.severity for finding in result.findings
                ),
            )
            if decision.action == "attention-required":
                break
    else:
        for lane, round_, result in ready:
            decision = gate.complete_round(
                run,
                lane,
                round_,
                result,
            )
            _emit_round_telemetry(
                worktree,
                vault,
                runtime_root,
                round_,
                event="review-callback",
                terminal_status="accepted",
            )
            _emit_round_telemetry(
                worktree,
                vault,
                runtime_root,
                round_,
                event="review-round-complete",
                terminal_status=result.verdict,
                severities=tuple(
                    finding.severity for finding in result.findings
                ),
            )
            if decision.action == "attention-required":
                break
    next_status = str(gate.read().get("status") or "")
    return _receipt(
        status=next_status,
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        context_manifest=context_manifest,
        run=None if next_status == "skipped" else gate.rehydrate(),
    )


def recover_finalizing_review(
    worktree: Path,
    *,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    """Recover only the exact accepted-approval verification crash window."""

    worktree = worktree.expanduser().resolve()
    meta, vault, task_id = _validate_task(worktree)
    runtime_root = _runtime_root(vault, task_id)
    store_root = vault / ".vault-meta" / "harness"
    store = OperationStore(store_root)
    runtime = runtime_manager or RuntimeSessionManager.for_root(
        vault, store_root=store_root
    )
    gate = ReviewGateController(
        _gate_root(vault, task_id),
        runtime,
        store,
    )
    if not gate.state_path.is_file() or gate.state_path.is_symlink():
        raise TaskReviewError(
            "finalizing review recovery gate is unavailable"
        )
    run = gate.rehydrate()
    context, context_manifest = _context(
        meta,
        vault,
        worktree,
        runtime_root,
        task_id,
    )
    state = gate.read()
    if (
        state.get("status") == "approved"
        and isinstance(state.get("context"), dict)
        and state["context"].get("head_sha") == context.head_sha
        and isinstance(state.get("finalizing_recovery"), dict)
    ):
        return _receipt(
            status="approved",
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=run,
        )
    if (
        state.get("status") in {"fresh-reevaluation", "reviewing", "verifying"}
        and isinstance(state.get("context"), dict)
        and state["context"].get("head_sha") == context.head_sha
        and isinstance(state.get("fresh_boundary_authorization"), dict)
    ):
        return _receipt(
            status="reviewing",
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=run,
        )
    if context.head_sha == run.execution.request.context.head_sha:
        raise TaskReviewError(
            "finalizing review recovery requires an exact repaired HEAD"
        )
    recovery = _finalizing_resubmit_recovery(
        meta,
        vault,
        worktree,
        runtime_root,
        task_id,
        store,
        gate,
        run,
        context,
    )
    if recovery is None:
        raise TaskReviewError(
            "finalizing review recovery boundary is unavailable"
        )
    return _apply_finalizing_recovery(
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        task_id=task_id,
        gate=gate,
        run=run,
        recovery=recovery,
    )


def _dispatched_review_is_quiescent(
    store: OperationStore,
    task_id: str,
) -> bool:
    rows = store.list(task_id)
    if not rows:
        return False
    for row in rows:
        if row.resources != OwnedResources() or row.pending_effect:
            return False
        if (
            row.spec.operation_id == task_id
            and row.spec.kind == "dispatch"
        ):
            if row.state != "attention-required":
                return False
        elif row.state not in TERMINAL:
            return False
    return True


def _launch_authorized_task_review(
    *,
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    task_id: str,
    gate: ReviewGateController,
    run: ReviewGateRun,
    context: ReviewContext,
    context_manifest: Path,
    boundary: ReviewScopeBoundary,
    max_verify_iterations: int | None = None,
) -> dict[str, Any]:
    """Launch one pre-authorized fresh run after complete scratch preflight."""

    prompt_pointers = {
        axis: _prompt(
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context=context,
            axis=axis,
            verification=False,
        )
        for axis in run.execution.request.policy.axes
    }

    def prepare_lane(
        axis: str,
        _session_request: object,
        _result: object,
        round_: ReviewRound,
    ) -> None:
        _write_round_meta(
            runtime_root=runtime_root,
            vault=vault,
            worktree=worktree,
            task_id=task_id,
            depth=run.execution.request.policy.depth,
            context=context,
            lane_operation_id=round_.parent_operation_id,
            round_=round_,
        )

    for axis in run.execution.request.policy.axes:
        callback = _callback_path(runtime_root, axis)
        if callback.is_symlink():
            raise TaskReviewError("fresh review callback is invalid")
        callback.unlink(missing_ok=True)
    fresh = gate.restart_for_boundary(
        run,
        boundary=boundary,
        context=context,
        origin_surface=str(meta.get("task_surface") or ""),
        cwd=runtime_root,
        product_root=worktree,
        prompt_pointer=prompt_pointers[
            run.execution.request.policy.axes[0]
        ],
        prompt_pointers=prompt_pointers,
        callback_root="callbacks",
        max_verify_iterations=max_verify_iterations,
        prepare_lane=prepare_lane,
    )
    if fresh is None:
        raise TaskReviewError("fresh review boundary is exhausted")
    return _receipt(
        status="reviewing",
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        context_manifest=context_manifest,
        run=fresh,
    )


def recover_task_review_for_mechanism(
    worktree: Path,
    *,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    """Use one resolved mechanism escalation to replace a dead review lane."""

    worktree = worktree.expanduser().resolve()
    meta, vault, task_id = _validate_task(worktree)
    attention_path = worktree / ".task-needs-attention.json"
    attention = _read_json(attention_path, "task escalation")
    current_context, context_manifest = _context(
        meta,
        vault,
        worktree,
        _runtime_root(vault, task_id),
        task_id,
    )
    decision = str(attention.get("decision") or "")
    if (
        attention.get("status") != "resolved"
        or attention.get("category") != "mechanism-failure"
        or str(attention.get("worktree") or "") != str(worktree)
        or not decision.startswith(
            "authorize-one-bounded-fresh-context-review-boundary-for-"
        )
        or current_context.head_sha[:7] not in decision
    ):
        raise TaskReviewError(
            "review mechanism recovery lacks exact coordinator authorization"
        )
    store_root = vault / ".vault-meta" / "harness"
    store = OperationStore(store_root)
    runtime = runtime_manager or RuntimeSessionManager.for_root(
        vault, store_root=store_root
    )
    gate = ReviewGateController(
        _gate_root(vault, task_id),
        runtime,
        store,
    )
    state = gate.read()
    run = gate.rehydrate()
    stored_boundary = state.get("fresh_boundary")
    if (
        state.get("fresh_reevaluation_used") is True
        and state.get("status") in {"fresh-reevaluation", "reviewing", "verifying"}
        and isinstance(stored_boundary, dict)
        and str(attention.get("id") or "")
        in str(stored_boundary.get("reason") or "")
    ):
        return _receipt(
            status=(
                "verifying"
                if state.get("status") == "verifying"
                else "reviewing"
            ),
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=_runtime_root(vault, task_id),
            context_manifest=(
                _runtime_root(vault, task_id)
                / run.execution.request.context.manifest
            ),
            run=run,
        )
    previous_context = run.execution.request.context
    if (
        state.get("status") == "approved"
        and state.get("fresh_reevaluation_used") is not True
        and state.get("final_results") not in ({}, None)
        and run.execution.request.policy.depth == "simple"
        and run.execution.request.policy.axes == ("holistic",)
        and previous_context.head_sha == current_context.head_sha
        and previous_context.verification_profile
        == current_context.verification_profile
        and previous_context.verification_profile_sha256
        == current_context.verification_profile_sha256
        and bool(previous_context.implementer_summary_sha256)
        and previous_context.implementer_summary_sha256
        != current_context.implementer_summary_sha256
    ):
        raw_resolution_evidence = state.get("resolution_evidence")
        if (
            not isinstance(raw_resolution_evidence, dict)
            or len(raw_resolution_evidence) != 1
        ):
            raise TaskReviewError(
                "approved summary recovery resolution boundary is invalid"
            )
        persisted_pointer = Path(
            str(next(iter(raw_resolution_evidence.values())))
        )
        persisted_path = (gate.root / persisted_pointer).resolve()
        if (
            persisted_pointer.is_absolute()
            or gate.root not in persisted_path.parents
            or not persisted_path.is_file()
            or persisted_path.is_symlink()
        ):
            raise TaskReviewError(
                "approved summary recovery resolution evidence is unavailable"
            )
        try:
            persisted_resolution = validate_resolution_evidence(
                _read_json(
                    persisted_path, "persisted review resolution"
                )
            )
        except ResolutionError as exc:
            raise TaskReviewError(
                "approved summary recovery resolution evidence is invalid"
            ) from exc
        if (
            persisted_resolution.operation_id != task_id
            or persisted_resolution.axis != "holistic"
            or persisted_resolution.resolved_head_sha
            != current_context.head_sha
        ):
            raise TaskReviewError(
                "approved summary recovery resolution identity changed"
            )
        bundle = _recovery_resolution_bundle(
            worktree,
            task_id,
            persisted_resolution,
            current_context.head_sha,
            str(state.get("resolution_transport_identity_sha256") or ""),
        )
        current_context, context_manifest = _context(
            meta,
            vault,
            worktree,
            _runtime_root(vault, task_id),
            task_id,
            resolution_bundle=bundle,
        )
        boundary = ReviewScopeBoundary(
            "context",
            review_context_sha256(previous_context),
            review_context_sha256(current_context),
            (
                "resolved mechanism escalation "
                f"{attention.get('id')}: review refreshed summary bytes only"
            ),
        )
        authorization = {
            "schema_version": 1,
            "operation_id": task_id,
            "kind": boundary.kind,
            "previous_context_sha256": boundary.previous_context_sha256,
            "next_context_sha256": boundary.next_context_sha256,
            "reason": boundary.reason,
            "authorization_provenance": "coordinator-approved",
            "verification_operation_id": str(attention.get("id") or ""),
            "verification_receipt_sha256": hashlib.sha256(
                attention_path.read_bytes()
            ).hexdigest(),
            "status": "authorized",
        }
        authorization_name = (
            "fresh-boundary-authorization-"
            f"{_canonical_sha256(authorization)[:16]}.json"
        )
        authorization_path = gate.root / authorization_name
        if authorization_path.exists():
            if (
                authorization_path.is_symlink()
                or _read_json(
                    authorization_path,
                    "fresh summary boundary authorization",
                )
                != authorization
            ):
                raise TaskReviewError(
                    "approved summary recovery authorization changed"
                )
        else:
            _atomic_json(authorization_path, authorization)
        gate.authorize_fresh_summary_boundary(
            run,
            boundary=boundary,
            context=current_context,
            authorization_pointer=authorization_name,
            authorization_sha256=hashlib.sha256(
                authorization_path.read_bytes()
            ).hexdigest(),
        )
        return _launch_authorized_task_review(
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=_runtime_root(vault, task_id),
            task_id=task_id,
            gate=gate,
            run=run,
            context=current_context,
            context_manifest=context_manifest,
            boundary=boundary,
            max_verify_iterations=0,
        )
    if (
        state.get("status")
        not in {"verifying", "attention-required", "fresh-boundary-authorized"}
        or state.get("fresh_reevaluation_used") is True
        or state.get("final_results") not in ({}, None)
        or not run.execution.lanes
    ):
        raise TaskReviewError(
            "review mechanism recovery is not at one stale verification boundary"
        )
    for lane in run.execution.lanes:
        parent = store.read(task_id, lane.operation_id)
        round_ = run.rounds.get(lane.axis)
        if round_ is None:
            raise TaskReviewError("review mechanism recovery round is unavailable")
        child = store.read(task_id, round_.operation_id)
        if (
            parent.state not in TERMINAL
            or child.state not in TERMINAL
            or parent.resources != OwnedResources()
            or child.resources != OwnedResources()
            or parent.pending_effect
            or child.pending_effect
        ):
            raise TaskReviewError(
                "review mechanism recovery still has live review ownership"
            )
    boundary = ReviewScopeBoundary(
        "context",
        review_context_sha256(previous_context),
        review_context_sha256(current_context),
        (
            "resolved mechanism escalation "
            f"{attention.get('id')}: replace the dead verification runtime"
        ),
    )
    authorization = {
        "schema_version": 1,
        "operation_id": task_id,
        "kind": boundary.kind,
        "previous_context_sha256": boundary.previous_context_sha256,
        "next_context_sha256": boundary.next_context_sha256,
        "reason": boundary.reason,
        "authorization_provenance": "coordinator-approved",
        "verification_operation_id": str(attention.get("id") or ""),
        "verification_receipt_sha256": hashlib.sha256(
            attention_path.read_bytes()
        ).hexdigest(),
        "status": "authorized",
    }
    authorization_name = (
        "fresh-boundary-authorization-"
        f"{_canonical_sha256(authorization)[:16]}.json"
    )
    authorization_path = gate.root / authorization_name
    if authorization_path.exists():
        if (
            authorization_path.is_symlink()
            or _read_json(
                authorization_path, "fresh boundary authorization"
            )
            != authorization
        ):
            raise TaskReviewError(
                "review mechanism recovery authorization changed"
            )
    else:
        _atomic_json(authorization_path, authorization)
    if state.get("status") == "verifying":
        gate._mark_attention(run.execution.lanes)
    if gate.read().get("status") != "fresh-boundary-authorized":
        gate.authorize_fresh_boundary(
            run,
            boundary=boundary,
            authorization_pointer=authorization_name,
            authorization_sha256=hashlib.sha256(
                authorization_path.read_bytes()
            ).hexdigest(),
        )
    return _launch_authorized_task_review(
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=_runtime_root(vault, task_id),
        task_id=task_id,
        gate=gate,
        run=run,
        context=current_context,
        context_manifest=context_manifest,
        boundary=boundary,
        max_verify_iterations=0,
    )


def restart_task_review_for_boundary(
    worktree: Path,
    *,
    kind: str,
    reason: str,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    """Start the one persisted fresh review allowed for a dispatched task."""

    worktree = worktree.expanduser().resolve()
    meta, vault, task_id = _validate_task(worktree)
    runtime_root = _runtime_root(vault, task_id)
    store_root = vault / ".vault-meta" / "harness"
    store = OperationStore(store_root)
    runtime = runtime_manager or RuntimeSessionManager.for_root(
        vault, store_root=store_root
    )
    gate = ReviewGateController(
        _gate_root(vault, task_id),
        runtime,
        store,
    )
    if not gate.state_path.is_file() or gate.state_path.is_symlink():
        raise TaskReviewError("fresh review gate is unavailable")
    state = gate.read()
    run = gate.rehydrate()
    context, context_manifest = _context(
        meta,
        vault,
        worktree,
        runtime_root,
        task_id,
    )
    stored_boundary = state.get("fresh_boundary")
    if (
        state.get("status") in {"fresh-reevaluation", "reviewing", "verifying"}
        and state.get("fresh_reevaluation_used") is True
        and isinstance(stored_boundary, dict)
        and stored_boundary.get("kind") == kind
        and stored_boundary.get("reason") == reason
        and stored_boundary.get("next_context_sha256")
        == review_context_sha256(context)
    ):
        return _receipt(
            status="reviewing",
            meta=meta,
            vault=vault,
            worktree=worktree,
            runtime_root=runtime_root,
            context_manifest=context_manifest,
            run=run,
        )
    if (
        state.get("status") != "fresh-boundary-authorized"
        or state.get("fresh_reevaluation_used") is True
        or not _dispatched_review_is_quiescent(store, task_id)
    ):
        raise TaskReviewError(
            "fresh review requires one quiescent authorized boundary"
        )
    previous_context = run.execution.request.context
    boundary = ReviewScopeBoundary(
        kind,
        review_context_sha256(previous_context),
        review_context_sha256(context),
        reason,
    )
    return _launch_authorized_task_review(
        meta=meta,
        vault=vault,
        worktree=worktree,
        runtime_root=runtime_root,
        task_id=task_id,
        gate=gate,
        run=run,
        context=context,
        context_manifest=context_manifest,
        boundary=boundary,
    )


def run_task_review(
    worktree: Path,
    *,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    worktree = worktree.expanduser().resolve()
    meta, vault, task_id = _validate_task(worktree)
    return _run_review(
        meta,
        vault,
        worktree,
        task_id,
        _runtime_root(vault, task_id),
        runtime_manager=runtime_manager,
    )


def _validate_current_checkout(worktree: Path) -> Path:
    worktree = worktree.expanduser().resolve()
    required = (
        worktree / "wiki",
        worktree / "scripts",
        worktree / "skills/review/SKILL.md",
        worktree / "config/model-routing.toml",
        worktree / "config/verification-profiles.toml",
    )
    if (
        not worktree.is_dir()
        or any(not path.exists() for path in required)
        or _git(worktree, "rev-parse", "--show-toplevel") != str(worktree)
    ):
        raise TaskReviewError(
            "current review requires an exact llm-obsidian checkout root"
        )
    return worktree


def _current_policy(
    *,
    deep: bool,
    cross_model: bool,
    runtime: str,
    model: str,
    effort: str,
    no_review: bool,
    profile_sha256: str,
) -> dict[str, Any]:
    preset = ReviewPreset.from_flags(
        deep=deep,
        cross_model=cross_model,
        runtime=runtime,
        model=model,
        effort=effort,
        no_review=no_review,
    )
    mode = preset.depth if preset.enabled else "skip"
    return {
        "mode": mode,
        "cross_model": cross_model,
        "runtime": runtime,
        "model": model,
        "effort": effort,
        "max_verify_iterations": {
            "simple": 1,
            "deep": 2,
            "skip": 0,
        }[mode],
        "verification_profile": "scoped",
        "verification_profile_sha256": profile_sha256,
    }


def _same_requested_policy(
    stored: Mapping[str, Any],
    requested: Mapping[str, Any],
) -> bool:
    return all(
        stored.get(name) == requested.get(name)
        for name in (
            "mode",
            "cross_model",
            "runtime",
            "model",
            "effort",
            "max_verify_iterations",
            "verification_profile",
            "verification_profile_sha256",
        )
    )


def _pending_replay_is_safe(
    request: ReviewOperationRequest,
    store: OperationStore,
    gate: ReviewGateController,
    runtime: object,
) -> bool:
    for identity in review_session_specs(request):
        record = None
        safe = False
        for _attempt in range(2):
            try:
                record = store.read(
                    request.owner_id, identity.spec.operation_id
                )
            except StoreError:
                safe = True
                break
            resources = record.resources
            clean_created = (
                record.state == "created"
                and not record.pending_effect
                and not any(
                    (
                        resources.surface_id,
                        resources.process_group,
                        resources.supervisor_pid,
                        resources.process_identity,
                        resources.supervisor_identity,
                    )
                )
            )
            if clean_created:
                safe = True
                break
            if record.state not in {
                "running",
                "awaiting-callback",
                "verifying",
            }:
                break
            try:
                observed = runtime.status(
                    request.owner_id, identity.spec.operation_id
                )
                latest = store.read(
                    request.owner_id, identity.spec.operation_id
                )
            except Exception:
                continue
            observed_record = getattr(observed, "record", None)
            observed_resources = getattr(
                observed_record, "resources", None
            )
            if (
                observed_record == latest
                and observed_resources is not None
                and bool(observed_resources.surface_id)
                and observed_resources.process_group > 1
                and observed_resources.supervisor_pid > 1
                and bool(observed_resources.process_identity)
                and bool(observed_resources.supervisor_identity)
                and runtime_status_is_live(observed)
            ):
                safe = True
                break
        if safe:
            continue
        try:
            record = store.read(
                request.owner_id, identity.spec.operation_id
            )
        except StoreError:
            continue
        if (
            record.state not in TERMINAL
            and record.state != "attention-required"
        ):
            store.transition(
                request.owner_id,
                record.spec.operation_id,
                "attention-required",
                reason=AttentionReason.ATTENTION_REQUIRED,
            )
        gate.mark_pending_attention()
        return False
    return True


def run_current_review(
    worktree: Path,
    *,
    deep: bool = False,
    cross_model: bool = False,
    runtime: str = "",
    model: str = "",
    effort: str = "",
    no_review: bool = False,
    plan_file: Path | None = None,
    origin_surface: str = "",
    scratch_root: Path | None = None,
    runtime_manager: object | None = None,
) -> dict[str, Any]:
    worktree = _validate_current_checkout(worktree)
    vault = worktree
    profiles = load_profiles(vault / "config/verification-profiles.toml")
    profile = profiles.get("scoped")
    if profile is None:
        raise TaskReviewError("scoped verification profile is unavailable")
    requested_policy = _current_policy(
        deep=deep,
        cross_model=cross_model,
        runtime=runtime,
        model=model,
        effort=effort,
        no_review=no_review,
        profile_sha256=profile.sha256,
    )
    active_path = (
        vault
        / ".vault-meta"
        / "harness"
        / "current-review"
        / "active.json"
    )
    meta: dict[str, Any] | None = None
    if active_path.is_file() and not active_path.is_symlink():
        candidate = _read_json(active_path, "current review state")
        if (
            candidate.get("lifecycle") != "current-checkout"
            or candidate.get("worktree") != str(worktree)
        ):
            raise TaskReviewError("current review state belongs to another checkout")
        try:
            task_id = str(uuid.UUID(str(candidate.get("task_id") or "")))
        except (ValueError, TypeError, AttributeError) as exc:
            raise TaskReviewError("current review identity is invalid") from exc
        gate_state_path = _gate_root(vault, task_id) / "review-gate.json"
        stored_policy = candidate.get("review_policy")
        same_policy = isinstance(stored_policy, dict) and _same_requested_policy(
            stored_policy, requested_policy
        )
        terminal_stale = False
        if gate_state_path.is_file() and not gate_state_path.is_symlink():
            gate_state = _read_json(gate_state_path, "current review gate")
            status = str(gate_state.get("status") or "")
            bound = gate_state.get("context")
            bound_head = (
                str(bound.get("head_sha") or "")
                if isinstance(bound, dict)
                else ""
            )
            terminal_stale = (
                status in {"approved", "skipped"}
                and (
                    bound_head != _git(worktree, "rev-parse", "HEAD")
                    or not same_policy
                )
            ) or (
                status == "attention-required"
                and _current_review_is_quiescent(vault, task_id)
            )
        elif gate_state_path.exists():
            raise TaskReviewError("current review gate is not a regular file")
        if not terminal_stale:
            if not same_policy:
                raise TaskReviewError(
                    "an active current review uses another preset or override"
                )
            if plan_file is not None and (
                Path(str(candidate.get("plan_file") or "")).resolve()
                != plan_file.expanduser().resolve()
            ):
                raise TaskReviewError(
                    "an active current review uses another plan"
                )
            meta = candidate

    if meta is None:
        task_id = str(uuid.uuid4())
        runtime_root = _current_runtime_root(
            worktree, task_id, scratch_root
        )
        surface = (
            origin_surface.strip()
            or str(os.environ.get("CMUX_SURFACE_ID") or "").strip()
        )
        if not surface:
            raise TaskReviewError(
                "current review requires an exact cmux origin surface"
            )
        config = load_config(vault)
        session, source = routing_from_environment(config)
        if source == "tracked-default":
            raise TaskReviewError(
                "current review requires a host-confirmed current session route"
            )
        session = {**session, "source": source}
        if plan_file is None:
            plan = runtime_root / "inputs/current-review-scope.md"
            _atomic_text(
                plan,
                "\n".join(
                    (
                        "# Current checkout review scope",
                        "",
                        "Review the exact current checkout and HEAD against its "
                        "repository instructions, tests, and public contract.",
                        "",
                    )
                ),
            )
        else:
            plan = plan_file.expanduser().resolve()
            if not plan.is_file() or plan.is_symlink():
                raise TaskReviewError("current review plan is unavailable")
        meta = {
            "version": 4,
            "lifecycle": "current-checkout",
            "task_id": task_id,
            "task_name": "current checkout review",
            "task_surface": surface,
            "worktree": str(worktree),
            "vault_root": str(vault),
            "plan_file": str(plan),
            "routing": {"session": session},
            "review_policy": requested_policy,
            "runtime_root": str(runtime_root),
        }
        _request(
            meta,
            vault,
            task_id,
            ReviewContext(
                "pending/manifest.json",
                _git(worktree, "rev-parse", "HEAD"),
                "scoped",
                profile.sha256,
            ),
        )
        _atomic_json(runtime_root / "current-review.json", meta)
        _atomic_json(active_path, meta)
    else:
        task_id = str(meta["task_id"])
        runtime_root = Path(str(meta.get("runtime_root") or "")).resolve()
        expected_root = _current_runtime_root(
            worktree, task_id, scratch_root
        )
        if runtime_root != expected_root:
            raise TaskReviewError(
                "current review scratch root changed during an active gate"
            )
        if (
            runtime_root == worktree
            or worktree in runtime_root.parents
            or not (runtime_root / "current-review.json").is_file()
        ):
            raise TaskReviewError("current review scratch is unavailable")

    return _run_review(
        meta,
        vault,
        worktree,
        task_id,
        runtime_root,
        runtime_manager=runtime_manager,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--worktree", type=Path, required=True)
    fresh = sub.add_parser("fresh")
    fresh.add_argument("--worktree", type=Path, required=True)
    fresh.add_argument("--kind", choices=("scope", "context"), required=True)
    fresh.add_argument("--reason", required=True)
    recover = sub.add_parser("recover")
    recover.add_argument("--worktree", type=Path, required=True)
    current = sub.add_parser("current")
    current.add_argument("--worktree", type=Path, required=True)
    current.add_argument("--deep", action="store_true")
    current.add_argument("--cross-model", action="store_true")
    current.add_argument("--runtime", choices=("claude", "codex"), default="")
    current.add_argument("--model", default="")
    current.add_argument("--effort", default="")
    current.add_argument("--no-review", action="store_true")
    current.add_argument("--plan", type=Path)
    current.add_argument("--origin-surface", default="")
    return result


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime_manager: object | None = None,
) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "run":
            result = run_task_review(
                args.worktree, runtime_manager=runtime_manager
            )
        elif args.command == "fresh":
            result = restart_task_review_for_boundary(
                args.worktree,
                kind=args.kind,
                reason=args.reason,
                runtime_manager=runtime_manager,
            )
        elif args.command == "recover":
            result = recover_task_review_for_mechanism(
                args.worktree,
                runtime_manager=runtime_manager,
            )
        else:
            result = run_current_review(
                args.worktree,
                deep=args.deep,
                cross_model=args.cross_model,
                runtime=args.runtime,
                model=args.model,
                effort=args.effort,
                no_review=args.no_review,
                plan_file=args.plan,
                origin_surface=args.origin_surface,
                runtime_manager=runtime_manager,
            )
    except (OSError, TaskReviewError, ValueError, RuntimeError) as exc:
        print(f"task-review-runner: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
