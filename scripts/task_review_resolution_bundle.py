"""Durable review finding resolution and bounded context inputs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping, Sequence

from harness.context import ContextInput
from review_resolution import (
    MATERIAL_SEVERITIES,
    MAX_FIX_DELTA_TOTAL_BYTES,
    ResolutionError,
    ReviewResolution,
    ReviewResolutionEvidence,
    build_resolution_evidence,
    review_transport_identity_sha256,
    validate_resolution,
    validate_resolution_evidence,
)
from review_contract import axis_finding_id
from task_review_delta_packet import build_delta_packet
from task_review_shared import (
    ResolutionBundle,
    TaskReviewError,
    _git,
    _git_bytes,
    _read_json,
)


def _load_persisted_resolution_evidence(
    gate_root: Path,
    task_id: str,
    awaiting: Mapping[str, object],
    resolved_head: str,
    pointers: Mapping[str, object],
) -> tuple[
    dict[str, tuple[str, ...]],
    set[str],
    list[ReviewResolutionEvidence],
]:
    """Validate and select the persisted axes for one exact resolution HEAD."""

    finding_ids_by_axis: dict[str, tuple[str, ...]] = {}
    reviewed_heads: set[str] = set()
    evidence_batch: list[ReviewResolutionEvidence] = []
    for key, raw_pointer in sorted(pointers.items()):
        if not isinstance(key, str) or not isinstance(raw_pointer, str):
            raise TaskReviewError(
                "persisted review resolution pointer is invalid"
            )
        pointer = Path(raw_pointer)
        evidence_path = (gate_root / pointer).resolve()
        if (
            pointer.is_absolute()
            or gate_root not in evidence_path.parents
            or not evidence_path.is_file()
            or evidence_path.is_symlink()
        ):
            raise TaskReviewError(
                "persisted review resolution evidence is unavailable"
            )
        try:
            evidence = validate_resolution_evidence(
                _read_json(
                    evidence_path, "persisted review resolution evidence"
                )
            )
        except ResolutionError as exc:
            raise TaskReviewError(
                f"persisted review resolution evidence is invalid: {exc}"
            ) from exc
        if evidence.resolved_head_sha != resolved_head:
            continue
        if evidence.operation_id != task_id:
            raise TaskReviewError(
                "persisted review resolution operation changed"
            )
        if evidence.axis in finding_ids_by_axis or evidence.axis in awaiting:
            raise TaskReviewError(
                "review resolution axis is staged more than once"
            )
        finding_ids_by_axis[evidence.axis] = evidence.previous_finding_ids
        reviewed_heads.add(evidence.reviewed_head_sha)
        evidence_batch.append(evidence)
    return finding_ids_by_axis, reviewed_heads, evidence_batch


def _resolution_origin_head(
    gate_root: Path,
    task_id: str,
    pointers: Mapping[str, object],
    reviewed_head: str,
) -> str:
    """Walk the unique persisted resolution chain back to its first HEAD."""

    predecessors: dict[str, str] = {}
    for raw_pointer in pointers.values():
        if not isinstance(raw_pointer, str):
            raise TaskReviewError("persisted review resolution pointer is invalid")
        pointer = Path(raw_pointer)
        evidence_path = (gate_root / pointer).resolve()
        if (
            pointer.is_absolute()
            or gate_root not in evidence_path.parents
            or not evidence_path.is_file()
            or evidence_path.is_symlink()
        ):
            raise TaskReviewError(
                "persisted review resolution evidence is unavailable"
            )
        try:
            evidence = validate_resolution_evidence(
                _read_json(
                    evidence_path, "persisted review resolution evidence"
                )
            )
        except ResolutionError as exc:
            raise TaskReviewError(
                f"persisted review resolution evidence is invalid: {exc}"
            ) from exc
        if evidence.operation_id != task_id:
            raise TaskReviewError("persisted review resolution operation changed")
        previous = predecessors.setdefault(
            evidence.resolved_head_sha, evidence.reviewed_head_sha
        )
        if previous != evidence.reviewed_head_sha:
            raise TaskReviewError("persisted review resolution chain forks")
    cursor = reviewed_head
    seen: set[str] = set()
    while cursor in predecessors:
        if cursor in seen:
            raise TaskReviewError("persisted review resolution chain cycles")
        seen.add(cursor)
        cursor = predecessors[cursor]
    return cursor


def _resolution_bundle(
    worktree: Path,
    gate_root: Path,
    task_id: str,
    awaiting: Mapping[str, object],
    resolved_head: str,
    *,
    persisted_identity_sha256: str = "",
    persisted_resolution_pointers: Mapping[str, object] | None = None,
) -> ResolutionBundle:
    finding_ids_by_axis, reviewed_heads, persisted_evidence = (
        _load_persisted_resolution_evidence(
            gate_root,
            task_id,
            awaiting,
            resolved_head,
            persisted_resolution_pointers or {},
        )
    )
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
        local_material = tuple(
            str(finding.get("finding_id") or "")
            for finding in findings
            if isinstance(finding, dict)
            and finding.get("severity") in MATERIAL_SEVERITIES
        )
        if "" in local_material:
            raise TaskReviewError("material finding identity is invalid")
        raw_material = raw_boundary.get("material_finding_ids")
        if raw_material is None:
            material = local_material
        else:
            if (
                not isinstance(raw_material, list)
                or any(
                    not isinstance(finding_id, str) or not finding_id
                    for finding_id in raw_material
                )
            ):
                raise TaskReviewError(
                    "review resolution material identity is invalid"
                )
            material = tuple(raw_material)
            qualified = tuple(
                axis_finding_id(axis, finding_id)
                for finding_id in local_material
            )
            if material not in {local_material, qualified}:
                raise TaskReviewError(
                    "review resolution material identity drifted"
                )
        finding_ids_by_axis[str(axis)] = material
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
    all_finding_ids = tuple(
        finding_id
        for axis in sorted(finding_ids_by_axis)
        for finding_id in finding_ids_by_axis[axis]
    )
    if len(all_finding_ids) != len(set(all_finding_ids)):
        raise TaskReviewError("material finding identities repeat across axes")
    if len(reviewed_heads) != 1 or "" in reviewed_heads:
        raise TaskReviewError("review resolution heads are inconsistent")
    if (
        (review_operation_ids and len(review_operation_ids) != 1)
        or "" in review_operation_ids
        or (not review_operation_ids and not persisted_identity_sha256)
    ):
        raise TaskReviewError("review resolution operation is inconsistent")
    if persisted_identity_sha256:
        if not persisted_evidence:
            raise TaskReviewError(
                "persisted review identity has no exact-HEAD resolution evidence"
            )
        review_identity_sha256 = persisted_identity_sha256
    else:
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
            expected_finding_ids=all_finding_ids,
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
    if not fix_delta or len(fix_delta) > MAX_FIX_DELTA_TOTAL_BYTES:
        raise TaskReviewError(
            "review fix delta must be non-empty and at most 131072 bytes"
        )
    build_delta_packet(fix_delta, reviewed_head, resolved_head)
    fix_delta_sha256 = hashlib.sha256(fix_delta).hexdigest()
    if any(
        evidence.fix_delta_sha256 != fix_delta_sha256
        for evidence in persisted_evidence
    ):
        raise TaskReviewError("persisted review resolution fix delta changed")
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
    for evidence in persisted_evidence:
        rebuilt = by_axis.get(evidence.axis)
        if rebuilt is None or rebuilt.payload() != evidence.payload():
            raise TaskReviewError(
                "persisted review resolution finding rulings changed"
            )
    origin_reviewed_head_sha = _resolution_origin_head(
        gate_root,
        task_id,
        persisted_resolution_pointers or {},
        resolution.reviewed_head_sha,
    )
    return ResolutionBundle(
        resolution,
        fix_delta,
        by_axis,
        review_identity_sha256,
        origin_reviewed_head_sha,
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
    if not fix_delta or len(fix_delta) > MAX_FIX_DELTA_TOTAL_BYTES:
        raise TaskReviewError(
            "review fix delta must be non-empty and at most 131072 bytes"
        )
    build_delta_packet(
        fix_delta, persisted.reviewed_head_sha, resolved_head
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
