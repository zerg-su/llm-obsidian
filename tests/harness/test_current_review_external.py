#!/usr/bin/env python3
"""Integration seam for coordinator-owned review of plain external Git roots."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import task_review_current  # noqa: E402
from review_target import resolve_target  # noqa: E402
from task_review_transport import _callback_wake  # noqa: E402


spec = importlib.util.spec_from_file_location(
    "external_task_review_runner", ROOT / "scripts/task-review-runner.py"
)
assert spec is not None and spec.loader is not None
task_review_runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task_review_runner)


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def coordinator(path: Path) -> Path:
    (path / "wiki").mkdir(parents=True)
    (path / "scripts").mkdir()
    (path / "skills/review").mkdir(parents=True)
    (path / "config").mkdir()
    (path / "scripts/task-review-runner.py").write_text("# fixture\n", encoding="utf-8")
    shutil.copy2(ROOT / "skills/review/SKILL.md", path / "skills/review/SKILL.md")
    shutil.copy2(ROOT / "config/model-routing.toml", path / "config/model-routing.toml")
    shutil.copy2(ROOT / "config/verification-profiles.toml", path / "config/verification-profiles.toml")
    return path.resolve()


def product(path: Path, value: int) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "review@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "External Review Test"], cwd=path, check=True)
    (path / "app.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
    (path / "AGENTS.md").write_text("# Product instructions\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)
    return path.resolve()


with tempfile.TemporaryDirectory(prefix="external-current-review.") as raw:
    base = Path(raw)
    vault = coordinator(base / "coordinator")
    first = product(base / "products/first", 1)
    second = product(base / "products/second", 2)
    scratch = (base / "scratch").resolve()
    captures: list[tuple[dict, Path, Path, str, Path]] = []

    def stop_before_provider(meta, coordinator_root, worktree, task_id, runtime_root, **_kwargs):
        captures.append((dict(meta), coordinator_root, worktree, task_id, runtime_root))
        return {
            "status": "prepared",
            "task_id": task_id,
            "worktree": str(worktree),
            "vault_root": str(coordinator_root),
            "runtime_root": str(runtime_root),
        }

    original = task_review_current._run_review
    previous = {
        name: os.environ.get(name)
        for name in (
            "LLM_OBSIDIAN_SESSION_RUNTIME",
            "LLM_OBSIDIAN_SESSION_MODEL",
            "LLM_OBSIDIAN_SESSION_EFFORT",
        )
    }
    os.environ["LLM_OBSIDIAN_SESSION_RUNTIME"] = "codex"
    os.environ["LLM_OBSIDIAN_SESSION_MODEL"] = "gpt-5.6-sol"
    os.environ["LLM_OBSIDIAN_SESSION_EFFORT"] = "high"
    task_review_current._run_review = stop_before_provider
    try:
        first_result = task_review_runner.run_current_review(
            first,
            vault_root=vault,
            origin_surface="11111111-1111-4111-8111-111111111111",
            scratch_root=scratch,
        )
        second_result = task_review_runner.run_current_review(
            second,
            vault_root=vault,
            origin_surface="11111111-1111-4111-8111-111111111111",
            scratch_root=scratch,
        )
    finally:
        task_review_current._run_review = original
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    check("plain external target reaches the provider boundary", first_result["status"] == "prepared")
    check("coordinator and product roots remain distinct", first_result["vault_root"] == str(vault) and first_result["worktree"] == str(first))
    first_meta, captured_vault, captured_target, _, first_runtime = captures[0]
    check("current metadata binds both roots", first_meta["vault_root"] == str(vault) and first_meta["worktree"] == str(first))
    check("Harness state is coordinator-owned", captured_vault == vault and captured_target == first)
    check("scratch remains outside coordinator and target", first_runtime.is_relative_to(scratch) and not first_runtime.is_relative_to(first) and not first_runtime.is_relative_to(vault))
    check("external target receives no review metadata", not (first / ".task-meta.json").exists() and not (first / ".vault-meta").exists())
    check("external target stays Git-clean", subprocess.run(["git", "status", "--porcelain"], cwd=first, text=True, capture_output=True, check=True).stdout == "")

    first_key = resolve_target(first).target_key
    second_key = resolve_target(second).target_key
    first_active = vault / ".vault-meta/harness/current-review" / first_key / "active.json"
    second_active = vault / ".vault-meta/harness/current-review" / second_key / "active.json"
    check("each target owns an independent active pointer", first_key != second_key and first_active.is_file() and second_active.is_file())
    check("two targets do not share scratch", first_result["runtime_root"] != second_result["runtime_root"])

    wake = _callback_wake(first_meta, vault, first)
    check("callback binds the exact coordinator", f"--vault-root {vault}" in wake, wake)
    check("callback binds the exact target", f"--worktree {first}" in wake, wake)

    public = task_review_runner.parser().parse_args(
        ["current", "--target", str(first), "--vault-root", str(vault)]
    )
    legacy = task_review_runner.parser().parse_args(
        ["current", "--worktree", str(first), "--vault-root", str(vault)]
    )
    check("public target and legacy worktree forms resolve identically", public.target == legacy.worktree == first)
    defaulted = task_review_runner.parser().parse_args(
        ["current", "--vault-root", str(vault)]
    )
    check("current facade permits the current Git root default", defaulted.target is None and defaulted.worktree is None)

print("\nAll external current-review tests passed.")
