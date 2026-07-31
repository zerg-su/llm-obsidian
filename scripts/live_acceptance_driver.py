"""Repo-owned in-process driver for the four provider-backed acceptance cells."""

from __future__ import annotations

import hashlib
import json
import os
import re
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
    RuntimeRoute,
)


CELL_IDS = (
    "claude-lifecycle",
    "codex-lifecycle",
    "cross-runtime-composition",
    "deep-review",
)
SHA = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
EVIDENCE_KEYS = {
    "schema_version",
    "cell_id",
    "commit_sha",
    "dependency_fingerprint",
    "started_at",
    "finished_at",
    "operations",
    "trace",
    "status",
}
OPERATION_KEYS = {
    "operation_id",
    "kind",
    "runtime",
    "lane_id",
    "run_id",
    "terminal_state",
    "effect_outcome",
    "callback_count",
    "owned_resources_remaining",
}
RETRYABLE_CLEANUP_ATTENTION = {
    AttentionReason.ATTENTION_REQUIRED,
    AttentionReason.CLEANUP_INCOMPLETE,
}
PREFLIGHT_KEYS = {
    "schema_version",
    "commit_sha",
    "origin_surface",
    "routes",
    "status",
}
PREFLIGHT_ROUTE_KEYS = {
    "runtime",
    "model",
    "effort",
    "profile",
    "capabilities",
}


class LiveDriverError(ValueError):
    """A live cell cannot start or its typed evidence is invalid."""


class RuntimeSessions(Protocol):
    """Narrow consumption seam implemented by ``RuntimeSessionManager``."""

    store: object

    def start(
        self,
        request: object,
        *,
        on_surface_opened: Callable[[object], None] | None = None,
    ) -> object: ...

    def accept_callback(self, envelope: CallbackEnvelope) -> object: ...

    def register_callback_target(
        self,
        owner_id: str,
        parent_operation_id: str,
        child_operation_id: str,
        child_run_id: str,
        callback_pointer: str,
    ) -> object: ...

    def continue_session(
        self,
        owner_id: str,
        operation_id: str,
        checkpoint: str,
        prompt_pointer: str,
    ) -> object: ...

    def request_exit(self, owner_id: str, operation_id: str) -> object: ...

    def cleanup(self, owner_id: str, operation_id: str) -> object: ...

    def status(self, owner_id: str, operation_id: str) -> object: ...


