"""Task facade for the coordinator-authorized post-fresh synchronizer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from harness.store import OperationStore
from task_escalation_records import EscalationRecordError, load_latest
from task_review_context import _validate_task
from task_review_drift_contract import authorized_post_fresh_publication_sync
from task_review_post_fresh_publication import synchronize_post_fresh_publication
from task_review_shared import TaskReviewError


def recover_post_fresh_publication_sync(
    worktree: Path,
    *,
    fault_observer: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    """Finish only the publication of already-created fresh review lanes."""

    worktree = worktree.expanduser().resolve()
    _meta, vault, task_id = _validate_task(worktree)
    try:
        attention_record = load_latest(worktree)
    except EscalationRecordError as exc:
        raise TaskReviewError(f"task escalation record is invalid: {exc}") from exc
    if attention_record is None:
        return None
    authorization = authorized_post_fresh_publication_sync(
        attention_record, worktree
    )
    if authorization is None:
        return None
    if authorization.continuation.dispatch_operation_id != task_id:
        raise TaskReviewError(
            "post-fresh dispatch authorization identity drifted"
        )
    store = OperationStore(vault / ".vault-meta" / "harness")
    sync_path = (
        store.root
        / "owners"
        / task_id
        / "runtime"
        / task_id
        / "post-fresh-publication-sync.json"
    )
    previously_applied = False
    if sync_path.is_file() and not sync_path.is_symlink():
        try:
            previously_applied = (
                json.loads(sync_path.read_text(encoding="utf-8")).get("status")
                == "applied"
            )
        except (OSError, AttributeError, json.JSONDecodeError):
            pass
    receipt = synchronize_post_fresh_publication(
        worktree,
        store=store,
        operation_id=task_id,
        authorization=authorization,
        fault_observer=fault_observer,
    )
    return None if previously_applied else receipt
