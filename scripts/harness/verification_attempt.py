"""Immutable bounded identity for one local pipeline verification execution."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
GIT_OID = re.compile(r"[0-9a-f]{40,64}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ATTEMPT_FIELDS = {
    "schema_version",
    "parent_operation_id",
    "profile",
    "profile_sha256",
    "exact_head_sha",
    "attempt_index",
}
MAX_SAME_HEAD_ATTEMPT_INDEX = 1


class VerificationAttemptError(ValueError):
    """Raised when verification attempt authority is invalid or exhausted."""


def verification_input_sha256(
    definition_sha256: str,
    head_sha: str,
    profile_sha256: str,
    schema_version: int,
) -> str:
    """Bind the canonical immutable input consumed by one verification run."""

    return hashlib.sha256(
        json.dumps(
            {
                "definition_sha256": definition_sha256,
                "head_sha": head_sha,
                "profile_sha256": profile_sha256,
                "schema_version": schema_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class VerificationAttempt:
    """Freeze parent, profile, exact HEAD, and the bounded retry index."""

    parent_operation_id: str
    profile: str
    profile_sha256: str
    exact_head_sha: str
    attempt_index: int
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.schema_version != 1
            or not isinstance(self.parent_operation_id, str)
            or not IDENTIFIER.fullmatch(self.parent_operation_id)
            or not isinstance(self.profile, str)
            or not IDENTIFIER.fullmatch(self.profile)
            or not isinstance(self.profile_sha256, str)
            or not SHA256.fullmatch(self.profile_sha256)
            or not isinstance(self.exact_head_sha, str)
            or not GIT_OID.fullmatch(self.exact_head_sha)
            or type(self.attempt_index) is not int
            or not 0 <= self.attempt_index <= MAX_SAME_HEAD_ATTEMPT_INDEX
        ):
            raise VerificationAttemptError("verification attempt identity is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "parent_operation_id": self.parent_operation_id,
            "profile": self.profile,
            "profile_sha256": self.profile_sha256,
            "exact_head_sha": self.exact_head_sha,
            "attempt_index": self.attempt_index,
        }

    @classmethod
    def from_dict(cls, value: object) -> "VerificationAttempt":
        if not isinstance(value, dict) or set(value) != ATTEMPT_FIELDS:
            raise VerificationAttemptError("verification attempt identity is invalid")
        return cls(
            parent_operation_id=value.get("parent_operation_id"),
            profile=value.get("profile"),
            profile_sha256=value.get("profile_sha256"),
            exact_head_sha=value.get("exact_head_sha"),
            attempt_index=value.get("attempt_index"),
            schema_version=value.get("schema_version"),
        )

    @property
    def sha256(self) -> str:
        canonical = json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(canonical).hexdigest()

    def same_head_retry(self) -> "VerificationAttempt":
        if self.attempt_index != 0:
            raise VerificationAttemptError("same-HEAD verification retry is exhausted")
        return replace(self, attempt_index=1)

    def changed_head(self, exact_head_sha: str) -> "VerificationAttempt":
        if exact_head_sha == self.exact_head_sha:
            raise VerificationAttemptError(
                "changed-HEAD verification requires a different exact HEAD"
            )
        return replace(self, exact_head_sha=exact_head_sha, attempt_index=0)


def mechanism_flake_decision_text(
    failed_attempt: VerificationAttempt,
    verification_operation_id: str,
) -> str:
    """Return the exact coordinator decision accepted for attempt 1."""

    if (
        failed_attempt.attempt_index != 0
        or not isinstance(verification_operation_id, str)
        or not IDENTIFIER.fullmatch(verification_operation_id)
    ):
        raise VerificationAttemptError(
            "same-HEAD mechanism-flake decision identity is invalid"
        )
    return (
        "authorize-one-same-head-verification-attempt-1:"
        f"parent={failed_attempt.parent_operation_id};"
        f"verification={verification_operation_id};"
        f"profile={failed_attempt.profile};"
        f"head={failed_attempt.exact_head_sha};"
        f"failed_attempt={failed_attempt.sha256}"
    )
