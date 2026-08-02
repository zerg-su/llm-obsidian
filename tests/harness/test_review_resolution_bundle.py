#!/usr/bin/env python3
"""Partial deep-lane resolution preserves the full callback identity."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

module_spec = importlib.util.spec_from_file_location(
    "task_review_runner_resolution_bundle",
    ROOT / "scripts" / "task-review-runner.py",
)
assert module_spec is not None and module_spec.loader is not None
runner = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(runner)

from review_resolution import review_transport_identity_sha256  # noqa: E402


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


with tempfile.TemporaryDirectory(prefix="review-resolution-bundle.") as raw:
    tmp = Path(raw)
    worktree = tmp / "product"
    gate_root = tmp / "review-data"
    worktree.mkdir()
    (gate_root / "review-task").mkdir(parents=True)
    worktree = worktree.resolve()
    gate_root = gate_root.resolve()
    subprocess.run(["git", "init", "-b", "main"], cwd=worktree, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, check=True)
    product = worktree / "product.py"
    product.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.py"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=worktree, check=True, capture_output=True)
    reviewed = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip()
    product.write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.py"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "fix"], cwd=worktree, check=True, capture_output=True)
    resolved = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=worktree, text=True).strip()

    task_id = "review-task"
    axes = ("spec", "standards-correctness-architecture-security")
    boundaries = {}
    callbacks = []
    for axis in axes:
        pointer = f"{task_id}/round-{axis}.json"
        findings = (
            [{"finding_id": "SPEC-001", "severity": "important"}]
            if axis == "spec"
            else []
        )
        (gate_root / pointer).write_text(
            json.dumps({"axis": axis, "findings": findings}) + "\n",
            encoding="utf-8",
        )
        boundary = {
            "pointer": pointer,
            "review_operation_id": task_id,
            "reviewed_head_sha": reviewed,
            "round_operation_id": f"round-{axis}",
            "round_run_id": f"run-{axis}",
            "callback_id": f"callback-{axis}",
            "callback_sha256": hashlib.sha256(axis.encode()).hexdigest(),
        }
        boundaries[axis] = boundary
        callbacks.append(
            {
                "axis": axis,
                "round_operation_id": boundary["round_operation_id"],
                "round_run_id": boundary["round_run_id"],
                "callback_id": boundary["callback_id"],
                "callback_sha256": boundary["callback_sha256"],
            }
        )
    identity = review_transport_identity_sha256(task_id, callbacks)
    (worktree / ".task-review-resolution.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": task_id,
                "reviewed_head_sha": reviewed,
                "resolved_head_sha": resolved,
                "review_identity_sha256": identity,
                "resolutions": [
                    {
                        "finding_id": "SPEC-001",
                        "disposition": "applied",
                        "rationale": "The exact repair is present.",
                        "follow_up": "",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    initial = runner._resolution_bundle(
        worktree, gate_root, task_id, boundaries, resolved
    )
    persisted_pointer = f"{task_id}/resolution-spec-0.json"
    (gate_root / persisted_pointer).write_text(
        json.dumps(initial.by_axis["spec"].payload()) + "\n",
        encoding="utf-8",
    )
    remaining = {axes[1]: boundaries[axes[1]]}
    resumed = runner._resolution_bundle(
        worktree,
        gate_root,
        task_id,
        remaining,
        resolved,
        persisted_identity_sha256=identity,
        persisted_resolution_pointers={"spec:0": persisted_pointer},
    )
    check(
        "partial deep resolution preserves full transport identity",
        resumed.review_identity_sha256 == identity,
    )
    check(
        "partial deep resolution retains already-staged material findings",
        tuple(item.finding_id for item in resumed.resolution.resolutions)
        == ("SPEC-001",),
    )
    check(
        "remaining finding-free axis receives exact empty evidence",
        resumed.by_axis[axes[1]].previous_finding_ids == (),
    )

print("\nAll review resolution bundle tests passed.")
