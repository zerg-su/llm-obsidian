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
    _git_bytes,
    _load_review_boundary_input,
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
    explicit_artifact_paths: Mapping[str, str]

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


def _regular_file_identity(
    root: Path,
    source: Path,
    label: str,
    *,
    lexical_root: Path | None = None,
) -> tuple[Path, tuple[int, int]]:
    supplied_root = (lexical_root or root).expanduser().absolute()
    lexical = source.expanduser().absolute()
    try:
        relative = lexical.relative_to(supplied_root)
    except ValueError:
        raise _failure(f"{label} pointer is unavailable")
    if not relative.parts:
        raise _failure(f"{label} pointer is unavailable")
    cursor = supplied_root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise _failure(f"{label} pointer is unavailable")
    target = lexical.resolve()
    if target == root or root not in target.parents or not target.is_file():
        raise _failure(f"{label} pointer is unavailable")
    stat = target.stat()
    return target, (stat.st_dev, stat.st_ino)


def _relative_file(
    root: Path, value: str, label: str
) -> tuple[tuple[int, int], bytes]:
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
    target, identity = _regular_file_identity(root, root / value, label)
    return identity, target.read_bytes()


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
    explicit_identities: dict[str, tuple[int, int]] = {}
    explicit_paths: dict[str, str] = {}
    plan_stat = plan.stat()
    plan_identity = (plan_stat.st_dev, plan_stat.st_ino)
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
            identity, content = _relative_file(root, pointer, name)
            explicit_identities[name] = identity
            explicit_paths[name] = pointer
            protected[name] = content
    if len(set(explicit_identities.values())) != len(explicit_identities):
        raise _failure("explicit protected artifacts overlap")
    if plan_identity in explicit_identities.values():
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
        explicit_paths,
    )


def compile_plan_review(
    worktree: Path,
    plan_file: Path,
    *,
    capability_dispositions: str = "",
    success_evidence_map: str = "",
) -> PlanReviewCompilation:
    supplied_root = worktree.expanduser().absolute()
    root = supplied_root.resolve()
    if not root.is_dir():
        raise _failure("plan must be one exact regular file inside the checkout")
    source = plan_file.expanduser()
    try:
        plan, _identity = _regular_file_identity(
            root, source, "plan", lexical_root=supplied_root
        )
    except PlanReviewError as exc:
        raise _failure("plan must be one exact regular file inside the checkout") from exc
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
        if len(parents) != 2 or changed != {compilation.plan_relative_path}:
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
    repository_sources = {
        "plan": (
            compilation.plan_relative_path,
            compilation.plan_sha256,
        ),
        **{
            name: (relative, compilation.artifact_sha256[name])
            for name, relative in compilation.explicit_artifact_paths.items()
        },
    }
    for label, (relative, expected_sha256) in repository_sources.items():
        try:
            committed = _git_bytes(root, "show", f"{head}:{relative}")
        except TaskReviewError as exc:
            raise PlanReviewError(
                BASE_INVALID,
                f"{label} is not available at the exact review HEAD",
            ) from exc
        if hashlib.sha256(committed).hexdigest() != expected_sha256:
            raise PlanReviewError(
                BASE_INVALID,
                f"{label} working bytes are not bound to the exact review HEAD",
            )
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


def protected_artifact_changes(
    reviewed: PlanReviewCompilation,
    resolved: PlanReviewCompilation,
) -> tuple[str, ...]:
    """Return the frozen artifact names whose semantic bytes changed."""

    return tuple(
        name
        for name in (
            "outcome",
            "capability_dispositions",
            "success_evidence",
        )
        if reviewed.artifact_sha256[name] != resolved.artifact_sha256[name]
    )


