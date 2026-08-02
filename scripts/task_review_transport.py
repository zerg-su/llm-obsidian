"""Review callback transport, typed receipts, and bounded telemetry."""

from __future__ import annotations

import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from harness.store import OperationStore
from harness.workflows.review import ReviewContext, ReviewResult, ReviewRound
from harness.workflows.review_gate import ReviewGateController, ReviewGateRun
from review_telemetry import emit_review_event
from task_review_context import _callback_path, _envelope
from task_review_shared import (
    ActiveReviewRound,
    StaleRoundCallbackError,
    TaskReviewError,
    _atomic_json,
)


def _write_round_meta(
    *,
    runtime_root: Path,
    vault: Path,
    worktree: Path,
    task_id: str,
    depth: str,
    context: ReviewContext,
    lane_operation_id: str,
    round_: ReviewRound,
) -> None:
    directory = _callback_path(runtime_root, round_.axis).parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    started_at = _round_telemetry_state(runtime_root, round_)["started_at"]
    _atomic_json(
        directory / ".review-meta.json",
        {
            "schema_version": 1,
            "transport": "review-round",
            "operation_id": round_.operation_id,
            "run_id": round_.run_id,
            "review_id": task_id,
            "parent_session_operation_id": lane_operation_id,
            "review_mode": depth,
            "axis": round_.axis,
            "verification_iteration": round_.verification_iteration,
            "started_at": started_at,
            "worktree": str(worktree),
            "task_name": task_id,
            "head_sha": context.head_sha,
            "review_purpose": context.purpose,
            "review_boundary_input_sha256": (
                context.boundary_input_sha256
            ),
            "verification_profile": {
                "name": context.verification_profile,
                "sha256": context.verification_profile_sha256,
            },
        },
    )
    _emit_round_telemetry(
        worktree,
        vault,
        runtime_root,
        round_,
        event="review-round-start",
        terminal_status="started",
    )
def _telemetry_marker(runtime_root: Path, axis: str) -> Path:
    return _callback_path(runtime_root, axis).parent / ".review-telemetry.json"


