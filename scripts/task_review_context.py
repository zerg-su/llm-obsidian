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

from harness.context import (
    OUTCOME_POINTER_ID,
    ContextBuilder,
    ContextInput,
    outcome_contract_input,
)
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
from outcome_contract import OutcomeContractError, extract_from_bytes
from task_contract import normalize
from task_review_delta_packet import build_delta_packet
from task_review_resolution_bundle import _bounded_input
from task_review_identity import (
    _current_review_is_quiescent,
    _current_runtime_root,
    _gate_root,
    _runtime_root,
    _validate_task,
)
from task_review_request import (
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
    _atomic_bytes,
    _atomic_text,
    _git,
    _git_bytes,
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


class _ReviewedArtifactMissing(TaskReviewError):
    """The exact reviewed tree has no entry for a boundary artifact."""


def _reviewed_artifact_input(
    worktree: Path,
    relative: Path,
    *,
    name: str,
    artifact_head: str,
    expected_sha256: str,
    pointer_root: Path,
) -> ContextInput:
    if relative.is_absolute() or relative == Path(".") or ".." in relative.parts:
        raise TaskReviewError(
            f"review boundary artifact is unavailable: {relative}"
        )
    tracked_path = _git(
        worktree,
        "ls-tree",
        "--name-only",
        artifact_head,
        "--",
        relative.as_posix(),
    )
    if tracked_path != relative.as_posix():
        raise _ReviewedArtifactMissing(
            f"review boundary artifact has no reviewed Git blob: {relative}"
        )
    raw = _git_bytes(
        worktree,
        "show",
        f"{artifact_head}:{relative.as_posix()}",
    )
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise TaskReviewError(
            f"review boundary artifact digest is stale: {relative}"
        )
    source = f"git:{artifact_head}:{relative.as_posix()}"
    if len(raw) <= 65_536:
        return ContextInput(name, source, raw, role="outcome")
    _atomic_bytes(pointer_root / name, raw)
    return ContextInput.pointer(
        name,
        source,
        byte_count=len(raw),
        content_sha256=digest,
        role="outcome",
    )


def _reviewed_plan_outcome_input(
    worktree: Path,
    plan: Path,
    boundary: ReviewBoundaryInput,
    *,
    artifact_head: str,
) -> ContextInput:
    """Bind resolution semantics to the exact plan bytes originally reviewed."""

    root = worktree.expanduser().resolve()
    resolved_plan = plan.expanduser().resolve()
    try:
        relative = resolved_plan.relative_to(root)
    except ValueError as exc:
        raise TaskReviewError("review program plan is outside the worktree") from exc
    raw = _git_bytes(
        worktree,
        "show",
        f"{artifact_head}:{relative.as_posix()}",
    )
    if hashlib.sha256(raw).hexdigest() != boundary.plan_sha256:
        raise TaskReviewError("review program plan digest is stale")
    try:
        contract = extract_from_bytes(raw)
    except OutcomeContractError as exc:
        raise TaskReviewError(f"review Outcome Contract is invalid: {exc}") from exc
    if contract.sha256 != boundary.outcome_contract_sha256:
        raise TaskReviewError("review Outcome Contract input digest changed")
    return ContextInput(
        "outcome-contract.json",
        f"git:{artifact_head}:{relative.as_posix()}",
        contract.canonical,
        role="outcome",
        pointer_id=OUTCOME_POINTER_ID,
    )


def _boundary_file_input(
    source_root: Path,
    relative: Path,
    *,
    name: str,
    expected_sha256: str,
    pointer_root: Path,
) -> ContextInput:
    """Materialize one path-confined artifact with its frozen digest."""

    source_root = source_root.resolve()
    candidate = source_root / relative
    target = candidate.resolve()
    if (
        target == source_root
        or source_root not in target.parents
        or not target.is_file()
        or candidate.is_symlink()
    ):
        raise TaskReviewError(
            f"review boundary artifact is unavailable: {relative}"
        )
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
    if item.content_sha256 != expected_sha256:
        raise TaskReviewError(
            f"review boundary artifact digest is stale: {relative}"
        )
    return item


def _purpose_boundary_inputs(
    worktree: Path,
    plan: Path,
    boundary: ReviewBoundaryInput,
    *,
    pointer_root: Path,
    artifact_root: Path | None = None,
    artifact_head: str = "",
) -> tuple[ContextInput, ...]:
    """Validate and materialize every exact artifact named by a purpose boundary."""

    if artifact_head and artifact_root is None:
        contract = _reviewed_plan_outcome_input(
            worktree,
            plan,
            boundary,
            artifact_head=artifact_head,
        )
    else:
        try:
            contract = outcome_contract_input(
                plan,
                expected_sha256=boundary.outcome_contract_sha256,
            )
            plan_bytes = plan.read_bytes()
        except (OSError, HarnessContractError) as exc:
            raise TaskReviewError(
                f"review Outcome Contract is invalid: {exc}"
            ) from exc
        if hashlib.sha256(plan_bytes).hexdigest() != boundary.plan_sha256:
            raise TaskReviewError("review program plan digest is stale")
    inputs = [contract]
    source_root = (artifact_root or worktree).resolve()
    for name, path_field, digest_field in _BOUNDARY_ARTIFACTS[boundary.purpose]:
        relative = Path(str(getattr(boundary, path_field)))
        expected_sha256 = str(getattr(boundary, digest_field))
        if artifact_head:
            if artifact_root is not None:
                raise TaskReviewError(
                    f"review boundary artifact is unavailable: {relative}"
                )
            try:
                item = _reviewed_artifact_input(
                    worktree, relative, name=name,
                    artifact_head=artifact_head,
                    expected_sha256=expected_sha256,
                    pointer_root=pointer_root,
                )
            except _ReviewedArtifactMissing:
                # Ignored release receipts have no Git blob. Their frozen
                # boundary digest remains authoritative, and exact path
                # confinement prevents substituting a foreign artifact.
                item = _boundary_file_input(
                    worktree,
                    relative,
                    name=name,
                    expected_sha256=expected_sha256,
                    pointer_root=pointer_root,
                )
            inputs.append(item)
            continue
        inputs.append(
            _boundary_file_input(
                source_root,
                relative,
                name=name,
                expected_sha256=expected_sha256,
                pointer_root=pointer_root,
            )
        )
    return tuple(inputs)


def _head_diff_input(worktree: Path) -> ContextInput:
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
    return ContextInput("head-diff.patch", "git:show:HEAD", diff, role="diff")

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
    plan_review = meta.get("plan_review")
    plan_artifact_root: Path | None = None
    plan_review_inputs: list[ContextInput] = []
    packet_metadata = {
        "task_id": task_id,
        "task_name": str(meta.get("task_name") or ""),
        "head_sha": head,
    }
    if plan_review is not None:
        if not isinstance(plan_review, Mapping):
            raise TaskReviewError("plan review metadata is invalid")
        base_sha = str(plan_review.get("base_sha") or "")
        bound_head = str(plan_review.get("head_sha") or "")
        if (
            plan_review.get("schema_version") != 1
            or plan_review.get("artifact_root") != "runtime"
            or bound_head != head
            or not base_sha
        ):
            raise TaskReviewError("plan review OID binding is stale")
        inspection = runtime_root / "inputs/plan-review-inspection.json"
        evidence = _read_json(inspection, "plan review inspection evidence")
        commands = evidence.get("commands")
        if (
            evidence.get("schema_version") != 1
            or evidence.get("base_sha") != base_sha
            or evidence.get("head_sha") != head
            or not isinstance(commands, list)
            or len(commands) != 4
            or any(not isinstance(item, str) or not item for item in commands)
        ):
            raise TaskReviewError("plan review inspection evidence is stale")
        plan_artifact_root = runtime_root
        packet_metadata["base_sha"] = base_sha
        plan_review_inputs.extend(
            (
                ContextInput(
                    "exact-base.txt",
                    "git:base",
                    (base_sha + "\n").encode(),
                    role="base",
                ),
                ContextInput(
                    "review-inspect-commands.txt",
                    str(inspection),
                    ("\n".join(commands) + "\n").encode(),
                    role="instructions",
                ),
            )
        )
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
        *plan_review_inputs,
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
            resolution_bundle.origin_reviewed_head_sha
            or resolution_bundle.resolution.reviewed_head_sha
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
                artifact_root=plan_artifact_root,
                artifact_head=(
                    boundary_head
                    if exact_resolution_rebind and plan_artifact_root is None
                    else ""
                ),
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
    inputs.append(_head_diff_input(worktree))
    if resolution_bundle is not None:
        delta_packet = build_delta_packet(
            resolution_bundle.fix_delta,
            resolution_bundle.resolution.reviewed_head_sha,
            resolution_bundle.resolution.resolved_head_sha,
        )
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
        delta_source = (
            "git:diff:"
            f"{resolution_bundle.resolution.reviewed_head_sha}.."
            f"{resolution_bundle.resolution.resolved_head_sha}"
        )
        inputs.append(
            ContextInput(
                "fix-delta.manifest.json",
                delta_source + "#manifest",
                delta_packet.manifest,
                role="fix",
            )
        )
        inputs.extend(
            ContextInput(
                part.name,
                delta_source
                + f"#part={index}/{len(delta_packet.parts)}",
                part.content,
                role="fix",
            )
            for index, part in enumerate(delta_packet.parts, start=1)
        )
        inputs.append(
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
            )
        )
    builder = ContextBuilder(runtime_root / "packets")
    manifest = builder.build(
        task_id,
        tuple(inputs),
        metadata=packet_metadata,
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
