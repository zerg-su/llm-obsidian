"""Typed result receipts for outcome-preserving review checkpoints."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Mapping

from .review_program_contracts import (
    IDENTIFIER,
    PURPOSES,
    ReviewBoundaryInput,
    ReviewProgramError,
    require_sha256,
)


@dataclass(frozen=True)
class ReviewBoundaryReceipt:
    operation_id: str
    purpose: str
    boundary_input_sha256: str
    verdict: str
    result_sha256: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or not IDENTIFIER.fullmatch(self.operation_id)
            or self.purpose not in PURPOSES
            or self.verdict not in {"approved", "stopped"}
        ):
            raise ReviewProgramError("review boundary receipt is invalid")
        require_sha256(self.boundary_input_sha256, "boundary input digest")
        require_sha256(self.result_sha256, "review result digest")

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, object]
    ) -> "ReviewBoundaryReceipt":
        if set(raw) != {field.name for field in fields(cls)}:
            raise ReviewProgramError("review boundary receipt fields are not exact")
        return cls(**raw)

    def payload(self) -> dict[str, object]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @classmethod
    def approved(
        cls,
        *,
        operation_id: str,
        boundary: ReviewBoundaryInput,
        result_sha256: str,
    ) -> "ReviewBoundaryReceipt":
        return cls(
            operation_id,
            boundary.purpose,
            boundary.input_sha256,
            "approved",
            result_sha256,
        )

    @classmethod
    def stopped(
        cls,
        *,
        operation_id: str,
        boundary: ReviewBoundaryInput,
        result_sha256: str,
    ) -> "ReviewBoundaryReceipt":
        return cls(
            operation_id,
            boundary.purpose,
            boundary.input_sha256,
            "stopped",
            result_sha256,
        )


@dataclass(frozen=True)
class ReviewProgramDecision:
    action: str
    purpose: str
    may_fix: bool
