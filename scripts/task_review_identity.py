"""Review task identity and owned runtime path contracts."""

from __future__ import annotations

import hashlib
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping

from harness.state_machine import TERMINAL
from harness.store import OperationStore
from task_contract import normalize
from task_review_shared import TaskReviewError, _read_json


def _validate_task(worktree: Path) -> tuple[dict[str, Any], Path, str]:
    worktree = worktree.expanduser().resolve()
    if not worktree.is_dir():
        raise TaskReviewError("task worktree is unavailable")
    meta = _read_json(worktree / ".task-meta.json", "task metadata")
    version = meta.get("version")
    if version not in {3, 4}:
        raise TaskReviewError("automatic review requires v3 or v4 task metadata")
    normalize(meta)
    try:
        task_id = str(uuid.UUID(str(meta.get("task_id") or "")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise TaskReviewError("task identity is invalid") from exc
    if task_id != meta.get("task_id"):
        raise TaskReviewError("task identity must be canonical")
    declared = Path(str(meta.get("worktree") or "")).expanduser()
    if not declared.is_absolute() or declared.resolve() != worktree:
        raise TaskReviewError("task metadata identifies another worktree")
    vault = Path(str(meta.get("vault_root") or "")).expanduser()
    if (
        not vault.is_absolute()
        or not (vault.resolve() / "wiki").is_dir()
        or not (vault.resolve() / "scripts").is_dir()
    ):
        raise TaskReviewError("coordinator vault is unavailable")
    vault = vault.resolve()
    if vault == worktree:
        raise TaskReviewError(
            "coordinator vault and generic product worktree must be separate"
        )
    policy = meta.get("review_policy")
    if not isinstance(policy, dict):
        raise TaskReviewError("review policy is unavailable")
    required = {
        "mode",
        "cross_model",
        "runtime",
        "model",
        "effort",
        "max_verify_iterations",
        "verification_profile",
        "verification_profile_sha256",
    }
    if version == 3:
        required.update(
            {"auto_resolve_severities", "escalate_severities"}
        )
    if set(policy) != required:
        raise TaskReviewError(f"v{version} review policy fields are not exact")
    mode = str(policy.get("mode") or "")
    budget = policy.get("max_verify_iterations")
    if (
        mode not in {"simple", "deep", "full", "skip"}
        or budget != {"simple": 1, "deep": 2, "full": 2, "skip": 0}[mode]
        or not isinstance(policy.get("cross_model"), bool)
        or not all(
            isinstance(policy.get(field), str)
            for field in (
                "runtime",
                "model",
                "effort",
                "verification_profile",
                "verification_profile_sha256",
            )
        )
    ):
        raise TaskReviewError(f"v{version} review policy values are invalid")
    if mode == "skip" and any(
        (
            policy["cross_model"],
            policy["runtime"],
            policy["model"],
            policy["effort"],
        )
    ):
        raise TaskReviewError("typed review skip cannot carry overrides")
    return meta, vault, task_id


def _runtime_root(vault: Path, task_id: str) -> Path:
    root = (
        vault
        / ".vault-meta"
        / "harness"
        / "review-runtime"
        / task_id
    ).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


def _current_runtime_root(
    worktree: Path,
    task_id: str,
    scratch_root: Path | None,
) -> Path:
    if scratch_root is None:
        checkout_key = hashlib.sha256(
            str(worktree).encode("utf-8")
        ).hexdigest()[:16]
        base = (
            Path(tempfile.gettempdir())
            / "llm-obsidian-current-review"
            / checkout_key
        )
    else:
        base = scratch_root.expanduser().resolve()
    root = (base / task_id).resolve()
    if root == worktree or worktree in root.parents:
        raise TaskReviewError(
            "current review scratch must stay outside the product checkout"
        )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


def _gate_root(vault: Path, task_id: str) -> Path:
    return (
        vault
        / ".vault-meta"
        / "harness"
        / "review-data"
        / task_id
        / task_id
    ).resolve()


def _current_review_is_quiescent(vault: Path, task_id: str) -> bool:
    """Permit a fresh current review only after exact old ownership is gone."""

    rows = OperationStore(vault / ".vault-meta" / "harness").list(task_id)
    return bool(rows) and all(
        row.state in TERMINAL
        and not row.resources.surface_id
        and row.resources.process_group == 0
        and row.resources.supervisor_pid == 0
        for row in rows
    )


def _zero_effect_attention_shape(gate_state: Mapping[str, Any]) -> bool:
    """Recognize the exact pre-provider terminal gate shape."""

    attempt = gate_state.get("attempt")
    terminal = attempt.get("terminal") if isinstance(attempt, Mapping) else None
    return bool(
        gate_state.get("status") == "attention-required"
        and gate_state.get("lanes") == []
        and gate_state.get("round_results") == {}
        and gate_state.get("final_results") == {}
        and isinstance(attempt, Mapping)
        and attempt.get("status") == "terminal"
        and isinstance(terminal, Mapping)
        and terminal.get("result") == "attention-required"
        and terminal.get("lane_results") == []
    )


def _zero_effect_attention_is_quiescent(
    vault: Path,
    task_id: str,
    gate_state: Mapping[str, Any],
) -> bool:
    """Recognize an exact pre-provider attempt that created no operation row.

    The superseded gate, ledger, and scratch bytes are deliberately retained
    as derived diagnostics; the next current review starts a new lineage only
    after this predicate proves that the old lineage owned no durable effect.
    """

    if not _zero_effect_attention_shape(gate_state):
        return False
    return not OperationStore(vault / ".vault-meta" / "harness").list(task_id)
