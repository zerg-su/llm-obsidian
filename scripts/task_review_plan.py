"""Compile and launch one fail-closed purpose=intent plan review boundary."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from harness.review_program import ReviewBoundaryInput
from outcome_contract import OutcomeContractError, extract_from_bytes
from task_review_shared import (
    TaskReviewError,
    _atomic_bytes,
    _atomic_json,
    _git,
    _read_json,
)


BOUNDARY_INVALID = "plan-review-artifact-boundary-invalid"
PROTECTED_CHANGED = "plan-review-protected-artifact-changed"
BASE_INVALID = "plan-review-base-invalid"
GIT_OID = re.compile(r"[0-9a-f]{40,64}\Z")
HEADINGS = {
    "outcome": b"## Outcome Contract",
    "capability_dispositions": b"## Capability Dispositions and Defect Ledger",
    "success_evidence": b"## Success Evidence Map",
}
ARTIFACT_PATHS = {
    "design": "inputs/plan-artifacts/design.md",
    "capability_dispositions": "inputs/plan-artifacts/capability-dispositions.md",
    "success_evidence": "inputs/plan-artifacts/success-evidence-map.md",
}


class PlanReviewError(TaskReviewError):
    """A typed pre-provider plan review rejection."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class PlanReviewCompilation:
    worktree: Path
    plan_path: Path
    plan_relative_path: str
    plan_sha256: str
    artifacts: Mapping[str, bytes]
    artifact_sha256: Mapping[str, str]

    def boundary(self) -> ReviewBoundaryInput:
        return ReviewBoundaryInput(
            purpose="intent",
            outcome_contract_sha256=self.artifact_sha256["outcome"],
            plan_sha256=self.plan_sha256,
            design_sha256=self.artifact_sha256["design"],
            design_path=ARTIFACT_PATHS["design"],
            capability_dispositions_sha256=self.artifact_sha256[
                "capability_dispositions"
            ],
            capability_dispositions_path=ARTIFACT_PATHS[
                "capability_dispositions"
            ],
            success_evidence_map_sha256=self.artifact_sha256[
                "success_evidence"
            ],
            success_evidence_map_path=ARTIFACT_PATHS["success_evidence"],
        )


def _failure(message: str) -> PlanReviewError:
    return PlanReviewError(BOUNDARY_INVALID, message)


def _relative_file(root: Path, value: str, label: str) -> tuple[str, bytes]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _failure(f"{label} pointer must be repository-relative")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != value
        or any(part in {"", "."} for part in relative.parts)
    ):
        raise _failure(f"{label} pointer must be repository-relative")
    source = root / value
    target = source.resolve()
    if (
        target == root
        or root not in target.parents
        or not target.is_file()
        or source.is_symlink()
    ):
        raise _failure(f"{label} pointer is unavailable")
    return value, target.read_bytes()


def _section_spans(raw: bytes) -> dict[str, tuple[int, int] | None]:
    lines = raw.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    result: dict[str, tuple[int, int] | None] = {}
    for name, heading in HEADINGS.items():
        matches = [
            index
            for index, line in enumerate(lines)
            if line.rstrip(b"\r\n") == heading
        ]
        if len(matches) > 1:
            raise _failure(f"{heading.decode()} heading is duplicated")
        if not matches:
            result[name] = None
            continue
        start_line = matches[0]
        end_line = next(
            (
                index
                for index in range(start_line + 1, len(lines))
                if lines[index].rstrip(b"\r\n").startswith(b"## ")
            ),
            len(lines),
        )
        start = offsets[start_line]
        end = offsets[end_line] if end_line < len(lines) else len(raw)
        if not raw[start + len(lines[start_line]) : end].strip():
            raise _failure(f"{heading.decode()} section is empty")
        result[name] = (start, end)
    return result


