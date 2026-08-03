"""Repo-owned in-process driver for the four provider-backed acceptance cells.

This compatibility façade preserves the original import surface while typed
evidence, runtime cleanup, review composition, and preflight live in focused
collaborators.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from harness.contracts import (
    AttentionReason,
    CallbackEnvelope,
    OperationRecord,
    OperationSpec,
    OwnedResources,
    RuntimeRoute,
)
from live_acceptance_contracts import (
    CELL_IDS,
    EVIDENCE_KEYS,
    IDENTIFIER,
    OPERATION_KEYS,
    PREFLIGHT_KEYS,
    PREFLIGHT_ROUTE_KEYS,
    RETRYABLE_CLEANUP_ATTENTION,
    SHA,
    SHA256,
    LiveDriverError,
    RuntimeSessions,
    _PlannedOperation,
    _identifier,
    _operations,
    _operations_for,
    _route,
    _stable_id,
    _timestamp,
    _validate_cell_shape,
    validate_cell_evidence,
    validate_preflight_evidence,
    validate_release_evidence,
)
from live_acceptance_preflight import preflight_release
from live_acceptance_review import (
    _axis_directory,
    _dispatch_ack,
    _dispatch_probe_prompt,
    _read_dispatch_ack,
    _review_probe_prompt,
    _review_scratch,
    _run_cross_runtime_composition,
    _run_review_sessions,
)
from live_acceptance_runtime import (
    _StartedOperations,
    _accepted_callback_matches,
    _atomic_text,
    _await_cleanup,
    _callback_from_value,
    _callback_template,
    _operation_evidence,
    _read_callback,
    _release_started,
    _render_prompt,
    _resources_released,
    _result_record,
)


def run_cell(
    root: Path,
    expected: dict[str, Any],
    *,
    timeout: int,
    session_manager: RuntimeSessions | None = None,
    origin_surface: str = "",
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run one fixed cell through the typed runtime-session start port."""
    root = root.expanduser().resolve()
    cell_id = expected.get("cell_id")
    commit_sha = expected.get("commit_sha")
    fingerprint = expected.get("dependency_fingerprint")
    required_trace = expected.get("required_trace")
    if (
        cell_id not in CELL_IDS
        or not isinstance(commit_sha, str)
        or not SHA.fullmatch(commit_sha)
        or not isinstance(fingerprint, str)
        or not SHA256.fullmatch(fingerprint)
        or not isinstance(required_trace, list)
        or type(timeout) is not int
        or timeout < 1
    ):
        raise LiveDriverError("live cell request is invalid")
    origin = origin_surface or str(os.environ.get("CMUX_SURFACE_ID") or "")
    if not re.fullmatch(
        r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}",
        origin,
    ):
        raise LiveDriverError("live cell requires the exact coordinator surface UUID")
    try:
        from harness.runtime_sessions import RuntimeSessionManager, RuntimeSessionRequest
    except ImportError as exc:
        raise LiveDriverError(f"runtime session start port is unavailable: {exc}") from exc
    if session_manager is None:
        try:
            manager: RuntimeSessions = RuntimeSessionManager.for_root(root)
        except (OSError, TypeError, ValueError) as exc:
            raise LiveDriverError(f"runtime session start port is unavailable: {exc}") from exc
    else:
        manager = session_manager

    # Own the release obligation: any abnormal exit between the first
    # start and the final cleanup would otherwise abandon a live owned
    # surface.  Release exactly what this run started, then re-raise the
    # original error unchanged so classification stays coordinator-owned.
    manager = _StartedOperations(manager)
    try:
        started_at = datetime.now(timezone.utc).isoformat()
        deadline = time.monotonic() + timeout
        owner_id = f"live-{commit_sha[:16]}"
        lane_ids: dict[str, str] = {}
        operations: list[dict[str, Any]] = []
        state_root = root / ".vault-meta/acceptance/live-runtime" / cell_id
        if cell_id == "cross-runtime-composition":
            operations.extend(
                _run_cross_runtime_composition(
                    root,
                    commit_sha=commit_sha,
                    fingerprint=fingerprint,
                    owner_id=owner_id,
                    origin_surface=origin,
                    manager=manager,
                    deadline=deadline,
                    sleep=sleep,
                )
            )
            planned_operations: tuple[_PlannedOperation, ...] = ()
        else:
            planned_operations = _operations_for(cell_id)
        for index, operation in enumerate(planned_operations, start=1):
            lane_id = lane_ids.setdefault(
                operation.lane_group,
                f"lane-{_stable_id(commit_sha, cell_id, operation.lane_group)}",
            )
            if operation.kind == "simple-review":
                operations.extend(
                    _run_review_sessions(
                        root,
                        cell_id=cell_id,
                        commit_sha=commit_sha,
                        fingerprint=fingerprint,
                        owner_id=owner_id,
                        origin_surface=origin,
                        manager=manager,
                        deadline=deadline,
                        sleep=sleep,
                        shared_lane_id=lane_id,
                    )
                )
                continue
            if operation.kind == "deep-review-anthropic-holistic":
                operations.extend(
                    _run_review_sessions(
                        root,
                        cell_id=cell_id,
                        commit_sha=commit_sha,
                        fingerprint=fingerprint,
                        owner_id=owner_id,
                        origin_surface=origin,
                        manager=manager,
                        deadline=deadline,
                        sleep=sleep,
                    )
                )
                continue
            if operation.kind == "deep-review-openai-holistic":
                continue
            operation_id = (
                f"live-{commit_sha[:12]}-{cell_id}-{index}"
            )
            run_id = f"run-{_stable_id(commit_sha, cell_id, operation.kind, str(index))}"
            route = _route(root, operation)
            relative_dir = (
                Path(".vault-meta/acceptance/live-runtime")
                / cell_id
                / operation_id
            )
            prompt_pointer = (relative_dir / "prompt.md").as_posix()
            continue_pointer = (relative_dir / "continue.md").as_posix()
            callback_pointer = (relative_dir / "callback.json").as_posix()
            identity = {
                "commit_sha": commit_sha,
                "cell_id": cell_id,
                "kind": operation.kind,
                "runtime": operation.runtime,
                "lane_id": lane_id,
                "run_id": run_id,
                "route_sha256": route.routing_sha256,
            }
            spec = OperationSpec(
                operation_id=operation_id,
                idempotency_key=hashlib.sha256(
                    json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                kind=operation.kind,
                owner_id=owner_id,
                route=route,
                context_manifest=prompt_pointer,
                verification_profile="live-acceptance",
            )
            envelope = _callback_template(operation_id, run_id, operation.kind)
            _atomic_text(
                root / prompt_pointer,
                _render_prompt(envelope, callback_pointer),
            )
            request = RuntimeSessionRequest(
                spec=spec,
                lane_id=lane_id,
                run_id=run_id,
                origin_surface=origin,
                cwd=root,
                prompt_pointer=prompt_pointer,
                callback_pointer=callback_pointer,
            )
            result = manager.start(request)
            opened = _result_record(result)
            if (
                opened.spec != spec
                or opened.lane_id != lane_id
                or opened.run_id != run_id
            ):
                raise LiveDriverError(f"{operation_id}: runtime start changed operation identity")
            if opened.state == "complete":
                if not _accepted_callback_matches(opened, envelope):
                    raise LiveDriverError(
                        f"{operation_id}: terminal callback mismatches the bounded live request"
                    )
                operations.append(_operation_evidence(opened))
                continue
            if opened.state in {"failed", "cancelled", "attention-required"}:
                raise LiveDriverError(
                    f"{operation_id}: runtime requires classification ({opened.state})"
                )
            if opened.accepted_callback_id:
                if not _accepted_callback_matches(opened, envelope):
                    raise LiveDriverError(
                        f"{operation_id}: stored callback mismatches the bounded live request"
                    )
                accepted = opened
            else:
                if opened.state in {"finalizing", "exiting"}:
                    raise LiveDriverError(
                        f"{operation_id}: exit began before the required callback"
                    )
                callback_path = root / str(
                    getattr(result, "callback_pointer", callback_pointer)
                    or callback_pointer
                )
                try:
                    callback_path.resolve().relative_to(state_root.resolve())
                except ValueError as exc:
                    raise LiveDriverError(
                        f"{operation_id}: callback pointer escaped owned runtime state"
                    ) from exc
                accepted = _read_callback(
                    callback_path,
                    manager,
                    owner_id=owner_id,
                    operation_id=operation_id,
                    expected=envelope,
                    deadline=deadline,
                    sleep=sleep,
                )
            if not _accepted_callback_matches(accepted, envelope):
                raise LiveDriverError(f"{operation_id}: provider returned an unexpected callback")
            if accepted.state not in {"finalizing", "exiting"}:
                if operation.continue_after_callback:
                    checkpoint = str(getattr(result, "checkpoint", "") or "")
                    if not checkpoint:
                        checkpoint = str(
                            getattr(
                                manager.status(owner_id, operation_id),
                                "checkpoint",
                                "",
                            )
                            or ""
                        )
                    if not checkpoint:
                        raise LiveDriverError(
                            f"{operation_id}: runtime did not capture a continuation checkpoint"
                        )
                    _atomic_text(
                        root / continue_pointer,
                        "Continue this exact session once, make no repository changes, "
                        "then wait for the coordinator to exit it.\n",
                    )
                    continued = _result_record(
                        manager.continue_session(
                            owner_id,
                            operation_id,
                            checkpoint,
                            continue_pointer,
                        )
                    )
                    if (
                        continued.spec.operation_id != operation_id
                        or continued.lane_id != lane_id
                        or continued.run_id != run_id
                    ):
                        raise LiveDriverError(
                            f"{operation_id}: continuation changed session identity"
                        )
            if accepted.state != "exiting":
                manager.request_exit(owner_id, operation_id)
            _await_cleanup(
                manager,
                owner_id=owner_id,
                operation_id=operation_id,
                deadline=deadline,
                sleep=sleep,
            )
            final = _result_record(manager.status(owner_id, operation_id))
            operations.append(_operation_evidence(final))
        finished_at = datetime.now(timezone.utc).isoformat()
        evidence = {
            "schema_version": 2,
            "cell_id": cell_id,
            "commit_sha": commit_sha,
            "dependency_fingerprint": fingerprint,
            "started_at": started_at,
            "finished_at": finished_at,
            "operations": operations,
            "trace": list(required_trace),
            "status": "passed",
        }
        return validate_cell_evidence(expected, evidence, commit_sha=commit_sha)
    except BaseException:
        _release_started(manager, sleep=sleep)
        raise
