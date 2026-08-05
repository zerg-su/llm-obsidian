"""Durable publication primitives for retained plan-review rebinds."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from task_review_shared import _atomic_bytes, _atomic_json, _read_json


ErrorFactory = Callable[[str], Exception]


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def regular_target_sha256(path: Path, error: ErrorFactory) -> str:
    if path.is_symlink():
        raise error("plan rebind target is not a regular file")
    if not path.exists():
        return ""
    if not path.is_file():
        raise error("plan rebind target is not a regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def publish_target(
    path: Path,
    value: bytes,
    *,
    old_sha256: str,
    new_sha256: str,
    error: ErrorFactory,
) -> None:
    if hashlib.sha256(value).hexdigest() != new_sha256:
        raise error("plan rebind transaction payload changed")
    observed = regular_target_sha256(path, error)
    if observed == new_sha256:
        return
    if observed != old_sha256:
        raise error("plan rebind transaction target changed")
    _atomic_bytes(path, value)


def validate_transaction(
    transaction: Mapping[str, Any],
    *,
    task_id: str,
    reviewed_head: str,
    resolved_head: str,
    relative: str,
    requested_policy: Mapping[str, Any],
    boundary_input_sha256: str,
    updated: Mapping[str, Any],
    delta: Mapping[str, object],
    payloads: Mapping[str, tuple[Path, bytes]],
    error: ErrorFactory,
) -> None:
    targets = transaction.get("targets")
    if (
        transaction.get("schema_version") != 1
        or transaction.get("task_id") != task_id
        or transaction.get("reviewed_head_sha") != reviewed_head
        or transaction.get("resolved_head_sha") != resolved_head
        or transaction.get("plan_relative_path") != relative
        or transaction.get("resolved_boundary_input_sha256")
        != boundary_input_sha256
        or transaction.get("requested_policy_sha256")
        != hashlib.sha256(canonical_json_bytes(requested_policy)).hexdigest()
        or transaction.get("resolved_meta_sha256")
        != hashlib.sha256(canonical_json_bytes(updated)).hexdigest()
        or transaction.get("delta") != dict(delta)
        or not isinstance(targets, Mapping)
        or set(targets) != set(payloads)
    ):
        raise error("plan rebind transaction identity changed")
    for name, (path, value) in payloads.items():
        target = targets.get(name)
        if (
            not isinstance(target, Mapping)
            or target.get("path") != str(path.resolve())
            or target.get("new_sha256")
            != hashlib.sha256(value).hexdigest()
            or not isinstance(target.get("old_sha256"), str)
        ):
            raise error("plan rebind transaction target identity changed")


def advance_stage(
    transaction_path: Path,
    transaction: Mapping[str, Any],
    stage: str,
) -> dict[str, Any]:
    updated = {**dict(transaction), "stage": stage}
    _atomic_json(transaction_path, updated)
    return updated


def finalize_active(
    candidate: Mapping[str, Any],
    active_path: Path,
    *,
    error: ErrorFactory,
) -> dict[str, Any]:
    """Finish a crash after the active plan-review commit point."""

    plan_meta = candidate.get("plan_review")
    if not isinstance(plan_meta, Mapping):
        return dict(candidate)
    runtime_root = Path(str(candidate.get("runtime_root") or "")).resolve()
    transaction_path = runtime_root / "plan-review-rebind.json"
    if not transaction_path.is_file() or transaction_path.is_symlink():
        return dict(candidate)
    transaction = _read_json(transaction_path, "plan rebind transaction")
    targets = transaction.get("targets")
    if (
        transaction.get("schema_version") != 1
        or transaction.get("task_id") != candidate.get("task_id")
        or not isinstance(targets, Mapping)
    ):
        raise error("plan rebind transaction identity changed")
    active = targets.get("active")
    if (
        not isinstance(active, Mapping)
        or active.get("path") != str(active_path.resolve())
    ):
        raise error("plan rebind active target changed")
    for target in targets.values():
        if not isinstance(target, Mapping):
            raise error("plan rebind transaction is incomplete")
        target_path = target.get("path")
        target_sha256 = target.get("new_sha256")
        if (
            not isinstance(target_path, str)
            or not target_path
            or not isinstance(target_sha256, str)
            or regular_target_sha256(Path(target_path), error)
            != target_sha256
        ):
            raise error("plan rebind transaction is incomplete")
    if transaction.get("stage") != "complete":
        advance_stage(transaction_path, transaction, "complete")
    return dict(candidate)


__all__ = (
    "advance_stage",
    "canonical_json_bytes",
    "finalize_active",
    "publish_target",
    "regular_target_sha256",
    "validate_transaction",
)
