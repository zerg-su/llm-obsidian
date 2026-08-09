"""Read-only RC1 projection over existing durable lifecycle owners."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from harness.contracts import EffectOutcome, OperationRecord, OwnedResources
from harness.finalization_ledger import FinalizationLedger, FinalizationLedgerError
from harness.review_finalization import require_task_review
from harness.state_machine import TERMINAL
from harness.store import OperationStore, StoreError


class LiveAuthorityError(ValueError):
    """The selected corridor lacks exact accepted durable authority."""


@dataclass(frozen=True)
class DispatchAuthority:
    request_id: str
    owner_id: str
    operation_id: str
    lane_id: str
    run_id: str
    worktree: Path
    meta: dict[str, object]
    store_root: Path


def _durable_json(path: Path, label: str) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise LiveAuthorityError(f"{label} is unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveAuthorityError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise LiveAuthorityError(f"{label} is invalid")
    return value


def _dispatch_authority(
    receipt: dict[str, object], position: int, root: Path
) -> DispatchAuthority:
    request_id = str(receipt["request_id"])
    dispatch = _durable_json(
        root / ".vault-meta" / "dispatch-runs" / f"{request_id}.json",
        f"receipt {position} accepted dispatch record",
    )
    launch = dispatch.get("result")
    harness = launch.get("harness") if isinstance(launch, dict) else None
    if (
        dispatch.get("schema_version") != 1
        or dispatch.get("request_id") != request_id
        or dispatch.get("status") != "launched"
        or not isinstance(launch, dict)
        or launch.get("schema_version") != 1
        or launch.get("status") != "launched"
        or launch.get("request_id") != request_id
        or not isinstance(harness, dict)
    ):
        raise LiveAuthorityError(
            f"receipt {position} accepted dispatch identity is invalid"
        )
    owner_id = str(harness.get("owner_id") or "")
    operation_id = str(harness.get("operation_id") or "")
    lane_id = str(harness.get("lane_id") or "")
    run_id = str(harness.get("run_id") or "")
    if not all((owner_id, operation_id, lane_id, run_id)) or (
        request_id != owner_id
        or owner_id != operation_id
        or receipt["owner_id"] != owner_id
        or receipt["run_id"] != run_id
    ):
        raise LiveAuthorityError(
            f"receipt {position} does not match the launched Harness identity"
        )
    raw_worktree = launch.get("worktree")
    if not isinstance(raw_worktree, str) or not Path(raw_worktree).is_absolute():
        raise LiveAuthorityError(
            f"receipt {position} worktree identity is invalid"
        )
    worktree = Path(raw_worktree).expanduser().resolve()
    meta = _durable_json(
        worktree / ".task-meta.json", f"receipt {position} task contract"
    )
    _validate_task_identity(receipt, position, root, request_id, worktree, meta)
    store_root = root.resolve() / ".vault-meta" / "harness"
    if receipt["store_id"] != f"{store_root}#owners/{owner_id}":
        raise LiveAuthorityError(
            f"receipt {position} does not match the durable store identity"
        )
    return DispatchAuthority(
        request_id,
        owner_id,
        operation_id,
        lane_id,
        run_id,
        worktree,
        meta,
        store_root,
    )


def _validate_task_identity(
    receipt: dict[str, object],
    position: int,
    root: Path,
    request_id: str,
    worktree: Path,
    meta: dict[str, object],
) -> None:
    declared_worktree = Path(str(meta.get("worktree") or "")).expanduser()
    receipt_worktree = Path(str(receipt["worktree_id"])).expanduser()
    declared_vault = Path(str(meta.get("vault_root") or "")).expanduser()
    if (
        meta.get("task_id") != request_id
        or not declared_worktree.is_absolute()
        or declared_worktree.resolve() != worktree
        or not declared_vault.is_absolute()
        or declared_vault.resolve() != root.resolve()
        or not receipt_worktree.is_absolute()
        or receipt_worktree.resolve() != worktree
    ):
        raise LiveAuthorityError(f"receipt {position} task identity is stale")


def _operation_authority(
    identity: DispatchAuthority,
    receipt: dict[str, object],
    position: int,
) -> tuple[OperationRecord, list[OperationRecord]]:
    store = OperationStore(identity.store_root)
    try:
        root_record = store.read(identity.owner_id, identity.operation_id)
        owned_records = store.list(identity.operation_id)
    except (OSError, StoreError, ValueError) as exc:
        raise LiveAuthorityError(
            f"receipt {position} live corridor authority is unavailable"
        ) from exc
    route = root_record.spec.route
    if (
        root_record.spec.kind != "dispatch"
        or root_record.spec.owner_id != identity.owner_id
        or root_record.lane_id != identity.lane_id
        or root_record.run_id != identity.run_id
        or root_record.state != "complete"
        or root_record.resources != OwnedResources()
        or root_record.pending_effect
        or root_record.effect_id != "reap-finalize"
        or root_record.effect_outcome != EffectOutcome.SUCCEEDED
        or root_record.accepted_callback_kind != "wiki-summary"
        or receipt["executor_route"]
        != {
            "runtime": route.runtime,
            "model": route.model,
            "effort": route.effort,
        }
    ):
        raise LiveAuthorityError(
            f"receipt {position} executor corridor is not terminally accepted"
        )
    return root_record, owned_records


def _review_authority(
    identity: DispatchAuthority,
    receipt: dict[str, object],
    position: int,
) -> None:
    policy = identity.meta.get("review_policy")
    expected_route = (
        {key: policy.get(key) for key in ("mode", "runtime", "model", "effort")}
        if isinstance(policy, dict)
        else None
    )
    if receipt["review_route"] != expected_route:
        raise LiveAuthorityError(f"receipt {position} review route is stale")
    try:
        authorization = require_task_review(
            identity.meta,
            identity.worktree,
            expected_vault=identity.store_root.parents[1],
            expected_operation_id=identity.operation_id,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise LiveAuthorityError(
            f"receipt {position} review/verification authority is unavailable"
        ) from exc
    if not authorization.approved:
        raise LiveAuthorityError(
            f"receipt {position} review/verification is not approved"
        )


def _runtime_authority(
    records: list[OperationRecord],
    root_record: OperationRecord,
    identity: DispatchAuthority,
    receipt: dict[str, object],
    position: int,
) -> tuple[list[OperationRecord], list[OperationRecord]]:
    relevant = [
        row
        for row in records
        if row.spec.kind == "dispatch"
        or row.spec.kind == "pipeline-verify"
        or row.spec.kind == "review-round"
        or row.spec.kind.startswith(
            ("simple-review-", "deep-review-", "full-review-")
        )
    ]
    if any(
        row.state != "complete"
        or row.resources != OwnedResources()
        or bool(row.pending_effect)
        for row in relevant
    ):
        raise LiveAuthorityError(
            f"receipt {position} retains non-terminal runtime resources"
        )
    verification = [
        row
        for row in relevant
        if row.spec.kind == "pipeline-verify"
        and row.spec.parent_operation_id == identity.operation_id
    ]
    reviews = [row for row in relevant if row.spec.kind == "review-round"]
    if not verification or any(
        row.effect_outcome != EffectOutcome.SUCCEEDED for row in verification
    ):
        raise LiveAuthorityError(
            f"receipt {position} lacks accepted verification evidence"
        )
    if not reviews or any(
        row.accepted_callback_kind != "review" or not row.accepted_callback_id
        for row in reviews
    ):
        raise LiveAuthorityError(
            f"receipt {position} lacks accepted provider/review evidence"
        )
    provider_sessions = sorted({root_record.run_id, *(row.run_id for row in reviews)})
    if receipt["provider_session_ids"] != provider_sessions:
        raise LiveAuthorityError(
            f"receipt {position} provider sessions are not runtime-derived"
        )
    return verification, reviews


def _current_head(identity: DispatchAuthority, position: int) -> str:
    result = subprocess.run(
        ["git", "-C", str(identity.worktree), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    current_head = result.stdout.strip()
    if result.returncode or len(current_head) != 40:
        raise LiveAuthorityError(
            f"receipt {position} corrected HEAD is not review-derived"
        )
    return current_head


def _finalization_authority(
    identity: DispatchAuthority,
    current_head: str,
    verification: list[OperationRecord],
    reviews: list[OperationRecord],
    position: int,
) -> bool:
    policy = identity.meta.get("finalization_policy")
    max_cycles = policy.get("max_cycles") if isinstance(policy, dict) else None
    try:
        ledger = FinalizationLedger(
            identity.store_root / "finalization-ledger",
            lineage_id=identity.operation_id,
            origin_task_id=identity.operation_id,
            plan_sha256=str(identity.meta.get("approved_plan_sha256") or ""),
            outcome_contract_sha256=str(
                identity.meta.get("outcome_contract_sha256") or ""
            ),
            max_cycles=max_cycles,
        ).snapshot()
    except (OSError, TypeError, FinalizationLedgerError) as exc:
        raise LiveAuthorityError(
            f"receipt {position} finalization authority is unavailable"
        ) from exc
    cycles = ledger.get("cycles")
    if not isinstance(cycles, list) or not cycles:
        raise LiveAuthorityError(
            f"receipt {position} finalization lineage is unavailable"
        )
    if (
        ledger.get("terminal_disposition") != "approved"
        or cycles[-1].get("terminal_result") != "approved"
        or cycles[-1].get("exact_head") != current_head
    ):
        raise LiveAuthorityError(
            f"receipt {position} finalization lineage is not terminally approved"
        )
    if any(
        cycle.get("task_id") != identity.operation_id
        or Path(str(cycle.get("worktree") or "")).expanduser().resolve()
        != identity.worktree
        for cycle in cycles
    ):
        raise LiveAuthorityError(
            f"receipt {position} finalization lineage identity is stale"
        )
    material = any(
        cycle.get("terminal_result") == "changes-requested" for cycle in cycles
    )
    if len(verification) < len(cycles) or len(reviews) < len(cycles):
        raise LiveAuthorityError(
            f"receipt {position} material corridor is incomplete"
        )
    if material and len(cycles) < 2:
        raise LiveAuthorityError(
            f"receipt {position} material corridor is incomplete"
        )
    return material


def validate_live_corridor(
    receipt: dict[str, object], position: int, *, root: Path
) -> tuple[bool, str]:
    """Re-derive one accepted cell without creating lifecycle authority."""

    identity = _dispatch_authority(receipt, position, Path(root))
    root_record, records = _operation_authority(identity, receipt, position)
    _review_authority(identity, receipt, position)
    verification, reviews = _runtime_authority(
        records, root_record, identity, receipt, position
    )
    current_head = _current_head(identity, position)
    material = _finalization_authority(
        identity, current_head, verification, reviews, position
    )
    return material, current_head


def validate_live_non_success(
    receipt: dict[str, object], position: int, *, root: Path
) -> None:
    """Bind one failed/invalidated cell to a terminal resource-free run.

    This is negative closure only: it can reset the streak and release the
    gate reservation, but it cannot establish a successful cell.  The durable
    dispatch/store identity and terminal state remain authoritative; caller
    result/recovery fields merely select the matching negative disposition.
    """

    identity = _dispatch_authority(receipt, position, Path(root))
    store = OperationStore(identity.store_root)
    try:
        root_record = store.read(identity.owner_id, identity.operation_id)
        records = store.list(identity.operation_id)
    except (OSError, StoreError, ValueError) as exc:
        raise LiveAuthorityError(
            f"receipt {position} negative corridor authority is unavailable"
        ) from exc
    expected_state = {
        "failed": ("failed", False),
        "invalidated": ("cancelled", True),
    }.get(str(receipt["result"]))
    route = root_record.spec.route
    policy = identity.meta.get("review_policy")
    expected_review = (
        {key: policy.get(key) for key in ("mode", "runtime", "model", "effort")}
        if isinstance(policy, dict)
        else None
    )
    if (
        expected_state is None
        or root_record.spec.kind != "dispatch"
        or root_record.spec.owner_id != identity.owner_id
        or root_record.lane_id != identity.lane_id
        or root_record.run_id != identity.run_id
        or root_record.state != expected_state[0]
        or receipt["coordinator_recovery"] is not expected_state[1]
        or receipt["resource_free"] is not True
        or receipt["executor_route"]
        != {
            "runtime": route.runtime,
            "model": route.model,
            "effort": route.effort,
        }
        or receipt["review_route"] != expected_review
    ):
        raise LiveAuthorityError(
            f"receipt {position} negative corridor disposition is not durable"
        )
    relevant = [
        row
        for row in records
        if row.spec.kind == "dispatch"
        or row.spec.kind == "pipeline-verify"
        or row.spec.kind == "review-round"
        or row.spec.kind.startswith(
            ("simple-review-", "deep-review-", "full-review-")
        )
    ]
    if any(
        row.state not in TERMINAL
        or row.resources != OwnedResources()
        or bool(row.pending_effect)
        for row in relevant
    ):
        raise LiveAuthorityError(
            f"receipt {position} negative corridor retains live resources"
        )
    reviews = [row for row in relevant if row.spec.kind == "review-round"]
    provider_sessions = sorted({root_record.run_id, *(row.run_id for row in reviews)})
    if receipt["provider_session_ids"] != provider_sessions:
        raise LiveAuthorityError(
            f"receipt {position} provider sessions are not runtime-derived"
        )
