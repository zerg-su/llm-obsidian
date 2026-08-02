"""Authority checks for implementation-review HEAD resolution chains."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Mapping

from review_resolution import (
    MAX_FIX_DELTA_BYTES,
    ResolutionError,
    validate_resolution_evidence,
)

from .review_program_contracts import ReviewBoundaryInput, ReviewProgramError


_AXIS_SHORT = {
    "holistic": "holistic",
    "spec": "spec",
    "standards-correctness-architecture-security": "standards",
}
_MATERIAL = frozenset({"critical", "important"})


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


def _bound_object(root: Path, pointer: str, label: str) -> dict[str, object]:
    relative = Path(pointer)
    target = (root / relative).resolve()
    if (
        relative.is_absolute()
        or target == root
        or root not in target.parents
        or not target.is_file()
        or target.is_symlink()
    ):
        raise ReviewProgramError(f"{label} is unavailable")
    return _object(target, label)


def _material_ids(
    gate_root: Path,
    operation_id: str,
    axis: str,
    iteration: int,
) -> tuple[str, ...]:
    short = _AXIS_SHORT[axis]
    result = _bound_object(
        gate_root,
        f"{operation_id}/round-{short}-{iteration}.json",
        "trusted pre-resolution round",
    )
    findings = result.get("findings")
    if (
        result.get("axis") != axis
        or result.get("verification_iteration") != iteration
        or result.get("verdict") not in {"approve", "changes-requested", "blocked"}
        or not isinstance(findings, list)
    ):
        raise ReviewProgramError("trusted pre-resolution round is invalid")
    material: list[str] = []
    for finding in findings:
        if not isinstance(finding, dict):
            raise ReviewProgramError("trusted pre-resolution round is invalid")
        severity = finding.get("severity")
        finding_id = finding.get("finding_id")
        if severity in _MATERIAL:
            if not isinstance(finding_id, str) or not finding_id:
                raise ReviewProgramError("trusted pre-resolution round is invalid")
            material.append(finding_id)
    if result.get("verdict") == "approve" and material:
        raise ReviewProgramError("trusted pre-resolution round is invalid")
    return tuple(material)


def _fix_delta_sha256(root: Path, reviewed_head: str, resolved_head: str) -> str:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "diff",
                "--binary",
                "--no-ext-diff",
                reviewed_head,
                resolved_head,
                "--",
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReviewProgramError("trusted review fix delta is unavailable") from exc
    if (
        result.returncode != 0
        or not result.stdout
        or len(result.stdout) > MAX_FIX_DELTA_BYTES
    ):
        raise ReviewProgramError("trusted review fix delta is unavailable")
    return hashlib.sha256(result.stdout).hexdigest()


def _require_final_iterations(
    gate_root: Path,
    gate: Mapping[str, object],
    iterations: Mapping[str, int],
) -> None:
    pointers = gate.get("final_results")
    if not isinstance(pointers, dict) or set(pointers) != set(iterations):
        raise ReviewProgramError("trusted review verification evidence is invalid")
    for axis, count in iterations.items():
        pointer = pointers.get(axis)
        if not isinstance(pointer, str):
            raise ReviewProgramError("trusted review verification evidence is invalid")
        result = _bound_object(gate_root, pointer, "trusted final result")
        if (
            result.get("axis") != axis
            or result.get("verdict") != "approve"
            or result.get("verification_iteration") != count
        ):
            raise ReviewProgramError("trusted review verification evidence is invalid")


def resolved_terminal_head(
    root: Path,
    gate_root: Path,
    gate: Mapping[str, object],
    boundary: ReviewBoundaryInput,
    operation_id: str,
) -> str:
    """Bind a moved implementation HEAD to material rounds and exact Git deltas."""

    context = gate.get("context")
    if not isinstance(context, dict):
        raise ReviewProgramError("trusted review gate identity is stale")
    terminal_head = str(context.get("head_sha") or "")
    reviewed_head = boundary.product_head_sha or boundary.integration_head_sha
    if not reviewed_head or terminal_head == reviewed_head:
        return terminal_head
    if boundary.purpose != "implementation":
        raise ReviewProgramError("trusted review gate HEAD is stale")

    meta = _object(gate_root / ".review-meta.json", "trusted review metadata")
    entries = meta.get("resolution_evidence")
    gate_entries = gate.get("resolution_evidence")
    if (
        meta.get("schema_version") != 1
        or meta.get("operation_id") != operation_id
        or meta.get("worktree") != str(root)
        or meta.get("head_sha") != terminal_head
        or meta.get("review_boundary_input_sha256") != boundary.input_sha256
        or not isinstance(entries, list)
        or not entries
        or len(entries) > 10
        or not isinstance(gate_entries, dict)
    ):
        raise ReviewProgramError("trusted review resolution evidence is invalid")

    pointers: set[str] = set()
    expected_keys: set[str] = set()
    terminal_by_axis: dict[str, str] = {}
    chains: dict[str, list[tuple[str, str]]] = {}
    material_by_transition: dict[tuple[str, str], int] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"pointer", "sha256"}:
            raise ReviewProgramError("trusted review resolution evidence is invalid")
        pointer = str(entry.get("pointer") or "")
        match = re.fullmatch(
            rf"{re.escape(operation_id)}/resolution-(holistic|spec|standards)-(\d+)\.json",
            pointer,
        )
        path = (gate_root / pointer).resolve()
        digest = str(entry.get("sha256") or "")
        if (
            match is None
            or pointer in pointers
            or gate_root not in path.parents
            or not path.is_file()
            or path.is_symlink()
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise ReviewProgramError("trusted review resolution evidence is invalid")
        try:
            evidence = validate_resolution_evidence(
                _object(path, "trusted review resolution evidence")
            )
        except ResolutionError as exc:
            raise ReviewProgramError(
                "trusted review resolution evidence is invalid"
            ) from exc
        short, raw_iteration = match.groups()
        iteration = int(raw_iteration)
        axis = evidence.axis
        chain = chains.setdefault(axis, [])
        if (
            evidence.operation_id != operation_id
            or _AXIS_SHORT.get(axis) != short
            or iteration != len(chain)
            or evidence.reviewed_head_sha
            != terminal_by_axis.get(axis, reviewed_head)
        ):
            raise ReviewProgramError("trusted review resolution chain is stale")
        material_ids = _material_ids(gate_root, operation_id, axis, iteration)
        if evidence.previous_finding_ids != material_ids:
            raise ReviewProgramError("trusted review resolution findings are stale")
        if evidence.fix_delta_sha256 != _fix_delta_sha256(
            root, evidence.reviewed_head_sha, evidence.resolved_head_sha
        ):
            raise ReviewProgramError("trusted review fix delta is stale")
        transition = (evidence.reviewed_head_sha, evidence.resolved_head_sha)
        material_by_transition[transition] = (
            material_by_transition.get(transition, 0) + len(material_ids)
        )
        chain.append(transition)
        terminal_by_axis[axis] = evidence.resolved_head_sha
        pointers.add(pointer)
        expected_keys.add(f"{axis}:{iteration}")

    chain_values = list(chains.values())
    if (
        not chain_values
        or any(chain != chain_values[0] for chain in chain_values[1:])
        or any(count == 0 for count in material_by_transition.values())
        or set(gate_entries) != expected_keys
        or {str(value) for value in gate_entries.values()} != pointers
        or any(head != terminal_head for head in terminal_by_axis.values())
    ):
        raise ReviewProgramError("trusted review resolution chain is stale")
    _require_final_iterations(
        gate_root,
        gate,
        {axis: len(chain) for axis, chain in chains.items()},
    )
    return terminal_head
