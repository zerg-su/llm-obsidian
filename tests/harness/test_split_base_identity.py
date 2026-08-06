#!/usr/bin/env python3
"""Split ownership is sealed to one exact ancestor commit."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from dispatch_contracts import resolve_base_commit  # noqa: E402
import dispatch_workspace  # noqa: E402
from harness.split_contracts import ParentContract  # noqa: E402
from harness.split_evidence import child_head_descends_from_base  # noqa: E402


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


with tempfile.TemporaryDirectory(prefix="split-base-identity.") as raw:
    repo = Path(raw) / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "split@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Split Test"], cwd=repo, check=True)
    (repo / "owned.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "owned.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    base = resolve_base_commit(repo, "main")
    (repo / "owned.txt").write_text("child\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "child"], cwd=repo, check=True, capture_output=True)
    child = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "branch", "-f", "moved-base", child], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "moved-base"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-D", "main"], cwd=repo, check=True, capture_output=True)
    check(
        "sealed base survives branch movement and deletion",
        resolve_base_commit(repo, base) == base
        and child_head_descends_from_base(repo, base, child),
    )

    subprocess.run(["git", "checkout", "--orphan", "unrelated"], cwd=repo, check=True, capture_output=True)
    (repo / "owned.txt").write_text("other\n", encoding="utf-8")
    subprocess.run(["git", "add", "owned.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "other"], cwd=repo, check=True, capture_output=True)
    unrelated = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    check(
        "unrelated history cannot satisfy Split ancestry",
        not child_head_descends_from_base(repo, base, unrelated),
    )

parent = ParentContract(
    plan_sha256="a" * 64,
    outcome_contract_sha256="b" * 64,
    base_sha="1" * 40,
    evidence_ids=("split-base",),
    non_goals=("No mutable base refs.",),
)
check("Split parent contract carries an exact base SHA", parent.base_sha == "1" * 40)


class RecordingGit:
    calls: list[tuple[Path, Path, str, str]] = []

    def __init__(self, target_repo: Path) -> None:
        self.target_repo = target_repo

    def create_worktree(self, worktree: Path, branch: str, base: str) -> None:
        self.calls.append((self.target_repo, worktree, branch, base))


original_git = dispatch_workspace.GitAdapter
dispatch_workspace.GitAdapter = RecordingGit
try:
    dispatch_workspace.create_worktree(
        {
            "target_repo": Path("/repo"),
            "worktree": Path("/worktree"),
            "branch": "task/child",
            "base_branch": "main",
            "base_sha": "2" * 40,
        }
    )
finally:
    dispatch_workspace.GitAdapter = original_git
check(
    "worktree creation consumes the sealed SHA, not the diagnostic branch",
    RecordingGit.calls == [
        (Path("/repo"), Path("/worktree"), "task/child", "2" * 40)
    ],
)

print("\nAll sealed Split base identity tests passed.")
