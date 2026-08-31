"""Explicit approval contract for a current-checkout review range."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from harness.context import (
    OUTCOME_POINTER_ID,
    ContextInput,
)
from outcome_contract import OutcomeContractError, extract_from_bytes
from task_review_shared import TaskReviewError


def current_plan_identity(path: Path) -> tuple[str, str]:
    """Validate one behavior-bound plan before ownership effects."""

    try:
        raw = path.read_bytes()
        outcome = extract_from_bytes(raw)
    except (OSError, OutcomeContractError) as exc:
        raise TaskReviewError(
            f"current review Outcome Contract is invalid: {exc}"
        ) from exc
    if not any(
        item.get("evidence_kind") == "behavior" and item.get("subject")
        for item in outcome.value["success_evidence"]
    ):
        raise TaskReviewError(
            "current review Outcome Contract requires behavior-bound "
            "success evidence"
        )
    return hashlib.sha256(raw).hexdigest(), outcome.sha256


def current_review_outcome_inputs(
    meta: Mapping[str, Any],
    plan: Path,
    boundary_input_sha256: str,
    *,
    plan_bytes: bytes,
) -> tuple[ContextInput, ...]:
    """Package the synthetic/explicit contract on the legacy current path."""

    if (
        meta.get("version") != 4
        or meta.get("lifecycle") != "current-checkout"
        or boundary_input_sha256
    ):
        return ()
    try:
        contract = extract_from_bytes(plan_bytes)
    except OutcomeContractError as exc:
        raise TaskReviewError(
            f"review Outcome Contract is invalid: {exc}"
        ) from exc
    if contract.sha256 != str(meta.get("outcome_contract_sha256") or ""):
        raise TaskReviewError(
            "review Outcome Contract input digest changed"
        )
    return (
        ContextInput(
            "outcome-contract.json",
            str(plan),
            contract.canonical,
            role="outcome",
            pointer_id=OUTCOME_POINTER_ID,
        ),
    )