def _compile_bytes(
    root: Path,
    plan: Path,
    raw: bytes,
    *,
    capability_dispositions: str = "",
    success_evidence_map: str = "",
) -> PlanReviewCompilation:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _failure("plan must be UTF-8") from exc
    spans = _section_spans(raw)
    outcome_span = spans["outcome"]
    if outcome_span is None:
        raise _failure("exactly one Outcome Contract heading is required")
    try:
        complete_contract = extract_from_bytes(raw)
        section_contract = extract_from_bytes(raw[slice(*outcome_span)])
    except OutcomeContractError as exc:
        raise _failure(str(exc)) from exc
    if complete_contract.sha256 != section_contract.sha256:
        raise _failure("Outcome Contract must be contained by its exact section")

    explicit = {
        "capability_dispositions": capability_dispositions,
        "success_evidence": success_evidence_map,
    }
    protected: dict[str, bytes] = {"outcome": section_contract.canonical}
    explicit_paths: dict[str, str] = {}
    for name in ("capability_dispositions", "success_evidence"):
        span = spans[name]
        pointer = explicit[name]
        if span is not None and pointer:
            raise _failure(f"{name} is ambiguous between inline and explicit input")
        if span is None and not pointer:
            raise _failure(f"{name} requires an exact section or explicit pointer")
        if span is not None:
            protected[name] = raw[slice(*span)]
        else:
            exact, content = _relative_file(root, pointer, name)
            explicit_paths[name] = exact
            protected[name] = content
    if len(set(explicit_paths.values())) != len(explicit_paths):
        raise _failure("explicit protected artifacts overlap")
    if any(path == plan.relative_to(root).as_posix() for path in explicit_paths.values()):
        raise _failure("the plan cannot also be an explicit protected artifact")

    protected_sha = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in protected.items()
    }
    replacements = [
        (span, name)
        for name, span in spans.items()
        if span is not None
    ]
    replacements.sort(key=lambda item: item[0][0])
    if any(
        left[0][1] > right[0][0]
        for left, right in zip(replacements, replacements[1:])
    ):
        raise _failure("protected plan regions overlap")
    design_parts: list[bytes] = []
    cursor = 0
    for (start, end), name in replacements:
        design_parts.append(raw[cursor:start])
        design_parts.append(
            (
                f"<!-- plan-review-protected:{name}:sha256:"
                f"{protected_sha[name]} -->\n"
            ).encode()
        )
        cursor = end
    design_parts.append(raw[cursor:])
    for name in ("capability_dispositions", "success_evidence"):
        if spans[name] is None:
            design_parts.append(
                (
                    f"\n<!-- plan-review-protected:{name}:sha256:"
                    f"{protected_sha[name]} -->\n"
                ).encode()
            )
    artifacts = {**protected, "design": b"".join(design_parts)}
    ordered = {
        "outcome": artifacts["outcome"],
        "design": artifacts["design"],
        "capability_dispositions": artifacts["capability_dispositions"],
        "success_evidence": artifacts["success_evidence"],
    }
    digests = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in ordered.items()
    }
    return PlanReviewCompilation(
        root,
        plan,
        plan.relative_to(root).as_posix(),
        hashlib.sha256(raw).hexdigest(),
        ordered,
        digests,
    )


def compile_plan_review(
    worktree: Path,
    plan_file: Path,
    *,
    capability_dispositions: str = "",
    success_evidence_map: str = "",
) -> PlanReviewCompilation:
    root = worktree.expanduser().resolve()
    source = plan_file.expanduser()
    plan = source.resolve()
    if (
        not root.is_dir()
        or plan == root
        or root not in plan.parents
        or not plan.is_file()
        or source.is_symlink()
    ):
        raise _failure("plan must be one exact regular file inside the checkout")
    return _compile_bytes(
        root,
        plan,
        plan.read_bytes(),
        capability_dispositions=capability_dispositions,
        success_evidence_map=success_evidence_map,
    )


def _exact_commit(worktree: Path, value: str, label: str) -> str:
    if not GIT_OID.fullmatch(value):
        raise PlanReviewError(BASE_INVALID, f"{label} must be a lowercase exact Git OID")
    try:
        resolved = _git(worktree, "rev-parse", f"{value}^{{commit}}")
    except TaskReviewError as exc:
        raise PlanReviewError(BASE_INVALID, f"{label} is not a local commit") from exc
    if resolved != value:
        raise PlanReviewError(BASE_INVALID, f"{label} must be the complete commit OID")
    return value


