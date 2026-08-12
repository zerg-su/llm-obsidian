"""Scratch transport materialization for live review acceptance probes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from typing import Any

from harness.contracts import to_dict
from harness.review_submit import publish_review_input_template
from harness.workflows.review import (
    ReviewResult,
    review_round_envelope,
)
from live_acceptance_contracts import LiveDriverError, _stable_id
from live_acceptance_runtime import _atomic_text


def axis_directory(axis: str) -> str:
    return axis


def review_scratch(root: Path, commit_sha: str, cell_id: str) -> Path:
    scratch_base = (
        Path(tempfile.gettempdir())
        / f"llm-obsidian-live-review-{os.getuid()}"
    )
    if scratch_base.is_symlink():
        raise LiveDriverError("review callback scratch cannot be a symlink")
    scratch_base.mkdir(parents=True, exist_ok=True)
    scratch_base.chmod(0o700)
    scratch = scratch_base
    for component in (
        _stable_id(str(root), length=32),
        commit_sha,
        cell_id,
    ):
        scratch = scratch / component
        if scratch.is_symlink():
            raise LiveDriverError("review callback scratch cannot contain symlinks")
        scratch.mkdir(exist_ok=True)
        scratch.chmod(0o700)
    return scratch.resolve()


def review_probe_prompt(
    callback_root: Path,
    product_root: Path,
    axes: tuple[str, ...],
) -> str:
    submit = product_root / "scripts" / "harness" / "review_submit.py"
    commands = []
    for axis in axes:
        directory = callback_root / axis_directory(axis)
        commands.append(
            shlex.join(
                (
                    str(Path(sys.executable).resolve()),
                    str(submit),
                    "--worktree",
                    str(product_root),
                    "--state-dir",
                    str(directory),
                    "--input-file",
                    str(directory / ".review-input.json"),
                )
            )
        )
    return (
        "This is a bounded product-read-only review transport probe. "
        "Do not edit tracked repository files and do not start another model. "
        "Use only the owner-scoped reviewer callback scratch below. In your "
        "axis directory, copy the exact contents of input-template.json to "
        ".review-input.json, run the matching exact review_submit.py command "
        "below, then wait for coordinator cleanup. Never write "
        ".review-callback.json directly.\n\n"
        f"Callback scratch root: {callback_root.as_posix()}\n"
        f"Reviewed product root (read-only): {product_root}\n"
        "Exact submit commands (run only the one for your axis):\n"
        + "\n".join(commands)
        + "\n"
    )


def prepare_review_probe_lane(
    *,
    scratch: Path,
    callback_pointer: str,
    runtime_result: object,
    round_: Any,
    axis: str,
    base_id: str,
    deep: bool,
    root: Path,
    commit_sha: str,
    fingerprint: str,
) -> None:
    """Materialize the same typed submit boundary used by real reviews."""

    record = getattr(runtime_result, "record", None)
    if record is None:
        raise LiveDriverError("review probe runtime returned no record")
    callback_dir = (scratch / callback_pointer).parent
    result = ReviewResult(axis, "approve", verification_iteration=0)
    envelope = review_round_envelope(round_, result)
    meta = {
        "schema_version": 1,
        "transport": "review-round",
        "operation_id": round_.operation_id,
        "run_id": round_.run_id,
        "review_id": base_id,
        "parent_session_operation_id": record.spec.operation_id,
        "review_mode": "deep" if deep else "simple",
        "axis": axis,
        "verification_iteration": 0,
        "worktree": str(root),
        "task_name": base_id,
        "head_sha": commit_sha,
        "verification_profile": {
            "name": "live-acceptance",
            "sha256": fingerprint,
        },
    }
    meta = publish_review_input_template(
        state_root=scratch,
        state_dir=callback_dir,
        worktree=root,
        meta=meta,
    )
    round_input = {
        "schema_version": 1,
        "axis": axis,
        "verdict": "approve",
        "verification_iteration": 0,
        "findings": [],
    }
    for name, value in (
        (".review-meta.json", meta),
        ("input-template.json", round_input),
        ("expected.json", to_dict(envelope)),
    ):
        _atomic_text(
            callback_dir / name,
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        )
