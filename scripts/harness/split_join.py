"""Pure manifest-ordered join decisions over exact terminal child receipts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .contracts import ContractError, ID_RE, SHA256_RE
from .split_validation import ValidatedSplit


TERMINAL_STATUSES = frozenset(
    {"approved", "attention-required", "failed", "cancelled", "conflict"}
)
STOP_STATUSES = frozenset(TERMINAL_STATUSES - {"approved"})
JOIN_DISPOSITIONS = frozenset(
    {
        "ready",
        "attention-required",
        "failed",
        "cancelled",
        "conflict",
        "stale-head",
        "receipt-invalid",
    }
)


def _sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ContractError(f"{label} must be a lowercase sha256")


@dataclass(frozen=True)
class ChildReceipt:
    manifest_sha256: str
    subplan_id: str
    branch: str
    head_sha: str
    summary_sha256: str
    review_receipt_sha256: str
    evidence_ids: tuple[str, ...]
    status: str

    def __post_init__(self) -> None:
        _sha256(self.manifest_sha256, "receipt manifest_sha256")
        if not isinstance(self.subplan_id, str) or not ID_RE.fullmatch(self.subplan_id):
            raise ContractError("receipt subplan_id must be a bounded identifier")
        if (
            not isinstance(self.branch, str)
            or not self.branch
            or self.branch.startswith("/")
            or ".." in self.branch.split("/")
        ):
            raise ContractError("receipt branch must be a bounded repository ref")
        if (
            not isinstance(self.head_sha, str)
            or len(self.head_sha) not in {40, 64}
            or any(char not in "0123456789abcdef" for char in self.head_sha)
        ):
            raise ContractError("receipt head_sha must be a lowercase Git object id")
        _sha256(self.summary_sha256, "receipt summary_sha256")
        _sha256(self.review_receipt_sha256, "receipt review_receipt_sha256")
        if (
            not isinstance(self.evidence_ids, tuple)
            or not self.evidence_ids
            or len(set(self.evidence_ids)) != len(self.evidence_ids)
            or any(not isinstance(item, str) or not ID_RE.fullmatch(item) for item in self.evidence_ids)
        ):
            raise ContractError("receipt evidence_ids must be unique bounded identifiers")
        if self.status not in TERMINAL_STATUSES:
            raise ContractError("receipt status must be terminal")


@dataclass(frozen=True)
class JoinItem:
    subplan_id: str
    branch: str
    head_sha: str
    summary_sha256: str
    review_receipt_sha256: str


@dataclass(frozen=True)
class JoinDecision:
    disposition: str
    reason: str
    integration_order: tuple[JoinItem, ...] = ()
    parent_evidence_proven: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.disposition not in JOIN_DISPOSITIONS or not self.reason:
            raise ContractError("join decision is invalid")
        if self.disposition != "ready" and self.integration_order:
            raise ContractError("stopped join cannot expose an integration order")


def _stop(disposition: str, reason: str) -> JoinDecision:
    return JoinDecision(disposition=disposition, reason=reason)


def evaluate_join(
    validated: ValidatedSplit,
    receipts: tuple[ChildReceipt, ...],
    *,
    current_heads: Mapping[str, str],
) -> JoinDecision:
    """Accept only exact approved receipts in immutable manifest order."""

    if not isinstance(validated, ValidatedSplit):
        raise ContractError("join requires a validated Split capability")
    if not isinstance(receipts, tuple) or any(
        not isinstance(item, ChildReceipt) for item in receipts
    ):
        raise ContractError("join receipts must be a typed tuple")
    manifest = validated.manifest
    ordered_subplans = manifest.subplans
    expected_ids = tuple(item.subplan_id for item in ordered_subplans)
    observed_ids = tuple(item.subplan_id for item in receipts)
    if observed_ids != expected_ids:
        return _stop("receipt-invalid", "child receipts changed manifest order or coverage")
    if set(current_heads) != set(expected_ids):
        return _stop("receipt-invalid", "current HEAD map changed child coverage")

    for subplan, receipt in zip(ordered_subplans, receipts, strict=True):
        if (
            receipt.manifest_sha256 != manifest.manifest_sha256
            or receipt.evidence_ids != subplan.evidence_ids
        ):
            return _stop("receipt-invalid", f"{subplan.subplan_id} receipt identity drifted")
        current_head = current_heads[subplan.subplan_id]
        if (
            not isinstance(current_head, str)
            or current_head != receipt.head_sha
        ):
            return _stop("stale-head", f"{subplan.subplan_id} HEAD changed after receipt")
        if receipt.status in STOP_STATUSES:
            return _stop(receipt.status, f"{subplan.subplan_id} ended {receipt.status}")

    proven = {
        evidence_id for receipt in receipts for evidence_id in receipt.evidence_ids
    }
    if proven != set(manifest.parent.evidence_ids):
        return _stop("receipt-invalid", "approved receipts do not prove exact parent evidence")
    integration_order = tuple(
        JoinItem(
            subplan_id=receipt.subplan_id,
            branch=receipt.branch,
            head_sha=receipt.head_sha,
            summary_sha256=receipt.summary_sha256,
            review_receipt_sha256=receipt.review_receipt_sha256,
        )
        for receipt in receipts
    )
    return JoinDecision(
        disposition="ready",
        reason="all exact approved receipts are current and parent evidence is complete",
        integration_order=integration_order,
        parent_evidence_proven=manifest.parent.evidence_ids,
    )
