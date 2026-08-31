"""Stable finalization-lineage binding for mutable current-review scopes."""

from __future__ import annotations

import json
import shlex
import stat
import sys
from pathlib import Path
from typing import Mapping

from harness.finalization_ledger import FinalizationLedger, FinalizationLedgerError
from task_review_shared import TaskReviewError


def current_pivot_scratch(meta: Mapping[str, object]) -> Path | None:
    """Resolve the current review's existing external scratch authority."""

    if meta.get("lifecycle") != "current-checkout":
        return None
    raw = meta.get("runtime_root")
    if not isinstance(raw, str) or not raw:
        raise FinalizationLedgerError(
            "current review pivot scratch authority is invalid"
        )
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise FinalizationLedgerError(
            "current review pivot scratch authority is invalid"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise FinalizationLedgerError(
            "current review pivot scratch authority is unavailable"
        ) from exc
    if not resolved.is_dir():
        raise FinalizationLedgerError(
            "current review pivot scratch authority is invalid"
        )
    return resolved


def current_review_callback_wake(
    meta: Mapping[str, object], vault: Path, worktree: Path
) -> str:
    """Compile the exact idempotent callback for a current review."""

    if meta.get("lifecycle") != "current-checkout":
        return ""
    raw_policy = meta["review_policy"]
    if not isinstance(raw_policy, Mapping):
        raise TaskReviewError("current review callback policy is invalid")
    wake_argv = [
        str(Path(sys.executable).resolve()),
        str(vault / "scripts" / "task-review-runner.py"),
        "current",
        "--worktree",
        str(worktree),
        "--vault-root",
        str(vault),
    ]
    mode = str(raw_policy.get("mode") or "")
    if mode == "deep":
        wake_argv.append("--deep")
    elif mode == "full":
        wake_argv.append("--full")
    if raw_policy.get("cross_model") is True:
        wake_argv.append("--cross-model")
    for option in ("runtime", "model", "effort"):
        value = str(raw_policy.get(option) or "")
        if value:
            wake_argv.extend((f"--{option}", value))
    base_sha = str(raw_policy.get("base_sha") or "")
    if base_sha:
        wake_argv.extend(("--base", base_sha))
    purpose = str(raw_policy.get("purpose") or "implementation")
    boundary_file = str(
        (
            meta.get("review_boundary_input_source_file")
            if purpose == "release"
            else meta.get("review_boundary_input_file")
        )
        or ""
    )
    if purpose != "implementation" or boundary_file:
        wake_argv.extend(("--purpose", purpose))
    if boundary_file:
        wake_argv.extend(("--boundary-input", boundary_file))
    artifact_root = str(meta.get("review_artifact_root") or "")
    if artifact_root:
        wake_argv.extend(("--artifact-root", artifact_root))
    if purpose == "release":
        plan_file = str(meta.get("plan_file") or "")
        if not plan_file:
            raise TaskReviewError(
                "current release review callback has no bound plan"
            )
        wake_argv.extend(("--plan", plan_file))
    return (
        "Typed current-review callback is ready. Run this exact command: "
        + shlex.join(wake_argv)
    )


def structural_pivot_callback_wake(
    meta: Mapping[str, object], vault: Path, worktree: Path
) -> str:
    """Preserve the owning lifecycle when a structural callback wakes it."""

    current = current_review_callback_wake(meta, vault, worktree)
    if current:
        return current
    return (
        "Structural pivot callback is ready. Run this exact command: "
        + shlex.join(
            (
                str(Path(sys.executable).resolve()),
                str(vault / "scripts/task-review-runner.py"),
                "run",
                "--worktree",
                str(worktree.expanduser().resolve()),
            )
        )
    )


def bound_finalization_ledger(
    root: Path,
    *,
    lineage_id: str,
    origin_task_id: str,
    plan_sha256: str,
    outcome_contract_sha256: str,
    max_cycles: int,
    reopen_existing: bool,
) -> FinalizationLedger:
    """Use the durable initial binding while current attempt inputs rebind."""

    initial = FinalizationLedger(
        root,
        lineage_id=lineage_id,
        origin_task_id=origin_task_id,
        plan_sha256=plan_sha256,
        outcome_contract_sha256=outcome_contract_sha256,
        max_cycles=max_cycles,
    )
    path = initial.path
    if not reopen_existing or (
        not path.exists() and not path.is_symlink()
    ):
        return initial
    if initial.root.is_symlink():
        raise FinalizationLedgerError("ledger root cannot be a symlink")
    if path.is_symlink() or not path.is_file():
        raise FinalizationLedgerError(
            "finalization ledger must be a regular file"
        )
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise FinalizationLedgerError("finalization ledger must be owner-only")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalizationLedgerError(
            "finalization ledger is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise FinalizationLedgerError("finalization ledger shape is invalid")
    reopened = FinalizationLedger(
        root,
        lineage_id=lineage_id,
        origin_task_id=origin_task_id,
        plan_sha256=value.get("plan_sha256"),
        outcome_contract_sha256=value.get("outcome_contract_sha256"),
        max_cycles=max_cycles,
    )
    reopened.snapshot()
    return reopened