@dataclass(frozen=True)
class _PlannedOperation:
    kind: str
    runtime: str
    lane_group: str
    continue_after_callback: bool = False


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise LiveDriverError(f"{label} must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveDriverError(f"{label} must be a timezone-aware timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LiveDriverError(f"{label} must be a timezone-aware timestamp")
    return parsed


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise LiveDriverError(f"{label} must be a bounded identifier")
    return value


def _operations(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise LiveDriverError("operations must be a non-empty list")
    result: list[dict[str, Any]] = []
    for index, operation in enumerate(value):
        if not isinstance(operation, dict) or set(operation) != OPERATION_KEYS:
            raise LiveDriverError(f"operations[{index}] has an invalid typed shape")
        for field in ("operation_id", "kind", "lane_id", "run_id"):
            _identifier(operation[field], f"operations[{index}].{field}")
        if operation["runtime"] not in {"claude", "codex"}:
            raise LiveDriverError(f"operations[{index}].runtime is invalid")
        if operation["terminal_state"] != "complete":
            raise LiveDriverError(f"operations[{index}] is not terminal complete")
        if operation["effect_outcome"] != "succeeded":
            raise LiveDriverError(f"operations[{index}] has an unresolved effect")
        if type(operation["callback_count"]) is not int or operation["callback_count"] != 1:
            raise LiveDriverError(f"operations[{index}] callback count is not exact")
        if (
            type(operation["owned_resources_remaining"]) is not int
            or operation["owned_resources_remaining"] != 0
        ):
            raise LiveDriverError(f"operations[{index}] retains owned resources")
        result.append(operation)
    if len({row["operation_id"] for row in result}) != len(result):
        raise LiveDriverError("operation identities must be unique")
    return result


def _validate_cell_shape(cell_id: str, operations: list[dict[str, Any]]) -> None:
    kinds = [str(row["kind"]) for row in operations]
    runtimes = [str(row["runtime"]) for row in operations]
    lanes = [str(row["lane_id"]) for row in operations]
    runs = [str(row["run_id"]) for row in operations]
    if cell_id == "claude-lifecycle":
        valid = len(operations) == 1 and kinds == ["runtime-lifecycle"] and runtimes == ["claude"]
    elif cell_id == "codex-lifecycle":
        valid = len(operations) == 1 and kinds == ["runtime-lifecycle"] and runtimes == ["codex"]
    elif cell_id == "cross-runtime-composition":
        valid = (
            kinds == ["dispatch", "simple-review-holistic"]
            and runtimes == ["codex", "claude"]
            and len(set(lanes)) == 1
            and len(set(runs)) == 2
        )
    else:
        valid = (
            kinds == ["deep-review-spec", "deep-review-correctness"]
            and runtimes == ["claude", "codex"]
            and len(set(lanes)) == 2
            and len(set(runs)) == 2
        )
    if not valid:
        raise LiveDriverError(f"{cell_id}: operations do not satisfy the live cell contract")


def _operations_for(cell_id: str) -> tuple[_PlannedOperation, ...]:
    if cell_id == "claude-lifecycle":
        return (_PlannedOperation("runtime-lifecycle", "claude", "lifecycle", True),)
    if cell_id == "codex-lifecycle":
        return (_PlannedOperation("runtime-lifecycle", "codex", "lifecycle", True),)
    if cell_id == "cross-runtime-composition":
        return (
            _PlannedOperation("dispatch", "codex", "composition"),
            _PlannedOperation("simple-review", "claude", "composition"),
            _PlannedOperation("reap", "codex", "composition"),
        )
    if cell_id == "deep-review":
        return (
            _PlannedOperation("deep-review-spec", "claude", "spec"),
            _PlannedOperation(
                "deep-review-correctness", "codex", "correctness"
            ),
        )
    raise LiveDriverError("unknown live acceptance cell")


def _stable_id(*parts: str, length: int = 24) -> str:
    canonical = "\0".join(parts).encode()
    return hashlib.sha256(canonical).hexdigest()[:length]


def _route(root: Path, operation: _PlannedOperation) -> RuntimeRoute:
    try:
        from model_routing import load_tracked_config

        config = load_tracked_config(root)
        if operation.kind == "simple-review":
            value = config.reviewer_default(operation.runtime, "simple")
        elif operation.kind.startswith("deep-review-"):
            value = config.reviewer_default(operation.runtime, "deep")
        else:
            value = config.runtime_default(operation.runtime)
    except (OSError, ValueError) as exc:
        raise LiveDriverError(f"cannot resolve tracked live route: {exc}") from exc
    profile = (
        "reviewer-callback"
        if "review" in operation.kind
        else "executor"
    )
    return RuntimeRoute(
        operation.runtime,
        value["model"],
        value["effort"],
        profile,
        config.fingerprint,
    )


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _callback_template(
    operation_id: str,
    run_id: str,
    kind: str,
) -> CallbackEnvelope:
    if kind == "runtime-lifecycle":
        callback_kind = "review"
        payload: dict[str, Any] = {"verdict": "changes-requested"}
    elif "review" in kind:
        callback_kind = "review"
        payload = {"verdict": "approve"}
    else:
        callback_kind = "result"
        payload = {"status": "complete"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return CallbackEnvelope(
        callback_id=f"live-{_stable_id(operation_id, run_id, callback_kind)}",
        operation_id=operation_id,
        run_id=run_id,
        kind=callback_kind,
        payload=payload,
        payload_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _render_prompt(envelope: CallbackEnvelope, callback_pointer: str) -> str:
    callback = {
        "schema_version": envelope.schema_version,
        "callback_id": envelope.callback_id,
        "operation_id": envelope.operation_id,
        "run_id": envelope.run_id,
        "kind": envelope.kind,
        "payload": dict(envelope.payload),
        "payload_sha256": envelope.payload_sha256,
    }
    return (
        "This is a bounded LLM Obsidian live-acceptance probe. "
        "Do not edit tracked repository files and do not start another model. "
        "Confirm that this interactive session is usable, then atomically write "
        "the exact JSON object below to the registered callback pointer and wait "
        "for the coordinator's next instruction.\n\n"
        f"Callback pointer: {callback_pointer}\n"
        f"Callback JSON: {json.dumps(callback, ensure_ascii=False, sort_keys=True)}\n"
    )


def _callback_from_value(value: object) -> CallbackEnvelope:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "callback_id",
        "operation_id",
        "run_id",
        "kind",
        "payload",
        "payload_sha256",
    }:
        raise LiveDriverError("provider callback has an invalid typed shape")
    try:
        return CallbackEnvelope(
            callback_id=value["callback_id"],
            operation_id=value["operation_id"],
            run_id=value["run_id"],
            kind=value["kind"],
            payload=value["payload"],
            payload_sha256=value["payload_sha256"],
            schema_version=value["schema_version"],
        )
    except (TypeError, ValueError) as exc:
        raise LiveDriverError(f"provider callback is invalid: {exc}") from exc


def _result_record(value: object) -> OperationRecord:
    record = getattr(value, "record", value)
    if not isinstance(record, OperationRecord):
        raise LiveDriverError("runtime session port returned no typed operation record")
    return record


def _read_callback(
    path: Path,
    manager: RuntimeSessions,
    *,
    owner_id: str,
    operation_id: str,
    expected: CallbackEnvelope,
    deadline: float,
    sleep: Callable[[float], None],
) -> OperationRecord:
    while True:
        if path.is_file():
            try:
                if path.stat().st_size > CallbackEnvelope.MAX_PAYLOAD_BYTES:
                    raise LiveDriverError("provider callback exceeds size cap")
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise LiveDriverError(f"cannot read provider callback: {exc}") from exc
            envelope = _callback_from_value(value)
            if envelope != expected:
                raise LiveDriverError("provider callback mismatches the bounded live request")
            if (
                envelope.operation_id
                != _result_record(manager.status(owner_id, operation_id)).spec.operation_id
            ):
                raise LiveDriverError("provider callback operation identity mismatches")
            manager.accept_callback(envelope)
            return _result_record(manager.status(owner_id, operation_id))
        status = _result_record(manager.status(owner_id, operation_id))
        if status.state in {"complete", "failed", "cancelled", "attention-required"}:
            raise LiveDriverError(
                f"{operation_id}: runtime stopped before its typed callback ({status.state})"
            )
        if time.monotonic() >= deadline:
            raise LiveDriverError(f"{operation_id}: typed callback timed out")
        sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def _operation_evidence(
    record: OperationRecord,
    *,
    callback_count: int | None = None,
) -> dict[str, Any]:
    resources = record.resources
    remaining = sum(
        bool(value)
        for value in (
            resources.surface_id,
            resources.process_group,
            resources.supervisor_pid,
        )
    )
    return {
        "operation_id": record.spec.operation_id,
        "kind": record.spec.kind,
        "runtime": record.spec.route.runtime,
        "lane_id": record.lane_id,
        "run_id": record.run_id,
        "terminal_state": record.state,
        "effect_outcome": record.effect_outcome.value,
        "callback_count": (
            int(bool(record.accepted_callback_id))
            if callback_count is None
            else callback_count
        ),
        "owned_resources_remaining": remaining,
    }


def _accepted_callback_matches(
    record: OperationRecord,
    expected: CallbackEnvelope,
) -> bool:
    return (
        record.accepted_callback_id == expected.callback_id
        and record.accepted_callback_kind == expected.kind
        and record.accepted_callback_sha256 == expected.payload_sha256
    )


def _await_cleanup(
    manager: RuntimeSessions,
    *,
    owner_id: str,
    operation_id: str,
    deadline: float,
    sleep: Callable[[float], None],
) -> OperationRecord:
    ambiguous_retries = 0
    while True:
        current = _result_record(manager.status(owner_id, operation_id))
        if current.state == "attention-required":
            if current.attention_reason not in RETRYABLE_CLEANUP_ATTENTION:
                raise LiveDriverError(
                    f"{operation_id}: cleanup stopped in attention-required"
                )
            if ambiguous_retries >= 2:
                raise LiveDriverError(
                    f"{operation_id}: exit ownership remained ambiguous"
                )
            ambiguous_retries += 1
            exit_result = manager.request_exit(owner_id, operation_id)
            current = _result_record(exit_result)
            if current.state == "attention-required":
                if (
                    ambiguous_retries >= 2
                    or time.monotonic() >= deadline
                ):
                    raise LiveDriverError(
                        f"{operation_id}: exit ownership remained ambiguous"
                    )
                sleep(min(0.25, max(0.0, deadline - time.monotonic())))
                continue
        if current.state in {"complete", "failed", "cancelled"}:
            return current
        if current.state != "exiting":
            raise LiveDriverError(
                f"{operation_id}: cleanup requires an exiting operation"
            )
        result = manager.cleanup(owner_id, operation_id)
        record = _result_record(result)
        if record.state == "complete":
            return record
        if record.state == "attention-required":
            if record.attention_reason not in RETRYABLE_CLEANUP_ATTENTION:
                raise LiveDriverError(
                    f"{operation_id}: cleanup stopped in attention-required"
                )
            if ambiguous_retries >= 2 or time.monotonic() >= deadline:
                raise LiveDriverError(
                    f"{operation_id}: exit ownership remained ambiguous"
                )
            sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            continue
        if record.state in {"failed", "cancelled"}:
            raise LiveDriverError(
                f"{operation_id}: cleanup stopped in {record.state}"
            )
        if getattr(result, "action", "") not in {
            "wait-for-exit",
            "wait-for-ownership",
            "wait-for-supervisor",
            "exit-requested",
        }:
            raise LiveDriverError(
                f"{operation_id}: cleanup did not prove provider exit"
            )
        if time.monotonic() >= deadline:
            raise LiveDriverError(f"{operation_id}: provider exit timed out")
        sleep(min(0.25, max(0.0, deadline - time.monotonic())))


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


def validate_cell_evidence(
    expected: dict[str, Any],
    evidence: object,
    *,
    commit_sha: str,
) -> dict[str, Any]:
    """Validate one content-free schema-v2 live result against its clean commit."""
    if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_KEYS:
        raise LiveDriverError("cell evidence has an invalid typed shape")
    cell_id = evidence.get("cell_id")
    if cell_id not in CELL_IDS or cell_id != expected.get("cell_id"):
        raise LiveDriverError("cell evidence identity mismatches the release contract")
    if not SHA.fullmatch(commit_sha) or evidence.get("commit_sha") != commit_sha:
        raise LiveDriverError(f"{cell_id}: evidence is not bound to the exact commit")
    fingerprint = expected.get("dependency_fingerprint")
    if not isinstance(fingerprint, str) or not SHA256.fullmatch(fingerprint):
        raise LiveDriverError(f"{cell_id}: release dependency fingerprint is invalid")
    if evidence.get("dependency_fingerprint") != fingerprint:
        raise LiveDriverError(f"{cell_id}: dependency fingerprint changed")
    if evidence.get("schema_version") != 2 or evidence.get("status") != "passed":
        raise LiveDriverError(f"{cell_id}: live status is not a schema-v2 pass")
    started = _timestamp(evidence.get("started_at"), "started_at")
    finished = _timestamp(evidence.get("finished_at"), "finished_at")
    if finished < started:
        raise LiveDriverError(f"{cell_id}: live timestamps are reversed")
    required_trace = expected.get("required_trace")
    trace = evidence.get("trace")
    if (
        not isinstance(required_trace, list)
        or not all(isinstance(item, str) for item in required_trace)
        or trace != required_trace
    ):
        raise LiveDriverError(f"{cell_id}: required lifecycle trace is incomplete")
    operations = _operations(evidence.get("operations"))
    _validate_cell_shape(str(cell_id), operations)
    return evidence


def validate_release_evidence(
    release: dict[str, Any],
    report: object,
) -> dict[str, Any]:
    """Validate the complete four-cell report and global operation identity."""
    if (
        not isinstance(report, dict)
        or set(report)
        != {
            "schema_version",
            "commit_sha",
            "preflight",
            "cells",
            "failures",
        }
        or report.get("schema_version") != 3
        or report.get("commit_sha") != release.get("commit_sha")
    ):
        raise LiveDriverError("live report has an invalid schema-v3 commit binding")
    validate_preflight_evidence(
        report.get("preflight"),
        commit_sha=str(release.get("commit_sha") or ""),
    )
    if report.get("failures") != []:
        raise LiveDriverError("complete live report retains failed cells")
    rows = report.get("cells")
    if not isinstance(rows, list) or len(rows) != len(CELL_IDS):
        raise LiveDriverError("live report must contain exactly four cells")
    expected_rows = release.get("cells")
    if not isinstance(expected_rows, list):
        raise LiveDriverError("release contract cells are invalid")
    expected = {
        row.get("cell_id"): row for row in expected_rows if isinstance(row, dict)
    }
    if set(expected) != set(CELL_IDS):
        raise LiveDriverError("release contract must contain exactly four cells")
    validated: list[dict[str, Any]] = []
    operation_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("cell_id") not in expected:
            raise LiveDriverError("live report contains an unknown cell")
        value = validate_cell_evidence(
            expected[row["cell_id"]],
            row,
            commit_sha=release["commit_sha"],
        )
        for operation in value["operations"]:
            operation_id = operation["operation_id"]
            if operation_id in operation_ids:
                raise LiveDriverError("operation identity is reused across live cells")
            operation_ids.add(operation_id)
        validated.append(value)
    if {row["cell_id"] for row in validated} != set(CELL_IDS):
        raise LiveDriverError("live report contains duplicate or missing cells")
    return report


def validate_preflight_evidence(
    evidence: object,
    *,
    commit_sha: str,
) -> dict[str, Any]:
    """Validate the content-free global host proof bound to one release SHA."""

    if (
        not isinstance(evidence, dict)
        or set(evidence) != PREFLIGHT_KEYS
        or evidence.get("schema_version") != 1
        or evidence.get("commit_sha") != commit_sha
        or not SHA.fullmatch(commit_sha)
        or evidence.get("status") != "compatible"
        or not re.fullmatch(
            r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}"
            r"[0-9A-Fa-f]{12}",
            str(evidence.get("origin_surface") or ""),
        )
    ):
        raise LiveDriverError("global preflight evidence is invalid")
    routes = evidence.get("routes")
    if not isinstance(routes, list) or not routes:
        raise LiveDriverError("global preflight routes are missing")
    identities: set[tuple[str, str, str, str]] = set()
    for index, route in enumerate(routes):
        if (
            not isinstance(route, dict)
            or set(route) != PREFLIGHT_ROUTE_KEYS
            or route.get("runtime") not in {"claude", "codex"}
        ):
            raise LiveDriverError(
                f"global preflight route {index} has an invalid shape"
            )
        identity = tuple(
            _identifier(route.get(field), f"preflight.routes[{index}].{field}")
            for field in ("runtime", "model", "effort", "profile")
        )
        capabilities = route.get("capabilities")
        if (
            not isinstance(capabilities, list)
            or not capabilities
            or any(
                not isinstance(item, str)
                or not IDENTIFIER.fullmatch(item)
                for item in capabilities
            )
            or len(capabilities) != len(set(capabilities))
        ):
            raise LiveDriverError(
                f"global preflight route {index} capabilities are invalid"
            )
        if identity in identities:
            raise LiveDriverError("global preflight route is duplicated")
        identities.add(identity)
    return evidence


def preflight_release(
    root: Path,
    release: dict[str, Any],
    *,
    timeout: int,
    origin_surface: str = "",
    route_preflight: Callable[
        [tuple[tuple[RuntimeRoute, Path, str], ...]], object
    ]
    | None = None,
) -> dict[str, Any]:
    """Check every provider/profile and the exact origin before any model starts."""
    root = root.expanduser().resolve()
    commit_sha = release.get("commit_sha")
    rows = release.get("cells")
    if (
        not SHA.fullmatch(str(commit_sha or ""))
        or not isinstance(rows, list)
        or len(rows) != len(CELL_IDS)
        or {row.get("cell_id") for row in rows if isinstance(row, dict)}
        != set(CELL_IDS)
        or type(timeout) is not int
        or timeout < 1
    ):
        raise LiveDriverError("release preflight request is invalid")
    origin = origin_surface or str(os.environ.get("CMUX_SURFACE_ID") or "")
    if not re.fullmatch(
        r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}",
        origin,
    ):
        raise LiveDriverError("release preflight requires the exact origin surface")

    checked: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    requests: list[tuple[RuntimeRoute, Path, str]] = []
    for cell_id in CELL_IDS:
        for operation in _operations_for(cell_id):
            route = _route(root, operation)
            key = (
                route.runtime,
                route.model,
                route.effort,
                route.profile,
                route.routing_sha256,
            )
            if key in seen:
                continue
            seen.add(key)
            if route.profile == "reviewer-callback":
                callback_dir = (
                    _review_scratch(root, str(commit_sha), cell_id)
                    / "preflight"
                    / operation.kind
                )
            else:
                callback_dir = (
                    root
                    / ".vault-meta/acceptance/live-runtime/preflight"
                    / operation.kind
                )
            callback_dir.mkdir(parents=True, exist_ok=True)
            callback_dir.chmod(0o700)
            requests.append((route, callback_dir, origin))
    if route_preflight is None:
        from harness.runtime_sessions import RuntimeSessionManager

        route_preflight = RuntimeSessionManager.for_root(
            root,
            start_timeout_seconds=float(timeout),
        ).preflight_routes
    reports = tuple(route_preflight(tuple(requests)))
    if len(reports) != len(requests):
        raise LiveDriverError("release route preflight returned an incomplete report")
    for (route, _callback_dir, _origin), report in zip(requests, reports):
        if getattr(report, "route", None) != route:
            raise LiveDriverError("release route preflight changed the requested route")
        if getattr(report, "compatible", False) is not True:
            reason = getattr(getattr(report, "reason", None), "value", "")
            raise LiveDriverError(
                "release route preflight failed"
                + (f": {reason}" if reason else "")
            )
        checked.append(
            {
                "runtime": route.runtime,
                "model": route.model,
                "effort": route.effort,
                "profile": route.profile,
                "capabilities": list(
                    getattr(report, "capabilities", ())
                ),
            }
        )
    artifact = {
        "schema_version": 1,
        "commit_sha": commit_sha,
        "origin_surface": origin,
        "routes": checked,
        "status": "compatible",
    }
    return validate_preflight_evidence(
        artifact, commit_sha=str(commit_sha)
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
        if operation.kind == "deep-review-spec":
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
        if operation.kind == "deep-review-correctness":
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
