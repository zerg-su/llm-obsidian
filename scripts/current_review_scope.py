"""Explicit approval contract for a current-checkout review range."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from harness.context import ContextInput, outcome_contract_input
from harness.contracts import ContractError as HarnessContractError
from outcome_contract import OutcomeContractError, extract_from_bytes
from task_review_shared import TaskReviewError


GENERIC_REVIEW_EVIDENCE_IDS = frozenset(
    {"scope-integrity", "repository-contract", "verification"}
)


def current_plan_identity(path: Path) -> tuple[str, str]:
    """Validate one behavior-specific plan before ownership effects."""

    try:
        raw = path.read_bytes()
        outcome = extract_from_bytes(raw)
    except (OSError, OutcomeContractError) as exc:
        raise TaskReviewError(
            f"current review Outcome Contract is invalid: {exc}"
        ) from exc
    if set(outcome.evidence_ids).issubset(GENERIC_REVIEW_EVIDENCE_IDS):
        raise TaskReviewError(
            "current review Outcome Contract requires behavior-specific "
            "success evidence"
        )
    return hashlib.sha256(raw).hexdigest(), outcome.sha256


def current_review_outcome_inputs(
    meta: Mapping[str, Any], plan: Path, boundary_input_sha256: str
) -> tuple[ContextInput, ...]:
    """Package the synthetic/explicit contract on the legacy current path."""

    if (
        meta.get("version") != 4
        or meta.get("lifecycle") != "current-checkout"
        or boundary_input_sha256
    ):
        return ()
    try:
        return (
            outcome_contract_input(
                plan,
                expected_sha256=str(meta.get("outcome_contract_sha256") or ""),
            ),
        )
    except HarnessContractError as exc:
        raise TaskReviewError(
            f"review Outcome Contract is invalid: {exc}"
        ) from exc
