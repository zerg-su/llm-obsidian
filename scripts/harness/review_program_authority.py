"""Trusted plan-risk and terminal-gate authority for review programs."""

from __future__ import annotations

import hashlib
import json
import subprocess
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
from .review_program_resolution import resolved_terminal_head
from .workflows.review_gate import authorize_task_finalization


_BOUNDARY_SOURCES = {
    "intent": (
        ("design_path", "design_sha256", "intent design"),
        (
            "capability_dispositions_path",
            "capability_dispositions_sha256",
            "intent capability dispositions",
        ),
        (
            "success_evidence_map_path",
            "success_evidence_map_sha256",
            "intent success evidence",
        ),
    ),
    "implementation": (
        (
            "verification_evidence_path",
            "verification_evidence_sha256",
            "implementation verification evidence",
        ),
    ),
    "release": (
        (
            "outcome_evidence_map_path",
            "outcome_evidence_map_sha256",
            "release outcome evidence",
        ),
        (
            "accepted_deviations_path",
            "accepted_deviations_sha256",
            "release accepted deviations",
        ),
    ),
}


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


def _validate_boundary_sources(root: Path, boundary: ReviewBoundaryInput) -> None:
    if not root.is_dir():
        raise ReviewProgramError("trusted review worktree is unavailable")
    for path_field, digest_field, label in _BOUNDARY_SOURCES[boundary.purpose]:
        pointer = getattr(boundary, path_field)
        source = root
        for part in Path(pointer).parts:
            source /= part
            if source.is_symlink():
                raise ReviewProgramError(f"trusted {label} is unavailable")
        _path, raw = _bound_artifact(root, pointer, label)
        if hashlib.sha256(raw).hexdigest() != getattr(boundary, digest_field):
            raise ReviewProgramError(f"trusted {label} digest is stale")


def _actual_candidate_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel", "HEAD^{commit}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReviewProgramError("trusted candidate HEAD is unavailable") from exc
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 2:
        raise ReviewProgramError("trusted candidate HEAD is unavailable")
    try:
        git_root = Path(lines[0]).resolve(strict=True)
    except OSError as exc:
        raise ReviewProgramError("trusted candidate HEAD is unavailable") from exc
    if git_root != root:
        raise ReviewProgramError("trusted candidate HEAD is unavailable")
    return lines[1]


def _require_candidate_head(root: Path, expected: tuple[str, ...]) -> None:
    if expected and _actual_candidate_head(root) not in expected:
        raise ReviewProgramError("trusted candidate HEAD is stale")


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


def _trusted_review_receipt(
    worktree: Path,
    boundary: ReviewBoundaryInput,
    operation_id: str,
    *,
    require_candidate_head: bool,
) -> ReviewBoundaryReceipt:
    """Derive a receipt only from an exact terminal harness gate and result bytes."""

    if not isinstance(operation_id, str) or not IDENTIFIER.fullmatch(operation_id):
        raise ReviewProgramError("trusted review operation id is invalid")
    root = worktree.expanduser().resolve()
    _validate_boundary_sources(root, boundary)
    expected_head = boundary.product_head_sha or boundary.integration_head_sha
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
    terminal_head = resolved_terminal_head(
        root, gate_root, gate, boundary, operation_id
    )
    if require_candidate_head and expected_head and terminal_head:
        _require_candidate_head(root, (terminal_head,))

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
        if callback.get("operation_id") != operation_id:
            raise ReviewProgramError("trusted review callback is not terminal approval")
        try:
            authorization = authorize_task_finalization(
                gate_root,
                dispatch_operation_id=operation_id,
                expected_head_sha=str(context.get("head_sha") or ""),
                expected_profile=str(context.get("verification_profile") or ""),
                expected_profile_sha256=str(
                    context.get("verification_profile_sha256") or ""
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewProgramError(
                f"trusted review callback is not terminal approval: {exc}"
            ) from exc
        if not authorization.approved:
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


def trusted_review_receipt(
    worktree: Path,
    boundary: ReviewBoundaryInput,
    operation_id: str,
) -> ReviewBoundaryReceipt:
    """Mint only while the reviewed source evidence and candidate are current."""

    return _trusted_review_receipt(
        worktree,
        boundary,
        operation_id,
        require_candidate_head=True,
    )


def validate_trusted_receipts(
    worktree: Path,
    boundaries: tuple[ReviewBoundaryInput, ...],
    receipts: tuple[ReviewBoundaryReceipt, ...],
) -> None:
    if len(receipts) > len(boundaries):
        raise ReviewProgramError("review receipt count exceeds the program")
    root = worktree.expanduser().resolve()
    trusted_heads: list[str] = []
    for boundary, receipt in zip(boundaries, receipts, strict=False):
        trusted = _trusted_review_receipt(
            root,
            boundary,
            receipt.operation_id,
            require_candidate_head=False,
        )
        if trusted != receipt:
            raise ReviewProgramError("review receipt does not match trusted review gate")
        gate_root = (
            root
            / ".vault-meta/harness/review-data"
            / receipt.operation_id
            / receipt.operation_id
        ).resolve()
        gate = _object(gate_root / "review-gate.json", "trusted review gate")
        context = gate.get("context")
        if not isinstance(context, dict) or not isinstance(
            context.get("head_sha"), str
        ):
            raise ReviewProgramError("trusted review gate identity is stale")
        trusted_heads.append(context["head_sha"])
    if not receipts:
        return
    current_index = len(receipts) - 1
    expected_heads: list[str] = []
    current_head = trusted_heads[current_index]
    if current_head:
        expected_heads.append(current_head)
    if len(receipts) < len(boundaries):
        next_boundary = boundaries[len(receipts)]
        next_head = next_boundary.product_head_sha or next_boundary.integration_head_sha
        if next_head:
            _validate_boundary_sources(root, next_boundary)
            expected_heads.append(next_head)
    if not expected_heads:
        return
    _require_candidate_head(
        root,
        tuple(dict.fromkeys(expected_heads)),
    )


def stale_resolution_boundary(
    status: str,
    bound_head: str,
    current_head: str,
    quiescent: bool,
) -> bool:
    """Allow replacement only after the reviewed HEAD moved and owners closed."""

    return (
        status == "awaiting-resolution"
        and bool(bound_head)
        and bound_head != current_head
        and quiescent
    )
