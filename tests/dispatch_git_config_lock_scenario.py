#!/usr/bin/env python3
"""Isolated contention scenario for the shared Git-config dispatch lock."""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "dispatch_workspace", ROOT / "scripts" / "dispatch_workspace.py"
)
assert SPEC and SPEC.loader
dispatch_workspace = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dispatch_workspace)


def run_scenario(*, bypass_lock: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="dispatch-config-lock.") as raw:
        tmp = Path(raw)
        worktrees = (
            tmp / "worktrees" / "concurrent-one",
            tmp / "worktrees" / "concurrent-two",
        )
        for candidate in worktrees:
            candidate.mkdir(parents=True)
        git_dirs = {
            candidate: tmp / "git-dirs" / candidate.name for candidate in worktrees
        }
        shared_git_dir = tmp / "shared-git-dir"
        shared_git_dir.mkdir()
        writer_entered = threading.Event()
        second_worker_started = threading.Event()
        second_worker_allowed = threading.Event()
        second_lock_attempted = threading.Event()
        first_writer_released = threading.Event()
        overlap_observed = threading.Event()
        task_thread = threading.local()
        config_lock_changed = threading.Condition()
        config_lock_owner = {"role": ""}

        def git_command(argv, *, cwd=None, **_kwargs):
            if argv == ["git", "rev-parse", "--git-common-dir"]:
                task_thread.awaiting_config_lock = True
                return subprocess.CompletedProcess(
                    argv, 0, f"{shared_git_dir}\n", ""
                )
            if argv == ["git", "rev-parse", "--absolute-git-dir"]:
                return subprocess.CompletedProcess(
                    argv, 0, f"{git_dirs[Path(cwd)]}\n", ""
                )
            if argv == ["git", "config", "extensions.worktreeConfig", "true"]:
                if getattr(task_thread, "awaiting_config_lock", False):
                    if task_thread.role == "first":
                        writer_entered.set()
                    raise AssertionError("shared Git config writer bypassed its lock")
                if task_thread.role == "first":
                    writer_entered.set()
                    second_lock_attempted.wait()
                    first_writer_released.set()
                elif not first_writer_released.is_set():
                    overlap_observed.set()
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[:3] == ["git", "config", "--worktree"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            raise AssertionError(f"unexpected Git command: {argv}")

        def controlled_flock(_descriptor, operation):
            if (
                operation == fcntl.LOCK_EX
                and getattr(task_thread, "awaiting_config_lock", False)
            ):
                if bypass_lock:
                    return None
                task_thread.awaiting_config_lock = False
                with config_lock_changed:
                    if task_thread.role == "second":
                        second_lock_attempted.set()
                    while config_lock_owner["role"]:
                        config_lock_changed.wait()
                    config_lock_owner["role"] = task_thread.role
                    task_thread.holds_config_lock = True
                return None
            if operation == fcntl.LOCK_UN and getattr(
                task_thread, "holds_config_lock", False
            ):
                with config_lock_changed:
                    config_lock_owner["role"] = ""
                    task_thread.holds_config_lock = False
                    config_lock_changed.notify_all()
            return None

        def configure(role: str, candidate: Path) -> None:
            task_thread.role = role
            if role == "second":
                second_worker_started.set()
                second_worker_allowed.wait()
            dispatch_workspace.ensure_task_git_excludes(candidate)

        with mock.patch.object(
            dispatch_workspace, "run_command", side_effect=git_command
        ), mock.patch.object(
            dispatch_workspace.fcntl, "flock", side_effect=controlled_flock
        ):
            pool = ThreadPoolExecutor(max_workers=len(worktrees))
            try:
                first = pool.submit(configure, "first", worktrees[0])
                writer_entered.wait()
                second = pool.submit(configure, "second", worktrees[1])
                second_worker_started.wait()
                assert not first_writer_released.is_set()
                assert not second_lock_attempted.is_set()
                second_worker_allowed.set()
                first.result()
                second.result()
            finally:
                second_worker_allowed.set()
                second_lock_attempted.set()
                with config_lock_changed:
                    config_lock_owner["role"] = ""
                    config_lock_changed.notify_all()
                pool.shutdown(wait=True)

        assert second_lock_attempted.is_set()
        assert not overlap_observed.is_set()
        for candidate in worktrees:
            task_exclude = git_dirs[candidate] / "info" / "task-exclude"
            assert set(task_exclude.read_text(encoding="utf-8").splitlines()) == set(
                dispatch_workspace.TASK_LOCAL_GIT_EXCLUDES
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--simulate-lock-bypass", action="store_true")
    args = parser.parse_args()
    run_scenario(bypass_lock=args.simulate_lock_bypass)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