def resolve_plan_oids(
    worktree: Path,
    compilation: PlanReviewCompilation,
    *,
    explicit_base: str = "",
) -> tuple[str, str]:
    root = worktree.expanduser().resolve()
    head = _exact_commit(root, _git(root, "rev-parse", "HEAD"), "HEAD")
    task_meta = root / ".task-meta.json"
    dispatched_base = ""
    if task_meta.is_file() and not task_meta.is_symlink():
        raw = _read_json(task_meta, "task metadata")
        candidate = str(raw.get("initial_head_sha") or "")
        if candidate:
            if Path(str(raw.get("worktree") or "")).expanduser().resolve() != root:
                raise PlanReviewError(BASE_INVALID, "dispatched plan metadata is foreign")
            dispatched_base = _exact_commit(root, candidate, "initial_head_sha")
    if dispatched_base:
        if explicit_base and explicit_base != dispatched_base:
            raise PlanReviewError(BASE_INVALID, "explicit base conflicts with dispatched authority")
        base = dispatched_base
    elif explicit_base:
        base = _exact_commit(root, explicit_base, "--base")
    else:
        parents = _git(root, "rev-list", "--parents", "-n", "1", head).split()
        changed = set(
            filter(
                None,
                _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", head).splitlines(),
            )
        )
        if len(parents) != 2 or compilation.plan_relative_path not in changed:
            raise PlanReviewError(
                BASE_INVALID,
                "current plan review requires --base unless HEAD is a single-parent commit changing the exact plan",
            )
        base = _exact_commit(root, parents[1], "derived base")
    if base == head:
        raise PlanReviewError(BASE_INVALID, "plan review base and HEAD must differ")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, head],
        cwd=root,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if ancestor.returncode != 0:
        raise PlanReviewError(BASE_INVALID, "plan review base must be an ancestor of HEAD")
    return base, head


def review_inspection_commands(worktree: Path, base: str, head: str) -> tuple[str, ...]:
    prefix = (
        str(Path(sys.executable).resolve()),
        str((worktree / "scripts/review-inspect.py").resolve()),
        "--worktree",
        str(worktree.resolve()),
    )
    return (
        shlex.join((*prefix, "status", "--expect-head", head)),
        shlex.join((*prefix, "log", "--ref", head)),
        shlex.join((*prefix, "diff", "--base", base, "--head", head, "--format", "stat")),
        shlex.join((*prefix, "commit", "--ref", head, "--format", "metadata")),
    )


def materialize_plan_review(
    runtime_root: Path,
    compilation: PlanReviewCompilation,
    *,
    base_sha: str,
    head_sha: str,
) -> ReviewBoundaryInput:
    for name, relative in ARTIFACT_PATHS.items():
        _atomic_bytes(runtime_root / relative, compilation.artifacts[name])
    boundary = compilation.boundary()
    commands = review_inspection_commands(
        compilation.worktree, base_sha, head_sha
    )
    _atomic_json(
        runtime_root / "inputs/plan-review-inspection.json",
        {
            "schema_version": 1,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "commands": list(commands),
        },
    )
    return boundary


def run_plan_review(
    worktree: Path,
    *,
    plan_file: Path,
    base: str = "",
    capability_dispositions: str = "",
    success_evidence_map: str = "",
    deep: bool = False,
    full: bool = False,
    cross_model: bool = False,
    runtime: str = "",
    model: str = "",
    effort: str = "",
    origin_surface: str = "",
    scratch_root: Path | None = None,
    runtime_manager: object | None = None,
    apply_finalizing_recovery: Any,
) -> dict[str, Any]:
    from task_review_current import run_current_review

    compilation = compile_plan_review(
        worktree,
        plan_file,
        capability_dispositions=capability_dispositions,
        success_evidence_map=success_evidence_map,
    )
    base_sha, head_sha = resolve_plan_oids(
        worktree, compilation, explicit_base=base
    )
    return run_current_review(
        worktree,
        deep=deep,
        full=full,
        cross_model=cross_model,
        runtime=runtime,
        model=model,
        effort=effort,
        purpose="intent",
        plan_file=plan_file,
        origin_surface=origin_surface,
        scratch_root=scratch_root,
        runtime_manager=runtime_manager,
        apply_finalizing_recovery=apply_finalizing_recovery,
        plan_compilation=compilation,
        plan_base_sha=base_sha,
        plan_head_sha=head_sha,
    )


__all__ = (
    "BOUNDARY_INVALID",
    "BASE_INVALID",
    "PROTECTED_CHANGED",
    "PlanReviewCompilation",
    "PlanReviewError",
    "compile_plan_review",
    "materialize_plan_review",
    "resolve_plan_oids",
    "review_inspection_commands",
    "run_plan_review",
)
