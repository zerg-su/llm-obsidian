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
from typing import Callable


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
from task_review_resolution_flow import (  # noqa: E402
    _preload_resolution_bundle,
)


def check(label: str, value: bool) -> None:
    if not value:
        raise AssertionError(label)
    print(f"OK   {label}")


def expect_error(
    label: str, action: Callable[[], object], message: str
) -> None:
    try:
        action()
    except runner.TaskReviewError as exc:
        check(label, message in str(exc))
    else:
        raise AssertionError(label)


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
    axes = ("anthropic-holistic", "openai-holistic")
    boundaries = {}
    callbacks = []
    for axis in axes:
        pointer = f"{task_id}/round-{axis}.json"
        findings = [
            {
                "finding_id": (
                    "SPEC-001" if axis == "anthropic-holistic" else "STD-001"
                ),
                "severity": "important",
            }
        ]
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
            "material_finding_ids": [
                f"{axis}:{findings[0]['finding_id']}"
            ],
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

    def write_resolution(
        review_identity: str,
        resolved_head: str,
        *,
        reviewed_head: str = reviewed,
        spec_rationale: str = "The exact repair is present.",
    ) -> None:
        (worktree / ".task-review-resolution.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "operation_id": task_id,
                    "reviewed_head_sha": reviewed_head,
                    "resolved_head_sha": resolved_head,
                    "review_identity_sha256": review_identity,
                    "resolutions": [
                        {
                            "finding_id": "anthropic-holistic:SPEC-001",
                            "disposition": "applied",
                            "rationale": spec_rationale,
                            "follow_up": "",
                        },
                        {
                            "finding_id": "openai-holistic:STD-001",
                            "disposition": "applied",
                            "rationale": (
                                "The exact standards repair is present."
                            ),
                            "follow_up": "",
                        },
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )

    write_resolution(identity, resolved)
    initial = runner._resolution_bundle(
        worktree, gate_root, task_id, boundaries, resolved
    )
    persisted_pointer = f"{task_id}/resolution-spec-0.json"
    (gate_root / persisted_pointer).write_text(
        json.dumps(initial.by_axis["anthropic-holistic"].payload()) + "\n",
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
        persisted_resolution_pointers={"anthropic-holistic:0": persisted_pointer},
    )
    check(
        "partial deep resolution preserves full transport identity",
        resumed.review_identity_sha256 == identity,
    )
    check(
        "partial deep resolution retains already-staged material findings",
        tuple(item.finding_id for item in resumed.resolution.resolutions)
        == (
            "anthropic-holistic:SPEC-001",
            "openai-holistic:STD-001",
        ),
    )
    check(
        "remaining material axis receives its exact evidence",
        resumed.by_axis[axes[1]].previous_finding_ids
        == ("openai-holistic:STD-001",),
    )

    persisted_standards_pointer = f"{task_id}/resolution-standards-0.json"
    (gate_root / persisted_standards_pointer).write_text(
        json.dumps(initial.by_axis[axes[1]].payload()) + "\n",
        encoding="utf-8",
    )
    reverse_resumed = runner._resolution_bundle(
        worktree,
        gate_root,
        task_id,
        {axes[0]: boundaries[axes[0]]},
        resolved,
        persisted_identity_sha256=identity,
        persisted_resolution_pointers={
            "openai-holistic:0": (
                persisted_standards_pointer
            )
        },
    )
    check(
        "material finding order is canonical when standards stages first",
        tuple(
            item.finding_id
            for item in reverse_resumed.resolution.resolutions
        )
        == (
            "anthropic-holistic:SPEC-001",
            "openai-holistic:STD-001",
        ),
    )
    persisted_only = runner._resolution_bundle(
        worktree,
        gate_root,
        task_id,
        {},
        resolved,
        persisted_identity_sha256=identity,
        persisted_resolution_pointers={
            "anthropic-holistic:0": persisted_pointer,
            "openai-holistic:0": (
                persisted_standards_pointer
            ),
        },
    )
    check(
        "verifying replay rebuilds the fully persisted resolution batch",
        persisted_only.resolution.resolved_head_sha == resolved
        and set(persisted_only.by_axis) == set(axes),
    )
    preloaded = _preload_resolution_bundle(
        worktree=worktree,
        gate_root=gate_root,
        task_id=task_id,
        state={
            "status": "verifying",
            "context": {"head_sha": resolved},
            "awaiting_resolution": {},
            "resolution_transport_identity_sha256": identity,
            "resolution_evidence": {
                "anthropic-holistic:0": persisted_pointer,
                "openai-holistic:0": (
                    persisted_standards_pointer
                ),
            },
        },
    )
    check(
        "verifying current review preloads its durable resolution context",
        preloaded is not None
        and preloaded.resolution.resolved_head_sha == resolved,
    )

    fresh_operation_id = f"{task_id}-fresh-deadbeef"
    fresh_boundaries = {
        axis: {**boundary, "review_operation_id": fresh_operation_id}
        for axis, boundary in boundaries.items()
    }
    fresh_identity = review_transport_identity_sha256(
        fresh_operation_id, callbacks
    )
    write_resolution(fresh_identity, resolved)
    fresh_initial = runner._resolution_bundle(
        worktree,
        gate_root,
        task_id,
        fresh_boundaries,
        resolved,
    )
    fresh_spec_pointer = f"{task_id}/resolution-fresh-spec-0.json"
    (gate_root / fresh_spec_pointer).write_text(
        json.dumps(fresh_initial.by_axis[axes[0]].payload()) + "\n",
        encoding="utf-8",
    )
    fresh_resumed = runner._resolution_bundle(
        worktree,
        gate_root,
        task_id,
        {axes[1]: fresh_boundaries[axes[1]]},
        resolved,
        persisted_identity_sha256=fresh_identity,
        persisted_resolution_pointers={"anthropic-holistic:0": fresh_spec_pointer},
    )
    check(
        "fresh deep review keeps transport and resolution identities separate",
        fresh_resumed.review_identity_sha256 == fresh_identity,
    )

    product.write_text("VALUE = 3\n", encoding="utf-8")
    subprocess.run(["git", "add", "product.py"], cwd=worktree, check=True)
    subprocess.run(
        ["git", "commit", "-m", "second fix"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    second_resolved = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=worktree, text=True
    ).strip()
    sequential_boundaries = {
        axis: {**boundary, "reviewed_head_sha": resolved}
        for axis, boundary in boundaries.items()
    }
    write_resolution(identity, second_resolved, reviewed_head=resolved)
    sequential = runner._resolution_bundle(
        worktree,
        gate_root,
        task_id,
        sequential_boundaries,
        second_resolved,
        persisted_resolution_pointers={
            "anthropic-holistic:0": persisted_pointer,
            "openai-holistic:0": (
                persisted_standards_pointer
            ),
        },
    )
    check(
        "a second verification fix preserves the original boundary HEAD",
        sequential.resolution.reviewed_head_sha == resolved
        and sequential.origin_reviewed_head_sha == reviewed,
    )
    write_resolution(identity, second_resolved)
    second_initial = runner._resolution_bundle(
        worktree,
        gate_root,
        task_id,
        boundaries,
        second_resolved,
    )
    second_spec_pointer = f"{task_id}/resolution-spec-1.json"
    (gate_root / second_spec_pointer).write_text(
        json.dumps(second_initial.by_axis[axes[0]].payload()) + "\n",
        encoding="utf-8",
    )
    historical_standards_pointer = (
        f"{task_id}/resolution-standards-historical.json"
    )
    (gate_root / historical_standards_pointer).write_text(
        json.dumps(initial.by_axis[axes[1]].payload()) + "\n",
        encoding="utf-8",
    )
    historical_resumed = runner._resolution_bundle(
        worktree,
        gate_root,
        task_id,
        {axes[1]: boundaries[axes[1]]},
        second_resolved,
        persisted_identity_sha256=identity,
        persisted_resolution_pointers={
            "anthropic-holistic:1": second_spec_pointer,
            "openai-holistic:0": (
                historical_standards_pointer
            ),
        },
    )
    check(
        "historical evidence is skipped without replacing the current batch",
        historical_resumed.by_axis[axes[0]].resolved_head_sha
        == second_resolved,
    )

    expect_error(
        "persisted identity requires exact-head evidence",
        lambda: runner._resolution_bundle(
            worktree,
            gate_root,
            task_id,
            {axes[1]: boundaries[axes[1]]},
            second_resolved,
            persisted_identity_sha256=identity,
            persisted_resolution_pointers={
                "openai-holistic:0": (
                    historical_standards_pointer
                )
            },
        ),
        "persisted review identity has no exact-HEAD resolution evidence",
    )

    duplicate_spec_pointer = f"{task_id}/resolution-spec-duplicate.json"
    (gate_root / duplicate_spec_pointer).write_text(
        json.dumps(second_initial.by_axis[axes[0]].payload()) + "\n",
        encoding="utf-8",
    )
    expect_error(
        "one axis cannot be staged twice",
        lambda: runner._resolution_bundle(
            worktree,
            gate_root,
            task_id,
            {axes[1]: boundaries[axes[1]]},
            second_resolved,
            persisted_identity_sha256=identity,
            persisted_resolution_pointers={
                "anthropic-holistic:1": second_spec_pointer,
                "anthropic-holistic:duplicate": duplicate_spec_pointer,
            },
        ),
        "review resolution axis is staged more than once",
    )

    changed_delta_payload = dict(initial.by_axis[axes[0]].payload())
    changed_delta_payload["resolved_head_sha"] = second_resolved
    changed_delta_pointer = f"{task_id}/resolution-spec-stale-delta.json"
    (gate_root / changed_delta_pointer).write_text(
        json.dumps(changed_delta_payload) + "\n",
        encoding="utf-8",
    )
    expect_error(
        "persisted evidence cannot substitute a changed fix delta",
        lambda: runner._resolution_bundle(
            worktree,
            gate_root,
            task_id,
            {axes[1]: boundaries[axes[1]]},
            second_resolved,
            persisted_identity_sha256=identity,
            persisted_resolution_pointers={"anthropic-holistic:1": changed_delta_pointer},
        ),
        "persisted review resolution fix delta changed",
    )

    write_resolution(
        identity,
        second_resolved,
        spec_rationale="A different staged ruling.",
    )
    expect_error(
        "persisted finding rulings are immutable across resume",
        lambda: runner._resolution_bundle(
            worktree,
            gate_root,
            task_id,
            {axes[1]: boundaries[axes[1]]},
            second_resolved,
            persisted_identity_sha256=identity,
            persisted_resolution_pointers={"anthropic-holistic:1": second_spec_pointer},
        ),
        "persisted review resolution finding rulings changed",
    )

with tempfile.TemporaryDirectory(prefix="review-resolution-large-input.") as raw:
    root = Path(raw)
    source = root / "approved-plan.md"
    pointer_root = root / "pointers"
    payload = b"x" * 65_537
    source.write_bytes(payload)
    bounded = runner._bounded_input(
        "approved-plan.md",
        source,
        role="plan",
        pointer_root=pointer_root,
    )
    pointer = pointer_root / "approved-plan.md"
    check(
        "large resolution input is atomically materialized as a pointer",
        bounded.content is None
        and bounded.source == str(pointer)
        and bounded.byte_count == len(payload)
        and bounded.content_sha256 == hashlib.sha256(payload).hexdigest()
        and pointer.read_bytes() == payload,
    )

print("\nAll review resolution bundle tests passed.")
