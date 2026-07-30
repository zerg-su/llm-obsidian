"""Exact conflicted-worktree evidence and authorization contract."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from ..git_ops import ConflictEvidence, GitError


@dataclass(frozen=True)
class ConflictRequest:
    worktree: str
    operation: str
    conflicts: tuple[str, ...]
    stage_authorized: bool = False
    continue_authorized: bool = False
    abort_authorized: bool = False

    def __post_init__(self) -> None:
        if not self.worktree.startswith("/") or self.operation not in {"merge", "rebase", "cherry-pick"}:
            raise ValueError("conflict request must bind one exact operation/worktree")
        if not self.conflicts or any(path.startswith("/") or ".." in path.split("/") for path in self.conflicts):
            raise ValueError("conflict paths must be non-empty and worktree-relative")
        if sum((self.continue_authorized, self.abort_authorized)) > 1:
            raise ValueError("continue and abort cannot both be authorized")


@dataclass(frozen=True)
class ConflictResolution:
    operation: str
    conflicts: tuple[str, ...]
    proposed_stage: tuple[str, ...]
    verification_passed: bool
    staged: bool = False
    continued: bool = False
    attention: str = ""
    base: str = ""
    ours: str = ""
    theirs: str = ""


class ConflictGit(Protocol):
    root: Path

    def stage_exact(
        self, paths: tuple[str, ...], *, authorized: bool = False
    ) -> None: ...

    def continue_operation(
        self, operation: str, *, authorized: bool = False
    ) -> None: ...

    def conflict_evidence(self) -> ConflictEvidence: ...


def _contains_marker(path: Path) -> bool:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return any(marker in content for marker in ("<<<<<<<", "=======", ">>>>>>>"))


def resolve_conflict(
    request: ConflictRequest,
    git: ConflictGit,
    resolved_paths: tuple[str, ...],
    *,
    verification_passed: bool,
    marker_probe: Callable[[Path], bool] = _contains_marker,
) -> ConflictResolution:
    """Gather exact evidence, then mutate only under explicit authorization."""

    exact_root = Path(request.worktree).expanduser().resolve()
    if git.root.expanduser().resolve() != exact_root:
        raise GitError("conflict adapter is bound to another worktree")
    evidence = git.conflict_evidence()
    if evidence.operation != request.operation:
        raise GitError("conflict operation changed")
    if tuple(sorted(evidence.conflicts)) != tuple(sorted(request.conflicts)):
        raise GitError("conflict path set changed")
    proposed = tuple(sorted(set(resolved_paths)))
    if proposed != tuple(sorted(request.conflicts)):
        raise GitError("resolution must cover the exact conflict path set")
    result = ConflictResolution(
        request.operation,
        tuple(sorted(request.conflicts)),
        proposed,
        verification_passed,
        base=evidence.base,
        ours=evidence.ours,
        theirs=evidence.theirs,
    )
    if not request.stage_authorized:
        return result
    if not verification_passed:
        return ConflictResolution(
            result.operation,
            result.conflicts,
            result.proposed_stage,
            False,
            attention="verification-failed",
            base=evidence.base,
            ours=evidence.ours,
            theirs=evidence.theirs,
        )
    if any(marker_probe(exact_root / path) for path in proposed):
        return ConflictResolution(
            result.operation,
            result.conflicts,
            result.proposed_stage,
            True,
            attention="conflict-markers-remain",
            base=evidence.base,
            ours=evidence.ours,
            theirs=evidence.theirs,
        )
    git.stage_exact(proposed, authorized=True)
    continued = False
    if request.continue_authorized:
        git.continue_operation(request.operation, authorized=True)
        continued = True
    return ConflictResolution(
        result.operation,
        result.conflicts,
        result.proposed_stage,
        True,
        staged=True,
        continued=continued,
        base=evidence.base,
        ours=evidence.ours,
        theirs=evidence.theirs,
    )
