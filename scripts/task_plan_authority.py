"""Resolve one active task plan from its base snapshot and explicit amendments."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from approved_plan_snapshot import (
    ApprovedPlanSnapshot,
    PlanSnapshotError,
    bind_approved_plan_snapshot,
    validate_approved_plan_snapshot,
)
from outcome_contract import OutcomeContractError, extract_from_bytes
from task_escalation_records import (
    DecisionRecord,
    EscalationRecordError,
    append_amendment,
    load_amendments,
    load_latest,
    load_pointed_amendments,
)


class PlanAuthorityError(ValueError):
    """The base snapshot or explicit amendment lineage is not authoritative."""


@dataclass(frozen=True)
class TaskPlanAuthority:
    path: Path
    content: bytes
    plan_sha256: str
    outcome_sha256: str
    amendments: tuple[DecisionRecord, ...]


def _outcome(content: bytes) -> str:
    try:
        return extract_from_bytes(content).sha256
    except OutcomeContractError as exc:
        raise PlanAuthorityError("active plan Outcome Contract is invalid") from exc


def _record_snapshot(
    meta: Mapping[str, Any], worktree: Path, record: DecisionRecord
) -> ApprovedPlanSnapshot:
    payload = record.payload
    try:
        return validate_approved_plan_snapshot(
            {
                "vault_root": meta.get("vault_root"),
                "worktree": str(worktree),
                "plan_snapshot_file": payload.get("new_plan_snapshot_file"),
                "approved_plan_sha256": payload.get("new_plan_sha256"),
            }
        )
    except PlanSnapshotError as exc:
        raise PlanAuthorityError("amendment plan snapshot is invalid") from exc


def _resolve_plan_authority(
    meta: Mapping[str, Any],
    worktree: Path,
    amendments: tuple[DecisionRecord, ...],
) -> TaskPlanAuthority:
    root = worktree.expanduser().resolve()
    try:
        base = validate_approved_plan_snapshot(meta)
    except PlanSnapshotError as exc:
        raise PlanAuthorityError("active plan authority is invalid") from exc
    task_id = str(meta.get("task_id") or "")
    current_path = base.path
    current_content = base.content
    current_plan = base.sha256
    current_outcome = _outcome(base.content)
    expected_base_outcome = str(meta.get("outcome_contract_sha256") or "")
    if current_outcome != expected_base_outcome:
        raise PlanAuthorityError("base plan Outcome identity is stale")
    prior_amendment: DecisionRecord | None = None
    for record in amendments:
        payload = record.payload
        expected_prior_id = (
            "" if prior_amendment is None else prior_amendment.record_id
        )
        expected_prior_sha = "" if prior_amendment is None else prior_amendment.sha256
        if (
            payload.get("task_id") != task_id
            or payload.get("root_operation_id") != task_id
            or payload.get("prior_plan_sha256") != current_plan
            or payload.get("prior_outcome_sha256") != current_outcome
            or payload.get("prior_amendment_id") != expected_prior_id
            or payload.get("prior_amendment_sha256") != expected_prior_sha
        ):
            raise PlanAuthorityError("amendment predecessor or task identity is stale")
        snapshot = _record_snapshot(meta, root, record)
        outcome = _outcome(snapshot.content)
        if outcome != payload.get("new_outcome_sha256"):
            raise PlanAuthorityError("amendment plan/outcome chain is mixed")
        current_path = snapshot.path
        current_content = snapshot.content
        current_plan = snapshot.sha256
        current_outcome = outcome
        prior_amendment = record
    return TaskPlanAuthority(
        current_path,
        current_content,
        current_plan,
        current_outcome,
        amendments,
    )


def resolve_plan_authority(
    meta: Mapping[str, Any], worktree: Path
) -> TaskPlanAuthority:
    """Return the only ordered, identity-bound active plan for one task."""

    root = worktree.expanduser().resolve()
    try:
        amendments = load_amendments(root)
    except EscalationRecordError as exc:
        raise PlanAuthorityError("active plan authority is invalid") from exc
    return _resolve_plan_authority(meta, root, amendments)


def record_plan_amendment(
    worktree: Path, plan_file: Path, *, decision: str
) -> DecisionRecord:
    """Capture new plan bytes, then append their exact predecessor-bound record."""

    root = worktree.expanduser().resolve()
    try:
        import json

        meta = json.loads((root / ".task-meta.json").read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            raise ValueError("metadata root")
        authority = _resolve_plan_authority(
            meta, root, load_pointed_amendments(root)
        )
        bound = bind_approved_plan_snapshot(
            {
                "vault_root": Path(str(meta.get("vault_root") or "")),
                "plan_file": plan_file.expanduser().resolve(),
            }
        )
        snapshot = validate_approved_plan_snapshot(
            {
                "vault_root": meta.get("vault_root"),
                "worktree": str(root),
                "plan_snapshot_file": str(bound["_approved_plan_file"]),
                "approved_plan_sha256": bound["_approved_plan_sha256"],
            }
        )
        new_outcome = _outcome(snapshot.content)
        if snapshot.sha256 == authority.plan_sha256:
            raise PlanAuthorityError("amendment must change the active plan identity")
        prior = authority.amendments[-1] if authority.amendments else None
        latest = load_latest(root)
        return append_amendment(
            root,
            task_id=str(meta.get("task_id") or ""),
            root_operation_id=str(meta.get("task_id") or ""),
            prior_plan_sha256=authority.plan_sha256,
            prior_outcome_sha256=authority.outcome_sha256,
            prior_amendment_id="" if prior is None else prior.record_id,
            prior_amendment_sha256="" if prior is None else prior.sha256,
            new_plan_sha256=snapshot.sha256,
            new_plan_snapshot_file=str(snapshot.path),
            new_outcome_sha256=new_outcome,
            decision=decision,
            expected_record_sha256=None if latest is None else latest.sha256,
        )
    except PlanAuthorityError:
        raise
    except (OSError, ValueError, EscalationRecordError, PlanSnapshotError) as exc:
        raise PlanAuthorityError("cannot record the explicit plan amendment") from exc
