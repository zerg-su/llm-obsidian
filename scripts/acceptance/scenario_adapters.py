"""Scenario-owned shared cleanup and proof contracts."""

from __future__ import annotations

import hashlib
import re
import subprocess
import uuid
from pathlib import Path

from .contracts import AcceptanceRunnerError, read_json
from .sandbox import git_output

DISPOSABLE_VAULT_BOOKKEEPING = {
    ".raw/.manifest.json", ".vault-meta/address-counter.txt",
    ".vault-meta/address-map.tsv", ".vault-meta/index.jsonl",
    ".vault-meta/recent.jsonl", ".vault-meta/session-to-pages.jsonl",
    ".vault-meta/tag-index.json", "wiki/hot.md", "wiki/log.md",
}

def is_disposable_bookkeeping(path: str, status: str) -> bool:
    if status.startswith("??"):
        # A fresh ingestion manifest is derived provenance inside the disposable
        # clone. Product pages and raw sources are still rejected independently.
        return path == ".raw/.manifest.json"
    return path in DISPOSABLE_VAULT_BOOKKEEPING or re.fullmatch(
        r"wiki(?:/[^/]+)*/_index\.md", path
    ) is not None

def sandbox_cleanup_proof(sandbox: Path, commit: str) -> tuple[bool, str]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if head.returncode != 0 or head.stdout.strip() != commit:
        return False, "disposable clone HEAD changed"
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if status.returncode != 0:
        return False, "disposable clone status is unreadable"
    dirty = []
    bookkeeping = []
    for line in status.stdout.splitlines():
        path = line[3:]
        if path == ".acceptance-sandbox.json":
            continue
        if path.startswith(".vault-meta/acceptance-worktrees/"):
            continue
        if is_disposable_bookkeeping(path, line[:2]):
            bookkeeping.append(path)
            continue
        dirty.append(line)
    if dirty:
        return False, "disposable clone retained product or vault changes"
    if bookkeeping:
        return True, "product outputs removed; only disposable vault bookkeeping remains"
    return True, "committed HEAD and worktree restored"

