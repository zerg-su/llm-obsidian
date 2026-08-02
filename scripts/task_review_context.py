"""Exact review context, routing, prompts, and callback envelopes."""

from __future__ import annotations

import hashlib
import json
import shlex
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping

from harness.context import ContextBuilder, ContextInput, outcome_contract_input
from harness.contracts import CallbackEnvelope, ContractError as HarnessContractError, RuntimeRoute
from harness.review_program import QUESTIONS as REVIEW_QUESTIONS, ReviewBoundaryInput
from harness.review_submit import round_schema_lines
from harness.state_machine import TERMINAL
from harness.store import OperationStore
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
from task_contract import normalize
from task_review_resolution_bundle import _bounded_input
from task_review_identity import (
    _current_review_is_quiescent,
    _current_runtime_root,
    _gate_root,
    _runtime_root,
    _validate_task,
)
from task_review_request import (
    _axis_name,
    _callback_path,
    _canonical_sha256,
    _envelope,
    _prompt,
    _request,
    _route,
)
from task_review_shared import (
    ResolutionBundle,
    StaleRoundCallbackError,
    TaskReviewError,
    _atomic_text,
    _git,
    _load_review_boundary_input,
    _read_json,
)


_BOUNDARY_ARTIFACTS = {
    "intent": (
        ("review-design", "design_path", "design_sha256"),
        ("review-capability-dispositions", "capability_dispositions_path", "capability_dispositions_sha256"),
        ("review-success-evidence-map", "success_evidence_map_path", "success_evidence_map_sha256"),
    ),
    "implementation": (
        ("review-verification-evidence", "verification_evidence_path", "verification_evidence_sha256"),
    ),
    "release": (
        ("review-outcome-evidence", "outcome_evidence_map_path", "outcome_evidence_map_sha256"),
        ("review-accepted-deviations", "accepted_deviations_path", "accepted_deviations_sha256"),
    ),
}


def _purpose_boundary_inputs(
    worktree: Path,
    plan: Path,
    boundary: ReviewBoundaryInput,
    *,
    pointer_root: Path,
) -> tuple[ContextInput, ...]:
    """Validate and materialize every exact artifact named by a purpose boundary."""

    try:
        contract = outcome_contract_input(
            plan,
            expected_sha256=boundary.outcome_contract_sha256,
        )
        plan_bytes = plan.read_bytes()
    except (OSError, HarnessContractError) as exc:
        raise TaskReviewError(f"review Outcome Contract is invalid: {exc}") from exc
    if hashlib.sha256(plan_bytes).hexdigest() != boundary.plan_sha256:
        raise TaskReviewError("review program plan digest is stale")
    inputs = [contract]
    for name, path_field, digest_field in _BOUNDARY_ARTIFACTS[boundary.purpose]:
        relative = Path(str(getattr(boundary, path_field)))
        candidate = worktree / relative
        target = candidate.resolve()
        if (
            target == worktree
            or worktree not in target.parents
            or not target.is_file()
            or candidate.is_symlink()
        ):
            raise TaskReviewError(f"review boundary artifact is unavailable: {relative}")
        try:
            item = _bounded_input(
                name,
                target,
                role="outcome",
                pointer_root=pointer_root,
            )
        except OSError as exc:
            raise TaskReviewError(
                f"review boundary artifact is unavailable: {relative}"
            ) from exc
        if item.content_sha256 != getattr(boundary, digest_field):
            raise TaskReviewError(f"review boundary artifact digest is stale: {relative}")
        inputs.append(item)
    return tuple(inputs)

def _context(
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    task_id: str,
    resolution_bundle: ResolutionBundle | None = None,
) -> tuple[ReviewContext, Path]:
    head = _git(worktree, "rev-parse", "HEAD")
    policy = meta["review_policy"]
    purpose = str(policy.get("purpose") or "implementation")
    boundary_input_sha256 = str(
        policy.get("boundary_input_sha256") or ""
    )
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
    if boundary_input_sha256:
        boundary_path = Path(
            str(meta.get("review_boundary_input_file") or "")
        )
        boundary = _load_review_boundary_input(
            boundary_path, purpose=purpose
        )
        if boundary.input_sha256 != boundary_input_sha256:
            raise TaskReviewError("review boundary input digest is stale")
        rebound_reviewed_head = (
            resolution_bundle.resolution.reviewed_head_sha
            if resolution_bundle is not None
            else ""
        )
        rebound_resolved_head = (
            resolution_bundle.resolution.resolved_head_sha
            if resolution_bundle is not None
            else ""
        )
        boundary_head = (
            boundary.product_head_sha
            if purpose == "implementation"
            else boundary.integration_head_sha
            if purpose == "release"
            else ""
        )
        exact_resolution_rebind = (
            bool(boundary_head)
            and boundary_head == rebound_reviewed_head
            and head == rebound_resolved_head
        )
        if (
            purpose == "implementation"
            and boundary.product_head_sha != head
        ) or (
            purpose == "release" and boundary.integration_head_sha != head
        ):
            if not exact_resolution_rebind:
                raise TaskReviewError("review boundary input targets another HEAD")
        inputs.append(
            ContextInput(
                "review-boundary-input.json",
                str(boundary_path),
                (
                    json.dumps(
                        boundary.payload(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode(),
                role="outcome",
            )
        )
        inputs.extend(
            _purpose_boundary_inputs(
                worktree,
                plan,
                boundary,
                pointer_root=runtime_root / "pointers",
            )
        )
    elif purpose != "implementation":
        raise TaskReviewError(
            "non-legacy review purpose requires a boundary input"
        )
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
    return (
        ReviewContext(
            manifest_path.relative_to(runtime_root).as_posix(),
            head,
            str(policy["verification_profile"]),
            str(policy["verification_profile_sha256"]),
            implementer_summary_sha256,
            purpose,
            boundary_input_sha256,
        ),
        manifest_path,
    )
