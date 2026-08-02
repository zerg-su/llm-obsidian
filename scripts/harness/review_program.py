"""Compiler and reconciler for purpose-bound review checkpoints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .review_program_contracts import (
    PURPOSES,
    QUESTIONS,
    RISK_PURPOSES,
    VERIFY_BUDGETS,
    ReviewBoundaryInput,
    ReviewProgramError,
)
from .review_program_results import (
    ReviewBoundaryReceipt,
    ReviewProgramDecision,
)


@dataclass(frozen=True)
class ReviewBoundary:
    purpose: str
    input: ReviewBoundaryInput
    question: str
    max_verify_iterations: int
    intent_collapsed: bool = False

    def __post_init__(self) -> None:
        if (
            self.purpose not in PURPOSES
            or self.input.purpose != self.purpose
            or self.question != QUESTIONS[self.purpose]
            or self.max_verify_iterations != VERIFY_BUDGETS[self.purpose]
        ):
            raise ReviewProgramError("compiled review boundary is invalid")


@dataclass(frozen=True)
class ReviewProgram:
    risk_profile: str
    boundaries: tuple[ReviewBoundary, ...]
    schema_version: int = 1

    @property
    def purposes(self) -> tuple[str, ...]:
        return tuple(boundary.purpose for boundary in self.boundaries)

    @property
    def definition_sha256(self) -> str:
        value = {
            "schema_version": self.schema_version,
            "risk_profile": self.risk_profile,
            "boundaries": [
                {
                    "purpose": boundary.purpose,
                    "input_sha256": boundary.input.input_sha256,
                    "question": boundary.question,
                    "max_verify_iterations": boundary.max_verify_iterations,
                    "intent_collapsed": boundary.intent_collapsed,
                }
                for boundary in self.boundaries
            ],
        }
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()


def compile_review_program(
    risk_profile: str,
    inputs: tuple[ReviewBoundaryInput, ...],
) -> ReviewProgram:
    expected = RISK_PURPOSES.get(risk_profile)
    if expected is None:
        raise ReviewProgramError("review risk profile is invalid")
    if not isinstance(inputs, tuple):
        raise ReviewProgramError("review boundary inputs must be a tuple")
    observed = tuple(item.purpose for item in inputs)
    if observed != expected:
        raise ReviewProgramError(
            "review boundaries are missing, duplicated, or out of order"
        )
    if len({item.outcome_contract_sha256 for item in inputs}) != 1:
        raise ReviewProgramError(
            "review boundaries must preserve one Outcome Contract digest"
        )
    collapsed = risk_profile == "small-reversible"
    boundaries = tuple(
        ReviewBoundary(
            purpose=item.purpose,
            input=item,
            question=QUESTIONS[item.purpose],
            max_verify_iterations=VERIFY_BUDGETS[item.purpose],
            intent_collapsed=collapsed and item.purpose == "implementation",
        )
        for item in inputs
    )
    return ReviewProgram(risk_profile, boundaries)


def reconcile_review_program(
    program: ReviewProgram,
    receipts: tuple[ReviewBoundaryReceipt, ...],
) -> ReviewProgramDecision:
    if len(receipts) > len(program.boundaries):
        raise ReviewProgramError("review receipt count exceeds the program")
    seen_operations: set[str] = set()
    for index, receipt in enumerate(receipts):
        boundary = program.boundaries[index]
        if receipt.operation_id in seen_operations:
            raise ReviewProgramError("review operation receipt is duplicated")
        seen_operations.add(receipt.operation_id)
        if (
            receipt.purpose != boundary.purpose
            or receipt.boundary_input_sha256 != boundary.input.input_sha256
        ):
            raise ReviewProgramError("review receipt is stale or out of order")
        if receipt.verdict == "stopped":
            return ReviewProgramDecision(
                "stop",
                receipt.purpose,
                may_fix=receipt.purpose != "release",
            )
    if len(receipts) == len(program.boundaries):
        return ReviewProgramDecision("complete", "", may_fix=False)
    return ReviewProgramDecision(
        "start", program.boundaries[len(receipts)].purpose, may_fix=False
    )


__all__ = (
    "PURPOSES",
    "QUESTIONS",
    "RISK_PURPOSES",
    "VERIFY_BUDGETS",
    "ReviewBoundary",
    "ReviewBoundaryInput",
    "ReviewBoundaryReceipt",
    "ReviewProgram",
    "ReviewProgramDecision",
    "ReviewProgramError",
    "compile_review_program",
    "reconcile_review_program",
)
