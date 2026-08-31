"""Stable finalization-lineage binding for mutable current-review scopes."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Mapping

from harness.finalization_ledger import FinalizationLedger, FinalizationLedgerError


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
