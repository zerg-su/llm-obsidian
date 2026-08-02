"""Review transport and cross-runtime composition for live acceptance."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable
import uuid

from harness.contracts import OperationRecord
from live_acceptance_contracts import (
    LiveDriverError,
    RuntimeSessions,
    _PlannedOperation,
    _route,
    _stable_id,
)
from live_acceptance_runtime import (
    _atomic_text,
    _await_cleanup,
    _operation_evidence,
    _read_callback,
    _result_record,
)


def _axis_directory(axis: str) -> str:
    return (
        "standards"
        if axis == "standards-correctness-architecture-security"
        else axis
    )


def _review_probe_prompt(callback_root: str, product_root: Path) -> str:
    return (
        "This is a bounded product-read-only review transport probe. "
        "Do not edit tracked repository files and do not start another model. "
        "Use only the owner-scoped reviewer callback scratch below. Copy the "
        "exact contents of expected.json atomically to its sibling "
        ".review-callback.json, then wait for coordinator cleanup.\n\n"
        f"Callback scratch root: {callback_root}\n"
        f"Reviewed product root (read-only): {product_root}\n"
        "For a holistic session use holistic/expected.json. For the deep "
        "Claude spec session use spec/expected.json. For the deep Codex "
        "correctness session use standards/expected.json.\n"
    )


def _review_scratch(root: Path, commit_sha: str, cell_id: str) -> Path:
    scratch_base = (
        Path(tempfile.gettempdir())
        / f"llm-obsidian-live-review-{os.getuid()}"
    )
    if scratch_base.is_symlink():
        raise LiveDriverError("review callback scratch cannot be a symlink")
    scratch_base.mkdir(parents=True, exist_ok=True)
    scratch_base.chmod(0o700)
    scratch = scratch_base
    for component in (
        _stable_id(str(root), length=32),
        commit_sha,
        cell_id,
    ):
        scratch = scratch / component
        if scratch.is_symlink():
            raise LiveDriverError("review callback scratch cannot contain symlinks")
        scratch.mkdir(exist_ok=True)
        scratch.chmod(0o700)
    return scratch.resolve()


def _dispatch_ack(operation_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "operation_id": operation_id,
        "status": "ready-for-review",
    }


def _dispatch_probe_prompt(operation_id: str) -> str:
    return (
        "This is a bounded LLM Obsidian composition probe. "
        "Do not edit tracked repository files and do not start another model. "
        "Do not write .task-summary.json: finalization belongs to the "
        "code-owned acceptance coordinator after automatic review. Atomically "
        "write the exact JSON object below to .live-dispatch-ack.json, then "
        "wait for coordinator cleanup.\n\n"
        f"Ack JSON: {json.dumps(_dispatch_ack(operation_id), sort_keys=True)}\n"
    )


def _read_dispatch_ack(
    path: Path,
    *,
    expected: dict[str, object],
    manager: RuntimeSessions,
    owner_id: str,
    operation_id: str,
    deadline: float,
    sleep: Callable[[float], None],
) -> OperationRecord:
    while True:
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise LiveDriverError(
                    f"cannot read dispatch acceptance ack: {exc}"
                ) from exc
            if value != expected:
                raise LiveDriverError("dispatch acceptance ack changed identity")
            return _result_record(manager.status(owner_id, operation_id))
        record = _result_record(manager.status(owner_id, operation_id))
        if record.state in {
            "complete",
            "failed",
            "cancelled",
            "attention-required",
        }:
            raise LiveDriverError(
                "dispatch stopped before automatic review acknowledgement"
            )
        if time.monotonic() >= deadline:
            raise LiveDriverError("dispatch acceptance ack timed out")
        sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def _run_review_sessions(
    root: Path,
    *,
    cell_id: str,
    commit_sha: str,
    fingerprint: str,
    owner_id: str,
    origin_surface: str,
    manager: RuntimeSessions,
    deadline: float,
    sleep: Callable[[float], None],
    shared_lane_id: str = "",
    dispatch_operation_id: str = "",
) -> list[dict[str, Any]]:
    from harness.contracts import to_dict
    from harness.workflows.review import (
        ReviewContext,
        ReviewLaneSession,
        ReviewOperationRequest,
        ReviewRequest,
        ReviewResult,
        review_round_envelope,
    )
    from harness.workflows.review_gate import (
        ReviewGateController,
        authorize_task_finalization,
    )

    deep = cell_id == "deep-review"
    base_id = f"live-{commit_sha[:12]}-{cell_id}-review"
    scratch = _review_scratch(root, commit_sha, cell_id)
    callback_root = (Path(base_id) / "callbacks").as_posix()
    prompt_pointer = (Path(base_id) / "prompt.md").as_posix()
    axes = (
        ("spec", "standards-correctness-architecture-security")
        if deep
        else ("holistic",)
    )
    for axis in axes:
        directory = scratch / callback_root / _axis_directory(axis)
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    _atomic_text(
        scratch / prompt_pointer,
        _review_probe_prompt(
            (scratch / callback_root).resolve().as_posix(),
            root,
        ),
    )

    if deep:
        planned = {
            "spec": _PlannedOperation(
                "deep-review-spec", "claude", "spec"
            ),
            "standards-correctness-architecture-security": _PlannedOperation(
                "deep-review-correctness", "codex", "correctness"
            ),
        }
    else:
        planned = {
            "holistic": _PlannedOperation(
                "simple-review", "claude", "composition"
            )
        }
    axis_routes = {
        axis: _route(root, operation) for axis, operation in planned.items()
    }
    policy = ReviewRequest(
        base_id,
        depth="deep" if deep else "simple",
        cross_model=True,
        max_verify_iterations=2 if deep else 1,
    )
    request = ReviewOperationRequest(
        policy,
        owner_id,
        axis_routes[axes[0]],
        ReviewContext(
            prompt_pointer,
            commit_sha,
            "live-acceptance",
            fingerprint,
        ),
        axis_routes=axis_routes,
        lane_ids=(
            {"holistic": shared_lane_id}
            if shared_lane_id
            else None
        ),
    )
    def prepare_lane(
        axis: str,
        session_request: object,
        _result: object,
        round_: object,
    ) -> None:
        result = ReviewResult(axis, "approve", verification_iteration=0)
        envelope = review_round_envelope(round_, result)
        callback_path = scratch / str(session_request.callback_pointer)
        _atomic_text(
            callback_path.with_name("expected.json"),
            json.dumps(
                to_dict(envelope),
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
        )

    gate_root = scratch / base_id / "gate"
    gate = ReviewGateController(gate_root, manager, manager.store)
    gate_run = gate.begin(
        dispatch_operation_id=(
            dispatch_operation_id or base_id
        ),
        request=request,
        origin_surface=origin_surface,
        cwd=scratch,
        product_root=root,
        prompt_pointer=prompt_pointer,
        callback_root=callback_root,
        prepare_lane=prepare_lane,
    )
    execution = gate_run.execution
    evidence: list[dict[str, Any]] = []
    for lane in execution.lanes:
        if not isinstance(lane, ReviewLaneSession) or lane.axis not in axes:
            raise LiveDriverError("review runtime returned an unknown parent lane")
        round_ = gate_run.rounds[lane.axis]
        envelope = review_round_envelope(
            round_,
            ReviewResult(lane.axis, "approve", verification_iteration=0),
        )
        callback_path = (
            scratch
            / callback_root
            / _axis_directory(lane.axis)
            / ".review-callback.json"
        )
        _atomic_text(
            callback_path.with_name("expected.json"),
            json.dumps(
                to_dict(envelope),
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
        )
        child = _read_callback(
            callback_path,
            manager,
            owner_id=round_.owner_id,
            operation_id=round_.operation_id,
            expected=envelope,
            deadline=deadline,
            sleep=sleep,
        )
        if not child.accepted_callback_id:
            raise LiveDriverError(
                f"{round_.operation_id}: review callback receipt is missing"
            )
        decision = gate.complete_round(
            gate_run,
            lane,
            round_,
            ReviewResult(
                lane.axis,
                "approve",
                verification_iteration=0,
            ),
        )
        terminal_child = manager.store.read(
            round_.owner_id, round_.operation_id
        )
        if terminal_child.state != "complete":
            raise LiveDriverError(
                f"{round_.operation_id}: review callback child is not terminal"
            )
        _await_cleanup(
            manager,
            owner_id=lane.owner_id,
            operation_id=lane.operation_id,
            deadline=deadline,
            sleep=sleep,
        )
        parent = _result_record(
            manager.status(lane.owner_id, lane.operation_id)
        )
        evidence.append(_operation_evidence(parent, callback_count=1))
        if decision.action not in {"awaiting-axes", "approved"}:
            raise LiveDriverError("review gate did not reach an approvable state")
    if dispatch_operation_id:
        authorization = authorize_task_finalization(
            gate_root,
            dispatch_operation_id=dispatch_operation_id,
            expected_head_sha=commit_sha,
            expected_profile="live-acceptance",
            expected_profile_sha256=fingerprint,
        )
        if not authorization.approved or authorization.skipped:
            raise LiveDriverError(
                "automatic simple review did not authorize final reap"
            )
    return evidence


def _run_cross_runtime_composition(
    root: Path,
    *,
    commit_sha: str,
    fingerprint: str,
    owner_id: str,
    origin_surface: str,
    manager: RuntimeSessions,
    deadline: float,
    sleep: Callable[[float], None],
) -> list[dict[str, Any]]:
    """Compose the public dispatch, automatic review, and reap facades."""

    from harness.workflows.dispatch import (
        DispatchRequest,
        ReviewPolicy,
        start_dispatch,
    )
    from harness.workflows.reap import run_reap

    operation_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"llm-obsidian-live:{commit_sha}:cross-runtime-composition",
        )
    )
    scratch = _review_scratch(
        root, commit_sha, "cross-runtime-composition-dispatch"
    )
    prompt_path = scratch / ".task-prompt.md"
    ack_path = scratch / ".live-dispatch-ack.json"
    summary_path = scratch / ".task-summary.json"
    try:
        existing = manager.store.read(owner_id, operation_id)
    except Exception:
        existing = None
    if existing is None:
        ack_path.unlink(missing_ok=True)
        summary_path.unlink(missing_ok=True)
    _atomic_text(prompt_path, _dispatch_probe_prompt(operation_id))
    request = DispatchRequest(
        task_id=operation_id,
        owner_id=owner_id,
        plan_sha256=fingerprint,
        context_manifest=".task-prompt.md",
        route=_route(
            root,
            _PlannedOperation("dispatch", "codex", "composition"),
        ),
        review=ReviewPolicy(),
    )
    result = start_dispatch(
        request,
        manager,
        origin_surface=origin_surface,
        cwd=scratch,
    )
    opened = _result_record(result)
    if (
        opened.spec.kind != "dispatch"
        or opened.spec.operation_id != operation_id
        or opened.spec.owner_id != owner_id
    ):
        raise LiveDriverError("dispatch facade changed composition identity")
    _read_dispatch_ack(
        ack_path,
        expected=_dispatch_ack(operation_id),
        manager=manager,
        owner_id=owner_id,
        operation_id=operation_id,
        deadline=deadline,
        sleep=sleep,
    )
    review_evidence = _run_review_sessions(
        root,
        cell_id="cross-runtime-composition",
        commit_sha=commit_sha,
        fingerprint=fingerprint,
        owner_id=owner_id,
        origin_surface=origin_surface,
        manager=manager,
        deadline=deadline,
        sleep=sleep,
        shared_lane_id=opened.lane_id,
        dispatch_operation_id=operation_id,
    )
    summary = {
        "schema_version": 1,
        "type": "repo-touch",
        "title": "Bounded live acceptance composition",
        "session": None,
        "body": (
            "The isolated dispatch acknowledgement and automatic simple "
            "review completed for this exact release SHA."
        ),
    }
    reaped = run_reap(
        manager.store,
        owner_id=owner_id,
        operation_id=operation_id,
        summary=summary,
        finalize=lambda _record: {
            "schema_version": 1,
            "status": "complete",
        },
    )
    if (
        reaped.record.state != "finalizing"
        or reaped.record.effect_id != "reap-finalize"
        or reaped.record.effect_outcome.value != "succeeded"
    ):
        raise LiveDriverError("reap facade did not finalize exact dispatch")
    manager.request_exit(owner_id, operation_id)
    _await_cleanup(
        manager,
        owner_id=owner_id,
        operation_id=operation_id,
        deadline=deadline,
        sleep=sleep,
    )
    final = _result_record(manager.status(owner_id, operation_id))
    return [_operation_evidence(final), *review_evidence]
