"""Public harness CLI; diagnostics work without a model session."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Sequence

from review_zero_effect import (
    EXACT_HEAD_ATTEMPT_PROTOCOL,
    zero_effect_gate_shape,
)

from .adapters.cmux import CmuxAdapter
from .adapters.process import ProcessAdapter
from .contracts import (
    AttentionReason,
    ContractError,
    OperationRecord,
    OwnedResources,
    TransitionResult,
    to_dict,
)
from .cli_io import emit as _emit, parser
from .cli_readonly import COMMANDS as READ_ONLY_COMMANDS
from .cli_readonly import execute as execute_read_only
from .reconciliation import (
    ReconcileDecision,
    prove_accepted_callback_ownership,
    reconcile,
)
from .state_machine import TERMINAL
from .status_segment import publish as publish_status
from .store import OperationStore, StoreError
from .supervisor import OperationSupervisor
from .pre_model_reviewer_retirement import retire_failed_reviewer_start
from .runtime_sessions import RuntimeSessionError, RuntimeSessionManager
from .review_finalization import _head as _review_worktree_head
from .runtime_worker import _review_resolution_handoff_ready
from .runtime_worker_review_bridge import publish_review_resolution_transport


def _has_owned_resources(record: OperationRecord) -> bool:
    resources = record.resources
    return bool(
        resources.surface_id
        or resources.process_group
        or resources.supervisor_pid
    )


def _attention(
    store: OperationStore,
    owner: str,
    operation_id: str,
    *,
    reason: AttentionReason,
) -> TransitionResult:
    record = store.read(owner, operation_id)
    if record.state in TERMINAL:
        return store.transition(owner, operation_id, record.state)
    return store.transition(
        owner,
        operation_id,
        "attention-required",
        reason=reason,
    )


def _continuation_time_budget(
    store: OperationStore,
    record: OperationRecord,
) -> float | None:
    path = (
        store.root
        / "owners"
        / record.spec.owner_id
        / "runtime"
        / record.spec.operation_id
        / "session.json"
    )
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    budget = value.get("time_budget_seconds") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("operation_id") != record.spec.operation_id
        or value.get("run_id") != record.run_id
        or not isinstance(budget, (int, float))
        or isinstance(budget, bool)
        or budget <= 0
    ):
        return None
    return float(budget)


def _reconcile_owned_resources(
    store: OperationStore,
    record: OperationRecord,
    process_adapter: object,
    cmux_adapter: object,
) -> ReconcileDecision:
    """Probe exact ownership and confirm any cleanup with a fresh liveness read."""

    try:
        resources = record.resources
        has_process_group = resources.process_group > 1
        has_surface = bool(resources.surface_id)
        accepted = prove_accepted_callback_ownership(
            record,
            process_adapter,
        )
        if accepted.applicable:
            process_status = accepted.process_status
            supervisor_status = accepted.supervisor_status
            if "unknown" in {process_status, supervisor_status} or (
                process_status == "alive" and supervisor_status == "dead"
            ):
                return ReconcileDecision(
                    "none", AttentionReason.ATTENTION_REQUIRED
                )
            if "alive" in {process_status, supervisor_status}:
                return ReconcileDecision("continue")
        if resources.supervisor_pid and not has_process_group:
            return ReconcileDecision(
                "none", AttentionReason.ATTENTION_REQUIRED
            )
        if has_surface and not has_process_group:
            surface = cmux_adapter.status(resources.surface_id)
            if surface == "alive":
                return ReconcileDecision("continue")
            if surface == "missing":
                return ReconcileDecision("complete")
            return ReconcileDecision(
                "none", AttentionReason.ATTENTION_REQUIRED
            )
        if has_process_group and not has_surface:
            process = process_adapter.process_status(
                resources.process_group,
                resources.process_identity,
            )
            if process == "alive":
                return ReconcileDecision("continue")
            if process == "dead":
                return ReconcileDecision("complete")
            return ReconcileDecision(
                "none", AttentionReason.ATTENTION_REQUIRED
            )
        decision = reconcile(
            record,
            process_adapter,
            cmux_adapter,
        )
        if decision.action in {"close-exact", "terminate-exact"}:
            decision = reconcile(
                record,
                process_adapter,
                cmux_adapter,
            )
        return decision
    except Exception:
        return ReconcileDecision("none", AttentionReason.ATTENTION_REQUIRED)


def _clear_owned_resources(
    store: OperationStore,
    owner: str,
    operation_id: str,
) -> None:
    OperationSupervisor(store, owner, operation_id).bind_resources(OwnedResources())


def _cancel_or_close(
    store: OperationStore,
    owner: str,
    operation_id: str,
    *,
    process_adapter: object,
    cmux_adapter: object,
    bounded_cancel: bool = False,
) -> TransitionResult:
    record = store.read(owner, operation_id)
    if record.state in TERMINAL:
        return store.transition(owner, operation_id, record.state)
    if record.pending_effect:
        return _attention(
            store,
            owner,
            operation_id,
            reason=AttentionReason.ATTENTION_REQUIRED,
        )
    retired = retire_failed_reviewer_start(
        store,
        owner,
        operation_id,
        cmux_adapter=cmux_adapter,
        process_adapter=process_adapter,
    )
    if retired is not None:
        return retired
    if (
        record.spec.route.profile == "reviewer-callback"
        and _has_owned_resources(record)
    ):
        runtime = RuntimeSessionManager(
            store,
            cmux_adapter,
            process_adapter,
        )
        try:
            runtime.prove_durable_cleanup_ownership(owner, operation_id)
        except RuntimeSessionError:
            return _attention(
                store,
                owner,
                operation_id,
                reason=AttentionReason.CLEANUP_INCOMPLETE,
            )
        initial = record
        if bounded_cancel:
            result = runtime.cancel(owner, operation_id)
        elif record.state == "exiting":
            result = runtime.cleanup(owner, operation_id)
        else:
            result = runtime.request_exit(owner, operation_id)
        current = result.record
        return TransitionResult(
            operation_id,
            initial.state,
            current.state,
            current.revision,
            current.revision != initial.revision,
            current.attention_reason,
        )
    if _has_owned_resources(record):
        decision = _reconcile_owned_resources(
            store, record, process_adapter, cmux_adapter
        )
        if decision.action == "continue":
            runtime = RuntimeSessionManager(
                store,
                cmux_adapter,
                process_adapter,
            )
            requested = (
                runtime.cancel(owner, operation_id)
                if bounded_cancel
                else runtime.request_exit(owner, operation_id)
            )
            current = requested.record
            return TransitionResult(
                operation_id,
                record.state,
                current.state,
                current.revision,
                current.revision != record.revision,
                current.attention_reason,
            )
        if decision.action != "complete":
            return _attention(
                store,
                owner,
                operation_id,
                reason=AttentionReason.CLEANUP_INCOMPLETE,
            )
        _clear_owned_resources(store, owner, operation_id)
        record = store.read(owner, operation_id)
    if record.state not in {"cancelling", "exiting"}:
        store.transition(owner, operation_id, "cancelling")
        record = store.read(owner, operation_id)
    if record.state == "cancelling":
        store.transition(owner, operation_id, "exiting")
        record = store.read(owner, operation_id)
    terminal_state = (
        "complete"
        if record.state == "exiting"
        and record.accepted_callback_id
        and record.accepted_callback_kind
        and record.accepted_callback_sha256
        else "cancelled"
    )
    return store.transition(owner, operation_id, terminal_state)


def _exact_cancel_subtree(
    store: OperationStore,
    owner: str,
    root_operation_id: str,
) -> list[str]:
    """Order one root's exact owned lineage child-first, deepest child first.

    Membership is exact: only records under the same durable ``owner`` whose
    ``parent_operation_id`` chain reaches ``root_operation_id`` are included.
    Unknown roots fail closed through ``store.read``; a corrupt parent cycle
    fails closed rather than looping.
    """

    root = store.read(owner, root_operation_id)
    children: dict[str, list[str]] = {}
    for record in store.list(owner):
        parent = record.spec.parent_operation_id
        if parent:
            children.setdefault(parent, []).append(record.spec.operation_id)

    order: list[str] = []
    seen: set[str] = set()

    def walk(operation_id: str) -> None:
        if operation_id in seen:
            raise StoreError(f"operation lineage cycles at {operation_id}")
        seen.add(operation_id)
        for child in children.get(operation_id, ()):
            walk(child)
        order.append(operation_id)

    walk(root.spec.operation_id)
    return order


CASCADE_PARTIAL_EXIT = 3


@dataclass(frozen=True)
class CascadeOutcome:
    """Result of one supported root cancellation over its exact owned subtree."""

    root_operation_id: str
    root_state: str
    result: TransitionResult | None
    blocked: TransitionResult | None

    @property
    def complete(self) -> bool:
        return self.blocked is None


def _cancel_or_close_subtree(
    store: OperationStore,
    owner: str,
    root_operation_id: str,
    *,
    process_adapter: object,
    cmux_adapter: object,
    bounded_cancel: bool = False,
) -> CascadeOutcome:
    """Apply the supported per-operation cancellation child-first, then the root.

    Each per-operation call is its own durable boundary, so repeating the
    request is harmless and a crash mid-cascade resumes from the same command.
    A descendant that cannot reach a terminal state stops the cascade before the
    root, keeping the root honest about its still-live subtree; the outcome then
    reports the requested root together with the exact blocking descendant so
    the truncation is never mistaken for a completed cascade.
    """

    order = _exact_cancel_subtree(store, owner, root_operation_id)
    result = None
    for operation_id in order:
        result = _cancel_or_close(
            store,
            owner,
            operation_id,
            process_adapter=process_adapter,
            cmux_adapter=cmux_adapter,
            bounded_cancel=bounded_cancel,
        )
        if result.state not in TERMINAL and (
            operation_id != root_operation_id or bounded_cancel
        ):
            return CascadeOutcome(
                root_operation_id,
                store.read(owner, root_operation_id).state,
                None,
                result,
            )
    return CascadeOutcome(root_operation_id, result.state, result, None)


def _cascade_payload(outcome: CascadeOutcome) -> dict[str, object]:
    """Render one cascade outcome without hiding a truncated cancellation."""

    if outcome.complete:
        return to_dict(outcome.result)
    blocked = outcome.blocked
    return {
        "operation_id": outcome.root_operation_id,
        "status": "partial",
        "state": outcome.root_state,
        "blocked_operation_id": blocked.operation_id,
        "blocked_state": blocked.state,
        "blocked_attention_reason": (
            blocked.attention_reason.value
            if blocked.attention_reason is not None
            else None
        ),
    }


def _review_recovery_kind(
    gate: object, response_path: Path
) -> str:
    """Select exact callback recovery before the legacy response boundary."""

    if not isinstance(gate, dict):
        return ""
    attempt = gate.get("attempt")
    if (
        gate.get("status") == "reviewing"
        and gate.get("execution_protocol") == "exact-head-attempt-v1"
        and isinstance(attempt, dict)
        and attempt.get("status") == "awaiting-callback"
    ):
        return "accepted-exact-callbacks"
    terminal = attempt.get("terminal") if isinstance(attempt, dict) else None
    if zero_effect_gate_shape(
        gate, execution_protocol=EXACT_HEAD_ATTEMPT_PROTOCOL
    ):
        return "zero-lane-preflight"
    if (
        gate.get("status") in {"approved", "changes-requested"}
        and gate.get("execution_protocol") == "exact-head-attempt-v1"
        and isinstance(attempt, dict)
        and attempt.get("status") == "terminal"
        and isinstance(terminal, dict)
        and terminal.get("result") == gate.get("status")
    ):
        return "accepted-exact-callbacks"
    if not response_path.is_file():
        return ""
    if gate.get("status") in {
        "verifying",
        "recovery-verification-required",
        "fresh-boundary-authorized",
    }:
        return "legacy-finalizing"
    return ""


def _recover_finalizing_review_if_present(
    store: OperationStore,
    owner: str,
    operation_id: str,
    *,
    runtime_manager: object | None = None,
    cmux_adapter: object,
) -> str:
    """Recover one exact accepted review callback without starting a model."""

    store_root = store.root.expanduser().resolve()
    vault = store_root.parents[1]
    session_path = (
        store_root
        / "owners"
        / owner
        / "runtime"
        / operation_id
        / "session.json"
    )
    if not session_path.is_file() or session_path.is_symlink():
        return ""
    try:
        session = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeSessionError(
            "dispatch recovery session metadata is invalid"
        ) from exc
    if not isinstance(session, dict):
        raise RuntimeSessionError(
            "dispatch recovery session metadata is invalid"
        )
    worktree = Path(str(session.get("cwd") or "")).expanduser()
    response_path = worktree / ".task-verification-response.json"
    if (
        session.get("operation_id") != operation_id
        or not worktree.is_absolute()
    ):
        return ""
    gate_path = (
        vault
        / ".vault-meta"
        / "harness"
        / "review-data"
        / operation_id
        / operation_id
        / "review-gate.json"
    )
    if not gate_path.is_file() or gate_path.is_symlink():
        return ""
    try:
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeSessionError(
            "dispatch recovery review gate is invalid"
        ) from exc
    recovery_kind = _review_recovery_kind(gate, response_path)
    if not recovery_kind:
        return ""
    if (
        recovery_kind == "accepted-exact-callbacks"
        and gate.get("status") == "changes-requested"
        and _review_findings_transport_required(
            worktree=worktree,
            operation_id=operation_id,
            gate_state=gate,
        )
    ):
        try:
            _publish_recovered_review_resolution(
                store_root=store_root,
                owner=owner,
                operation_id=operation_id,
                worktree=worktree,
                gate_path=gate_path,
                gate_state=gate,
                cmux_adapter=cmux_adapter,
            )
        except Exception as exc:
            raise RuntimeSessionError(
                "dispatch review resolution transport recovery failed"
            ) from exc
        return "changes-requested"
    runner_path = Path(__file__).resolve().parents[1] / "task-review-runner.py"
    module_spec = importlib.util.spec_from_file_location(
        "_harness_task_review_recovery",
        runner_path,
    )
    if module_spec is None or module_spec.loader is None:
        raise RuntimeSessionError(
            "dispatch recovery implementation is unavailable"
        )
    module = importlib.util.module_from_spec(module_spec)
    try:
        module_spec.loader.exec_module(module)
        recover = getattr(
            module,
            (
                "run_task_review"
                if recovery_kind
                in {"accepted-exact-callbacks", "zero-lane-preflight"}
                else "recover_finalizing_review"
            ),
        )
        runtime = runtime_manager or RuntimeSessionManager.for_root(
            vault,
            store_root=store_root,
        )
        receipt = recover(worktree, runtime_manager=runtime)
    except Exception as exc:
        raise RuntimeSessionError(
            "dispatch finalizing review recovery failed"
        ) from exc
    if not isinstance(receipt, dict) or receipt.get("status") not in {
        "approved",
        "changes-requested",
        "verifying",
        "reviewing",
    }:
        raise RuntimeSessionError(
            "dispatch finalizing review recovery did not make bounded progress"
        )
    return str(receipt["status"])


def _review_findings_transport_required(
    *,
    worktree: Path,
    operation_id: str,
    gate_state: dict[str, object],
) -> bool:
    """Distinguish an undelivered finding packet from a completed handoff."""

    resolution_path = worktree / ".task-review-resolution.json"
    current_head = ""
    if resolution_path.is_file() and not resolution_path.is_symlink():
        try:
            resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            resolution = None
        if isinstance(resolution, dict):
            candidate = resolution.get("resolved_head_sha")
            if isinstance(candidate, str) and candidate:
                try:
                    current_head = _review_worktree_head(worktree)
                except (OSError, ValueError):
                    current_head = ""
    return not _review_resolution_handoff_ready(
        worktree=worktree,
        operation_id=operation_id,
        gate_state=gate_state,
        current_head=current_head,
    )


def _publish_recovered_review_resolution(
    *,
    store_root: Path,
    owner: str,
    operation_id: str,
    worktree: Path,
    gate_path: Path,
    gate_state: object,
    cmux_adapter: object,
) -> None:
    """Republish one terminal review generation without replaying review."""

    runtime_root = store_root / "owners" / owner / "runtime" / operation_id
    launch_path = runtime_root / "launch.json"
    summary_path = worktree / ".task-summary.json"
    if (
        not isinstance(gate_state, dict)
        or gate_state.get("status") != "changes-requested"
        or gate_path.name != "review-gate.json"
        or gate_path.parent.parent.parent.parent.resolve() != store_root.resolve()
        or launch_path.is_symlink()
        or not launch_path.is_file()
        or summary_path.is_symlink()
        or not summary_path.is_file()
    ):
        raise RuntimeSessionError(
            "dispatch review resolution transport identity is invalid"
        )
    launch = json.loads(launch_path.read_text(encoding="utf-8"))
    if (
        not isinstance(launch, dict)
        or launch.get("schema_version") != 1
        or launch.get("owner_id") != owner
        or launch.get("operation_id") != operation_id
        or Path(str(launch.get("cwd") or "")).expanduser().resolve()
        != worktree.resolve()
        or not isinstance(launch.get("surface_id"), str)
        or not launch["surface_id"]
    ):
        raise RuntimeSessionError(
            "dispatch review resolution launch identity is invalid"
        )
    summary_sha256 = hashlib.sha256(summary_path.read_bytes()).hexdigest()
    publish_review_resolution_transport(
        gate_state=gate_state,
        gate_root=gate_path.parent,
        worktree=worktree,
        operation_id=operation_id,
        surface_id=str(launch["surface_id"]),
        summary_sha256=summary_sha256,
        runtime_spec_path=launch_path,
        cmux_adapter=cmux_adapter,
    )


def _resume(
    store: OperationStore,
    owner: str,
    operation_id: str,
    *,
    process_adapter: object,
    cmux_adapter: object,
    review_runtime_manager: object | None = None,
) -> TransitionResult:
    """Restore the exact paused phase and drive cleanup without a model."""

    initial = store.read(owner, operation_id)
    if initial.pending_effect:
        return _attention(
            store,
            owner,
            operation_id,
            reason=AttentionReason.ATTENTION_REQUIRED,
        )
    recovery_status = ""
    if (
        initial.spec.kind == "dispatch"
        and initial.state in {"attention-required", "finalizing"}
    ):
        recovery_status = _recover_finalizing_review_if_present(
            store,
            owner,
            operation_id,
            runtime_manager=review_runtime_manager,
            cmux_adapter=cmux_adapter,
        )
        if recovery_status == "approved":
            current = store.read(owner, operation_id)
            if _has_owned_resources(current):
                if (
                    current.state != "attention-required"
                    or current.resume_state != "awaiting-callback"
                    or current.pending_effect
                ):
                    raise RuntimeSessionError(
                        "approved review recovery cannot resume dispatch ownership"
                    )
                resumed = store.transition(
                    owner, operation_id, current.resume_state
                )
                return TransitionResult(
                    operation_id,
                    initial.state,
                    resumed.state,
                    resumed.revision,
                    True,
                    resumed.attention_reason,
                )
            if (
                current.state not in {"attention-required", "finalizing"}
                or (
                    current.state == "attention-required"
                    and current.resume_state not in {"finalizing", "exiting"}
                )
                or current.pending_effect
                or _has_owned_resources(current)
            ):
                raise RuntimeSessionError(
                    "approved review recovery cannot terminalize dispatch ownership"
                )
            if current.state == "attention-required":
                store.transition(owner, operation_id, current.resume_state)
                current = store.read(owner, operation_id)
            if current.state == "finalizing":
                store.transition(owner, operation_id, "exiting")
            completed = store.transition(owner, operation_id, "complete")
            return TransitionResult(
                operation_id,
                initial.state,
                completed.state,
                completed.revision,
                True,
                completed.attention_reason,
            )
        if recovery_status == "changes-requested":
            current = store.read(owner, operation_id)
            if (
                current.state != "attention-required"
                or current.resume_state != "awaiting-callback"
                or current.pending_effect
                or not _has_owned_resources(current)
            ):
                raise RuntimeSessionError(
                    "review findings recovery cannot resume dispatch ownership"
                )
            resumed = store.transition(owner, operation_id, current.resume_state)
            return TransitionResult(
                operation_id,
                initial.state,
                resumed.state,
                resumed.revision,
                True,
                resumed.attention_reason,
            )
        if recovery_status:
            current = store.read(owner, operation_id)
            return TransitionResult(
                operation_id,
                initial.state,
                current.state,
                current.revision,
                current.revision != initial.revision,
                current.attention_reason,
            )
    if initial.state == "attention-required":
        if not initial.resume_state:
            return store.transition(owner, operation_id, initial.state)
        store.transition(owner, operation_id, initial.resume_state)
        current = store.read(owner, operation_id)
        if (
            current.state in {"running", "awaiting-callback", "verifying"}
            and current.deadline_at
            and current.deadline_at <= time()
        ):
            time_budget_seconds = _continuation_time_budget(store, current)
            if time_budget_seconds is None:
                return _attention(
                    store,
                    owner,
                    operation_id,
                    reason=AttentionReason.ATTENTION_REQUIRED,
                )
            OperationSupervisor(
                store, owner, operation_id
            ).begin_continuation(
                time_budget_seconds=time_budget_seconds
            )
    current = store.read(owner, operation_id)
    if current.state == "cancelling":
        return _cancel_or_close(
            store,
            owner,
            operation_id,
            process_adapter=process_adapter,
            cmux_adapter=cmux_adapter,
        )
    if current.state == "exiting":
        runtime = RuntimeSessionManager(
            store,
            cmux_adapter,
            process_adapter,
        )
        final = runtime.cleanup(owner, operation_id).record
        return TransitionResult(
            operation_id,
            initial.state,
            final.state,
            final.revision,
            final.revision != initial.revision,
            final.attention_reason,
        )
    return TransitionResult(
        operation_id,
        initial.state,
        current.state,
        current.revision,
        current.revision != initial.revision,
        current.attention_reason,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    process_adapter: object | None = None,
    cmux_adapter: object | None = None,
    review_runtime_manager: object | None = None,
    inventory_probe: object | None = None,
) -> int:
    args = parser().parse_args(argv)
    store = OperationStore(args.store)
    process_adapter = process_adapter or ProcessAdapter()
    cmux_adapter = cmux_adapter or CmuxAdapter()
    suppress_status_publish = False
    cascade_truncated = False
    try:
        if args.command in READ_ONLY_COMMANDS:
            value = execute_read_only(
                args,
                store,
                inventory_probe=inventory_probe,
            )
        elif args.command == "reconcile":
            value = []
            for row in store.list(args.owner):
                if row.state in TERMINAL:
                    continue
                operation_id = row.spec.operation_id
                if row.pending_effect:
                    _attention(
                        store,
                        args.owner,
                        operation_id,
                        reason=AttentionReason.ATTENTION_REQUIRED,
                    )
                    action = "inspect-pending-effect"
                elif _has_owned_resources(row):
                    decision = _reconcile_owned_resources(
                        store, row, process_adapter, cmux_adapter
                    )
                    if decision.action == "continue":
                        action = "resources-live"
                    elif decision.action == "complete":
                        _clear_owned_resources(store, args.owner, operation_id)
                        if row.state in {"cancelling", "exiting"}:
                            _cancel_or_close(
                                store,
                                args.owner,
                                operation_id,
                                process_adapter=process_adapter,
                                cmux_adapter=cmux_adapter,
                            )
                            action = (
                                "callback-complete"
                                if row.accepted_callback_id
                                else "cancel-complete"
                            )
                        else:
                            _attention(
                                store,
                                args.owner,
                                operation_id,
                                reason=(
                                    decision.reason
                                    or AttentionReason.CLEANUP_INCOMPLETE
                                ),
                            )
                            action = "resources-released"
                    else:
                        _attention(
                            store,
                            args.owner,
                            operation_id,
                            reason=AttentionReason.CLEANUP_INCOMPLETE,
                        )
                        action = "inspect-owned-resources"
                elif row.state in {"cancelling", "exiting"}:
                    _cancel_or_close(
                        store,
                        args.owner,
                        operation_id,
                        process_adapter=process_adapter,
                        cmux_adapter=cmux_adapter,
                    )
                    action = (
                        "callback-complete"
                        if row.accepted_callback_id
                        else "cancel-complete"
                    )
                else:
                    action = "none"
                current = store.read(args.owner, operation_id)
                value.append(
                    {
                        "operation_id": operation_id,
                        "state": current.state,
                        "action": action,
                    }
                )
        else:
            operation_id = args.operation_id
            record = store.read(args.owner, operation_id)
            value = None
            if args.command == "resume":
                result = _resume(
                    store,
                    args.owner,
                    operation_id,
                    process_adapter=process_adapter,
                    cmux_adapter=cmux_adapter,
                    review_runtime_manager=review_runtime_manager,
                )
            elif args.command in {"cancel", "close"}:
                outcome = _cancel_or_close_subtree(
                    store,
                    args.owner,
                    operation_id,
                    process_adapter=process_adapter,
                    cmux_adapter=cmux_adapter,
                    bounded_cancel=args.command == "cancel",
                )
                cascade_truncated = not outcome.complete
                value = _cascade_payload(outcome)
            if value is None:
                value = to_dict(result)
        _emit(value, json_mode=args.json)
        if (
            args.command in {"resume", "cancel", "close", "reconcile"}
            and not suppress_status_publish
        ):
            publish_status(
                args.store,
                trigger_owner=args.owner,
                trigger_operation=(
                    operation_id if args.command != "reconcile" else ""
                ),
            )
        return CASCADE_PARTIAL_EXIT if cascade_truncated else 0
    except (ContractError, RuntimeSessionError, StoreError) as exc:
        _emit({"status": "error", "reason": str(exc)}, json_mode=True)
        return 2
