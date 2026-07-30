"""Constrained git/worktree adapter: no implicit broad stage, push, or abort."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence


class GitError(RuntimeError):
    pass


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class GitSnapshot:
    root: Path
    head: str
    base: str
    dirty_paths: tuple[str, ...]
    conflicts: tuple[str, ...]
    operation: str = ""


@dataclass(frozen=True)
class GitIsolation:
    root: Path
    common_dir: Path
    branch: str
    detached: bool


@dataclass(frozen=True)
class ConflictEvidence:
    operation: str
    conflicts: tuple[str, ...]
    base: str
    ours: str
    theirs: str


_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")


class GitAdapter:
    def __init__(self, root: Path | str, runner: Runner = subprocess.run):
        self.root = Path(root).expanduser().resolve()
        self.runner = runner

    def _result(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if args and args[0] in {"push", "reset", "clean"}:
            raise GitError(f"git {args[0]} is outside the harness adapter")
        return self.runner(
            ["git", *args],
            cwd=cwd or self.root,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    def _run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        result = self._result(args, cwd=cwd, env=env)
        if result.returncode:
            raise GitError((result.stderr or result.stdout).strip()[:2000])
        return result.stdout.strip()

    @staticmethod
    def _paths(porcelain: str) -> tuple[str, ...]:
        paths: list[str] = []
        for line in porcelain.splitlines():
            if not line:
                continue
            value = line[3:] if len(line) > 3 else line
            if " -> " in value:
                value = value.split(" -> ", 1)[1]
            paths.append(value)
        return tuple(sorted(set(paths)))

    def isolation(self) -> GitIsolation:
        top = Path(self._run(["rev-parse", "--show-toplevel"])).resolve()
        if top != self.root:
            raise GitError(
                f"GitAdapter root must be exact worktree root: {self.root} != {top}"
            )
        raw_common = Path(self._run(["rev-parse", "--git-common-dir"]))
        common = (
            raw_common if raw_common.is_absolute() else self.root / raw_common
        ).resolve()
        branch_result = self._result(
            ["symbolic-ref", "--quiet", "--short", "HEAD"]
        )
        branch = (
            branch_result.stdout.strip()
            if branch_result.returncode == 0
            else ""
        )
        return GitIsolation(top, common, branch, not bool(branch))

    def _operation(self) -> str:
        for operation, marker in (
            ("rebase", "rebase-merge"),
            ("rebase", "rebase-apply"),
            ("merge", "MERGE_HEAD"),
            ("cherry-pick", "CHERRY_PICK_HEAD"),
        ):
            result = self._result(["rev-parse", "--git-path", marker])
            raw = result.stdout.strip()
            if result.returncode or not raw:
                continue
            path = Path(raw)
            if not path.is_absolute():
                path = self.root / path
            if path.exists():
                return operation
        return ""

    def inspect(self, base: str = "HEAD") -> GitSnapshot:
        head = self._run(["rev-parse", "HEAD"])
        resolved_base = self._run(["merge-base", head, base])
        dirty = self._paths(self._run(["status", "--porcelain"]))
        conflicts = tuple(
            sorted(
                line
                for line in self._run(
                    ["diff", "--name-only", "--diff-filter=U"]
                ).splitlines()
                if line
            )
        )
        return GitSnapshot(
            self.root, head, resolved_base, dirty, conflicts, self._operation()
        )

    def base_head(self, base: str) -> tuple[str, str]:
        snapshot = self.inspect(base)
        return snapshot.base, snapshot.head

    def diff_package(self, base: str, output: Path) -> Path:
        content = self._run(["diff", "--binary", f"{base}...HEAD"])
        output = output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content + ("\n" if content else ""), encoding="utf-8")
        return output

    def create_worktree(self, target: Path, branch: str, base: str) -> GitIsolation:
        target = target.expanduser().resolve()
        if target.exists() or self.root == target or self.root in target.parents:
            raise GitError("owned worktree target must be a new external exact path")
        source = self.isolation()
        if (
            not _BRANCH_RE.fullmatch(branch)
            or branch in {"main", "master", source.branch}
            or ".." in branch
            or branch.endswith(("/", "."))
        ):
            raise GitError("owned worktree requires a new non-main exact branch")
        if self._result(["check-ref-format", "--branch", branch]).returncode:
            raise GitError("owned worktree branch is invalid")
        if not self._result(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
        ).returncode:
            raise GitError(f"owned worktree branch already exists: {branch}")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._run(["worktree", "add", "-b", branch, str(target), base])
        child = GitAdapter(target, self.runner).isolation()
        raw_git_dir = Path(
            self._run(["rev-parse", "--git-dir"], cwd=target)
        )
        git_dir = (
            raw_git_dir if raw_git_dir.is_absolute() else target / raw_git_dir
        ).resolve()
        marker = git_dir / "llm-obsidian-harness-owner.json"
        marker.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_root": str(self.root),
                    "target": str(target),
                    "branch": branch,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        return child

    def stage_exact(
        self, paths: Sequence[str], *, authorized: bool = False
    ) -> None:
        if not authorized:
            raise GitError("stage_exact requires explicit authorization")
        normalized: list[str] = []
        for path in paths:
            if not isinstance(path, str) or not path or "\\" in path:
                raise GitError("stage_exact requires non-empty repo-relative paths")
            candidate = PurePosixPath(path)
            segments = path.split("/")
            if (
                candidate.is_absolute()
                or candidate.as_posix() != path
                or any(segment in {"", ".", ".."} for segment in segments)
                or path.startswith(":")
                or path.endswith("/")
                or any(token in path for token in ("*", "?", "["))
            ):
                raise GitError("stage_exact rejects broad or escaping pathspecs")
            normalized.append(path)
        if not normalized:
            raise GitError("stage_exact requires non-empty repo-relative paths")
        self._run(["add", "--", *normalized])

    def conflict_evidence(self) -> ConflictEvidence:
        snapshot = self.inspect("HEAD")
        if not snapshot.operation or not snapshot.conflicts:
            raise GitError("exact worktree is not in a supported conflict operation")
        theirs_ref = {
            "merge": "MERGE_HEAD",
            "cherry-pick": "CHERRY_PICK_HEAD",
            "rebase": "REBASE_HEAD",
        }[snapshot.operation]
        ours = self._run(["rev-parse", "HEAD"])
        theirs = self._run(["rev-parse", theirs_ref])
        base = self._run(["merge-base", ours, theirs])
        return ConflictEvidence(
            snapshot.operation,
            snapshot.conflicts,
            base,
            ours,
            theirs,
        )

    def continue_operation(
        self, operation: str, *, authorized: bool = False
    ) -> None:
        if not authorized:
            raise GitError("continue requires explicit authorization")
        if operation not in {"merge", "rebase", "cherry-pick"}:
            raise GitError("unsupported conflict operation")
        snapshot = self.inspect("HEAD")
        if snapshot.operation != operation:
            raise GitError("conflict operation changed before continue")
        if snapshot.conflicts:
            raise GitError("cannot continue while unmerged paths remain")
        env = os.environ.copy()
        env.setdefault("GIT_EDITOR", "true")
        self._run([operation, "--continue"], env=env)

    def cleanup_owned_worktree(
        self, target: Path, *, discard: bool = False
    ) -> None:
        target = target.expanduser().resolve()
        if target == self.root or self.root in target.parents or not target.is_dir():
            raise GitError("cleanup target is not an external exact worktree")
        raw_git_dir = Path(
            self._run(["rev-parse", "--git-dir"], cwd=target)
        )
        git_dir = (
            raw_git_dir if raw_git_dir.is_absolute() else target / raw_git_dir
        ).resolve()
        marker = git_dir / "llm-obsidian-harness-owner.json"
        try:
            owner = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GitError("owned worktree marker is missing or invalid") from exc
        if owner.get("source_root") != str(self.root) or owner.get(
            "target"
        ) != str(target):
            raise GitError("owned worktree marker does not match exact target")
        dirty = GitAdapter(target, self.runner).inspect("HEAD").dirty_paths
        if dirty and not discard:
            raise GitError("owned worktree is dirty; explicit discard is required")
        args = ["worktree", "remove"]
        if discard:
            args.append("--force")
        args.append(str(target))
        self._run(args)
