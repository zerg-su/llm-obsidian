"""Identity-bound recovery for accepted callbacks blocked on reviewer cleanup."""

from __future__ import annotations

import re
from typing import Any, Mapping

from .review_attempt import (
    ReviewAttempt,
    ReviewAttemptError,
    ReviewAttemptTerminalResult,
)
from .store import StoreError
from .workflows.review import finish_review_lane
from .workflows.review_gate_contracts import _atomic_json, _read_json


def _attention_attempt(controller: Any) -> tuple[dict[str, object], ReviewAttempt] | None:
    state = controller.read()
    raw_attempt = state.get("attempt")
    stored_lanes = state.get("lanes")
    if not isinstance(raw_attempt, Mapping):
        return None
    attempt = ReviewAttempt.from_mapping(raw_attempt)
    terminal = attempt.terminal
    if (
        len(attempt.identity.lanes) != 1
        or attempt.status != "terminal"
        or terminal is None
        or terminal.result != ReviewAttemptTerminalResult.ATTENTION_REQUIRED
        or terminal.lane_results
        or state.get("status") != "attention-required"
        or state.get("owner_id") != attempt.identity.lanes[0].owner_id
        or not isinstance(stored_lanes, list)
        or len(stored_lanes) != 1
        or state.get("round_results") not in ({}, None)
        or state.get("final_results") not in ({}, None)
        or state.get("evidence") not in ({}, None)
    ):
        return None
    raw_lane = stored_lanes[0]
    identity = attempt.identity.lanes[0]
    if (
        not isinstance(raw_lane, dict)
        or raw_lane.get("axis") != identity.axis
        or raw_lane.get("operation_id") != identity.operation_id
        or raw_lane.get("lane_id") != identity.lane_id
        or raw_lane.get("run_id") != identity.run_id
    ):
        return None
    return state, attempt


def _accepted_records(controller: Any, attempt: ReviewAttempt) -> tuple[object, object] | None:
    identity = attempt.identity.lanes[0]
    try:
        parent = controller.round_store.read(
            identity.owner_id, identity.operation_id
        )
        children = [
            record
            for record in controller.round_store.list(identity.owner_id)
            if record.spec.kind == "review-round"
            and record.spec.parent_operation_id == identity.operation_id
            and record.lane_id == identity.lane_id
        ]
    except StoreError:
        return None
    if len(children) != 1:
        return None
    child = children[0]
    if (
        parent.run_id != identity.run_id
        or parent.lane_id != identity.lane_id
        or parent.spec.route.runtime != identity.runtime
        or parent.spec.route.model != identity.model
        or parent.spec.route.effort != identity.effort
        or parent.spec.route.profile != identity.profile
        or parent.spec.route.routing_sha256 != identity.routing_sha256
        or parent.pending_effect
        or parent.state not in {"attention-required", "exiting", "complete"}
        or (
            parent.state == "attention-required"
            and parent.resume_state != "exiting"
        )
        or child.pending_effect
        or child.state not in {"verifying", "finalizing", "exiting", "complete"}
        or child.accepted_callback_kind != "review"
        or not child.accepted_callback_id
        or re.fullmatch(r"[0-9a-f]{64}", child.accepted_callback_sha256)
        is None
    ):
        return None
    return parent, child


def recover_accepted_callback_cleanup(controller: Any) -> bool:
    """Finish exact cleanup, then reopen the same callback-bound attempt."""

    observed = _attention_attempt(controller)
    if observed is None:
        return False
    state, attempt = observed
    if _accepted_records(controller, attempt) is None:
        return False
    run = controller.rehydrate()
    if len(run.execution.lanes) != 1:
        return False
    cleanup = finish_review_lane(controller.runtime, run.execution.lanes[0])
    if cleanup.state != "complete" or any(
        (
            cleanup.resources.surface_id,
            cleanup.resources.process_group,
            cleanup.resources.supervisor_pid,
        )
    ):
        return False
    raw_lane = state["lanes"][0]
    recovered = ReviewAttempt(attempt.identity, "awaiting-callback")
    restored_lane = {
        **raw_lane,
        "surface_id": "",
        "checkpoint": "",
        "checkpoint_sha256": "",
        "state": "complete",
    }
    with controller._locked():
        current = _read_json(controller.state_path)
        if current != state:
            raise ReviewAttemptError(
                "accepted callback cleanup gate changed during recovery"
            )
        current.update(
            status="reviewing",
            lanes=[restored_lane],
            attempt=recovered.payload(),
        )
        _atomic_json(controller.state_path, current)
    return True


def recover_interrupted_review_attempt(controller: Any) -> bool:
    """Select one exact no-replay recovery for the frozen attempt."""

    return recover_accepted_callback_cleanup(
        controller
    ) or controller.recover_late_started_attempt()