def guard_active_protected_artifacts(
    runtime_root: Path,
    candidate: Mapping[str, Any],
    compilation: PlanReviewCompilation,
) -> None:
    """Reject protected drift until an external amendment opens a fresh boundary."""

    boundary_path = Path(
        str(candidate.get("review_boundary_input_file") or "")
    ).resolve()
    expected = (runtime_root / "inputs/review-boundary-input.json").resolve()
    if boundary_path != expected:
        raise PlanReviewError(
            PROTECTED_CHANGED,
            "active plan boundary pointer is outside exact review scratch",
        )
    old = _load_review_boundary_input(boundary_path, purpose="intent")
    current = compilation.boundary()
    changed = tuple(
        name
        for name, before, after in (
            (
                "outcome",
                old.outcome_contract_sha256,
                current.outcome_contract_sha256,
            ),
            (
                "capability_dispositions",
                old.capability_dispositions_sha256,
                current.capability_dispositions_sha256,
            ),
            (
                "success_evidence",
                old.success_evidence_map_sha256,
                current.success_evidence_map_sha256,
            ),
        )
        if before != after
    )
    if changed:
        raise PlanReviewError(
            PROTECTED_CHANGED,
            "protected plan artifacts require an amendment and fresh boundary: "
            + ", ".join(changed),
        )


def validate_design_rebind(
    worktree: Path,
    reviewed: PlanReviewCompilation,
    resolved: PlanReviewCompilation,
    *,
    reviewed_head: str,
    resolved_head: str,
) -> dict[str, object]:
    """Prove one exact plan-only Git delta changed only the design subject."""

    root = worktree.expanduser().resolve()
    reviewed_head = _exact_commit(root, reviewed_head, "reviewed HEAD")
    resolved_head = _exact_commit(root, resolved_head, "resolved HEAD")
    if (
        reviewed.worktree != root
        or resolved.worktree != root
        or reviewed.plan_relative_path != resolved.plan_relative_path
        or reviewed.plan_path != resolved.plan_path
        or reviewed_head == resolved_head
    ):
        raise PlanReviewError(PROTECTED_CHANGED, "plan rebind identity is invalid")
    protected = protected_artifact_changes(reviewed, resolved)
    if protected:
        raise PlanReviewError(
            PROTECTED_CHANGED,
            "protected plan artifacts changed: " + ", ".join(protected),
        )
    if reviewed.artifact_sha256["design"] == resolved.artifact_sha256["design"]:
        raise PlanReviewError(PROTECTED_CHANGED, "plan design did not change")
    relative = reviewed.plan_relative_path
    try:
        reviewed_plan = _git_bytes(root, "show", f"{reviewed_head}:{relative}")
        resolved_plan = _git_bytes(root, "show", f"{resolved_head}:{relative}")
        changed_paths = list(
            filter(
                None,
                _git(
                    root,
                    "diff",
                    "--name-only",
                    reviewed_head,
                    resolved_head,
                    "--",
                ).splitlines(),
            )
        )
        delta = _git_bytes(
            root,
            "diff",
            "--binary",
            "--no-ext-diff",
            reviewed_head,
            resolved_head,
            "--",
            relative,
        )
    except TaskReviewError as exc:
        raise PlanReviewError(PROTECTED_CHANGED, "exact plan Git delta is unavailable") from exc
    if (
        hashlib.sha256(reviewed_plan).hexdigest() != reviewed.plan_sha256
        or hashlib.sha256(resolved_plan).hexdigest() != resolved.plan_sha256
        or changed_paths != [relative]
        or not delta
    ):
        raise PlanReviewError(
            PROTECTED_CHANGED,
            "plan rebind must contain one exact plan-only Git delta",
        )
    return {
        "reviewed_head_sha": reviewed_head,
        "resolved_head_sha": resolved_head,
        "reviewed_plan_sha256": reviewed.plan_sha256,
        "resolved_plan_sha256": resolved.plan_sha256,
        "changed_paths": changed_paths,
        "git_delta_sha256": hashlib.sha256(delta).hexdigest(),
    }