def lifecycle_acceptance_cleanup_proof(sandbox: Path, commit: str) -> tuple[bool, str]:
    """Allow only product pages durably bound to one disposable task lifecycle."""

    ok, head = git_output(sandbox, "rev-parse", "HEAD")
    if not ok or head.strip() != commit:
        return False, "disposable lifecycle clone HEAD changed"
    worktree_root = sandbox / ".vault-meta" / "acceptance-worktrees"
    if worktree_root.is_symlink() or not worktree_root.is_dir():
        return False, "disposable lifecycle worktree root is missing"
    allowed_pages: set[str] = set()

    def add_page(raw: object) -> bool:
        value = str(raw or "").strip()
        if not value:
            return True
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = sandbox / candidate
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(sandbox.resolve())
        except ValueError:
            return False
        if (
            not relative.parts
            or relative.parts[0] != "wiki"
            or relative.suffix != ".md"
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            return False
        allowed_pages.add(relative.as_posix())
        return True

    task_roots: list[Path] = []
    for task_worktree in sorted(worktree_root.iterdir()):
        if task_worktree.is_symlink() or not task_worktree.is_dir():
            return False, "disposable lifecycle worktree identity is invalid"
        try:
            meta = read_json(task_worktree / ".task-meta.json")
        except AcceptanceRunnerError as exc:
            return False, str(exc)
        target_repo = str(meta.get("target_repo") or "").strip()
        if (
            meta.get("version") != 3
            or Path(str(meta.get("vault_root") or "")).resolve() != sandbox.resolve()
            or (target_repo and Path(target_repo).resolve() != sandbox.resolve())
            or not add_page(meta.get("plan_file"))
        ):
            return False, "disposable lifecycle task metadata is not bound to this clone"
        project_id = str(meta.get("project_id") or "")
        task_id = str(meta.get("task_id") or "")
        try:
            if str(uuid.UUID(project_id)) != project_id or str(uuid.UUID(task_id)) != task_id:
                raise ValueError
        except ValueError:
            return False, "disposable lifecycle task identity is invalid"
        task_root = (
            sandbox / ".vault-meta" / "task-sessions" / "projects" / project_id
            / "tasks" / task_id
        )
        if task_root.is_symlink() or not task_root.is_dir():
            return False, "disposable lifecycle task registry is missing"
        task_roots.append(task_root.resolve())
        for name in (".task-reap-prepared.json", ".task-reap-complete.json"):
            artifact = task_worktree / name
            if not artifact.is_file():
                continue
            try:
                value = read_json(artifact)
            except AcceptanceRunnerError as exc:
                return False, str(exc)
            if not add_page(value.get("plan_path")) or not add_page(value.get("result_path")):
                return False, "disposable reap artifact escapes the coordinator wiki"
            if not add_page(value.get("review_archive_path")):
                return False, "disposable review archive escapes the coordinator wiki"
            archives = value.get("review_archives", [])
            if not isinstance(archives, list):
                return False, "disposable reap archive list is invalid"
            for archive in archives:
                if not isinstance(archive, dict) or not add_page(archive.get("path")):
                    return False, "disposable reap archive path is invalid"
    if not task_roots:
        return False, "disposable lifecycle task registry is empty"
    for task_root in task_roots:
        for marker_path in task_root.glob("lanes/*/operations/*/.review-archive.json"):
            try:
                marker = read_json(marker_path)
            except AcceptanceRunnerError as exc:
                return False, str(exc)
            if not add_page(marker.get("path")):
                return False, "disposable review marker path is invalid"
            archive = sandbox / str(marker.get("path") or "")
            digest = str(marker.get("content_sha256") or "")
            if archive.exists() and (
                archive.is_symlink()
                or not archive.is_file()
                or digest != hashlib.sha256(archive.read_bytes()).hexdigest()
            ):
                return False, "disposable review archive changed after its durable marker"

    ok, status = git_output(sandbox, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if not ok:
        return False, "disposable lifecycle clone status is unreadable"
    unexpected: list[str] = []
    retained_pages = 0
    for entry in status.split("\0"):
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        if path == ".acceptance-sandbox.json" or path.startswith(
            ".vault-meta/acceptance-worktrees/"
        ):
            continue
        if is_disposable_bookkeeping(path, code):
            continue
        if path in allowed_pages and code == "??":
            page = sandbox / path
            if page.is_symlink() or not page.is_file():
                return False, "bound disposable lifecycle page is not a regular file"
            retained_pages += 1
            continue
        unexpected.append(path)
    if unexpected:
        return False, "disposable clone retained product or vault changes: " + ", ".join(unexpected[:5])
    return True, f"exact lifecycle state retained {retained_pages} bound page(s) for clone cleanup"

def daily_acceptance_cleanup(sandbox: Path, commit: str) -> tuple[bool, str]:
    """Accept one exact disposable session-evidence commit deleted after proof."""

    count = subprocess.run(
        ["git", "rev-list", "--count", f"{commit}..HEAD"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if count.returncode != 0 or count.stdout.strip() != "1":
        return False, "daily fixture must create exactly one local evidence commit"
    changed = subprocess.run(
        ["git", "diff", "--name-status", "-z", commit, "HEAD"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    parts = changed.stdout.rstrip("\0").split("\0") if changed.returncode == 0 else []
    if len(parts) != 2 or parts[0] != "A":
        return False, "daily evidence commit changed unexpected paths"
    path = parts[1]
    if re.fullmatch(r"wiki/meta/sessions/Acceptance[^/]*\.md", path) is None:
        return False, "daily evidence commit used an unexpected fixture path"
    if (sandbox / path).exists():
        return False, "daily fixture session was not removed after verification"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if head.returncode != 0:
        return False, "daily fixture HEAD is unreadable"
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, head.stdout.strip()],
        cwd=sandbox, text=True, capture_output=True, check=False,
    )
    if ancestry.returncode != 0:
        return False, "daily fixture commit is not based on the release candidate"
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--", path],
        cwd=sandbox, text=True, capture_output=True, check=False,
    )
    deletion = status.stdout.strip("\n")
    if status.returncode != 0 or not deletion.startswith(" D "):
        return False, "daily fixture deletion is not independently proven"
    restored = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=sandbox,
        text=True, capture_output=True, check=False,
    )
    if restored.returncode != 0:
        return False, "disposable clone status is unreadable"
    dirty = []
    for line in restored.stdout.splitlines():
        candidate = line[3:]
        if line == deletion or candidate == ".acceptance-sandbox.json":
            continue
        if candidate.startswith(".vault-meta/acceptance-worktrees/"):
            continue
        if is_disposable_bookkeeping(candidate, line[:2]):
            continue
        dirty.append(line)
    if dirty:
        return False, "disposable clone retained product or vault changes"
    return True, "one bounded daily evidence commit verified and product outputs removed"
