"""Immutable contracts for outcome-preserving review checkpoints."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields
from pathlib import PurePosixPath
from typing import Mapping


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
GIT_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
PURPOSES = ("intent", "implementation", "release")
RISK_PURPOSES = {
    "small-reversible": ("implementation",),
    "standard": ("intent", "implementation"),
    "architecture": PURPOSES,
    "migration": PURPOSES,
    "release": PURPOSES,
    "skill-integration": PURPOSES,
}
QUESTIONS = {
    "intent": (
        "Does the approved design and plan preserve the exact Outcome Contract, "
        "capability dispositions, success evidence, and non-goals?"
    ),
    "implementation": (
        "Does the exact product HEAD and independently checked verification "
        "evidence implement the approved outcome without scope drift?"
    ),
    "release": (
        "Does the exact integration HEAD, complete outcome-evidence map, accepted "
        "deviations, and merge/refactor delta justify releasing this outcome?"
    ),
}
VERIFY_BUDGETS = {"intent": 1, "implementation": 2, "release": 0}


class ReviewProgramError(ValueError):
    """Raised when a purpose-bound review contract is incomplete or stale."""


def require_sha256(value: str, label: str, *, optional: bool = False) -> None:
    if optional and not value:
        return
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ReviewProgramError(f"{label} must be a lowercase sha256")


def require_relative_path(value: str, label: str, *, optional: bool = False) -> None:
    if optional and not value:
        return
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ReviewProgramError(f"{label} must be a repository-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ReviewProgramError(f"{label} must be a repository-relative path")


@dataclass(frozen=True)
class ReviewBoundaryInput:
    purpose: str
    outcome_contract_sha256: str
    plan_sha256: str = ""
    design_sha256: str = ""
    design_path: str = ""
    capability_dispositions_sha256: str = ""
    capability_dispositions_path: str = ""
    success_evidence_map_sha256: str = ""
    success_evidence_map_path: str = ""
    product_head_sha: str = ""
    verification_evidence_sha256: str = ""
    verification_evidence_path: str = ""
    integration_head_sha: str = ""
    outcome_evidence_map_sha256: str = ""
    outcome_evidence_map_path: str = ""
    accepted_deviations_sha256: str = ""
    accepted_deviations_path: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.purpose not in PURPOSES:
            raise ReviewProgramError("review boundary purpose is invalid")
        require_sha256(self.outcome_contract_sha256, "outcome contract digest")
        for label, value in {
            "plan_sha256": self.plan_sha256,
            "design_sha256": self.design_sha256,
            "capability_dispositions_sha256": self.capability_dispositions_sha256,
            "success_evidence_map_sha256": self.success_evidence_map_sha256,
            "verification_evidence_sha256": self.verification_evidence_sha256,
            "outcome_evidence_map_sha256": self.outcome_evidence_map_sha256,
            "accepted_deviations_sha256": self.accepted_deviations_sha256,
        }.items():
            require_sha256(value, label, optional=True)
        for label, value in {
            "design_path": self.design_path,
            "capability_dispositions_path": self.capability_dispositions_path,
            "success_evidence_map_path": self.success_evidence_map_path,
            "verification_evidence_path": self.verification_evidence_path,
            "outcome_evidence_map_path": self.outcome_evidence_map_path,
            "accepted_deviations_path": self.accepted_deviations_path,
        }.items():
            require_relative_path(value, label, optional=True)
        for value, label in (
            (self.product_head_sha, "product HEAD"),
            (self.integration_head_sha, "integration HEAD"),
        ):
            if value and not GIT_SHA.fullmatch(value):
                raise ReviewProgramError(f"{label} must be an exact Git object id")
        _validate_boundary_evidence(self)

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, object]
    ) -> "ReviewBoundaryInput":
        expected = {field.name for field in fields(cls)}
        if set(raw) != expected:
            raise ReviewProgramError("review boundary input fields are not exact")
        if any(
            not isinstance(value, str)
            for name, value in raw.items()
            if name != "schema_version"
        ):
            raise ReviewProgramError("review boundary input values must be strings")
        return cls(**raw)

    def payload(self) -> dict[str, object]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @property
    def input_sha256(self) -> str:
        canonical = json.dumps(
            self.payload(), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(canonical).hexdigest()


def _validate_boundary_evidence(boundary: ReviewBoundaryInput) -> None:
    intent = (
        boundary.design_sha256,
        boundary.design_path,
        boundary.capability_dispositions_sha256,
        boundary.capability_dispositions_path,
        boundary.success_evidence_map_sha256,
        boundary.success_evidence_map_path,
    )
    implementation = (
        boundary.product_head_sha,
        boundary.verification_evidence_sha256,
        boundary.verification_evidence_path,
    )
    release = (
        boundary.integration_head_sha,
        boundary.outcome_evidence_map_sha256,
        boundary.outcome_evidence_map_path,
        boundary.accepted_deviations_sha256,
        boundary.accepted_deviations_path,
    )
    required = {
        "intent": intent,
        "implementation": implementation,
        "release": release,
    }[boundary.purpose]
    if not boundary.plan_sha256 or not all(required):
        raise ReviewProgramError(
            f"{boundary.purpose} review boundary is missing required evidence"
        )
    forbidden = {
        "intent": implementation + release,
        "implementation": intent + release,
        "release": intent + implementation,
    }[boundary.purpose]
    if any(forbidden):
        raise ReviewProgramError(
            f"{boundary.purpose} review boundary carries foreign-stage evidence"
        )