def rebind_active_plan_review(
    worktree: Path,
    active_path: Path,
    candidate: Mapping[str, Any],
    gate_state: Mapping[str, Any],
    requested_policy: Mapping[str, Any],
    compilation: PlanReviewCompilation,
    *,
    requested_base_sha: str,
    requested_head_sha: str,
) -> dict[str, Any]:
    """Atomically replace only a validated design subject on retained lanes."""

    plan_meta = candidate.get("plan_review")
    context = gate_state.get("context")
    if (
        gate_state.get("status") != "awaiting-resolution"
        or not isinstance(plan_meta, Mapping)
        or not isinstance(context, Mapping)
    ):
        raise PlanReviewError(
            PROTECTED_CHANGED,
            "a changed plan boundary requires awaiting retained lanes",
        )
    task_id = str(candidate.get("task_id") or "")
    reviewed_head = str(context.get("head_sha") or "")
    original_base = str(plan_meta.get("base_sha") or "")
    relative = str(plan_meta.get("plan_relative_path") or "")
    runtime_root = Path(str(candidate.get("runtime_root") or "")).resolve()
    if (
        requested_base_sha != reviewed_head
        or requested_head_sha != _git(worktree, "rev-parse", "HEAD")
        or str(plan_meta.get("head_sha") or "") != reviewed_head
        or relative != compilation.plan_relative_path
        or runtime_root == worktree
        or worktree in runtime_root.parents
    ):
        raise PlanReviewError(PROTECTED_CHANGED, "plan rebind OID identity is stale")
    boundary_path = Path(
        str(candidate.get("review_boundary_input_file") or "")
    ).resolve()
    if boundary_path != (
        runtime_root / "inputs/review-boundary-input.json"
    ).resolve():
        raise PlanReviewError(
            PROTECTED_CHANGED,
            "plan rebind boundary pointer is outside exact review scratch",
        )
    old_boundary = _load_review_boundary_input(boundary_path, purpose="intent")
    old_artifacts = {
        "outcome": b"",
        "design": (runtime_root / ARTIFACT_PATHS["design"]).read_bytes(),
        "capability_dispositions": (
            runtime_root / ARTIFACT_PATHS["capability_dispositions"]
        ).read_bytes(),
        "success_evidence": (
            runtime_root / ARTIFACT_PATHS["success_evidence"]
        ).read_bytes(),
    }
    reviewed = PlanReviewCompilation(
        worktree.expanduser().resolve(),
        compilation.plan_path,
        relative,
        old_boundary.plan_sha256,
        old_artifacts,
        {
            "outcome": old_boundary.outcome_contract_sha256,
            "design": old_boundary.design_sha256,
            "capability_dispositions": old_boundary.capability_dispositions_sha256,
            "success_evidence": old_boundary.success_evidence_map_sha256,
        },
        {},
    )
    delta = validate_design_rebind(
        worktree,
        reviewed,
        compilation,
        reviewed_head=reviewed_head,
        resolved_head=requested_head_sha,
    )
    resolution = _read_json(
        worktree / ".task-review-resolution.json",
        "plan review resolution",
    )
    if (
        resolution.get("schema_version") != 1
        or resolution.get("operation_id") != task_id
        or resolution.get("reviewed_head_sha") != reviewed_head
        or resolution.get("resolved_head_sha") != requested_head_sha
    ):
        raise PlanReviewError(
            PROTECTED_CHANGED,
            "typed plan resolution does not bind the exact reviewed/resolved OIDs",
        )
    new_boundary = materialize_plan_review(
        runtime_root,
        compilation,
        base_sha=original_base,
        head_sha=requested_head_sha,
    )
    updated = dict(candidate)
    updated["review_policy"] = dict(requested_policy)
    updated["plan_review"] = {
        **dict(plan_meta),
        "head_sha": requested_head_sha,
        **delta,
    }
    _atomic_json(boundary_path, new_boundary.payload())
    _atomic_json(runtime_root / "current-review.json", updated)
    _atomic_json(active_path, updated)
    return updated


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
    "guard_active_protected_artifacts",
    "materialize_plan_review",
    "protected_artifact_changes",
    "rebind_active_plan_review",
    "resolve_plan_oids",
    "review_inspection_commands",
    "run_plan_review",
    "validate_design_rebind",
)
