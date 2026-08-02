"""Trusted plan-risk and terminal-gate authority for review programs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

from vault_schema import parse_frontmatter, split_frontmatter

from .review_program_contracts import (
    IDENTIFIER,
    RISK_PURPOSES,
    ReviewBoundaryInput,
    ReviewProgramError,
    require_sha256,
)
from .review_program_results import ReviewBoundaryReceipt


def _object(path: Path, label: str) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise ReviewProgramError(f"{label} is unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewProgramError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise ReviewProgramError(f"{label} must be an object")
    return value


def approved_risk_from_plan(
    worktree: Path,
    plan: Path,
    boundaries: tuple[ReviewBoundaryInput, ...],
) -> str:
    """Derive risk from exact plan metadata instead of a caller assertion."""

    root = worktree.expanduser().resolve()
    source = plan.expanduser()
    target = source.resolve()
    if (
        not root.is_dir()
        or target == root
        or root not in target.parents
        or not target.is_file()
        or source.is_symlink()
    ):
        raise ReviewProgramError("approved review plan is unavailable")
    raw = target.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if not boundaries or any(item.plan_sha256 != digest for item in boundaries):
        raise ReviewProgramError("review program plan binding is stale")
    block = split_frontmatter(raw.decode("utf-8"))
    if block is None:
        raise ReviewProgramError("approved review plan metadata is unavailable")
    try:
        risk = parse_frontmatter(block).get("review_risk_profile")
    except ValueError as exc:
        raise ReviewProgramError("approved review plan metadata is invalid") from exc
    if not isinstance(risk, str) or risk not in RISK_PURPOSES:
        raise ReviewProgramError("approved review risk profile is unavailable")
    return risk


def _bound_artifact(root: Path, pointer: object, label: str) -> tuple[Path, bytes]:
    if not isinstance(pointer, str) or not pointer:
        raise ReviewProgramError(f"trusted {label} pointer is invalid")
    source = root / pointer
    target = source.resolve()
    if (
        target == root
        or root not in target.parents
        or not target.is_file()
        or source.is_symlink()
    ):
        raise ReviewProgramError(f"trusted {label} is unavailable")
    return target, target.read_bytes()


def _result_files(
    gate_root: Path,
    pointers: object,
    label: str,
) -> dict[str, str]:
    if not isinstance(pointers, dict) or not pointers:
        raise ReviewProgramError(f"trusted review gate has no {label}")
    result: dict[str, str] = {}
    for axis, pointer in sorted(pointers.items()):
        if not isinstance(axis, str) or not axis:
            raise ReviewProgramError(f"trusted review gate {label} are invalid")
        _path, raw = _bound_artifact(gate_root, pointer, label)
        result[axis] = hashlib.sha256(raw).hexdigest()
    return result


def trusted_review_receipt(
    worktree: Path,
    boundary: ReviewBoundaryInput,
    operation_id: str,
) -> ReviewBoundaryReceipt:
    """Derive a receipt only from an exact terminal harness gate and result bytes."""

    if not isinstance(operation_id, str) or not IDENTIFIER.fullmatch(operation_id):
        raise ReviewProgramError("trusted review operation id is invalid")
    root = worktree.expanduser().resolve()
    gate_root = (
        root
        / ".vault-meta/harness/review-data"
        / operation_id
        / operation_id
    ).resolve()
    trusted_root = (root / ".vault-meta/harness/review-data").resolve()
    if trusted_root not in gate_root.parents:
        raise ReviewProgramError("trusted review gate is unavailable")
    gate = _object(gate_root / "review-gate.json", "trusted review gate")
    context = gate.get("context")
    policy = gate.get("policy")
    if (
        gate.get("schema_version") != 1
        or gate.get("active_review_operation_id") != operation_id
        or gate.get("product_root") != str(root)
        or not isinstance(context, dict)
        or not isinstance(policy, dict)
        or context.get("purpose") != boundary.purpose
        or policy.get("purpose") != boundary.purpose
        or context.get("boundary_input_sha256") != boundary.input_sha256
    ):
        raise ReviewProgramError("trusted review gate identity is stale")
    expected_head = boundary.product_head_sha or boundary.integration_head_sha
    if expected_head and context.get("head_sha") != expected_head:
        raise ReviewProgramError("trusted review gate HEAD is stale")

    status = gate.get("status")
    if status == "approved":
        _result_files(gate_root, gate.get("final_results"), "final results")
        evidence = gate.get("evidence")
        if not isinstance(evidence, dict) or evidence.get("operation_id") != operation_id:
            raise ReviewProgramError("trusted review gate evidence is invalid")
        callback_path, callback_bytes = _bound_artifact(
            gate_root, evidence.get("pointer"), "review callback"
        )
        digest = hashlib.sha256(callback_bytes).hexdigest()
        require_sha256(str(evidence.get("sha256") or ""), "review callback digest")
        if evidence["sha256"] != digest:
            raise ReviewProgramError("trusted review callback digest is stale")
        callback = _object(callback_path, "trusted review callback")
        payload = callback.get("payload")
        if (
            callback.get("operation_id") != operation_id
            or not isinstance(payload, dict)
            or payload.get("operation_id") != operation_id
            or payload.get("verdict") != "approve"
            or payload.get("head_sha") != context.get("head_sha")
        ):
            raise ReviewProgramError("trusted review callback is not terminal approval")
        return ReviewBoundaryReceipt.approved(
            operation_id=operation_id,
            boundary=boundary,
            result_sha256=digest,
        )
    if status == "stopped":
        result_digests = _result_files(
            gate_root, gate.get("stopped_results"), "stopped results"
        )
        digest = hashlib.sha256(
            json.dumps(result_digests, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ReviewBoundaryReceipt.stopped(
            operation_id=operation_id,
            boundary=boundary,
            result_sha256=digest,
        )
    raise ReviewProgramError("trusted review gate is not terminal")


def validate_trusted_receipts(
    worktree: Path,
    boundaries: tuple[ReviewBoundaryInput, ...],
    receipts: tuple[ReviewBoundaryReceipt, ...],
) -> None:
    for boundary, receipt in zip(boundaries, receipts, strict=False):
        trusted = trusted_review_receipt(worktree, boundary, receipt.operation_id)
        if trusted != receipt:
            raise ReviewProgramError("review receipt does not match trusted review gate")
