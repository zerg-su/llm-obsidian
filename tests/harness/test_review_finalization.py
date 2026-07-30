#!/usr/bin/env python3
"""Fail-closed review authorization at the task-summary/reap boundary."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from harness.review_finalization import (  # noqa: E402
    review_gate_root,
    require_task_review,
    task_review_status,
)
from harness.verification import load_profiles  # noqa: E402
from harness.workflows.review import ReviewContext  # noqa: E402
from harness.workflows.review_gate import (  # noqa: E402
    ReviewGateController,
    ReviewPreset,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


with tempfile.TemporaryDirectory(prefix="review-finalization.") as raw:
    base = Path(raw)
    vault = base / "vault"
    worktree = base / "worktree"
    (vault / ".vault-meta" / "harness").mkdir(parents=True)
    (vault / "config").mkdir()
    shutil.copy2(
        ROOT / "config" / "verification-profiles.toml",
        vault / "config" / "verification-profiles.toml",
    )
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    git(worktree, "config", "user.email", "review@example.invalid")
    git(worktree, "config", "user.name", "Review Gate Test")
    (worktree / "product.txt").write_text("one\n", encoding="utf-8")
    git(worktree, "add", "product.txt")
    git(worktree, "commit", "-m", "one")
    head = git(worktree, "rev-parse", "HEAD")

    task_id = str(uuid.uuid4())
    profile_sha = load_profiles(
        vault / "config" / "verification-profiles.toml"
    )["scoped"].sha256
    meta = {
        "version": 3,
        "task_id": task_id,
        "vault_root": str(vault),
        "worktree": str(worktree),
        "review_policy": {
            "mode": "skip",
            "cross_model": False,
            "runtime": "",
            "model": "",
            "effort": "",
            "max_verify_iterations": 0,
            "verification_profile": "scoped",
            "verification_profile_sha256": profile_sha,
        },
    }
    gate = review_gate_root(meta, worktree)
    check(
        "gate root is derived from trusted vault/task identity",
        gate
        == (
            vault
            / ".vault-meta"
            / "harness"
            / "review-data"
            / task_id
            / task_id
        ).resolve(),
    )
    missing = task_review_status(meta, worktree)
    check(
        "missing gate is a typed non-terminal wait state",
        missing.status == "missing" and missing.authorization is None,
    )
    gate.mkdir(parents=True)
    active_meta = {
        **meta,
        "review_policy": {
            **meta["review_policy"],
            "mode": "simple",
            "max_verify_iterations": 1,
        },
    }
    (gate / "review-gate.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dispatch_operation_id": task_id,
                "status": "reviewing",
                "product_root": str(worktree),
                "policy": {"enabled": True},
                "context": {
                    "head_sha": head,
                    "verification_profile": "scoped",
                    "verification_profile_sha256": profile_sha,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    active = task_review_status(active_meta, worktree)
    check(
        "active review is a typed non-terminal wait state",
        active.status == "reviewing",
    )
    (gate / "review-gate.json").unlink()

    ReviewGateController.skip(
        gate,
        dispatch_operation_id=task_id,
        owner_id=task_id,
        preset=ReviewPreset.from_flags(no_review=True),
        context=ReviewContext(
            "packets/task/manifest.json",
            head,
            "scoped",
            profile_sha,
        ),
        product_root=worktree,
    )
    authorization = require_task_review(meta, worktree)
    check(
        "only a typed exact-HEAD no-review skip bypasses provider review",
        authorization.skipped and not authorization.approved,
    )
    check(
        "typed skip is exposed without an archive requirement",
        task_review_status(meta, worktree).status == "skipped",
    )
    (worktree / ".task-meta.json").write_text(
        json.dumps(meta) + "\n", encoding="utf-8"
    )
    archive = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "archive_task_reviews.py"),
            "--worktree",
            str(worktree),
            "--vault-root",
            str(vault),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    archived = json.loads(archive.stdout)
    check(
        "public archive path uses typed skip with zero legacy markers",
        archive.returncode == 0
        and archived["status"] == "archived"
        and archived["markers"] == []
        and archived["failed_operations"] == [],
    )
    check(
        "v3 archive implementation no longer reads TaskSessionStore",
        "TaskSessionStore"
        not in (
            ROOT / "scripts" / "archive_task_reviews.py"
        ).read_text(encoding="utf-8"),
    )

    (worktree / "product.txt").write_text("two\n", encoding="utf-8")
    git(worktree, "add", "product.txt")
    git(worktree, "commit", "-m", "two")
    check(
        "a later HEAD produces a typed stale terminal state",
        task_review_status(meta, worktree).status == "stale",
    )

    (gate / "review-gate.json").write_text("{broken\n", encoding="utf-8")
    check(
        "invalid gate evidence produces a typed attention terminal state",
        task_review_status(meta, worktree).status == "attention",
    )

    check(
        "generic product roots do not need a local verification config",
        not (worktree / "config" / "verification-profiles.toml").exists(),
    )

print("\nAll review finalization tests passed.")