def _round_telemetry_state(
    runtime_root: Path, round_: ReviewRound
) -> dict[str, Any]:
    identity = {
        "schema_version": 1,
        "operation_id": round_.operation_id,
        "verification_iteration": round_.verification_iteration,
    }
    try:
        prior = json.loads(
            _telemetry_marker(runtime_root, round_.axis).read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        prior = {}
    if (
        isinstance(prior, dict)
        and all(prior.get(key) == value for key, value in identity.items())
        and isinstance(prior.get("started_at"), str)
        and isinstance(prior.get("emitted"), list)
    ):
        return prior
    return {
        **identity,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "emitted": [],
    }


def _emit_round_telemetry(
    worktree: Path,
    vault: Path,
    runtime_root: Path,
    round_: ReviewRound,
    *,
    event: str,
    terminal_status: str,
    severities: Sequence[str] = (),
) -> None:
    """Emit one replay-bounded event; every failure remains non-fatal."""

    try:
        state = _round_telemetry_state(runtime_root, round_)
        event_key = f"{event}:{terminal_status}"
        emitted = set(str(item) for item in state["emitted"])
        if event_key in emitted:
            return
        if not emit_review_event(
            worktree,
            vault,
            event=event,
            axis=round_.axis,
            reviewer_runtime=round_.spec.route.runtime,
            iteration=round_.verification_iteration,
            terminal_status=terminal_status,
            started_at=str(state["started_at"]),
            severities=severities,
        ):
            return
        _atomic_json(
            _telemetry_marker(runtime_root, round_.axis),
            {**state, "emitted": sorted(emitted | {event_key})},
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return


def load_active_round(
    gate_root: Path,
    store: OperationStore,
    runtime_manager: object,
    *,
    axis: str,
) -> ActiveReviewRound:
    run = ReviewGateController(
        gate_root, runtime_manager, store
    ).rehydrate()
    for lane in run.execution.lanes:
        if lane.axis == axis:
            return ActiveReviewRound(run, lane, run.rounds[axis])
    raise TaskReviewError("review axis is not active")


def _receipt(
    *,
    status: str,
    meta: Mapping[str, Any],
    vault: Path,
    worktree: Path,
    runtime_root: Path,
    context_manifest: Path,
    run: ReviewGateRun | None = None,
) -> dict[str, Any]:
    lanes = []
    if run is not None:
        lanes = [
            {
                "axis": lane.axis,
                "operation_id": lane.operation_id,
                "run_id": lane.run_id,
                "surface_id": lane.surface_id,
                "verification_iteration": lane.verification_iteration,
                "callback_path": str(
                    _callback_path(
                        runtime_root,
                        lane.axis,
                    )
                ),
            }
            for lane in run.execution.lanes
        ]
    return {
        "schema_version": 1,
        "status": status,
        "review_purpose": str(
            meta.get("review_policy", {}).get("purpose")
            if isinstance(meta.get("review_policy"), Mapping)
            else "implementation"
        )
        or "implementation",
        "review_boundary_input_sha256": str(
            meta.get("review_policy", {}).get("boundary_input_sha256")
            if isinstance(meta.get("review_policy"), Mapping)
            else ""
        ),
        "task_id": meta["task_id"],
        "worktree": str(worktree),
        "vault_root": str(vault),
        "context_manifest": str(context_manifest),
        "lanes": lanes,
    }


def _callback_wake(
    meta: Mapping[str, Any], vault: Path, worktree: Path
) -> str:
    if meta.get("lifecycle") != "current-checkout":
        return ""
    raw_policy = meta["review_policy"]
    wake_argv = [
        str(Path(sys.executable).resolve()),
        str(vault / "scripts" / "task-review-runner.py"),
        "current",
        "--worktree",
        str(worktree),
    ]
    if raw_policy["mode"] == "deep":
        wake_argv.append("--deep")
    if raw_policy["cross_model"]:
        wake_argv.append("--cross-model")
    for option in ("runtime", "model", "effort"):
        value = str(raw_policy.get(option) or "")
        if value:
            wake_argv.extend((f"--{option}", value))
    purpose = str(raw_policy.get("purpose") or "implementation")
    boundary_file = str(meta.get("review_boundary_input_file") or "")
    if purpose != "implementation" or boundary_file:
        wake_argv.extend(("--purpose", purpose))
    if boundary_file:
        wake_argv.extend(("--boundary-input", boundary_file))
    wake_argv.extend(("--plan", str(meta["plan_file"])))
    return (
        "Typed current-review callback is ready. Run this exact command: "
        + shlex.join(wake_argv)
    )
def _collect_ready_results(
    run: ReviewGateRun,
    runtime_root: Path,
    worktree: Path,
    vault: Path,
) -> list[tuple[object, ReviewRound, ReviewResult]]:
    ready: list[tuple[object, ReviewRound, ReviewResult]] = []
    for lane in run.execution.lanes:
        round_ = run.rounds[lane.axis]
        callback = _callback_path(runtime_root, lane.axis)
        if not callback.is_file() or callback.is_symlink():
            continue
        try:
            _unused, result = _envelope(callback, round_)
        except (StaleRoundCallbackError, TaskReviewError, OSError, ValueError):
            _emit_round_telemetry(
                worktree,
                vault,
                runtime_root,
                round_,
                event="review-callback",
                terminal_status="rejected",
            )
            raise
        ready.append((lane, round_, result))
    return ready


def _record_accepted_result(
    worktree: Path,
    vault: Path,
    runtime_root: Path,
    round_: ReviewRound,
    result: ReviewResult,
) -> None:
    _emit_round_telemetry(
        worktree,
        vault,
        runtime_root,
        round_,
        event="review-callback",
        terminal_status="accepted",
    )
    _emit_round_telemetry(
        worktree,
        vault,
        runtime_root,
        round_,
        event="review-round-complete",
        terminal_status=result.verdict,
        severities=tuple(finding.severity for finding in result.findings),
    )
