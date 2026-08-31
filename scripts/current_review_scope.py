"""Deterministic approval contract for a current-checkout review range."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from harness.context import ContextInput, outcome_contract_input
from harness.contracts import ContractError as HarnessContractError
from outcome_contract import OutcomeContractError, extract_from_bytes
from task_review_shared import TaskReviewError


GIT_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


@dataclass(frozen=True)
class CurrentReviewScope:
    text: str
    plan_sha256: str
    outcome_sha256: str


def _oid(value: str, label: str) -> str:
    if not GIT_OID.fullmatch(value):
        raise TaskReviewError(f"current review {label} is invalid")
    return value


def _outcome_value(denominator: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "purpose": "Approval-capable review of an exact committed checkout scope.",
        "desired_outcome": (
            f"The committed review denominator {denominator} is correct, "
            "complete, maintainable, and ready for its intended use."
        ),
        "success_evidence": [
            {
                "evidence_id": "scope-integrity",
                "observable": (
                    f"Every finding and approval is grounded in the exact "
                    f"committed denominator {denominator}."
                ),
            },
            {
                "evidence_id": "repository-contract",
                "observable": (
                    "The implementation satisfies repository instructions, "
                    "public behavior, and engineering quality criteria."
                ),
            },
            {
                "evidence_id": "verification",
                "observable": (
                    "Relevant deterministic verification passes, or any "
                    "material evidence gap is reported as a finding."
                ),
            },
        ],
        "non_goals": [
            "Do not modify the reviewed checkout while performing review.",
            "Do not assess commits or uncommitted bytes outside the bound denominator.",
        ],
    }


def compile_current_review_scope(
    *, base_sha: str, head_sha: str
) -> CurrentReviewScope:
    """Compile one range-bound plan and its canonical Outcome Contract."""

    head = _oid(head_sha, "HEAD")
    base = _oid(base_sha, "base") if base_sha else ""
    denominator = f"{base}..{head}" if base else head
    scope = (
        f"Review the exact committed range {denominator}. Treat the cumulative "
        "range diff as the review denominator."
        if base
        else f"Review exact committed HEAD {head} against its repository "
        "instructions, tests, and public contract."
    )
    text = "\n".join(
        (
            "# Current checkout review scope",
            "",
            scope,
            "",
            "## Outcome Contract",
            "",
            "```json",
            json.dumps(
                _outcome_value(denominator),
                ensure_ascii=False,
                sort_keys=True,
            ),
            "```",
            "",
        )
    )
    raw = text.encode("utf-8")
    try:
        outcome = extract_from_bytes(raw)
    except OutcomeContractError as exc:  # pragma: no cover - code-owned value
        raise TaskReviewError(
            f"compiled current review Outcome Contract is invalid: {exc}"
        ) from exc
    return CurrentReviewScope(
        text=text,
        plan_sha256=hashlib.sha256(raw).hexdigest(),
        outcome_sha256=outcome.sha256,
    )


def current_plan_outcome_sha256(path: Path) -> str:
    """Validate an explicit current-review plan before ownership effects."""

    try:
        return extract_from_bytes(path.read_bytes()).sha256
    except (OSError, OutcomeContractError) as exc:
        raise TaskReviewError(
            f"current review Outcome Contract is invalid: {exc}"
        ) from exc


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
